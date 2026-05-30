"""
run_search.py — обход поиска ПО ДНЯМ с возобновляемым файлом прогресса.

Логика:
    • идём по календарным дням НАЗАД: сначала вчера, потом позавчера, … (до --days дней);
    • за каждый день прогоняем все поисковые фразы;
    • каждую завершённую пару (дата, поисковый запрос) отмечаем в текстовом файле
      прогресса — видно, какие запросы уже пройдены за конкретную дату;
    • когда все фразы за дату пройдены — переходим к предыдущей дате;
    • при перезапуске уже отмеченные пары (дата, запрос) пропускаются — продолжаем
      с того места, где остановились.

Прочее:
    • только активные лоты (af=on), цена до --price-max (по умолчанию 1 000 000 ₽);
    • первичный 8-фильтровый скоринг + upsert в БД;
    • детальные блоки common-info сохраняются для ВСЕХ лотов с решением ≠ NO-GO.

Файл прогресса: search_progress.tsv (TSV: дата<TAB>запрос<TAB>найдено<TAB>с_деталями<TAB>время).

Использование:
    python run_search.py                       # вчера→назад 30 дней, ≤1 000 000 ₽, детали для не-NO-GO
    python run_search.py --days 7              # только последние 7 дней
    python run_search.py --pages 5 --refresh-details
    python run_search.py --reset-progress      # начать обход заново (очистить файл прогресса)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import config
import database as db
from filter_engine import run_stage1_filters
from scraper import search_eis, fetch_tender_details

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("run_search")


# ── Файл прогресса ──────────────────────────────────────────────────────────

def load_done(path: Path) -> set[tuple[str, str]]:
    """Читает файл прогресса → множество пройденных пар (дата_iso, запрос)."""
    done: set[tuple[str, str]] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] and not parts[0].startswith("#"):
            done.add((parts[0], parts[1]))
    return done


def mark_done(path: Path, date_iso: str, phrase: str, found: int, with_details: int) -> None:
    """Дописывает строку прогресса (append-only) и сразу сбрасывает на диск."""
    ts = datetime.now().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{date_iso}\t{phrase}\t{found}\t{with_details}\t{ts}\n")
        f.flush()


# ── Обработка лотов ───────────────────────────────────────────────────────────

def process(tenders, phrase, *, save_details, refresh_details) -> tuple[int, int, int, int]:
    """Скоринг + upsert + детали (для не-NO-GO).

    Лоты, уже сохранённые в БД, пропускаем целиком (не скорим, не тянем детали) —
    кроме режима --refresh-details, когда детали нужно пересобрать.
    Возвращает (saved, with_details, nogo, skipped).
    """
    saved = with_details = nogo = skipped = 0
    for tender in tenders:
        pnum = tender.get("purchase_number", "")
        if not pnum:
            continue
        # Уже в БД — повторно не грузим
        if db.is_seen(pnum) and not refresh_details:
            skipped += 1
            continue
        primary_text = tender.get("primary_text", "")
        fr = run_stage1_filters(tender, primary_text)
        tender["filter_decision"] = fr.decision
        tender["filter_scores"]   = fr.to_filter_scores()
        tender["filter_stop"]     = " | ".join(fr.stop_factors)
        mk = sorted(set(tender.get("matched_keywords") or []) | {phrase})

        db.upsert_primary(tender, fr.total_score, fr.to_reasons(), mk)
        db.save_filter_result(fr, stage="stage1")
        saved += 1

        if fr.decision == "NO-GO":
            nogo += 1
            continue

        if save_details:
            already = db.get_tender_details(pnum) is not None
            if already and not refresh_details:
                with_details += 1
                continue
            try:
                det = fetch_tender_details(tender.get("url", ""))
                if det:
                    db.save_tender_details(pnum, det)
                    with_details += 1
            except Exception as exc:
                logger.warning("    детали %s не собраны: %s", pnum, exc)
    return saved, with_details, nogo, skipped


# ── Главный обход по дням ─────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Обход поиска по дням с файлом прогресса (детали кроме NO-GO)")
    ap.add_argument("--price-min", type=int, default=0, help="Нижняя граница цены, ₽ (0 = без границы)")
    ap.add_argument("--price-max", type=int, default=1_000_000, help="Верхняя граница цены, ₽")
    ap.add_argument("--days", type=int, default=30, help="Сколько дней назад обходить (от вчера)")
    ap.add_argument("--pages", type=int, default=None, help="Страниц выдачи на фразу (по умолчанию config.SEARCH_PAGES)")
    ap.add_argument("--no-okpd2", action="store_true", help=argparse.SUPPRESS)  # Совместимость: ОКПД2 больше не используется.
    ap.add_argument("--no-details", action="store_true", help="Не собирать детали вовсе")
    ap.add_argument("--refresh-details", action="store_true", help="Пересобрать детали, даже если уже есть")
    ap.add_argument("--progress-file", default="search_progress.tsv", help="Путь к файлу прогресса")
    ap.add_argument("--reset-progress", action="store_true", help="Очистить файл прогресса и начать заново")
    args = ap.parse_args()

    db.connect_db()
    db.check_db()
    db.ensure_extra_columns()

    keywords = config.get_runtime("SEARCH_KEYWORDS", config.SEARCH_KEYWORDS)
    pages = args.pages if args.pages is not None else config.SEARCH_PAGES
    save_details = not args.no_details

    progress_path = Path(args.progress_file)
    if args.reset_progress and progress_path.exists():
        progress_path.unlink()
        logger.info("Файл прогресса очищен: %s", progress_path)
    done = load_done(progress_path)

    # Список фраз за день: только поисковые ключевые слова.
    phrases = list(keywords)

    logger.info("═" * 64)
    logger.info("Обход по дням: вчера→назад %d дн. · фраз/день: %d · цена ≤%s ₽ · страниц=%d · детали=%s%s",
                args.days, len(phrases), f"{args.price_max:,}".replace(",", " "), pages,
                "не-NO-GO" if save_details else "нет", " (refresh)" if args.refresh_details else "")
    if done:
        logger.info("В файле прогресса уже отмечено пар (дата, запрос): %d — они будут пропущены", len(done))

    today = datetime.now().date()
    grand_saved = grand_details = grand_nogo = grand_skipped = 0

    for offset in range(1, args.days + 1):               # 1 = вчера, 2 = позавчера, …
        day = today - timedelta(days=offset)
        date_iso = day.isoformat()                        # YYYY-MM-DD (в файл прогресса)
        date_ru  = day.strftime("%d.%m.%Y")               # дд.мм.гггг (для ЕИС)
        logger.info("─" * 64)
        logger.info("📅 ДЕНЬ %s (вчера-%d)", date_iso, offset - 1)

        day_saved = day_details = day_nogo = day_skipped = 0
        for phrase in phrases:
            if (date_iso, phrase) in done:
                logger.info("  ⏭  уже пройдено: %s · «%s»", date_iso, phrase)
                continue
            try:
                tenders = search_eis(
                    keyword=phrase, price_from=args.price_min or None, price_to=args.price_max,
                    fz44=config.SEARCH_44FZ, fz223=config.SEARCH_223FZ, pages=pages,
                    date_from=date_ru, date_to=date_ru,
                )
                db.reconnect_db()
                s, wd, ng, sk = process(tenders, phrase, save_details=save_details, refresh_details=args.refresh_details)
                mark_done(progress_path, date_iso, phrase, len(tenders), wd)
                done.add((date_iso, phrase))
                day_saved += s; day_details += wd; day_nogo += ng; day_skipped += sk
                logger.info("  ✓ %s · «%s»: найдено %d, новых %d, с деталями %d, NO-GO %d, уже в БД %d",
                            date_iso, phrase, len(tenders), s, wd, ng, sk)
            except Exception as exc:
                logger.error("  ✗ %s · «%s»: %s (НЕ отмечаю — повторим при следующем запуске)",
                             date_iso, phrase, exc)

        logger.info("📅 ДЕНЬ %s пройден: фраз %d, новых %d, с деталями %d, NO-GO %d, уже в БД %d",
                    date_iso, len(phrases), day_saved, day_details, day_nogo, day_skipped)
        grand_saved += day_saved; grand_details += day_details; grand_nogo += day_nogo; grand_skipped += day_skipped

    logger.info("═" * 64)
    logger.info("ИТОГО: новых карточек %d · с деталями %d · NO-GO %d · уже в БД (пропущено) %d · прогресс в %s",
                grand_saved, grand_details, grand_nogo, grand_skipped, progress_path)
    db.close_db()


if __name__ == "__main__":
    main()
