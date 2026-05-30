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


def extract_verdict(analysis: str) -> str:
    if not analysis:
        return "—"
    for line in analysis.splitlines():
        if line.strip().upper().startswith("ВЕРДИКТ"):
            return line.split(":", 1)[-1].strip()
    return "—"
