"""filter_engine.py — 8-фильтровый движок оценки тендеров.

Идея:
- Stage 1: быстрый скоринг по карточке закупки, без скачивания ТЗ.
- Stage 2: детальный скоринг по карточке + странице + документам/ТЗ.

Каждый фильтр даёт 1–5 баллов. Итого 8–40.
Результат сохраняется в PostgreSQL через database.save_filter_result().
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import config


PROFILE = {
    "competencies": [
        "1с-битрикс", "битрикс24", "bitrix", "битрикс", "интеграция с 1с",
        "обмен с 1с", "crm", "api", "личный кабинет", "php", "mysql", "postgresql",
        "сопровождение сайта", "доработка сайта", "модернизация сайта",
        "техническая поддержка сайта", "администрирование сайта", "сервер", "vps",
        "linux", "резервное копирование", "терминал сбора данных", "сканер штрихкода",
        "принтер этикеток", "сетевое оборудование", "видеонаблюдение", "скуд",
        "обмен данными",
    ],
    "forbidden": ["фстэк", "фсб", "сро", "государственная тайна"],
}


# ══════════════════════════════════════════════════════════════════════════════
# СЛОВАРЬ ФРАЗ — единый источник истины для 8 фильтров.
#
# Это ДЕФОЛТЫ. При наличии записей в таблице scoring_rules (редактируется в /rules)
# движок берёт фразы оттуда, иначе откатывается на эти списки — поэтому система
# работает даже до миграции/наполнения БД.
#
# Ключ: (номер фильтра 1–8, имя «корзины»). Значение: список фраз-триггеров.
# Фраза может содержать "|" (любой из вариантов) и "*" (для wildcard-корзин).
# Каждое правило в БД дополнительно может иметь стражей:
#   unless  — не срабатывать, если рядом есть одна из фраз (Битрикс БЕЗ «лицензий»),
#   require — срабатывать только если рядом есть одна из фраз.
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_BUCKETS: dict[tuple[int, str], list[str]] = {
    # ── Ф1 Профиль задачи ──────────────────────────────────────────────────
    (1, "strong"): [
        "1с-битрикс", "битрикс24", "bitrix", "битрикс", "интеграция с 1с",
        "обмен с 1с", "crm", "api", "личный кабинет", "php", "mysql", "postgresql",
    ],
    (1, "medium"): [
        "сопровождение сайта", "доработка сайта", "модернизация сайта", "техническая поддержка сайта",
        "администрирование сайта", "сервер", "vps", "linux", "резервное копирование",
        "терминал сбора данных", "сканер штрихкода", "принтер этикеток", "сетевое оборудование",
        "видеонаблюдение", "скуд", "обмен данными",
    ],
    (1, "bad"): [
        "smm", "ведение социальных сетей", "seo-продвижение", "контекстная реклама",
        "дизайн-концепция", "мобильное приложение", "разработка мобильного приложения",
        "поставка лицензий", "продление лицензий",
    ],
    # ── Ф2 Финансовый вход (фразовая часть) ────────────────────────────────
    (2, "no_advance"): [
        "аванс не предусмотрен", "авансирование не предусмотрено", "без аванса",
    ],
    # ── Ф3 Объём и границы работ ───────────────────────────────────────────
    (3, "unlimited"): [
        "неограниченное количество", "без ограничения объема", "без ограничения объёма",
        "любые доработки", "все необходимые работы", "по требованию заказчика",
        "по заявкам заказчика", "поддержка всех систем", "в полном объеме по заявкам",
    ],
    (3, "bounded"): [
        "лимит часов", "не более * часов", "перечень работ", "фиксированный объем",
        "фиксированный объём", "отдельные доработки", "по отдельному заданию", "приемка по актам",
        "приёмка по актам", "техническое задание содержит перечень",
    ],
    # ── Ф4 Сроки и SLA ─────────────────────────────────────────────────────
    (4, "hard"): [
        "24/7", "круглосуточно", "ежедневно", "в выходные", "праздничные дни",
        "не более 1 часа", "не более одного часа", "в течение 1 часа", "в течение одного часа",
        "аварийное восстановление", "выезд специалиста", "срок реакции",
    ],
    (4, "calm"): [
        "в рабочие дни", "в рабочее время", "не более 3 рабочих дней", "не более 5 рабочих дней",
        "по согласованию сторон", "плановые работы", "срок выполнения заявки согласовывается",
    ],
    # ── Ф5 Требования к участнику ──────────────────────────────────────────
    (5, "hard"): [
        "лицензия фстэк", "лицензии фстэк",
        "лицензия фсб", "лицензии фсб",
        "государственная тайна", "гостайна",
        "членство в сро",
        "наличие допуска сро",
        "критическая информационная инфраструктура", "кии",
        "аттестат соответствия фстэк",
        "допуск к государственной тайне",
        "лицензия на осуществление деятельности",
    ],
    (5, "medium"): [
        "опыт исполнения аналогичных", "аналогичный контракт",
        "не менее 3 лет", "не менее трех лет",
        "аккредитация минцифры", "реестр российского по",
        "сертификат специалиста", "штатных сотрудников",
        "персональные данные", "152-фз",
    ],
    (5, "easy"): [
        "единые требования", "декларация соответствия", "отсутствие задолженности",
    ],
    # ── Ф6 Признаки «под своего» ───────────────────────────────────────────
    (6, "signs"): [
        "существующая система", "действующая система", "текущая система", "ранее разработан",
        "продолжение сопровождения", "без передачи исходного кода", "исходный код не передается",
        "обследование до подачи", "осмотр объекта до подачи", "знание существующего кода",
        "модули заказчика", "внутренний портал", "эксплуатируемой системы", "доработка имеющейся системы",
        "доступы предоставляются после заключения", "документация отсутствует",
    ],
    # ── Ф7 Поставка / перекуп / логистика ──────────────────────────────────
    (7, "good_combo"): [
        "поставка и настройка", "поставка, монтаж и настройка", "внедрение", "настройка оборудования",
        "интеграция с 1с", "терминал сбора данных", "сканер штрихкода", "принтер этикеток",
        "nas", "резервное копирование", "сетевое оборудование", "видеонаблюдение", "скуд",
    ],
    (7, "commodity"): [
        "ноутбук", "ноутбуков", "монитор", "мониторов", "мфу", "картридж", "картриджей",
        "канцеляр", "офисная бумага", "антивирус", "продление лицензий", "поставка лицензий",
        "персональные компьютеры", "системные блоки", "планшет", "смартфон",
    ],
    (7, "pure_service"): [
        "сопровождение сайта", "доработка сайта", "интеграция", "техническая поддержка сайта",
        "администрирование сервера", "разработка модуля", "обмен данными",
    ],
    (7, "fast_delivery"): [
        "в течение 1 дня", "в течение 2 дней", "в течение 3 дней", "складской остаток",
    ],
    # ── Ф8 Договорные риски ────────────────────────────────────────────────
    (8, "real_risk"): [
        "казначейское сопровождение",
        "ответственность за простой",
        "штраф в размере 20",
        "штраф в размере 30",
        "штраф за каждый день",
        "односторонний отказ заказчика",
        "расторжение без возмещения",
        "мотивированный отказ от подписания",
    ],
    (8, "warn_risk"): [
        "штраф в размере 10",
        "оплата после",
        "в течение 60 дней",
        "в течение 90 дней",
        "гарантийный срок 3 года",
        "гарантийный срок 5 лет",
    ],
    (8, "good"): [
        "поэтапная оплата", "оплата в течение 7 рабочих дней",
        "оплата в течение 10 рабочих дней", "аванс",
        "этапы выполнения", "акт оказанных услуг", "акт выполненных работ",
    ],
}

# Корзины, где фразы матчатся как wildcard (символ "*" → .{0,40}).
WILDCARD_BUCKETS: set[tuple[int, str]] = {(3, "bounded")}

# Человекочитаемые имена корзин — для UI-редактора и подсказок.
BUCKET_LABELS: dict[str, str] = {
    "strong": "сильный профиль (+)", "medium": "смежно (+)", "bad": "не наш профиль (−)",
    "no_advance": "нет аванса (−)",
    "unlimited": "безлимит объёма (−)", "bounded": "объём ограничен (+)",
    "hard": "жёсткое (−)", "calm": "спокойный режим (+)", "easy": "стандартное (+)",
    "signs": "признаки заточки (−)",
    "good_combo": "товар+услуга (+)", "commodity": "перекуп-товар (−)",
    "pure_service": "чистая услуга (+)", "fast_delivery": "жёсткая логистика (−)",
    "real_risk": "серьёзный риск (−−)", "warn_risk": "риск (−)", "good": "нормальная конструкция (+)",
}

FILTER_NAMES: dict[int, str] = {
    1: "Профиль задачи", 2: "Финансовый вход", 3: "Объём и границы работ",
    4: "Сроки и SLA", 5: "Требования к участнику", 6: "Признаки 'под своего'",
    7: "Поставка / перекуп / логистика", 8: "Договорные риски",
}


# ── Загрузка правил из БД с кешем и откатом на DEFAULT_BUCKETS ──────────────
_RULES_CACHE: dict[tuple[int, str], list[tuple[str, list[str], list[str]]]] | None = None
_RULES_CACHE_TS: float = 0.0
_RULES_TTL = 60.0


def _load_rules() -> dict[tuple[int, str], list[tuple[str, list[str], list[str]]]]:
    """Возвращает {(dim, bucket): [(term, unless, require), ...]} из scoring_rules.

    Кешируется на 60 секунд. При недоступной/пустой БД возвращает пустой словарь —
    тогда _bucket_entries() откатывается на DEFAULT_BUCKETS.
    """
    global _RULES_CACHE, _RULES_CACHE_TS
    import time
    now = time.monotonic()
    if _RULES_CACHE is not None and (now - _RULES_CACHE_TS) < _RULES_TTL:
        return _RULES_CACHE

    grouped: dict[tuple[int, str], list[tuple[str, list[str], list[str]]]] = {}
    try:
        import database as db
        for r in db.scoring_rules_active():
            key = (int(r.get("dim") or 0), str(r.get("bucket") or ""))
            grouped.setdefault(key, []).append((
                str(r.get("term") or ""),
                list(r.get("unless") or []),
                list(r.get("require") or []),
            ))
    except Exception:
        grouped = {}

    _RULES_CACHE = grouped
    _RULES_CACHE_TS = now
    return grouped


def invalidate_rules_cache() -> None:
    """Сбросить кеш правил — вызывать после сохранения/удаления через UI."""
    global _RULES_CACHE, _RULES_CACHE_TS
    _RULES_CACHE = None
    _RULES_CACHE_TS = 0.0


def _bucket_entries(dim: int, bucket: str) -> list[tuple[str, list[str], list[str]]]:
    rules = _load_rules()
    db_entries = rules.get((dim, bucket))
    if db_entries:
        return db_entries
    return [(p, [], []) for p in DEFAULT_BUCKETS.get((dim, bucket), [])]


def _term_hit(text: str, term: str, wildcard: bool = False) -> bool:
    """True, если фраза (или любой из вариантов через "|") встречается в тексте."""
    alts = [t.strip() for t in term.split("|")] if "|" in term else [term]
    return bool(_hits(text, [a for a in alts if a], wildcard=wildcard))


def _match_bucket(text: str, dim: int, bucket: str) -> list[str]:
    """Возвращает список сработавших фраз корзины с учётом стражей unless/require."""
    wildcard = (dim, bucket) in WILDCARD_BUCKETS
    found: list[str] = []
    for term, unless, require in _bucket_entries(dim, bucket):
        if not _term_hit(text, term, wildcard):
            continue
        if unless and any(_term_hit(text, u) for u in unless):
            continue
        if require and not any(_term_hit(text, rq) for rq in require):
            continue
        if term not in found:
            found.append(term)
    return found


@dataclass
class FilterScore:
    number: int
    name: str
    score: int
    signals: list[str] = field(default_factory=list)
    stop_factor: bool = False

    def __post_init__(self) -> None:
        self.score = max(1, min(5, int(self.score or 1)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "filter_number": self.number,
            "filter_name": self.name,
            "score": self.score,
            "signals": " | ".join(self.signals),
            "stop_factor": self.stop_factor,
        }


@dataclass
class FilterResult:
    purchase_number: str
    tender_title: str
    stage: str
    filters: list[FilterScore]
    total_score: int
    decision: str
    stop_factors: list[str] = field(default_factory=list)

    def to_reasons(self) -> list[str]:
        reasons: list[str] = []
        for f in self.filters:
            first = f.signals[0] if f.signals else "нет явных сигналов"
            prefix = "⛔" if f.stop_factor else f"Ф{f.number}"
            reasons.append(f"{prefix} {f.name}: {f.score}/5 — {first}")
        if self.stop_factors:
            reasons.append("Стоп-факторы: " + "; ".join(self.stop_factors[:5]))
        reasons.append(f"Итог 8 фильтров: {self.total_score}/40 — {self.decision}")
        return reasons

    def to_filter_scores(self) -> list[dict[str, Any]]:
        return [f.to_dict() for f in self.filters]


def run_stage1_filters(tender: dict[str, Any], text: str = "") -> FilterResult:
    """Быстрые фильтры по карточке закупки."""
    return run_filters(tender, text=text, stage="stage1")


def run_stage2_filters(tender: dict[str, Any], text: str = "") -> FilterResult:
    """Детальные фильтры по карточке + странице + документам."""
    return run_filters(tender, text=text, stage="stage2")


def run_filters(tender: dict[str, Any], text: str = "", stage: str = "stage2") -> FilterResult:
    full_text = _build_text(tender, text)

    filters = [
        _filter_profile(tender, full_text, stage),
        _filter_finance(tender, full_text, stage),
        _filter_scope(tender, full_text, stage),
        _filter_sla(tender, full_text, stage),
        _filter_requirements(tender, full_text, stage),
        _filter_tailored(tender, full_text, stage),
        _filter_supply_resell(tender, full_text, stage),
        _filter_contract_risks(tender, full_text, stage),
    ]

    try:
        from knowledge_base import apply_custom_rules, has_stop_rule_match
        kb_delta, kb_signals = apply_custom_rules(full_text)
        kb_stop = has_stop_rule_match(full_text)
        if kb_signals:
            f0 = filters[0]
            new_score = max(1, min(5, f0.score + max(-3, min(3, kb_delta // 3))))
            filters[0] = FilterScore(
                f0.number,
                f0.name,
                new_score,
                f0.signals + kb_signals,
                stop_factor=f0.stop_factor or kb_stop,
            )
    except Exception:
        pass

    # Мягкая интеграция поисковых профилей (search_profiles.py, Stage 1): скор
    # профиля с лучшим совпадением корректирует Ф1 на ±1 (см. docs/tz-search-profiles.md, §7.3).
    if tender.get("matched_profiles"):
        filters[0] = _apply_profile_signal(filters[0], tender)

    # Мягкая интеграция LLM-триажа (Stage 1): вердикт корректирует ТОЛЬКО Ф1
    # (профиль) на ±1–2 балла. Итоговое решение по-прежнему считается по фильтрам.
    triage = tender.get("llm_triage")
    if isinstance(triage, dict) and triage.get("verdict"):
        filters[0] = _apply_triage(filters[0], triage)

    total = sum(f.score for f in filters)
    stop_factors: list[str] = []
    for f in filters:
        if f.stop_factor:
            signal = f.signals[0] if f.signals else f.name
            stop_factors.append(f"Ф{f.number} {f.name}: {signal}")

    decision = _decision(total, stop_factors, stage=stage, filters=filters)
    return FilterResult(
        purchase_number=str(tender.get("purchase_number", "")),
        tender_title=str(tender.get("title", "")),
        stage=stage,
        filters=filters,
        total_score=total,
        decision=decision,
        stop_factors=stop_factors,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ФИЛЬТРЫ
# ══════════════════════════════════════════════════════════════════════════════


def _filter_profile(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Профиль задачи"
    strong = _match_bucket(text, 1, "strong")
    medium = _match_bucket(text, 1, "medium")
    bad = _match_bucket(text, 1, "bad")
    offprofile = _offprofile_markers(text)

    try:
        from knowledge_base import get_profile as _kb_profile
        kb = _kb_profile()
        kb_comps = kb.get("competencies", [])
        kb_strong = [c for c in kb_comps if c in text.lower() and c not in strong + medium]
        if kb_strong:
            strong = strong + kb_strong[:3]
    except Exception:
        kb_strong = []

    signals = []
    signals += [f"✓ профиль: {x}" for x in strong[:4]]
    signals += [f"✓ смежно: {x}" for x in medium[:3]]
    if kb_strong:
        signals += [f"✓ [БЗ] компетенция: {x}" for x in kb_strong[:2]]
    signals += [f"⚠ не основной профиль: {x}" for x in bad[:3]]

    if offprofile:
        return FilterScore(
            1,
            name,
            1,
            [f"✗ не ИТ-разработка/поддержка: {x}" for x in offprofile[:3]],
            stop_factor=True,
        )

    if strong:
        score = 5 if len(strong) >= 2 else 4
    elif medium:
        score = 4 if len(medium) >= 2 else 3
    elif bad:
        score = 2
    else:
        score = 2
        signals.append("⚠ нет явного совпадения с Битрикс/1С/CRM/API/серверами")

    if bad and not (strong or medium):
        score = min(score, 2)

    # Страж перекупа: «поставка/предоставление лицензий», поставка оборудования/
    # товара без работ по настройке/доработке/внедрению — это перекуп, не наш
    # профиль, даже если в названии есть «Битрикс/1С/сервер». Иначе Ф1 ошибочно = 5.
    if _is_resale_without_service(text):
        score = min(score, 2)
        signals.append("⚠ перекуп/поставка без работ по настройке — не наш профиль")

    return FilterScore(1, name, score, signals or ["нет явных профильных сигналов"])


def _filter_finance(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Финансовый вход"
    price = _num(tender.get("price"))
    app_sec = _num(tender.get("application_security_amount"))
    contract_sec = _num(tender.get("contract_security_amount"))
    if contract_sec is None:
        contract_sec = _contract_security_from_details(tender)
    warranty_sec = _num(tender.get("warranty_security_amount"))
    advance = _num(tender.get("advance_percent"))

    signals: list[str] = []
    score = 3
    price_stop = False

    # Ценовой коридор скоринга: целевая зона — бонус; вне её, но в пределах
    # hard_max — штраф 1 балл; выше hard_max — отсекаем (стоп-фактор: не хватит
    # обеспечения). Профиль (search_profiles.py) может переопределить границы —
    # tender["profile_price_*"] выставляется в search_profiles.filter_and_tag();
    # что не задано в профиле (None), берётся из глобального config.
    lo = float(tender.get("profile_price_lo") if tender.get("profile_price_lo") is not None
               else getattr(config, "PRICE_TARGET_LO", 100_000))
    hi = float(tender.get("profile_price_hi") if tender.get("profile_price_hi") is not None
               else getattr(config, "PRICE_TARGET_HI", 500_000))
    hard_max = float(tender.get("profile_price_hard_max") if tender.get("profile_price_hard_max") is not None
                      else getattr(config, "PRICE_HARD_MAX", 1_000_000))
    if price:
        if lo <= price <= hi:
            score += 1
            signals.append(f"✓ НМЦК в целевом коридоре {_money(lo)}–{_money(hi)}: {_money(price)}")
        elif price > hard_max:
            score = 1
            price_stop = True
            signals.append(f"✗ НМЦК выше {_money(hard_max)} — отсекаем (не хватит обеспечения): {_money(price)}")
        else:
            score -= 1
            signals.append(f"⚠ НМЦК вне целевого коридора: {_money(price)}")
    else:
        signals.append("⚠ НМЦК не найдена")

    if stage == "stage1":
        # На первом этапе обеспечение часто ещё не извлечено.
        return FilterScore(2, name, max(1, min(5, score)), signals, stop_factor=price_stop)

    total_security = (app_sec or 0) + (contract_sec or 0) + (warranty_sec or 0)
    if app_sec:
        signals.append(f"обеспечение заявки: {_money(app_sec)}")
        if app_sec > float(config.MAX_APPLICATION_SECURITY):
            score -= 1
    if contract_sec:
        signals.append(f"обеспечение исполнения: {_money(contract_sec)}")
        if contract_sec > float(config.MAX_CONTRACT_SECURITY):
            score -= 2
        elif contract_sec <= float(config.MAX_CONTRACT_SECURITY) / 2:
            score += 1
    if warranty_sec:
        signals.append(f"⚠ гарантийное обеспечение: {_money(warranty_sec)}")
        score -= 1
    if advance:
        signals.append(f"✓ аванс: {advance:g}%")
        score += 1
    elif _match_bucket(text, 2, "no_advance"):
        signals.append("⚠ аванс не предусмотрен")
        score -= 1

    if total_security:
        if total_security <= 50_000:
            score += 1
            signals.append(f"✓ финансовый вход до 50 тыс.: {_money(total_security)}")
        elif total_security > 300_000:
            score -= 2
            signals.append(f"✗ финансовый вход выше 300 тыс.: {_money(total_security)}")
        elif total_security > 150_000:
            score -= 1
            signals.append(f"⚠ финансовый вход 150–300 тыс.: {_money(total_security)}")
    else:
        signals.append("обеспечение не найдено в извлечённом тексте")

    stop = price_stop or bool(total_security and total_security > 500_000)
    return FilterScore(2, name, score, signals, stop_factor=stop)


def _filter_scope(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Объём и границы работ"
    unlimited = _match_bucket(text, 3, "unlimited")
    bounded = _match_bucket(text, 3, "bounded")
    place_warnings, place_stops = _non_moscow_work_place_risks(tender, text)

    score = 3
    signals: list[str] = []
    if bounded:
        score += 1 if len(bounded) == 1 else 2
        signals += [f"✓ объём ограничен: {x}" for x in bounded[:4]]
        signals += _context_signals(text, bounded[:4])
    if unlimited:
        score -= 2 if len(unlimited) == 1 else 3
        signals += [f"✗ риск безлимита: {x}" for x in unlimited[:4]]
        signals += _context_signals(text, unlimited[:4])
    if not bounded and not unlimited:
        signals.append("⚠ явных границ объёма не найдено")
    if place_warnings:
        score -= 1
        signals += [f"⚠ выездной/локальный риск: {x}" for x in place_warnings]
    if place_stops:
        score = 1
        signals = [f"✗ не московский лот, работа только на месте: {x}" for x in place_stops] + signals

    stop = bool(place_stops) or len(unlimited) >= 2 or any("неогранич" in x for x in unlimited)
    return FilterScore(3, name, score, signals, stop_factor=stop)


def _filter_sla(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Сроки и SLA"
    days_left = _days_left(tender.get("deadline"))
    hard = _match_bucket(text, 4, "hard")
    calm = _match_bucket(text, 4, "calm")

    score = 4
    signals: list[str] = []
    if days_left is not None:
        # Короткий срок подачи сам по себе не является стоп-фактором:
        # простую поставку/лёгкую услугу иногда можно взять и за 1–2 дня.
        # Он только снижает оценку и усиливает другие риски.
        if days_left < 0:
            score -= 3
            signals.append(f"✗ срок подачи истёк: {abs(days_left)} дн. назад; исключить из активной выдачи")
        elif days_left < 1:
            score -= 2
            signals.append(f"⚠ срок подачи почти истёк: {days_left} дн.; не стоп сам по себе")
        elif days_left < 3:
            score -= 1
            signals.append(f"⚠ короткий срок подачи: {days_left} дн.; проверять сложность")
        elif days_left < 5:
            signals.append(f"⚠ до окончания подачи {days_left} дн.")
        else:
            signals.append(f"✓ до окончания подачи {days_left} дн.")
    if calm:
        score += 1
        signals += [f"✓ спокойный режим: {x}" for x in calm[:3]]
    if hard:
        score -= min(3, len(hard))
        signals += [f"⚠ жесткий SLA: {x}" for x in hard[:5]]

    hard_sla_stop = ("24/7" in hard or "круглосуточно" in hard) and any(
        any(mark in x for mark in ("1 часа", "одного часа")) for x in hard
    )
    stop = hard_sla_stop
    return FilterScore(4, name, score, signals or ["SLA явно не найден"], stop_factor=stop)


def _filter_requirements(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Требования к участнику"
    # Жёсткие стоп-требования — явная невозможность участия
    hard = _match_bucket(text, 5, "hard")
    medium = _match_bucket(text, 5, "medium")
    easy = _match_bucket(text, 5, "easy")

    score = 5
    signals: list[str] = []
    if easy:
        signals += [f"✓ стандартное требование: {x}" for x in easy[:2]]
    if medium:
        score -= min(2, len(medium))
        signals += [f"⚠ требование проверить: {x}" for x in medium[:5]]
    if hard:
        score = 1
        signals += [f"✗ жесткое требование: {x}" for x in hard[:5]]

    # На Stage 1 ТЗ ещё не скачано — отсутствие требований не подтверждено.
    # Не ставим максимум 5 «на доверии», держим нейтральные 3, чтобы итог не
    # раздувался на неоценённом фильтре.
    if stage == "stage1" and not hard and not medium:
        score = 3
        signals.append("· требования по ТЗ не проверены (Stage 1)")

    return FilterScore(5, name, score, signals or ["специальные требования не найдены"], stop_factor=bool(hard))


def _filter_tailored(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Признаки 'под своего'"
    days_left = _days_left(tender.get("deadline"))
    signs = _match_bucket(text, 6, "signs")
    score = 5
    signals: list[str] = []

    if days_left is not None and days_left < 3:
        # Сам по себе короткий срок не значит «под своего». Это только усиливает
        # подозрения, если вместе с ним есть признаки текущего подрядчика.
        signals.append(f"⚠ короткий срок подачи: {days_left} дн.; стоп только вместе с признаками заточки")
    if signs:
        score -= min(4, len(signs))
        signals += [f"⚠ возможно текущий подрядчик: {x}" for x in signs[:6]]
    if not signs:
        signals.append("явных признаков заточки не найдено")

    # На Stage 1 ТЗ не скачано — заточку по карточке не оценить, держим нейтраль.
    if stage == "stage1" and not signs:
        score = 3

    stop = len(signs) >= 4 or (days_left is not None and days_left < 3 and len(signs) >= 2)
    return FilterScore(6, name, score, signals, stop_factor=stop)


def _filter_supply_resell(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Поставка / перекуп / логистика"
    good_combo = _match_bucket(text, 7, "good_combo")
    commodity = _match_bucket(text, 7, "commodity")
    pure_service = _match_bucket(text, 7, "pure_service")
    fast_delivery = _match_bucket(text, 7, "fast_delivery")

    score = 4
    signals: list[str] = []
    if pure_service:
        score += 1
        signals.append("✓ преимущественно услуга, не нужна товарная оборотка")
    if good_combo:
        score += 1
        signals += [f"✓ товар + услуга/настройка: {x}" for x in good_combo[:4]]
    if commodity:
        score -= min(3, len(commodity))
        signals += [f"⚠ товарная конкуренция: {x}" for x in commodity[:5]]
    if fast_delivery:
        score -= 1
        signals += [f"⚠ жесткая логистика: {x}" for x in fast_delivery[:3]]

    stop = len(commodity) >= 4 and not good_combo
    return FilterScore(7, name, score, signals or ["поставка/логистика явно не выделены"], stop_factor=stop)


def _filter_contract_risks(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Договорные риски"
    # ── Реальные риски — специфичные паттерны, не встречающиеся в стандартных контрактах ──
    # НЕ включаем: "штраф", "пени", "приемка" — они есть в 100% контрактов по 44-ФЗ
    # и снижают балл за нормальные закупки.
    real_risk = _match_bucket(text, 8, "real_risk")
    if "казначейское сопровождение" in real_risk and _treasury_support_not_required(tender, text):
        real_risk = [risk for risk in real_risk if risk != "казначейское сопровождение"]
    warn_risk = _match_bucket(text, 8, "warn_risk")
    good = _match_bucket(text, 8, "good")

    score = 4
    signals: list[str] = []
    if good:
        score = min(5, score + 1)
        signals += [f"✓ нормальная конструкция: {x}" for x in good[:3]]
    if real_risk:
        score -= min(3, len(real_risk)) * 2
        signals += [f"✗ серьёзный договорный риск: {x}" for x in real_risk[:4]]
        signals += _context_signals(text, real_risk[:4])
    if warn_risk:
        score -= min(2, len(warn_risk))
        signals += [f"⚠ договорный риск: {x}" for x in warn_risk[:4]]
        signals += _context_signals(text, warn_risk[:4])
    if not real_risk and not warn_risk and not good:
        signals.append("договорные условия явно не извлечены")

    # Стоп только на реально критичные условия
    stop = bool("казначейское сопровождение" in real_risk or "ответственность за простой" in real_risk)
    return FilterScore(8, name, score, signals, stop_factor=stop)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════


_RESALE_MARKERS = [
    "поставка лицензи", "поставке лицензи", "предоставление лицензи",
    "предоставлению лицензи", "продление лицензи", "право использовани",
    "права использовани", "неисключительн", "поставка оборудовани",
    "поставка серверного", "поставка товара", "поставка литератур",
    "приобретение оборудовани", "приобретение сервер", "закупка оборудовани",
]
_SERVICE_MARKERS = [
    "настройк", "внедрен", "доработк", "разработк", "интеграц", "сопровожден",
    "модернизац", "администрирован", "обслуживани", "миграц",
    "техническая поддержк", "техническое сопровожд",
]

_OFFPROFILE_MARKERS = [
    "широкополосному доступу",
    "доступ к информационно-коммуникационной сети интернет",
    "доступу к информационно-коммуникационной сети интернет",
    "доступ к сети интернет",
    "услуги связи",
    "телематическ",
    "канал связи",
    "содержание автомобильных дорог",
    "ремонт автомобильных дорог",
    "автомобильных дорог регионального",
]


def _is_resale_without_service(text: str) -> bool:
    """True, если это поставка/перекуп (лицензии, оборудование, товар) БЕЗ работ."""
    has_resale = any(m in text for m in _RESALE_MARKERS)
    if not has_resale:
        return False
    return not any(m in text for m in _SERVICE_MARKERS)


def _offprofile_markers(text: str) -> list[str]:
    return [marker for marker in _OFFPROFILE_MARKERS if marker in text]


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


def _is_moscow_lot(tender: dict[str, Any]) -> bool:
    values = [
        tender.get("region", ""),
        tender.get("customer", ""),
        tender.get("title", ""),
    ]
    details = _details_dict(tender)
    for key in ("customer_address", "delivery_place", "place", "region"):
        value = details.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            values.extend(str(v) for v in value.values() if v)

    haystack = _normalize(" ".join(str(v) for v in values if v))
    return re.search(r"(?<![а-яa-z])москва(?![а-яa-z])", haystack) is not None


def _work_scope_text(tender: dict[str, Any], fallback_text: str) -> str:
    details = _details_dict(tender)
    scope = details.get("work_scope")
    parts: list[str] = []
    if isinstance(scope, dict):
        for key in ("title", "text", "source"):
            value = scope.get(key)
            if isinstance(value, str):
                parts.append(value)
        for key in ("items", "conditions"):
            value = scope.get(key)
            if isinstance(value, list):
                parts.extend(str(item) for item in value if item)
    elif isinstance(scope, str):
        parts.append(scope)

    if parts:
        return _normalize("\n".join(parts))
    return fallback_text


def _non_moscow_work_place_risks(tender: dict[str, Any], text: str) -> tuple[list[str], list[str]]:
    if _is_moscow_lot(tender):
        return [], []

    scope_text = _work_scope_text(tender, text)
    warnings: list[str] = []
    stops: list[str] = []

    if "за пределы" in scope_text:
        warnings.append("есть формулировка «за пределы»")
    if "строго на рабочем месте заказчика" in scope_text:
        stops.append("«строго на рабочем месте Заказчика»")
    if "строго на рабочих местах заказчика" in scope_text:
        stops.append("«строго на рабочих местах Заказчика»")
    if "удаленное оказание услуг не предусмотрено" in scope_text:
        stops.append("«Удаленное оказание услуг не предусмотрено»")

    return warnings, stops


def _contract_security_from_details(tender: dict[str, Any]) -> float | None:
    details = _details_dict(tender)
    contract_security = details.get("contract_security")
    if not isinstance(contract_security, dict):
        return None
    return _num(contract_security.get("amount"))


def _treasury_support_not_required(tender: dict[str, Any], text: str) -> bool:
    details = _details_dict(tender)
    support = details.get("contract_support")
    if isinstance(support, dict):
        required = support.get("required")
        if required is False:
            return True
        support_text = _normalize(str(support.get("text") or ""))
        if _negated_treasury_support(support_text):
            return True

    return _negated_treasury_support(text)


def _negated_treasury_support(text: str) -> bool:
    normalized = _normalize(text)
    if "казначейск" not in normalized:
        return False
    for match in re.finditer(r"казначейск\w*(?:\s+\w+){0,4}\s+сопровожд\w*", normalized):
        fragment = normalized[max(0, match.start() - 120):match.end() + 120]
        if re.search(r"\bне\s+(?:требуется|предусмотрено|установлено)\b", fragment):
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Детекторы для помощника решения (decision_aid.py)
#
# detect_flags() — не часть скоринга 8 фильтров; это отдельные сигналы для
# карточки решения новичка: национальный режим / реестр ПО и возможность
# удалённой работы. Все маркеры пишутся уже нормализованными (нижний регистр,
# «ё»→«е»), т.к. сравниваются с _normalize(text).
# ══════════════════════════════════════════════════════════════════════════════

# Признак того, что в закупке вообще затронут национальный режим / реестр ПО.
_NR_MARKERS = [
    "национальный режим", "ст. 14 44-фз", "статья 14 44-фз", "постановление 1875",
    "пп рф №1875", "пп №1875", "постановление 1236", "пп №1236",
    "реестр российского по", "реестр российского программного обеспечения",
    "реестр евразийского по", "реестр евразийского программного обеспечения",
    "реестровая запись", "российское происхождение",
    "доверенное программное обеспечение",
]
# Якоря: рядом с ними слова «запрет/ограничение/преимущество» считаем мерой
# нацрежима (а не, например, «запрет привлекать субподрядчика»).
_NR_ANCHORS = [
    "национальный режим", "ст. 14", "статья 14", "постановление 1875", "№1875",
    "реестр российского", "реестр евразийского", "реестровая запись",
    "российское происхождение",
]
_NR_BAN = ["запрет допуска", "запрет на допуск", "запрещен допуск", "запрет закупки"]
_NR_RESTRICTION = ["ограничение допуска", "ограничения допуска", "ограничение закупки"]
_NR_PREFERENCE = ["преимущество", "ценовая преференция", "пятнадцатипроцент", "преференци"]

_REGISTRY_RU = [
    "реестр российского по", "реестр российского программного обеспечения",
    "российское происхождение", "реестровая запись",
]
_REGISTRY_EAEU = ["реестр евразийского по", "реестр евразийского программного обеспечения"]
_TRUSTED_SW = ["доверенное программное обеспечение", "доверенное по"]

_REMOTE_MARKERS = [
    "дистанционно", "удаленный доступ", "удаленно", "по vpn", "без выезда",
    "электронная приемка",
]
_ONSITE_MARKERS = [
    "выезд на объект", "на территории заказчика", "по месту нахождения заказчика",
    "очное обследование", "монтаж на объекте", "выезд специалиста",
]


def _measure_in_context(text: str, terms: Iterable[str], anchors: Iterable[str],
                        window: int = 400) -> bool:
    """True, если одно из `terms` встречается в окне ±window рядом с якорем `anchors`."""
    anchors = list(anchors)
    for term in terms:
        start = 0
        while True:
            idx = text.find(term, start)
            if idx == -1:
                break
            lo = max(0, idx - window)
            hi = min(len(text), idx + len(term) + window)
            ctx = text[lo:hi]
            if any(a in ctx for a in anchors):
                return True
            start = idx + len(term)
    return False


def detect_flags(text: str) -> dict:
    """Сигналы нацрежима/реестра ПО и удалёнки для карточки решения.

    Возвращает dict с булевыми/строковыми полями. На пустом тексте — нейтральные
    значения (ничего не обнаружено), решение об «unknown» принимает decision_aid.
    """
    t = _normalize(text or "")
    empty = not t
    nr_detected = (not empty) and any(m in t for m in _NR_MARKERS)

    measure = "unknown"
    if nr_detected:
        if _measure_in_context(t, _NR_BAN, _NR_ANCHORS):
            measure = "ban"
        elif _measure_in_context(t, _NR_RESTRICTION, _NR_ANCHORS):
            measure = "restriction"
        elif _measure_in_context(t, _NR_PREFERENCE, _NR_ANCHORS):
            measure = "preference"

    registry_ru = (not empty) and any(m in t for m in _REGISTRY_RU)
    registry_eaeu = (not empty) and any(m in t for m in _REGISTRY_EAEU)
    registry_record = (not empty) and ("реестровая запись" in t or "реестровый номер" in t)
    trusted = (not empty) and any(m in t for m in _TRUSTED_SW)
    software_registry_required = bool(
        registry_ru or registry_eaeu or registry_record
        or (nr_detected and measure in ("ban", "restriction"))
    )

    remote = (not empty) and any(m in t for m in _REMOTE_MARKERS)
    onsite = (not empty) and any(m in t for m in _ONSITE_MARKERS)
    if remote and onsite:
        remote = False  # выезд важнее: не показываем «можно удалённо»

    return {
        "national_regime_detected": nr_detected,
        "national_regime_measure": measure,
        "software_registry_required": software_registry_required,
        "registry_record_required": registry_record,
        "domestic_software_required": registry_ru,
        "trusted_software_required": trusted,
        "registry_type": (
            "russian_software" if registry_ru
            else "eurasian_software" if registry_eaeu
            else "unknown"
        ),
        "remote_possible": remote,
        "onsite_required": onsite,
    }


def _apply_profile_signal(profile_filter: "FilterScore", tender: dict[str, Any]) -> "FilterScore":
    """Мягкая корректировка Ф1 по скору поисковых профилей (search_profiles.py).

    Профиль явно перекрыл порог (скор ≥ min_score+4) → +1. Тендер прошёл
    только за счёт того, что сработали минус-фразы со штрафом (был хотя бы
    один отрицательный хит) → -1. Иначе — без изменений.
    """
    matches = tender.get("matched_profiles") or []
    if not isinstance(matches, list) or not matches:
        return profile_filter
    try:
        best = max((m for m in matches if isinstance(m, dict)), key=lambda m: m.get("score", 0))
    except ValueError:
        return profile_filter

    score = best.get("score")
    min_score = best.get("min_score")
    if not isinstance(score, (int, float)) or not isinstance(min_score, (int, float)):
        return profile_filter

    hits = best.get("phrases") or []
    had_penalty = any(isinstance(h, dict) and (h.get("weight") or 0) < 0 for h in hits)

    delta = 0
    if score >= min_score + 4:
        delta = 1
    elif had_penalty:
        delta = -1
    if delta == 0:
        return profile_filter

    tag = f"🔎 профиль «{best.get('profile_name', '')}»: скор {score}" + (" (со штрафом)" if had_penalty and delta < 0 else "")
    new_score = max(1, min(5, profile_filter.score + delta))
    return FilterScore(
        profile_filter.number,
        profile_filter.name,
        new_score,
        profile_filter.signals + [tag],
        stop_factor=profile_filter.stop_factor,
    )


def _apply_triage(profile: "FilterScore", triage: dict) -> "FilterScore":
    """Мягкая корректировка Ф1 по вердикту LLM-триажа (±1–2 балла)."""
    verdict = str(triage.get("verdict", "")).upper()
    fit = str(triage.get("fit", "")).lower()
    resale = bool(triage.get("resale"))
    reason = str(triage.get("reason", "")).strip()

    delta = 0
    if resale or verdict == "МИМО" or fit == "нет":
        delta = -2
    elif verdict == "БЕРУ" or fit == "да":
        delta = +2
    elif verdict == "ЧАСТИЧНО" or fit == "частично":
        delta = -1

    new_score = max(1, min(5, profile.score + delta))
    tag = f"🤖 LLM: {verdict}" + (f" — {reason}" if reason else "")
    return FilterScore(
        profile.number,
        profile.name,
        new_score,
        profile.signals + [tag],
        stop_factor=profile.stop_factor,
    )


def _decision(total: int, stop_factors: list[str], stage: str = "stage2", filters: list["FilterScore"] | None = None) -> str:
    if stop_factors:
        # Критичные стоп-факторы лучше не маскировать высоким профильным баллом.
        return "NO-GO"
    if stage == "stage1" and filters:
        # На Stage 1 фильтры 3/5/6 не оценены по ТЗ (держатся нейтральными), поэтому
        # решение считаем по оцениваемым по карточке: Ф1,Ф2,Ф4,Ф7,Ф8 (макс 25).
        by_num = {f.number: f.score for f in filters}
        ev_sum = sum(by_num.get(n, 3) for n in (1, 2, 4, 7, 8))
        profile = by_num.get(1, 3)
        if ev_sum >= 20 and profile >= 3:
            return "GO"
        if ev_sum >= 15:
            return "CAUTION"
        return "NO-GO"
    if total >= 32:
        return "GO"
    if total >= 24:
        return "CAUTION"
    return "NO-GO"


def _build_text(tender: dict[str, Any], text: str) -> str:
    parts = [
        tender.get("title", ""),
        tender.get("customer", ""),
        tender.get("law_type", ""),
        tender.get("deadline", ""),
        tender.get("published_at", ""),
        tender.get("payment_terms", ""),
        text or "",
    ]
    return _normalize("\n".join(str(p) for p in parts if p is not None))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def _contains(text: str, phrases: Iterable[str]) -> bool:
    return bool(_hits(text, phrases))


def _hits(text: str, phrases: Iterable[str], wildcard: bool = False) -> list[str]:
    found: list[str] = []
    normalized = text
    for phrase in phrases:
        p = _normalize(phrase)
        if not p:
            continue
        ok = False
        if wildcard and "*" in p:
            pattern = re.escape(p).replace(r"\*", r".{0,40}")
            ok = re.search(pattern, normalized, re.IGNORECASE) is not None
        else:
            ok = p in normalized
        if ok and phrase not in found:
            found.append(phrase)
    return found


def _context_signals(text: str, terms: Iterable[str], max_chars: int = 700) -> list[str]:
    signals: list[str] = []
    seen: set[str] = set()
    for term in terms:
        fragment = _term_context(text, term, max_chars=max_chars)
        if fragment and fragment not in seen:
            signals.append(f"↳ {fragment}")
            seen.add(fragment)
    return signals


def _term_context(text: str, term: str, max_chars: int = 700) -> str:
    span = _find_term_span(text, term)
    if not span:
        return ""

    start_idx, end_idx = span
    start = _context_left_boundary(text, start_idx, max_chars=max_chars)
    end = _context_right_boundary(text, end_idx, max_chars=max_chars)
    fragment = text[start:end].strip(" \t\r\n.;")

    if len(fragment) > max_chars:
        original_len = len(fragment)
        rel_start = max(0, start_idx - start)
        rel_end = max(rel_start, end_idx - start)
        before = max(0, rel_start - max_chars // 3)
        after = min(original_len, rel_end + max_chars * 2 // 3)
        fragment = fragment[before:after].strip(" \t\r\n.;")
        if before > 0:
            fragment = "... " + fragment
        if after < original_len:
            fragment = fragment + " ..."

    return fragment.replace("|", "/")


def _find_term_span(text: str, term: str) -> tuple[int, int] | None:
    alts = [t.strip() for t in term.split("|")] if "|" in term else [term]
    for alt in alts:
        p = _normalize(alt)
        if not p:
            continue
        if "*" in p:
            pattern = re.escape(p).replace(r"\*", r".{0,40}")
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.span()
        else:
            idx = text.find(p)
            if idx >= 0:
                return idx, idx + len(p)
    return None


def _context_left_boundary(text: str, idx: int, max_chars: int) -> int:
    floor = max(0, idx - max_chars)
    best = floor
    for match in re.finditer(r"[.!?;]\s+", text[floor:idx]):
        pos = floor + match.end()
        if pos > best:
            best = pos
    return best


def _context_right_boundary(text: str, idx: int, max_chars: int) -> int:
    ceiling = min(len(text), idx + max_chars)
    match = re.search(r"[.!?;](?:\s+|$)", text[idx:ceiling])
    if match:
        return idx + match.end()
    return ceiling


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(" ", "").replace("\u00a0", "").replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value: float | int | None) -> str:
    if not value:
        return "0 ₽"
    return f"{float(value):,.0f} ₽".replace(",", " ")


def _days_left(value: Any) -> int | None:
    if not value:
        return None
    s = str(value)
    dt: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(s[:19] if "%H" in fmt and len(s) >= 19 else s[:10], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if dt is None:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (dt.date() - now.date()).days
