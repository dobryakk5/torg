"""scorer.py — правиловой скоринг закупки."""

from __future__ import annotations

import logging
from datetime import datetime

import config

logger = logging.getLogger(__name__)


def score_tender(tender: dict, document_text: str = "") -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    combined = " ".join(
        str(tender.get(key, "") or "")
        for key in ["title", "customer", "law_type", "region", "payment_terms"]
    ).lower()
    if document_text:
        combined += " " + document_text[:15000].lower()

    price = tender.get("price") or 0
    law_type = tender.get("law_type", "")

    for pattern, points, label in config.POSITIVE_SIGNALS:
        if pattern.lower() in combined:
            score += points
            reasons.append(f"✅ {label} (+{points})")

    for pattern, points, label in config.NEGATIVE_SIGNALS:
        if pattern.lower() in combined:
            score += points
            reasons.append(f"⚠️ {label} ({points})")

    if price and config.PRICE_MIN <= price <= config.PRICE_MAX:
        for p_min, p_max, bonus, label in config.PRICE_BONUS_RANGES:
            if p_min <= price <= p_max:
                score += bonus
                reasons.append(f"💰 {label} (+{bonus})")
                break
        else:
            score += 1
            reasons.append("💰 Цена в диапазоне (+1)")
    elif price and price < config.PRICE_MIN:
        score -= 2
        reasons.append(f"💸 Цена ниже порога ({price:,.0f} ₽, -2)".replace(",", " "))
    elif price and price > config.PRICE_MAX:
        score -= 1
        reasons.append(f"💸 Цена выше лимита ({price:,.0f} ₽, -1)".replace(",", " "))

    if law_type == "44-ФЗ":
        score += 1
        reasons.append("📋 44-ФЗ (+1)")

    app_sec = tender.get("application_security_amount") or 0
    contract_sec = tender.get("contract_security_amount") or 0
    if app_sec:
        if app_sec > config.MAX_APPLICATION_SECURITY:
            score -= 3
            reasons.append(f"💸 Обеспечение заявки высокое ({app_sec:,.0f} ₽, -3)".replace(",", " "))
        else:
            score += 1
            reasons.append(f"💰 Обеспечение заявки терпимое ({app_sec:,.0f} ₽, +1)".replace(",", " "))
    if contract_sec:
        if contract_sec > config.MAX_CONTRACT_SECURITY:
            score -= 4
            reasons.append(f"💸 Обеспечение исполнения высокое ({contract_sec:,.0f} ₽, -4)".replace(",", " "))
        else:
            score += 1
            reasons.append(f"💰 Обеспечение исполнения терпимое ({contract_sec:,.0f} ₽, +1)".replace(",", " "))

    deadline = tender.get("deadline", "")
    days_left = _days_until_deadline(deadline)
    if days_left is not None:
        if days_left < 2:
            score -= 5
            reasons.append("⚠️ До конца подачи меньше 2 дней (-5)")
        elif days_left < 5:
            score -= 2
            reasons.append("⚠️ Мало времени на заявку (-2)")
        else:
            score += 1
            reasons.append("✅ Есть время на заявку (+1)")

    logger.debug("Закупка '%s' → балл %d", tender.get("title", "?")[:60], score)
    return score, reasons


def _days_until_deadline(deadline: str) -> int | None:
    if not deadline:
        return None
    for fmt in ["%d.%m.%Y %H:%M", "%d.%m.%Y"]:
        try:
            dt = datetime.strptime(deadline[:16], fmt)
            return (dt - datetime.now()).days
        except ValueError:
            continue
    return None


def format_score_summary(score: int, reasons: list[str]) -> str:
    positives = [r for r in reasons if r.startswith(("✅", "💰", "📋"))]
    negatives = [r for r in reasons if r.startswith(("⚠️", "💸"))]
    lines = []
    if positives:
        lines.append("Плюсы: " + ", ".join(r.split(" ", 1)[1] for r in positives[:8]))
    if negatives:
        lines.append("Риски: " + ", ".join(r.split(" ", 1)[1] for r in negatives[:8]))
    return "\n".join(lines)
