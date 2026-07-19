"""notifier.py — отправка уведомлений и кнопок решений в Telegram."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import requests

import config

logger = logging.getLogger(__name__)


def send_message(text: str, bot_token: str, chat_id: str, parse_mode: str = "HTML", reply_markup: dict | None = None) -> bool:
    if not bot_token or not chat_id or bot_token == "YOUR_BOT_TOKEN" or chat_id == "YOUR_CHAT_ID":
        logger.warning("Telegram не настроен (нет токена или chat_id)")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        logger.error("Telegram ответил %s: %s", resp.status_code, resp.text[:300])
        return False
    except requests.RequestException as e:
        logger.error("Ошибка отправки в Telegram: %s", e)
        return False


def send_tender_message(tender: dict, score: int, score_reasons: list[str], llm_analysis: Optional[str], bot_token: str, chat_id: str) -> bool:
    return send_message(
        format_tender_message(tender, score, score_reasons, llm_analysis),
        bot_token,
        chat_id,
        reply_markup=decision_keyboard(tender.get("purchase_number", "")),
    )


def decision_keyboard(purchase_number: str) -> dict:
    """Компактная клавиатура под уведомлением: лайк / дизлайк / скрыть + «Подробности».

    callback_data ограничен 64 байтами: префикс «dec:interesting:» = 16 символов,
    поэтому номер закупки режем до 44 (реальные номера ЕИС/ЕАТ короче).
    """
    key = purchase_number[:44]
    # Два вида «нет» — сырьё для будущих эвристик: «не профиль» = система ошиблась
    # с отбором, «пас» = отбор верный, просто решили не участвовать.
    buttons = [
        [
            {"text": "👍 в работу", "callback_data": f"dec:interesting:{key}"},
            {"text": "👎 не профиль", "callback_data": f"dec:rejected:{key}"},
        ],
        [
            {"text": "🤷 пас (профиль ок)", "callback_data": f"dec:declined:{key}"},
            {"text": "🙈 скрыть", "callback_data": f"dec:hidden:{key}"},
        ],
    ]
    if purchase_number:
        # Ведёт на карточку тендера в веб-панели, а не на площадку-источник.
        details_url = f"{config.WEB_APP_URL}/tender/{quote(purchase_number, safe='')}"
        buttons.append([{"text": "🔗 Подробности", "url": details_url}])
    return {"inline_keyboard": buttons}


# Метки для секции «ОБЪЕМ → что надо сделать» из LLM-разбора.
_TODO_MARKERS = ("что надо сделать", "что нужно сделать", "что требуется")


def _extract_todo(llm_analysis: Optional[str]) -> str:
    """Достаёт из LLM-разбора короткую суть «что надо сделать» (секция ОБЪЕМ)."""
    if not llm_analysis:
        return ""
    lines = llm_analysis.splitlines()
    for i, ln in enumerate(lines):
        low = ln.lower().lstrip("-• \t")
        if any(low.startswith(m) for m in _TODO_MARKERS):
            val = ln.split(":", 1)[1].strip() if ":" in ln else ""
            if not val:  # значение развёрнуто буллетами на следующих строках
                collected = []
                for nxt in lines[i + 1 : i + 4]:
                    t = nxt.strip("-• \t")
                    if not t or ":" in t[:24] or t.isupper():
                        break
                    collected.append(t)
                val = "; ".join(collected)
            return val.strip()
    return ""


def _deadline_urgency(deadline: str) -> str:
    """«через 2 дня» / «завтра» / «сегодня!» из строки дедлайна для срочности."""
    from datetime import datetime

    s = str(deadline or "").strip()
    dt = None
    for fmt, cut in (("%d.%m.%Y %H:%M", 16), ("%d.%m.%Y", 10), ("%Y-%m-%dT%H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            dt = datetime.strptime(s[:cut], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return ""
    days = (dt.date() - datetime.now().date()).days
    if days < 0:
        return "просрочен"
    if days == 0:
        return "сегодня!"
    if days == 1:
        return "завтра"
    if days <= 6:
        return f"через {days} дн."
    return ""


def format_tender_message(tender: dict, score: int, score_reasons: list[str], llm_analysis: Optional[str] = None) -> str:
    """Краткое уведомление: суть, дедлайн, НМЦК, что сделать, вердикт Claude.

    Подробная раскладка по 8 фильтрам намеренно убрана — она есть в веб-панели,
    а в Telegram нужен быстрый триаж «брать / не брать» одним взглядом.
    """
    def money(value):
        return f"{value:,.0f} ₽".replace(",", " ") if value else "не указана"

    decision = tender.get("filter_decision") or ("GO" if score >= 32 else ("CAUTION" if score >= 24 else "NO-GO"))
    emoji_score = "🟢" if decision == "GO" else ("🟡" if decision == "CAUTION" else "🔴")

    lines = [
        f"{emoji_score} <b>{_esc(str(tender.get('title', '—'))[:180])}</b>",
        f"🏛 {_esc(str(tender.get('customer', '—'))[:120])}",
    ]

    deadline = str(tender.get("deadline") or "").strip()
    if deadline:
        urg = _deadline_urgency(deadline)
        lines.append(f"📅 до {_esc(deadline[:16])}" + (f"  ·  ⏳ {urg}" if urg else ""))

    lines.append(f"💰 {money(tender.get('price'))} · {_esc(str(tender.get('law_type', '—')))}")

    app_sec = tender.get("application_security_amount")
    contract_sec = tender.get("contract_security_amount")
    fin = []
    if app_sec:
        fin.append(f"заявка {money(app_sec)}")
    if contract_sec:
        fin.append(f"исполнение {money(contract_sec)}")
    if fin:
        lines.append("💳 обеспечение: " + " · ".join(fin))

    todo = _extract_todo(llm_analysis) or str(tender.get("title", "") or "")
    if todo:
        lines.append(f"🛠 <b>Что нужно:</b> {_esc(todo[:220])}")

    if llm_analysis:
        from llm_analyzer import extract_verdict

        verdict = extract_verdict(llm_analysis)
        verdict_emoji = {
            "СМОТРЕТЬ": "🟢",
            "ОСТОРОЖНО": "🟡",
            "ПРОПУСТИТЬ": "🔴",
        }.get(verdict.upper(), "⚪")
        lines.append(f"🤖 Claude: {verdict_emoji} {_esc(verdict)}")

    return "\n".join(lines)


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_startup_message(bot_token: str, chat_id: str, keywords: list[str]) -> None:
    msg = (
        "🚀 <b>Тендерный монитор запущен</b>\n\n"
        f"Слежу за {len(keywords)} ключевыми словами.\n"
        "Команды/кнопки решений обрабатываются через <code>python telegram_decisions.py</code>."
    )
    send_message(msg, bot_token, chat_id)


def send_summary(bot_token: str, chat_id: str, found: int, notified: int, keyword_count: int) -> None:
    if notified == 0:
        return
    msg = f"📊 Прогон завершён\nЗапросов: {keyword_count} · Новых: {found} · Отправлено: {notified}"
    send_message(msg, bot_token, chat_id)
