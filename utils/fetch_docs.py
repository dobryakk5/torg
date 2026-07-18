"""
fetch_docs.py — ТРЕТЬЯ ФАЗА: скачивание документов лота и разбор требований.

Зачем: содержательные требования к участнику (опыт, лицензии, СРО, доптребования
по ч. 2 ст. 31 / ПП РФ 2571, квалификация, обеспечение заявки) лежат ВНУТРИ
документов (информационная карта, извещение, ТЗ, проект контракта), а не на
странице common-info, где только формулярные ссылки ст. 31 / ст. 14.

Что делает по явным флагам:
    • --info N: заново открывает карточку лота, обновляет details_json,
      финансовые условия и пересчитывает Stage2 для лотов с filter_total >= N;
    • --docs N: открывает вкладку «Документы», скачивает файлы (docx/pdf/xlsx/zip),
      извлекает текст и требования для лотов с filter_total >= N.

Если не указан ни --info, ни --docs, скрипт ничего не обновляет.
Документы есть только у ЕИС; недоступные вкладки просто дадут 0 файлов.

Использование:
    python fetch_docs.py --info 30 --docs 33
    python fetch_docs.py --law 44-ФЗ     # только 44-ФЗ
    python fetch_docs.py --only GO       # только GO (или CAUTION)
    python fetch_docs.py --limit 20      # не больше 20 лотов за прогон
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
    extract_object_description_items, extract_participant_requirements,
    extract_work_scope, extract_work_scope_from_files, hash_files,
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
    limit: int | None,
    info_score: int | None,
    docs_score: int | None,
    numbers: list[str] | None = None,
) -> list[dict]:
    import psycopg2.extras
    score_parts: list[str] = []
    params: list = []
    if info_score is not None:
        score_parts.append("filter_total >= %s")
        params.append(info_score)
    if docs_score is not None:
        score_parts.append("filter_total >= %s")
        params.append(docs_score)
    if not score_parts:
        return []

    conds = ["(" + " OR ".join(score_parts) + ")"]
    if law:
        conds.append("law_type = %s"); params.append(law)
    if only:
        conds.append("filter_decision = %s"); params.append(only.upper())
    if numbers:
        conds.append("purchase_number = ANY(%s)")
        params.append(numbers)
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


def strip_nul(value):
    """PostgreSQL text/json fields cannot contain NUL bytes."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: strip_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [strip_nul(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_nul(item) for item in value)
    return value


def parse_page_details(html: str, page_text: str, price: float | None) -> dict:
    details = parse_common_info_details(page_text, price=price)
    dom_details = parse_common_info_details_from_html(html, price=price)
    return strip_nul(deep_merge(details, dom_details) if dom_details else details)


def apply_terms(tender: dict, terms: dict) -> None:
    for key, value in (terms or {}).items():
        if value not in (None, ""):
            tender[key] = value


def save_doc_refresh(
    tender: dict,
    doc_text: str,
    requirements: list[dict],
    work_scope: dict | None = None,
) -> dict:
    pnum = tender.get("purchase_number", "")
    details = deep_merge(db.get_tender_details(pnum) or {}, {})
    requirements = strip_nul(requirements)
    details["doc_requirements"] = requirements
    details["requirements_special"] = bool(requirements)
    if work_scope:
        details["work_scope"] = strip_nul(work_scope)
    db.save_tender_details(pnum, strip_nul(details))
    tender = strip_nul(tender)
    doc_text = strip_nul(doc_text or "")

    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders SET
                document_count = %s,
                documents_dir = %s,
                documents_hash = %s,
                document_text_excerpt = %s,
                application_security_amount = COALESCE(%s, application_security_amount),
                contract_security_amount = COALESCE(%s, contract_security_amount),
                warranty_security_amount = COALESCE(%s, warranty_security_amount),
                advance_percent = COALESCE(%s, advance_percent),
                payment_terms = COALESCE(NULLIF(%s, ''), payment_terms),
                execution_days = COALESCE(%s, execution_days),
                updated_at = NOW()
            WHERE purchase_number = %s
            """,
            (
                tender.get("document_count", 0),
                tender.get("documents_dir", ""),
                tender.get("documents_hash", ""),
                (doc_text or "")[:4000],
                tender.get("application_security_amount"),
                tender.get("contract_security_amount"),
                tender.get("warranty_security_amount"),
                tender.get("advance_percent"),
                tender.get("payment_terms", ""),
                tender.get("execution_days"),
                pnum,
            ),
        )
    return details


def save_info_refresh(
    tender: dict,
    full_text: str,
    details: dict,
    requirements: list[dict] | None,
) -> tuple[int, str]:
    pnum = tender.get("purchase_number", "")
    details = deep_merge(db.get_tender_details(pnum) or {}, details)
    if requirements is not None:
        requirements = strip_nul(requirements)
        details["doc_requirements"] = requirements
        details["requirements_special"] = bool(requirements)
    db.save_tender_details(pnum, strip_nul(details))

    tender = strip_nul(tender)
    full_text = strip_nul(full_text or "")
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
    ap = argparse.ArgumentParser(description="Refresh info/docs for non-NO-GO tenders by score thresholds")
    ap.add_argument("--law", default=None, help="Фильтр по закону (например 44-ФЗ)")
    ap.add_argument("--only", default=None, help="Только решение GO или CAUTION")
    ap.add_argument("--info", type=int, default=None,
                    help="Обновить карточку/details/скоринг для filter_total >= N")
    ap.add_argument("--docs", type=int, default=None,
                    help="Скачать документы и требования для filter_total >= N")
    ap.add_argument("--numbers", default="",
                    help="Только эти номера закупок, через запятую")
    ap.add_argument("--min-score", type=int, default=None,
                    help="Устаревший alias: если --info/--docs не указаны, задаёт оба порога")
    ap.add_argument("--limit", type=int, default=None, help="Максимум лотов за прогон")
    ap.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true", help="Не качать/не писать — только список")
    args = ap.parse_args()

    if args.min_score is not None and args.info is None and args.docs is None:
        args.info = args.min_score
        args.docs = args.min_score

    if args.info is None and args.docs is None:
        logger.info("Нечего делать: укажи --info N и/или --docs N")
        return

    db.connect_db()
    db.check_db()
    db.ensure_extra_columns()

    numbers = [n.strip() for n in (args.numbers or "").split(",") if n.strip()]
    lots = select_lots(args.law, args.only, args.limit, args.info, args.docs, numbers=numbers)
    logger.info("Лотов к обработке (info >= %s, docs >= %s, не-NO-GO%s%s): %d%s",
                args.info if args.info is not None else "—",
                args.docs if args.docs is not None else "—",
                f", закон {args.law}" if args.law else "",
                f", {args.only}" if args.only else "",
                len(lots), " [dry-run]" if args.dry_run else "")

    n_info = n_docs = n_reqs = n_empty = 0
    for i, t in enumerate(lots, 1):
        pnum = t["purchase_number"]
        page_url = to_common_info_url(t.get("url", "") or pnum)
        score_before = int(t.get("filter_total") or 0)
        do_info = args.info is not None and score_before >= args.info
        do_docs = args.docs is not None and score_before >= args.docs
        if args.dry_run:
            actions = ", ".join(a for a, enabled in (("info", do_info), ("docs", do_docs)) if enabled)
            logger.info("  [%d/%d] %s (%s, score %s, %s) → %s",
                        i, len(lots), pnum, t.get("filter_decision"),
                        t.get("filter_total"), actions or "skip", page_url)
            continue
        try:
            html, page_text = get_tender_page(page_url)
            page_text = strip_nul(page_text or "")
            db.reconnect_db()
            if not html:
                logger.warning("  [%d/%d] %s: карточка закупки не открылась", i, len(lots), pnum)
                continue
            t["url"] = page_url
            details = parse_page_details(html, page_text, t.get("price")) if do_info else {}
            files = []
            doc_text = ""
            reqs: list[dict] | None = None

            if do_docs:
                docs = download_documents(pnum, html, page_url)
                files = docs.get("files", [])
                if not files:
                    n_empty += 1
                    logger.info("  [%d/%d] %s (%s): документов не найдено", i, len(lots), pnum, t.get("filter_decision"))
                doc_text = strip_nul(collect_document_text(files, config.MAX_DOCUMENT_TEXT_CHARS))
                t["document_count"] = len(files)
                t["documents_dir"] = docs.get("dir", "")
                t["documents_hash"] = hash_files(files)
            else:
                doc_text = strip_nul(str(t.get("document_text_excerpt") or ""))

            full_text = strip_nul("\n".join(
                part for part in (
                    str(t.get("primary_text") or ""),
                    page_text or "",
                    doc_text or "",
                ) if part
            ))
            terms_source = full_text if do_info else doc_text
            terms = extract_financial_terms(terms_source)
            apply_terms(t, terms)

            if do_docs:
                reqs = extract_participant_requirements(full_text)
                work_scope = extract_work_scope_from_files(files) or extract_work_scope(doc_text)
                saved_details = save_doc_refresh(t, doc_text, reqs, work_scope)
                t["details_json"] = saved_details
                spec = extract_object_description_items(files)
                if spec.get("items") and not db.list_tender_items(pnum):
                    db.replace_tender_items(pnum, spec["items"])
                if files:
                    n_docs += 1
                if reqs:
                    n_reqs += 1

            score = score_before
            decision = t.get("filter_decision") or ""
            if do_info:
                score, decision = save_info_refresh(t, full_text, details, reqs)
                n_info += 1
            elif do_docs:
                score, decision = save_info_refresh(t, full_text, {}, reqs)

            logger.info(
                "  [%d/%d] %s: info=%s docs=%s файлов %d, требований %d, скор %d/%s %s",
                i, len(lots), pnum,
                "yes" if do_info else "no",
                "yes" if do_docs else "no",
                len(files),
                len(reqs or []),
                score,
                decision,
                "[" + ", ".join(r["type"] for r in (reqs or [])) + "]" if reqs else "",
            )
        except Exception as exc:
            logger.error("  [%d/%d] %s: ошибка — %s", i, len(lots), pnum, exc)

    logger.info("Готово. Info: %d · Docs с файлами: %d · с требованиями: %d · без файлов: %d",
                n_info, n_docs, n_reqs, n_empty)
    db.close_db()


if __name__ == "__main__":
    main()
