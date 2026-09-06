#!/usr/bin/env python3
"""Очистка тяжёлых данных по завершённым тендерам.

Что делает для тендеров, у которых дедлайн подачи заявок **уже прошёл**:
  • удаляет с диска каталог со скачанными ТЗ (``data/documents/<номер>/``);
  • обнуляет объёмную колонку ``tenders.document_text_full`` в БД;
  • сбрасывает ``document_count`` / ``documents_hash`` / ``documents_dir``.

Что СОХРАНЯЕТСЯ:
  • ``tenders.document_text_excerpt`` — краткая выжимка (≤4000 симв.):
    описание лота и ключевые условия ТЗ;
  • сама строка тендера, скоринг, решения, аналитика — всё нетронуто.

Активные тендеры (дедлайн в будущем, пустой или неразборчивый) не трогаются:
их ТЗ ещё нужно для разбора.

По умолчанию — сухой прогон (только отчёт). Для реального удаления: ``--apply``.

Примеры:
    python purge_old_documents.py                 # что будет удалено
    python purge_old_documents.py --apply         # выполнить
    python purge_old_documents.py --older-than-days 30 --apply
    python purge_old_documents.py --disk-only --apply
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import config
import database as db
from database import _active_or_unknown_deadline_sql, _conn, _deadline_timestamp_sql
from document_processor import safe_filename


def _human(num: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} ПБ"


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _candidate_dirs(purchase_number: str, stored_dir: str | None) -> list[Path]:
    """Каталоги на диске, которые относятся к этому тендеру.

    Берём канонический ``DOCUMENTS_DIR/<safe(номер)>`` и, если в БД записан
    другой путь, — тоже, но лишь когда он лежит внутри DOCUMENTS_DIR
    (защита от случайного ``rmtree`` по произвольному пути с сервера).
    """
    docs_root = config.DOCUMENTS_DIR.resolve()
    out: list[Path] = []

    canonical = (config.DOCUMENTS_DIR / safe_filename(purchase_number)).resolve()
    if canonical.is_dir():
        out.append(canonical)

    if stored_dir:
        try:
            sd = Path(stored_dir).resolve()
        except (OSError, RuntimeError):
            sd = None
        if sd and sd != canonical and sd.is_dir():
            try:
                sd.relative_to(docs_root)
            except ValueError:
                pass  # путь вне DOCUMENTS_DIR — не трогаем
            else:
                out.append(sd)
    return out


def _fetch_expired(older_than_days: int, limit: int | None) -> list[dict]:
    expired_sql = f"NOT {_active_or_unknown_deadline_sql('t.deadline')}"
    age_sql = ""
    if older_than_days > 0:
        age_sql = (
            f"AND ({_deadline_timestamp_sql('t.deadline')}) "
            f"< NOW() - INTERVAL '{int(older_than_days)} days'"
        )
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
        SELECT t.purchase_number,
               t.deadline,
               t.documents_dir,
               t.document_count,
               COALESCE(length(t.document_text_full), 0)    AS full_len,
               COALESCE(length(t.document_text_excerpt), 0)  AS excerpt_len
        FROM tenders t
        WHERE {expired_sql}
          {age_sql}
          AND (
                COALESCE(t.document_text_full, '') <> ''
             OR COALESCE(t.document_count, 0) > 0
             OR COALESCE(t.documents_dir, '') <> ''
          )
        ORDER BY t.purchase_number
        {limit_sql}
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _clear_db_rows(purchase_numbers: list[str], disk_only: bool) -> int:
    if disk_only or not purchase_numbers:
        return 0
    sql = """
        UPDATE tenders
           SET document_text_full = NULL,
               document_count     = 0,
               documents_hash     = NULL,
               documents_dir      = NULL
         WHERE purchase_number = ANY(%s)
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, (purchase_numbers,))
        return cur.rowcount


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="выполнить удаление (без флага — только отчёт)")
    ap.add_argument("--older-than-days", type=int, default=0, metavar="N",
                    help="чистить только тендеры, чей дедлайн прошёл более N дней назад (по умолчанию 0 — любой прошедший)")
    ap.add_argument("--limit", type=int, default=None, metavar="N", help="ограничить число тендеров за прогон")
    ap.add_argument("--disk-only", action="store_true", help="только диск, БД не трогать")
    ap.add_argument("--db-only", action="store_true", help="только БД, файлы на диске не трогать")
    args = ap.parse_args(argv)

    if args.disk_only and args.db_only:
        ap.error("--disk-only и --db-only взаимоисключающие")

    db.connect_db()
    db.check_db()

    rows = _fetch_expired(args.older_than_days, args.limit)
    if not rows:
        print("Нечего чистить: завершённых тендеров с тяжёлыми данными не найдено.")
        return 0

    total_disk = 0
    total_full_chars = 0
    dirs_found = 0
    per_tender: list[tuple[str, int, list[Path]]] = []

    for r in rows:
        total_full_chars += r["full_len"] or 0
        dirs = [] if args.db_only else _candidate_dirs(r["purchase_number"], r["documents_dir"])
        size = sum(_dir_size(d) for d in dirs)
        total_disk += size
        dirs_found += len(dirs)
        per_tender.append((r["purchase_number"], size, dirs))

    mode = "УДАЛЕНИЕ" if args.apply else "СУХОЙ ПРОГОН (ничего не меняется)"
    print(f"=== {mode} ===")
    print(f"Тендеров с завершённым дедлайном под очистку: {len(rows)}")
    if not args.disk_only:
        print(f"  document_text_full к обнулению: ~{_human(total_full_chars)} текста "
              f"(excerpt сохраняется)")
    if not args.db_only:
        print(f"  каталогов с ТЗ на диске: {dirs_found}, освободится ~{_human(total_disk)}")

    sample = [p for p in per_tender if p[1] > 0][:10]
    if sample:
        print("\nПримеры (номер — размер на диске):")
        for pnum, size, dirs in sample:
            print(f"  {pnum}  {_human(size)}  [{', '.join(str(d) for d in dirs)}]")

    if not args.apply:
        print("\nЭто был сухой прогон. Для реального удаления добавь --apply")
        return 0

    # --- выполнение ---
    removed_dirs = 0
    freed = 0
    if not args.db_only:
        for pnum, size, dirs in per_tender:
            for d in dirs:
                try:
                    shutil.rmtree(d)
                    removed_dirs += 1
                    freed += size
                except OSError as exc:
                    print(f"  ! не удалось удалить {d}: {exc}", file=sys.stderr)

    updated = _clear_db_rows([r["purchase_number"] for r in rows], args.disk_only)

    print(f"\nГотово. Удалено каталогов: {removed_dirs} (~{_human(freed)}). "
          f"Строк БД обновлено: {updated}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
