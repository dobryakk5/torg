"""llm_analyzer.py — анализ закупки через Claude API с контекстом из Базы Знаний."""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Базовый профиль (fallback если БЗ пустая) ────────────────────────────────
_BASE_PROFILE = """
Профиль исполнителя:
- 1С-Битрикс, Bitrix24, сайты, личные кабинеты;
- интеграции: 1С, CRM, API, платежи;
- серверное администрирование: Linux/Windows, VPS, backup;
- поставка с настройкой: ТСД, сканеры штрихкода, принтеры этикеток, сетевое оборудование;
- без лицензий ФСТЭК/ФСБ/СРО;
- целевая цена: 300 тыс. – 2 млн ₽;
- нежелательны: безлимитные заявки, 24/7, жесткий SLA, большое обеспечение.
"""

_SYSTEM_TEMPLATE = """
Ты — опытный тендерный аналитик. Помогаешь ИТ-специалисту выбирать выгодные закупки по 44-ФЗ и 223-ФЗ.

{profile_block}
{kb_context}
Дай короткий практический вывод. Не придумывай данные, которых нет в тексте.

Формат ответа строго:
ВЕРДИКТ: [СМОТРЕТЬ / ОСТОРОЖНО / ПРОПУСТИТЬ]

ПОДХОДИТ ПО ПРОФИЛЮ: [да/нет/частично]

ФИНАНСЫ:
- НМЦК:
- обеспечение заявки:
- обеспечение исполнения:
- аванс:
- оплата:
- риск кассового разрыва:

ОБЪЕМ:
- что надо сделать:
- есть ли лимит часов:
- есть ли безлимитные заявки:
- срок исполнения:

РИСКИ:
- ...

ПРИЗНАКИ "ПОД СВОЕГО":
- ...

ВОПРОСЫ ЗАКАЗЧИКУ:
1.
2.
3.

ИТОГ:
- смотреть / не смотреть и почему
"""

USER_PROMPT_TEMPLATE = """
Проанализируй закупку:

Название: {title}
Заказчик: {customer}
НМЦК: {price}
Закон: {law_type}
Срок подачи: {deadline}
Обеспечение заявки: {application_security}
Обеспечение исполнения: {contract_security}
Ссылка: {url}

Текст карточки и документов:
---
{page_text}
---
"""


def _build_system_prompt(tender: dict, page_text: str) -> str:
    """Собирает system prompt: базовый профиль + контекст из БЗ."""
    # Пробуем загрузить живой профиль из БЗ
    profile_block = _BASE_PROFILE
    kb_context    = ""

    try:
        from knowledge_base import build_llm_context, get_profile
        profile = get_profile()
        comps   = profile.get("competencies", [])
        if comps:
            profile_block = (
                "\nПрофиль исполнителя (из Базы Знаний):\n"
                + f"- Технологии: {', '.join(comps[:20])}\n"
                + f"- Целевая цена: {profile.get('price_target_lo',300_000):,.0f}"
                  f" – {profile.get('price_target_hi',2_000_000):,.0f} ₽\n"
                + "- Без лицензий ФСТЭК/ФСБ/СРО.\n"
            )
        raw_kb = build_llm_context(tender, page_text[:3000])
        if raw_kb:
            kb_context = "\n" + raw_kb + "\n"
    except Exception:
        pass

    return _SYSTEM_TEMPLATE.format(
        profile_block=profile_block,
        kb_context=kb_context,
    )


def analyze_tender(tender: dict, page_text: str = "") -> Optional[str]:
    import config
    import llm_provider

    if not llm_provider.is_configured():
        logger.info("LLM-провайдер без ключа — детальный анализ пропущен")
        return None

    def money(value):
        return f"{value:,.0f} ₽".replace(",", " ") if value else "не найдено"

    system_prompt = _build_system_prompt(tender, page_text)

    user_message = USER_PROMPT_TEMPLATE.format(
        title                = tender.get("title", "—"),
        customer             = tender.get("customer", "—"),
        price                = money(tender.get("price")),
        law_type             = tender.get("law_type", "—"),
        deadline             = tender.get("deadline", "—"),
        application_security = money(tender.get("application_security_amount")),
        contract_security    = money(tender.get("contract_security_amount")),
        url                  = tender.get("url", "—"),
        page_text            = (page_text or "(текст не получен)")[:config.LLM_TEXT_CHARS],
    )

    result = llm_provider.complete(
        system=system_prompt,
        user=user_message,
        model=llm_provider.deep_model(),
        max_tokens=1200,
    )
    if result:
        logger.info("LLM-анализ получен для '%s'", tender.get("title", "?")[:60])
    return result


# ── Триаж по карточке (Stage 1) ──────────────────────────────────────────────
# Дешёвый массовый проход: классифицируем КАРТОЧКУ (без ТЗ), чтобы отсеять
# ложные совпадения ключевых слов (книги/мебель «с личным кабинетом») и перекуп
# лицензий/железа, и поднять реальные ИТ-работы.

_TRIAGE_SYSTEM = """\
Ты — тендерный аналитик ИТ-подрядчика (1С-Битрикс, сайты, личные кабинеты,
интеграции 1С/CRM/API, серверное администрирование, backup, поставка ИТ-оборудования
С НАСТРОЙКОЙ). Тебе дают КАРТОЧКУ закупки (без полного ТЗ). Оцени пригодность.
{profile_block}
Важно:
- «поставка/предоставление лицензий», «продление лицензий», «право использования»
  без работ по настройке/доработке/внедрению — это ПЕРЕКУП (resale=true), не наш профиль.
- поставка оборудования/литературы/мебели без ИТ-настройки — не наш профиль.
- случайное совпадение слова (напр. «личный кабинет» в поставке книг) — fit=нет.

Ответь СТРОГО одним JSON-объектом, без пояснений вокруг:
{{"verdict":"БЕРУ|ЧАСТИЧНО|МИМО","fit":"да|частично|нет","resale":true|false,"category":"<2-4 слова>","reason":"<до 200 символов, по-русски>"}}
"""

_TRIAGE_USER = """\
Название: {title}
Заказчик: {customer}
НМЦК: {price}
Закон: {law_type}
Совпавшие ключевые слова: {keywords}

Текст карточки:
---
{text}
---
"""


def triage_tender(tender: dict) -> Optional[dict]:
    """Быстрый триаж карточки через дешёвую LLM. Возвращает dict или None.

    Ключи результата: verdict (БЕРУ/ЧАСТИЧНО/МИМО), fit (да/частично/нет),
    resale (bool), category (str), reason (str).
    """
    import config
    import llm_provider

    if not llm_provider.is_configured():
        return None

    profile_block = ""
    try:
        from knowledge_base import get_profile
        comps = get_profile().get("competencies", [])
        if comps:
            profile_block = "Технологии профиля: " + ", ".join(comps[:20]) + "."
    except Exception:
        pass

    def money(value):
        return f"{value:,.0f} ₽".replace(",", " ") if value else "не указана"

    kws = tender.get("matched_keywords") or ""
    if isinstance(kws, (list, tuple)):
        kws = ", ".join(kws)
    text = (tender.get("primary_text") or tender.get("title") or "")[: config.LLM_TRIAGE_TEXT_CHARS]

    raw = llm_provider.complete(
        system=_TRIAGE_SYSTEM.format(profile_block=profile_block),
        user=_TRIAGE_USER.format(
            title=tender.get("title", "—"),
            customer=tender.get("customer", "—"),
            price=money(tender.get("price")),
            law_type=tender.get("law_type", "—"),
            keywords=kws or "—",
            text=text or "(текст не получен)",
        ),
        model=llm_provider.triage_model(),
        max_tokens=300,
        temperature=0.0,
        json_mode=True,
    )
    data = llm_provider.parse_json(raw)
    if not data:
        return None
    return _normalize_triage(data)


def _normalize_triage(data: dict) -> dict:
    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in {"БЕРУ", "ЧАСТИЧНО", "МИМО"}:
        verdict = "ЧАСТИЧНО"
    fit = str(data.get("fit", "")).strip().lower()
    if fit not in {"да", "частично", "нет"}:
        fit = {"БЕРУ": "да", "ЧАСТИЧНО": "частично", "МИМО": "нет"}[verdict]
    resale = data.get("resale")
    resale = bool(resale) if isinstance(resale, bool) else str(resale).strip().lower() in {"true", "да", "1"}
    return {
        "verdict": verdict,
        "fit": fit,
        "resale": resale,
        "category": str(data.get("category", "")).strip()[:80],
        "reason": str(data.get("reason", "")).strip()[:250],
    }


# ── MVP2: объяснение готовой карточки решения простым языком ──────────────────
# LLM получает УЖЕ посчитанную карточку (decision_aid.build_card) и только объясняет
# её новичку: ничего не пересчитывает, не меняет вердикт/деньги/гейты.

# Версия промпта — часть ключа кеша. Меняй при правках формулировок ниже.
EXPLAIN_PROMPT_VERSION = "v1"

_EXPLAIN_SYSTEM = """\
Ты — наставник для новичка в госзакупках (ИТ-подрядчик: 1С-Битрикс, сайты, интеграции,
серверы, поставка ИТ-оборудования с настройкой). Тебе дают УЖЕ ГОТОВУЮ карточку решения
по тендеру (вердикт, гейты допуска, финмодель, флаги), посчитанную кодом.

ТВОЯ ЗАДАЧА — объяснить эту карточку простым языком. СТРОГО:
- НЕ пересчитывай числа (НМЦК, маржу, обеспечение) — бери их как есть из карточки.
- НЕ меняй вердикт (БЕРИ/ПОДУМАЙ/ПРОПУСТИ) — объясняй именно его.
- Опирайся ТОЛЬКО на данные карточки и текст тендера, не выдумывай фактов.
- Пиши коротко и по-человечески, как будто объясняешь новичку.

Ответь СТРОГО одним JSON-объектом:
{{"plain_explanation":"<2-4 предложения: почему такой вердикт>",
"main_risks":["<риск>", "..."],
"what_to_check_manually":["<что проверить в документации руками>", "..."],
"questions_to_customer":["<вопрос заказчику>", "..."],
"application_notes":["<на что обратить внимание при подготовке заявки>", "..."]}}
"""

_EXPLAIN_USER = """\
КАРТОЧКА РЕШЕНИЯ (посчитана кодом, НЕ меняй её):
Вердикт: {verdict_label} ({verdict})
Почему (черновая причина): {verdict_reason}
Уверенность: {confidence}

Гейты допуска:
{gates}

Смогу ли сделать: {can_do}

Финмодель (оценка {fin_quality}): {finance}

Зелёные флаги: {green}
Красные флаги: {red}

ДАННЫЕ ТЕНДЕРА:
Название: {title}
Заказчик: {customer}
НМЦК: {price}
Закон: {law_type}
Срок подачи: {deadline}

Фрагмент текста документации:
---
{text}
---
"""


def explain_for_novice(tender: dict, card: dict, page_text: str = "") -> Optional[dict]:
    """LLM-объяснение готовой карточки решения. Возвращает dict или None.

    Ключи результата: plain_explanation (str), main_risks/what_to_check_manually/
    questions_to_customer/application_notes (list[str]).
    """
    import config
    import llm_provider

    if not llm_provider.is_configured():
        return None

    def _fmt_gates(gates):
        out = []
        for g in gates or []:
            out.append(f"- [{g.get('status')}] {g.get('label')}: {g.get('explain')}")
        return "\n".join(out) or "—"

    def _fmt_can_do(cd):
        if not cd or cd.get("status") == "unknown":
            return "не определено"
        parts = [f"статус={cd.get('status')}"]
        if cd.get("score") is not None:
            parts.append(f"совпадение {cd.get('score')}%")
        if cd.get("matched"):
            parts.append("умеем: " + ", ".join(cd["matched"][:8]))
        if cd.get("gaps"):
            parts.append("не хватает: " + ", ".join(cd["gaps"][:8]))
        return "; ".join(parts)

    def _fmt_finance(f):
        if not f or f.get("nmck") is None:
            return "НМЦК не указана — расчёт невозможен"
        def m(v):
            return "не найдено" if v is None else f"{v:,.0f} ₽".replace(",", " ")
        return (f"ставка {m(f.get('recommended_bid'))}, налог {m(f.get('tax_usn'))}, "
                f"обеспеч.исполн. {m(f.get('contract_security'))}, "
                f"аванс {f.get('advance_pct') if f.get('advance_pct') is not None else 'не найден'}, "
                f"кассовый разрыв {m(f.get('cash_gap'))}, "
                f"маржа {m(f.get('net_margin'))} → {f.get('verdict')}")

    def money(value):
        return f"{value:,.0f} ₽".replace(",", " ") if value else "не указана"

    finance = card.get("finance") or {}
    user_message = _EXPLAIN_USER.format(
        verdict=card.get("verdict", "—"),
        verdict_label=card.get("verdict_label", "—"),
        verdict_reason=card.get("verdict_reason", "—"),
        confidence=card.get("confidence", "—"),
        gates=_fmt_gates(card.get("gates")),
        can_do=_fmt_can_do(card.get("can_do")),
        fin_quality=finance.get("quality", "—"),
        finance=_fmt_finance(finance),
        green="; ".join(card.get("green_flags") or []) or "—",
        red="; ".join(card.get("red_flags") or []) or "—",
        title=tender.get("title", "—"),
        customer=tender.get("customer", "—"),
        price=money(tender.get("price")),
        law_type=tender.get("law_type", "—"),
        deadline=tender.get("deadline", "—"),
        text=(page_text or "(текст не получен)")[: config.LLM_TEXT_CHARS],
    )

    raw = llm_provider.complete(
        system=_EXPLAIN_SYSTEM,
        user=user_message,
        model=llm_provider.deep_model(),
        max_tokens=1000,
        temperature=0.3,
        json_mode=True,
    )
    data = llm_provider.parse_json(raw)
    if not data:
        return None
    return _normalize_explain(data)


def _normalize_explain(data: dict) -> dict:
    def _as_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()][:6]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []
    return {
        "plain_explanation": str(data.get("plain_explanation", "")).strip()[:800],
        "main_risks": _as_list(data.get("main_risks")),
        "what_to_check_manually": _as_list(data.get("what_to_check_manually")),
        "questions_to_customer": _as_list(data.get("questions_to_customer")),
        "application_notes": _as_list(data.get("application_notes")),
    }


# ── MVP3: разбор критериев оценки и черновик запроса на разъяснение ───────────

_CRITERIA_SYSTEM = """\
Ты — тендерный аналитик. Тебе дают ФРАГМЕНТЫ документации о закупке (вокруг раздела
оценки заявок). Разбери критерии оценки для новичка.

СТРОГО:
- Не выдумывай критерии. Если в тексте нет критерия или его веса — ставь null/"unknown".
- Опирайся только на присланный текст.

Ответь СТРОГО одним JSON-объектом:
{"procedure_hint":"конкурс|аукцион|котировки|unknown",
"summary":"<простое объяснение для новичка, 1-3 предложения>",
"criteria":[{"label":"<критерий>","weight":<число процентов или null>,
"how_to_win":"<как набрать баллы>","what_to_prepare":"<что приложить к заявке>",
"source_snippet":"<короткая цитата из текста>"}],
"warnings":["<на что обратить внимание>"]}
"""

_CRITERIA_USER = """\
Фрагменты документации:
---
{chunks}
---
"""


def extract_criteria_llm(chunks) -> Optional[dict]:
    """LLM-разбор критериев оценки по фрагментам текста (не по всему документу)."""
    import config
    import llm_provider

    if not llm_provider.is_configured():
        return None

    if isinstance(chunks, (list, tuple)):
        text = "\n\n---\n\n".join(str(c) for c in chunks if c)
    else:
        text = str(chunks or "")
    if not text.strip():
        return None
    text = text[: config.LLM_TEXT_CHARS]

    raw = llm_provider.complete(
        system=_CRITERIA_SYSTEM,
        user=_CRITERIA_USER.format(chunks=text),
        model=llm_provider.deep_model(),
        max_tokens=1000,
        temperature=0.2,
        json_mode=True,
    )
    data = llm_provider.parse_json(raw)
    if not data:
        return None

    def _as_list(v):
        if isinstance(v, list):
            return v
        return [v] if v else []

    crits = []
    for c in _as_list(data.get("criteria")):
        if not isinstance(c, dict):
            continue
        w = c.get("weight")
        try:
            w = int(w) if w not in (None, "", "unknown", "null") else None
        except (TypeError, ValueError):
            w = None
        crits.append({
            "label": str(c.get("label", "")).strip()[:120],
            "weight": w,
            "how_to_win": str(c.get("how_to_win", "")).strip()[:300],
            "what_to_prepare": str(c.get("what_to_prepare", "")).strip()[:300],
            "source_snippet": str(c.get("source_snippet", "")).strip()[:300],
        })
    return {
        "procedure_hint": str(data.get("procedure_hint", "unknown")).strip()[:20] or "unknown",
        "summary": str(data.get("summary", "")).strip()[:600],
        "criteria": crits,
        "warnings": [str(x).strip() for x in _as_list(data.get("warnings")) if str(x).strip()][:6],
    }


_CLARIFY_SYSTEM = """\
Ты помогаешь составить официальный запрос на разъяснение положений документации о закупке
по 44-ФЗ/223-ФЗ. Тебе дают список вопросов. Оформи их в вежливый официальный запрос.

СТРОГО:
- НЕ добавляй новых вопросов сверх данных и не выдумывай факты.
- Пиши по-деловому, кратко. Пронумеруй вопросы.
- Верни только текст запроса (без markdown-разметки и пояснений).
"""

_CLARIFY_USER = """\
Закупка №{pnum}: {title}
Заказчик: {customer}

Вопросы для запроса:
{questions}
"""


def draft_clarification(tender: dict, questions: list[str],
                        card: dict | None = None, criteria: dict | None = None) -> Optional[str]:
    """LLM-полировка запроса на разъяснение из готового списка вопросов. None если LLM нет."""
    import llm_provider

    if not llm_provider.is_configured() or not questions:
        return None

    q_block = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    raw = llm_provider.complete(
        system=_CLARIFY_SYSTEM,
        user=_CLARIFY_USER.format(
            pnum=tender.get("purchase_number", "—"),
            title=tender.get("title", "—"),
            customer=tender.get("customer", "—"),
            questions=q_block,
        ),
        model=llm_provider.deep_model(),
        max_tokens=800,
        temperature=0.3,
    )
    return raw.strip() if raw else None


# ── MVP4b-1: разбор спецификации позиций по фрагментам ────────────────────────

_SPEC_SYSTEM = """\
Ты — тендерный аналитик. Тебе дают ФРАГМЕНТЫ документации о закупке (вокруг таблицы/раздела
спецификации товаров). Извлеки товарные позиции для поставки.

СТРОГО:
- Не выдумывай позиции и количества. Если количества нет — qty=null.
- Если это услуга/работы без товарных позиций — верни items=[] и заполни warning.
- Не превращай этапы работ в товары.

Ответь СТРОГО одним JSON-объектом:
{"items":[{"name":"<наименование>","qty":<число или null>,"unit":"<ед. изм или null>",
"requirements":"<характеристики из ТЗ>"}],"warning":"<если позиций нет/это услуга>"}
"""

_SPEC_USER = """\
Фрагменты документации:
---
{chunks}
---
"""


def extract_spec_llm(chunks) -> Optional[dict]:
    """LLM-разбор спецификации по фрагментам текста. Возвращает {items, warning} или None."""
    import config
    import llm_provider

    if not llm_provider.is_configured():
        return None

    if isinstance(chunks, (list, tuple)):
        text = "\n\n---\n\n".join(str(c) for c in chunks if c)
    else:
        text = str(chunks or "")
    if not text.strip():
        return None
    text = text[: config.LLM_TEXT_CHARS]

    raw = llm_provider.complete(
        system=_SPEC_SYSTEM,
        user=_SPEC_USER.format(chunks=text),
        model=llm_provider.deep_model(),
        max_tokens=1200,
        temperature=0.1,
        json_mode=True,
    )
    data = llm_provider.parse_json(raw)
    if not data:
        return None

    items = []
    for it in (data.get("items") or []):
        if not isinstance(it, dict) or not str(it.get("name", "")).strip():
            continue
        qty = it.get("qty")
        try:
            qty = float(qty) if qty not in (None, "", "null", "unknown") else None
        except (TypeError, ValueError):
            qty = None
        items.append({
            "name": str(it.get("name", "")).strip()[:200],
            "qty": qty,
            "unit": (str(it.get("unit", "")).strip()[:20] or None) if it.get("unit") else None,
            "requirements": str(it.get("requirements", "")).strip()[:500],
            "source": "llm",
        })
    return {"items": items, "warning": str(data.get("warning", "")).strip()[:300]}


def extract_verdict(analysis: str) -> str:
    if not analysis:
        return "—"
    for line in analysis.splitlines():
        if line.strip().upper().startswith("ВЕРДИКТ"):
            return line.split(":", 1)[-1].strip()
    return "—"
