"""
config.py — настройки тендерного монитора.

Секреты и параметры задаются через .env или переменные окружения.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DATA_DIR      = BASE_DIR / "data"
DOCUMENTS_DIR = DATA_DIR / "documents"
LOG_PATH      = BASE_DIR / "monitor.log"

# --- PostgreSQL ---
DATABASE_URL = os.getenv("DATABASE_URL", "")
PG_HOST      = os.getenv("PG_HOST",     "localhost")
PG_PORT      = int(os.getenv("PG_PORT", "5432"))
PG_DBNAME    = os.getenv("PG_DBNAME",   "tenders_db")
PG_USER      = os.getenv("PG_USER",     "postgres")
PG_PASSWORD  = os.getenv("PG_PASSWORD", "")
PG_POOL_MIN  = int(os.getenv("PG_POOL_MIN", "1"))
PG_POOL_MAX  = int(os.getenv("PG_POOL_MAX", "5"))

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# --- Anthropic (Claude) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# --- Диапазон цен (₽) ---
PRICE_MIN = int(os.getenv("PRICE_MIN", "200000"))
PRICE_MAX = int(os.getenv("PRICE_MAX", "5000000"))

# --- Деньги на вход ---
MAX_APPLICATION_SECURITY = float(os.getenv("MAX_APPLICATION_SECURITY", "50000"))
MAX_CONTRACT_SECURITY    = float(os.getenv("MAX_CONTRACT_SECURITY",    "150000"))

# --- Законы ---
SEARCH_44FZ  = os.getenv("SEARCH_44FZ",  "1") != "0"
SEARCH_223FZ = os.getenv("SEARCH_223FZ", "1") != "0"

# --- Регион (пусто = все регионы) ---
REGIONS = [x.strip() for x in os.getenv("REGIONS", "").split(",") if x.strip()]

# --- Расписание ---
SCHEDULE_HOURS = int(os.getenv("SCHEDULE_HOURS", "3"))
SEARCH_PAGES   = int(os.getenv("SEARCH_PAGES",   "2"))
REQUEST_DELAY  = float(os.getenv("REQUEST_DELAY", "3.0"))

# Фильтр по дате публикации.
# БЕЗ этого параметра ЕИС отдаёт архив за все годы (включая 2014).
PUBLISH_DAYS_BACK = int(os.getenv("PUBLISH_DAYS_BACK", "30"))

# --- Двухэтапная воронка ---
MIN_PRIMARY_SCORE_FOR_DETAIL    = int(os.getenv("MIN_PRIMARY_SCORE_FOR_DETAIL",    "24"))
MIN_DETAILED_SCORE_FOR_NOTIFY   = int(os.getenv("MIN_DETAILED_SCORE_FOR_NOTIFY",   "30"))
MIN_SCORE_FOR_LLM               = int(os.getenv("MIN_SCORE_FOR_LLM",               "28"))
STAGE2_LIMIT                    = int(os.getenv("STAGE2_LIMIT",                    "20"))
STAGE3_LIMIT                    = int(os.getenv("STAGE3_LIMIT",                    "50"))
MIN_SCORE_FOR_NOTIFY = MIN_DETAILED_SCORE_FOR_NOTIFY   # совместимость

# --- Документы ---
DOWNLOAD_DOCUMENTS        = os.getenv("DOWNLOAD_DOCUMENTS",        "1") != "0"
MAX_DOCUMENTS_PER_TENDER  = int(os.getenv("MAX_DOCUMENTS_PER_TENDER",  "8"))
MAX_DOCUMENT_TEXT_CHARS   = int(os.getenv("MAX_DOCUMENT_TEXT_CHARS",   "30000"))
LLM_TEXT_CHARS            = int(os.getenv("LLM_TEXT_CHARS",            "12000"))

# ============================================================
# КЛЮЧЕВЫЕ СЛОВА ДЛЯ ПОИСКА
# ============================================================
# ЕИС включает морфологию → короткие/общие слова дают мусор.
#
# ИСКЛЮЧЕНЫ (проверено тестом — давали нерелевантные результаты):
#   "обмен с 1С"        → теплообменник, обмундирование
#   "обмен данными"     → широко, много нерелевантного
#   "разработка модуля" → строительные модули, НИОКР
#   "настройка сервера" → широко, низкая точность

SEARCH_KEYWORDS = [
    # Битрикс — точные фразы
    "1С-Битрикс",
    "Битрикс24",
    "Bitrix",

    # Сайты
    "сопровождение сайта",
    "доработка сайта",
    "техническая поддержка сайта",
    "модернизация сайта",
    "администрирование сайта",

    # Интеграции — только с явным указанием системы
    "интеграция с 1С",
    "интеграция 1С",

    # Серверы / инфраструктура
    "администрирование сервера",
    "резервное копирование",
    "техническое сопровождение информационной системы",

    # Личные кабинеты / порталы
    "личный кабинет",

    # Перекуп с настройкой — конкретные предметы
    "терминал сбора данных",
    "сканер штрихкода",
    "принтер этикеток",
    "сетевое оборудование",
    "видеонаблюдение",
    "СКУД",
]

# ============================================================
# ПРАВИЛА СКОРИНГА (scorer.py — первичный скор по карточке)
# ============================================================

POSITIVE_SIGNALS = [
    ("битрикс",                 6,  "Битрикс"),
    ("bitrix",                  6,  "Bitrix"),
    ("1с-битрикс",              7,  "1С-Битрикс"),
    ("битрикс24",               6,  "Битрикс24"),
    ("интеграция с 1с",         5,  "Интеграция 1С"),
    ("интеграция 1с",           5,  "Интеграция 1С"),
    ("crm",                     4,  "CRM"),
    ("api",                     3,  "API"),
    ("личный кабинет",          4,  "Личный кабинет"),
    ("личного кабинета",        4,  "Личный кабинет"),
    ("сопровождение",           3,  "Сопровождение"),
    ("техническая поддержка",   3,  "Техподдержка"),
    ("доработка",               3,  "Доработка"),
    ("модернизация",            2,  "Модернизация"),
    ("администрирование",       3,  "Администрирование"),
    ("миграция",                3,  "Миграция"),
    ("перенос сайта",           4,  "Перенос сайта"),
    ("восстановление",          4,  "Восстановление"),
    ("резервное копирование",   3,  "Backup"),
    ("linux",                   3,  "Linux"),
    ("vps",                     3,  "VPS"),
    ("сервер",                  2,  "Сервер"),
    ("php",                     3,  "PHP"),
    ("mysql",                   3,  "MySQL"),
    ("postgresql",              3,  "PostgreSQL"),
    ("терминал сбора данных",   4,  "ТСД"),
    ("сканер штрихкода",        3,  "Сканер"),
    ("принтер этикеток",        3,  "Принтер этикеток"),
    ("сетевое оборудование",    3,  "Сеть"),
    ("nas",                     3,  "NAS"),
    ("видеонаблюдение",         3,  "Видеонаблюдение"),
    ("скуд",                    3,  "СКУД"),
]

NEGATIVE_SIGNALS = [
    ("лицензия фстэк",                  -8,  "Нужна лицензия ФСТЭК"),
    ("лицензия фсб",                    -8,  "Нужна лицензия ФСБ"),
    ("членство в сро",                  -5,  "Нужно СРО"),
    ("государственная тайна",           -10, "Гостайна"),
    ("аттестат соответствия",           -4,  "Аттестация"),
    ("наличие лицензии на",             -5,  "Требуется лицензия"),
    ("по заявкам заказчика без",        -4,  "Безлимитные заявки"),
    ("неограниченное количество",       -6,  "Безлимит"),
    ("без ограничения объема",          -4,  "Без лимита объёма"),
    ("круглосуточно",                   -5,  "24/7"),
    ("24/7",                            -5,  "24/7"),
    ("не более 1 часа",                 -4,  "Жесткий SLA"),
    ("выезд специалиста",               -3,  "Нужны выезды"),
    ("опыт исполнения аналогичных",     -4,  "Требуется опыт"),
    ("не менее 3 лет",                  -3,  "Опыт 3 года"),
    ("строительство",                   -6,  "Стройка"),
    ("охрана",                          -5,  "Охрана"),
    ("уборка",                          -5,  "Уборка"),
    ("питание",                         -5,  "Питание"),
    ("теплообменник",                   -8,  "Теплообменник"),
    ("обмундирование",                  -8,  "Обмундирование"),
    ("медицинское оборудование",        -5,  "Мед. оборудование"),
    ("сенсорная интеграция",            -8,  "Сенсорная интеграция (не ИТ)"),
]

PRICE_BONUS_RANGES = [
    (300_000,   800_000, 3, "Цена 300–800 тыс."),
    (800_000, 2_000_000, 2, "Цена 800 тыс. – 2 млн"),
]
