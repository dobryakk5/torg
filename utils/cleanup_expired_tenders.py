"""
cleanup_expired_tenders.py — очистка тяжёлых данных по тендерам с истекшим сроком.

По умолчанию работает в dry-run: только показывает, что будет очищено.

Примеры:
    python cleanup_expired_tenders.py
    python cleanup_expired_tenders.py --apply
    python cleanup_expired_tenders.py --apply --grace-days 7
    python cleanup_expired_tenders.py --apply --delete-rows
"""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg2.extras

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cleanup_expired")

ALLOWED_DOCUMENT_ROOTS = (
    config.DOCUMENTS_DIR,
    config.BASE_DIR / "tender_docs",
)


def _format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _safe_document_dir(path_value: str | None, purchase_number: str) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = config.BASE_DIR / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None

    safe_name = purchase_number.strip()
    if not safe_name or resolved.name != safe_name:
        return None

    for root in ALLOWED_DOCUMENT_ROOTS:
        root_resolved = root.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    return None


def _fetch_candidates(delete_rows: bool, limit: int | None, cutoff: datetime) -> list[dict[str, Any]]:
    doc_condition = """
        (
            COALESCE(document_count, 0) > 0
            OR COALESCE(documents_dir, '') <> ''
            OR COALESCE(documents_hash, '') <> ''
            OR COALESCE(document_text_excerpt, '') <> ''
        )
    """
    sql = """
        SELECT purchase_number, title, deadline, documents_dir, document_count,
               documents_hash, document_text_excerpt, created_at, updated_at
          FROM tenders
         WHERE deadline IS NOT NULL
           AND btrim(deadline) <> ''
    """
    if not delete_rows:
        sql += f" AND {doc_condition}"
    sql += " ORDER BY created_at ASC"

    with db._conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]

    expired: list[dict[str, Any]] = []
    for row in rows:
        deadline = db.parse_deadline(row.get("deadline"))
        if deadline and deadline < cutoff:
            expired.append(row)
            if limit and len(expired) >= limit:
                break
    return expired


def _fetch_known_purchase_numbers() -> set[str]:
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT purchase_number FROM tenders")
        return {str(row[0]) for row in cur.fetchall() if row and row[0]}


def _find_orphan_document_dirs(known_purchase_numbers: set[str]) -> list[Path]:
    root = config.DOCUMENTS_DIR
    if not root.exists():
        return []
    orphans: list[Path] = []
    for child in root.iterdir():
        if child.is_dir() and child.name not in known_purchase_numbers:
            orphans.append(child)
    return sorted(orphans)


def _clear_document_columns(purchase_numbers: list[str]) -> None:
    if not purchase_numbers:
        return
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders
               SET document_count = 0,
                   documents_dir = '',
                   documents_hash = '',
                   document_text_excerpt = NULL
             WHERE purchase_number = ANY(%s)
            """,
            (purchase_numbers,),
        )


def _delete_tender_rows(purchase_numbers: list[str]) -> None:
    if not purchase_numbers:
        return
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tender_items WHERE purchase_number = ANY(%s)", (purchase_numbers,))
        cur.execute("DELETE FROM tenders WHERE purchase_number = ANY(%s)", (purchase_numbers,))


def main() -> None:
    parser = argparse.ArgumentParser(description="Очистка документов/тяжёлых данных по истекшим тендерам")
    parser.add_argument("--apply", action="store_true", help="Выполнить очистку. Без флага только dry-run")
    parser.add_argument("--delete-rows", action="store_true", help="Удалить строки тендеров из БД, а не только документы")
    parser.add_argument("--include-orphans", action="store_true", help="Также удалить папки data/documents/* без строки в tenders")
    parser.add_argument("--grace-days", type=int, default=0, help="Не трогать тендеры, истекшие меньше N дней назад")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число тендеров за прогон")
    args = parser.parse_args()

    db.connect_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, args.grace_days))
    rows = _fetch_candidates(args.delete_rows, args.limit, cutoff)

    dirs: list[Path] = []
    unsafe_dirs: list[tuple[str, str]] = []
    total_size = 0
    for row in rows:
        pnum = row.get("purchase_number") or ""
        doc_dir = _safe_document_dir(row.get("documents_dir"), pnum)
        if doc_dir:
            dirs.append(doc_dir)
            total_size += _dir_size(doc_dir)
        elif row.get("documents_dir"):
            unsafe_dirs.append((pnum, str(row.get("documents_dir"))))

    orphan_dirs: list[Path] = []
    orphan_size = 0
    if args.include_orphans:
        orphan_dirs = _find_orphan_document_dirs(_fetch_known_purchase_numbers())
        orphan_size = sum(_dir_size(path) for path in orphan_dirs)
        total_size += orphan_size

    mode = "DELETE ROWS" if args.delete_rows else "documents only"
    action = "apply" if args.apply else "dry-run"
    logger.info(
        "%s: %d expired tenders, %d safe document dirs, %s on disk, mode=%s",
        action,
        len(rows),
        len(dirs),
        _format_size(total_size),
        mode,
    )
    if args.include_orphans:
        logger.info("orphan dirs: %d, %s", len(orphan_dirs), _format_size(orphan_size))

    for row in rows[:20]:
        logger.info(
            "  %s deadline=%s docs=%s dir=%s title=%s",
            row.get("purchase_number"),
            row.get("deadline"),
            row.get("document_count") or 0,
            row.get("documents_dir") or "—",
            str(row.get("title") or "")[:90],
        )
    if len(rows) > 20:
        logger.info("  ... and %d more", len(rows) - 20)
    for pnum, path in unsafe_dirs[:10]:
        logger.warning("Unsafe documents_dir skipped for %s: %s", pnum, path)
    for path in orphan_dirs[:20]:
        logger.info("  orphan dir: %s", path)
    if len(orphan_dirs) > 20:
        logger.info("  ... and %d more orphan dirs", len(orphan_dirs) - 20)

    if not args.apply:
        logger.info("Dry-run only. Re-run with --apply to delete files/update DB.")
        db.close_db()
        return

    for path in dirs + orphan_dirs:
        if path.exists():
            shutil.rmtree(path)
    purchase_numbers = [row["purchase_number"] for row in rows]
    if args.delete_rows:
        _delete_tender_rows(purchase_numbers)
    else:
        _clear_document_columns(purchase_numbers)
    logger.info("Cleanup complete: %d tenders processed", len(purchase_numbers))
    db.close_db()


if __name__ == "__main__":
    main()
