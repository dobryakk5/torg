"""
main.py — двухэтапный тендерный монитор.

Этап 1: массово собирает карточки закупок из ЕИС, делает первичный скоринг по описанию,
        сохраняет ссылку и метаданные. Документы не скачиваются.
Этап 2: берет только закупки с высоким первичным скором, скачивает ТЗ/документы,
        извлекает условия, делает детальный скоринг и складывает всё в БД.
Этап 2.5: разбирает собранное LLM и отправляет лучшее в Telegram — читает только БД,
        к площадкам не ходит. Сеть и LLM разделены: сбор не ждёт модель, а падение
        LLM/OpenRouter не заставляет заново качать документы.
Этап 3: после дедлайна подтягивает результаты/протоколы для аналитики конкуренции.

Запуск:
    python main.py --stage1   # только массовая подгрузка карточек
    python main.py --stage2   # только сбор документов и деталей в БД
    python main.py --llm      # только LLM-разбор и отправка по данным из БД
    python main.py --once     # stage1 + stage2 + llm
    python main.py --test     # то же без отправки в Telegram
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Должно выполняться до импорта requests/urllib3 из модулей проекта.
from tls_bootstrap import NATIVE_TRUSTSTORE_ACTIVE

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
    prioritize_document_files,
    safe_filename as _safe_fn,
)
from llm_analyzer import analyze_tender
from notifier import format_tender_message, send_startup_message, send_summary, send_tender_message
from scraper import (
    extract_notice_guid, get_tender_page, parse_result_info, search_eis, search_eis_by_okpd2,
    to_common_info_url, to_documents_url,
)
from sources.tenderplan import search_tenderplan, download_tenderplan_documents as _download_tp_docs, _is_eis_number
import search_profiles as sp
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
# PUBLISH_DATE_FROM=last — инкрементальный режим: искать с даты последнего
# успешного Stage 1 (хранится в settings.STAGE1_WATERMARK), а не за весь период.
LAST_RUN_DATE_VALUES = {"last", "since-last", "инкремент", "incremental"}
STAGE1_WATERMARK_KEY = "STAGE1_WATERMARK"


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


def _is_last_run_date(value: str | None) -> bool:
    return str(value or "").strip().lower() in LAST_RUN_DATE_VALUES


def _resolve_watermark_date_from() -> str:
    """Дата «с» для инкрементального Stage 1 (PUBLISH_DATE_FROM=last).

    Берём метку последнего успешного прогона минус запас перекрытия
    (STAGE1_WATERMARK_OVERLAP_DAYS): площадки публикуют карточки с задержкой,
    и без нахлёста граничные лоты потерялись бы навсегда. Если метки ещё нет
    (первый запуск) — откатываемся на обычный PUBLISH_DAYS_BACK.
    """
    saved = db.get_setting(STAGE1_WATERMARK_KEY)
    if not saved:
        logger.info(
            "Stage1 инкремент: метки прошлого прогона нет — беру период %d дн. (первый запуск)",
            config.PUBLISH_DAYS_BACK,
        )
        return ""
    try:
        base = datetime.strptime(saved.strip(), "%Y-%m-%d")
    except ValueError:
        logger.warning("Stage1 инкремент: битая метка %r — беру период по умолчанию", saved)
        return ""
    overlap = int(getattr(config, "STAGE1_WATERMARK_OVERLAP_DAYS", 2))
    date_from = (base - timedelta(days=overlap)).strftime("%d.%m.%Y")
    logger.info(
        "Stage1 инкремент: прошлый прогон %s, ищу с %s (перекрытие %d дн.)",
        saved, date_from, overlap,
    )
    return date_from


def _save_watermark(had_errors: bool, dry_run: bool) -> None:
    """Двигает метку только после успешного боевого прогона: при ошибках канала
    часть карточек могла не сохраниться, и сдвиг метки потерял бы их навсегда."""
    if dry_run:
        return
    if had_errors:
        logger.warning("Stage1 инкремент: были ошибки — метку не двигаю, следующий прогон повторит период")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    db.set_setting(STAGE1_WATERMARK_KEY, today, "Дата последнего успешного Stage 1 (инкрементальный режим)")
    logger.info("Stage1 инкремент: метка сдвинута на %s", today)


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


def _apply_profile_filter(
    tenders: list[dict],
    profiles: list["sp.Profile"],
) -> tuple[list[dict], int]:
    """Пост-фильтр по поисковым профилям — тонкая обёртка над
    search_profiles.filter_and_tag() (общая для main.py и web_app.SearchRunner).
    Tenderplan через этот фильтр не проходит — вызывается только для
    ЕИС/ОКПД2/B2B/витрин.
    """
    return sp.filter_and_tag(tenders, profiles=profiles)


def _storefront_keywords(profiles: list["sp.Profile"]) -> list[str]:
    """Короткие фразы (≤2 слова) из активных профилей — для витрин с точным
    полнотекстовым поиском (СПб, ЕАТ, mos, mosreg)."""
    out: list[str] = []
    for profile in profiles:
        for q in sp.storefront_queries(profile):
            if q not in out:
                out.append(q)
    return out


def _process_stage1_tenders(
    tenders: list[dict],
    matched_keyword: str,
    dry_run: bool,
    seen_this_run: set[str],
    only_new: bool = False,
) -> tuple[int, int, int]:
    saved = 0
    newly_counted = 0
    primary_candidates = 0

    existing_pnums: set[str] = set()
    if only_new:
        existing_pnums = db.get_existing_purchase_numbers([
            tender.get("purchase_number", "")
            for tender in tenders
            if isinstance(tender, dict)
        ])
        logger.info(
            "Stage1 only_new: из %d карточек уже есть в БД %d",
            len(tenders),
            len(existing_pnums),
        )

    for tender in tenders:
        pnum = str(tender.get("purchase_number", "") or "").strip()
        if not pnum:
            logger.warning(
                "Stage1 skip: карточка без purchase_number, source=%s, title=%s",
                matched_keyword,
                str(tender.get("title", "") or "")[:120],
            )
            continue
        if only_new and pnum in existing_pnums:
            logger.info("Stage1 skip existing: %s", pnum)
            continue
        tender["purchase_number"] = pnum
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
    spb: bool | None = None,
    eat: bool | None = None,
    mos: bool | None = None,
    mosreg: bool | None = None,
    zakazrf: bool | None = None,
    tenderplan: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_new: bool = False,
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

    # Поиск идёт только по включённым поисковым профилям (search_profiles.py).
    # keywords передаётся явно только для точечных override-запусков (например
    # --tenderplan-only использует keywords=[] чтобы отключить ЕИС/B2B-каналы) —
    # в этом случае профили используются лишь для пост-фильтра/тегирования.
    profiles = sp.load_profiles()
    explicit_override = keywords is not None
    if explicit_override:
        eis_query_pairs = [(None, kw) for kw in keywords]
    else:
        eis_query_pairs = [(profile, q) for profile in profiles for q in sp.eis_queries(profile)]
    kw_list = keywords if keywords is not None else [q for _, q in eis_query_pairs]
    p_min = price_min if price_min is not None else config.PRICE_MIN
    p_max = price_max if price_max is not None else config.PRICE_MAX
    d_back = days_back if days_back is not None else (0 if backfill_active else config.PUBLISH_DAYS_BACK)
    d_from = None if backfill_active else (date_from if date_from is not None else getattr(config, "PUBLISH_DATE_FROM", ""))
    d_to = None if backfill_active else (date_to if date_to is not None else getattr(config, "PUBLISH_DATE_TO", ""))
    d_from = str(d_from or "").strip() or None
    d_to = str(d_to or "").strip() or None
    # PUBLISH_DATE_FROM=last — стартуем с даты прошлого успешного прогона.
    incremental = _is_last_run_date(d_from)
    if incremental:
        d_from = _resolve_watermark_date_from() or None
        d_to = None
        if d_from is None:          # первый запуск: обычный период по умолчанию
            d_back = config.PUBLISH_DAYS_BACK
    auto_active = _is_auto_active_date(d_from)
    if auto_active:
        d_back = 0
        d_from = None
        d_to = None
    use_44 = fz44 if fz44 is not None else config.SEARCH_44FZ
    use_223 = fz223 if fz223 is not None else config.SEARCH_223FZ
    use_okpd2 = okpd2 if okpd2 is not None else getattr(config, "OKPD2_SEARCH_ENABLED", True)
    use_b2b = b2b if b2b is not None else getattr(config, "SOURCE_B2B_ENABLED", False)
    use_spb = spb if spb is not None else getattr(config, "SOURCE_SPB_ENABLED", False)
    use_eat = eat if eat is not None else getattr(config, "SOURCE_EAT_ENABLED", False)
    use_mos = mos if mos is not None else getattr(config, "SOURCE_MOS_ENABLED", False)
    use_mosreg = mosreg if mosreg is not None else getattr(config, "SOURCE_MOSREG_ENABLED", False)
    use_zakazrf = zakazrf if zakazrf is not None else getattr(config, "SOURCE_ZAKAZRF_ENABLED", False)
    use_tenderplan = tenderplan if tenderplan is not None else getattr(config, "SOURCE_TENDERPLAN_ENABLED", False)
    pages = config.BACKFILL_SEARCH_PAGES if (backfill_active or auto_active) else config.SEARCH_PAGES

    logger.info(
        "Этап 1: поиск карточек (%d ключей, цена %s–%s ₽, период=%s, 44-ФЗ=%s, 223-ФЗ=%s, ОКПД2=%s)",
        len(kw_list), f"{p_min:,}", f"{p_max:,}", _stage1_period_label(d_back, d_from, d_to, auto_active), use_44, use_223, use_okpd2,
    )

    # ── Канал 1: поиск по ключевым словам (плюс-фразы активных профилей) ───
    for profile, keyword in eis_query_pairs:
        label = f"{profile.name}:{keyword}" if profile else keyword
        phase_started_at = datetime.now().isoformat(timespec="seconds")
        phase_mode = _stage1_phase_mode("keyword", label, d_back, pages, d_from, d_to, auto_active)
        if skip_completed_today and db.was_stage_completed_today(phase_mode):
            logger.info("Поиск '%s' уже готов сегодня — пропускаю", label)
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
            tenders, dropped = _apply_profile_filter(tenders, profiles)
            if dropped:
                logger.info("Профильный фильтр отсеял %d карточек по запросу '%s'", dropped, label)
            phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                tenders,
                label,
                dry_run,
                seen_this_run,
            )
            saved += phase_saved
            unique_saved += phase_unique
            primary_candidates += phase_candidates
            db.log_run(phase_mode, phase_started_at, found=len(tenders), processed=phase_saved, notified=0, errors="")
        except Exception as exc:
            msg = f"Ошибка поиска по '{label}': {exc}"
            logger.error(msg)
            errors.append(msg)
            continue

    # ── Канал 2: поиск по ОКПД2 — опциональный канал КАЖДОГО профиля ───────
    # (пусто okpd2_codes у профиля = профиль в этом канале не участвует).
    if use_okpd2:
        for profile in profiles:
            if not profile.okpd2_codes:
                continue
            phase_started_at = datetime.now().isoformat(timespec="seconds")
            okpd2_value = ",".join(profile.okpd2_codes)
            phase_mode = _stage1_phase_mode("okpd2", f"{profile.name}:{okpd2_value}", d_back, pages, d_from, d_to, auto_active)
            if skip_completed_today and db.was_stage_completed_today(phase_mode):
                logger.info("ОКПД2-поиск %s (%s) уже готов сегодня — пропускаю", profile.okpd2_codes, profile.name)
                continue
            before_unique = len(seen_this_run)
            try:
                okpd2_tenders = search_eis_by_okpd2(
                    okpd2_codes=profile.okpd2_codes,
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
                okpd2_tenders, dropped = _apply_profile_filter(okpd2_tenders, profiles)
                if dropped:
                    logger.info("Профильный фильтр отсеял %d карточек по ОКПД2 (%s)", dropped, profile.name)
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    okpd2_tenders,
                    f"okpd2:{profile.name}",
                    dry_run,
                    seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                logger.info("ОКПД2-канал (%s) добавил %d новых тендеров", profile.name, len(seen_this_run) - before_unique)
                db.log_run(phase_mode, phase_started_at, found=len(okpd2_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                logger.error("Ошибка ОКПД2-поиска (%s): %s", profile.name, exc)
                errors.append(f"ОКПД2 ({profile.name}): {exc}")

    # ── Канал 3: B2B-Center (коммерческие закупки и 223-ФЗ вне ЕИС) ──────────
    if use_b2b:
        from sources.b2b_center import search_b2b
        b2b_pages = getattr(config, "B2B_SEARCH_PAGES", 1)
        for profile, keyword in eis_query_pairs:
            label = f"{profile.name}:{keyword}" if profile else keyword
            phase_started_at = datetime.now().isoformat(timespec="seconds")
            kw_clean = re.sub(r"\s+", " ", label).strip()
            phase_mode = f"stage1:b2b:{kw_clean}:pages={b2b_pages}"
            if skip_completed_today and db.was_stage_completed_today(phase_mode):
                logger.info("B2B-поиск '%s' уже готов сегодня — пропускаю", label)
                continue
            try:
                b2b_tenders = search_b2b(keyword, price_from=p_min, price_to=p_max, pages=b2b_pages)
                db.reconnect_db()
                b2b_tenders, dropped = _apply_profile_filter(b2b_tenders, profiles)
                if dropped:
                    logger.info("Профильный фильтр отсеял %d карточек B2B по запросу '%s'", dropped, label)
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    b2b_tenders, f"b2b:{label}", dry_run, seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                db.log_run(phase_mode, phase_started_at, found=len(b2b_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                msg = f"Ошибка B2B-поиска по '{label}': {exc}"
                logger.error(msg)
                errors.append(msg)
                continue

    # Витрины (каналы 4-7) с точным полнотекстовым поиском: фразы — всегда из
    # активных профилей (короткие, ≤2 слова), НЕЗАВИСИМО от override keywords
    # для ЕИС/B2B (иначе keywords=[], которым отключают ЕИС+B2B для точечного
    # запуска одного канала, обнулял бы и витрины — см. --eat-only).
    storefront_kw_fallback = _storefront_keywords(profiles)

    # ── Канал 4: Электронный магазин СПб (закупки малого объёма) ────────────
    if use_spb:
        from sources.spb_estore import search_spb
        spb_pages = getattr(config, "SPB_SEARCH_PAGES", 2)
        spb_keywords = getattr(config, "SPB_SEARCH_KEYWORDS", None) or storefront_kw_fallback
        for keyword in spb_keywords:
            phase_started_at = datetime.now().isoformat(timespec="seconds")
            kw_clean = re.sub(r"\s+", " ", keyword).strip()
            phase_mode = f"stage1:spb:{kw_clean}:pages={spb_pages}"
            if skip_completed_today and db.was_stage_completed_today(phase_mode):
                logger.info("ЭМ СПб-поиск '%s' уже готов сегодня — пропускаю", keyword)
                continue
            try:
                spb_tenders = search_spb(keyword, price_from=p_min, price_to=p_max, pages=spb_pages)
                db.reconnect_db()
                spb_tenders, dropped = _apply_profile_filter(spb_tenders, profiles)
                if dropped:
                    logger.info("Профильный фильтр отсеял %d карточек ЭМ СПб по '%s'", dropped, keyword)
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    spb_tenders, f"spb:{keyword}", dry_run, seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                db.log_run(phase_mode, phase_started_at, found=len(spb_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                msg = f"Ошибка ЭМ СПб-поиска по '{keyword}': {exc}"
                logger.error(msg)
                errors.append(msg)
                continue

    # ── Канал 5: ЕАТ «Берёзка» (федеральные закупки малого объёма) ──────────
    if use_eat:
        from sources.eat import search_eat
        eat_pages = getattr(config, "EAT_SEARCH_PAGES", 1)
        eat_keywords = (
            getattr(config, "EAT_SEARCH_KEYWORDS", None)
            or getattr(config, "SPB_SEARCH_KEYWORDS", None)
            or storefront_kw_fallback
        )
        for keyword in eat_keywords:
            phase_started_at = datetime.now().isoformat(timespec="seconds")
            kw_clean = re.sub(r"\s+", " ", keyword).strip()
            phase_mode = f"stage1:eat:{kw_clean}:pages={eat_pages}"
            if skip_completed_today and db.was_stage_completed_today(phase_mode):
                logger.info("ЕАТ-поиск '%s' уже готов сегодня — пропускаю", keyword)
                continue
            try:
                eat_tenders = search_eat(keyword, price_from=p_min, price_to=p_max, pages=eat_pages)
                db.reconnect_db()
                eat_tenders, dropped = sp.filter_and_tag(
                    eat_tenders, profiles=profiles, keep_unmatched=True,
                )
                if dropped:
                    logger.info(
                        "Профильный фильтр понизил score %d карточек ЕАТ по '%s'",
                        dropped, keyword,
                    )
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    eat_tenders, f"eat:{keyword}", dry_run, seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                db.log_run(phase_mode, phase_started_at, found=len(eat_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                msg = f"Ошибка ЕАТ-поиска по '{keyword}': {exc}"
                logger.error(msg)
                errors.append(msg)
                continue

    # ── Канал 6: Портал поставщиков Москвы (котировочные сессии / потребности) ──
    if use_mos:
        from sources.mos_supplier import search_mos
        mos_pages = getattr(config, "MOS_SEARCH_PAGES", 1)
        mos_keywords = (
            getattr(config, "MOS_SEARCH_KEYWORDS", None)
            or getattr(config, "SPB_SEARCH_KEYWORDS", None)
            or storefront_kw_fallback
        )
        for keyword in mos_keywords:
            phase_started_at = datetime.now().isoformat(timespec="seconds")
            kw_clean = re.sub(r"\s+", " ", keyword).strip()
            phase_mode = f"stage1:mos:{kw_clean}:pages={mos_pages}"
            if skip_completed_today and db.was_stage_completed_today(phase_mode):
                logger.info("ПП Москвы-поиск '%s' уже готов сегодня — пропускаю", keyword)
                continue
            try:
                # Портал поставщиков ищем в форме живого фильтра фронтенда:
                # глобальные PRICE_MIN/PRICE_MAX к queryDto этого канала не добавляем.
                mos_tenders = search_mos(keyword, pages=mos_pages)
                db.reconnect_db()
                mos_tenders, dropped = _apply_profile_filter(mos_tenders, profiles)
                if dropped:
                    logger.info("Профильный фильтр отсеял %d карточек ПП Москвы по '%s'", dropped, keyword)
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    mos_tenders, f"mos:{keyword}", dry_run, seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                db.log_run(phase_mode, phase_started_at, found=len(mos_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                msg = f"Ошибка ПП Москвы-поиска по '{keyword}': {exc}"
                logger.error(msg)
                errors.append(msg)
                continue

    # ── Канал 7: Электронный магазин Московской области (ЗМО) ────────────────
    if use_mosreg:
        from sources.mosreg import search_mosreg
        mosreg_pages = getattr(config, "MOSREG_SEARCH_PAGES", 1)
        mosreg_keywords = (
            getattr(config, "MOSREG_SEARCH_KEYWORDS", None)
            or getattr(config, "SPB_SEARCH_KEYWORDS", None)
            or storefront_kw_fallback
        )
        for keyword in mosreg_keywords:
            phase_started_at = datetime.now().isoformat(timespec="seconds")
            kw_clean = re.sub(r"\s+", " ", keyword).strip()
            phase_mode = f"stage1:mosreg:{kw_clean}:pages={mosreg_pages}"
            if skip_completed_today and db.was_stage_completed_today(phase_mode):
                logger.info("ЭМ МО-поиск '%s' уже готов сегодня — пропускаю", keyword)
                continue
            try:
                mosreg_tenders = search_mosreg(keyword, price_from=p_min, price_to=p_max, pages=mosreg_pages)
                db.reconnect_db()
                mosreg_tenders, dropped = _apply_profile_filter(mosreg_tenders, profiles)
                if dropped:
                    logger.info("Профильный фильтр отсеял %d карточек ЭМ МО по '%s'", dropped, keyword)
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    mosreg_tenders, f"mosreg:{keyword}", dry_run, seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                db.log_run(phase_mode, phase_started_at, found=len(mosreg_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                msg = f"Ошибка ЭМ МО-поиска по '{keyword}': {exc}"
                logger.error(msg)
                errors.append(msg)
                continue

    # ── Канал 8: Бизнес-площадка ZakazRF (запросы доставки) ────────────────
    if use_zakazrf:
        from sources.zakazrf import search_zakazrf
        zakazrf_pages = getattr(config, "ZAKAZRF_SEARCH_PAGES", 1)
        zakazrf_keywords = (
            getattr(config, "ZAKAZRF_SEARCH_KEYWORDS", None)
            or getattr(config, "SPB_SEARCH_KEYWORDS", None)
            or storefront_kw_fallback
        )
        for keyword in zakazrf_keywords:
            phase_started_at = datetime.now().isoformat(timespec="seconds")
            kw_clean = re.sub(r"\s+", " ", keyword).strip()
            phase_mode = f"stage1:zakazrf:{kw_clean}:pages={zakazrf_pages}"
            if skip_completed_today and db.was_stage_completed_today(phase_mode):
                logger.info("БП ZakazRF-поиск '%s' уже готов сегодня — пропускаю", keyword)
                continue
            try:
                zakazrf_tenders = search_zakazrf(keyword, pages=zakazrf_pages)
                db.reconnect_db()
                zakazrf_tenders, dropped = _apply_profile_filter(zakazrf_tenders, profiles)
                if dropped:
                    logger.info("Профильный фильтр отсеял %d карточек БП ZakazRF по '%s'", dropped, keyword)
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    zakazrf_tenders, f"zakazrf:{keyword}", dry_run, seen_this_run,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                db.log_run(
                    phase_mode, phase_started_at, found=len(zakazrf_tenders),
                    processed=phase_saved, notified=0, errors="",
                )
            except Exception as exc:
                msg = f"Ошибка БП ZakazRF-поиска по '{keyword}': {exc}"
                logger.error(msg)
                errors.append(msg)

    # ── Канал 9: Tenderplan ───────────────────────────────────────────────────
    if use_tenderplan:
        phase_started_at = datetime.now().isoformat(timespec="seconds")
        phase_mode = "stage1:tenderplan"
        if skip_completed_today and db.was_stage_completed_today(phase_mode):
            logger.info("Tenderplan уже готов сегодня — пропускаю")
        else:
            try:
                tp_tenders = search_tenderplan(
                    token=getattr(config, "TENDERPLAN_TOKEN", ""),
                    base_url=getattr(config, "TENDERPLAN_BASE_URL", "https://tenderplan.ru"),
                    list_params=getattr(config, "TENDERPLAN_LIST_PARAMS", {}),
                )
                db.reconnect_db()
                phase_saved, phase_unique, phase_candidates = _process_stage1_tenders(
                    tp_tenders, "tenderplan", dry_run, seen_this_run, only_new=only_new,
                )
                saved += phase_saved
                unique_saved += phase_unique
                primary_candidates += phase_candidates
                db.log_run(phase_mode, phase_started_at, found=len(tp_tenders), processed=phase_saved, notified=0, errors="")
            except Exception as exc:
                msg = f"Ошибка Tenderplan-канала: {exc}"
                logger.error(msg)
                errors.append(msg)

    logger.info("Этап 1 завершён: найдено %d, кандидатов на этап 2: %d", unique_saved, primary_candidates)
    db.log_run("stage1", started_at, found=unique_saved, processed=saved, notified=0, errors="; ".join(errors))
    if incremental:
        _save_watermark(had_errors=bool(errors), dry_run=dry_run)
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


def run_redocs(dry_run: bool = False, limit: int | None = None) -> tuple[int, int]:
    """Офлайн-переобработка уже скачанных документов с диска (без сети и LLM).

    Перечитывает файлы из documents_dir, пересобирает текст для скоринга
    (документы — В НАЧАЛО, чтобы обрезки excerpt/LLM-промпта не съедали ТЗ),
    переизвлекает финусловия/спецификации и пересчитывает Stage 2-фильтры.
    Нужен после фикса порядка scoring_text: у ранее обработанных лотов
    document_text_excerpt был забит шаблонной шапкой страницы ЕИС вместо ТЗ.
    """
    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("═" * 50)
    logger.info("Переобработка скачанных документов с диска (без сети/LLM)")

    from document_processor import SUPPORTED_EXTENSIONS

    tenders = db.get_redocs_candidates(limit=limit)
    logger.info("Лотов с документами на диске: %d", len(tenders))

    processed = 0
    with_text = 0
    errors: list[str] = []

    for tender in tenders:
        pnum = tender.get("purchase_number", "")
        try:
            docs_dir = Path(str(tender.get("documents_dir") or ""))
            files = prioritize_document_files(
                p for p in docs_dir.iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
            ) if docs_dir.is_dir() else []
            if not files:
                continue

            # Сохраняем влияние ранее полученного LLM-триажа на Ф1 (как в rescore).
            if tender.get("llm_triage_verdict"):
                tender["llm_triage"] = {
                    "verdict":  tender.get("llm_triage_verdict"),
                    "fit":      tender.get("llm_triage_fit"),
                    "resale":   tender.get("llm_triage_resale"),
                    "category": tender.get("llm_triage_category"),
                    "reason":   tender.get("llm_triage_reason"),
                }

            document_text = collect_document_text(files, config.MAX_DOCUMENT_TEXT_CHARS)
            if document_text.strip():
                with_text += 1

            # Старый excerpt содержит текст страницы ЕИС (полезен для финусловий),
            # но при повторном прогоне он уже начинается с текста документов —
            # тогда не дублируем.
            old_excerpt = str(tender.get("document_text_excerpt") or "")
            if old_excerpt and document_text and old_excerpt[:200] == document_text[:200]:
                old_excerpt = ""
            scoring_text = "\n".join(filter(None, [
                document_text,
                tender.get("primary_text") or "",
                old_excerpt,
            ]))

            terms = extract_financial_terms(scoring_text)
            for key, value in terms.items():
                if value not in (None, ""):
                    tender[key] = value

            tender["document_count"] = len(files)
            tender["documents_hash"] = hash_files(files)

            if not dry_run:
                spec = extract_object_description_items(files)
                if spec.get("items") and not db.list_tender_items(pnum):
                    db.replace_tender_items(pnum, spec["items"])
                work_scope = extract_work_scope_from_files(files)
                if work_scope:
                    details = db.get_tender_details(pnum) or {}
                    details["work_scope"] = work_scope
                    db.save_tender_details(pnum, details)
                    tender["details_json"] = details

            filter_result = run_stage2_filters(tender, scoring_text)
            tender["filter_decision"] = filter_result.decision
            tender["filter_stop"] = " | ".join(filter_result.stop_factors)

            if dry_run:
                print(
                    f"{pnum}: {filter_result.total_score}/{filter_result.decision} "
                    f"docs={len(files)} text={len(document_text)} "
                    f"{str(tender.get('title', ''))[:60]}"
                )
            else:
                db.save_detail(
                    tender,
                    filter_result.total_score,
                    filter_result.to_reasons(),
                    llm_analysis=tender.get("llm_analysis") or "",
                    document_text=scoring_text,
                )
                db.save_filter_result(filter_result, stage="stage2")
            processed += 1
        except Exception as exc:
            msg = f"Redocs {pnum}: {type(exc).__name__}: {exc}"
            logger.warning(msg)
            errors.append(msg)

    logger.info("Переобработка завершена: обработано %d, с текстом ТЗ %d", processed, with_text)
    if not dry_run:
        db.log_run("redocs", started_at, found=len(tenders), processed=processed, notified=0, errors="; ".join(errors))
    return processed, with_text


def run_stage2(
    dry_run: bool = False,
    limit: int | None = None,
    platform: str | None = None,
    force: bool = False,
) -> tuple[int, int]:
    """Этап 2 — ТОЛЬКО сбор: страницы, документы, условия, скоринг → в БД.

    Сеть здесь есть, LLM и Telegram — нет: разбор моделью и отправка вынесены в
    Stage 2.5 (`run_llm`), который читает уже сохранённые данные из БД. Так дорогой
    и хрупкий LLM-проход не блокирует скачивание и переживает падение сети.

    Ошибка одного тендера не должна останавливать весь проход. Финальная
    статистика и запись runs выполняются всегда.

    Возвращает (обработано, кандидатов на LLM).
    """
    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("═" * 50)
    logger.info("Этап 2: сбор документов и деталей (без LLM)")

    limit = limit or config.STAGE2_LIMIT
    tenders = db.get_detail_candidates(
        limit=limit,
        min_primary_score=config.MIN_PRIMARY_SCORE_FOR_DETAIL,
        platform=platform,
        force=force,
    )
    if platform:
        logger.info("Фильтр по площадке: %s%s", platform, " (force: и уже разобранные)" if force else "")
    logger.info("Кандидатов на детальный анализ: %d", len(tenders))

    processed = 0
    llm_pending = 0
    failed_count = 0
    docs_reported = 0
    docs_saved = 0
    docs_with_text = 0
    decision_counts = {"GO": 0, "CAUTION": 0, "NO-GO": 0}
    errors: list[str] = []

    for tender in tenders:
        pnum = tender.get("purchase_number", "")
        page_html = ""
        page_text = ""
        document_text = ""
        scoring_text = tender.get("primary_text", "") or ""

        try:
            is_tenderplan = (tender.get("platform") or "") == "Tenderplan"
            is_eat = (tender.get("platform") or "") == "ЕАТ"
            is_zakazrf = (tender.get("platform") or "") == "БП ZakazRF"
            is_eis = ((tender.get("platform") or "ЕИС") == "ЕИС") or (is_tenderplan and _is_eis_number(pnum))

            # Для ЕИС-тендеров (включая пришедшие через Tenderplan с ЕИС-номером)
            # тянем zakupki.gov.ru. Для коммерческих Tenderplan и ЕАТ (Angular SPA
            # с анти-ботом, common-info-URL для них не строится) — не тянем: их
            # primary_text из Stage 1 уже содержит спецификацию/условия поставки.
            if (is_tenderplan and not _is_eis_number(pnum)) or is_eat or is_zakazrf:
                source_label = (
                    "коммерческий Tenderplan-тендер" if is_tenderplan
                    else "лот ЕАТ" if is_eat
                    else "запрос доставки ZakazRF"
                )
                logger.info("%s: %s, страницу ЕИС не тянем", pnum, source_label)
            else:
                page_url = to_common_info_url(tender.get("url", "") or pnum)
                tender["url"] = page_url
                page_html, page_text = get_tender_page(page_url)
                notice_guid = extract_notice_guid(page_html)
                if notice_guid:
                    tender["notice_guid"] = notice_guid

            full_text_for_terms = "\n".join([scoring_text, page_text])
            scoring_text = "\n".join([scoring_text, page_text])

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
                docs_url = to_documents_url(
                    tender.get("url", "") or pnum,
                    tender.get("notice_guid", ""),
                )
                docs_html, _ = get_tender_page(docs_url)
                docs = download_documents(pnum, docs_html or page_html, docs_url if docs_html else page_url)
                reported_files = [Path(path) for path in docs.get("files", [])]
                docs_reported += len(reported_files)
                files = [path for path in reported_files if path.is_file()]
                if len(files) != len(reported_files):
                    logger.warning(
                        "%s: загрузчик ЕИС сообщил %d файлов, реально сохранено %d",
                        pnum, len(reported_files), len(files),
                    )
                if not files and docs_html:
                    logger.info("%s: на вкладке документов файлы не найдены, пробую common-info fallback", pnum)
                    docs = download_documents(pnum, page_html, page_url)
                    reported_files = [Path(path) for path in docs.get("files", [])]
                    docs_reported += len(reported_files)
                    files = [path for path in reported_files if path.is_file()]
                logger.info("%s: реально скачано документов %d", pnum, len(files))
                docs_saved += len(files)
                files = prioritize_document_files(files)
                document_text = collect_document_text(files, config.MAX_DOCUMENT_TEXT_CHARS)
                if document_text.strip():
                    docs_with_text += 1
                tender["document_count"] = len(files)
                tender["documents_dir"] = docs.get("dir", "") if files else ""
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
                # document_text (реальное ТЗ) — в начало: и document_text_excerpt (обрезка на
                # 4000 симв. в db.save_detail), и промпт LLM (обрезка на LLM_TEXT_CHARS в
                # analyze_tender) режут текст ХВОСТОМ, а без этого самое ценное содержимое
                # документов вытеснялось шаблонной шапкой карточки ЕИС.
                scoring_text = "\n".join([document_text, scoring_text])

            # Tenderplan API: используем только физически сохранённые файлы.
            if is_tenderplan and config.DOWNLOAD_DOCUMENTS and not document_text:
                tp_token = getattr(config, "TENDERPLAN_TOKEN", "")
                if tp_token:
                    logger.info("%s: пробую скачать документы через Tenderplan API", pnum)
                    tp_docs = _download_tp_docs(
                        pnum,
                        token=tp_token,
                        base_url=getattr(config, "TENDERPLAN_BASE_URL", "https://tenderplan.ru"),
                        max_chars=config.MAX_DOCUMENT_TEXT_CHARS,
                    )
                    reported_tp_files = [Path(path) for path in tp_docs.get("files", [])]
                    docs_reported += len(reported_tp_files)
                    tp_files = [path for path in reported_tp_files if path.is_file()]
                    missing_count = len(reported_tp_files) - len(tp_files)
                    if missing_count:
                        logger.warning(
                            "%s: Tenderplan сообщил %d документов, но %d файлов отсутствуют на диске; "
                            "они не будут считаться скачанными",
                            pnum, len(reported_tp_files), missing_count,
                        )
                    if tp_files:
                        docs_saved += len(tp_files)
                        tp_files = prioritize_document_files(tp_files)
                        # Не доверяем готовому text без очистки; если он пуст, читаем реальные файлы.
                        document_text = str(tp_docs.get("text", "") or "").replace("\x00", "")
                        if not document_text.strip():
                            document_text = collect_document_text(tp_files, config.MAX_DOCUMENT_TEXT_CHARS)
                        if document_text.strip():
                            docs_with_text += 1
                        tender["document_count"] = len(tp_files)
                        tender["documents_dir"] = tp_docs.get("dir", "")
                        tender["documents_hash"] = hash_files(tp_files)
                        spec = extract_object_description_items(tp_files)
                        if spec.get("items") and not db.list_tender_items(pnum):
                            db.replace_tender_items(pnum, spec["items"])
                        work_scope = extract_work_scope_from_files(tp_files)
                        if work_scope:
                            details = db.get_tender_details(pnum) or {}
                            details["work_scope"] = work_scope
                            db.save_tender_details(pnum, details)
                            tender["details_json"] = details
                        full_text_for_terms += "\n" + document_text
                        scoring_text = "\n".join([document_text, scoring_text])
                        logger.info("%s: Tenderplan реально скачано документов %d", pnum, len(tp_files))
                    else:
                        tender["document_count"] = 0
                        tender["documents_dir"] = ""
                        tender["documents_hash"] = ""
                        logger.info("%s: Tenderplan не сохранил документов", pnum)
                else:
                    logger.info("%s: Tenderplan-канал без токена — документы не скачиваются", pnum)

            # ЕАТ «Берёзка»: контракт/приложение к контракту (ТЗ) отдаются анонимно
            # через отдельный файловый эндпоинт (see sources/eat.download_document).
            if is_eat and config.DOWNLOAD_DOCUMENTS and not document_text:
                from sources.eat import get_lot_details, download_lot_documents
                guid_match = re.search(r"/announcement/([0-9a-f-]{36})", tender.get("url", ""))
                if guid_match:
                    guid = guid_match.group(1)
                    detail = get_lot_details(guid)
                    lot = ((detail or {}).get("trade") or {}).get("lot") or {}
                    docs_meta = lot.get("documents") or []
                    if docs_meta:
                        eat_dir = config.DOCUMENTS_DIR / _safe_fn(pnum)
                        eat_files = download_lot_documents(guid, docs_meta, eat_dir)
                        docs_reported += len(docs_meta)
                        if eat_files:
                            docs_saved += len(eat_files)
                            eat_files = prioritize_document_files(eat_files)
                            document_text = collect_document_text(eat_files, config.MAX_DOCUMENT_TEXT_CHARS)
                            if document_text.strip():
                                docs_with_text += 1
                            tender["document_count"] = len(eat_files)
                            tender["documents_dir"] = str(eat_dir)
                            tender["documents_hash"] = hash_files(eat_files)
                            spec = extract_object_description_items(eat_files)
                            if spec.get("items") and not db.list_tender_items(pnum):
                                db.replace_tender_items(pnum, spec["items"])
                            work_scope = extract_work_scope_from_files(eat_files)
                            if work_scope:
                                details = db.get_tender_details(pnum) or {}
                                details["work_scope"] = work_scope
                                db.save_tender_details(pnum, details)
                                tender["details_json"] = details
                            full_text_for_terms += "\n" + document_text
                            scoring_text = "\n".join([document_text, scoring_text])
                            logger.info("%s: ЕАТ реально скачано документов %d", pnum, len(eat_files))
                        else:
                            logger.info("%s: ЕАТ — документы лота не скачаны (см. WARNING выше)", pnum)
                    else:
                        logger.info("%s: ЕАТ — детали лота не получены (антибот/куки)", pnum)
                else:
                    logger.warning("%s: ЕАТ — не удалось извлечь GUID лота из url=%s", pnum, tender.get("url", ""))

            terms = extract_financial_terms(full_text_for_terms)
            for key, value in terms.items():
                if value not in (None, ""):
                    tender[key] = value

            filter_result = run_stage2_filters(tender, scoring_text)
            detail_score = filter_result.total_score
            detail_reasons = filter_result.to_reasons()
            tender["filter_decision"] = filter_result.decision
            tender["filter_scores"] = filter_result.to_filter_scores()
            tender["filter_stop"] = " | ".join(filter_result.stop_factors)
            decision_counts[filter_result.decision] = decision_counts.get(filter_result.decision, 0) + 1
            logger.info(
                "Stage2 8-фильтровый скор %d/%s: %s (%s)",
                detail_score,
                filter_result.decision,
                str(tender.get("title", "?"))[:90],
                pnum,
            )

            # LLM и отправка в Telegram живут в Stage 2.5 (run_llm) и работают
            # уже по данным из БД: здесь только сбор и скоринг.
            db.save_detail(
                tender,
                detail_score,
                detail_reasons,
                document_text=scoring_text,
            )
            db.save_filter_result(filter_result, stage="stage2")
            processed += 1
            if detail_score >= config.MIN_SCORE_FOR_LLM:
                llm_pending += 1

        except Exception as exc:
            failed_count += 1
            msg = f"Stage2 {pnum}: {type(exc).__name__}: {exc}"
            logger.error("Ошибка обработки тендера %s; продолжаю следующий: %s", pnum, exc)
            errors.append(msg)
            continue

    logger.info("═" * 50)
    logger.info(
        "ИТОГО ЭТАПА 2: кандидатов %d; успешно %d; ошибок %d; "
        "GO %d; CAUTION %d; NO-GO %d; ждут LLM %d",
        len(tenders), processed, failed_count,
        decision_counts.get("GO", 0),
        decision_counts.get("CAUTION", 0),
        decision_counts.get("NO-GO", 0),
        llm_pending,
    )
    logger.info(
        "ИТОГО ПО ДОКУМЕНТАМ: заявлено загрузчиками %d; реально сохранено %d; "
        "тендеров с извлечённым текстом %d",
        docs_reported, docs_saved, docs_with_text,
    )
    if errors:
        logger.warning("Ошибки этапа 2 (%d): %s", len(errors), " || ".join(errors[:10]))

    try:
        db.log_run(
            "stage2",
            started_at,
            found=len(tenders),
            processed=processed,
            notified=0,
            errors="; ".join(errors),
        )
    except Exception as exc:
        logger.error("Не удалось сохранить статистику запуска Stage2: %s", exc)

    return processed, llm_pending


def run_llm(dry_run: bool = False, limit: int | None = None) -> tuple[int, int]:
    """Этап 2.5 — LLM-разбор и отправка в Telegram ПО ДАННЫМ ИЗ БД, без сети к площадкам.

    Читает лоты, которые Stage 2 уже собрал (текст ТЗ лежит в document_text_full),
    прогоняет через `analyze_tender` те, у кого скор ≥ MIN_SCORE_FOR_LLM, и отправляет
    в Telegram те, у кого скор ≥ MIN_DETAILED_SCORE_FOR_NOTIFY и решение не NO-GO.

    Возвращает (разобрано моделью, отправлено).
    """
    import llm_provider

    started_at = datetime.now().isoformat(timespec="seconds")
    logger.info("═" * 50)
    logger.info("Этап 2.5: LLM-разбор собранных лотов (без обращения к площадкам)")

    limit = limit or config.STAGE2_LIMIT
    # Порог выборки — нижний из двух: лот со скором ниже LLM-порога, но выше порога
    # отправки, всё равно должен уйти в Telegram (пусть и без разбора моделью).
    min_score = min(config.MIN_SCORE_FOR_LLM, config.MIN_DETAILED_SCORE_FOR_NOTIFY)
    tenders = db.get_llm_candidates(limit=limit, min_score=min_score)
    logger.info("Лотов в очереди на LLM/отправку: %d", len(tenders))

    llm_configured = llm_provider.is_configured()
    if not llm_configured:
        logger.warning("LLM-провайдер не настроен — будет только отправка без разбора")

    analyzed = 0
    notified_count = 0
    failed_count = 0
    errors: list[str] = []

    for tender in tenders:
        pnum = tender.get("purchase_number", "")
        try:
            detail_score = int(tender.get("detail_score") or 0)
            detail_reasons = [
                part.strip()
                for part in str(tender.get("detail_reasons") or "").split("|")
                if part.strip()
            ]
            # Полный текст пишет Stage 2; для лотов, собранных до разделения этапов,
            # остаётся только обрезка excerpt — этого хватает на разбор.
            scoring_text = str(
                tender.get("document_text_full")
                or tender.get("document_text_excerpt")
                or tender.get("primary_text")
                or ""
            )

            llm_analysis = None
            wants_llm = detail_score >= config.MIN_SCORE_FOR_LLM
            if llm_configured and wants_llm:
                try:
                    llm_analysis = analyze_tender(tender, scoring_text)
                    if llm_analysis:
                        analyzed += 1
                except Exception as exc:
                    logger.warning("Ошибка LLM-анализа %s: %s", pnum, exc)
            # Лот считается разобранным, если модель ответила или разбор ему не
            # полагался по скору. Сбой провайдера оставляет лот в очереди.
            mark_analyzed = bool(llm_analysis) or not wants_llm

            should_notify = (
                detail_score >= config.MIN_DETAILED_SCORE_FOR_NOTIFY
                and (tender.get("filter_decision") or "") != "NO-GO"
                and not tender.get("notified_at")
            )
            notified = False
            if should_notify:
                if dry_run:
                    print("\n" + "═" * 50)
                    print(re.sub(r"<[^>]+>", "", format_tender_message(tender, detail_score, detail_reasons, llm_analysis)))
                    print("═" * 50)
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
            if notified:
                notified_count += 1

            if not dry_run:
                db.save_llm_analysis(
                    pnum,
                    llm_analysis or "",
                    model=llm_provider.deep_model() if llm_analysis else "",
                    notified=notified,
                    mark_analyzed=mark_analyzed,
                )
        except Exception as exc:
            failed_count += 1
            msg = f"LLM {pnum}: {type(exc).__name__}: {exc}"
            logger.error("Ошибка LLM-разбора %s; продолжаю следующий: %s", pnum, exc)
            errors.append(msg)
            continue

    logger.info("═" * 50)
    logger.info(
        "ИТОГО ЭТАПА 2.5: в очереди %d; разобрано моделью %d; отправлено %d; ошибок %d",
        len(tenders), analyzed, notified_count, failed_count,
    )
    if errors:
        logger.warning("Ошибки этапа 2.5 (%d): %s", len(errors), " || ".join(errors[:10]))

    if not dry_run:
        try:
            send_summary(
                config.TELEGRAM_BOT_TOKEN,
                config.TELEGRAM_CHAT_ID,
                len(tenders),
                notified_count,
                len(config.SEARCH_KEYWORDS),
            )
        except Exception as exc:
            logger.warning("Не удалось отправить итог Stage2.5 в Telegram: %s", exc)

        try:
            db.log_run(
                "llm",
                started_at,
                found=len(tenders),
                processed=analyzed,
                notified=notified_count,
                errors="; ".join(errors),
            )
        except Exception as exc:
            logger.error("Не удалось сохранить статистику запуска Stage2.5: %s", exc)

    return analyzed, notified_count


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

    processed = 0
    if not _stage_completed_today("stage2", skip_completed_today):
        processed, _ = run_stage2(dry_run=dry_run, limit=stage2_limit)

    # Stage 2.5 отделён от сбора, но в автоцикле идёт следом: он разбирает моделью
    # и отправляет то, что Stage 2 уже положил в БД (включая очередь с прошлых прогонов).
    _, notified = run_llm(dry_run=dry_run, limit=stage2_limit)
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



def _log_global_stats() -> None:
    """Печатает накопительную статистику даже после частичной ошибки запуска."""
    try:
        stats = db.get_stats()
    except Exception as exc:
        logger.error("Не удалось получить итоговую статистику БД: %s", exc)
        return
    logger.info("═" * 50)
    logger.info(
        "ИТОГО В БД: всего %d; primary-кандидатов %d; детально проверено %d; "
        "отправлено %d; интересно %d; отклонено %d",
        stats["total"],
        stats["primary_candidates"],
        stats["detailed"],
        stats["sent"],
        stats.get("interesting", 0),
        stats.get("rejected", 0),
    )

def main() -> None:
    parser = argparse.ArgumentParser(description="Двухэтапный тендерный монитор ЕИС")
    parser.add_argument("--analytics", action="store_true", help="Ценовые коридоры + карточки заказчиков + детектор изменений")
    parser.add_argument("--stage1", action="store_true", help="Только поиск карточек и первичный скоринг")
    parser.add_argument("--triage", action="store_true", help="Только LLM-триаж карточек (Stage 1.5)")
    parser.add_argument("--rescore", action="store_true", help="Разовый пересчёт скоринга всех лотов (без сети/LLM)")
    parser.add_argument("--redocs", action="store_true", help="Переобработка уже скачанных документов с диска (без сети/LLM)")
    parser.add_argument("--stage2", action="store_true", help="Только сбор документов и деталей в БД (без LLM/Telegram)")
    parser.add_argument("--platform", default=None,
                        help="Stage2: только одна площадка (ЕАТ, ЕИС, 'ПП Москвы', 'ЭМ СПб', 'ЭМ МО', B2B-Center, Tenderplan)")
    parser.add_argument("--force", action="store_true",
                        help="Stage2: брать и уже разобранные лоты (для отладки одной площадки)")
    parser.add_argument("--llm", action="store_true", help="Только LLM-разбор и отправка по данным из БД (без обращения к площадкам)")
    parser.add_argument("--stage3", action="store_true", help="После дедлайна подтянуть результаты/победителей")
    parser.add_argument("--results", action="store_true", help="Алиас для --stage3")
    parser.add_argument("--tenderplan-only", action="store_true", help="Только импорт Tenderplan")
    parser.add_argument("--eat-only", action="store_true", help="Stage1 только через ЕАТ «Берёзка» (остальные каналы выключены)")
    parser.add_argument("--mos-only", action="store_true", help="Stage1 только через Портал поставщиков (остальные каналы выключены)")
    parser.add_argument("--zakazrf-only", action="store_true", help="Stage1 только через БП ZakazRF (остальные каналы выключены)")
    parser.add_argument("--only-new", action="store_true", help="Stage1/Tenderplan: обрабатывать только новые закупки")
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
    config.STAGE1_WATERMARK_OVERLAP_DAYS = config.get_runtime("STAGE1_WATERMARK_OVERLAP_DAYS", config.STAGE1_WATERMARK_OVERLAP_DAYS)
    config.PUBLISH_DATE_TO              = config.get_runtime("PUBLISH_DATE_TO",              config.PUBLISH_DATE_TO)
    config.SCHEDULE_HOURS               = config.get_runtime("SCHEDULE_HOURS",               config.SCHEDULE_HOURS)
    config.MIN_PRIMARY_SCORE_FOR_DETAIL = config.get_runtime("MIN_PRIMARY_SCORE_FOR_DETAIL", config.MIN_PRIMARY_SCORE_FOR_DETAIL)
    config.MIN_DETAILED_SCORE_FOR_NOTIFY= config.get_runtime("MIN_DETAILED_SCORE_FOR_NOTIFY",config.MIN_DETAILED_SCORE_FOR_NOTIFY)
    config.DOCUMENT_DOWNLOAD_MIN_SCORE  = config.get_runtime("DOCUMENT_DOWNLOAD_MIN_SCORE",  config.DOCUMENT_DOWNLOAD_MIN_SCORE)
    config.MIN_SCORE_FOR_LLM            = config.get_runtime("MIN_SCORE_FOR_LLM",            config.MIN_SCORE_FOR_LLM)
    config.STAGE2_LIMIT                 = config.get_runtime("STAGE2_LIMIT",                 config.STAGE2_LIMIT)
    config.SEARCH_KEYWORDS              = config.get_runtime("SEARCH_KEYWORDS",              config.SEARCH_KEYWORDS)
    config.OKPD2_SEARCH_ENABLED         = config.get_runtime("OKPD2_SEARCH_ENABLED",         config.OKPD2_SEARCH_ENABLED)
    config.OKPD2_CODES                  = config.get_runtime("OKPD2_CODES",                  config.OKPD2_CODES)
    config.BACKFILL_SEARCH_PAGES        = config.get_runtime("BACKFILL_SEARCH_PAGES",        config.BACKFILL_SEARCH_PAGES)
    config.SOURCE_B2B_ENABLED           = config.get_runtime("SOURCE_B2B_ENABLED",           config.SOURCE_B2B_ENABLED)
    config.B2B_SEARCH_PAGES             = config.get_runtime("B2B_SEARCH_PAGES",             config.B2B_SEARCH_PAGES)
    config.SOURCE_SPB_ENABLED           = config.get_runtime("SOURCE_SPB_ENABLED",           config.SOURCE_SPB_ENABLED)
    config.SPB_SEARCH_PAGES             = config.get_runtime("SPB_SEARCH_PAGES",             config.SPB_SEARCH_PAGES)
    config.SPB_SEARCH_KEYWORDS          = config.get_runtime("SPB_SEARCH_KEYWORDS",          config.SPB_SEARCH_KEYWORDS)
    config.SOURCE_EAT_ENABLED           = config.get_runtime("SOURCE_EAT_ENABLED",           config.SOURCE_EAT_ENABLED)
    config.EAT_SEARCH_PAGES             = config.get_runtime("EAT_SEARCH_PAGES",             config.EAT_SEARCH_PAGES)
    config.EAT_SEARCH_KEYWORDS          = config.get_runtime("EAT_SEARCH_KEYWORDS",          config.EAT_SEARCH_KEYWORDS)
    config.SOURCE_MOS_ENABLED           = config.get_runtime("SOURCE_MOS_ENABLED",           config.SOURCE_MOS_ENABLED)
    config.MOS_SEARCH_PAGES             = config.get_runtime("MOS_SEARCH_PAGES",             config.MOS_SEARCH_PAGES)
    config.MOS_SEARCH_KEYWORDS          = config.get_runtime("MOS_SEARCH_KEYWORDS",          config.MOS_SEARCH_KEYWORDS)
    config.MOS_REGION_PATHS             = config.get_runtime("MOS_REGION_PATHS",             config.MOS_REGION_PATHS)
    config.SOURCE_MOSREG_ENABLED        = config.get_runtime("SOURCE_MOSREG_ENABLED",        config.SOURCE_MOSREG_ENABLED)
    config.MOSREG_SEARCH_PAGES          = config.get_runtime("MOSREG_SEARCH_PAGES",          config.MOSREG_SEARCH_PAGES)
    config.MOSREG_SEARCH_KEYWORDS       = config.get_runtime("MOSREG_SEARCH_KEYWORDS",       config.MOSREG_SEARCH_KEYWORDS)
    config.SOURCE_ZAKAZRF_ENABLED       = config.get_runtime("SOURCE_ZAKAZRF_ENABLED",       config.SOURCE_ZAKAZRF_ENABLED)
    config.ZAKAZRF_SEARCH_PAGES         = config.get_runtime("ZAKAZRF_SEARCH_PAGES",         config.ZAKAZRF_SEARCH_PAGES)
    config.ZAKAZRF_SEARCH_KEYWORDS      = config.get_runtime("ZAKAZRF_SEARCH_KEYWORDS",      config.ZAKAZRF_SEARCH_KEYWORDS)
    config.ZAKAZRF_PLANED_DATE_FROM_TICKS = config.get_runtime(
        "ZAKAZRF_PLANED_DATE_FROM_TICKS", config.ZAKAZRF_PLANED_DATE_FROM_TICKS,
    )
    config.SOURCE_TENDERPLAN_ENABLED    = config.get_runtime("SOURCE_TENDERPLAN_ENABLED",    config.SOURCE_TENDERPLAN_ENABLED)
    config.TENDERPLAN_LIST_PARAMS       = config.get_runtime("TENDERPLAN_LIST_PARAMS",       config.TENDERPLAN_LIST_PARAMS)
    # LLM-настройки (провайдер/модели/триаж) — тоже из БД с откатом на env.
    config.LLM_PROVIDER                 = config.get_runtime("LLM_PROVIDER",                 config.LLM_PROVIDER)
    config.OPENROUTER_TRIAGE_MODEL      = config.get_runtime("OPENROUTER_TRIAGE_MODEL",      config.OPENROUTER_TRIAGE_MODEL)
    config.OPENROUTER_DEEP_MODEL        = config.get_runtime("OPENROUTER_DEEP_MODEL",        config.OPENROUTER_DEEP_MODEL)
    config.LLM_TRIAGE_ENABLED           = config.get_runtime("LLM_TRIAGE_ENABLED",           config.LLM_TRIAGE_ENABLED)

    if args.analytics:
        run_analytics(dry_run=args.test)
    elif args.tenderplan_only:
        stage1_found, stage1_candidates = run_stage1(
            dry_run=args.test,
            skip_completed_today=False,
            backfill_active=False,
            keywords=[],
            okpd2=False,
            b2b=False,
            zakazrf=False,
            tenderplan=True,
            only_new=args.only_new,
        )
        stage2_processed = 0
        stage2_notified = 0
        # Сразу запускаем детальный анализ, чтобы скачать ТЗ через Tenderplan API
        if not _stage_completed_today("stage2", args.skip_completed_today):
            stage2_processed, _ = run_stage2(dry_run=args.test, limit=args.limit)
        _, stage2_notified = run_llm(dry_run=args.test, limit=args.limit)
        logger.info(
            "ИТОГО ЗАПУСКА TENDERPLAN: найдено новых %d; кандидатов Stage2 %d; "
            "детально обработано %d; отправлено %d",
            stage1_found, stage1_candidates, stage2_processed, stage2_notified,
        )
    elif args.eat_only:
        found, candidates = run_stage1(
            dry_run=args.test,
            skip_completed_today=args.skip_completed_today,
            backfill_active=args.backfill_active,
            keywords=[],   # отключает каналы ЕИС и B2B (см. run_stage1: explicit_override)
            okpd2=False,
            b2b=False,
            spb=False,
            eat=True,
            mos=False,
            mosreg=False,
            zakazrf=False,
            tenderplan=False,
            only_new=args.only_new,
        )
        logger.info("ИТОГО ЗАПУСКА ЕАТ: найдено новых %d; кандидатов Stage2 %d", found, candidates)
    elif args.mos_only:
        found, candidates = run_stage1(
            dry_run=args.test,
            skip_completed_today=args.skip_completed_today,
            backfill_active=args.backfill_active,
            keywords=[],   # отключает ЕИС и поисковые каналы общих профилей
            okpd2=False,
            b2b=False,
            spb=False,
            eat=False,
            mos=True,
            mosreg=False,
            zakazrf=False,
            tenderplan=False,
            only_new=args.only_new,
        )
        logger.info(
            "ИТОГО ЗАПУСКА ПП МОСКВЫ: сохранено %d; кандидатов Stage2 %d",
            found,
            candidates,
        )
    elif args.zakazrf_only:
        found, candidates = run_stage1(
            dry_run=args.test,
            skip_completed_today=args.skip_completed_today,
            backfill_active=args.backfill_active,
            keywords=[],
            okpd2=False,
            b2b=False,
            spb=False,
            eat=False,
            mos=False,
            mosreg=False,
            zakazrf=True,
            tenderplan=False,
            only_new=args.only_new,
        )
        logger.info(
            "ИТОГО ЗАПУСКА БП ZAKAZRF: сохранено %d; кандидатов Stage2 %d",
            found,
            candidates,
        )
    elif args.stage1:
        run_stage1(
            dry_run=args.test,
            skip_completed_today=args.skip_completed_today,
            backfill_active=args.backfill_active,
            only_new=args.only_new,
        )
    elif args.triage:
        run_triage(dry_run=args.test, limit=args.limit)
    elif args.rescore:
        run_rescore(dry_run=args.test, limit=args.limit)
    elif args.redocs:
        run_redocs(dry_run=args.test, limit=args.limit)
    elif args.stage2:
        if not _stage_completed_today("stage2", args.skip_completed_today):
            run_stage2(dry_run=args.test, limit=args.limit,
                       platform=args.platform, force=args.force)
    elif args.llm:
        run_llm(dry_run=args.test, limit=args.limit)
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


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except KeyboardInterrupt:
        exit_code = 130
        logger.warning("Запуск остановлен пользователем")
    except Exception as exc:
        exit_code = 1
        # Не выводим пользователю многополосный traceback для ожидаемых runtime-сбоев.
        # Полный traceback остаётся доступен при запуске с DEBUG-логированием.
        logger.error("Критическая ошибка запуска: %s: %s", type(exc).__name__, exc)
        logger.debug("Traceback критической ошибки", exc_info=True)
    finally:
        _log_global_stats()
        db.close_db()
    raise SystemExit(exit_code)
