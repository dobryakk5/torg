"""
pipeline.py — ОДНА команда на весь конвейер: лоты → документы → пересчёт скоринга.

Запускать нужно только этот скрипт. Внутри по очереди:
    1) run_search.py   — поиск лотов + детали (common-info), кроме NO-GO детали не тянем;
    2) fetch_docs.py   — скачивание документов и разбор требований (всё, кроме NO-GO);
    3) rescore.py      — пересчёт 8-фильтрового скоринга по собранным деталям/документам
                         (поднимает профиль, часть CAUTION законно становится GO).

Два режима:

  • Ежедневный (вперёд, по умолчанию) — ищем свежие лоты и сразу обогащаем:
        python pipeline.py
        python pipeline.py --days 3        # на случай пропусков — захватить 3 последних дня
    (берёт сегодня + вчера, дальше дедуп по файлу прогресса и по «уже в БД»)

  • Обогащение прошлого (без нового поиска) — для уже собранных периодов:
        python pipeline.py --enrich-only
    (пропускает поиск, только качает документы и пересчитывает скоринг)

Прочие флаги пробрасываются в шаги:
    --price-max 1000000  --pages 2  --law 44-ФЗ  --refresh-details  --refresh-docs
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [PIPELINE] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("pipeline")

HERE = Path(__file__).resolve().parent


def step(title: str, script: str, script_args: list[str]) -> int:
    logger.info("═" * 64)
    logger.info("▶ %s  (%s %s)", title, script, " ".join(script_args))
    t0 = time.time()
    rc = subprocess.run([sys.executable, str(HERE / script), *script_args]).returncode
    logger.info("◀ %s завершён за %.0f c (код %d)", title, time.time() - t0, rc)
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description="Единый конвейер: лоты → документы → пересчёт")
    ap.add_argument("--days", type=int, default=2, help="Сколько последних дней искать (вперёд-режим). По умолчанию 2 (сегодня+вчера)")
    ap.add_argument("--price-max", type=int, default=1_000_000, help="Верхняя граница цены, ₽")
    ap.add_argument("--price-min", type=int, default=0, help="Нижняя граница цены, ₽")
    ap.add_argument("--pages", type=int, default=None, help="Страниц выдачи на фразу")
    ap.add_argument("--law", default="44-ФЗ", help="Закон для шага документов (223-ФЗ за логином — даст 0 файлов). Пусто = все")
    ap.add_argument("--enrich-only", action="store_true", help="Не искать новые лоты — только документы + пересчёт по уже собранным")
    ap.add_argument("--refresh-details", action="store_true", help="Пересобрать детали common-info, даже если есть")
    ap.add_argument("--refresh-docs", action="store_true", help="Перекачать документы, даже если уже есть")
    ap.add_argument("--no-docs", action="store_true", help="Пропустить шаг документов")
    ap.add_argument("--no-rescore", action="store_true", help="Пропустить шаг пересчёта")
    args = ap.parse_args()

    logger.info("Старт конвейера. Режим: %s", "обогащение прошлого" if args.enrich_only else "ежедневный (вперёд)")

    # ── Шаг 1: поиск лотов (только в ежедневном режиме) ─────────────────────
    if not args.enrich_only:
        search_args = [
            "--days", str(args.days), "--include-today",
            "--price-min", str(args.price_min), "--price-max", str(args.price_max),
        ]
        if args.pages is not None:
            search_args += ["--pages", str(args.pages)]
        if args.refresh_details:
            search_args += ["--refresh-details"]
        step("Шаг 1 · Поиск лотов + детали", "run_search.py", search_args)
    else:
        logger.info("Шаг 1 · Поиск пропущен (--enrich-only)")

    # ── Шаг 2: документы + требования (кроме NO-GO) ─────────────────────────
    if not args.no_docs:
        docs_args: list[str] = []
        if args.law:
            docs_args += ["--law", args.law]
        if args.refresh_docs:
            docs_args += ["--refresh"]
        step("Шаг 2 · Документы + требования", "fetch_docs.py", docs_args)
    else:
        logger.info("Шаг 2 · Документы пропущены (--no-docs)")

    # ── Шаг 3: пересчёт скоринга по деталям/документам ──────────────────────
    if not args.no_rescore:
        rescore_args = ["--law", args.law] if args.law else []
        step("Шаг 3 · Пересчёт скоринга", "rescore.py", rescore_args)
    else:
        logger.info("Шаг 3 · Пересчёт пропущен (--no-rescore)")

    logger.info("═" * 64)
    logger.info("Конвейер завершён.")


if __name__ == "__main__":
    main()
