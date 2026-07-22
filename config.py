"""
config.py — настройки тендерного монитора.

Секреты и параметры задаются через .env или переменные окружения.
"""

from __future__ import annotations

import json
import os
import sys
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


def reload_llm_secrets() -> None:
    """Перечитывает LLM-ключи из .env для долгоживущего web-процесса."""
    global ANTHROPIC_API_KEY, OPENROUTER_API_KEY, OPENROUTER_BASE_URL
    env_path = BASE_DIR / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    ANTHROPIC_API_KEY = values.get("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY", ""))
    OPENROUTER_API_KEY = values.get("OPENROUTER_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))
    OPENROUTER_BASE_URL = values.get(
        "OPENROUTER_BASE_URL",
        os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )


def env_bool(key: str, default: bool = False) -> bool:
    """Читает bool из env: 1/true/yes/on — True, 0/false/no/off/пусто — False."""
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def load_json_env(key: str, default):
    """Читает JSON из env, возвращая default при пустом или некорректном значении."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


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
PG_CONNECT_TIMEOUT       = int(os.getenv("PG_CONNECT_TIMEOUT", "10"))
PG_STATEMENT_TIMEOUT_MS  = int(os.getenv("PG_STATEMENT_TIMEOUT_MS", "120000"))
PG_IDLE_TX_TIMEOUT_MS    = int(os.getenv("PG_IDLE_TX_TIMEOUT_MS", "120000"))
PG_KEEPALIVES_IDLE       = int(os.getenv("PG_KEEPALIVES_IDLE", "30"))
PG_KEEPALIVES_INTERVAL   = int(os.getenv("PG_KEEPALIVES_INTERVAL", "10"))
PG_KEEPALIVES_COUNT      = int(os.getenv("PG_KEEPALIVES_COUNT", "3"))
PG_RETRY_ATTEMPTS        = int(os.getenv("PG_RETRY_ATTEMPTS", "3"))
PG_RETRY_DELAY           = float(os.getenv("PG_RETRY_DELAY", "1.5"))

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# Адрес веб-панели — используется для ссылки «Подробности» в Telegram-уведомлениях
# (ведёт на карточку тендера в системе, а не на площадку-источник).
WEB_APP_URL = os.getenv("WEB_APP_URL", "http://178.104.150.180:8000").rstrip("/")

# --- Anthropic (Claude) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# --- LLM-провайдер (OpenRouter по умолчанию) ---
# LLM_PROVIDER: "openrouter" (универсальный шлюз, ключ OPENROUTER_API_KEY) либо
#              "anthropic" (прямой Claude API, ключ ANTHROPIC_API_KEY).
# Модели задаются слагами OpenRouter (см. https://openrouter.ai/models) и
# редактируются на лету в /control и .env — выбираешь любую сам.
LLM_PROVIDER        = os.getenv("LLM_PROVIDER", "openrouter")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Дешёвая модель для массового триажа карточек на Stage 1.
OPENROUTER_TRIAGE_MODEL = os.getenv("OPENROUTER_TRIAGE_MODEL", "openai/gpt-4o-mini")
# Сильная модель для детального разбора ТЗ на Stage 2.
OPENROUTER_DEEP_MODEL   = os.getenv("OPENROUTER_DEEP_MODEL",   "openai/gpt-4o")
# Триаж по карточке (Stage 1): включён/выключен, лимит карточек за прогон, объём текста.
LLM_TRIAGE_ENABLED    = os.getenv("LLM_TRIAGE_ENABLED", "1") != "0"
LLM_TRIAGE_MAX_CARDS  = int(os.getenv("LLM_TRIAGE_MAX_CARDS", "300"))
LLM_TRIAGE_TEXT_CHARS = int(os.getenv("LLM_TRIAGE_TEXT_CHARS", "2500"))
LLM_HTTP_TIMEOUT      = int(os.getenv("LLM_HTTP_TIMEOUT", "60"))
# Резервные модели OpenRouter — пробуются по очереди, если основная (triage/deep)
# отдала 429/5xx/пустой ответ после ретраев. Особенно важно для бесплатных
# (:free) моделей — у них плавающие лимиты и они периодически недоступны.
OPENROUTER_FALLBACK_MODELS = [
    m.strip() for m in os.getenv("OPENROUTER_FALLBACK_MODELS", "").split(",") if m.strip()
]
# Дневной лимит запросов к OpenRouter (общий на триаж+анализ) — МЯГКИЙ ориентир,
# не блокирует запросы после превышения (см. llm_provider.py: жёсткая остановка
# только при реальном 429 от провайдера). Бесплатные модели: ~50/день без
# пополнения баланса, 1000/день при балансе от $10.
LLM_DAILY_REQUEST_LIMIT = int(os.getenv("LLM_DAILY_REQUEST_LIMIT", "1000"))

# --- Диапазон цен (₽) ---
# PRICE_MIN/PRICE_MAX — границы ПОИСКА в ЕИС (что вообще скачиваем).
PRICE_MIN = int(os.getenv("PRICE_MIN", "200000"))
PRICE_MAX = int(os.getenv("PRICE_MAX", "5000000"))

# Ценовой коридор для СКОРИНГА (фильтр №2), независимо от границ поиска:
#   [PRICE_TARGET_LO, PRICE_TARGET_HI] — целевая зона (бонус),
#   (0, PRICE_HARD_MAX] вне целевой зоны — штраф 1 балл,
#   > PRICE_HARD_MAX — отсекаем (стоп-фактор → NO-GO: не хватит обеспечения).
PRICE_TARGET_LO = int(os.getenv("PRICE_TARGET_LO", "100000"))
PRICE_TARGET_HI = int(os.getenv("PRICE_TARGET_HI", "500000"))
PRICE_HARD_MAX  = int(os.getenv("PRICE_HARD_MAX",  "1100000"))

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
BACKFILL_SEARCH_PAGES = int(os.getenv("BACKFILL_SEARCH_PAGES", "20"))
REQUEST_DELAY  = float(os.getenv("REQUEST_DELAY", "3.0"))

# Фильтр по дате публикации.
# БЕЗ этого параметра ЕИС отдаёт архив за все годы (включая 2014).
PUBLISH_DAYS_BACK = int(os.getenv("PUBLISH_DAYS_BACK", "30"))
# Явный диапазон дат публикации для поиска в ЕИС. Формат: YYYY-MM-DD или ДД.ММ.ГГГГ.
# PUBLISH_DATE_FROM=auto отключает дату и собирает все активные торги по страницам.
# Если PUBLISH_DATE_FROM заполнен другим значением, он важнее PUBLISH_DAYS_BACK.
PUBLISH_DATE_FROM = os.getenv("PUBLISH_DATE_FROM", "").strip()
PUBLISH_DATE_TO   = os.getenv("PUBLISH_DATE_TO",   "").strip()
# PUBLISH_DATE_FROM=last — инкрементальный Stage 1: искать с даты прошлого
# успешного прогона (settings.STAGE1_WATERMARK) минус запас перекрытия:
# площадки публикуют карточки с задержкой, без нахлёста граничные лоты теряются.
STAGE1_WATERMARK_OVERLAP_DAYS = int(os.getenv("STAGE1_WATERMARK_OVERLAP_DAYS", "2"))

# --- Двухэтапная воронка ---
MIN_PRIMARY_SCORE_FOR_DETAIL    = int(os.getenv("MIN_PRIMARY_SCORE_FOR_DETAIL",    "24"))
PROFILE_MISS_SCORE_PENALTY      = int(os.getenv("PROFILE_MISS_SCORE_PENALTY",      "2"))
MIN_DETAILED_SCORE_FOR_NOTIFY   = int(os.getenv("MIN_DETAILED_SCORE_FOR_NOTIFY",   "30"))
MIN_SCORE_FOR_LLM               = int(os.getenv("MIN_SCORE_FOR_LLM",               "28"))
STAGE2_LIMIT                    = int(os.getenv("STAGE2_LIMIT",                    "20"))
STAGE3_LIMIT                    = int(os.getenv("STAGE3_LIMIT",                    "50"))
MIN_SCORE_FOR_NOTIFY = MIN_DETAILED_SCORE_FOR_NOTIFY   # совместимость

# В Telegram слать ТОЛЬКО закупки малого объёма (ЗМО: ЕАТ, ПП Москвы, ЭМ МО,
# ЭМ СПб, ZakazRF — все с law_type="ЗМО"). Остальные площадки (ЕИС, B2B,
# Tenderplan) продолжают собираться, скориться и разбираться LLM — их видно в
# веб-панели, но уведомления в TG по ним не идут. Выключи (0), чтобы слать всё.
NOTIFY_ONLY_ZMO = env_bool("NOTIFY_ONLY_ZMO", True)

# --- Документы ---
DOWNLOAD_DOCUMENTS        = os.getenv("DOWNLOAD_DOCUMENTS",        "1") != "0"
DOCUMENT_DOWNLOAD_MIN_SCORE = int(os.getenv("DOCUMENT_DOWNLOAD_MIN_SCORE", "30"))
MAX_DOCUMENTS_PER_TENDER  = int(os.getenv("MAX_DOCUMENTS_PER_TENDER",  "8"))
MAX_DOCUMENT_TEXT_CHARS   = int(os.getenv("MAX_DOCUMENT_TEXT_CHARS",   "30000"))
LLM_TEXT_CHARS            = int(os.getenv("LLM_TEXT_CHARS",            "12000"))

# --- TLS/SSL для ЕИС ---
# На macOS по умолчанию используем Keychain через пакет truststore.
EIS_USE_SYSTEM_TRUSTSTORE = env_bool(
    "EIS_USE_SYSTEM_TRUSTSTORE",
    sys.platform == "darwin",
)
EIS_NATIVE_TRUSTSTORE_ACTIVE = env_bool("EIS_NATIVE_TRUSTSTORE_ACTIVE", False)

def _default_eis_ca_bundle() -> str:
    """Возвращает системный CA bundle, если он доступен.

    ЕИС использует цепочку Russian Trusted CA. Она присутствует в системном
    хранилище macOS, но может отсутствовать в certifi внутри pyenv/venv.
    Явный EIS_CA_BUNDLE из .env всегда имеет приоритет.
    """
    explicit = os.getenv("EIS_CA_BUNDLE", "").strip()
    if explicit:
        return explicit

    candidates = (
        "/etc/ssl/cert.pem",                  # macOS
        "/etc/ssl/certs/ca-certificates.crt", # Debian/Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",   # RHEL/CentOS
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return ""


EIS_CA_BUNDLE = _default_eis_ca_bundle()
EIS_VERIFY_SSL = env_bool("EIS_VERIFY_SSL", True)
# Используется только когда URL содержит один номер без исходного типа извещения.
EIS_DEFAULT_NOTICE_TYPE = os.getenv("EIS_DEFAULT_NOTICE_TYPE", "ea20").strip() or "ea20"

# --- Тендерплан ---
SOURCE_TENDERPLAN_ENABLED = env_bool("SOURCE_TENDERPLAN_ENABLED", False)
TENDERPLAN_TOKEN          = os.getenv("TENDERPLAN_TOKEN", "").strip()
TENDERPLAN_BASE_URL       = os.getenv("TENDERPLAN_BASE_URL", "https://tenderplan.ru").strip()
TENDERPLAN_LIST_PARAMS    = load_json_env("TENDERPLAN_LIST_PARAMS_JSON", {"limit": 50})

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
    "1С-Битрикс",
    "личный кабинет",
    "техническое сопровождение информационной системы",
    "техническая поддержка сайта",
    "сопровождение сайта",
    "резервное копирование",
    "администрирование сервера",
    "модернизация сайта",
    "интеграция 1С",
    "администрирование сайта",
    "доработка сайта",
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

# ============================================================
# OKPD2 — коды для параллельного поиска
# ============================================================
# Параллельный канал: ловит тендеры, в названии которых
# нет наших ключевых слов, но код деятельности — ИТ.
#
# Документация кодов:
#   62.01   — разработка программного обеспечения
#   62.02   — консультирование в области ИТ
#   62.02.1 — деятельность в области планирования ИТ-инфраструктуры
#   62.09   — прочая деятельность в области ИТ
#   63.11   — обработка данных, хостинг

OKPD2_SEARCH_ENABLED = os.getenv("OKPD2_SEARCH_ENABLED", "0") != "0"

# ============================================================
# ДОПОЛНИТЕЛЬНЫЕ ИСТОЧНИКИ (сверх ЕИС)
# ============================================================
# B2B-Center — коммерческие закупки и 223-ФЗ, которых нет в ЕИС.
# Открытая витрина, поиск по тем же SEARCH_KEYWORDS.
SOURCE_B2B_ENABLED = os.getenv("SOURCE_B2B_ENABLED", "0") != "0"
B2B_SEARCH_PAGES   = int(os.getenv("B2B_SEARCH_PAGES", "1"))

# Электронный магазин СПб (estore.gz-spb.ru) — закупки малого объёма.
# Ключевые слова: SPB_SEARCH_KEYWORDS (JSON-массив или через запятую в env),
# пусто → используются общие SEARCH_KEYWORDS. У витрины полнотекстовый поиск
# по точной фразе, поэтому сюда лучше короткие слова («сайт», «1С», «сервер»).
SOURCE_SPB_ENABLED = os.getenv("SOURCE_SPB_ENABLED", "0") != "0"
SPB_SEARCH_PAGES   = int(os.getenv("SPB_SEARCH_PAGES", "2"))
SPB_SEARCH_KEYWORDS = [
    x.strip() for x in os.getenv("SPB_SEARCH_KEYWORDS", "").split(",") if x.strip()
]

# ЕАТ «Берёзка» (agregatoreat.ru) — федеральные закупки малого объёма.
# API анти-бот пропускает российские IP; при капче задай EAT_COOKIE (см. sources/eat.py).
# Ключевые слова: EAT_SEARCH_KEYWORDS, пусто → SPB_SEARCH_KEYWORDS → SEARCH_KEYWORDS.
SOURCE_EAT_ENABLED = os.getenv("SOURCE_EAT_ENABLED", "0") != "0"
EAT_SEARCH_PAGES   = int(os.getenv("EAT_SEARCH_PAGES", "1"))
EAT_SEARCH_KEYWORDS = [
    x.strip() for x in os.getenv("EAT_SEARCH_KEYWORDS", "").split(",") if x.strip()
]
EAT_COOKIE = os.getenv("EAT_COOKIE", "")
# UA браузера, из которого взяты куки EAT_COOKIE (анти-бот сверяет отпечаток).
EAT_USER_AGENT = os.getenv("EAT_USER_AGENT", "")
# Тянуть детальную карточку лота отдельным запросом. По умолчанию ВЫКЛ: список
# list-published-trade-lots уже содержит полные lotItems (спецификация) и
# deliveryInfos (условия поставки), а детальная ручка возвращает иной формат.
EAT_FETCH_DETAILS = os.getenv("EAT_FETCH_DETAILS", "0") != "0"
# Автообновление анти-бот куков через headless-браузер (refresh_eat_cookies.py)
# при отказе анти-бота. Нужен установленный playwright.
EAT_AUTO_REFRESH = os.getenv("EAT_AUTO_REFRESH", "1") != "0"

# Портал поставщиков (zakupki.mos.ru) — котировочные сессии и потребности (ЗМО).
# Реестр общероссийский: MOS_REGION_PATHS режет по регионам (Москва ".1.504.",
# пусто = вся РФ). Ключевые слова: MOS_SEARCH_KEYWORDS → SPB_SEARCH_KEYWORDS → общие.
SOURCE_MOS_ENABLED = os.getenv("SOURCE_MOS_ENABLED", "0") != "0"
MOS_SEARCH_PAGES   = int(os.getenv("MOS_SEARCH_PAGES", "1"))
MOS_SEARCH_KEYWORDS = [
    x.strip() for x in os.getenv("MOS_SEARCH_KEYWORDS", "").split(",") if x.strip()
]
MOS_REGION_PATHS = [
    x.strip() for x in os.getenv("MOS_REGION_PATHS", "").split(",") if x.strip()
]

# Электронный магазин Московской области (market.mosreg.ru) — ЗМО.
# Ключевые слова: MOSREG_SEARCH_KEYWORDS → SPB_SEARCH_KEYWORDS → общие.
SOURCE_MOSREG_ENABLED = os.getenv("SOURCE_MOSREG_ENABLED", "0") != "0"
MOSREG_SEARCH_PAGES   = int(os.getenv("MOSREG_SEARCH_PAGES", "1"))
MOSREG_SEARCH_KEYWORDS = [
    x.strip() for x in os.getenv("MOSREG_SEARCH_KEYWORDS", "").split(",") if x.strip()
]

# Бизнес-площадка ZakazRF (bp.zakazrf.ru) — запросы доставки из HTML-выдачи.
SOURCE_ZAKAZRF_ENABLED = os.getenv("SOURCE_ZAKAZRF_ENABLED", "0") != "0"
ZAKAZRF_SEARCH_PAGES = int(os.getenv("ZAKAZRF_SEARCH_PAGES", "1"))
ZAKAZRF_SEARCH_KEYWORDS = [
    x.strip() for x in os.getenv("ZAKAZRF_SEARCH_KEYWORDS", "").split(",") if x.strip()
]
ZAKAZRF_PLANED_DATE_FROM_TICKS = os.getenv("ZAKAZRF_PLANED_DATE_FROM_TICKS", "")

# РТС-Тендер (market.rts-tender.ru → zmo-new-webapi.rts-tender.ru) — ЗМО.
# Сессия живёт ~10 ч: RTS_COOKIE_STRING (строка -b из curl целиком) и RTS_TOKEN
# (заголовок token) вставляются свежими из DevTools (в /control или .env).
# RTS_TENANT_IDS — список разделов витрины (по умолчанию 132, широкий агрегатор).
# Ключевые слова: RTS_SEARCH_KEYWORDS → SPB_SEARCH_KEYWORDS → общие.
SOURCE_RTS_ENABLED = os.getenv("SOURCE_RTS_ENABLED", "0") != "0"
RTS_SEARCH_PAGES   = int(os.getenv("RTS_SEARCH_PAGES", "1"))
RTS_SEARCH_KEYWORDS = [
    x.strip() for x in os.getenv("RTS_SEARCH_KEYWORDS", "").split(",") if x.strip()
]
RTS_COOKIE_STRING = os.getenv("RTS_COOKIE_STRING", "")
RTS_TOKEN = os.getenv("RTS_TOKEN", "")
# Автоперевыпуск сессии headless-браузером при отказе (refresh_rts_session.py).
RTS_AUTO_REFRESH = os.getenv("RTS_AUTO_REFRESH", "1") != "0"
RTS_TENANT_IDS = [
    x.strip() for x in os.getenv("RTS_TENANT_IDS", "132").split(",") if x.strip()
]

# SberB2B (sberb2b.ru) — публичные заявки (сбор КП) как ЗМО.
# SBERB2B_SESSION_ID (кука SFSESSID) можно оставить пустым — коннектор пробует
# добыть сессию автоматически GET-запросом. SBERB2B_SUBJECT_DOMAIN — необязательный
# фильтр по региону-поддомену ("rostov" и т.п.), пусто = все регионы.
# Ключевые слова: SBERB2B_SEARCH_KEYWORDS → SPB_SEARCH_KEYWORDS → общие.
SOURCE_SBERB2B_ENABLED = os.getenv("SOURCE_SBERB2B_ENABLED", "0") != "0"
SBERB2B_SEARCH_PAGES   = int(os.getenv("SBERB2B_SEARCH_PAGES", "1"))
SBERB2B_SEARCH_KEYWORDS = [
    x.strip() for x in os.getenv("SBERB2B_SEARCH_KEYWORDS", "").split(",") if x.strip()
]
SBERB2B_SESSION_ID = os.getenv("SBERB2B_SESSION_ID", "")
SBERB2B_SUBJECT_DOMAIN = os.getenv("SBERB2B_SUBJECT_DOMAIN", "")

# ============================================================
# ПРОКСИ ДЛЯ ТЕНДЕРНЫХ ПЛОЩАДОК
# ============================================================
# Все запросы к площадкам (ЕИС, ЕАТ, ПП Москвы, ЭМ МО/СПб, B2B, ZakazRF,
# Tenderplan) и скачивание документов идут через этот прокси — анти-боты
# (ЕАТ «Берёзка» и пр.) пропускают только российские IP.
# LLM (OpenRouter/Anthropic) и Telegram ходят НАПРЯМУЮ — потоки разделены,
# поэтому переменная НЕ называется HTTP_PROXY/HTTPS_PROXY (те подхватились бы
# библиотекой requests глобально и увели бы в прокси и LLM-трафик).
# Формат: http://user:pass@host:port  Пусто = без прокси.
SOURCES_PROXY_URL = os.getenv("SOURCES_PROXY_URL", "").strip()


def source_proxies() -> dict | None:
    """Словарь proxies= для requests при походах на тендерные площадки.

    None, если прокси не настроен — requests тогда идёт напрямую.
    Читает атрибут модуля (а не env), чтобы значение можно было
    переопределить на лету в тестах/скриптах.
    """
    url = str(globals().get("SOURCES_PROXY_URL", "") or "").strip()
    return {"http": url, "https": url} if url else None


OKPD2_CODES = [
    "62.01",    # разработка ПО
    "62.02",    # консультирование в ИТ
    "62.09",    # прочая ИТ-деятельность
    "63.11",    # хостинг / обработка данных
]

# ============================================================
# CHANGE DETECTOR
# ============================================================
CHANGE_CHECK_HOURS = int(os.getenv("CHANGE_CHECK_HOURS", "6"))
CHANGE_MIN_SCORE   = int(os.getenv("CHANGE_MIN_SCORE",   "20"))

# ============================================================
# CUSTOMER SCORER
# ============================================================
CUSTOMER_SCORE_LIMIT   = int(os.getenv("CUSTOMER_SCORE_LIMIT",   "30"))
CUSTOMER_REFRESH_DAYS  = int(os.getenv("CUSTOMER_REFRESH_DAYS",  "7"))

# ============================================================
# WINNER ANALYTICS
# ============================================================
WINNER_ANALYTICS_PAGES = int(os.getenv("WINNER_ANALYTICS_PAGES", "3"))


# ============================================================
# RUNTIME SETTINGS — читаем из БД, с откатом на env vars
# ============================================================

def get_runtime(key: str, default=None):
    """
    Читает настройку из таблицы settings (редактируется через веб-интерфейс).
    Порядок приоритетов: БД → переменная окружения → default.

    Использовать в коде который вызывается при каждом запуске задачи
    (не при импорте модуля), например в начале run_stage1().
    """
    import json as _json
    try:
        from database import get_setting as _gs
        val = _gs(key)
        if val is not None:
            # Автокасты по типу default
            if isinstance(default, bool):
                return val.strip() not in ("0", "false", "no", "")
            if isinstance(default, int):
                return int(val)
            if isinstance(default, float):
                return float(val)
            if isinstance(default, list):
                return _json.loads(val)
            if isinstance(default, dict):
                return _json.loads(val)
            return val
    except Exception:
        pass
    env_val = os.getenv(key)
    if env_val is not None:
        if isinstance(default, bool):
            return env_val.strip() not in ("0", "false", "no", "")
        if isinstance(default, int):
            return int(env_val)
        if isinstance(default, float):
            return float(env_val)
        if isinstance(default, list):
            try:
                return _json.loads(env_val)
            except Exception:
                return [s.strip() for s in env_val.split(",") if s.strip()]
        if isinstance(default, dict):
            try:
                return _json.loads(env_val)
            except Exception:
                return default
        return env_val
    return default
