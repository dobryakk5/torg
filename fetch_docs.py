"""
fetch_docs.py — ТРЕТЬЯ ФАЗА: скачивание документов лота и разбор требований.

Зачем: содержательные требования к участнику (опыт, лицензии, СРО, доптребования
по ч. 2 ст. 31 / ПП РФ 2571, квалификация, обеспечение заявки) лежат ВНУТРИ
документов (информационная карта, извещение, ТЗ, проект контракта), а не на
странице common-info, где только формулярные ссылки ст. 31 / ст. 14.

Что делает:
    • берёт лоты с решением ≠ NO-GO (NO-GO документы НЕ качаем);
    • открывает вкладку «Документы» лота, скачивает файлы (docx/pdf/xlsx/zip);
    • извлекает текст, достаёт финансовые условия и требования к участнику;
    • пишет в БД: document_count/documents_dir/documents_hash/document_text_excerpt,
      обеспечение/аванс/срок, а требования — в details_json.doc_requirements
      (+ обновляет requirements_special).

По умолчанию пропускает лоты, у которых документы уже скачаны (--refresh — перекачать).
Документы есть только у ЕИС; 223-ФЗ обычно за логином — такие лоты просто дадут 0 файлов.

Использование:
    python fetch_docs.py                 # все score >= 30 и не-NO-GO без документов
    python fetch_docs.py --law 44-ФЗ     # только 44-ФЗ
    python fetch_docs.py --only GO       # только GO (или CAUTION)
    python fetch_docs.py --min-score 32  # другой порог скоринга
    python fetch_docs.py --limit 20      # не больше 20 лотов за прогон
    python fetch_docs.py --refresh       # перекачать даже если документы уже есть
    python fetch_docs.py --dry-run       # показать, что бы скачали, без сети/записи
"""

from __future__ import annotations

import argparse
import logging
import sys

import config
import database as db
from document_processor import (
    download_documents, collect_document_text, extract_financial_terms,
    extract_participant_requirements, hash_files,
)
from filter_engine import run_stage2_filters
from scraper import (
    get_tender_page,
    parse_common_info_details,
    parse_common_info_details_from_html,
    to_common_info_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("fetch_docs")


def select_lots(
    law: str | None,
    only: str | None,
    refresh: bool,
    limit: int | None,
    min_score: int,
) -> list[dict]:
    import psycopg2.extras
    conds = [
        "filter_decision IS DISTINCT FROM 'NO-GO'",   # NO-GO не качаем
        "filter_total >= %s",
    ]
    params: list = [min_score]
    if law:
        conds.append("law_type = %s"); params.append(law)
    if only:
        conds.append("filter_decision = %s"); params.append(only.upper())
    if not refresh:
        conds.append("COALESCE(document_count, 0) = 0")     # только те, где доков ещё нет
    sql = "SELECT * FROM tenders WHERE " + " AND ".join(conds) + " ORDER BY filter_total DESC NULLS LAST"
    if limit:
        sql += " LIMIT %s"; params.append(limit)
    with db._conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def deep_merge(base: dict, extra: dict) -> dict:
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_page_details(html: str, page_text: str, price: float | None) -> dict:
    details = parse_common_info_details(page_text, price=price)
    dom_details = parse_common_info_details_from_html(html, price=price)
    return deep_merge(details, dom_details) if dom_details else details


def apply_terms(tender: dict, terms: dict) -> None:
    for key, value in (terms or {}).items():
        if value not in (None, ""):
            tender[key] = value


def save_full_refresh(tender: dict, full_text: str, details: dict, requirements: list[dict]) -> tuple[int, str]:
    pnum = tender.get("purchase_number", "")
    details = deep_merge(db.get_tender_details(pnum) or {}, details)
    details["doc_requirements"] = requirements
    details["requirements_special"] = bool(requirements)
    db.save_tender_details(pnum, details)

    filter_result = run_stage2_filters(tender, full_text)
    tender["filter_decision"] = filter_result.decision
    tender["filter_scores"] = filter_result.to_filter_scores()
    tender["filter_stop"] = " | ".join(filter_result.stop_factors)

    db.save_detail(
        tender,
        filter_result.total_score,
        filter_result.to_reasons(),
        llm_analysis=tender.get("llm_analysis") or "",
        document_text=full_text,
        notified=False,
    )
    db.save_filter_result(filter_result, stage="stage2")
    return filter_result.total_score, filter_result.decision


def main() -> None:
    ap = argparse.ArgumentParser(description="Третья фаза: документы + требования (score >= 30, кроме NO-GO)")
    ap.add_argument("--law", default=None, help="Фильтр по закону (например 44-ФЗ)")
    ap.add_argument("--only", default=None, help="Только решение GO или CAUTION")
    ap.add_argument("--min-score", type=int, default=config.DOCUMENT_DOWNLOAD_MIN_SCORE,
                    help="Минимальный filter_total для скачивания документов")
    ap.add_argument("--limit", type=int, default=None, help="Максимум лотов за прогон")
    ap.add_argument("--refresh", action="store_true", help="Перекачать даже если документы уже есть")
    ap.add_argument("--dry-run", action="store_true", help="Не качать/не писать — только список")
    args = ap.parse_args()

    db.connect_db()
    db.check_db()
    db.ensure_extra_columns()

    lots = select_lots(args.law, args.only, args.refresh, args.limit, args.min_score)
    logger.info("Лотов к обработке (score >= %d, не-NO-GO%s%s): %d%s",
                args.min_score,
                f", закон {args.law}" if args.law else "",
                f", {args.only}" if args.only else "",
                len(lots), " [dry-run]" if args.dry_run else "")

    n_docs = n_reqs = n_empty = 0
    for i, t in enumerate(lots, 1):
        pnum = t["purchase_number"]
        page_url = to_common_info_url(t.get("url", "") or pnum)
        if args.dry_run:
            logger.info("  [%d/%d] %s (%s, score %s) → %s",
                        i, len(lots), pnum, t.get("filter_decision"),
                        t.get("filter_total"), page_url)
            continue
        try:
            html, page_text = get_tender_page(page_url)
            db.reconnect_db()
            if not html:
                logger.warning("  [%d/%d] %s: карточка закупки не открылась", i, len(lots), pnum)
                continue
            t["url"] = page_url
            details = parse_page_details(html, page_text, t.get("price"))

            docs = download_documents(pnum, html, page_url)
            files = docs.get("files", [])
            if not files:
                n_empty += 1
                logger.info("  [%d/%d] %s (%s): документов не найдено", i, len(lots), pnum, t.get("filter_decision"))
            text = collect_document_text(files, config.MAX_DOCUMENT_TEXT_CHARS)
            full_text = "\n".join(
                part for part in (
                    str(t.get("primary_text") or ""),
                    page_text or "",
                    text or "",
                ) if part
            )
            terms = extract_financial_terms(full_text)
            apply_terms(t, terms)
            reqs = extract_participant_requirements(full_text)
            t["document_count"] = len(files)
            t["documents_dir"] = docs.get("dir", "")
            t["documents_hash"] = hash_files(files)

            score, decision = save_full_refresh(t, full_text, details, reqs)
            if files:
                n_docs += 1
            if reqs:
                n_reqs += 1
            logger.info("  [%d/%d] %s: файлов %d, требований %d, скор %d/%s %s",
                        i, len(lots), pnum, len(files), len(reqs), score, decision,
                        "[" + ", ".join(r["type"] for r in reqs) + "]" if reqs else "")
        except Exception as exc:
            logger.error("  [%d/%d] %s: ошибка — %s", i, len(lots), pnum, exc)

    logger.info("Готово. Обновлено с файлами: %d · с требованиями: %d · без файлов: %d",
                n_docs, n_reqs, n_empty)
    db.close_db()


if __name__ == "__main__":
    main()
