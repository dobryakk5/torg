"""
customer_scorer.py — карточка репутации заказчика.

Источники данных (все публичные):
  1. Реестр контрактов ЕИС — история контрактов, расторжения, средний drop
  2. kad.arbitr.ru — количество арбитражных дел (ответчик по иску о взыскании долга)

Что строит:
  - Счётчик контрактов и расторжений
  - Детектор монопольного победителя
  - Флаг арбитражных споров
  - Итоговый reliability_score 1–5

Запуск отдельно:
    python customer_scorer.py --inn 1234567890
    python customer_scorer.py --all          # обрабатывает всех новых заказчиков из БД
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config
import database as db
from winner_analytics import scrape_customer_contracts, detect_monopoly

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer":         "https://kad.arbitr.ru/",
}

KAD_API = "https://kad.arbitr.ru/data/search"


# ══════════════════════════════════════════════════════════════════════════════
# Арбитражные дела (kad.arbitr.ru)
# ══════════════════════════════════════════════════════════════════════════════

def check_arbitration(inn: str) -> dict:
    """
    Проверяет kad.arbitr.ru — сколько раз организация выступала ответчиком.
    При ошибке возвращает пустой dict (не ломает пайплайн).
    """
    payload = {
        "Page": 1,
        "Count": 1,
        "Sides": [{"Name": "", "Inn": inn, "Type": "Defendant"}],
        "DateFrom": "2022-01-01T00:00:00",
        "DateTo": None,
        "CaseType": None,
        "CourtType": None,
        "IsActive": None,
    }
    try:
        resp = requests.post(
            KAD_API,
            json=payload,
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.debug("kad.arbitr.ru ответил %s для ИНН %s", resp.status_code, inn)
            return {}
        data = resp.json()
        total = (
            data.get("Result", {}).get("TotalCount")
            or data.get("TotalCount")
            or 0
        )
        last_date = None
        items = (
            data.get("Result", {}).get("Items")
            or data.get("Items")
            or []
        )
        if items:
            date_str = items[0].get("Date") or items[0].get("AcceptDate") or ""
            if date_str:
                m = re.search(r"\d{4}-\d{2}-\d{2}", date_str)
                if m:
                    last_date = m.group(0)

        logger.info("kad.arbitr: ИНН %s → %d дел ответчиком", inn, total)
        return {"arbitration_count": int(total), "last_arbitration_date": last_date}

    except Exception as e:
        logger.debug("kad.arbitr.ru недоступен для %s: %s", inn, e)
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# Контракты ЕИС — статистика
# ══════════════════════════════════════════════════════════════════════════════

_TERMINATION_MARKERS = [
    "расторгнут", "расторжение", "односторонний отказ",
    "прекращён", "недействительным", "нарушение условий",
]

_PAYMENT_DELAY_MARKERS = [
    "взыскание долга", "взыскание задолженности",
    "неоплата", "неосновательное обогащение",
]


def _count_terminations(contracts: list[dict]) -> int:
    count = 0
    for c in contracts:
        text = (c.get("title", "") + " " + c.get("status", "")).lower()
        if any(m in text for m in _TERMINATION_MARKERS):
            count += 1
    return count


def _calc_avg_drop(contracts: list[dict]) -> Optional[float]:
    drops = [
        c["drop_pct"]
        for c in contracts
        if c.get("drop_pct") is not None and 0 <= c["drop_pct"] <= 60
    ]
    if not drops:
        return None
    return round(sum(drops) / len(drops), 2)


# ══════════════════════════════════════════════════════════════════════════════
# Итоговый reliability_score
# ══════════════════════════════════════════════════════════════════════════════

def _calc_reliability(
    total: int,
    terminated: int,
    monopoly: bool,
    arbitration: int,
) -> int:
    """
    Оценка 1–5 (5 = надёжный заказчик):
      - Много расторжений → -2
      - Монополия → -1 (скорее всего задержит нового участника оформить)
      - Арбитраж > 5 дел → -2, арбитраж 1–5 → -1
    """
    score = 5

    if total > 0:
        term_rate = terminated / total
        if term_rate >= 0.15:
            score -= 2
        elif term_rate >= 0.05:
            score -= 1

    if monopoly:
        score -= 1

    if arbitration >= 5:
        score -= 2
    elif arbitration >= 1:
        score -= 1

    return max(1, min(5, score))


def _build_notes(
    total: int,
    terminated: int,
    monopoly_data: dict,
    arbitration: int,
) -> str:
    parts = []
    if terminated:
        parts.append(f"расторжений: {terminated}/{total}")
    if monopoly_data.get("monopoly_flag"):
        w = monopoly_data.get("repeat_winner_name", "?")[:40]
        s = monopoly_data.get("repeat_winner_share", 0)
        parts.append(f"монополия: {w} побеждает {s}% торгов")
    if arbitration >= 1:
        parts.append(f"арбитраж: {arbitration} дел (ответчик)")
    return "; ".join(parts) if parts else "нарушений не выявлено"


# ══════════════════════════════════════════════════════════════════════════════
# Главная функция
# ══════════════════════════════════════════════════════════════════════════════

def score_customer(inn: str, name: str = "") -> dict:
    """
    Строит профиль заказчика по ИНН.
    Возвращает dict, готовый к db.upsert_customer().
    """
    logger.info("Анализирую заказчика ИНН=%s (%s)", inn, name or "?")

    # 1. Контракты из ЕИС
    contracts  = scrape_customer_contracts(inn, pages=3)
    total      = len(contracts)
    terminated = _count_terminations(contracts)
    avg_drop   = _calc_avg_drop(contracts)
    monopoly   = detect_monopoly(contracts)

    time.sleep(1.5)   # пауза между ЕИС и kad.arbitr

    # 2. Арбитраж
    arb_data   = check_arbitration(inn)
    arb_count  = arb_data.get("arbitration_count", 0)
    arb_date   = arb_data.get("last_arbitration_date")

    # 3. Итоговый скор
    rel_score  = _calc_reliability(total, terminated, monopoly.get("monopoly_flag", False), arb_count)
    notes      = _build_notes(total, terminated, monopoly, arb_count)

    profile = {
        "inn":                    inn,
        "name":                   name or (contracts[0].get("customer_name") if contracts else ""),
        "total_contracts":        total,
        "terminated_contracts":   terminated,
        "avg_drop_pct":           avg_drop,
        "avg_participants":       None,
        "repeat_winner_inn":      monopoly.get("repeat_winner_inn"),
        "repeat_winner_name":     monopoly.get("repeat_winner_name"),
        "repeat_winner_share":    monopoly.get("repeat_winner_share"),
        "monopoly_flag":          monopoly.get("monopoly_flag", False),
        "arbitration_count":      arb_count,
        "last_arbitration_date":  arb_date,
        "reliability_score":      rel_score,
        "notes":                  notes,
        "raw_json":               {
            "contracts_sample": contracts[:5],
            "monopoly":         monopoly,
            "arbitration":      arb_data,
        },
    }

    db.upsert_customer(profile)
    logger.info(
        "Профиль заказчика %s: score=%d, contracts=%d, monopoly=%s, arbitration=%d, notes: %s",
        inn, rel_score, total, monopoly.get("monopoly_flag"), arb_count, notes,
    )
    return profile


# ══════════════════════════════════════════════════════════════════════════════
# Пакетная обработка
# ══════════════════════════════════════════════════════════════════════════════

def run_new_customers(limit: int = 30) -> int:
    """Обрабатывает заказчиков из наших тендеров, у которых ещё нет профиля."""
    inns = db.get_customer_inns_to_score(limit)
    logger.info("Новых заказчиков для скоринга: %d", len(inns))
    for inn in inns:
        try:
            score_customer(inn)
        except Exception as e:
            logger.error("Ошибка при скоринге ИНН=%s: %s", inn, e)
        time.sleep(2)
    return len(inns)


def run_refresh_customers(limit: int = 20, days: int = 7) -> int:
    """Обновляет устаревшие профили заказчиков (старше N дней)."""
    inns = db.get_stale_customer_inns(days=days, limit=limit)
    logger.info("Устаревших профилей для обновления: %d", len(inns))
    for inn in inns:
        try:
            existing = db.get_customer(inn)
            name = existing.get("name", "") if existing else ""
            score_customer(inn, name)
        except Exception as e:
            logger.error("Ошибка при обновлении ИНН=%s: %s", inn, e)
        time.sleep(2)
    return len(inns)


def get_customer_risk_label(inn: str) -> str:
    """
    Быстрая метка риска для встраивания в Telegram-уведомление.
    Например: '⚠️ риск: монополия (ООО Вебстар 80%) · арбитраж x3'
    """
    if not inn:
        return ""
    c = db.get_customer(inn)
    if not c:
        return ""
    parts = []
    score = c.get("reliability_score", 3)
    if score <= 2:
        if c.get("monopoly_flag"):
            w = (c.get("repeat_winner_name") or "")[:25]
            s = c.get("repeat_winner_share", 0)
            parts.append(f"монополия: {w} {s}%")
        if c.get("arbitration_count", 0) >= 1:
            parts.append(f"арбитраж x{c['arbitration_count']}")
        term = c.get("terminated_contracts", 0)
        if term:
            parts.append(f"расторжений: {term}")
    if not parts:
        return ""
    emoji = "🔴" if score == 1 else "⚠️"
    return f"{emoji} заказчик: {'; '.join(parts)}"


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()

    ap = argparse.ArgumentParser()
    ap.add_argument("--inn",  help="Проанализировать конкретного заказчика")
    ap.add_argument("--name", default="", help="Название (необязательно)")
    ap.add_argument("--all",  action="store_true", help="Обработать всех новых из БД")
    ap.add_argument("--refresh", action="store_true", help="Обновить устаревшие профили")
    args = ap.parse_args()

    if args.inn:
        profile = score_customer(args.inn, args.name)
        print(f"\nРезультат для ИНН {args.inn}:")
        for k, v in profile.items():
            if k != "raw_json":
                print(f"  {k:30s}: {v}")
    elif args.all:
        n = run_new_customers()
        print(f"Обработано новых заказчиков: {n}")
    elif args.refresh:
        n = run_refresh_customers()
        print(f"Обновлено профилей: {n}")
    else:
        ap.print_help()
