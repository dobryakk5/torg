"""
main.py — двухэтапный тендерный монитор.

Этап 1: массово собирает карточки закупок из ЕИС, делает первичный скоринг по описанию,
        сохраняет ссылку и метаданные. Документы не скачиваются.
Этап 2: берет только закупки с высоким первичным скором, скачивает ТЗ/документы,
        извлекает условия, делает детальный скоринг и отправляет лучшие в Telegram.
Этап 3: после дедлайна подтягивает результаты/протоколы для аналитики конкуренции.

Запуск:
    python main.py --stage1   # только массовая подгрузка карточек
    python main.py --stage2   # только детальный анализ кандидатов
    python main.py --once     # stage1 + stage2
    python main.py --test     # stage1 + stage2 без отправки в Telegram
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import schedule
except ImportError:  # schedule нужен только для daemon-режима
    schedule = None

import config
import database as db
from document_processor import (
    collect_document_text,
    download_documents,
    extract_financial_terms,
    extract_object_description_items,
    extract_work_scope_from_files,
    find_document_items,
    hash_files,
)
from llm_analyzer import analyze_tender
from notifier import format_tender_message, send_startup_message, send_summary, send_tender_message
from scraper import (
    get_tender_page, parse_result_info, search_eis, search_eis_by_okpd2,
    to_common_info_url, to_documents_url,
)
from winner_analytics   import run_update   as run_winner_update, classify_category, recommend_bid
from customer_scorer    import run_new_customers, run_refresh_customers, get_customer_risk_label
from change_detector    import check_once   as check_changes
from filter_engine import run_stage1_filters, run_stage2_filters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

AUTO_ACTIVE_DATE_VALUES = {"auto", "active", "all-active", "активные", "авто"}


def _stage1_period_label(
    days_back: int | None,
    date_from: str | None = None,
    date_to: str | None = None,
    auto_active: bool = False,
) -> str:
    if auto_active:
        return "all-active"
    if date_from:
        return f"{date_from}-{date_to}" if date_to else f"from-{date_from}"
    return "all-active" if days_back is not None and days_back <= 0 else f"{days_back or config.PUBLISH_DAYS_BACK}d"


def _is_auto_active_date(value: str | None) -> bool:
    return str(value or "").strip().lower() in AUTO_ACTIVE_DATE_VALUES


def _stage1_phase_mode(
    kind: str,
    value: str,
    days_back: int | None,
    pages: int,
    date_from: str | None = None,
    date_to: str | None = None,
    auto_active: bool = False,
) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    laws = "".join([("44" if config.SEARCH_44FZ else ""), ("223" if config.SEARCH_223FZ else "")])
    return (
        f"stage1:{kind}:{value}:period={_stage1_period_label(days_back, date_from, date_to, auto_active)}:pages={pages}:"
        f"price={config.PRICE_MIN}-{config.PRICE_MAX}:fz={laws or 'none'}"
    )


def _process_stage1_tenders(
    tenders: list[dict],
    matched_keyword: str,
    dry_run: bool,
    seen_this_run: set[str],
) -> tuple[int, int, int]:
    saved = 0
    newly_counted = 0
    primary_candidates = 0

    for tender in tenders:
        pnum = tender.get("purchase_number", "")
        if not pnum:
            continue
        if db.deadline_is_expired(tender.get("deadline")):
            logger.info(
                "Stage1 skip expired deadline: %s, deadline=%s, %s",
                pnum,
                tender.get("deadline") or "—",
                str(tender.get("title", "?"))[:80],
            )
            continue

        matched_keywords = sorted(set(tender.get("matched_keywords") or []) | {matched_keyword})
        tender["matched_keywords"] = matched_keywords
        primary_text = tender.get("primary_text", "")
        filter_result = run_stage1_filters(tender, primary_text)
        primary_score = filter_result.total_score
        primary_reasons = filter_result.to_reasons()
        tender["filter_decision"] = filter_result.decision
        tender["filter_scores"] = filter_result.to_filter_scores()
        tender["filter_stop"] = " | ".join(filter_result.stop_factors)

        customer_inn = tender.get("customer_inn", "")
        risk_label = get_customer_risk_label(customer_inn)
        if risk_label:
            logger.info("Риск заказчика %s: %s", customer_inn, risk_label)
            tender["customer_risk_label"] = risk_label

        result = db.upsert_primary(tender, primary_score, primary_reasons, matched_keywords)
        db.save_filter_result(filter_result, stage="stage1")
        saved += 1

        first_in_run = pnum not in seen_this_run
        if first_in_run:
            seen_this_run.add(pnum)
            newly_counted += 1
            if primary_score >= config.MIN_PRIMARY_SCORE_FOR_DETAIL:
                primary_candidates += 1

        logger.info(
            "Stage1 %s: скор %d, %s (%s)",
            result,
            primary_score,
            str(tender.get("title", "?"))[:80],
            pnum,
        )

        if dry_run and first_in_run and primary_score >= config.MIN_PRIMARY_SCORE_FOR_DETAIL:
            print("\n" + "─" * 50)
            print(f"PRIMARY SCORE: {primary_score}")
            print(re.sub(r"<[^>]+>", "", format_tender_message(tender, primary_score, primary_reasons, None)))

    return saved, newly_counted, primary_candidates


def run_stage1(
    dry_run: bool = False,
    skip_completed_today: bool = False,
    backfill_active: bool = False,
    keywords: list[str] | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    days_back: int | None = None,
    fz44: bool | None = None,
    fz223: bool | None = None,
    okpd2: bool | None = None,
    b2b: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[int, int]:
    """
    Массовая подгрузка карточек и первичный скоринг без ТЗ.

    Override-параметры позволяют запускать Stage 1 с настройками из веб-панели.
    Если параметр не передан, используется текущее значение из config/settings.
    """
    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("═" * 50)

    errors: list[str] = []
    seen_this_run: set[str] = set()
    saved = 0
    unique_saved = 0
    primary_candidates = 0

    kw_list = keywords if keywords is not None else config.SEARCH_KEYWORDS
    p_min = price_min if price_min is not None else config.PRICE_MIN
    p_max = price_max if price_max is not None else config.PRICE_MAX
    d_back = days_back if days_back is not None else (0 if backfill_active else config.PUBLISH_DAYS_BACK)
    d_from = None if backfill_active else (date_from if date_from is not None else getattr(config, "PUBLISH_DATE_FROM", ""))
    d_to = None if backfill_active else (date_to if date_to is not None else getattr(config, "PUBLISH_DATE_TO", ""))
    d_from = str(d_from or "").strip() or None
    d_to = str(d_to or "").strip() or None
    auto_active = _is_auto_active_date(d_from)
    if auto_active:
        d_back = 0
        d_from = None
        d_to = None
    use_44 = fz44 if fz44 is not None else config.SEARCH_44FZ
    use_223 = fz223 if fz223 is not None else config.SEARCH_223FZ
    use_okpd2 = okpd2 if okpd2 is not None else getattr(config, "OKPD2_SEARCH_ENABLED", True)
    use_b2b = b2b if b2b is not None else getattr(config, "SOURCE_B2B_ENABLED", False)
    pages = config.BACKFILL_SEARCH_PAGES if (backfill_active or auto_active) else config.SEARCH_PAGES

    logger.info(
        "Этап 1: поиск карточек (%d ключей, цена %s–%s ₽, период=%s, 44-ФЗ=%s, 223-ФЗ=%s, ОКПД2=%s)",
        len(kw_list), f"{p_min:,}", f"{p_max:,}", _stage1_period_label(d_back, d_from, d_to, auto_active), use_44, use_223, use_okpd2,
    )

    # ── Канал 1: поиск по ключевым словам ──────────────────────────────────
    for keyword in kw_list:
        phase_started_at = datetime.now().isoformat(timespec="seconds")
        phase_mode = _stage1_phase_mode("keyword", keyword, d_back, pages, d_from, d_to, auto_active)
        if skip_completed_today and db.was_stage_completed_today(phase_mode):
            logger.info("Поиск '%s' уже готов сегодня — пропускаю", keyword)
            continue
        try:
            tenders = search_eis(
                keyword=keyword,
                price_from=p_min,
                price_to=p_max,
                fz44=use_44,
                fz223=use_223,
                pages=pages,
                days_back=d_back,
                date_from=d_from,
                date_to=d_to,
            )
            db.reconnect_db()
            phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                tenders,
                keyword,
                dry_run,
                seen_this_run,
            )
            saved += phase_saved
            unique_saved += phase_unique
            primary_candidates += phase_candidates
            db.log_run(phase_mode, phase_started_at, found=len(tenders), processed=phase_saved, notified=0, errors="")
        except Exception as exc:
            msg = f"Ошибка поиска по '{keyword}': {exc}"
            logger.error(msg)
            errors.append(msg)
            continue

    # ── Канал 2: поиск по ОКПД2 (параллельный, ловит без ключевых слов) ───
    if use_okpd2 and config.OKPD2_CODES:
        phase_started_at = datetime.now().isoformat(timespec="seconds")
        okpd2_value = ",".join(config.OKPD2_CODES)
        phase_mode = _stage1_phase_mode("okpd2", okpd2_value, d_back, pages, d_from, d_to, auto_active)
        if skip_completed_today and db.was_stage_completed_today(phase_mode):
            logger.info("ОКПД2-поиск %s уже готов сегодня — пропускаю", config.OKPD2_CODES)
        else:
            before_unique = len(seen_this_run)
            try:
                okpd2_tenders = search_eis_by_okpd2(
                    okpd2_codes=config.OKPD2_CODES,
                    price_from=p_min,
                    price_to=p_max,
                    fz44=use_44,
                    fz223=use_223,
                    pages=pages,
                    days_back=d_back,
                    date_from=d_from,
                    date_to=d_to,
                )
                db.reconnect_db()
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    okpd2_tenders,
                    "okpd2",
                    dry_run,
                    seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                logger.info("ОКПД2-канал добавил %d новых тендеров", len(seen_this_run) - before_unique)
                db.log_run(phase_mode, phase_started_at, found=len(okpd2_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                logger.error("Ошибка ОКПД2-поиска: %s", exc)
                errors.append(f"ОКПД2: {exc}")

    # ── Канал 3: B2B-Center (коммерческие закупки и 223-ФЗ вне ЕИС) ──────────
    if use_b2b:
        from sources.b2b_center import search_b2b
        b2b_pages = getattr(config, "B2B_SEARCH_PAGES", 1)
        for keyword in kw_list:
            phase_started_at = datetime.now().isoformat(timespec="seconds")
            kw_clean = re.sub(r"\s+", " ", keyword).strip()
            phase_mode = f"stage1:b2b:{kw_clean}:pages={b2b_pages}"
            if skip_completed_today and db.was_stage_completed_today(phase_mode):
                logger.info("B2B-поиск '%s' уже готов сегодня — пропускаю", keyword)
                continue
            try:
                b2b_tenders = search_b2b(keyword, price_from=p_min, price_to=p_max, pages=b2b_pages)
                db.reconnect_db()
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    b2b_tenders, f"b2b:{keyword}", dry_run, seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                db.log_run(phase_mode, phase_started_at, found=len(b2b_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                msg = f"Ошибка B2B-поиска по '{keyword}': {exc}"
                logger.error(msg)
                errors.append(msg)
                continue

    logger.info("Этап 1 завершён: найдено %d, кандидатов на этап 2: %d", unique_saved, primary_candidates)
    db.log_run("stage1", started_at, found=unique_saved, processed=saved, notified=0, errors="; ".join(errors))
    return unique_saved, primary_candidates


def run_triage(dry_run: bool = False, limit: int | None = None) -> tuple[int, int]:
    """Stage 1.5 — LLM-триаж карточек.

    Дешёвый массовый проход по собранным карточкам: классифицирует пригодность
    (БЕРУ/ЧАСТИЧНО/МИМО), ловит перекуп лицензий/железа и ложные совпадения
    ключевых слов. Результат сохраняется и мягко корректирует Ф1 (профиль).
    """
    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("═" * 50)

    if not getattr(config, "LLM_TRIAGE_ENABLED", True):
        logger.info("LLM-триаж выключен (LLM_TRIAGE_ENABLED=0) — пропускаю")
        return 0, 0

    import llm_provider
    from llm_analyzer import triage_tender

    if not llm_provider.is_configured():
        logger.warning(
            "LLM-триаж пропущен: не задан ключ провайдера %s "
            "(OPENROUTER_API_KEY/ANTHROPIC_API_KEY)", llm_provider.provider(),
        )
        return 0, 0

    limit = limit or getattr(config, "LLM_TRIAGE_MAX_CARDS", 300)
    model = llm_provider.triage_model()
    candidates = db.get_triage_candidates(limit=limit, only_new=True)
    logger.info("Stage 1.5 LLM-триаж: карточек к разметке %d (модель %s)", len(candidates), model)

    processed = 0
    taken = 0
    errors: list[str] = []

    for tender in candidates:
        pnum = tender.get("purchase_number", "")
        try:
            triage = triage_tender(tender)
            if not triage:
                continue
            db.save_triage(pnum, triage, model)
            tender["llm_triage"] = triage
            filter_result = run_stage1_filters(tender, tender.get("primary_text", "") or "")
            db.update_stage1_after_triage(filter_result)
            processed += 1
            if triage.get("verdict") == "БЕРУ":
                taken += 1
            mark = "/перекуп" if triage.get("resale") else ""
            line = f"[{triage.get('verdict')}/{triage.get('fit')}{mark}] {str(tender.get('title',''))[:80]} — {triage.get('reason','')}"
            if dry_run:
                print(line)
            logger.info("Триаж %s", line)
        except Exception as exc:
            msg = f"Ошибка триажа {pnum}: {exc}"
            logger.warning(msg)
            errors.append(msg)

    logger.info("Stage 1.5 завершён: размечено %d, из них БЕРУ %d", processed, taken)
    db.log_run("triage", started_at, found=len(candidates), processed=processed, notified=0, errors="; ".join(errors))
    return processed, taken


def run_rescore(dry_run: bool = False, limit: int | None = None) -> tuple[int, int]:
    """Разовый пересчёт скоринга всех существующих лотов по текущему движку.

    БЕЗ обращения к сети и БЕЗ LLM — только по уже сохранённому тексту карточек/ТЗ.
    Нужен после правок filter_engine (ценовой коридор, страж перекупа, нейтрализация
    Stage 1 и т.п.), чтобы дашборд сразу отразил новые оценки по всей базе.
    Уже проставленные LLM-триаж-вердикты сохраняются и учитываются.
    """
    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("═" * 50)
    logger.info("Пересчёт скоринга существующих лотов (без сети/LLM)")

    tenders = db.get_all_tenders_for_rescore(limit=limit)
    logger.info("Лотов к пересчёту: %d", len(tenders))

    rescored = 0
    changed = 0
    errors: list[str] = []

    for tender in tenders:
        pnum = tender.get("purchase_number", "")
        try:
            # Сохраняем влияние ранее полученного LLM-триажа на Ф1.
            if tender.get("llm_triage_verdict"):
                tender["llm_triage"] = {
                    "verdict":  tender.get("llm_triage_verdict"),
                    "fit":      tender.get("llm_triage_fit"),
                    "resale":   tender.get("llm_triage_resale"),
                    "category": tender.get("llm_triage_category"),
                    "reason":   tender.get("llm_triage_reason"),
                }

            old_decision = tender.get("filter_decision")
            detailed = tender.get("detail_checked_at") is not None

            if detailed:
                # По уже скачанному тексту (ТЗ/страница), без новой загрузки.
                text = "\n".join(
                    str(tender.get(k) or "")
                    for k in ("primary_text", "document_text_excerpt")
                )
                filter_result = run_stage2_filters(tender, text)
                if not dry_run:
                    db.save_filter_result(filter_result, stage="stage2")
            else:
                filter_result = run_stage1_filters(tender, tender.get("primary_text", "") or "")
                if not dry_run:
                    db.update_stage1_after_triage(filter_result)

            rescored += 1
            if filter_result.decision != old_decision:
                changed += 1
            if dry_run:
                print(f"{pnum}: {old_decision or '—'} → {filter_result.decision} "
                      f"({filter_result.total_score}) {str(tender.get('title',''))[:70]}")
        except Exception as exc:
            msg = f"Ошибка пересчёта {pnum}: {exc}"
            logger.warning(msg)
            errors.append(msg)

    logger.info("Пересчёт завершён: обработано %d, решение изменилось у %d", rescored, changed)
    if not dry_run:
        db.log_run("rescore", started_at, found=len(tenders), processed=rescored, notified=0, errors="; ".join(errors))
    return rescored, changed


def run_stage2(dry_run: bool = False, limit: int | None = None) -> tuple[int, int]:
    """Детальный анализ ТЗ/документов только по кандидатам этапа 1."""
    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("═" * 50)
    logger.info("Этап 2: детальный анализ документов")

    limit = limit or config.STAGE2_LIMIT
    tenders = db.get_detail_candidates(limit=limit, min_primary_score=config.MIN_PRIMARY_SCORE_FOR_DETAIL)
    logger.info("Кандидатов на детальный анализ: %d", len(tenders))

    processed = 0
    notified_count = 0
    errors: list[str] = []

    for tender in tenders:
        pnum = tender.get("purchase_number", "")
        page_html = ""
        page_text = ""
        document_text = ""
        scoring_text = tender.get("primary_text", "") or ""

        try:
            page_url = to_common_info_url(tender.get("url", "") or pnum)
            tender["url"] = page_url
            page_html, page_text = get_tender_page(page_url)
            full_text_for_terms = "\n".join([scoring_text, page_text])
            scoring_text = "\n".join([scoring_text, page_text])

            is_eis = (tender.get("platform") or "ЕИС") == "ЕИС"
            terms = extract_financial_terms(full_text_for_terms)
            for key, value in terms.items():
                if value not in (None, ""):
                    tender[key] = value

            preliminary_result = run_stage2_filters(tender, scoring_text)
            should_download_docs = (
                config.DOWNLOAD_DOCUMENTS
                and page_html
                and is_eis
                and preliminary_result.total_score >= config.DOCUMENT_DOWNLOAD_MIN_SCORE
            )
            if should_download_docs:
                docs_url = _documents_url_from_page(page_html, page_url) or to_documents_url(tender.get("url", "") or pnum)
                docs_html, _ = get_tender_page(docs_url)
                docs = download_documents(pnum, docs_html or page_html, docs_url if docs_html else page_url)
                files = docs.get("files", [])
                if not files and docs_html:
                    logger.info("%s: на вкладке документов файлы не найдены, пробую common-info fallback", pnum)
                    docs = download_documents(pnum, page_html, page_url)
                    files = docs.get("files", [])
                logger.info("%s: скачано документов %d", pnum, len(files))
                document_text = collect_document_text(files, config.MAX_DOCUMENT_TEXT_CHARS)
                tender["document_count"] = len(files)
                tender["documents_dir"] = docs.get("dir", "")
                tender["documents_hash"] = hash_files(files)
                spec = extract_object_description_items(files)
                if spec.get("items") and not db.list_tender_items(pnum):
                    db.replace_tender_items(pnum, spec["items"])
                work_scope = extract_work_scope_from_files(files)
                if work_scope:
                    details = db.get_tender_details(pnum) or {}
                    details["work_scope"] = work_scope
                    db.save_tender_details(pnum, details)
                    tender["details_json"] = details
                full_text_for_terms += "\n" + document_text
                scoring_text = "\n".join([scoring_text, document_text])

            terms = extract_financial_terms(full_text_for_terms)
            for key, value in terms.items():
                if value not in (None, ""):
                    tender[key] = value
        except Exception as exc:
            msg = f"Ошибка получения/анализа документов {pnum}: {exc}"
            logger.warning(msg)
            errors.append(msg)

        filter_result = run_stage2_filters(tender, scoring_text)
        detail_score = filter_result.total_score
        detail_reasons = filter_result.to_reasons()
        tender["filter_decision"] = filter_result.decision
        tender["filter_scores"] = filter_result.to_filter_scores()
        tender["filter_stop"] = " | ".join(filter_result.stop_factors)
        logger.info("Stage2 8-фильтровый скор %d/%s: %s (%s)", detail_score, filter_result.decision, str(tender.get("title", "?"))[:90], pnum)

        llm_analysis = None
        import llm_provider
        if detail_score >= config.MIN_SCORE_FOR_LLM and llm_provider.is_configured():
            try:
                llm_analysis = analyze_tender(tender, scoring_text)
            except Exception as exc:
                logger.warning("Ошибка LLM-анализа %s: %s", pnum, exc)

        should_notify = (
            detail_score >= config.MIN_DETAILED_SCORE_FOR_NOTIFY
            and filter_result.decision != "NO-GO"
            and not tender.get("notified_at")
        )
        notified = False
        if should_notify:
            if dry_run:
                print("\n" + "═" * 50)
                print(re.sub(r"<[^>]+>", "", format_tender_message(tender, detail_score, detail_reasons, llm_analysis)))
                print("═" * 50)
                notified = False
            else:
                notified = send_tender_message(
                    tender,
                    detail_score,
                    detail_reasons,
                    llm_analysis,
                    config.TELEGRAM_BOT_TOKEN,
                    config.TELEGRAM_CHAT_ID,
                )
                time.sleep(0.5)

        db.save_detail(
            tender,
            detail_score,
            detail_reasons,
            llm_analysis or "",
            document_text=scoring_text,
            notified=notified,
        )
        db.save_filter_result(filter_result, stage="stage2")
        processed += 1
        if notified:
            notified_count += 1

    logger.info("Этап 2 завершён: обработано %d, отправлено %d", processed, notified_count)
    if not dry_run:
        send_summary(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, processed, notified_count, len(config.SEARCH_KEYWORDS))
    db.log_run("stage2", started_at, found=len(tenders), processed=processed, notified=notified_count, errors="; ".join(errors))
    return processed, notified_count



def _parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _deadline_passed(value: str | None) -> bool:
    dt = _parse_deadline(value)
    return bool(dt and dt < datetime.now(timezone.utc))


def run_stage3(dry_run: bool = False, limit: int | None = None) -> tuple[int, int]:
    """После дедлайна подтягивает результат закупки для аналитики конкуренции."""
    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("═" * 50)
    logger.info("Этап 3: подтягивание результатов и протоколов")

    limit = limit or config.STAGE3_LIMIT
    candidates = db.get_result_candidates(limit=limit)
    candidates = [t for t in candidates if _deadline_passed(t.get("deadline"))]
    logger.info("Кандидатов на Stage 3 после фильтра по дедлайну: %d", len(candidates))

    processed = 0
    errors: list[str] = []
    for tender in candidates:
        pnum = tender.get("purchase_number", "")
        try:
            _, page_text = get_tender_page(to_common_info_url(tender.get("url", "") or pnum))
            result = parse_result_info(page_text, initial_price=tender.get("price"))
            if dry_run:
                print(f"{pnum}: {result}")
            else:
                db.save_result(pnum, result)
            processed += 1
            time.sleep(0.5)
        except Exception as exc:
            msg = f"Ошибка Stage3 {pnum}: {exc}"
            logger.warning(msg)
            errors.append(msg)

    logger.info("Этап 3 завершён: обработано %d", processed)
    db.log_run("stage3", started_at, found=len(candidates), processed=processed, notified=0, errors="; ".join(errors))
    return processed, 0


def _stage_completed_today(stage: str, skip_completed_today: bool) -> bool:
    if not skip_completed_today:
        return False
    if db.was_stage_completed_today(stage):
        logger.info("%s уже успешно выполнялся сегодня — пропускаю", stage)
        return True
    return False


def _documents_url_from_page(page_html: str, page_url: str) -> str:
    for link, _label in find_document_items(page_html, page_url):
        if "documents.html" in link.lower():
            return link
    return ""


def _once_delta_dates(backfill_active: bool) -> tuple[str | None, str | None]:
    if backfill_active:
        return None, None
    configured_from = str(getattr(config, "PUBLISH_DATE_FROM", "") or "").strip()
    configured_to = str(getattr(config, "PUBLISH_DATE_TO", "") or "").strip()
    if configured_from or configured_to:
        return None, None
    last_started = db.get_last_successful_run_started("stage1")
    if not last_started:
        return None, None
    if isinstance(last_started, str):
        last_started = datetime.fromisoformat(last_started)
    date_from = last_started.date().isoformat()
    date_to = datetime.now().date().isoformat()
    logger.info("once: Stage1 дельта по дате публикации %s — %s", date_from, date_to)
    return date_from, date_to


def run_once(
    dry_run: bool = False,
    skip_completed_today: bool = False,
    backfill_active: bool = False,
    stage2_limit: int | None = None,
) -> tuple[int, int]:
    date_from, date_to = _once_delta_dates(backfill_active)
    found, _ = run_stage1(
        dry_run=dry_run,
        skip_completed_today=skip_completed_today,
        backfill_active=backfill_active,
        date_from=date_from,
        date_to=date_to,
    )

    # LLM-триаж в автоцикл НЕ включаем — он запускается вручную кнопкой в /control
    # (или `python main.py --triage`) и размечает только лоты без LLM-оценки.

    if _stage_completed_today("stage2", skip_completed_today):
        processed, notified = 0, 0
    else:
        processed, notified = run_stage2(dry_run=dry_run, limit=stage2_limit)
    return found + processed, notified


def run_analytics(dry_run: bool = False) -> None:
    """
    Аналитический цикл (запускается реже — раз в день или вручную):
      1. Ценовые коридоры — скрейп реестра контрактов + пересчёт
      2. Карточки заказчиков — новые и обновление устаревших
      3. Детектор изменений ТЗ
    """
    logger.info("═" * 50)
    logger.info("Аналитический цикл")

    # 1. Ценовые коридоры
    try:
        logger.info("Обновление ценовых коридоров…")
        run_winner_update(
            keywords=config.SEARCH_KEYWORDS[:8],
            pages=getattr(config, "WINNER_ANALYTICS_PAGES", 3),
        )
    except Exception as e:
        logger.error("Ошибка winner_analytics: %s", e)

    if dry_run:
        logger.info("dry_run: пропускаем customer_scorer и change_detector")
        return

    # 2. Заказчики — новые
    try:
        n = run_new_customers(limit=getattr(config, "CUSTOMER_SCORE_LIMIT", 30))
        logger.info("Новых профилей заказчиков: %d", n)
    except Exception as e:
        logger.error("Ошибка customer_scorer (новые): %s", e)

    # 3. Заказчики — обновление устаревших
    try:
        n = run_refresh_customers(
            limit=10,
            days=getattr(config, "CUSTOMER_REFRESH_DAYS", 7),
        )
        logger.info("Обновлено профилей заказчиков: %d", n)
    except Exception as e:
        logger.error("Ошибка customer_scorer (refresh): %s", e)

    # 4. Детектор изменений ТЗ
    try:
        checked, changed = check_changes()
        logger.info("Детектор изменений: проверено %d, изменений %d", checked, changed)
    except Exception as e:
        logger.error("Ошибка change_detector: %s", e)


def _start_analytics_subprocess() -> None:
    """Запускает change_detector.py как фоновый демон."""
    import subprocess, sys
    script = Path(__file__).parent / "change_detector.py"
    if not script.exists():
        logger.warning("change_detector.py не найден")
        return
    proc = subprocess.Popen(
        [sys.executable, str(script), "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("change_detector.py запущен (PID %d)", proc.pid)


def _start_telegram_decisions_subprocess() -> None:
    """
    Запускает telegram_decisions.py как дочерний процесс.
    Вызывается автоматически при запуске daemon-режима.

    Требует: TELEGRAM_BOT_TOKEN задан в .env или переменных окружения.
    Если процесс уже запущен (например, через systemd) — повторный запуск безопасен:
    Telegram API не теряет апдейты (long polling с offset).
    """
    import subprocess, sys
    script = Path(__file__).parent / "telegram_decisions.py"
    if not script.exists():
        logger.warning("telegram_decisions.py не найден, кнопки решений работать не будут")
        return
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("telegram_decisions.py запущен (PID %d)", proc.pid)


def main() -> None:
    parser = argparse.ArgumentParser(description="Двухэтапный тендерный монитор ЕИС")
    parser.add_argument("--analytics", action="store_true", help="Ценовые коридоры + карточки заказчиков + детектор изменений")
    parser.add_argument("--stage1", action="store_true", help="Только поиск карточек и первичный скоринг")
    parser.add_argument("--triage", action="store_true", help="Только LLM-триаж карточек (Stage 1.5)")
    parser.add_argument("--rescore", action="store_true", help="Разовый пересчёт скоринга всех лотов (без сети/LLM)")
    parser.add_argument("--stage2", action="store_true", help="Только детальный анализ кандидатов")
    parser.add_argument("--stage3", action="store_true", help="После дедлайна подтянуть результаты/победителей")
    parser.add_argument("--results", action="store_true", help="Алиас для --stage3")
    parser.add_argument("--once", action="store_true", help="Один полный цикл: stage1 + stage2")
    parser.add_argument("--test", action="store_true", help="Тестовый полный цикл без отправки в Telegram")
    parser.add_argument("--reset-db", action="store_true", help="Сбросить PostgreSQL-таблицы проекта и создать чистую схему")
    parser.add_argument("--limit", type=int, default=None, help="Лимит кандидатов для stage2")
    parser.add_argument("--skip-completed-today", action="store_true", help="Не запускать стадии, которые уже успешно завершались сегодня")
    parser.add_argument("--backfill-active", action="store_true", help="Stage1: собрать все активные закупки без фильтра по дате публикации")
    args = parser.parse_args()

    if args.reset_db:
        db.reset_db()   # DROP всех таблиц + пересоздание схемы + defaults
    else:
        db.connect_db()
        db.check_db()
        db.ensure_extra_columns()   # идемпотентный ALTER: колонки триажа и пр.

    # Читаем runtime-настройки из БД (могут быть изменены через веб-интерфейс)
    config.PRICE_MIN                    = config.get_runtime("PRICE_MIN",                    config.PRICE_MIN)
    config.PRICE_MAX                    = config.get_runtime("PRICE_MAX",                    config.PRICE_MAX)
    config.PUBLISH_DAYS_BACK            = config.get_runtime("PUBLISH_DAYS_BACK",            config.PUBLISH_DAYS_BACK)
    config.PUBLISH_DATE_FROM            = config.get_runtime("PUBLISH_DATE_FROM",            config.PUBLISH_DATE_FROM)
    config.PUBLISH_DATE_TO              = config.get_runtime("PUBLISH_DATE_TO",              config.PUBLISH_DATE_TO)
    config.SCHEDULE_HOURS               = config.get_runtime("SCHEDULE_HOURS",               config.SCHEDULE_HOURS)
    config.MIN_PRIMARY_SCORE_FOR_DETAIL = config.get_runtime("MIN_PRIMARY_SCORE_FOR_DETAIL", config.MIN_PRIMARY_SCORE_FOR_DETAIL)
    config.MIN_DETAILED_SCORE_FOR_NOTIFY= config.get_runtime("MIN_DETAILED_SCORE_FOR_NOTIFY",config.MIN_DETAILED_SCORE_FOR_NOTIFY)
    config.DOCUMENT_DOWNLOAD_MIN_SCORE  = config.get_runtime("DOCUMENT_DOWNLOAD_MIN_SCORE",  config.DOCUMENT_DOWNLOAD_MIN_SCORE)
    config.SEARCH_KEYWORDS              = config.get_runtime("SEARCH_KEYWORDS",              config.SEARCH_KEYWORDS)
    config.OKPD2_SEARCH_ENABLED         = config.get_runtime("OKPD2_SEARCH_ENABLED",         config.OKPD2_SEARCH_ENABLED)
    config.OKPD2_CODES                  = config.get_runtime("OKPD2_CODES",                  config.OKPD2_CODES)
    config.BACKFILL_SEARCH_PAGES        = config.get_runtime("BACKFILL_SEARCH_PAGES",        config.BACKFILL_SEARCH_PAGES)
    config.SOURCE_B2B_ENABLED           = config.get_runtime("SOURCE_B2B_ENABLED",           config.SOURCE_B2B_ENABLED)
    config.B2B_SEARCH_PAGES             = config.get_runtime("B2B_SEARCH_PAGES",             config.B2B_SEARCH_PAGES)
    # LLM-настройки (провайдер/модели/триаж) — тоже из БД с откатом на env.
    config.LLM_PROVIDER                 = config.get_runtime("LLM_PROVIDER",                 config.LLM_PROVIDER)
    config.OPENROUTER_TRIAGE_MODEL      = config.get_runtime("OPENROUTER_TRIAGE_MODEL",      config.OPENROUTER_TRIAGE_MODEL)
    config.OPENROUTER_DEEP_MODEL        = config.get_runtime("OPENROUTER_DEEP_MODEL",        config.OPENROUTER_DEEP_MODEL)
    config.LLM_TRIAGE_ENABLED           = config.get_runtime("LLM_TRIAGE_ENABLED",           config.LLM_TRIAGE_ENABLED)

    if args.analytics:
        run_analytics(dry_run=args.test)
    elif args.stage1:
        run_stage1(
            dry_run=args.test,
            skip_completed_today=args.skip_completed_today,
            backfill_active=args.backfill_active,
        )
    elif args.triage:
        run_triage(dry_run=args.test, limit=args.limit)
    elif args.rescore:
        run_rescore(dry_run=args.test, limit=args.limit)
    elif args.stage2:
        if not _stage_completed_today("stage2", args.skip_completed_today):
            run_stage2(dry_run=args.test, limit=args.limit)
    elif args.stage3 or args.results:
        if not _stage_completed_today("stage3", args.skip_completed_today):
            run_stage3(dry_run=args.test, limit=args.limit)
    elif args.once or args.test:
        run_once(
            dry_run=args.test,
            skip_completed_today=args.skip_completed_today,
            backfill_active=args.backfill_active,
            stage2_limit=args.limit,
        )
    else:
        if schedule is None:
            raise SystemExit("Для daemon-режима установи зависимость: pip install schedule")
        logger.info("Запуск по расписанию каждые %d ч.: stage1 + stage2", config.SCHEDULE_HOURS)
        send_startup_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, config.SEARCH_KEYWORDS)
        _start_telegram_decisions_subprocess()
        _start_analytics_subprocess()          # change_detector фоновым процессом
        run_once()
        schedule.every(config.SCHEDULE_HOURS).hours.do(run_once)
        # Аналитика — раз в сутки (в 04:00, чтобы не нагружать в рабочие часы)
        schedule.every().day.at("04:00").do(run_analytics)
        logger.info("Аналитика запланирована: ежедневно в 04:00")
        while True:
            schedule.run_pending()
            time.sleep(60)

    stats = db.get_stats()
    logger.info(
        "Статистика: всего %d, primary-кандидатов %d, детально проверено %d, отправлено %d",
        stats["total"],
        stats["primary_candidates"],
        stats["detailed"],
        stats["sent"],
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close_db()
