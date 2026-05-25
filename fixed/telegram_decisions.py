"""
telegram_decisions.py — обработчик Telegram-кнопок.

Запуск отдельно:
  python telegram_decisions.py

Он читает callback-кнопки и обновляет decision/status в PostgreSQL.
"""

from __future__ import annotations

import logging
import time

import requests

import config
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DECISION_LABELS = {
    "interesting": "интересно",
    "rejected": "мусор",
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

    offset = None
    logger.info("Слушаю Telegram callbacks...")
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
                callback = update.get("callback_query")
                if not callback:
                    continue
                handle_callback(token, callback)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.warning("Ошибка polling: %s", exc)
            time.sleep(5)


def handle_callback(token: str, callback: dict) -> None:
    data = callback.get("data", "")
    cb_id = callback.get("id")
    if not data.startswith("dec:"):
        return
    _, decision, purchase_number = data.split(":", 2)
    if decision != "noop":
        db.set_decision(purchase_number, decision)
        label = DECISION_LABELS.get(decision, decision)
        text = f"Записал решение: {label}"
        logger.info("%s → %s", purchase_number, decision)
    else:
        text = "Это номер закупки, решение не изменено"

    requests.post(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        json={"callback_query_id": cb_id, "text": text, "show_alert": False},
        timeout=10,
    )


if __name__ == "__main__":
    main()
