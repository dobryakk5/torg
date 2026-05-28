"""filter_engine.py — 8-фильтровый движок оценки тендеров.

Идея:
- Stage 1: быстрый скоринг по карточке закупки, без скачивания ТЗ.
- Stage 2: детальный скоринг по карточке + странице + документам/ТЗ.

Каждый фильтр даёт 1–5 баллов. Итого 8–40.
Результат сохраняется в PostgreSQL через database.save_filter_result().
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import config


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

    # ── Применяем персональные правила из Базы Знаний ────────────────────────
    try:
        from knowledge_base import apply_custom_rules, has_stop_rule_match
        kb_delta, kb_signals = apply_custom_rules(full_text)
        kb_stop = has_stop_rule_match(full_text)
        if kb_signals:
            # Добавляем KB-сигналы к первому фильтру (профиль), корректируем скор
            f0 = filters[0]
            new_score = max(1, min(5, f0.score + max(-3, min(3, kb_delta // 3))))
            filters[0] = FilterScore(
                f0.number, f0.name, new_score,
                f0.signals + kb_signals,
                stop_factor=f0.stop_factor or kb_stop,
            )
    except Exception:
        pass   # KB недоступна — работаем без неё

    total = sum(f.score for f in filters)
    stop_factors: list[str] = []
    for f in filters:
        if f.stop_factor:
            signal = f.signals[0] if f.signals else f.name
            stop_factors.append(f"Ф{f.number} {f.name}: {signal}")

    decision = _decision(total, stop_factors)
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
    strong = _hits(text, [
        "1с-битрикс", "битрикс24", "bitrix", "битрикс", "интеграция с 1с",
        "обмен с 1с", "crm", "api", "личный кабинет", "php", "mysql", "postgresql",
    ])
    medium = _hits(text, [
        "сопровождение сайта", "доработка сайта", "модернизация сайта", "техническая поддержка сайта",
        "администрирование сайта", "сервер", "vps", "linux", "резервное копирование",
        "терминал сбора данных", "сканер штрихкода", "принтер этикеток", "сетевое оборудование",
        "видеонаблюдение", "скуд", "обмен данными",
    ])
    bad = _hits(text, [
        "smm", "ведение социальных сетей", "seo-продвижение", "контекстная реклама",
        "дизайн-концепция", "мобильное приложение", "разработка мобильного приложения",
        "поставка лицензий", "продление лицензий",
    ])

    # Дополняем компетенциями из Базы Знаний
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
    return FilterScore(1, name, score, signals or ["нет явных профильных сигналов"])


def _filter_finance(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Финансовый вход"
    price = _num(tender.get("price"))
    app_sec = _num(tender.get("application_security_amount"))
    contract_sec = _num(tender.get("contract_security_amount"))
    warranty_sec = _num(tender.get("warranty_security_amount"))
    advance = _num(tender.get("advance_percent"))

    signals: list[str] = []
    score = 3

    if price:
        if 300_000 <= price <= 1_500_000:
            score += 1
            signals.append(f"✓ НМЦК в рабочем диапазоне: {_money(price)}")
        elif 1_500_000 < price <= 3_000_000:
            signals.append(f"⚠ НМЦК выше стартовой зоны: {_money(price)}")
        elif price < 200_000:
            score -= 1
            signals.append(f"⚠ маленькая НМЦК: {_money(price)}")
        elif price > 5_000_000:
            score -= 1
            signals.append(f"⚠ высокая НМЦК: {_money(price)}")
    else:
        signals.append("⚠ НМЦК не найдена")

    if stage == "stage1":
        # На первом этапе обеспечение часто ещё не извлечено.
        return FilterScore(2, name, max(1, min(5, score)), signals)

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
    elif _contains(text, ["аванс не предусмотрен", "авансирование не предусмотрено", "без аванса"]):
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

    stop = bool(total_security and total_security > 500_000)
    return FilterScore(2, name, score, signals, stop_factor=stop)


def _filter_scope(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Объём и границы работ"
    unlimited = _hits(text, [
        "неограниченное количество", "без ограничения объема", "без ограничения объёма",
        "любые доработки", "все необходимые работы", "по требованию заказчика",
        "по заявкам заказчика", "поддержка всех систем", "в полном объеме по заявкам",
    ])
    bounded = _hits(text, [
        "лимит часов", "не более * часов", "перечень работ", "фиксированный объем",
        "фиксированный объём", "отдельные доработки", "по отдельному заданию", "приемка по актам",
        "приёмка по актам", "техническое задание содержит перечень",
    ], wildcard=True)

    score = 3
    signals: list[str] = []
    if bounded:
        score += 1 if len(bounded) == 1 else 2
        signals += [f"✓ объём ограничен: {x}" for x in bounded[:4]]
    if unlimited:
        score -= 2 if len(unlimited) == 1 else 3
        signals += [f"✗ риск безлимита: {x}" for x in unlimited[:4]]
    if not bounded and not unlimited:
        signals.append("⚠ явных границ объёма не найдено")

    stop = len(unlimited) >= 2 or any("неогранич" in x for x in unlimited)
    return FilterScore(3, name, score, signals, stop_factor=stop)


def _filter_sla(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Сроки и SLA"
    days_left = _days_left(tender.get("deadline"))
    hard = _hits(text, [
        "24/7", "круглосуточно", "ежедневно", "в выходные", "праздничные дни",
        "не более 1 часа", "не более одного часа", "в течение 1 часа", "в течение одного часа",
        "аварийное восстановление", "выезд специалиста", "срок реакции",
    ])
    calm = _hits(text, [
        "в рабочие дни", "в рабочее время", "не более 3 рабочих дней", "не более 5 рабочих дней",
        "по согласованию сторон", "плановые работы", "срок выполнения заявки согласовывается",
    ])

    score = 4
    signals: list[str] = []
    if days_left is not None:
        # Короткий срок подачи сам по себе не является стоп-фактором:
        # простую поставку/лёгкую услугу иногда можно взять и за 1–2 дня.
        # Он только снижает оценку и усиливает другие риски.
        if days_left < 1:
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
    hard = _hits(text, [
        "лицензия фстэк", "лицензии фстэк",
        "лицензия фсб", "лицензии фсб",
        "государственная тайна", "гостайна",
        "членство в сро",           # точная фраза, не ловит "аэросъёмка"
        "наличие допуска сро",
        "критическая информационная инфраструктура", "кии",
        "аттестат соответствия фстэк",
        "допуск к государственной тайне",
        "лицензия на осуществление деятельности",
    ])
    medium = _hits(text, [
        "опыт исполнения аналогичных", "аналогичный контракт",
        "не менее 3 лет", "не менее трех лет",
        "аккредитация минцифры", "реестр российского по",
        "сертификат специалиста", "штатных сотрудников",
        "персональные данные", "152-фз",
    ])
    easy = _hits(text, [
        "единые требования", "декларация соответствия", "отсутствие задолженности",
    ])

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

    return FilterScore(5, name, score, signals or ["специальные требования не найдены"], stop_factor=bool(hard))


def _filter_tailored(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Признаки 'под своего'"
    days_left = _days_left(tender.get("deadline"))
    signs = _hits(text, [
        "существующая система", "действующая система", "текущая система", "ранее разработан",
        "продолжение сопровождения", "без передачи исходного кода", "исходный код не передается",
        "обследование до подачи", "осмотр объекта до подачи", "знание существующего кода",
        "модули заказчика", "внутренний портал", "эксплуатируемой системы", "доработка имеющейся системы",
        "доступы предоставляются после заключения", "документация отсутствует",
    ])
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

    stop = len(signs) >= 4 or (days_left is not None and days_left < 3 and len(signs) >= 2)
    return FilterScore(6, name, score, signals, stop_factor=stop)


def _filter_supply_resell(tender: dict[str, Any], text: str, stage: str) -> FilterScore:
    name = "Поставка / перекуп / логистика"
    good_combo = _hits(text, [
        "поставка и настройка", "поставка, монтаж и настройка", "внедрение", "настройка оборудования",
        "интеграция с 1с", "терминал сбора данных", "сканер штрихкода", "принтер этикеток",
        "nas", "резервное копирование", "сетевое оборудование", "видеонаблюдение", "скуд",
    ])
    commodity = _hits(text, [
        "ноутбук", "ноутбуков", "монитор", "мониторов", "мфу", "картридж", "картриджей",
        "канцеляр", "офисная бумага", "антивирус", "продление лицензий", "поставка лицензий",
        "персональные компьютеры", "системные блоки", "планшет", "смартфон",
    ])
    pure_service = _hits(text, [
        "сопровождение сайта", "доработка сайта", "интеграция", "техническая поддержка сайта",
        "администрирование сервера", "разработка модуля", "обмен данными",
    ])
    fast_delivery = _hits(text, ["в течение 1 дня", "в течение 2 дней", "в течение 3 дней", "складской остаток"])

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
    real_risk = _hits(text, [
        "казначейское сопровождение",           # блокирует платежи — стоп
        "ответственность за простой",
        "штраф в размере 20",                   # конкретная сумма штрафа — уже риск
        "штраф в размере 30",
        "штраф за каждый день",
        "односторонний отказ заказчика",        # право заказчика отказаться без оснований
        "расторжение без возмещения",
        "мотивированный отказ от подписания",
    ])
    warn_risk = _hits(text, [
        "штраф в размере 10",
        "оплата после",                         # только если нет аванса — смотреть контекст
        "в течение 60 дней",                    # длинный срок оплаты
        "в течение 90 дней",
        "гарантийный срок 3 года",              # длинный гарантийный период
        "гарантийный срок 5 лет",
    ])
    good = _hits(text, [
        "поэтапная оплата", "оплата в течение 7 рабочих дней",
        "оплата в течение 10 рабочих дней", "аванс",
        "этапы выполнения", "акт оказанных услуг", "акт выполненных работ",
    ])

    score = 4
    signals: list[str] = []
    if good:
        score = min(5, score + 1)
        signals += [f"✓ нормальная конструкция: {x}" for x in good[:3]]
    if real_risk:
        score -= min(3, len(real_risk)) * 2
        signals += [f"✗ серьёзный договорный риск: {x}" for x in real_risk[:4]]
    if warn_risk:
        score -= min(2, len(warn_risk))
        signals += [f"⚠ договорный риск: {x}" for x in warn_risk[:4]]
    if not real_risk and not warn_risk and not good:
        signals.append("договорные условия явно не извлечены")

    # Стоп только на реально критичные условия
    stop = bool("казначейское сопровождение" in real_risk or "ответственность за простой" in real_risk)
    return FilterScore(8, name, score, signals, stop_factor=stop)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════


def _decision(total: int, stop_factors: list[str]) -> str:
    if stop_factors:
        # Критичные стоп-факторы лучше не маскировать высоким профильным баллом.
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
        tender.get("matched_keywords", ""),
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
