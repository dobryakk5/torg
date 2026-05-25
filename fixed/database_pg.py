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
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

logger = logging.getLogger(__name__)
_pool: Optional[ThreadedConnectionPool] = None


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
        f"connect_timeout=10"
    )


def _pool_limits() -> tuple[int, int]:
    return int(getattr(config, "PG_POOL_MIN", 1)), int(getattr(config, "PG_POOL_MAX", 5))


@contextmanager
def _conn() -> Iterator[psycopg2.extensions.connection]:
    global _pool
    if _pool is None:
        raise RuntimeError("PostgreSQL не инициализирован: сначала вызови init_db()")
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


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


def init_db() -> None:
    """Создаёт чистую PostgreSQL-схему, если таблиц ещё нет."""
    global _pool
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

    if _pool is None:
        pool_min, pool_max = _pool_limits()
        _pool = ThreadedConnectionPool(pool_min, pool_max, _build_dsn())
        logger.info("PostgreSQL pool создан: %d–%d", pool_min, pool_max)

    with _conn() as conn:
        cur = conn.cursor()
        for ddl in (DDL_TENDERS, DDL_FILTER_SCORES, DDL_RUNS, DDL_DECISIONS, DDL_TENDER_CHANGES, DDL_INDEXES, DDL_TRIGGER):
            cur.execute(ddl)
    logger.info("PostgreSQL БД инициализирована")


def close_db() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


def reset_db() -> None:
    """Полный сброс PostgreSQL-таблиц проекта. Использовать только осознанно."""
    global _pool
    if _pool is None:
        pool_min, pool_max = _pool_limits()
        _pool = ThreadedConnectionPool(pool_min, pool_max, _build_dsn())
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS filter_scores CASCADE")
        cur.execute("DROP TABLE IF EXISTS decisions CASCADE")
        cur.execute("DROP TABLE IF EXISTS tender_changes CASCADE")
        cur.execute("DROP TABLE IF EXISTS runs CASCADE")
        cur.execute("DROP TABLE IF EXISTS tenders CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS set_tenders_updated_at() CASCADE")
    init_db()


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
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO runs (mode, started_at, found, processed, notified, errors)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (mode, started_at, found, processed, notified, errors),
        )


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
    """Возвращает реальные filter_scores, если позже добавишь отдельный filter_engine."""
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT filter_number, filter_name, score, signals, stop_factor
            FROM filter_scores
            WHERE purchase_number = %s
            ORDER BY filter_number
            """,
            (purchase_number,),
        )
        rows = cur.fetchall()
    return _rows_to_dicts(rows)


def get_top_tenders(
    decision: str | None = "GO",
    limit: int = 20,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    law_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if decision:
        conditions.append("filter_decision = %s")
        params.append(decision)
    if price_min is not None:
        conditions.append("price >= %s")
        params.append(price_min)
    if price_max is not None:
        conditions.append("price <= %s")
        params.append(price_max)
    if law_type:
        conditions.append("law_type = %s")
        params.append(law_type)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)
    sql = f"""
        SELECT purchase_number, title, customer, price, law_type, deadline, url,
               score, score_reasons, primary_score, detail_score, total_score,
               filter_total, filter_decision, filter_stop, llm_verdict,
               notified_at, decision, status, created_at
        FROM tenders
        {where}
        ORDER BY filter_total DESC NULLS LAST, created_at DESC
        LIMIT %s
    """
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = _rows_to_dicts(cur.fetchall())

    for row in rows:
        row["filter_scores"] = get_filter_scores(row["purchase_number"])
    return rows


def get_stats_extended() -> dict[str, Any]:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tenders")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenders WHERE notified_at IS NOT NULL")
        sent = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenders WHERE decision = 'pending' AND notified_at IS NOT NULL")
        pending = cur.fetchone()[0]
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE filter_decision = 'GO')      AS go_count,
                COUNT(*) FILTER (WHERE filter_decision = 'CAUTION') AS caution_count,
                COUNT(*) FILTER (WHERE filter_decision = 'NO-GO')   AS nogo_count,
                COUNT(*) FILTER (WHERE filter_decision IS NULL)     AS unscored,
                ROUND(AVG(filter_total), 1)                         AS avg_score
            FROM tenders
            """
        )
        go_c, caution_c, nogo_c, unscored_c, avg_score = cur.fetchone()
        cur.execute(
            """
            SELECT customer, COUNT(*) AS cnt
            FROM tenders
            WHERE customer IS NOT NULL AND customer <> ''
            GROUP BY customer
            ORDER BY cnt DESC
            LIMIT 5
            """
        )
        top_customers = [{"customer": r[0], "count": int(r[1])} for r in cur.fetchall()]
        cur.execute("SELECT MIN(price), MAX(price), ROUND(AVG(price), 0) FROM tenders WHERE price > 0")
        pmin, pmax, pavg = cur.fetchone()

    return _to_jsonable({
        "total": total,
        "sent": sent,
        "pending": pending,
        "filter_go": go_c or 0,
        "filter_caution": caution_c or 0,
        "filter_nogo": nogo_c or 0,
        "filter_unscored": unscored_c or 0,
        "avg_filter_score": avg_score or 0,
        "top_customers": top_customers,
        "price_range": {"min": pmin, "max": pmax, "avg": pavg},
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
def save_filter_result(filter_result: Any) -> None:
    pnum = getattr(filter_result, "purchase_number", "")
    if not pnum:
        return
    stop_factors = getattr(filter_result, "stop_factors", []) or []
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders SET filter_total = %s, filter_decision = %s, filter_stop = %s
            WHERE purchase_number = %s
            """,
            (
                getattr(filter_result, "total_score", None),
                getattr(filter_result, "decision", None),
                " | ".join(stop_factors),
                pnum,
            ),
        )
        for f in getattr(filter_result, "filters", []) or []:
            cur.execute(
                """
                INSERT INTO filter_scores (purchase_number, filter_number, filter_name, score, signals, stop_factor)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (purchase_number, filter_number) DO UPDATE SET
                    filter_name = EXCLUDED.filter_name,
                    score = EXCLUDED.score,
                    signals = EXCLUDED.signals,
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
