"""
telegram_decisions.py — обработчик Telegram-кнопок и команд.

Запуск отдельно:
  python telegram_decisions.py

Он читает callback-кнопки и обновляет decision/status в PostgreSQL.
"""

from __future__ import annotations

import logging
import time

from tls_bootstrap import NATIVE_TRUSTSTORE_ACTIVE

import requests

import config
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# deleteMessages принимает не более 100 ID за вызов. Ограничение защищает от
# случайного прохода по огромной сквозной нумерации сообщений; для личного чата
# 10 000 сообщений с большим запасом покрывают доступное Telegram окно в 48 ч.
CLEAR_BATCH_SIZE = 100
CLEAR_MAX_MESSAGES = 10_000

DECISION_LABELS = {
    "interesting": "👍 в работу",
    "rejected": "👎 не профиль",
    "declined": "🤷 пас (профиль ок, не участвую)",
    "hidden": "🙈 скрыто",
    # старые метки — для обратной совместимости со ранее отправленными кнопками
    "tailored": "под своего",
    "need_calc": "надо посчитать",
    "applying": "подаемся",
    "noop": "без изменения",
}


def main() -> None:
    db.init_db()
    token = config.TELEGRAM_BOT_TOKEN
    if not token or token == "YOUR_BOT_TOKEN":
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан")

    register_commands(token)
    offset = None
    logger.info("Слушаю Telegram callbacks и команды...")
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=40)
            resp.raise_for_status()
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    handle_message(token, message)
                    continue
                callback = update.get("callback_query")
                if not callback:
                    continue
                handle_callback(token, callback)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.warning("Ошибка polling: %s", exc)
            time.sleep(5)


def register_commands(token: str) -> None:
    """Добавляет /clear в меню команд Telegram."""
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/setMyCommands",
            json={
                "commands": [
                    {"command": "clear", "description": "Очистить доступную историю чата"},
                ]
            },
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning("Telegram setMyCommands: %s %s", response.status_code, response.text[:200])
    except requests.RequestException as exc:
        logger.warning("Не удалось обновить меню Telegram-команд: %s", exc)


def handle_message(token: str, message: dict) -> None:
    """Обрабатывает служебные команды бота."""
    text = str(message.get("text") or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
    if command != "/clear":
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    configured_chat_id = str(config.TELEGRAM_CHAT_ID or "").strip()

    # Команда разрушительная: принимаем её только из чата, указанного в .env.
    if chat_id is None or str(chat_id) != configured_chat_id:
        logger.warning("Игнорирую /clear из неразрешённого чата %s", chat_id)
        return

    if chat.get("type") != "private":
        _send_plain(
            token,
            chat_id,
            "Команда /clear доступна только в личном чате с ботом.",
        )
        return

    if not isinstance(message_id, int) or message_id < 1:
        logger.warning("Не могу выполнить /clear: нет message_id")
        return

    _clear_private_chat(token, chat_id, message_id)


def _send_plain(token: str, chat_id: int | str, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Не удалось ответить на Telegram-команду: %s", exc)


def _clear_private_chat(token: str, chat_id: int | str, newest_message_id: int) -> None:
    """Удаляет доступную боту историю личного чата пакетами по 100 ID."""
    oldest_message_id = max(1, newest_message_id - CLEAR_MAX_MESSAGES + 1)
    attempted = 0

    for batch_end in range(newest_message_id, oldest_message_id - 1, -CLEAR_BATCH_SIZE):
        batch_start = max(oldest_message_id, batch_end - CLEAR_BATCH_SIZE + 1)
        message_ids = list(range(batch_start, batch_end + 1))
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/deleteMessages",
                json={"chat_id": chat_id, "message_ids": message_ids},
                timeout=15,
            )
            if response.status_code == 429:
                retry_after = min(5, int((response.json().get("parameters") or {}).get("retry_after", 1)))
                time.sleep(max(1, retry_after))
                response = requests.post(
                    f"https://api.telegram.org/bot{token}/deleteMessages",
                    json={"chat_id": chat_id, "message_ids": message_ids},
                    timeout=15,
                )
            if response.status_code != 200:
                logger.warning(
                    "Telegram не удалил пакет %d–%d: %s %s",
                    batch_start,
                    batch_end,
                    response.status_code,
                    response.text[:200],
                )
            attempted += len(message_ids)
        except requests.RequestException as exc:
            logger.warning("/clear: ошибка удаления пакета %d–%d: %s", batch_start, batch_end, exc)

    logger.info("/clear: обработано %d message_id в личном чате %s", attempted, chat_id)


def handle_callback(token: str, callback: dict) -> None:
    data = callback.get("data", "")
    cb_id = callback.get("id")
    if not data.startswith("dec:"):
        return
    _, decision, purchase_number = data.split(":", 2)
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")

    if decision == "noop":
        text = "Это номер закупки, решение не изменено"
    else:
        db.set_decision(purchase_number, decision)
        label = DECISION_LABELS.get(decision, decision)
        text = f"Записал: {label}"
        logger.info("%s → %s", purchase_number, decision)

    # Подтверждаем нажатие (всплывающая подсказка у кнопки).
    requests.post(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        json={"callback_query_id": cb_id, "text": text, "show_alert": False},
        timeout=10,
    )

    # Любое решение (кроме noop) убирает карточку из ленты целиком —
    # решение уже записано в БД, обратная связь дана во всплывашке выше.
    if decision != "noop" and chat_id is not None and message_id is not None:
        requests.post(
            f"https://api.telegram.org/bot{token}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10,
        )


if __name__ == "__main__":
    main()
