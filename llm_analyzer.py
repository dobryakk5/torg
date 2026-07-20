"""llm_analyzer.py — анализ закупки через Claude API с контекстом из Базы Знаний."""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Базовый профиль (fallback если БЗ пустая) ────────────────────────────────
_BASE_PROFILE = """
Профиль исполнителя:
- 1С-Битрикс, Bitrix24, сайты, личные кабинеты, веб-разработка;
- миграции БД и ПО, интеграции: 1С, CRM, API, платежи; PHP/Python/JS, SQL/СУБД;
- серверное администрирование: Linux/Windows, VPS, backup;
- поставка с настройкой: ТСД, сканеры штрихкода, принтеры этикеток, сетевое оборудование;
- без лицензий ФСТЭК/ФСБ/СРО;
- целевая цена: 300 тыс. – 2 млн ₽;
- нежелательны: безлимитные заявки, 24/7, жесткий SLA, большое обеспечение.
"""

# Принципы отбора — главный критерий вердикта. Исполнитель активно работает с
# ИИ-ассистентами, поэтому «незнакомая технология» сама по себе — не блокер.
_SELECTION_PRINCIPLES = """
Принципы отбора (важно, определяют вердикт):
- Исполнитель работает с ИИ-ассистентами и берётся за ШИРОКИЙ спектр задач разработки:
  миграции БД/ПО, интеграции, веб-приложения, парсеры, скрипты, API — даже если
  конкретная технология не названа в профиле, лишь бы стек был мейнстримный/открытый
  (веб, PHP/Python/JS, SQL/любые СУБД, Linux, REST/SOAP) и работа шла удалённо.
- ПРИОРИТЕТ: разработка / доработка / миграция / интеграция / внедрение.
  Поддержка допустима как доля контракта при основной разработке.
- Чистая поддержка/сопровождение — брать только при мягком SLA: время реакции от
  1 рабочего дня и дольше, без 24/7, без безлимитных заявок, ставка часа достойная.
  Жёсткий SLA (реакция минуты/часы, 24/7, безлимит) при чистой поддержке → ПРОПУСТИТЬ.
- 1С: интеграция, обмен, миграция данных — да. ЧИСТАЯ разработка/доработка конфигураций
  1С — максимум ОСТОРОЖНО (ИИ слабее знает язык 1С).
- ПРОПУСТИТЬ ставь только при жёстких блокерах: лицензии ФСТЭК/ФСБ/СРО (в т.ч.
  лицензия ФСБ на криптографию/шифровальные средства); работа через защищённые
  сети и СКЗИ (ViPNet, КриптоПро, ГОСТ VPN) — требует лицензий и аттестованного
  контура; обязательное партнёрство или сертификация вендора; очные работы на
  территории заказчика (установка, обучение, поддержка «очно» — особенно в другом
  регионе); чистый перекуп лицензий/железа без работ; в проект контракта уже
  вписан конкретный исполнитель (закупка под него); жёсткий SLA при чистой
  поддержке (24×7, гарантированная доступность в %, решение инцидентов за часы).
- Незнакомая, но открытая технология — НЕ причина для ПРОПУСТИТЬ: ставь ОСТОРОЖНО
  и напиши, что уточнить у заказчика.
- ОСОБО проверяй ТЗ и проект договора на СКРЫТЫЙ вендор-лок: закупка формально
  открытая, но предмет — адаптация/сопровождение проприетарного ПО (напр. CompanyMedia,
  Directum, ЛОЦМАН, Парус), работать с которым фактически вправе только вендор,
  его официальные партнёры или организации с договором сопровождения от вендора.
  Без такого статуса заказчик может не принять результат работ. Это стоп-фактор —
  назови ПО и вендора явно. То же касается: доступ к исходникам/SDK только у вендора,
  требование «согласовать работы с правообладателем», доработка самописной системы
  заказчика без передачи исходников.
- Типовые стоп-факторы, которые в ТЗ прячутся в середине документа (перечисляй
  каждый найденный в СТОП-ФАКТОРАХ отдельной строкой):
  * лицензия ФСБ на криптографию / шифровальные средства — часто следует из
    требования работать в защищённой сети (ViPNet, СКЗИ, КриптоПро, ГОСТ-VPN,
    «защищённый канал связи», ведомственная сеть региона);
  * очная установка/обучение/поддержка, присутствие специалистов на площадке
    заказчика — работаем только удалённо;
  * жёсткий SLA: 24х7/круглосуточно, гарантированная доступность в процентах
    (напр. 99,7%), реакция немедленно / решение критичных за 1 час.
"""

_SYSTEM_TEMPLATE = """
Ты — опытный тендерный аналитик. Помогаешь ИТ-специалисту выбирать выгодные закупки по 44-ФЗ и 223-ФЗ.

{profile_block}
{selection_principles}
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

СТОП-ФАКТОРЫ:
(жёсткие блокеры и серьёзные сомнения ИЗ ДОКУМЕНТОВ: вендор-лок, обязательный статус
партнёра, лицензии ФСТЭК/ФСБ/СРО и криптография/СКЗИ/ViPNet, вписанный исполнитель,
очные работы у заказчика (укажи город, если назван), жёсткий SLA (приведи цифры:
24×7, доступность %, время решения). Каждый — одной строкой «- <суть>».
Если их нет — ровно одна строка «- нет»)
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
        selection_principles=_SELECTION_PRINCIPLES,
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
        # Запас под reasoning-модели (tencent/hy3 и т.п.): они тратят ~1000+ токенов
        # на размышление ДО ответа; сам JSON-вердикт занимает <150 токенов.
        max_tokens=2000,
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
EXPLAIN_PROMPT_VERSION = "v2"

_EXPLAIN_SYSTEM = """\
Ты — наставник для новичка в госзакупках (ИТ-подрядчик: 1С-Битрикс, сайты, интеграции,
серверы, поставка ИТ-оборудования с настройкой). Тебе дают УЖЕ ГОТОВУЮ карточку решения
по тендеру (вердикт, гейты допуска, финмодель, флаги), посчитанную кодом, и текст
документации закупки (если она была скачана).

ТВОЯ ЗАДАЧА — объяснить эту карточку простым языком. СТРОГО:
- НЕ пересчитывай числа (НМЦК, маржу, обеспечение) — бери их как есть из карточки.
- НЕ меняй вердикт (БЕРИ/ПОДУМАЙ/ПРОПУСТИ) — объясняй именно его.
- Опирайся ТОЛЬКО на данные карточки и текст тендера, не выдумывай фактов.
- Пиши коротко и по-человечески, как будто объясняешь новичку.
- По тексту документации определи, что РЕАЛЬНО требуется делать: писать код/дорабатывать
  (разработка), настраивать и внедрять готовое ПО (внедрение готового продукта), просто
  поставить лицензии/оборудование без работ (поставка/перекуп), или это смесь того и
  другого (смешанный). Если из текста не ясно — используй "unclear".

Ответь СТРОГО одним JSON-объектом:
{{"project_type":"разработка|внедрение готового продукта|поставка лицензий или железа (перекуп)|смешанный|unclear",
"project_type_reason":"<1-2 предложения: почему именно так, со ссылкой на текст>",
"stop_factors":["<конкретный стоп-фактор из документации или карточки>", "..."],
"plain_explanation":"<2-4 предложения: почему такой вердикт>",
"main_risks":["<риск>", "..."],
"what_to_check_manually":["<что проверить в документации руками>", "..."],
"questions_to_customer":["<вопрос заказчику>", "..."],
"application_notes":["<на что обратить внимание при подготовке заявки>", "..."]}}

Если стоп-факторов нет — верни "stop_factors": [].
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
    project_type = str(data.get("project_type", "")).strip() or "unclear"
    return {
        "project_type": project_type,
        "project_type_reason": str(data.get("project_type_reason", "")).strip()[:400],
        "stop_factors": _as_list(data.get("stop_factors")),
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


def extract_stop_factors(analysis: str) -> list[str]:
    """Достаёт из детального разбора буллеты секции «СТОП-ФАКТОРЫ».

    Возвращает список строк (без маркеров списка); пустой список — если секции
    нет или модель написала «нет». Используется для строки «⚠️ Стоп-факторы»
    в Telegram-уведомлении.
    """
    if not analysis:
        return []
    out: list[str] = []
    in_section = False
    for line in analysis.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("СТОП-ФАКТОР") or upper.startswith("СТОП ФАКТОР"):
            in_section = True
            # значение может идти прямо в строке заголовка: «СТОП-ФАКТОРЫ: нет»
            tail = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            if tail and tail.lower() not in ("нет", "нет.", "-", "—"):
                out.append(tail)
            continue
        if not in_section:
            continue
        if not stripped:
            continue
        # следующий ЗАГОЛОВОК секции (ВОПРОСЫ ЗАКАЗЧИКУ: / ИТОГ:) — конец секции
        bare = stripped.lstrip("-•* \t")
        if stripped == upper and not stripped.startswith(("-", "•", "*")) and len(stripped) > 3:
            break
        if not stripped.startswith(("-", "•", "*")):
            break
        val = bare.strip(" .")
        if val and val.lower() not in ("нет", "не выявлены", "не найдены", "..."):
            out.append(val)
    return out


def extract_verdict(analysis: str) -> str:
    if not analysis:
        return "—"
    for line in analysis.splitlines():
        if line.strip().upper().startswith("ВЕРДИКТ"):
            # Модель нередко копирует формат из шаблона со скобками:
            # «ВЕРДИКТ: [ПРОПУСТИТЬ]» — чистим скобки/точки/пробелы, чтобы
            # значение совпадало с ключами (СМОТРЕТЬ/ОСТОРОЖНО/ПРОПУСТИТЬ).
            raw = line.split(":", 1)[-1]
            return raw.strip().strip("[]").strip(" .").strip()
    return "—"


# ══════════════════════════════════════════════════════════════════════════════
# Поисковые профили (search_profiles.py) — ИИ-помощник для /profiles
# ══════════════════════════════════════════════════════════════════════════════

_PROFILE_PHRASE_RULES = """
Синтаксис фразы поискового профиля:
- "*" в конце слова — усечение (стемминг): "разработк*" матчит "разработка/разработку/…".
- "|" внутри слова — варианты через ИЛИ: "битрикс|bitrix".
- "(слово слово)~N" — фраза из нескольких слов, допускается N лишних слов между ними
  (без ~N окно по умолчанию = 1, т.е. одно слово может быть между).
- kind: "plus" (даёт баллы и уходит в поиск ЕИС), "minus_hard" (жёсткое исключение —
  тендер отбрасывается), "minus_soft" (мягкий штраф к баллу, тендер не отбрасывается).

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА ГИГИЕНЫ (нарушение даёт огромный мусор в выдаче):
1. НИКОГДА не предлагай голые фразы "разработк* программ*" (без уточнения) —
   ловит "программа газификации", "образовательная программа", "муниципальная программа".
   Разрешено ТОЛЬКО с уточнением: "разработк* программн* обеспечен*", "разработк* прикладн* программ*".
2. НИКОГДА не предлагай голые "модул*", "платформ*", "функционал*", "ИТ", "IT" без
   уточняющего слова рядом (например "программн* модул*", "цифров* платформ*" — можно,
   голое "модул*" — нельзя, ловит "модульное здание").
3. Опасные слова типа "строител*", "ремонт", "поставк*", "приобретени*", "сопровожден*"
   НИКОГДА не делай kind="minus_hard" — только "minus_soft" (мягкий штраф) или узкое
   сочетание с "~N", например "(капитальн* ремонт*)~1".
4. Не дублируй фразы, которые уже есть в списке ниже.
"""

_SUGGEST_SYSTEM = """
Ты помогаешь настраивать поисковый профиль тендерного монитора для ИТ-подрядчика.
Профиль — это набор плюс/минус-фраз, по которым система ищет закупки в ЕИС и
оценивает найденные карточки локально (без похода в интернет).
""" + _PROFILE_PHRASE_RULES + """
Пользователь описал, какие услуги он хочет ловить этим профилем. Предложи 8-20
новых фраз (plus и, если уместно, minus_soft/minus_hard), которых ещё нет в
существующем списке. Для plus-фраз обязательно укажи query_text — чистую
словоформу без масок для отправки в поиск ЕИС (например для "разработк* сайт*"
query_text = "разработка сайта").

Ответ СТРОГО в формате JSON:
{"phrases": [
  {"kind": "plus", "phrase": "разработк* сайт*", "query_text": "разработка сайта",
   "weight": 3, "note": "почему подходит"},
  {"kind": "minus_soft", "phrase": "поставк*", "weight": 3, "note": "почему может быть шумом"}
]}
"""

_SUGGEST_USER = """
Описание услуг от пользователя:
{description}

Уже есть в профиле (не дублировать):
{existing}
"""


def suggest_profile_phrases(description: str, existing_phrases: list[str] | None = None) -> Optional[list[dict]]:
    """ИИ-черновик плюс/минус-фраз по описанию услуг пользователя.

    Возвращает список dict {kind, phrase, weight, query_text?, note} или None
    при недоступности LLM. Черновик НИКОГДА не применяется автоматически —
    только после ручного подтверждения в UI (см. /profiles).
    """
    import llm_provider

    if not llm_provider.is_configured() or not str(description or "").strip():
        return None

    existing = "\n".join(f"- {p}" for p in (existing_phrases or [])[:80]) or "(пока пусто)"
    raw = llm_provider.complete(
        system=_SUGGEST_SYSTEM,
        user=_SUGGEST_USER.format(description=str(description)[:2000], existing=existing),
        model=llm_provider.triage_model(),
        max_tokens=2000,
        temperature=0.3,
        json_mode=True,
    )
    data = llm_provider.parse_json(raw)
    if not data:
        return None
    return _normalize_suggested_phrases(data.get("phrases"))


def _normalize_suggested_phrases(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in {"plus", "minus_hard", "minus_soft"}:
            continue
        phrase = str(item.get("phrase", "")).strip()
        if not phrase:
            continue
        try:
            weight = int(item.get("weight", 3))
        except (TypeError, ValueError):
            weight = 3
        out.append({
            "kind": kind,
            "phrase": phrase[:200],
            "weight": max(1, min(10, weight)),
            "query_text": (str(item.get("query_text", "")).strip()[:200] or None) if item.get("query_text") else None,
            "note": str(item.get("note", "")).strip()[:300],
        })
    return out[:30]


_REVIEW_SYSTEM = """
Ты проверяешь фразы поискового профиля тендерного монитора на риск ложных
срабатываний (мусора в выдаче) и пропуска полезных закупок.
""" + _PROFILE_PHRASE_RULES + """
Для каждой фразы из списка оцени риск: "high" (почти наверняка даст много
мусора или отбросит полезное — особенно для kind="minus_hard"), "medium"
(может быть шумной в отдельных случаях), "low" (безопасна). Для high/medium
обязательно приведи конкретный пример ложного срабатывания.

Ответ СТРОГО в формате JSON:
{"reviews": [
  {"phrase": "разработк* программ*", "risk": "high",
   "example": "программа газификации, образовательная программа", "note": "уточни: программн* обеспечен*"}
]}
Фразы с risk="low" можно не включать в ответ вовсе.
"""

_REVIEW_USER = """
Фразы профиля "{profile_name}" (kind: фраза):
{phrases}
"""


def review_profile_phrases(profile_name: str, phrases: list[dict]) -> Optional[list[dict]]:
    """ИИ-проверка фраз профиля на риск ложных срабатываний.

    Возвращает список dict {phrase, risk, example, note} только для рискованных
    фраз (low не включаются), или None при недоступности LLM.
    """
    import llm_provider

    if not llm_provider.is_configured() or not phrases:
        return None

    lines = "\n".join(
        f"- ({p.get('kind', 'plus')}) {p.get('phrase', '')}" for p in phrases[:80] if p.get("phrase")
    )
    if not lines:
        return None

    raw = llm_provider.complete(
        system=_REVIEW_SYSTEM,
        user=_REVIEW_USER.format(profile_name=profile_name or "—", phrases=lines),
        model=llm_provider.triage_model(),
        max_tokens=2000,
        temperature=0.0,
        json_mode=True,
    )
    data = llm_provider.parse_json(raw)
    if not data:
        return None
    return _normalize_phrase_reviews(data.get("reviews"))


def _normalize_phrase_reviews(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        phrase = str(item.get("phrase", "")).strip()
        if not phrase:
            continue
        risk = str(item.get("risk", "")).strip().lower()
        if risk not in {"high", "medium", "low"}:
            risk = "medium"
        out.append({
            "phrase": phrase[:200],
            "risk": risk,
            "example": str(item.get("example", "")).strip()[:300],
            "note": str(item.get("note", "")).strip()[:300],
        })
    return out[:80]
