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
import json
import logging
import sys

import config
import database as db
from document_processor import (
    download_documents, collect_document_text, extract_financial_terms,
    extract_participant_requirements, hash_files,
)
from scraper import get_tender_page, to_common_info_url

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


def save_scan(pnum: str, *, count: int, docs_dir: str, docs_hash: str,
              text: str, terms: dict, requirements: list[dict]) -> None:
    """Пишет результаты скана документов в БД + мёржит требования в details_json."""
    with db._conn() as c:
        cur = c.cursor()
        cur.execute(
            """
            UPDATE tenders SET
                document_count = %s, documents_dir = %s, documents_hash = %s,
                document_text_excerpt = %s,
                application_security_amount = COALESCE(%s, application_security_amount),
                contract_security_amount   = COALESCE(%s, contract_security_amount),
                warranty_security_amount   = COALESCE(%s, warranty_security_amount),
                advance_percent            = COALESCE(%s, advance_percent),
                payment_terms              = COALESCE(NULLIF(%s,''), payment_terms),
                execution_days             = COALESCE(%s, execution_days),
                updated_at = NOW()
            WHERE purchase_number = %s
            """,
            (count, docs_dir, docs_hash, (text or "")[:4000],
             terms.get("application_security_amount"), terms.get("contract_security_amount"),
             terms.get("warranty_security_amount"), terms.get("advance_percent"),
             terms.get("payment_terms", ""), terms.get("execution_days"), pnum),
        )
    # требования — в details_json (мёрж, не перетираем остальные блоки)
    details = db.get_tender_details(pnum) or {}
    details["doc_requirements"] = requirements
    if requirements:
        details["requirements_special"] = True
    db.save_tender_details(pnum, details)


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
            html, _ = get_tender_page(page_url)
            db.reconnect_db()
            if not html:
                logger.warning("  [%d/%d] %s: карточка закупки не открылась", i, len(lots), pnum)
                continue
            docs = download_documents(pnum, html, page_url)
            files = docs.get("files", [])
            if not files:
                n_empty += 1
                logger.info("  [%d/%d] %s (%s): документов не найдено", i, len(lots), pnum, t.get("filter_decision"))
                # всё равно отметим, что скан был — пустой
                save_scan(pnum, count=0, docs_dir=docs.get("dir", ""), docs_hash="",
                          text="", terms={}, requirements=[])
                continue
            text = collect_document_text(files, config.MAX_DOCUMENT_TEXT_CHARS)
            terms = extract_financial_terms(text)
            reqs = extract_participant_requirements(text)
            save_scan(pnum, count=len(files), docs_dir=docs.get("dir", ""),
                      docs_hash=hash_files(files), text=text, terms=terms, requirements=reqs)
            n_docs += 1
            if reqs:
                n_reqs += 1
            logger.info("  [%d/%d] %s (%s): файлов %d, требований %d %s",
                        i, len(lots), pnum, t.get("filter_decision"), len(files), len(reqs),
                        "[" + ", ".join(r["type"] for r in reqs) + "]" if reqs else "")
        except Exception as exc:
            logger.error("  [%d/%d] %s: ошибка — %s", i, len(lots), pnum, exc)

    logger.info("Готово. С документами: %d · из них с требованиями: %d · пустых: %d",
                n_docs, n_reqs, n_empty)
    db.close_db()


if __name__ == "__main__":
    main()
