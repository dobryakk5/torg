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

SCHEMA_VERSION = "v4"   # bump при добавлении новых таблиц


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
            DDL_INDEXES, DDL_TRIGGER,
        ):
            cur.execute(ddl)
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (SCHEMA_VERSION,),
        )
    logger.info("PostgreSQL схема создана: %s", SCHEMA_VERSION)


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
                 last_seen_at, primary_checked_at, updated_at)
            VALUES
                (%(pnum)s, %(title)s, %(customer)s, %(price)s, %(law_type)s, %(deadline)s, %(url)s,
                 %(platform)s, %(region)s, %(customer_inn)s, %(published_at)s, %(matched_keywords)s,
                 %(primary_text)s, %(primary_score)s, %(primary_reasons)s, %(total_score)s,
                 %(score)s, %(score_reasons)s, %(filter_total)s, %(filter_decision)s,
                 %(status)s, %(decision)s, %(content_hash)s, %(last_changed_at)s, %(needs_detail_refresh)s,
                 %(first_seen_at)s, %(last_seen_at)s, %(primary_checked_at)s, %(updated_at)s)
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
                updated_at = EXCLUDED.updated_at
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
             ORDER BY needs_detail_refresh DESC, primary_score DESC, price DESC NULLS LAST
             LIMIT %s
            """,
            (min_primary_score, limit),
        )
        rows = cur.fetchall()
    return _rows_to_dicts(rows)

def save_detail(
    tender: dict[str, Any],
    detail_score: int,
    detail_reasons: list[str],
    llm_analysis: str = "",
    document_text: str = "",
    notified: bool = False,
) -> None:
    _with_db_retries(
        "save_detail",
        lambda: _save_detail_once(tender, detail_score, detail_reasons, llm_analysis, document_text, notified),
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
    status = (
        "detail_candidate"
        if detail_score >= config.MIN_DETAILED_SCORE_FOR_NOTIFY and filter_decision_in_tender != "NO-GO"
        else "detail_rejected"
    )
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


def get_top_tenders(
    decision:  str | None = "GO",
    limit:     int        = 200,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    law_type:  Optional[str]   = None,
    # Per-filter минимальные оценки (1–5) для Ф1–Ф8
    f1_min: Optional[int] = None,
    f2_min: Optional[int] = None,
    f3_min: Optional[int] = None,
    f4_min: Optional[int] = None,
    f5_min: Optional[int] = None,
    f6_min: Optional[int] = None,
    f7_min: Optional[int] = None,
    f8_min: Optional[int] = None,
) -> list[dict[str, Any]]:
    """
    Возвращает тендеры с оценками всех 8 фильтров в одном SQL-запросе (без N+1).
    Поддерживает фильтрацию по минимальному баллу каждого фильтра отдельно.

    Пример:
        # Только GO-тендеры, где нет заточки (Ф6≥3) и нормальная экономика (Ф2≥3):
        db.get_top_tenders(decision="GO", f6_min=3, f2_min=3)
    """
    conditions: list[str] = []
    params:     list[Any] = []

    if decision:
        conditions.append("t.filter_decision = %s")
        params.append(decision)
    if price_min is not None:
        conditions.append("t.price >= %s")
        params.append(price_min)
    if price_max is not None:
        conditions.append("t.price <= %s")
        params.append(price_max)
    if law_type:
        conditions.append("t.law_type = %s")
        params.append(law_type)

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
    params.append(limit)

    sql = f"""
        SELECT
            t.purchase_number, t.title, t.customer, t.price, t.law_type,
            t.deadline, t.url, t.score, t.score_reasons, t.primary_score,
            t.detail_score, t.total_score, t.filter_total, t.filter_decision,
            t.filter_stop, t.llm_verdict, t.notified_at, t.decision,
            t.status, t.created_at, t.published_at,

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
            t.status, t.created_at, t.published_at
        {having}
        ORDER BY t.filter_total DESC NULLS LAST, t.created_at DESC
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
                COUNT(*) FILTER (WHERE filter_decision = 'GO')      AS go_count,
                COUNT(*) FILTER (WHERE filter_decision = 'CAUTION') AS caution_count,
                COUNT(*) FILTER (WHERE filter_decision = 'NO-GO')   AS nogo_count,
                COUNT(*) FILTER (WHERE filter_decision IS NULL)     AS unscored,
                ROUND(AVG(filter_total), 1)                         AS avg_score
            FROM tenders
        """)
        go_c, caution_c, nogo_c, unscored_c, avg_score = cur.fetchone()

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
    _with_db_retries("save_filter_result", lambda: _save_filter_result_once(filter_result, stage))


def _save_filter_result_once(filter_result: Any, stage: str = "stage1") -> None:
    """
    Сохраняет результаты фильтрации.

    stage="stage1" — Stage1 НЕ перезаписывает оценки фильтров, если Stage2 уже прошёл
                      и контент тендера не изменился (needs_detail_refresh = FALSE).
                      Это защищает детальные оценки от затирания поверхностными.

    stage="stage2" — всегда записывает (Stage2 главнее Stage1).
    """
    pnum = getattr(filter_result, "purchase_number", "")
    if not pnum:
        return

    stop_factors = getattr(filter_result, "stop_factors", []) or []

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
                        getattr(f, "name", ""),
                        getattr(f, "score", 0),
                        " | ".join(getattr(f, "signals", []) or []),
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
                        getattr(f, "name", ""),
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
