"""database.py — PostgreSQL-хранилище для двухэтапного тендерного монитора.

Без SQLite, без локального tenders.db и без миграций старой схемы.

Подключение:
    DATABASE_URL=postgresql://user:password@localhost:5432/tenders_db
или PG_* параметры в config.py/.env.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterator, Optional, TypeVar

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

logger = logging.getLogger(__name__)
_pool: Optional[ThreadedConnectionPool] = None
T = TypeVar("T")


def _build_dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url:
        return url
    return (
        f"host={getattr(config, 'PG_HOST', 'localhost')} "
        f"port={getattr(config, 'PG_PORT', 5432)} "
        f"dbname={getattr(config, 'PG_DBNAME', 'tenders_db')} "
        f"user={getattr(config, 'PG_USER', 'postgres')} "
        f"password={getattr(config, 'PG_PASSWORD', '')} "
        f"connect_timeout=5"
    )


def _connect_kwargs() -> dict[str, Any]:
    statement_timeout_ms = int(getattr(config, "PG_STATEMENT_TIMEOUT_MS", os.getenv("PG_STATEMENT_TIMEOUT_MS", "120000")))
    idle_timeout_ms = int(getattr(config, "PG_IDLE_TX_TIMEOUT_MS", os.getenv("PG_IDLE_TX_TIMEOUT_MS", "120000")))
    return {
        "connect_timeout": int(getattr(config, "PG_CONNECT_TIMEOUT", os.getenv("PG_CONNECT_TIMEOUT", "5"))),
        "keepalives": 1,
        "keepalives_idle": int(getattr(config, "PG_KEEPALIVES_IDLE", os.getenv("PG_KEEPALIVES_IDLE", "10"))),
        "keepalives_interval": int(getattr(config, "PG_KEEPALIVES_INTERVAL", os.getenv("PG_KEEPALIVES_INTERVAL", "5"))),
        "keepalives_count": int(getattr(config, "PG_KEEPALIVES_COUNT", os.getenv("PG_KEEPALIVES_COUNT", "3"))),
        "application_name": "torg-monitor",
        "options": f"-c statement_timeout={statement_timeout_ms} -c idle_in_transaction_session_timeout={idle_timeout_ms}",
    }


def _pool_limits() -> tuple[int, int]:
    return int(getattr(config, "PG_POOL_MIN", 1)), int(getattr(config, "PG_POOL_MAX", 5))


def _reset_pool() -> None:
    reconnect_db()


def _db_retry_attempts() -> int:
    return max(1, int(getattr(config, "PG_RETRY_ATTEMPTS", os.getenv("PG_RETRY_ATTEMPTS", "3"))))


def _db_retry_delay() -> float:
    return max(0.0, float(getattr(config, "PG_RETRY_DELAY", os.getenv("PG_RETRY_DELAY", "1.5"))))


def _with_db_retries(label: str, func: Callable[[], T]) -> T:
    attempts = _db_retry_attempts()
    delay = _db_retry_delay()
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            if attempt >= attempts:
                raise
            logger.warning(
                "PostgreSQL временно недоступен (%s), попытка %d/%d: %s",
                label,
                attempt,
                attempts,
                exc,
            )
            try:
                _reset_pool()
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as reset_exc:
                logger.warning("Не удалось пересоздать PostgreSQL pool: %s", reset_exc)
            if delay:
                time.sleep(delay * attempt)
    raise RuntimeError("Недостижимая ветка retry PostgreSQL")


@contextmanager
def _conn() -> Iterator[psycopg2.extensions.connection]:
    global _pool
    if _pool is None:
        raise RuntimeError("PostgreSQL не инициализирован: сначала вызови init_db()")
    conn = _pool.getconn()
    discard = False
    if conn.closed:
        _pool.putconn(conn, close=True)
        conn = _pool.getconn()
    else:
        # Быстрая проверка живости соединения: ловит протухшие pooled-соединения
        # до того, как они заблокируются на 20+ секунд в TCP-таймауте.
        try:
            conn.cursor().execute("SELECT 1")
            if conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
                conn.rollback()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            _pool.putconn(conn, close=True)
            conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        discard = bool(conn.closed)
        if not conn.closed:
            try:
                conn.rollback()
            except (psycopg2.Error, psycopg2.InterfaceError):
                discard = True
        raise
    finally:
        _pool.putconn(conn, close=discard or bool(conn.closed))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def strip_nul(value: Any) -> Any:
    """Удаляет NUL-символы из строк и вложенных структур.

    PostgreSQL text/json/jsonb не принимают символ ``\x00``. Он иногда
    попадает в извлечённый текст старых Office/PDF-файлов. Очистка здесь
    является последней защитой перед любыми SQL-параметрами.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: strip_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [strip_nul(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_nul(item) for item in value)
    return value


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def _rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [_to_jsonable(dict(row)) for row in rows]


def _extract_llm_verdict(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"ВЕРДИКТ\s*[:：]\s*([^\n\r]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()[:80]
    upper = text.upper()
    for token in ("СМОТРЕТЬ", "ПРОПУСТИТЬ", "ОСТОРОЖНО"):
        if token in upper:
            return token
    return ""


def _decision_from_score(score: int | None, detailed: bool = False) -> str:
    score = int(score or 0)
    if detailed:
        if score >= config.MIN_DETAILED_SCORE_FOR_NOTIFY:
            return "GO"
        if score >= config.MIN_PRIMARY_SCORE_FOR_DETAIL:
            return "CAUTION"
        return "NO-GO"
    if score >= config.MIN_PRIMARY_SCORE_FOR_DETAIL:
        return "CAUTION"
    return "NO-GO"


def _filter_total_from_scores(primary_score: int | None, detail_score: int | None) -> int:
    return int(detail_score if detail_score is not None else (primary_score or 0))


def parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    formats = (
        ("%d.%m.%Y %H:%M", 16, False),
        ("%d.%m.%Y", 10, True),
        ("%Y-%m-%dT%H:%M:%S", 19, False),
        ("%Y-%m-%d", 10, True),
    )
    for fmt, size, date_only in formats:
        try:
            dt = datetime.strptime(raw[:size], fmt)
            if date_only:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def deadline_is_expired(value: str | None, now: datetime | None = None) -> bool:
    deadline = parse_deadline(value)
    if deadline is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return deadline < current


def _deadline_timestamp_sql(column: str) -> str:
    return f"""
        CASE
            WHEN {column} ~ '^\\d{{2}}\\.\\d{{2}}\\.\\d{{4}}\\s+\\d{{2}}:\\d{{2}}'
            THEN to_timestamp(substring({column} from 1 for 16), 'DD.MM.YYYY HH24:MI')
            WHEN {column} ~ '^\\d{{2}}\\.\\d{{2}}\\.\\d{{4}}'
            THEN to_timestamp(substring({column} from 1 for 10) || ' 23:59:59', 'DD.MM.YYYY HH24:MI:SS')
            WHEN {column} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}T\\d{{2}}:\\d{{2}}:\\d{{2}}'
            THEN substring({column} from 1 for 19)::timestamp AT TIME ZONE 'UTC'
            WHEN {column} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
            THEN to_timestamp(substring({column} from 1 for 10) || ' 23:59:59', 'YYYY-MM-DD HH24:MI:SS')
            ELSE NULL
        END
    """


def _active_or_unknown_deadline_sql(column: str = "t.deadline") -> str:
    parsed = _deadline_timestamp_sql(column)
    return f"""
        (
            {column} IS NULL
            OR btrim({column}) = ''
            OR ({parsed}) IS NULL
            OR ({parsed}) >= NOW()
        )
    """


DDL_TENDERS = """
CREATE TABLE IF NOT EXISTS tenders (
    id                           BIGSERIAL PRIMARY KEY,
    purchase_number              TEXT UNIQUE NOT NULL,
    title                        TEXT,
    customer                     TEXT,
    price                        NUMERIC(15, 2),
    law_type                     TEXT,
    deadline                     TEXT,
    url                          TEXT,
    platform                     TEXT,
    project_type                 TEXT,
    region                       TEXT,
    customer_inn                 TEXT,
    published_at                 TEXT,
    matched_keywords             TEXT,
    primary_text                 TEXT,

    primary_score                INTEGER DEFAULT 0,
    primary_reasons              TEXT,
    detail_score                 INTEGER,
    detail_reasons               TEXT,
    total_score                  INTEGER DEFAULT 0,

    -- совместимость с веб-интерфейсом / старым черновиком
    score                        INTEGER DEFAULT 0,
    score_reasons                TEXT,
    filter_total                 INTEGER DEFAULT 0,
    filter_decision              TEXT DEFAULT 'NO-GO',
    filter_stop                  TEXT,

    llm_analysis                 TEXT,
    llm_verdict                  TEXT,

    -- LLM-триаж по карточке (Stage 1)
    llm_triage_verdict           TEXT,
    llm_triage_fit               TEXT,
    llm_triage_resale            BOOLEAN,
    llm_triage_category          TEXT,
    llm_triage_reason            TEXT,
    llm_triage_model             TEXT,
    llm_triage_at                TIMESTAMPTZ,

    application_security_amount  NUMERIC(15, 2),
    contract_security_amount     NUMERIC(15, 2),
    warranty_security_amount     NUMERIC(15, 2),
    advance_percent              NUMERIC(5, 2),
    payment_terms                TEXT,
    execution_days               INTEGER,

    document_count               INTEGER DEFAULT 0,
    documents_dir                TEXT,
    documents_hash               TEXT,
    document_text_excerpt        TEXT,

    status                       TEXT DEFAULT 'discovered',
    decision                     TEXT DEFAULT 'pending',
    content_hash                 TEXT,
    last_changed_at              TIMESTAMPTZ,
    needs_detail_refresh         BOOLEAN DEFAULT FALSE,

    result_checked_at            TIMESTAMPTZ,
    winner_name                  TEXT,
    winner_inn                   TEXT,
    final_price                  NUMERIC(15, 2),
    participants_count           INTEGER,
    price_drop_percent           NUMERIC(6, 2),

    first_seen_at                TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at                 TIMESTAMPTZ DEFAULT NOW(),
    primary_checked_at           TIMESTAMPTZ,
    detail_checked_at            TIMESTAMPTZ,
    notified_at                  TIMESTAMPTZ,
    created_at                   TIMESTAMPTZ DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ DEFAULT NOW()
);
"""

DDL_FILTER_SCORES = """
CREATE TABLE IF NOT EXISTS filter_scores (
    id               BIGSERIAL PRIMARY KEY,
    purchase_number  TEXT NOT NULL REFERENCES tenders(purchase_number)
                     ON DELETE CASCADE ON UPDATE CASCADE,
    filter_number    SMALLINT NOT NULL,
    filter_name      TEXT,
    score            SMALLINT NOT NULL,
    signals          TEXT,
    stop_factor      BOOLEAN DEFAULT FALSE,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (purchase_number, filter_number)
);
"""

DDL_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    mode        TEXT,
    started_at  TIMESTAMPTZ,
    found       INTEGER DEFAULT 0,
    processed   INTEGER DEFAULT 0,
    notified    INTEGER DEFAULT 0,
    errors      TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

DDL_DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
    id              BIGSERIAL PRIMARY KEY,
    purchase_number TEXT NOT NULL REFERENCES tenders(purchase_number)
                    ON DELETE CASCADE ON UPDATE CASCADE,
    decision        TEXT NOT NULL,
    comment         TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

DDL_TENDER_CHANGES = """
CREATE TABLE IF NOT EXISTS tender_changes (
    id              BIGSERIAL PRIMARY KEY,
    purchase_number TEXT NOT NULL REFERENCES tenders(purchase_number)
                    ON DELETE CASCADE ON UPDATE CASCADE,
    change_type     TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    detected_at     TIMESTAMPTZ DEFAULT NOW()
);
"""

DDL_CUSTOMERS = """
CREATE TABLE IF NOT EXISTS customers (
    inn                   TEXT PRIMARY KEY,
    name                  TEXT,
    total_contracts       INTEGER DEFAULT 0,
    terminated_contracts  INTEGER DEFAULT 0,
    avg_drop_pct          NUMERIC(6,2),
    avg_participants      NUMERIC(5,2),
    repeat_winner_inn     TEXT,
    repeat_winner_name    TEXT,
    repeat_winner_share   NUMERIC(5,2),
    monopoly_flag         BOOLEAN DEFAULT FALSE,
    arbitration_count     INTEGER DEFAULT 0,
    last_arbitration_date DATE,
    reliability_score     SMALLINT DEFAULT 3,
    notes                 TEXT,
    raw_json              JSONB,
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);
"""

DDL_PRICE_CORRIDORS = """
CREATE TABLE IF NOT EXISTS price_corridors (
    category         TEXT    NOT NULL,
    law_type         TEXT    NOT NULL DEFAULT 'all',
    sample_count     INTEGER DEFAULT 0,
    avg_drop_pct     NUMERIC(6,2),
    p25_drop_pct     NUMERIC(6,2),
    p50_drop_pct     NUMERIC(6,2),
    p75_drop_pct     NUMERIC(6,2),
    min_drop_pct     NUMERIC(6,2),
    max_drop_pct     NUMERIC(6,2),
    avg_participants NUMERIC(5,2),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (category, law_type)
);
CREATE INDEX IF NOT EXISTS idx_customers_name        ON customers(name);
CREATE INDEX IF NOT EXISTS idx_customers_reliability ON customers(reliability_score);
CREATE INDEX IF NOT EXISTS idx_customers_monopoly    ON customers(monopoly_flag);
"""

DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tenders_primary_score ON tenders(primary_score DESC);
CREATE INDEX IF NOT EXISTS idx_tenders_detail_score ON tenders(detail_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_tenders_total_score ON tenders(total_score DESC);
CREATE INDEX IF NOT EXISTS idx_tenders_filter_total ON tenders(filter_total DESC);
CREATE INDEX IF NOT EXISTS idx_tenders_filter_decision ON tenders(filter_decision);
CREATE INDEX IF NOT EXISTS idx_tenders_status ON tenders(status);
CREATE INDEX IF NOT EXISTS idx_tenders_needs_detail_refresh ON tenders(needs_detail_refresh);
CREATE INDEX IF NOT EXISTS idx_tenders_result_checked_at ON tenders(result_checked_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_tenders_decision ON tenders(decision);
CREATE INDEX IF NOT EXISTS idx_tenders_created_at ON tenders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tenders_price ON tenders(price);
CREATE INDEX IF NOT EXISTS idx_filter_scores_pnum ON filter_scores(purchase_number);
"""

DDL_TRIGGER = """
CREATE OR REPLACE FUNCTION set_tenders_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_tenders_updated_at') THEN
        CREATE TRIGGER trg_tenders_updated_at
        BEFORE UPDATE ON tenders
        FOR EACH ROW EXECUTE FUNCTION set_tenders_updated_at();
    END IF;
END $$;
"""


DDL_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

DDL_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

SCHEMA_VERSION = "v7"   # bump при добавлении новых таблиц


DDL_KB = """
-- Реестр исполненных контрактов (опыт, для ст.31 44-ФЗ и контекста LLM)
CREATE TABLE IF NOT EXISTS kb_contracts (
    id           SERIAL PRIMARY KEY,
    title        TEXT NOT NULL,
    customer     TEXT,
    contract_num TEXT,
    year         INTEGER,
    price        NUMERIC,
    category     TEXT,
    tech_stack   JSONB    DEFAULT '[]',
    description  TEXT,
    problems     TEXT,
    result_url   TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Компетенции команды (технологии, сертификаты)
CREATE TABLE IF NOT EXISTS kb_competencies (
    id           SERIAL PRIMARY KEY,
    tech         TEXT NOT NULL,
    category     TEXT,
    level        SMALLINT DEFAULT 3,
    certified    BOOLEAN  DEFAULT FALSE,
    cert_name    TEXT,
    cert_expires DATE,
    notes        TEXT
);

-- Каталог оборудования и аналогов
CREATE TABLE IF NOT EXISTS kb_equipment (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    vendor        TEXT,
    model         TEXT,
    category      TEXT,
    specs         JSONB    DEFAULT '{}',
    price_rub     NUMERIC,
    lead_days     INTEGER,
    analog_for    TEXT[]   DEFAULT '{}',
    notes         TEXT
);

-- Персональные правила рисков (stop/warn/boost)
CREATE TABLE IF NOT EXISTS kb_risk_rules (
    id          SERIAL PRIMARY KEY,
    rule_type   TEXT NOT NULL CHECK (rule_type IN ('stop','warn','boost')),
    category    TEXT,
    pattern     TEXT NOT NULL,
    weight      INTEGER DEFAULT -5,
    reason      TEXT NOT NULL,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Шаблоны запросов, претензий, жалоб в ФАС
CREATE TABLE IF NOT EXISTS kb_templates (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    category    TEXT,
    content     TEXT NOT NULL,
    tags        TEXT[]  DEFAULT '{}',
    used_count  INTEGER DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kb_contracts_category ON kb_contracts(category);
CREATE INDEX IF NOT EXISTS idx_kb_competencies_tech  ON kb_competencies(tech);
CREATE INDEX IF NOT EXISTS idx_kb_equipment_category ON kb_equipment(category);
CREATE INDEX IF NOT EXISTS idx_kb_risk_rules_active  ON kb_risk_rules(active, rule_type);
"""

# Словарь фраз для 8 фильтров (редактируется в /rules).
# Заменяет вшитые списки в filter_engine.py. dim — номер фильтра 1–8,
# bucket — «корзина» (strong/medium/bad/...), term — фраза (| = ИЛИ, * = wildcard),
# unless/require — фразы-стражи контекста.
DDL_SCORING_RULES = """
CREATE TABLE IF NOT EXISTS scoring_rules (
    id          SERIAL PRIMARY KEY,
    dim         SMALLINT NOT NULL,
    bucket      TEXT NOT NULL,
    term        TEXT NOT NULL,
    unless      TEXT[]  DEFAULT '{}',
    require     TEXT[]  DEFAULT '{}',
    active      BOOLEAN DEFAULT TRUE,
    note        TEXT,
    sort        INTEGER DEFAULT 100,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scoring_rules_active ON scoring_rules(active, dim, bucket);
"""

# Поисковые профили: наборы плюс/минус-фраз для Stage 1 (заменяют плоский
# config.SEARCH_KEYWORDS). Редактируются в /profiles. См. search_profiles.py
# и docs/tz-search-profiles.md.
DDL_SEARCH_PROFILES = """
CREATE TABLE IF NOT EXISTS search_profiles (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    is_default  BOOLEAN NOT NULL DEFAULT FALSE,
    priority    INT NOT NULL DEFAULT 100,
    min_score   INT NOT NULL DEFAULT 3,
    okpd2_codes TEXT[] NOT NULL DEFAULT '{}',
    -- Ценовой коридор Ф2 (filter_engine._filter_finance) для ЭТОГО профиля.
    -- NULL у любого поля = наследовать глобальный config.PRICE_TARGET_LO/HI/PRICE_HARD_MAX.
    price_target_lo  INT,
    price_target_hi  INT,
    price_hard_max   INT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_search_profiles_default
    ON search_profiles (is_default) WHERE is_default;

CREATE TABLE IF NOT EXISTS search_phrases (
    id          SERIAL PRIMARY KEY,
    profile_id  INT NOT NULL REFERENCES search_profiles(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('plus','minus_hard','minus_soft')),
    phrase      TEXT NOT NULL,
    weight      INT NOT NULL DEFAULT 3,
    query_only  BOOLEAN NOT NULL DEFAULT FALSE,
    local_only  BOOLEAN NOT NULL DEFAULT FALSE,
    query_text  TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    note        TEXT DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (profile_id, kind, phrase)
);
CREATE INDEX IF NOT EXISTS idx_search_phrases_profile ON search_phrases(profile_id, enabled);
"""


def connect_db() -> None:
    """
    Открывает пул соединений. Не выполняет DDL.
    Вызывается при каждом старте web_app и main.py.
    """
    global _pool
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

    if _pool is None:
        pool_min, pool_max = _pool_limits()
        _pool = ThreadedConnectionPool(pool_min, pool_max, _build_dsn(), **_connect_kwargs())
        logger.info("PostgreSQL pool создан: %d–%d соединений", pool_min, pool_max)


def check_db() -> None:
    """
    Проверяет, что схема инициализирована (таблица schema_migrations существует
    и содержит текущую версию). Если нет — завершает процесс с понятной ошибкой.

    Вызывается при каждом старте после connect_db().
    """
    try:
        row = _with_db_retries("check_db", _check_db_version_once)
    except Exception as exc:
        if isinstance(exc, SystemExit):
            raise
        if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            raise SystemExit(
                f"\n❌ PostgreSQL сейчас недоступен или рвёт соединение.\n"
                f"   Схема БД может быть уже создана; это не ошибка init_db.py.\n"
                f"   Повтори запуск через минуту или проверь сеть/VPN/сервер БД.\n"
                f"   Детали: {exc}\n"
            ) from exc
        raise SystemExit(
            f"\n❌ БД не инициализирована.\n"
            f"   Запусти один раз: python init_db.py\n"
            f"   Детали: {exc}\n"
        ) from exc
    if not row:
        raise SystemExit(
            f"\n❌ БД не инициализирована или устарела (нужна {SCHEMA_VERSION}).\n"
            f"   Запусти один раз: python init_db.py\n"
        )
    logger.debug("Схема БД OK: %s", SCHEMA_VERSION)


def _check_db_version_once() -> tuple[str] | None:
    connect_db()
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT version FROM schema_migrations WHERE version = %s",
            (SCHEMA_VERSION,),
        )
        return cur.fetchone()


def init_db() -> None:
    """
    ТОЛЬКО ДЛЯ init_db.py — создаёт всю схему и регистрирует версию.
    НЕ вызывать при обычном старте приложения.
    """
    connect_db()
    with _conn() as conn:
        cur = conn.cursor()
        for ddl in (
            DDL_MIGRATIONS, DDL_SETTINGS,
            DDL_TENDERS, DDL_FILTER_SCORES, DDL_RUNS, DDL_DECISIONS,
            DDL_TENDER_CHANGES, DDL_CUSTOMERS, DDL_PRICE_CORRIDORS,
            DDL_KB, DDL_SCORING_RULES, DDL_SEARCH_PROFILES, DDL_INDEXES, DDL_TRIGGER,
        ):
            cur.execute(ddl)
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (SCHEMA_VERSION,),
        )
    logger.info("PostgreSQL схема создана: %s", SCHEMA_VERSION)


def ensure_extra_columns() -> None:
    """
    Идемпотентные ALTER-ы, которые можно безопасно выполнять при каждом старте.
    Не требуют bump SCHEMA_VERSION и перезапуска init_db.py.

    Сейчас: details_json — распарсенные детальные блоки страницы common-info
    (сроки исполнения, финансирование, объект закупки, требования к участникам).
    """
    connect_db()
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS details_json JSONB")
        cur.execute("ALTER TABLE tenders ADD COLUMN IF NOT EXISTS details_checked_at TIMESTAMPTZ")
        # LLM-триаж по карточке (Stage 1) — добавляется без bump SCHEMA_VERSION.
        for ddl in (
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS llm_triage_verdict TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS llm_triage_fit TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS llm_triage_resale BOOLEAN",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS llm_triage_category TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS llm_triage_reason TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS llm_triage_model TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS llm_triage_at TIMESTAMPTZ",
            # MVP2: кеш LLM-объяснения карточки решения (без bump SCHEMA_VERSION).
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS novice_explain JSONB",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS novice_explain_hash TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS novice_explain_model TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS novice_explain_created_at TIMESTAMPTZ",
            # MVP4a: lifecycle-трекинг (доска работы), независим от status/decision.
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS work_stage TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS work_due DATE",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS work_note TEXT",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS work_updated_at TIMESTAMPTZ",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS project_type TEXT",
            # MVP4b-1: спецификация позиций тендера (без FK — уникальность pn не гарантирована).
            """CREATE TABLE IF NOT EXISTS tender_items (
                id SERIAL PRIMARY KEY,
                purchase_number TEXT NOT NULL,
                pos_no INTEGER, name TEXT NOT NULL, qty NUMERIC, unit TEXT,
                requirements TEXT,
                matched_equipment_id INTEGER,
                unit_price NUMERIC, lead_days INTEGER,
                match_score NUMERIC, match_note TEXT,
                source TEXT, note TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            "CREATE INDEX IF NOT EXISTS idx_tender_items_purchase_number ON tender_items(purchase_number)",
            # Поисковые профили (Stage 1): причина попадания тендера в выборку.
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS profile_id INTEGER",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS profile_score INTEGER",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS matched_profiles JSONB",
            # Ценовой коридор Ф2 профиля с лучшим совпадением (наследуется Stage1→Stage2).
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS profile_price_lo INTEGER",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS profile_price_hi INTEGER",
            "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS profile_price_hard_max INTEGER",
        ):
            cur.execute(ddl)


# ── MVP4b-1: спецификация позиций ────────────────────────────────────────────

def list_tender_items(purchase_number: str) -> list[dict[str, Any]]:
    """Позиции спецификации тендера (+ название подобранного товара из каталога)."""
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT ti.*, e.name AS matched_name, e.vendor AS matched_vendor,
                   e.model AS matched_model
              FROM tender_items ti
              LEFT JOIN kb_equipment e ON e.id = ti.matched_equipment_id
             WHERE ti.purchase_number = %s
             ORDER BY ti.pos_no ASC NULLS LAST, ti.id ASC
            """,
            (purchase_number,),
        )
        rows = cur.fetchall()
    return [_to_jsonable(dict(r)) for r in rows]


_ITEM_FIELDS = {
    "pos_no", "name", "qty", "unit", "requirements", "matched_equipment_id",
    "unit_price", "lead_days", "match_score", "match_note", "source", "note",
}


def replace_tender_items(purchase_number: str, items: list[dict[str, Any]]) -> None:
    """Полностью заменяет позиции тендера (delete+insert). Только для извлечения!"""
    if not purchase_number:
        return
    _with_db_retries("replace_tender_items",
                     lambda: _replace_tender_items_once(purchase_number, items))


def _replace_tender_items_once(purchase_number: str, items: list[dict[str, Any]]) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tender_items WHERE purchase_number = %s", (purchase_number,))
        for i, item in enumerate(items or [], start=1):
            fields = {k: item.get(k) for k in _ITEM_FIELDS if k in item}
            fields.setdefault("pos_no", i)
            if not fields.get("name"):
                continue
            cols = ["purchase_number"] + list(fields.keys())
            vals = [purchase_number] + list(fields.values())
            ph = ", ".join(["%s"] * len(cols))
            cur.execute(
                f"INSERT INTO tender_items ({', '.join(cols)}) VALUES ({ph})", vals
            )


def add_tender_item(purchase_number: str, fields: dict[str, Any]) -> int:
    data = {k: fields.get(k) for k in _ITEM_FIELDS if k in fields}
    data["purchase_number"] = purchase_number
    data.setdefault("name", fields.get("name") or "Новая позиция")
    data.setdefault("source", "manual")
    with _conn() as conn:
        cur = conn.cursor()
        cols = list(data.keys())
        ph = ", ".join(["%s"] * len(cols))
        cur.execute(
            f"INSERT INTO tender_items ({', '.join(cols)}) VALUES ({ph}) RETURNING id",
            list(data.values()),
        )
        return int(cur.fetchone()[0])


def update_tender_item(item_id: int, fields: dict[str, Any]) -> None:
    data = {k: fields[k] for k in _ITEM_FIELDS if k in fields}
    if not data:
        return
    sets = [f"{k} = %s" for k in data] + ["updated_at = NOW()"]
    params = list(data.values()) + [item_id]
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE tender_items SET {', '.join(sets)} WHERE id = %s", params)


def delete_tender_item(item_id: int) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tender_items WHERE id = %s", (item_id,))


def get_tender_item(item_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM tender_items WHERE id = %s", (item_id,))
        row = cur.fetchone()
    return _to_jsonable(dict(row)) if row else None


def set_workflow(purchase_number: str, fields: dict[str, Any]) -> None:
    """Частичный апдейт lifecycle-полей тендера (доска работы).

    Обновляет только переданные ключи из {work_stage, work_due, work_note}.
    work_stage == "" → NULL (снять с доски). work_note обрезается до 2000 символов.
    Всегда выставляет work_updated_at = NOW().
    """
    if not purchase_number:
        return
    _with_db_retries("set_workflow", lambda: _set_workflow_once(purchase_number, fields))


def _set_workflow_once(purchase_number: str, fields: dict[str, Any]) -> None:
    allowed = {"work_stage", "work_due", "work_note"}
    sets: list[str] = []
    params: list[Any] = []
    for key in allowed:
        if key not in fields:
            continue
        val = fields[key]
        if key == "work_stage":
            val = val or None            # "" → NULL
        elif key == "work_note" and val:
            val = str(val)[:2000]
        elif key == "work_due":
            val = val or None
        sets.append(f"{key} = %s")
        params.append(val)
    if not sets:
        return
    sets.append("work_updated_at = NOW()")
    sets.append("updated_at = NOW()")
    params.append(purchase_number)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE tenders SET {', '.join(sets)} WHERE purchase_number = %s",
            params,
        )


def list_workflow_tenders() -> list[dict[str, Any]]:
    """Тендеры на доске работы (work_stage задан). Сортировка по внутреннему сроку."""
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT purchase_number, title, customer, price, deadline,
                   work_stage, work_due, work_note, work_updated_at, decision, url
              FROM tenders
             WHERE work_stage IS NOT NULL
             ORDER BY work_due ASC NULLS LAST, work_updated_at DESC NULLS LAST
            """
        )
        rows = cur.fetchall()
    return [_to_jsonable(dict(r)) for r in rows]


def get_novice_explain(purchase_number: str) -> Optional[dict[str, Any]]:
    """Возвращает кеш LLM-объяснения карточки: {explain, hash, model, created_at} или None."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT novice_explain, novice_explain_hash, novice_explain_model,
                      novice_explain_created_at
                 FROM tenders WHERE purchase_number = %s""",
            (purchase_number,),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    explain = row[0]
    if isinstance(explain, str):
        try:
            explain = json.loads(explain)
        except Exception:
            return None
    return {
        "explain": explain,
        "hash": row[1],
        "model": row[2],
        "created_at": row[3].isoformat() if hasattr(row[3], "isoformat") else row[3],
    }


def save_novice_explain(purchase_number: str, explain: dict[str, Any],
                        cache_hash: str, model: str) -> None:
    """Сохраняет (кеширует) LLM-объяснение карточки решения."""
    if not purchase_number:
        return
    _with_db_retries(
        "save_novice_explain",
        lambda: _save_novice_explain_once(purchase_number, explain, cache_hash, model),
    )


def _save_novice_explain_once(purchase_number: str, explain: dict[str, Any],
                              cache_hash: str, model: str) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE tenders
                  SET novice_explain = %s,
                      novice_explain_hash = %s,
                      novice_explain_model = %s,
                      novice_explain_created_at = NOW(),
                      updated_at = NOW()
                WHERE purchase_number = %s""",
            (json.dumps(explain or {}, ensure_ascii=False, default=str),
             cache_hash, model, purchase_number),
        )


def save_tender_details(purchase_number: str, details: dict[str, Any]) -> None:
    """Сохраняет распарсенные детальные блоки лота в tenders.details_json."""
    if not purchase_number:
        return
    _with_db_retries(
        "save_tender_details",
        lambda: _save_tender_details_once(purchase_number, details),
    )


def _save_tender_details_once(purchase_number: str, details: dict[str, Any]) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders
               SET details_json = %s,
                   details_checked_at = NOW(),
                   updated_at = NOW()
             WHERE purchase_number = %s
            """,
            (json.dumps(details or {}, ensure_ascii=False, default=str), purchase_number),
        )


def get_tender_details(purchase_number: str) -> Optional[dict[str, Any]]:
    """Возвращает details_json лота (или None, если ещё не собирали)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT details_json FROM tenders WHERE purchase_number = %s",
            (purchase_number,),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def close_db() -> None:
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception as exc:
            logger.debug("Не удалось закрыть PostgreSQL pool: %s", exc)
        _pool = None


def reconnect_db() -> None:
    """Пересоздаёт пул, чтобы не писать через соединения, простаивавшие во время долгого скрейпинга."""
    close_db()
    connect_db()


def reset_db() -> None:
    """
    Полный сброс всех таблиц проекта и пересоздание схемы.
    Использовать только осознанно — все данные будут удалены.
    """
    connect_db()   # открываем пул если ещё не открыт
    with _conn() as conn:
        cur = conn.cursor()
        for tbl in [
            "kb_templates", "kb_risk_rules", "kb_equipment",
            "kb_competencies", "kb_contracts", "scoring_rules",
            "search_phrases", "search_profiles",
            "filter_scores", "decisions", "tender_changes", "runs",
            "customers", "price_corridors", "settings",
            "schema_migrations", "tenders",
        ]:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS set_tenders_updated_at() CASCADE")
    logger.info("Все таблицы удалены")
    init_db()   # пересоздаём схему и регистрируем версию


def compute_tender_hash(tender: dict[str, Any]) -> str:
    payload = {
        "title": tender.get("title", ""),
        "customer": tender.get("customer", ""),
        "price": tender.get("price"),
        "deadline": tender.get("deadline", ""),
        "url": tender.get("url", ""),
        "published_at": tender.get("published_at", ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_tender_state(purchase_number: str) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM tenders WHERE purchase_number = %s", (purchase_number,))
        row = cur.fetchone()
    return _to_jsonable(dict(row)) if row else None


def get_existing_purchase_numbers(purchase_numbers: list[str]) -> set[str]:
    """Возвращает номера закупок, которые уже есть в таблице tenders."""
    numbers = list({
        str(pnum or "").strip()
        for pnum in purchase_numbers
        if str(pnum or "").strip()
    })
    if not numbers:
        return set()

    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT purchase_number
              FROM tenders
             WHERE purchase_number = ANY(%s)
            """,
            (numbers,),
        )
        return {row[0] for row in cur.fetchall()}


def tender_exists(purchase_number: str) -> bool:
    """Проверяет существование закупки по purchase_number без SELECT *."""
    return bool(get_existing_purchase_numbers([purchase_number]))


def create_manual_tender(
    title: str,
    source_url: str,
    price: float | Decimal | None,
    deadline: str,
) -> str:
    """Создаёт тендер, добавленный пользователем вручную из веб-интерфейса."""
    import uuid

    title = (title or "").strip()
    source_url = (source_url or "").strip()
    deadline = (deadline or "").strip()
    if not title:
        raise ValueError("Название обязательно")
    if not source_url:
        raise ValueError("Источник обязателен")
    if price is not None and Decimal(str(price)) < 0:
        raise ValueError("НМЦК не может быть отрицательной")

    pnum = f"manual-m{uuid.uuid4().hex[:16]}"
    now = _now()
    primary_text = "\n".join([title, source_url, deadline, "manual"])
    score = 24
    reason = "Создан вручную: требуется ручная проверка"
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tenders
                (purchase_number, title, price, deadline, url, platform, project_type,
                 matched_keywords, primary_text, primary_score, primary_reasons,
                 total_score, score, score_reasons, filter_total, filter_decision,
                 status, decision, content_hash, last_changed_at, first_seen_at,
                 last_seen_at, primary_checked_at, created_at, updated_at)
            VALUES
                (%(pnum)s, %(title)s, %(price)s, %(deadline)s, %(url)s, 'manual', 'manual',
                 'manual', %(primary_text)s, %(score)s, %(reason)s,
                 %(score)s, %(score)s, %(reason)s, %(score)s, 'CAUTION',
                 'manual', 'pending', %(content_hash)s, %(now)s, %(now)s,
                 %(now)s, %(now)s, %(now)s, %(now)s)
            """,
            {
                "pnum": pnum,
                "title": title,
                "price": price,
                "deadline": deadline,
                "url": source_url,
                "primary_text": primary_text[:4000],
                "score": score,
                "reason": reason,
                "content_hash": hashlib.sha256(
                    json.dumps(
                        {
                            "title": title,
                            "price": str(price or ""),
                            "deadline": deadline,
                            "url": source_url,
                            "project_type": "manual",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
                "now": now,
            },
        )
    return pnum


def is_seen(purchase_number: str) -> bool:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM tenders WHERE purchase_number = %s LIMIT 1", (purchase_number,))
        return cur.fetchone() is not None


def _is_changed(previous: Optional[dict[str, Any]], content_hash: str) -> bool:
    return bool(previous and previous.get("content_hash") and previous.get("content_hash") != content_hash)


def _record_change(cur, purchase_number: str, change_type: str, old_value: Any, new_value: Any) -> None:
    if str(old_value or "") == str(new_value or ""):
        return
    cur.execute(
        """
        INSERT INTO tender_changes (purchase_number, change_type, old_value, new_value)
        VALUES (%s, %s, %s, %s)
        """,
        (purchase_number, change_type, str(old_value or ""), str(new_value or "")),
    )


def upsert_primary(
    tender: dict[str, Any],
    primary_score: int,
    primary_reasons: list[str],
    matched_keywords: list[str],
) -> str:
    return _with_db_retries(
        "upsert_primary",
        lambda: _upsert_primary_once(tender, primary_score, primary_reasons, matched_keywords),
    )


def _upsert_primary_once(
    tender: dict[str, Any],
    primary_score: int,
    primary_reasons: list[str],
    matched_keywords: list[str],
) -> str:
    """
    Этап 1: сохраняет карточку, ссылку, первичный текст и primary score.

    Если карточка уже была детально разобрана и изменилась 
    (цена/срок/заголовок/ссылка/дата публикации), помечает её на повторный Stage 2.
    Это делает Stage 1 не только инкрементальной загрузкой, но и инкрементальным
    обновлением pipeline.
    """
    now = _now()
    pnum = tender.get("purchase_number", "")
    if not pnum:
        return "unchanged"

    previous = get_tender_state(pnum)
    content_hash = compute_tender_hash(tender)
    changed = _is_changed(previous, content_hash)
    old_keywords = set(filter(None, ((previous or {}).get("matched_keywords") or "").split(";")))
    new_keywords = old_keywords | set(matched_keywords or [])
    status = "primary_candidate" if primary_score >= config.MIN_PRIMARY_SCORE_FOR_DETAIL else "discovered"
    if previous and previous.get("status") not in {None, "", "discovered", "primary_candidate", "detail_rejected"}:
        status = previous["status"]

    primary_text = tender.get("primary_text") or "\n".join(
        str(tender.get(k, "") or "") for k in ["title", "customer", "law_type", "deadline", "published_at"]
    )
    filter_decision = _decision_from_score(primary_score, detailed=False)
    reasons_str = " | ".join(primary_reasons)
    result = "inserted"
    if previous:
        result = "updated" if changed else "unchanged"

    needs_detail_refresh = bool(
        changed
        and previous
        and previous.get("detail_checked_at") is not None
        and primary_score >= config.MIN_PRIMARY_SCORE_FOR_DETAIL
    )

    with _conn() as conn:
        cur = conn.cursor()
        if previous and changed:
            for field in ("title", "customer", "price", "deadline", "url", "published_at"):
                old = previous.get(field)
                new = tender.get(field)
                _record_change(cur, pnum, field, old, new)
            _record_change(cur, pnum, "content_hash", previous.get("content_hash"), content_hash)

        cur.execute(
            """
            INSERT INTO tenders
                (purchase_number, title, customer, price, law_type, deadline, url, platform, region,
                 customer_inn, published_at, matched_keywords, primary_text, primary_score,
                 primary_reasons, total_score, score, score_reasons, filter_total, filter_decision,
                 status, decision, content_hash, last_changed_at, needs_detail_refresh, first_seen_at,
                 last_seen_at, primary_checked_at, updated_at, profile_id, profile_score, matched_profiles,
                 profile_price_lo, profile_price_hi, profile_price_hard_max)
            VALUES
                (%(pnum)s, %(title)s, %(customer)s, %(price)s, %(law_type)s, %(deadline)s, %(url)s,
                 %(platform)s, %(region)s, %(customer_inn)s, %(published_at)s, %(matched_keywords)s,
                 %(primary_text)s, %(primary_score)s, %(primary_reasons)s, %(total_score)s,
                 %(score)s, %(score_reasons)s, %(filter_total)s, %(filter_decision)s,
                 %(status)s, %(decision)s, %(content_hash)s, %(last_changed_at)s, %(needs_detail_refresh)s,
                 %(first_seen_at)s, %(last_seen_at)s, %(primary_checked_at)s, %(updated_at)s,
                 %(profile_id)s, %(profile_score)s, %(matched_profiles)s,
                 %(profile_price_lo)s, %(profile_price_hi)s, %(profile_price_hard_max)s)
            ON CONFLICT (purchase_number) DO UPDATE SET
                title = EXCLUDED.title,
                customer = EXCLUDED.customer,
                price = EXCLUDED.price,
                law_type = EXCLUDED.law_type,
                deadline = EXCLUDED.deadline,
                url = EXCLUDED.url,
                platform = EXCLUDED.platform,
                region = EXCLUDED.region,
                customer_inn = EXCLUDED.customer_inn,
                published_at = EXCLUDED.published_at,
                matched_keywords = EXCLUDED.matched_keywords,
                primary_text = EXCLUDED.primary_text,
                primary_score = EXCLUDED.primary_score,
                primary_reasons = EXCLUDED.primary_reasons,
                total_score = COALESCE(tenders.detail_score, EXCLUDED.primary_score),
                score = EXCLUDED.score,
                score_reasons = EXCLUDED.score_reasons,
                filter_total = CASE
                    WHEN tenders.detail_checked_at IS NOT NULL AND NOT EXCLUDED.needs_detail_refresh THEN tenders.filter_total
                    ELSE EXCLUDED.filter_total
                END,
                filter_decision = CASE
                    WHEN tenders.detail_checked_at IS NOT NULL AND NOT EXCLUDED.needs_detail_refresh THEN tenders.filter_decision
                    ELSE EXCLUDED.filter_decision
                END,
                status = EXCLUDED.status,
                content_hash = EXCLUDED.content_hash,
                last_changed_at = CASE WHEN tenders.content_hash IS DISTINCT FROM EXCLUDED.content_hash THEN EXCLUDED.updated_at ELSE tenders.last_changed_at END,
                needs_detail_refresh = tenders.needs_detail_refresh OR EXCLUDED.needs_detail_refresh,
                last_seen_at = EXCLUDED.last_seen_at,
                primary_checked_at = EXCLUDED.primary_checked_at,
                updated_at = EXCLUDED.updated_at,
                profile_id = EXCLUDED.profile_id,
                profile_score = EXCLUDED.profile_score,
                matched_profiles = EXCLUDED.matched_profiles,
                profile_price_lo = EXCLUDED.profile_price_lo,
                profile_price_hi = EXCLUDED.profile_price_hi,
                profile_price_hard_max = EXCLUDED.profile_price_hard_max
            """,
            {
                "pnum": pnum,
                "title": tender.get("title", ""),
                "customer": tender.get("customer", ""),
                "price": tender.get("price"),
                "law_type": tender.get("law_type", ""),
                "deadline": tender.get("deadline", ""),
                "url": tender.get("url", ""),
                "platform": tender.get("platform", ""),
                "region": tender.get("region", ""),
                "customer_inn": tender.get("customer_inn", ""),
                "published_at": tender.get("published_at", ""),
                "matched_keywords": ";".join(sorted(new_keywords)),
                "primary_text": primary_text[:4000],
                "primary_score": primary_score,
                "primary_reasons": reasons_str,
                "total_score": primary_score,
                "score": primary_score,
                "score_reasons": reasons_str,
                "filter_total": primary_score,
                "filter_decision": filter_decision,
                "status": status,
                "decision": (previous or {}).get("decision", "pending"),
                "content_hash": content_hash,
                "last_changed_at": now if changed or not previous else previous.get("last_changed_at"),
                "needs_detail_refresh": needs_detail_refresh,
                "first_seen_at": (previous or {}).get("first_seen_at") or now,
                "last_seen_at": now,
                "primary_checked_at": now,
                "updated_at": now,
                "profile_id": tender.get("profile_id"),
                "profile_score": tender.get("profile_score"),
                "matched_profiles": json.dumps(tender.get("matched_profiles"), ensure_ascii=False)
                    if tender.get("matched_profiles") is not None else None,
                "profile_price_lo": tender.get("profile_price_lo"),
                "profile_price_hi": tender.get("profile_price_hi"),
                "profile_price_hard_max": tender.get("profile_price_hard_max"),
            },
        )
    return result

def get_detail_candidates(limit: int, min_primary_score: int) -> list[dict[str, Any]]:
    """
    Кандидаты для Stage 2.

    Берём новые сильные карточки и уже разобранные карточки, которые изменились на Stage 1
    и помечены needs_detail_refresh=true.
    """
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT * FROM tenders
             WHERE primary_score >= %s
               AND (detail_checked_at IS NULL OR needs_detail_refresh = TRUE)
               AND decision NOT IN ('rejected', 'tailored')
               AND (
                   deadline IS NULL OR deadline = ''
                   OR deadline !~ '^\\d{2}\\.\\d{2}\\.\\d{4}'
                   OR (
                       CASE
                           WHEN deadline ~ '^\\d{2}\\.\\d{2}\\.\\d{4}\\s+\\d{2}:\\d{2}'
                           THEN to_timestamp(substring(deadline from 1 for 16), 'DD.MM.YYYY HH24:MI')
                           ELSE to_timestamp(substring(deadline from 1 for 10), 'DD.MM.YYYY')
                       END
                   ) >= NOW()
               )
             ORDER BY needs_detail_refresh DESC,
                      -- ближайший дедлайн первым: короткие закупки (ЗМО, котировки)
                      -- должны успевать получить разбор до конца подачи заявок
                      CASE
                          WHEN deadline ~ '^\\d{2}\\.\\d{2}\\.\\d{4}\\s+\\d{2}:\\d{2}'
                          THEN to_timestamp(substring(deadline from 1 for 16), 'DD.MM.YYYY HH24:MI')
                          WHEN deadline ~ '^\\d{2}\\.\\d{2}\\.\\d{4}'
                          THEN to_timestamp(substring(deadline from 1 for 10), 'DD.MM.YYYY')
                          ELSE NULL
                      END ASC NULLS LAST,
                      primary_score DESC, price DESC NULLS LAST
             LIMIT %s
            """,
            (min_primary_score, limit),
        )
        rows = cur.fetchall()
    return _rows_to_dicts(rows)


def get_redocs_candidates(limit: int | None = None) -> list[dict[str, Any]]:
    """Лоты с уже скачанными документами (documents_dir) для офлайн-переобработки.

    Сортировка: активные (дедлайн впереди) первыми, внутри — ближайший дедлайн,
    затем более высокий первичный скор. Просроченные идут в конце (их текст
    всё равно полезен для аналитики и обучения фильтров).
    """
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sql = """
            SELECT * FROM (
                SELECT t.*,
                       CASE
                           WHEN t.deadline ~ '^\\d{2}\\.\\d{2}\\.\\d{4}\\s+\\d{2}:\\d{2}'
                           THEN to_timestamp(substring(t.deadline from 1 for 16), 'DD.MM.YYYY HH24:MI')
                           WHEN t.deadline ~ '^\\d{2}\\.\\d{2}\\.\\d{4}'
                           THEN to_timestamp(substring(t.deadline from 1 for 10), 'DD.MM.YYYY')
                           ELSE NULL
                       END AS _deadline_ts
                  FROM tenders t
                 WHERE t.documents_dir IS NOT NULL AND t.documents_dir <> ''
            ) x
            ORDER BY (_deadline_ts IS NULL) ASC,
                     (_deadline_ts < NOW()) ASC,
                     _deadline_ts ASC,
                     primary_score DESC NULLS LAST
        """
        if limit:
            sql += " LIMIT %s"
            cur.execute(sql, (limit,))
        else:
            cur.execute(sql)
        rows = cur.fetchall()
    result = _rows_to_dicts(rows)
    for row in result:
        row.pop("_deadline_ts", None)
    return result


def get_triage_candidates(limit: int, only_new: bool = True) -> list[dict[str, Any]]:
    """Карточки для LLM-триажа Stage 1: ещё не размеченные, не явный мусор.

    only_new=True — только те, где триаж ещё не делался (llm_triage_at IS NULL).
    Сортировка: сначала более перспективные по первичному скору и цене.
    """
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        where = "WHERE filter_decision <> 'NO-GO' AND status <> 'detail_rejected'"
        if only_new:
            where += " AND llm_triage_at IS NULL"
        cur.execute(
            f"""
            SELECT * FROM tenders
             {where}
             ORDER BY primary_score DESC, price DESC NULLS LAST
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return _rows_to_dicts(rows)


def save_triage(purchase_number: str, triage: dict[str, Any], model: str = "") -> None:
    """Сохраняет результат LLM-триажа карточки (Stage 1)."""
    if not purchase_number:
        return
    _with_db_retries(
        "save_triage",
        lambda: _save_triage_once(purchase_number, triage, model),
    )


def _save_triage_once(purchase_number: str, triage: dict[str, Any], model: str) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders
               SET llm_triage_verdict  = %(verdict)s,
                   llm_triage_fit      = %(fit)s,
                   llm_triage_resale   = %(resale)s,
                   llm_triage_category = %(category)s,
                   llm_triage_reason   = %(reason)s,
                   llm_triage_model    = %(model)s,
                   llm_triage_at       = NOW(),
                   updated_at          = NOW()
             WHERE purchase_number = %(pnum)s
            """,
            {
                "verdict": triage.get("verdict"),
                "fit": triage.get("fit"),
                "resale": bool(triage.get("resale")),
                "category": triage.get("category"),
                "reason": triage.get("reason"),
                "model": model,
                "pnum": purchase_number,
            },
        )


def get_all_tenders_for_rescore(limit: int | None = None) -> list[dict[str, Any]]:
    """Все лоты целиком (SELECT *) для разового пересчёта скоринга без сети/LLM."""
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sql = "SELECT * FROM tenders ORDER BY primary_score DESC NULLS LAST"
        if limit:
            sql += " LIMIT %s"
            cur.execute(sql, (limit,))
        else:
            cur.execute(sql)
        rows = cur.fetchall()
    return _rows_to_dicts(rows)


def update_stage1_after_triage(filter_result: Any) -> None:
    """Принудительно переписывает Stage 1-оценки после LLM-триажа.

    В отличие от save_filter_result(stage1) — обновляет и заголовочные поля
    (primary_score/filter_total/decision), и per-filter строки (Ф1 c учётом
    вердикта). Применяется только к лотам, ещё не прошедшим Stage 2.
    """
    _with_db_retries("update_stage1_after_triage", lambda: _update_stage1_after_triage_once(filter_result))


def _update_stage1_after_triage_once(filter_result: Any) -> None:
    pnum = strip_nul(getattr(filter_result, "purchase_number", ""))
    if not pnum:
        return
    total = getattr(filter_result, "total_score", 0)
    decision = getattr(filter_result, "decision", None)
    stop_factors = strip_nul(getattr(filter_result, "stop_factors", []) or [])
    reasons = " | ".join(filter_result.to_reasons()) if hasattr(filter_result, "to_reasons") else ""

    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders
               SET primary_score   = %(total)s,
                   total_score     = %(total)s,
                   score           = %(total)s,
                   filter_total    = %(total)s,
                   filter_decision = %(decision)s,
                   filter_stop     = %(stop)s,
                   primary_reasons = %(reasons)s,
                   updated_at      = NOW()
             WHERE purchase_number = %(pnum)s
               AND (detail_checked_at IS NULL OR needs_detail_refresh = TRUE)
            """,
            {
                "total": total, "decision": decision,
                "stop": " | ".join(stop_factors), "reasons": reasons, "pnum": pnum,
            },
        )
        for f in getattr(filter_result, "filters", []) or []:
            cur.execute(
                """
                INSERT INTO filter_scores
                    (purchase_number, filter_number, filter_name, score, signals, stop_factor)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (purchase_number, filter_number) DO UPDATE SET
                    filter_name = EXCLUDED.filter_name,
                    score       = EXCLUDED.score,
                    signals     = EXCLUDED.signals,
                    stop_factor = EXCLUDED.stop_factor
                """,
                (
                    pnum, getattr(f, "number", 0), getattr(f, "name", ""),
                    getattr(f, "score", 0), " | ".join(getattr(f, "signals", []) or []),
                    bool(getattr(f, "stop_factor", False)),
                ),
            )


def save_detail(
    tender: dict[str, Any],
    detail_score: int,
    detail_reasons: list[str],
    llm_analysis: str = "",
    document_text: str = "",
    notified: bool = False,
) -> None:
    # psycopg2 отклоняет любой строковый параметр с NUL, поэтому очищаем
    # весь набор данных до входа в retry/транзакцию.
    clean_tender = strip_nul(tender)
    clean_reasons = strip_nul(detail_reasons)
    clean_llm = strip_nul(llm_analysis or "")
    clean_document_text = strip_nul(document_text or "")
    _with_db_retries(
        "save_detail",
        lambda: _save_detail_once(
            clean_tender,
            detail_score,
            clean_reasons,
            clean_llm,
            clean_document_text,
            notified,
        ),
    )


def _save_detail_once(
    tender: dict[str, Any],
    detail_score: int,
    detail_reasons: list[str],
    llm_analysis: str = "",
    document_text: str = "",
    notified: bool = False,
) -> None:
    now = _now()
    filter_decision_in_tender = tender.get("filter_decision", "")
    status = "detail_rejected" if filter_decision_in_tender == "NO-GO" else "detail_candidate"
    notified_at = now if notified else tender.get("notified_at")
    filter_decision = _decision_from_score(detail_score, detailed=True)
    llm_verdict = _extract_llm_verdict(llm_analysis)
    reasons_str = " | ".join(detail_reasons)

    previous = get_tender_state(tender.get("purchase_number"))

    with _conn() as conn:
        cur = conn.cursor()
        if previous and tender.get("documents_hash") and previous.get("documents_hash") != tender.get("documents_hash"):
            _record_change(cur, tender.get("purchase_number"), "documents_hash", previous.get("documents_hash"), tender.get("documents_hash"))

        cur.execute(
            """
            UPDATE tenders
               SET detail_score = %(detail_score)s,
                   detail_reasons = %(detail_reasons)s,
                   total_score = %(detail_score)s,
                   score = %(detail_score)s,
                   score_reasons = %(detail_reasons)s,
                   filter_total = %(detail_score)s,
                   filter_decision = %(filter_decision)s,
                   llm_analysis = %(llm_analysis)s,
                   llm_verdict = %(llm_verdict)s,
                   application_security_amount = %(application_security_amount)s,
                   contract_security_amount = %(contract_security_amount)s,
                   warranty_security_amount = %(warranty_security_amount)s,
                   advance_percent = %(advance_percent)s,
                   payment_terms = %(payment_terms)s,
                   execution_days = %(execution_days)s,
                   document_count = %(document_count)s,
                   documents_dir = %(documents_dir)s,
	                   documents_hash = %(documents_hash)s,
	                   document_text_excerpt = %(document_text_excerpt)s,
	                   url = COALESCE(NULLIF(%(url)s, ''), url),
	                   status = %(status)s,
	                   detail_checked_at = %(detail_checked_at)s,
                   needs_detail_refresh = FALSE,
                   notified_at = %(notified_at)s,
                   updated_at = %(updated_at)s
             WHERE purchase_number = %(purchase_number)s
            """,
            {
                "detail_score": detail_score,
                "detail_reasons": reasons_str,
                "filter_decision": filter_decision,
                "llm_analysis": llm_analysis or "",
                "llm_verdict": llm_verdict,
                "application_security_amount": tender.get("application_security_amount"),
                "contract_security_amount": tender.get("contract_security_amount"),
                "warranty_security_amount": tender.get("warranty_security_amount"),
                "advance_percent": tender.get("advance_percent"),
                "payment_terms": tender.get("payment_terms", ""),
                "execution_days": tender.get("execution_days"),
                "document_count": tender.get("document_count", 0),
                "documents_dir": tender.get("documents_dir", ""),
                "documents_hash": tender.get("documents_hash", ""),
	                "document_text_excerpt": (document_text or "")[:4000],
	                "url": tender.get("url", ""),
	                "status": status,
                "detail_checked_at": now,
                "notified_at": notified_at,
                "updated_at": now,
                "purchase_number": tender.get("purchase_number"),
            },
        )



def get_result_candidates(limit: int = 50) -> list[dict[str, Any]]:
    """
    Кандидаты для Stage 3: лоты с прошедшим дедлайном, по которым ещё не подтягивали
    результат/протоколы. Дата дедлайна хранится строкой, поэтому используем best-effort regex.
    """
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT * FROM tenders
             WHERE deadline IS NOT NULL
               AND deadline <> ''
               AND result_checked_at IS NULL
               AND decision NOT IN ('rejected', 'tailored')
             ORDER BY deadline ASC, filter_total DESC NULLS LAST
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    # Финальную фильтрацию по факту прошедшего дедлайна делает Python: форматы в ЕИС бывают разные.
    return _rows_to_dicts(rows)


def save_result(purchase_number: str, result: dict[str, Any]) -> None:
    now = _now()
    status = result.get("status") or "result_checked"
    previous = get_tender_state(purchase_number) or {}
    with _conn() as conn:
        cur = conn.cursor()
        for field in ("winner_name", "winner_inn", "final_price", "participants_count", "price_drop_percent", "status"):
            if field in result:
                _record_change(cur, purchase_number, field, previous.get(field), result.get(field))
        cur.execute(
            """
            UPDATE tenders SET
                status = %s,
                result_checked_at = %s,
                winner_name = COALESCE(%s, winner_name),
                winner_inn = COALESCE(%s, winner_inn),
                final_price = COALESCE(%s, final_price),
                participants_count = COALESCE(%s, participants_count),
                price_drop_percent = COALESCE(%s, price_drop_percent),
                updated_at = %s
            WHERE purchase_number = %s
            """,
            (
                status,
                now,
                result.get("winner_name"),
                result.get("winner_inn"),
                result.get("final_price"),
                result.get("participants_count"),
                result.get("price_drop_percent"),
                now,
                purchase_number,
            ),
        )


def set_decision(purchase_number: str, decision: str, comment: str = "") -> None:
    now = _now()
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenders SET decision = %s, status = %s, updated_at = %s WHERE purchase_number = %s",
            (decision, decision, now, purchase_number),
        )
        cur.execute(
            "INSERT INTO decisions (purchase_number, decision, comment, created_at) VALUES (%s, %s, %s, %s)",
            (purchase_number, decision, comment, now),
        )


def log_run(mode: str, started_at: str, found: int = 0, processed: int = 0, notified: int = 0, errors: str = "") -> None:
    _with_db_retries("log_run", lambda: _log_run_once(mode, started_at, found, processed, notified, errors))


def _log_run_once(mode: str, started_at: str, found: int = 0, processed: int = 0, notified: int = 0, errors: str = "") -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO runs (mode, started_at, found, processed, notified, errors)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (mode, started_at, found, processed, notified, errors),
        )


def was_stage_completed_today(mode: str) -> bool:
    return _with_db_retries("was_stage_completed_today", lambda: _was_stage_completed_today_once(mode))


def _was_stage_completed_today_once(mode: str) -> bool:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1
            FROM runs
            WHERE mode = %s
              AND created_at >= date_trunc('day', now())
              AND COALESCE(errors, '') = ''
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (mode,),
        )
        return cur.fetchone() is not None


def get_last_successful_run_started(mode: str) -> Optional[datetime]:
    return _with_db_retries(
        "get_last_successful_run_started",
        lambda: _get_last_successful_run_started_once(mode),
    )


def _get_last_successful_run_started_once(mode: str) -> Optional[datetime]:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT started_at
            FROM runs
            WHERE mode = %s
              AND COALESCE(errors, '') = ''
            ORDER BY started_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            (mode,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def get_stats() -> dict[str, int]:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tenders")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenders WHERE primary_score >= %s", (config.MIN_PRIMARY_SCORE_FOR_DETAIL,))
        primary_candidates = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenders WHERE detail_checked_at IS NOT NULL")
        detailed = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenders WHERE notified_at IS NOT NULL")
        sent = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenders WHERE decision = 'interesting'")
        interesting = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenders WHERE decision = 'rejected'")
        rejected = cur.fetchone()[0]
    return {
        "total": int(total),
        "primary_candidates": int(primary_candidates),
        "detailed": int(detailed),
        "sent": int(sent),
        "interesting": int(interesting),
        "rejected": int(rejected),
    }


def get_filter_scores(purchase_number: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT filter_number, filter_name, score, signals, stop_factor
            FROM   filter_scores
            WHERE  purchase_number = %s
            ORDER  BY filter_number
            """,
            (purchase_number,),
        )
        rows = cur.fetchall()
    return [_to_jsonable(dict(r)) for r in rows]


def get_distinct_platforms() -> list[str]:
    """Список источников (platform), реально присутствующих в активных лотах —
    для фасета «Источник» на дашборде. Пустой/NULL platform нормализуем в 'ЕИС'."""
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT DISTINCT COALESCE(NULLIF(btrim(platform), ''), 'ЕИС') AS p
                     FROM tenders
                    WHERE decision IS DISTINCT FROM 'rejected'
                    ORDER BY p"""
            )
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def get_top_tenders(
    decision:  str | None = "GO",
    limit:     int        = 200,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    law_type:  Optional[str]   = None,
    matched_keyword: Optional[str] = None,   # фасет: фраза, по которой найден тендер
    profile_id: Optional[int] = None,         # фасет: поисковый профиль (search_profiles)
    platforms: Optional[list[str]] = None,    # фасет: источники (ЕИС/ЕАТ/…); пусто = все
    q: Optional[str] = None,                  # свободный текст: название/заказчик/номер
    exclude_keywords: Optional[list[str]] = None,
    # Per-filter минимальные оценки (1–5) для Ф1–Ф8
    f1_min: Optional[int] = None,
    f2_min: Optional[int] = None,
    f3_min: Optional[int] = None,
    f4_min: Optional[int] = None,
    f5_min: Optional[int] = None,
    f6_min: Optional[int] = None,
    f7_min: Optional[int] = None,
    f8_min: Optional[int] = None,
    sort_by: str = "score",          # score | price | phrase | date | deadline
    order:   str = "desc",           # asc | desc
    active_only: bool = True,
    manual_decision: Optional[str] = None,
    include_rejected: bool = False,
) -> list[dict[str, Any]]:
    """
    Возвращает тендеры с оценками всех 8 фильтров в одном SQL-запросе (без N+1).
    Поддерживает фильтрацию по минимальному баллу каждого фильтра отдельно.
    Сортировка: score (filter_total), price, phrase, date (created_at), deadline.

    Пример:
        # Только GO-тендеры, где нет заточки (Ф6≥3) и нормальная экономика (Ф2≥3):
        db.get_top_tenders(decision="GO", f6_min=3, f2_min=3)
    """
    conditions: list[str] = []
    params:     list[Any] = []

    if decision:
        conditions.append("t.filter_decision = %s")
        params.append(decision)
    if manual_decision:
        conditions.append("t.decision = %s")
        params.append(manual_decision)
    elif not include_rejected:
        conditions.append("t.decision IS DISTINCT FROM 'rejected'")
    if active_only:
        conditions.append(_active_or_unknown_deadline_sql("t.deadline"))
    if price_min is not None:
        conditions.append("t.price >= %s")
        params.append(price_min)
    if price_max is not None:
        conditions.append("t.price <= %s")
        params.append(price_max)
    if law_type:
        conditions.append("t.law_type = %s")
        params.append(law_type)
    if matched_keyword:
        conditions.append("t.matched_keywords ILIKE %s")
        params.append(f"%{matched_keyword}%")
    if profile_id is not None:
        conditions.append("t.profile_id = %s")
        params.append(int(profile_id))
    plats = [str(p).strip() for p in (platforms or []) if str(p).strip()]
    if plats:
        # Пустой platform считаем как ЕИС (исторические лоты без явного источника).
        if "ЕИС" in plats:
            conditions.append("(t.platform = ANY(%s) OR t.platform IS NULL OR btrim(t.platform) = '')")
        else:
            conditions.append("t.platform = ANY(%s)")
        params.append(plats)
    if q and q.strip():
        # Свободный поиск по уже загруженным лотам (без скрейпинга):
        # название, заказчик, номер закупки, категория триажа.
        like = f"%{q.strip()}%"
        conditions.append(
            "(t.title ILIKE %s OR t.customer ILIKE %s OR t.purchase_number ILIKE %s "
            "OR t.llm_triage_category ILIKE %s)"
        )
        params.extend([like, like, like, like])
    for keyword in exclude_keywords or []:
        kw = (keyword or "").strip()
        if kw:
            conditions.append(
                """
                CONCAT_WS(' ',
                    t.title, t.customer, t.purchase_number, t.matched_keywords,
                    t.primary_text, t.document_text_excerpt, t.llm_triage_category,
                    t.llm_triage_reason, t.filter_stop
                ) NOT ILIKE %s
                """
            )
            params.append(f"%{kw}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # HAVING: фильтрация по минимальным баллам отдельных фильтров
    having_parts: list[str] = []
    for fn, fmin in enumerate(
        [f1_min, f2_min, f3_min, f4_min, f5_min, f6_min, f7_min, f8_min], start=1
    ):
        if fmin is not None:
            having_parts.append(
                f"COALESCE(MAX(CASE WHEN fs.filter_number = {fn} THEN fs.score END), 0) >= %s"
            )
            params.append(fmin)

    having = ("HAVING " + " AND ".join(having_parts)) if having_parts else ""

    sort_col = {
        "score":    "t.filter_total",
        "price":    "t.price",
        "phrase":   "LOWER(t.matched_keywords)",
        "date":     "t.created_at",
        # Дедлайн хранится строкой (DD.MM.YYYY [HH:MM]); сортируем по разобранному
        # таймстампу, иначе алфавитная сортировка мешает дни/месяцы.
        "deadline": _deadline_timestamp_sql("t.deadline"),
    }.get((sort_by or "score").lower(), "t.filter_total")
    sort_dir = "ASC" if (order or "desc").lower() == "asc" else "DESC"
    order_by = f"{sort_col} {sort_dir} NULLS LAST, t.created_at DESC"

    params.append(limit)

    sql = f"""
        SELECT
            t.purchase_number, t.title, t.customer, t.price, t.law_type,
            t.deadline, t.url, t.score, t.score_reasons, t.primary_score,
            t.detail_score, t.total_score, t.filter_total, t.filter_decision,
            t.filter_stop, t.llm_verdict, t.notified_at, t.decision,
            t.status, t.created_at, t.published_at, t.matched_keywords,
            t.llm_triage_verdict, t.llm_triage_fit, t.llm_triage_resale,
            t.llm_triage_category, t.llm_triage_reason,
            t.profile_id, t.profile_score, t.matched_profiles, t.platform,

            -- Агрегируем 8 фильтров в один JSON-объект — без N+1 запросов
            json_object_agg(
                fs.filter_number::text,
                json_build_object(
                    'filter_number', fs.filter_number,
                    'filter_name',   fs.filter_name,
                    'score',         fs.score,
                    'signals',       fs.signals,
                    'stop_factor',   fs.stop_factor
                )
            ) FILTER (WHERE fs.filter_number IS NOT NULL) AS _filter_scores_raw

        FROM tenders t
        LEFT JOIN filter_scores fs ON fs.purchase_number = t.purchase_number
        {where}
        GROUP BY
            t.id, t.purchase_number, t.title, t.customer, t.price, t.law_type,
            t.deadline, t.url, t.score, t.score_reasons, t.primary_score,
            t.detail_score, t.total_score, t.filter_total, t.filter_decision,
            t.filter_stop, t.llm_verdict, t.notified_at, t.decision,
            t.status, t.created_at, t.published_at, t.matched_keywords,
            t.profile_id, t.profile_score, t.matched_profiles, t.platform
        {having}
        ORDER BY {order_by}
        LIMIT %s
    """

    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()

    result = []
    for row in rows:
        d = _to_jsonable(dict(row))
        raw_map = d.pop("_filter_scores_raw", None) or {}
        d["filter_scores"] = _unpack_filter_scores(raw_map)
        result.append(d)
    return result


def _unpack_filter_scores(raw: dict) -> list[dict[str, Any]]:
    """
    Разворачивает {'1': {filter_number, filter_name, score, signals, stop_factor}, ...}
    в список, отсортированный по filter_number.
    Signals хранятся строкой через |, превращаем в список.
    """
    if not raw:
        return []
    out = []
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        signals_raw = v.get("signals", "")
        signals_list = (
            [s.strip() for s in signals_raw.split("|") if s.strip()]
            if isinstance(signals_raw, str)
            else (signals_raw or [])
        )
        out.append({
            "filter_number": int(k),
            "filter_name":   v.get("filter_name", f"Ф{k}"),
            "score":         v.get("score", 0),
            "signals":       signals_list,
            "stop_factor":   bool(v.get("stop_factor")),
        })
    out.sort(key=lambda x: x["filter_number"])
    return out


def get_stats_extended() -> dict[str, Any]:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tenders")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM tenders WHERE notified_at IS NOT NULL")
        sent = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM tenders WHERE detail_checked_at IS NOT NULL")
        detailed = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM tenders WHERE decision = 'pending' AND notified_at IS NOT NULL")
        pending = cur.fetchone()[0]

        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE filter_decision = 'GO' AND decision IS DISTINCT FROM 'rejected')      AS go_count,
                COUNT(*) FILTER (WHERE filter_decision = 'CAUTION' AND decision IS DISTINCT FROM 'rejected') AS caution_count,
                COUNT(*) FILTER (WHERE filter_decision = 'NO-GO' AND decision IS DISTINCT FROM 'rejected')   AS nogo_count,
                COUNT(*) FILTER (WHERE filter_decision IS NULL)     AS unscored,
                COUNT(*) FILTER (WHERE decision = 'rejected')        AS rejected_count,
                ROUND(AVG(filter_total), 1)                         AS avg_score
            FROM tenders
        """)
        go_c, caution_c, nogo_c, unscored_c, rejected_c, avg_score = cur.fetchone()

        cur.execute("""
            SELECT customer, COUNT(*) AS cnt
            FROM tenders
            WHERE customer IS NOT NULL AND customer <> ''
            GROUP BY customer ORDER BY cnt DESC LIMIT 5
        """)
        top_customers = [{"customer": r[0], "count": int(r[1])} for r in cur.fetchall()]

        cur.execute("SELECT MIN(price), MAX(price), ROUND(AVG(price), 0) FROM tenders WHERE price > 0")
        pmin, pmax, pavg = cur.fetchone()

        # Заказчики и коридоры — для контрольной панели
        try:
            cur.execute("SELECT COUNT(*) FROM customers")
            n_customers = cur.fetchone()[0]
        except Exception:
            n_customers = 0

        try:
            cur.execute("SELECT COUNT(*) FROM price_corridors")
            n_corridors = cur.fetchone()[0]
        except Exception:
            n_corridors = 0

        # Кандидаты для Stage2
        try:
            cur.execute("""
                SELECT COUNT(*) FROM tenders
                WHERE primary_score >= (SELECT COALESCE(value::int, 24) FROM settings WHERE key='MIN_PRIMARY_SCORE_FOR_DETAIL' LIMIT 1)
                  AND detail_checked_at IS NULL
            """)
            primary_candidates = cur.fetchone()[0]
        except Exception:
            primary_candidates = 0

    return _to_jsonable({
        "total":             total,
        "sent":              sent,
        "detailed":          detailed,
        "pending":           pending,
        "primary_candidates":primary_candidates,
        # Псевдонимы для совместимости с шаблонами (index.html и control.html)
        "go":                go_c      or 0,
        "caution":           caution_c or 0,
        "nogo":              nogo_c    or 0,
        "filter_go":         go_c      or 0,
        "filter_caution":    caution_c or 0,
        "filter_nogo":       nogo_c    or 0,
        "filter_unscored":   unscored_c or 0,
        "rejected":          rejected_c or 0,
        "avg_filter_score":  avg_score  or 0,
        "customers":         n_customers,
        "corridors":         n_corridors,
        "top_customers":     top_customers,
        "price_range":       {"min": pmin, "max": pmax, "avg": pavg},
    })


def get_tender(purchase_number: str) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM tenders WHERE purchase_number = %s", (purchase_number,))
        row = cur.fetchone()
    if not row:
        return None
    result = _to_jsonable(dict(row))
    result["filter_scores"] = get_filter_scores(purchase_number)
    # Удобные алиасы для шаблонов черновика
    result.setdefault("score_reasons", result.get("detail_reasons") or result.get("primary_reasons") or "")
    result.setdefault("score", result.get("detail_score") or result.get("primary_score") or 0)
    return result


# Совместимость с черновиком database_pg.py: если позже появится filter_engine.py,
# можно будет записывать отдельные 8 фильтров без изменения web_app.
def save_filter_result(filter_result: Any, stage: str = "stage1") -> None:
    # filter_result — объект, поэтому очищаем строковые атрибуты в момент
    # формирования SQL-параметров внутри _save_filter_result_once.
    _with_db_retries("save_filter_result", lambda: _save_filter_result_once(filter_result, stage))


def _save_filter_result_once(filter_result: Any, stage: str = "stage1") -> None:
    """
    Сохраняет результаты фильтрации.

    stage="stage1" — Stage1 НЕ перезаписывает оценки фильтров, если Stage2 уже прошёл
                      и контент тендера не изменился (needs_detail_refresh = FALSE).
                      Это защищает детальные оценки от затирания поверхностными.

    stage="stage2" — всегда записывает (Stage2 главнее Stage1).
    """
    pnum = strip_nul(getattr(filter_result, "purchase_number", ""))
    if not pnum:
        return

    stop_factors = strip_nul(getattr(filter_result, "stop_factors", []) or [])

    with _conn() as conn:
        cur = conn.cursor()

        if stage == "stage1":
            # Обновляем filter_total/decision только если Stage2 ещё не прошёл
            # или если контент изменился (needs_detail_refresh = TRUE).
            cur.execute(
                """
                UPDATE tenders
                SET    filter_total    = %s,
                       filter_decision = %s,
                       filter_stop     = %s
                WHERE  purchase_number = %s
                  AND  (detail_checked_at IS NULL OR needs_detail_refresh = TRUE)
                """,
                (
                    getattr(filter_result, "total_score", None),
                    getattr(filter_result, "decision", None),
                    " | ".join(stop_factors),
                    pnum,
                ),
            )
        else:
            # Stage2 — пишем всегда
            cur.execute(
                """
                UPDATE tenders
                SET    filter_total    = %s,
                       filter_decision = %s,
                       filter_stop     = %s
                WHERE  purchase_number = %s
                """,
                (
                    getattr(filter_result, "total_score", None),
                    getattr(filter_result, "decision", None),
                    " | ".join(stop_factors),
                    pnum,
                ),
            )

        for f in getattr(filter_result, "filters", []) or []:
            if stage == "stage1":
                # Stage1: INSERT только если записи ещё нет.
                # Если Stage2 уже поставил оценку — не трогаем.
                cur.execute(
                    """
                    INSERT INTO filter_scores
                        (purchase_number, filter_number, filter_name, score, signals, stop_factor)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (purchase_number, filter_number) DO NOTHING
                    """,
                    (
                        pnum,
                        getattr(f, "number", 0),
                        strip_nul(getattr(f, "name", "")),
                        getattr(f, "score", 0),
                        " | ".join(strip_nul(getattr(f, "signals", []) or [])),
                        bool(getattr(f, "stop_factor", False)),
                    ),
                )
            else:
                # Stage2: всегда перезаписываем (детальный анализ точнее)
                cur.execute(
                    """
                    INSERT INTO filter_scores
                        (purchase_number, filter_number, filter_name, score, signals, stop_factor)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (purchase_number, filter_number) DO UPDATE SET
                        filter_name = EXCLUDED.filter_name,
                        score       = EXCLUDED.score,
                        signals     = EXCLUDED.signals,
                        stop_factor = EXCLUDED.stop_factor
                    """,
                    (
                        pnum,
                        getattr(f, "number", 0),
                        strip_nul(getattr(f, "name", "")),
                        getattr(f, "score", 0),
                        " | ".join(strip_nul(getattr(f, "signals", []) or [])),
                        bool(getattr(f, "stop_factor", False)),
                    ),
                )


def overwrite_filter_result(filter_result: Any) -> None:
    """
    Принудительно перезаписывает результат фильтрации (для пересчёта по деталям).

    В отличие от save_filter_result(stage1), который защищает оценки гардами и
    INSERT … ON CONFLICT DO NOTHING, здесь баллы Ф1..Ф8 и решение перетираются.
    Используется в rescore.py, когда мы досчитываем профиль по собранным details_json.
    """
    pnum = getattr(filter_result, "purchase_number", "")
    if not pnum:
        return
    score = int(getattr(filter_result, "total_score", 0) or 0)
    decision = getattr(filter_result, "decision", None)
    stop = " | ".join(getattr(filter_result, "stop_factors", []) or [])
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders
               SET filter_total = %s, filter_decision = %s, filter_stop = %s,
                   total_score = %s, score = %s,
                   primary_score = %s, updated_at = NOW()
             WHERE purchase_number = %s
            """,
            (score, decision, stop, score, score, score, pnum),
        )
        for f in getattr(filter_result, "filters", []) or []:
            cur.execute(
                """
                INSERT INTO filter_scores
                    (purchase_number, filter_number, filter_name, score, signals, stop_factor)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (purchase_number, filter_number) DO UPDATE SET
                    filter_name = EXCLUDED.filter_name,
                    score       = EXCLUDED.score,
                    signals     = EXCLUDED.signals,
                    stop_factor = EXCLUDED.stop_factor
                """,
                (
                    pnum, getattr(f, "number", 0), getattr(f, "name", ""),
                    getattr(f, "score", 0),
                    " | ".join(getattr(f, "signals", []) or []),
                    bool(getattr(f, "stop_factor", False)),
                ),
            )


def save_llm_verdict(purchase_number: str, llm_verdict: str) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE tenders SET llm_verdict = %s WHERE purchase_number = %s", (llm_verdict, purchase_number))


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMERS — профили заказчиков
# ══════════════════════════════════════════════════════════════════════════════

def upsert_customer(data: dict[str, Any]) -> None:
    """Создаёт или обновляет профиль заказчика. data обязан содержать 'inn'."""
    inn = data.get("inn")
    if not inn:
        return
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO customers (
                inn, name, total_contracts, terminated_contracts,
                avg_drop_pct, avg_participants,
                repeat_winner_inn, repeat_winner_name, repeat_winner_share,
                monopoly_flag, arbitration_count, last_arbitration_date,
                reliability_score, notes, raw_json, updated_at
            ) VALUES (
                %(inn)s, %(name)s, %(total_contracts)s, %(terminated_contracts)s,
                %(avg_drop_pct)s, %(avg_participants)s,
                %(repeat_winner_inn)s, %(repeat_winner_name)s, %(repeat_winner_share)s,
                %(monopoly_flag)s, %(arbitration_count)s, %(last_arbitration_date)s,
                %(reliability_score)s, %(notes)s, %(raw_json)s, NOW()
            )
            ON CONFLICT (inn) DO UPDATE SET
                name                  = COALESCE(EXCLUDED.name, customers.name),
                total_contracts       = EXCLUDED.total_contracts,
                terminated_contracts  = EXCLUDED.terminated_contracts,
                avg_drop_pct          = EXCLUDED.avg_drop_pct,
                avg_participants      = EXCLUDED.avg_participants,
                repeat_winner_inn     = EXCLUDED.repeat_winner_inn,
                repeat_winner_name    = EXCLUDED.repeat_winner_name,
                repeat_winner_share   = EXCLUDED.repeat_winner_share,
                monopoly_flag         = EXCLUDED.monopoly_flag,
                arbitration_count     = EXCLUDED.arbitration_count,
                last_arbitration_date = COALESCE(EXCLUDED.last_arbitration_date, customers.last_arbitration_date),
                reliability_score     = EXCLUDED.reliability_score,
                notes                 = COALESCE(EXCLUDED.notes, customers.notes),
                raw_json              = EXCLUDED.raw_json,
                updated_at            = NOW()
        """, {
            "inn":                   inn,
            "name":                  data.get("name"),
            "total_contracts":       data.get("total_contracts", 0),
            "terminated_contracts":  data.get("terminated_contracts", 0),
            "avg_drop_pct":          data.get("avg_drop_pct"),
            "avg_participants":      data.get("avg_participants"),
            "repeat_winner_inn":     data.get("repeat_winner_inn"),
            "repeat_winner_name":    data.get("repeat_winner_name"),
            "repeat_winner_share":   data.get("repeat_winner_share"),
            "monopoly_flag":         data.get("monopoly_flag", False),
            "arbitration_count":     data.get("arbitration_count", 0),
            "last_arbitration_date": data.get("last_arbitration_date"),
            "reliability_score":     data.get("reliability_score", 3),
            "notes":                 data.get("notes"),
            "raw_json":              json.dumps(data.get("raw_json") or {}, ensure_ascii=False),
        })


def get_customer(inn: str) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM customers WHERE inn = %s", (inn,))
        row = cur.fetchone()
    return _to_jsonable(dict(row)) if row else None


def get_customers_list(limit: int = 100, only_risky: bool = False) -> list[dict[str, Any]]:
    where = "WHERE monopoly_flag = TRUE OR reliability_score <= 2" if only_risky else ""
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"SELECT * FROM customers {where} ORDER BY reliability_score ASC, total_contracts DESC LIMIT %s",
            (limit,),
        )
        return [_to_jsonable(dict(r)) for r in cur.fetchall()]


def get_customer_inns_to_score(limit: int = 50) -> list[str]:
    """ИНН заказчиков из наших тендеров, у которых ещё нет профиля в customers."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT t.customer_inn
            FROM tenders t
            LEFT JOIN customers c ON c.inn = t.customer_inn
            WHERE t.customer_inn IS NOT NULL
              AND t.customer_inn <> ''
              AND c.inn IS NULL
            ORDER BY t.customer_inn
            LIMIT %s
        """, (limit,))
        return [r[0] for r in cur.fetchall()]


def get_stale_customer_inns(days: int = 7, limit: int = 30) -> list[str]:
    """ИНН заказчиков, чей профиль устарел (обновлялся более N дней назад)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT inn FROM customers
            WHERE updated_at < NOW() - INTERVAL '%s days'
            ORDER BY updated_at ASC
            LIMIT %s
        """, (days, limit))
        return [r[0] for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════════════════════════
# PRICE CORRIDORS — ценовые коридоры по категориям
# ══════════════════════════════════════════════════════════════════════════════

def upsert_price_corridor(data: dict[str, Any]) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO price_corridors (
                category, law_type, sample_count,
                avg_drop_pct, p25_drop_pct, p50_drop_pct, p75_drop_pct,
                min_drop_pct, max_drop_pct, avg_participants, updated_at
            ) VALUES (
                %(category)s, %(law_type)s, %(sample_count)s,
                %(avg_drop_pct)s, %(p25_drop_pct)s, %(p50_drop_pct)s, %(p75_drop_pct)s,
                %(min_drop_pct)s, %(max_drop_pct)s, %(avg_participants)s, NOW()
            )
            ON CONFLICT (category, law_type) DO UPDATE SET
                sample_count     = EXCLUDED.sample_count,
                avg_drop_pct     = EXCLUDED.avg_drop_pct,
                p25_drop_pct     = EXCLUDED.p25_drop_pct,
                p50_drop_pct     = EXCLUDED.p50_drop_pct,
                p75_drop_pct     = EXCLUDED.p75_drop_pct,
                min_drop_pct     = EXCLUDED.min_drop_pct,
                max_drop_pct     = EXCLUDED.max_drop_pct,
                avg_participants = EXCLUDED.avg_participants,
                updated_at       = NOW()
        """, data)


def get_price_corridor(category: str, law_type: str = "all") -> Optional[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM price_corridors WHERE category = %s AND law_type = %s",
            (category, law_type),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "SELECT * FROM price_corridors WHERE category = %s ORDER BY sample_count DESC LIMIT 1",
                (category,),
            )
            row = cur.fetchone()
    return _to_jsonable(dict(row)) if row else None


def get_all_price_corridors() -> list[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM price_corridors ORDER BY category, law_type"
        )
        return [_to_jsonable(dict(r)) for r in cur.fetchall()]


def rebuild_corridors_from_results() -> int:
    """
    Пересчитывает price_corridors из данных о результатах,
    уже сохранённых в таблице tenders (Stage3).
    Возвращает количество обновлённых записей.
    """
    import json as _json
    from winner_analytics import classify_category

    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT title, law_type, price, final_price, price_drop_percent, participants_count
            FROM tenders
            WHERE final_price IS NOT NULL
              AND price_drop_percent IS NOT NULL
              AND price_drop_percent BETWEEN 0 AND 60
        """)
        rows = cur.fetchall()

    if not rows:
        return 0

    from collections import defaultdict
    import statistics

    buckets: dict[tuple, list] = defaultdict(list)
    for r in rows:
        cat = classify_category(r["title"] or "")
        law = r["law_type"] or "all"
        drop = float(r["price_drop_percent"])
        parts = r["participants_count"] or 0
        buckets[(cat, law)].append((drop, parts))
        buckets[(cat, "all")].append((drop, parts))

    count = 0
    for (cat, law), items in buckets.items():
        drops = sorted(v[0] for v in items)
        parts = [v[1] for v in items if v[1] > 0]
        n = len(drops)
        if n < 3:
            continue

        def pct(lst, p):
            idx = max(0, int(len(lst) * p / 100) - 1)
            return round(lst[idx], 2)

        upsert_price_corridor({
            "category":         cat,
            "law_type":         law,
            "sample_count":     n,
            "avg_drop_pct":     round(statistics.mean(drops), 2),
            "p25_drop_pct":     pct(drops, 25),
            "p50_drop_pct":     pct(drops, 50),
            "p75_drop_pct":     pct(drops, 75),
            "min_drop_pct":     round(min(drops), 2),
            "max_drop_pct":     round(max(drops), 2),
            "avg_participants": round(statistics.mean(parts), 1) if parts else None,
        })
        count += 1

    return count


# нужен для rebuild_corridors_from_results
import json


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS — runtime-настройки, редактируемые через веб-интерфейс
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(key: str, default: str | None = None) -> str | None:
    """Возвращает значение из таблицы settings или default."""
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
        return row[0] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str, description: str = "") -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO settings (key, value, description)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET
                value      = EXCLUDED.value,
                description = COALESCE(NULLIF(EXCLUDED.description,''), settings.description),
                updated_at  = NOW()
            """,
            (key, str(value), description),
        )


def get_all_settings() -> dict[str, str]:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM settings ORDER BY key")
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        return {}


def upsert_settings_bulk(data: dict[str, str]) -> None:
    """Сохраняет несколько настроек за один вызов."""
    for key, value in data.items():
        set_setting(key, value)


def increment_llm_daily_requests(date_key: str) -> int:
    """Атомарно увеличивает счётчик LLM-запросов к OpenRouter за день и возвращает новое значение.

    Хранится в settings под ключом LLM_REQUESTS_<YYYY-MM-DD> — так дневной
    лимит (config.LLM_DAILY_REQUEST_LIMIT) переживает рестарты и общий для
    всех процессов (main.py, telegram_decisions, analytics), бьющих в одну БД.
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO settings (key, value, description)
            VALUES (%s, '1', 'Счётчик LLM-запросов OpenRouter за день (авто)')
            ON CONFLICT (key) DO UPDATE SET
                value      = (COALESCE(NULLIF(settings.value, '')::int, 0) + 1)::text,
                updated_at = NOW()
            RETURNING value
            """,
            (f"LLM_REQUESTS_{date_key}",),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 1


def get_llm_daily_requests(date_key: str) -> int:
    """Текущее значение счётчика LLM-запросов за день (без инкремента)."""
    val = get_setting(f"LLM_REQUESTS_{date_key}")
    try:
        return int(val) if val else 0
    except (TypeError, ValueError):
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# SCORING RULES — словарь фраз для 8 фильтров (редактируется в /rules)
# ══════════════════════════════════════════════════════════════════════════════

def _norm_phrase_list(value: Any) -> list[str]:
    """Приводит unless/require к списку строк (принимает list или строку через запятую/перенос)."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    parts: list[str] = []
    for chunk in str(value).replace(";", "\n").replace(",", "\n").splitlines():
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def scoring_rules_list() -> list[dict[str, Any]]:
    """Все правила, сгруппированно отсортированные для UI."""
    try:
        with _conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM scoring_rules ORDER BY dim, bucket, sort, id")
            return [_to_jsonable(dict(r)) for r in cur.fetchall()]
    except Exception:
        return []


def scoring_rules_active() -> list[dict[str, Any]]:
    """Активные правила для движка filter_engine (минимум полей)."""
    try:
        with _conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT dim, bucket, term, unless, require "
                "FROM scoring_rules WHERE active = TRUE"
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def scoring_rule_save(data: dict[str, Any]) -> int:
    clean = {
        "dim":     int(data.get("dim") or 1),
        "bucket":  str(data.get("bucket") or "").strip(),
        "term":    str(data.get("term") or "").strip(),
        "unless":  _norm_phrase_list(data.get("unless")),
        "require": _norm_phrase_list(data.get("require")),
        "active":  bool(data.get("active", True)),
        "note":    str(data.get("note") or "").strip() or None,
        "sort":    int(data.get("sort") or 100),
    }
    if not clean["bucket"] or not clean["term"]:
        raise ValueError("scoring_rule: требуются непустые bucket и term")

    row_id = data.get("id")
    with _conn() as conn:
        cur = conn.cursor()
        if row_id:
            clean["_id"] = int(row_id)
            cur.execute(
                """UPDATE scoring_rules SET
                       dim=%(dim)s, bucket=%(bucket)s, term=%(term)s,
                       unless=%(unless)s::text[], require=%(require)s::text[],
                       active=%(active)s, note=%(note)s, sort=%(sort)s
                   WHERE id=%(_id)s RETURNING id""",
                clean,
            )
        else:
            cur.execute(
                """INSERT INTO scoring_rules (dim, bucket, term, unless, require, active, note, sort)
                   VALUES (%(dim)s, %(bucket)s, %(term)s, %(unless)s::text[], %(require)s::text[],
                           %(active)s, %(note)s, %(sort)s)
                   RETURNING id""",
                clean,
            )
        return int(cur.fetchone()[0])


def scoring_rule_delete(row_id: int) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM scoring_rules WHERE id = %s", (int(row_id),))


def scoring_rules_seed(force: bool = False) -> int:
    """Засевает scoring_rules из filter_engine.DEFAULT_BUCKETS.

    Идемпотентно: если таблица непуста и force=False — ничего не делает.
    Вызывается из init_db.py (load_defaults) и из API /api/rules/seed.
    """
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM scoring_rules")
            count = cur.fetchone()[0]
    except Exception as exc:
        logger.warning("scoring_rules_seed: таблица недоступна: %s", exc)
        return 0
    if count and not force:
        logger.info("scoring_rules уже содержит %d записей, seed пропущен", count)
        return 0

    from filter_engine import DEFAULT_BUCKETS
    inserted = 0
    with _conn() as conn:
        cur = conn.cursor()
        for (dim, bucket), terms in DEFAULT_BUCKETS.items():
            for i, term in enumerate(terms, start=1):
                cur.execute(
                    "INSERT INTO scoring_rules (dim, bucket, term, sort) VALUES (%s, %s, %s, %s)",
                    (int(dim), bucket, term, i * 10),
                )
                inserted += 1
    logger.info("scoring_rules: засеяно %d правил из дефолтов", inserted)
    return inserted


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH PROFILES — поисковые профили Stage 1 (редактируются в /profiles)
# ══════════════════════════════════════════════════════════════════════════════

_PHRASE_FIELDS = ("kind", "phrase", "weight", "query_only", "local_only", "query_text", "enabled", "note")


def _profiles_with_phrases(where_sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"SELECT * FROM search_profiles {where_sql} ORDER BY priority, id", params,
        )
        profiles = [dict(r) for r in cur.fetchall()]
        if not profiles:
            return []
        ids = [p["id"] for p in profiles]
        cur.execute(
            "SELECT * FROM search_phrases WHERE profile_id = ANY(%s) ORDER BY id", (ids,),
        )
        phrases_by_profile: dict[int, list[dict[str, Any]]] = {}
        for r in cur.fetchall():
            phrases_by_profile.setdefault(r["profile_id"], []).append(dict(r))
    for p in profiles:
        p["phrases"] = [_to_jsonable(ph) for ph in phrases_by_profile.get(p["id"], [])]
    return [_to_jsonable(p) for p in profiles]


def search_profiles_list() -> list[dict[str, Any]]:
    """Все профили (вкл./выкл.) с их фразами — для страницы /profiles."""
    try:
        return _profiles_with_phrases("")
    except Exception:
        return []


def search_profiles_active() -> list[dict[str, Any]]:
    """Только включённые профили с включёнными фразами — для search_profiles.py."""
    try:
        profiles = _profiles_with_phrases("WHERE enabled = TRUE")
    except Exception:
        return []
    for p in profiles:
        p["phrases"] = [ph for ph in p["phrases"] if ph.get("enabled", True)]
    return profiles


def search_profile_get(profile_id: int) -> Optional[dict[str, Any]]:
    rows = _profiles_with_phrases("WHERE id = %s", (int(profile_id),))
    return rows[0] if rows else None


def _opt_int(value: Any) -> Optional[int]:
    """Пусто/None → None (наследовать глобальный дефолт), иначе int."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def search_profile_save(data: dict[str, Any]) -> int:
    """Создаёт/обновляет карточку профиля (без фраз — см. search_phrases_replace)."""
    clean = {
        "name":        str(data.get("name") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "enabled":     bool(data.get("enabled", True)),
        "priority":    int(data.get("priority") or 100),
        "min_score":   int(data.get("min_score") or 3),
        "okpd2_codes": [str(c).strip() for c in (data.get("okpd2_codes") or []) if str(c).strip()],
        "price_target_lo": _opt_int(data.get("price_target_lo")),
        "price_target_hi": _opt_int(data.get("price_target_hi")),
        "price_hard_max":  _opt_int(data.get("price_hard_max")),
    }
    if not clean["name"]:
        raise ValueError("search_profile: требуется непустое имя")

    row_id = data.get("id")
    with _conn() as conn:
        cur = conn.cursor()
        if row_id:
            clean["_id"] = int(row_id)
            cur.execute(
                """UPDATE search_profiles SET
                       name=%(name)s, description=%(description)s, enabled=%(enabled)s,
                       priority=%(priority)s, min_score=%(min_score)s,
                       okpd2_codes=%(okpd2_codes)s::text[],
                       price_target_lo=%(price_target_lo)s, price_target_hi=%(price_target_hi)s,
                       price_hard_max=%(price_hard_max)s, updated_at=NOW()
                   WHERE id=%(_id)s RETURNING id""",
                clean,
            )
        else:
            cur.execute(
                """INSERT INTO search_profiles
                       (name, description, enabled, priority, min_score, okpd2_codes,
                        price_target_lo, price_target_hi, price_hard_max)
                   VALUES (%(name)s, %(description)s, %(enabled)s, %(priority)s, %(min_score)s,
                           %(okpd2_codes)s::text[], %(price_target_lo)s, %(price_target_hi)s, %(price_hard_max)s)
                   RETURNING id""",
                clean,
            )
        return int(cur.fetchone()[0])


def search_profile_delete(profile_id: int) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM search_profiles WHERE enabled = TRUE")
        enabled_count = cur.fetchone()[0]
        cur.execute("SELECT enabled, is_default FROM search_profiles WHERE id = %s", (int(profile_id),))
        row = cur.fetchone()
        if not row:
            return
        is_enabled, is_default = row
        if is_default:
            raise ValueError("Нельзя удалить профиль по умолчанию — сначала назначьте другой")
        if is_enabled and enabled_count <= 1:
            raise ValueError("Нельзя удалить последний включённый профиль")
        cur.execute("DELETE FROM search_profiles WHERE id = %s", (int(profile_id),))


def search_profile_set_default(profile_id: int) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE search_profiles SET is_default = FALSE WHERE is_default = TRUE")
        cur.execute(
            "UPDATE search_profiles SET is_default = TRUE, enabled = TRUE, updated_at = NOW() WHERE id = %s",
            (int(profile_id),),
        )


def search_profile_copy(profile_id: int) -> int:
    """Копирует профиль со всеми фразами; копия — всегда выключена и не дефолтная."""
    src = search_profile_get(profile_id)
    if not src:
        raise ValueError("Профиль не найден")
    base_name = f"{src['name']} (копия)"
    name = base_name
    with _conn() as conn:
        cur = conn.cursor()
        n = 2
        while True:
            cur.execute("SELECT 1 FROM search_profiles WHERE name = %s", (name,))
            if not cur.fetchone():
                break
            name = f"{base_name} {n}"
            n += 1
        cur.execute(
            """INSERT INTO search_profiles
                   (name, description, enabled, is_default, priority, min_score, okpd2_codes,
                    price_target_lo, price_target_hi, price_hard_max)
               VALUES (%s, %s, FALSE, FALSE, %s, %s, %s::text[], %s, %s, %s) RETURNING id""",
            (name, src.get("description") or "", src.get("priority") or 100,
             src.get("min_score") or 3, src.get("okpd2_codes") or [],
             src.get("price_target_lo"), src.get("price_target_hi"), src.get("price_hard_max")),
        )
        new_id = int(cur.fetchone()[0])
        for ph in src.get("phrases") or []:
            cur.execute(
                """INSERT INTO search_phrases
                       (profile_id, kind, phrase, weight, query_only, local_only, query_text, enabled, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (new_id, ph.get("kind"), ph.get("phrase"), ph.get("weight"),
                 bool(ph.get("query_only")), bool(ph.get("local_only")),
                 ph.get("query_text"), bool(ph.get("enabled", True)), ph.get("note") or ""),
            )
    return new_id


def search_phrases_replace(profile_id: int, phrases: list[dict[str, Any]]) -> None:
    """Полностью заменяет список фраз профиля (delete+insert, транзакция)."""
    profile_id = int(profile_id)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM search_phrases WHERE profile_id = %s", (profile_id,))
        for ph in phrases or []:
            kind = str(ph.get("kind") or "plus").strip()
            phrase = str(ph.get("phrase") or "").strip()
            if kind not in ("plus", "minus_hard", "minus_soft") or not phrase:
                continue
            cur.execute(
                """INSERT INTO search_phrases
                       (profile_id, kind, phrase, weight, query_only, local_only, query_text, enabled, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (profile_id, kind, phrase, int(ph.get("weight") or 3),
                 bool(ph.get("query_only")), bool(ph.get("local_only")),
                 (str(ph.get("query_text")).strip() or None) if ph.get("query_text") else None,
                 bool(ph.get("enabled", True)), str(ph.get("note") or "")),
            )


def get_recent_tenders_for_preview(limit: int = 500) -> list[dict[str, Any]]:
    """Последние N карточек (title/primary_text/purchase_number) — для кнопки
    «Прогнать по базе» на странице /profiles. Без похода во внешние источники."""
    limit = max(1, min(2000, int(limit or 500)))
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT purchase_number, title, primary_text
                 FROM tenders
                ORDER BY updated_at DESC NULLS LAST
                LIMIT %s""",
            (limit,),
        )
        return [_to_jsonable(dict(r)) for r in cur.fetchall()]


def search_profiles_seed(force: bool = False) -> int:
    """Засевает search_profiles/search_phrases из search_profiles.DEFAULT_PROFILES.

    Идемпотентно: если таблица непуста и force=False — ничего не делает.
    Профиль «Импорт: текущий список» дополнительно наполняется текущим
    config.SEARCH_KEYWORDS (чтобы не терять охват при переходе на профили).
    Вызывается из init_db.py (load_defaults).
    """
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM search_profiles")
            count = cur.fetchone()[0]
    except Exception as exc:
        logger.warning("search_profiles_seed: таблица недоступна: %s", exc)
        return 0
    if count and not force:
        logger.info("search_profiles уже содержит %d записей, seed пропущен", count)
        return 0

    from search_profiles import DEFAULT_PROFILES
    inserted = 0
    with _conn() as conn:
        cur = conn.cursor()
        for prof in DEFAULT_PROFILES:
            cur.execute(
                """INSERT INTO search_profiles (name, description, enabled, is_default, priority, min_score, okpd2_codes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::text[]) RETURNING id""",
                (prof["name"], prof.get("description", ""), bool(prof.get("enabled", True)),
                 bool(prof.get("is_default", False)), int(prof.get("priority", 100)),
                 int(prof.get("min_score", 3)), list(prof.get("okpd2_codes", []))),
            )
            profile_id = int(cur.fetchone()[0])
            phrases = list(prof.get("phrases") or [])
            if not phrases and prof["name"] == "Импорт: текущий список":
                import config
                phrases = [
                    {"kind": "plus", "phrase": kw, "weight": 3, "query_text": kw}
                    for kw in getattr(config, "SEARCH_KEYWORDS", [])
                ]
            for ph in phrases:
                cur.execute(
                    """INSERT INTO search_phrases
                           (profile_id, kind, phrase, weight, query_only, local_only, query_text, note)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (profile_id, ph["kind"], ph["phrase"], int(ph.get("weight", 3)),
                     bool(ph.get("query_only", False)), bool(ph.get("local_only", False)),
                     ph.get("query_text"), ph.get("note", "")),
                )
                inserted += 1
    logger.info("search_profiles: засеяно %d профилей из дефолтов (%d фраз)", len(DEFAULT_PROFILES), inserted)
    return inserted


# ══════════════════════════════════════════════════════════════════════════════
# KNOWLEDGE BASE — CRUD для всех 5 разделов
# ══════════════════════════════════════════════════════════════════════════════

_KB_TABLES = {
    "kb_contracts",
    "kb_competencies",
    "kb_equipment",
    "kb_risk_rules",
    "kb_templates",
}


def _kb_table(table: str) -> str:
    if table not in _KB_TABLES:
        raise ValueError(f"Недопустимая таблица БЗ: {table}")
    return table


def _kb_list(
    table: str,
    where: str = "",
    params: tuple = (),
    order: str = "id DESC",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    table = _kb_table(table)
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" {where}"
    if order:
        sql += f" ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT %s"
        params = (*params, limit)
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [_to_jsonable(dict(r)) for r in cur.fetchall()]


def _kb_insert(table: str, data: dict[str, Any]) -> int:
    table = _kb_table(table)
    clean = {k: v for k, v in data.items() if k != "id"}
    cols = ", ".join(clean.keys())
    vals = ", ".join(f"%({k})s" for k in clean.keys())
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING id", clean)
        return int(cur.fetchone()[0])


def _kb_update(table: str, row_id: int, data: dict[str, Any]) -> None:
    table = _kb_table(table)
    clean = {k: v for k, v in data.items() if k != "id"}
    if not clean:
        return
    sets = ", ".join(f"{k} = %({k})s" for k in clean.keys())
    clean["_id"] = row_id
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE {table} SET {sets} WHERE id = %(_id)s", clean)


def _kb_delete(table: str, row_id: int) -> None:
    table = _kb_table(table)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))


def kb_contracts_list() -> list[dict[str, Any]]:
    return _kb_list("kb_contracts", order="id DESC")


def kb_contracts_by_category(category: str, limit: int = 5) -> list[dict[str, Any]]:
    if not category:
        return _kb_list("kb_contracts", order="id DESC", limit=limit)
    return _kb_list("kb_contracts", "WHERE category = %s", (category,), "id DESC", limit)


def kb_contract_save(data: dict[str, Any]) -> int:
    if isinstance(data.get("tech_stack"), list):
        data["tech_stack"] = json.dumps(data["tech_stack"], ensure_ascii=False)
    row_id = data.get("id")
    if row_id:
        _kb_update("kb_contracts", int(row_id), data)
        return int(row_id)
    return _kb_insert("kb_contracts", data)


def kb_contract_delete(row_id: int) -> None:
    _kb_delete("kb_contracts", row_id)


def kb_competencies_list() -> list[dict[str, Any]]:
    return _kb_list("kb_competencies", order="category NULLS LAST, level DESC, tech")


def kb_competencies_for_category(category: str) -> list[dict[str, Any]]:
    if not category:
        return kb_competencies_list()
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT * FROM kb_competencies
            WHERE category = %s OR category IS NULL OR category = ''
            ORDER BY CASE WHEN category = %s THEN 0 ELSE 1 END, level DESC, tech
            """,
            (category, category),
        )
        return [_to_jsonable(dict(r)) for r in cur.fetchall()]


def kb_competency_save(data: dict[str, Any]) -> int:
    data["certified"] = bool(data.get("certified", False))
    row_id = data.get("id")
    if row_id:
        _kb_update("kb_competencies", int(row_id), data)
        return int(row_id)
    return _kb_insert("kb_competencies", data)


def kb_competency_delete(row_id: int) -> None:
    _kb_delete("kb_competencies", row_id)


def kb_equipment_list() -> list[dict[str, Any]]:
    return _kb_list("kb_equipment", order="category NULLS LAST, name")


def kb_equipment_find_analogs(text: str) -> list[dict[str, Any]]:
    text_lower = (text or "").lower()
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM kb_equipment ORDER BY id DESC")
        rows = cur.fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        item = _to_jsonable(dict(row))
        analogs = item.get("analog_for") or []
        if isinstance(analogs, str):
            analogs = re.findall(r"[\w\-]+", analogs)
        probes = list(analogs)
        if item.get("vendor"):
            probes.append(str(item["vendor"]))
        if item.get("model"):
            probes.append(str(item["model"]))
        if any(str(probe).lower() in text_lower for probe in probes if probe):
            result.append(item)
    return result[:5]


def kb_equipment_save(data: dict[str, Any]) -> int:
    if isinstance(data.get("specs"), dict):
        data["specs"] = json.dumps(data["specs"], ensure_ascii=False)
    row_id = data.get("id")
    if row_id:
        _kb_update("kb_equipment", int(row_id), data)
        return int(row_id)
    return _kb_insert("kb_equipment", data)


def kb_equipment_delete(row_id: int) -> None:
    _kb_delete("kb_equipment", row_id)


def kb_risk_rules_list() -> list[dict[str, Any]]:
    return _kb_list("kb_risk_rules", order="rule_type, weight, id DESC")


def kb_risk_rules_active() -> list[dict[str, Any]]:
    return _kb_list("kb_risk_rules", "WHERE active = TRUE", order="weight, id DESC")


def kb_risk_rule_save(data: dict[str, Any]) -> int:
    data["active"] = bool(data.get("active", True))
    row_id = data.get("id")
    if row_id:
        _kb_update("kb_risk_rules", int(row_id), data)
        return int(row_id)
    return _kb_insert("kb_risk_rules", data)


def kb_risk_rule_delete(row_id: int) -> None:
    _kb_delete("kb_risk_rules", row_id)


def kb_templates_list() -> list[dict[str, Any]]:
    return _kb_list("kb_templates", order="category NULLS LAST, title")


def kb_template_save(data: dict[str, Any]) -> int:
    row_id = data.get("id")
    if row_id:
        _kb_update("kb_templates", int(row_id), data)
        return int(row_id)
    return _kb_insert("kb_templates", data)


def kb_template_delete(row_id: int) -> None:
    _kb_delete("kb_templates", row_id)


def kb_template_use(row_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "UPDATE kb_templates SET used_count = used_count + 1 WHERE id = %s RETURNING *",
            (row_id,),
        )
        row = cur.fetchone()
    return _to_jsonable(dict(row)) if row else None


def kb_stats() -> dict[str, int]:
    stats: dict[str, int] = {}
    try:
        with _conn() as conn:
            cur = conn.cursor()
            for table in _KB_TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                stats[table] = int(cur.fetchone()[0])
    except Exception:
        stats = {table: 0 for table in _KB_TABLES}
    return stats
