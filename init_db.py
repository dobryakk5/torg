"""
init_db.py — ЗАПУСТИТЬ ОДИН РАЗ перед первым стартом приложения.

Создаёт всю схему и регистрирует версию в schema_migrations.
При повторном запуске проверяет текущую версию и, при необходимости,
применяет новые миграции.

Использование:
    python init_db.py             # первичная инициализация / проверка
    python init_db.py --status    # показать текущую версию схемы
    python init_db.py --reset     # ПОЛНЫЙ СБРОС (необратимо, спросит подтверждение)
    python init_db.py --defaults  # загрузить настройки по умолчанию в таблицу settings

После первого запуска web_app.py и main.py при старте вызывают только
connect_db() + check_db() — без DDL.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("init_db")


def load_defaults(db) -> None:
    """
    Записывает значения по умолчанию в таблицу settings.
    Не перезаписывает уже сохранённые пользователем значения.
    """
    import config, json

    defaults: list[tuple[str, str, str]] = [
        ("PRICE_MIN",                    str(config.PRICE_MIN),
         "Минимальная НМЦК (₽)"),
        ("PRICE_MAX",                    str(config.PRICE_MAX),
         "Максимальная НМЦК (₽)"),
        ("PUBLISH_DAYS_BACK",            str(config.PUBLISH_DAYS_BACK),
         "Глубина поиска (дней)"),
        ("PUBLISH_DATE_FROM",            config.PUBLISH_DATE_FROM,
         "Явная дата публикации с"),
        ("PUBLISH_DATE_TO",              config.PUBLISH_DATE_TO,
         "Явная дата публикации по"),
        ("SCHEDULE_HOURS",               str(config.SCHEDULE_HOURS),
         "Интервал автозапуска (часов)"),
        ("MIN_PRIMARY_SCORE_FOR_DETAIL", str(config.MIN_PRIMARY_SCORE_FOR_DETAIL),
         "Мин. первичный скор для Stage2"),
        ("MIN_DETAILED_SCORE_FOR_NOTIFY",str(config.MIN_DETAILED_SCORE_FOR_NOTIFY),
         "Мин. детальный скор для Telegram"),
        ("OKPD2_SEARCH_ENABLED",         "1" if config.OKPD2_SEARCH_ENABLED else "0",
         "Поиск по ОКПД2 (1/0)"),
        ("OKPD2_CODES",                  json.dumps(config.OKPD2_CODES, ensure_ascii=False),
         "Коды ОКПД2 (JSON-массив)"),
        ("SEARCH_KEYWORDS",              json.dumps(config.SEARCH_KEYWORDS, ensure_ascii=False),
         "Ключевые слова (JSON-массив)"),
        ("BACKFILL_SEARCH_PAGES",        str(config.BACKFILL_SEARCH_PAGES),
         "Страниц для сбора всех активных торгов"),
        ("CHANGE_CHECK_HOURS",           str(config.CHANGE_CHECK_HOURS),
         "Интервал детектора изменений (часов)"),
        ("CHANGE_MIN_SCORE",             str(config.CHANGE_MIN_SCORE),
         "Мин. скор для мониторинга изменений"),
        ("WINNER_ANALYTICS_PAGES",       str(config.WINNER_ANALYTICS_PAGES),
         "Страниц для сбора статистики контрактов"),
        ("SOURCE_B2B_ENABLED",           "1" if config.SOURCE_B2B_ENABLED else "0",
         "Источник B2B-Center (1/0)"),
        ("B2B_SEARCH_PAGES",             str(config.B2B_SEARCH_PAGES),
         "B2B-Center: страниц на запрос"),
    ]

    # Не перетирать то, что пользователь уже сохранил
    existing = db.get_all_settings()
    inserted = 0
    for key, value, desc in defaults:
        if key not in existing:
            db.set_setting(key, value, desc)
            inserted += 1
            logger.info("  + %-40s = %s", key, value[:60])

    try:
        from knowledge_base import seed_defaults as _seed_kb
        n_kb = _seed_kb()
        if n_kb:
            logger.info("kb_risk_rules: загружено %d стартовых правил", n_kb)
    except Exception as e:
        logger.warning("Не удалось загрузить KB defaults: %s", e)

    try:
        n_rules = db.scoring_rules_seed()
        if n_rules:
            logger.info("scoring_rules: засеяно %d фраз из дефолтов", n_rules)
    except Exception as e:
        logger.warning("Не удалось засеять scoring_rules: %s", e)

    if inserted:
        logger.info("Загружено настроек по умолчанию: %d", inserted)
    else:
        logger.info("Все настройки уже заданы, defaults не применялись")


def run_init() -> None:
    import database as db
    logger.info("Подключение к PostgreSQL…")
    db.connect_db()

    # Проверяем, была ли уже инициализация
    current_version = db.get_setting("__schema_version__")   # fallback-проверка
    try:
        existing = db.get_setting("__noop__")  # просто проверяем коннект
    except Exception as e:
        logger.error("Не удалось подключиться к БД: %s", e)
        sys.exit(1)

    logger.info("Создание / обновление схемы…")
    db.init_db()
    logger.info("Загрузка настроек по умолчанию…")
    load_defaults(db)

    # Итог
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = [r[0] for r in cur.fetchall()]

    print("\n✅ БД инициализирована успешно")
    print(f"   Версия схемы: {db.SCHEMA_VERSION}")
    print(f"   Таблицы ({len(tables)}): {', '.join(tables)}")
    print("\n   Теперь можно запускать:")
    print("     uvicorn web_app:app --host 0.0.0.0 --port 8000 --reload")
    print()


def run_status() -> None:
    import database as db
    db.connect_db()
    try:
        with db._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT version, applied_at FROM schema_migrations ORDER BY applied_at")
            rows = cur.fetchall()
        if rows:
            print(f"\n📋 Версии схемы БД:")
            for version, ts in rows:
                marker = " ← текущая" if version == db.SCHEMA_VERSION else ""
                print(f"   {version}  ({ts}){marker}")
        else:
            print("\n⚠️  Таблица schema_migrations пуста — запустите init_db.py")
    except Exception as e:
        print(f"\n❌ БД не инициализирована: {e}")
        print("   Запустите: python init_db.py")
    print()


def run_reset() -> None:
    answer = input(
        "\n⚠️  ПОЛНЫЙ СБРОС удалит ВСЕ данные (тендеры, скоры, настройки).\n"
        "   Введите 'yes' для подтверждения: "
    ).strip()
    if answer.lower() != "yes":
        print("Отменено.")
        return

    import database as db
    db.connect_db()
    with db._conn() as conn:
        cur = conn.cursor()
        for tbl in [
            "kb_templates", "kb_risk_rules", "kb_equipment",
            "kb_competencies", "kb_contracts", "scoring_rules",
            "filter_scores", "decisions", "tender_changes", "runs",
            "customers", "price_corridors", "settings", "schema_migrations",
            "tenders",
        ]:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS set_tenders_updated_at() CASCADE")

    logger.info("Все таблицы удалены. Пересоздаю схему…")
    db.init_db()
    load_defaults(db)
    print("\n✅ Сброс и повторная инициализация завершены.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Инициализация схемы PostgreSQL")
    ap.add_argument("--status",   action="store_true", help="Показать текущую версию схемы")
    ap.add_argument("--reset",    action="store_true", help="ПОЛНЫЙ СБРОС данных (необратимо)")
    ap.add_argument("--defaults", action="store_true", help="Загрузить настройки по умолчанию в settings")
    args = ap.parse_args()

    if args.status:
        run_status()
    elif args.reset:
        run_reset()
    elif args.defaults:
        import database as db
        db.connect_db()
        db.check_db()
        load_defaults(db)
    else:
        run_init()
