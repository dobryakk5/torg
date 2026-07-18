"""
rescore.py — пересчёт 8-фильтрового скоринга по уже собранным деталям.

Зачем: на Stage 1 профиль (Ф1) считается по тексту карточки, где сильные ключи
(«1С Битрикс» и т.п.) часто отсутствуют — они лежат в объекте закупки / ТЗ.
Для лотов, у которых уже собран details_json, домешиваем текст объекта/характеристик
и пересчитываем фильтры: профиль поднимается, часть CAUTION законно становится GO.

Пишет результат принудительно (db.overwrite_filter_result) — перетирает Ф1..Ф8 и решение.
Идемпотентно: повторный прогон даёт тот же результат.

Использование:
    python rescore.py                 # все лоты с details_json
    python rescore.py --law 44-ФЗ     # только 44-ФЗ
    python rescore.py --dry-run       # показать переходы, НЕ писать в БД
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db
from filter_engine import run_stage1_filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("rescore")


def details_text(d: dict) -> str:
    """Текст из собранных деталей, который усиливает профиль (Ф1) и перекуп (Ф7)."""
    obj = d.get("object") or {}
    parts = [obj.get("raw", "")]
    parts += [p.get("name", "") for p in (obj.get("positions") or [])]
    parts.append((d.get("execution") or {}).get("raw", ""))
    parts.append(" ".join(d.get("requirements") or []))
    return "\n".join(x for x in parts if x)


def main() -> None:
    ap = argparse.ArgumentParser(description="Пересчёт скоринга по собранным деталям")
    ap.add_argument("--law", default=None, help="Фильтр по закону (например 44-ФЗ)")
    ap.add_argument("--dry-run", action="store_true", help="Не писать в БД, только показать переходы")
    args = ap.parse_args()

    db.connect_db()
    db.check_db()

    import psycopg2.extras
    sql = "SELECT * FROM tenders WHERE details_json IS NOT NULL"
    params: list = []
    if args.law:
        sql += " AND law_type = %s"
        params.append(args.law)
    with db._conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

    logger.info("Лотов с деталями: %d%s%s", len(rows),
                f" (закон {args.law})" if args.law else "", " [dry-run]" if args.dry_run else "")

    before = collections.Counter()
    after  = collections.Counter()
    trans  = collections.Counter()
    flips_to_go: list[str] = []

    for t in rows:
        dj = t.get("details_json")
        d = dj if isinstance(dj, dict) else (json.loads(dj) if dj else {})
        old = t.get("filter_decision")
        enriched = "\n".join([t.get("primary_text") or "", details_text(d)])
        fr = run_stage1_filters(t, enriched)
        before[old] += 1
        after[fr.decision] += 1
        if old != fr.decision:
            trans[f"{old}→{fr.decision}"] += 1
            if fr.decision == "GO":
                flips_to_go.append(t["purchase_number"])
        if not args.dry_run:
            db.overwrite_filter_result(fr)

    logger.info("Было : %s", dict(before))
    logger.info("Стало: %s", dict(after))
    logger.info("Переходы: %s", dict(trans) or "нет")
    if flips_to_go:
        logger.info("Стали GO (%d): %s", len(flips_to_go), ", ".join(flips_to_go[:20]))
    logger.info("Готово%s.", " (изменения НЕ сохранены — dry-run)" if args.dry_run else " — результаты записаны в БД")
    db.close_db()


if __name__ == "__main__":
    main()
