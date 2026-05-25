"""notifier.py — отправка уведомлений и кнопок решений в Telegram."""

from __future__ import annotations

import logging
from typing import Optional

import requests

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
        reply_markup=decision_keyboard(tender.get("purchase_number", ""), tender.get("url", "")),
    )


def decision_keyboard(purchase_number: str, url: str = "") -> dict:
    p = purchase_number[-16:] if len(purchase_number) > 16 else purchase_number
    # callback_data ограничен 64 байтами, поэтому берём номер закупки, обычно он и так помещается.
    key = purchase_number[:40]
    buttons = [
        [
            {"text": "👍 интересно", "callback_data": f"dec:interesting:{key}"},
            {"text": "👎 мусор", "callback_data": f"dec:rejected:{key}"},
        ],
        [
            {"text": "⚠️ под своего", "callback_data": f"dec:tailored:{key}"},
            {"text": "💰 посчитать", "callback_data": f"dec:need_calc:{key}"},
        ],
        [
            {"text": "✅ подаемся", "callback_data": f"dec:applying:{key}"},
            {"text": f"№ {p}", "callback_data": f"dec:noop:{key}"},
        ],
    ]
    if url:
        buttons.append([{"text": "🔗 открыть", "url": url}])
    return {"inline_keyboard": buttons}


def format_tender_message(tender: dict, score: int, score_reasons: list[str], llm_analysis: Optional[str] = None) -> str:
    from scorer import format_score_summary

    def money(value):
        return f"{value:,.0f} ₽".replace(",", " ") if value else "не найдено"

    price_str = money(tender.get("price")) if tender.get("price") else "не указана"
    decision = tender.get("filter_decision") or ("GO" if score >= 32 else ("CAUTION" if score >= 24 else "NO-GO"))
    emoji_score = "🟢" if decision == "GO" else ("🟡" if decision == "CAUTION" else "🔴")

    lines = [
        f"🔍 <b>НОВАЯ ЗАКУПКА</b>  {emoji_score} скор: {score}/40 · {decision}",
        "─" * 30,
        f"📌 <b>{_esc(str(tender.get('title', '—'))[:220])}</b>",
        f"🏛 {_esc(str(tender.get('customer', '—'))[:160])}",
        f"💰 {price_str} · {_esc(tender.get('law_type', '—'))}",
    ]

    if tender.get("deadline"):
        lines.append(f"📅 Подача до: {_esc(str(tender.get('deadline')))}")
    if tender.get("matched_keywords"):
        keywords = tender.get("matched_keywords")
        if isinstance(keywords, list):
            keywords = ", ".join(keywords[:8])
        lines.append(f"🔎 Ключи: {_esc(str(keywords)[:220])}")

    app_sec = tender.get("application_security_amount")
    contract_sec = tender.get("contract_security_amount")
    if app_sec or contract_sec:
        lines.append("─" * 30)
        lines.append("💳 <b>Финансовый вход</b>")
        if app_sec:
            lines.append(f"- заявка: {money(app_sec)}")
        if contract_sec:
            lines.append(f"- исполнение: {money(contract_sec)}")

    if tender.get("payment_terms"):
        lines.append(f"🧾 Оплата: {_esc(str(tender.get('payment_terms'))[:260])}")

    # Если пришли 8 фильтров — показываем краткую раскладку.
    filter_scores = tender.get("filter_scores") or []
    if filter_scores:
        lines.append("─" * 30)
        lines.append("🧮 <b>8 фильтров</b>")
        for f in filter_scores[:8]:
            fname = str(f.get("filter_name", ""))[:28]
            fscore = f.get("score", "—")
            signals = str(f.get("signals", ""))
            first_signal = signals.split("|")[0].strip() if signals else ""
            mark = "⛔" if f.get("stop_factor") else "•"
            lines.append(f"{mark} Ф{f.get('filter_number')}: {fscore}/5 {_esc(fname)}")
            if first_signal:
                lines.append(f"  {_esc(first_signal[:120])}")
    else:
        summary = format_score_summary(score, score_reasons)
        if not summary and score_reasons:
            summary = "\n".join(score_reasons[:8])
        if summary:
            lines.append("─" * 30)
            lines.append(_esc(summary))

    if tender.get("filter_stop"):
        lines.append("─" * 30)
        lines.append("⛔ <b>Стоп-факторы</b>")
        for item in str(tender.get("filter_stop", "")).split("|")[:5]:
            if item.strip():
                lines.append(f"- {_esc(item.strip()[:160])}")

    if llm_analysis:
        from llm_analyzer import extract_verdict
        verdict = extract_verdict(llm_analysis)
        verdict_emoji = {
            "СМОТРЕТЬ": "🟢",
            "ОСТОРОЖНО": "🟡",
            "ПРОПУСТИТЬ": "🔴",
        }.get(verdict.upper(), "⚪")
        lines.append("─" * 30)
        lines.append(f"🤖 <b>Claude: {verdict_emoji} {_esc(verdict)}</b>")
        analysis_lines = [
            line.strip() for line in llm_analysis.splitlines()
            if line.strip() and not line.strip().upper().startswith("ВЕРДИКТ")
        ][:12]
        for line in analysis_lines:
            lines.append(f"  {_esc(line)}")

    if tender.get("url"):
        lines.append("─" * 30)
        lines.append(f'🔗 <a href="{_esc(tender["url"])}">Открыть на ЕИС</a>')

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
