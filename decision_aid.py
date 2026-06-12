"""
decision_aid.py — «Помощник решения для новичка».

Синтезатор поверх существующих сигналов (8 фильтров, БЗ, ценовые коридоры) и
детекторов нацрежима/удалёнки. Превращает разрозненные технические данные в одну
понятную карточку «БЕРИ / ПОДУМАЙ / ПРОПУСТИ» для новичка в госзакупках.

ВАЖНО (принципы MVP 1):
  - Никаких LLM-вызовов. Все числа, гейты, деньги и допуски считает обычный код.
  - `None` означает «не нашли / не определили» (UI: «не найдено — проверь вручную»),
    а `0` — реальный ноль.
  - Неизвестность НЕ превращается в блокировку: статус гейта `unknown`, а не `ok`/`block`.

Контракт результата `build_card()` см. в плане /rules: verdict/gates/can_do/finance/...
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)

VERDICT_LABELS = {"TAKE": "БЕРИ", "THINK": "ПОДУМАЙ", "SKIP": "ПРОПУСТИ"}

# Статусы тендера, означающие, что участвовать уже нельзя.
_CLOSED_STATUSES = {
    "cancelled", "canceled", "completed", "finished",
    "result_published", "archived",
}

GLOSSARY = {
    "НМЦК": "Начальная (максимальная) цена контракта — потолок, ниже которого вы предлагаете свою цену.",
    "обеспечение заявки": "Сумма, которую временно замораживают на спецсчёте на время торгов. Возвращается после.",
    "обеспечение исполнения": "Залог/банковская гарантия на время исполнения контракта. Деньги выводятся из оборота.",
    "аванс": "Предоплата от заказчика. Чем больше аванс, тем меньше кассовый разрыв.",
    "кассовый разрыв": "Сколько своих денег нужно вложить в работу до того, как заказчик заплатит.",
    "национальный режим": "Ограничения/преимущества для иностранных товаров и ПО (ст.14 44-ФЗ, ПП №1875).",
    "реестр российского ПО": "Реестр отечественного ПО (ПП №1236). Иногда требуется реестровая запись на ваш продукт.",
    "ст. 31": "Требования к участнику закупки (в т.ч. опыт исполнения аналогичных контрактов).",
}


# ══════════════════════════════════════════════════════════════════════════════
# Главная функция
# ══════════════════════════════════════════════════════════════════════════════

def build_card(tender: dict[str, Any], filter_result: Any = None,
               text: Optional[str] = None) -> dict[str, Any]:
    """Собирает детерминированную карточку решения по тендеру.

    Args:
        tender:        запись тендера (dict, как из database.get_tender).
        filter_result: готовый FilterResult (если уже посчитан); иначе считаем сами.
        text:          текст ТЗ/документации; иначе берём из tender по фикс. порядку.
    """
    logger.info("decision_aid_build_started: %s", tender.get("purchase_number", "?"))

    if text is None:
        text = (
            tender.get("document_text")
            or tender.get("document_text_excerpt")
            or tender.get("primary_text")
            or tender.get("description")
            or tender.get("title")
            or ""
        )
    has_text = bool(text and text.strip())
    if not has_text:
        logger.info("decision_aid_missing_text: %s", tender.get("purchase_number", "?"))

    # Категория и сигналы нацрежима/удалёнки
    try:
        from winner_analytics import classify_category
        category = classify_category(tender.get("title", ""))
    except Exception:
        category = "прочее"

    try:
        import filter_engine as fe
        flags = fe.detect_flags(text)
    except Exception:
        logger.exception("detect_flags failed")
        flags = {}

    # 8 фильтров (если не передали готовые)
    if filter_result is None:
        try:
            import filter_engine as fe
            stage = "stage2" if has_text else "stage1"
            filter_result = fe.run_filters(tender, text=text, stage=stage)
        except Exception:
            logger.exception("run_filters failed")
            filter_result = None

    by_num = {}
    if filter_result is not None:
        by_num = {f.number: f for f in getattr(filter_result, "filters", [])}

    # ── Составные части карточки ──────────────────────────────────────────────
    gates = _build_gates(tender, text, has_text, flags, category, by_num)
    can_do = _build_can_do(text, has_text)
    finance = estimate_economics(tender, category)
    green_flags, red_flags = _build_flags(flags, by_num, tender)

    # ── Вердикт ───────────────────────────────────────────────────────────────
    hard_blocks = [g for g in gates if g["status"] == "block"]
    warn_gates = [g for g in gates if g["status"] == "warn"]
    warnings = bool(warn_gates) or bool(red_flags)
    # Грубость оценки (quality=="weak") НЕ понижает вердикт сама по себе — она влияет
    # только на confidence; в MVP1 quality всегда weak, иначе TAKE недостижим.
    bad_finance = finance["verdict"] in ("на грани", "убыточно")

    fr_decision = getattr(filter_result, "decision", None)
    if hard_blocks:
        verdict = "SKIP"
        reason = "Есть блокирующий барьер: " + "; ".join(g["label"] for g in hard_blocks[:2])
    elif fr_decision == "NO-GO" and _has_stop_reasons(filter_result):
        verdict = "SKIP"
        reason = "Сработали стоп-факторы фильтров — участвовать не стоит."
    elif warnings or bad_finance:
        verdict = "THINK"
        bits = [g["label"] for g in warn_gates[:2]] + red_flags[:1]
        if bad_finance:
            bits = bits[:1] + [f"экономика «{finance['verdict']}»"]
        reason = ("Можно участвовать, но проверьте: " + "; ".join(bits)) if bits else \
                 "Можно участвовать, но оценка приблизительная — проверьте детали."
    else:
        verdict = "TAKE"
        reason = "Подходит под ваш профиль, явных барьеров не видно."

    confidence = _confidence(gates, finance, has_text)

    card = {
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "verdict_reason": reason,
        "confidence": round(confidence, 2),
        "gates": gates,
        "can_do": can_do,
        "finance": finance,
        "red_flags": red_flags,
        "green_flags": green_flags,
        "questions": [],  # MVP1: заполняет LLM в MVP2
        "glossary": GLOSSARY,
    }
    logger.info("decision_aid_build_finished: %s → %s (conf=%.2f)",
                tender.get("purchase_number", "?"), verdict, confidence)
    return card


# ══════════════════════════════════════════════════════════════════════════════
# Гейты допуска
# ══════════════════════════════════════════════════════════════════════════════

def _build_gates(tender, text, has_text, flags, category, by_num) -> list[dict]:
    gates: list[dict] = []
    gates.append(_gate_license(text, has_text, by_num))
    gates.append(_gate_national_regime(flags, has_text))
    gates.append(_gate_finance_load(tender))
    gates.append(_gate_experience(category))
    gates.append(_gate_deadline(tender))
    status_gate = _gate_status(tender)
    if status_gate:
        gates.append(status_gate)
    return gates


def _gate_license(text, has_text, by_num) -> dict:
    g = {"key": "license", "label": "Лицензии/допуски"}
    if not has_text:
        return {**g, "status": "unknown",
                "explain": "Документация не загружена — проверьте требования к лицензиям вручную."}
    hit = _license_forbidden_hits(text)
    if hit:
        return {**g, "status": "warn",
                "explain": f"В тексте есть упоминание лицензий/допусков: {', '.join(hit)}. Проверьте вручную."}
    # Дополнительно подсветим, если Ф5 нашёл жёсткие требования
    f5 = by_num.get(5)
    if f5 is not None and f5.score <= 2:
        return {**g, "status": "warn",
                "explain": "Есть строгие требования к участнику — проверьте, проходите ли вы."}
    return {**g, "status": "ok", "explain": "Особых лицензий (ФСТЭК/ФСБ/СРО) не требуется."}


def _license_forbidden_hits(text: str) -> list[str]:
    """Ищет рискованные допуски без ложного срабатывания СРО внутри слова «срок»."""
    try:
        import filter_engine as fe
        t = fe._normalize(text)
        forbidden = fe.PROFILE.get("forbidden", [])
    except Exception:
        return []

    hits: list[str] = []
    for term in forbidden:
        norm = fe._normalize(term)
        if not norm:
            continue
        if norm == "сро":
            if re.search(r"(?<![0-9a-zа-я])сро(?![0-9a-zа-я])", t):
                hits.append(term)
        elif norm in t:
            hits.append(term)
    return hits


def _gate_national_regime(flags, has_text) -> dict:
    g = {
        "key": "national_regime", "label": "Национальный режим / реестр ПО",
        "measure": flags.get("national_regime_measure", "unknown"),
        "registry_required": None,
        "registry_type": flags.get("registry_type", "unknown"),
    }
    if not has_text:
        return {**g, "status": "unknown",
                "explain": "Документация не загружена — проверьте требования нацрежима вручную."}
    if not flags.get("national_regime_detected"):
        return {**g, "status": "ok", "explain": "Признаков национального режима не найдено."}

    measure = flags.get("national_regime_measure", "unknown")
    registry_required = bool(flags.get("software_registry_required"))
    g["registry_required"] = registry_required if measure != "unknown" else None

    if measure == "ban" and registry_required:
        return {**g, "status": "block",
                "explain": "Запрет на иностранное ПО + нужна реестровая запись. Без ПО в реестре "
                           "заявку отклонят."}
    if measure == "ban":
        return {**g, "status": "warn",
                "explain": "Запрет допуска иностранного — уточните, требуется ли реестровое ПО."}
    if measure == "restriction":
        return {**g, "status": "warn",
                "explain": "Ограничение допуска (правило «третий лишний»/реестр). Проверьте требования к ПО."}
    if measure == "preference":
        return {**g, "status": "warn",
                "explain": "Преимущество отечественному (≈15%). Не блокирует, но влияет на цену."}
    return {**g, "status": "warn",
            "explain": "Затронут нацрежим — мера не определена, проверьте документацию."}


def _gate_finance_load(tender) -> dict:
    g = {"key": "finance", "label": "Финансовая нагрузка (обеспечение)"}
    nmck = _num(tender.get("price"))
    if nmck and nmck > float(config.PRICE_HARD_MAX):
        return {**g, "status": "block",
                "explain": f"НМЦК {_fmt(nmck)} выше потолка {_fmt(config.PRICE_HARD_MAX)} — "
                           "не хватит обеспечения."}
    app_sec = _num(tender.get("application_security_amount"))
    con_sec = _contract_security_amount(tender)
    war_sec = _num(tender.get("warranty_security_amount"))
    if app_sec is None and con_sec is None and war_sec is None:
        return {**g, "status": "unknown",
                "explain": "Сумму обеспечения в документах не нашли — проверьте вручную."}
    total = (app_sec or 0) + (con_sec or 0) + (war_sec or 0)
    if con_sec is not None and con_sec > float(config.MAX_CONTRACT_SECURITY):
        return {**g, "status": "warn",
                "explain": f"Обеспечение исполнения {_fmt(con_sec)} велико "
                           f"(порог {_fmt(config.MAX_CONTRACT_SECURITY)}) — выводит деньги из оборота."}
    if total > float(config.MAX_CONTRACT_SECURITY) + float(config.MAX_APPLICATION_SECURITY):
        return {**g, "status": "warn",
                "explain": f"Суммарное обеспечение {_fmt(total)} заметное — оцените кассовый разрыв."}
    return {**g, "status": "ok",
            "explain": f"Обеспечение посильное: {_fmt(total)}."}


def _gate_experience(category) -> dict:
    g = {"key": "experience", "label": "Опыт (ст. 31)"}
    try:
        import database as db
        contracts = db.kb_contracts_by_category(category, limit=3)
        any_contracts = contracts or db.kb_contracts_list()
    except Exception:
        return {**g, "status": "unknown",
                "explain": "База опыта недоступна — проверьте требование к опыту вручную."}
    if not any_contracts:
        return {**g, "status": "unknown",
                "explain": "В базе знаний нет ваших контрактов — добавьте опыт в /kb."}
    if contracts:
        return {**g, "status": "ok",
                "explain": f"Есть похожий опыт в категории «{category}» ({len(contracts)} контр.)."}
    return {**g, "status": "warn",
            "explain": "Нет прямого опыта в этой категории — проверьте требование ст. 31 в ТЗ."}


def _gate_deadline(tender) -> dict:
    g = {"key": "deadline", "label": "Срок подачи"}
    try:
        import filter_engine as fe
        days = fe._days_left(tender.get("deadline"))
    except Exception:
        days = None
    if days is None:
        return {**g, "status": "unknown", "explain": "Срок подачи не распознан — проверьте на площадке."}
    if days < 0:
        return {**g, "status": "block", "explain": f"Срок подачи истёк ({abs(days)} дн. назад)."}
    if days <= 2:
        return {**g, "status": "warn", "explain": f"Мало времени до подачи: {days} дн."}
    return {**g, "status": "ok", "explain": f"До подачи {days} дн. — времени достаточно."}


def _gate_status(tender) -> Optional[dict]:
    status = str(tender.get("status") or "").lower()
    if status in _CLOSED_STATUSES:
        return {"key": "status", "label": "Статус закупки", "status": "block",
                "explain": f"Закупка в статусе «{status}» — участвовать уже нельзя."}
    return None


# ══════════════════════════════════════════════════════════════════════════════
# «Смогу ли сделать» — мэтчинг компетенций
# ══════════════════════════════════════════════════════════════════════════════

def _build_can_do(text, has_text) -> dict:
    if not has_text:
        return {"status": "unknown", "score": None, "matched": [], "gaps": [],
                "note": "Документация не загружена — оценка по требованиям недоступна."}
    try:
        from knowledge_base import match_competencies
        m = match_competencies(text)
    except Exception:
        logger.exception("match_competencies failed")
        return {"status": "unknown", "score": None, "matched": [], "gaps": []}

    score = m.get("score")
    gaps = m.get("gaps") or []
    matched = m.get("matched") or []
    if score is None:
        status = "unknown"
    elif score >= 60 and not gaps:
        status = "ok"
    elif score >= 30:
        status = "warn"
    else:
        status = "warn"
    return {"status": status, "score": score, "matched": matched, "gaps": gaps,
            "note": m.get("note", "")}


# ══════════════════════════════════════════════════════════════════════════════
# Финмодель
# ══════════════════════════════════════════════════════════════════════════════

def estimate_economics(tender: dict[str, Any], category: str = "прочее") -> dict[str, Any]:
    """Грубая финмодель. None = «не определили». `quality` помечает точность."""
    none_fin = {
        "quality": "weak", "nmck": None, "recommended_bid": None, "tax_usn": None,
        "app_security": None, "contract_security": None, "advance_pct": None,
        "cash_gap": None, "net_margin": None, "net_margin_pct": None,
        "verdict": "unknown", "assumptions": [],
    }
    nmck = _num(tender.get("price"))
    if not nmck:
        none_fin["assumptions"] = ["НМЦК не указана — расчёт невозможен."]
        return none_fin

    # Профиль (ставки)
    try:
        from knowledge_base import get_profile
        profile = get_profile()
        cost_rate = float(profile.get("cost_rate", 0.60))
        target_margin = float(profile.get("target_margin", 0.25))
    except Exception:
        cost_rate, target_margin = 0.60, 0.25

    assumptions: list[str] = []
    law_type = tender.get("law_type", "44-ФЗ")
    try:
        from winner_analytics import recommend_bid
        bid = recommend_bid(nmck, category, law_type,
                            target_margin=target_margin, cost_rate=cost_rate)
        recommended = _num(bid.get("recommended")) or round(nmck * 0.95)
        if not bid.get("sample_count"):
            assumptions.append("нет статистики по категории — ставка взята консервативно.")
    except Exception:
        recommended = round(nmck * 0.95)
        assumptions.append("ценовой коридор недоступен — ставка ≈ −5% от НМЦК.")

    tax_usn = round(recommended * 0.06)
    cost = round(recommended * cost_rate)
    assumptions.append(f"себестоимость оценена грубо ({int(cost_rate*100)}% от ставки): "
                       "нет данных по трудозатратам.")

    app_sec = _num(tender.get("application_security_amount"))
    con_sec = _contract_security_amount(tender)
    advance_pct = _num(tender.get("advance_percent"))

    advance_amount = round(recommended * advance_pct / 100) if advance_pct else 0
    if advance_pct is None:
        assumptions.append("аванс в документах не найден — принят за 0.")
    cash_gap = max(0, cost - advance_amount + (con_sec or 0))

    net_margin = recommended - tax_usn - cost
    net_margin_pct = net_margin / recommended if recommended else 0.0

    if net_margin_pct >= 0.25:
        verdict = "выгодно"
    elif net_margin_pct >= 0.10:
        verdict = "на грани"
    else:
        verdict = "убыточно"

    return {
        "quality": "weak",  # MVP1: всегда грубая оценка (нет данных по часам)
        "nmck": round(nmck),
        "recommended_bid": recommended,
        "tax_usn": tax_usn,
        "app_security": round(app_sec) if app_sec is not None else None,
        "contract_security": round(con_sec) if con_sec is not None else None,
        "advance_pct": advance_pct,
        "cash_gap": cash_gap,
        "net_margin": net_margin,
        "net_margin_pct": round(net_margin_pct, 3),
        "verdict": verdict,
        "assumptions": assumptions,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Флаги (зелёные/красные) простым языком
# ══════════════════════════════════════════════════════════════════════════════

def _build_flags(flags, by_num, tender) -> tuple[list[str], list[str]]:
    green: list[str] = []
    red: list[str] = []

    if flags.get("remote_possible"):
        green.append("Можно делать удалённо — выезд не требуется.")
    if flags.get("onsite_required"):
        red.append("Указан выезд/работа на объекте — учтите командировки.")

    f1 = by_num.get(1)
    if f1 is not None and f1.score >= 4:
        green.append("Профильная задача — прямо ваш стек.")
    f4 = by_num.get(4)
    if f4 is not None and f4.score <= 2:
        red.append("Жёсткие сроки/режим (SLA) — оцените, потянете ли.")
    f6 = by_num.get(6)
    if f6 is not None and f6.score <= 2:
        red.append("Похоже на заточку под конкретного исполнителя.")
    f7 = by_num.get(7)
    if f7 is not None and f7.score <= 2:
        red.append("Признаки перекупа/поставки без работ — не ваш профиль.")
    f8 = by_num.get(8)
    if f8 is not None and f8.score <= 2:
        red.append("Рискованные условия договора (штрафы/оплата) — читайте внимательно.")

    if _num(tender.get("advance_percent")):
        green.append("Есть аванс — меньше кассовый разрыв.")

    return green, red


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные
# ══════════════════════════════════════════════════════════════════════════════

def build_ambiguities(card: dict, criteria: dict | None = None,
                      flags: dict | None = None) -> list[dict]:
    """Детерминированные неоднозначности для запроса на разъяснение (MVP3).

    Правило: вопрос появляется ТОЛЬКО при unknown / противоречии / нехватке данных.
    Про то, что уже ясно (обеспечение найдено, лицензий не требуется) — не спрашиваем.
    Возвращает список {topic, question, reason}. Лимит (3–5) накладывает вызывающий код.
    """
    out: list[dict] = []
    gates = {g.get("key"): g for g in (card.get("gates") or [])}

    g = gates.get("license")
    if g and g.get("status") == "unknown":
        out.append({"topic": "license",
                    "question": "Просим подтвердить, требуется ли для выполнения работ лицензия "
                                "ФСТЭК/ФСБ или иной специальный допуск.",
                    "reason": g.get("explain", "")})

    g = gates.get("national_regime")
    if g and (g.get("status") in ("unknown", "warn")):
        out.append({"topic": "national_regime",
                    "question": "Просим разъяснить применение национального режима: требуется ли "
                                "реестровая запись российского/евразийского ПО для участия.",
                    "reason": g.get("explain", "")})

    g = gates.get("finance")
    if g and g.get("status") == "unknown":
        out.append({"topic": "finance",
                    "question": "Просим указать размеры обеспечения заявки и обеспечения "
                                "исполнения контракта, а также условия и сроки оплаты.",
                    "reason": g.get("explain", "")})

    # Конфликт удалёнка/выезд (или просто требование выезда)
    onsite = bool(flags and flags.get("onsite_required"))
    if onsite:
        out.append({"topic": "remote",
                    "question": "Просим уточнить, допускается ли удалённое выполнение работ или "
                                "обязателен выезд на объект заказчика.",
                    "reason": "В документации упоминается выезд на объект."})

    # Чего не хватает по компетенциям → вопрос про эквиваленты/аналоги
    gaps = (card.get("can_do") or {}).get("gaps") or []
    if gaps:
        out.append({"topic": "scope",
                    "question": "Просим разъяснить, допускается ли применение эквивалентов/аналогов "
                                f"по требованиям: {', '.join(gaps[:5])}.",
                    "reason": "Эти требования не покрываются нашим профилем напрямую."})

    # Критерии оценки: есть раздел, но вес критерия не распознан
    if criteria and criteria.get("has_criteria"):
        unclear = [c for c in (criteria.get("criteria") or []) if c.get("weight") is None]
        if unclear:
            label = unclear[0].get("label", "нестоимостные критерии")
            out.append({"topic": "criteria",
                        "question": f"Просим разъяснить порядок оценки и значимость (вес) по "
                                    f"критерию «{label}».",
                        "reason": "Вес критерия не указан явно."})

    return out


def _has_stop_reasons(filter_result) -> bool:
    """True, если у FilterResult есть явные стоп-факторы (не «просто низкий скоринг»)."""
    return bool(getattr(filter_result, "stop_factors", None))


def _confidence(gates, finance, has_text) -> float:
    unknown_gates = sum(1 for g in gates if g["status"] == "unknown")
    c = 1.0
    c -= 0.12 * unknown_gates
    c -= 0.15 if finance.get("quality") == "weak" else 0.0
    c -= 0.20 if not has_text else 0.0
    return max(0.0, min(1.0, c))


def _details_dict(tender: dict[str, Any]) -> dict[str, Any]:
    details = tender.get("details_json")
    if isinstance(details, dict):
        return details
    if isinstance(details, str) and details.strip():
        try:
            parsed = json.loads(details)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _contract_security_amount(tender: dict[str, Any]) -> float | None:
    direct = _num(tender.get("contract_security_amount"))
    if direct is not None:
        return direct
    details = _details_dict(tender)
    contract_security = details.get("contract_security")
    if not isinstance(contract_security, dict):
        return None
    return _num(contract_security.get("amount"))


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(" ", "").replace(" ", "").replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value) -> str:
    if value in (None, ""):
        return "не найдено"
    try:
        return f"{float(value):,.0f} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)
