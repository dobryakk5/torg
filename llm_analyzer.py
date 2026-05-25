"""llm_analyzer.py — анализ закупки через Claude API."""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
Ты — опытный тендерный аналитик. Помогаешь ИТ-специалисту Павлу выбирать выгодные закупки по 44-ФЗ и 223-ФЗ.

Профиль исполнителя:
- 1С-Битрикс, Bitrix24, сайты, личные кабинеты;
- интеграции: 1С, CRM, API, платежи;
- серверное администрирование: Linux/Windows, VPS, backup;
- поставка с настройкой: ТСД, сканеры штрихкода, принтеры этикеток, сетевое оборудование;
- без лицензий ФСТЭК/ФСБ/СРО;
- целевая цена: 300 тыс. – 2 млн ₽;
- нежелательны безлимитные заявки, 24/7, жесткий SLA, большой вход по обеспечению.

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


def analyze_tender(tender: dict, page_text: str = "") -> Optional[str]:
    import config

    api_key = config.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.info("ANTHROPIC_API_KEY не задан — LLM-анализ пропущен")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.error("Установи: pip install anthropic")
        return None

    def money(value):
        return f"{value:,.0f} ₽".replace(",", " ") if value else "не найдено"

    user_message = USER_PROMPT_TEMPLATE.format(
        title=tender.get("title", "—"),
        customer=tender.get("customer", "—"),
        price=money(tender.get("price")),
        law_type=tender.get("law_type", "—"),
        deadline=tender.get("deadline", "—"),
        application_security=money(tender.get("application_security_amount")),
        contract_security=money(tender.get("contract_security_amount")),
        url=tender.get("url", "—"),
        page_text=(page_text or "(текст не получен)")[: config.LLM_TEXT_CHARS],
    )

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1100,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        result = response.content[0].text.strip()
        logger.info("LLM-анализ получен для '%s'", tender.get("title", "?")[:60])
        return result
    except Exception as e:
        logger.error("Ошибка Claude API: %s", e)
        return None


def extract_verdict(analysis: str) -> str:
    if not analysis:
        return "—"
    for line in analysis.splitlines():
        if line.strip().upper().startswith("ВЕРДИКТ"):
            return line.split(":", 1)[-1].strip()
    return "—"
