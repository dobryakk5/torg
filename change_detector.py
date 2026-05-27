"""
change_detector.py — детектор изменений документации активных тендеров.

Заказчики часто правят ТЗ после публикации — сужают требования под своего,
меняют сроки, добавляют уникальные характеристики. Без мониторинга ты об этом
узнаёшь только если сам перечитаешь.

Что делает:
  1. Каждые N часов берёт активные тендеры с высоким скором
  2. Скачивает актуальный хеш документов
  3. Если хеш изменился — уведомляет в Telegram и перезапускает Stage2

Запуск:
    python change_detector.py --once     # один прогон
    python change_detector.py            # daemon (каждые CHANGE_CHECK_HOURS часов)
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config
import database as db
from notifier import send_message

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Сколько часов между проверками (из .env или дефолт 6)
CHECK_HOURS = int(getattr(config, "CHANGE_CHECK_HOURS", 6))
# Минимальный скор тендера для мониторинга изменений
MIN_SCORE   = int(getattr(config, "CHANGE_MIN_SCORE", 20))


# ══════════════════════════════════════════════════════════════════════════════
# Хеширование текущего состояния страницы
# ══════════════════════════════════════════════════════════════════════════════

def _get_page(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        logger.debug("Не удалось получить страницу %s: %s", url, e)
    return None


def _extract_stable_content(html: str) -> str:
    """
    Извлекаем контент, который меняется при изменении ТЗ:
      - Список прикреплённых документов (имена файлов + размеры)
      - Основной текст описания объекта закупки

    Намеренно исключаем: счётчики заявок, дату обновления,
    рекламные блоки — они меняются постоянно и не говорят о важных правках.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Убираем шум
    for tag in soup.select("script, style, .counter, .views, .pagination, nav, footer, header"):
        tag.decompose()

    parts: list[str] = []

    # 1. Список документов (имена + размеры файлов)
    for el in soup.select(
        "span.cardMainInfo__fileName, "
        "a.expandedBlockFileList__fileName, "
        "td.fileNameColumn, "
        "div.attachDocumentsForm__docTitle"
    ):
        parts.append(el.get_text(strip=True))

    # 2. Описание объекта закупки
    for el in soup.select(
        "div.cardMainInfo__content, "
        "div.blockInfoCard__content, "
        "section.purchaseNotice__main, "
        "div.noticeMainInfoBlock"
    ):
        parts.append(el.get_text(" ", strip=True)[:3000])

    # 3. Даты и суммы обеспечений
    for el in soup.select("div.date-block, div.cardMainInfo__section--deadline"):
        parts.append(el.get_text(strip=True))

    content = "\n".join(parts)
    if not content.strip():
        # Fallback: весь основной текст
        main = soup.select_one("main, #content, body")
        content = (main or soup).get_text(" ", strip=True)[:8000]

    return content


def compute_hash(url: str) -> Optional[str]:
    """Скачивает страницу и возвращает MD5-хеш стабильного контента."""
    html = _get_page(url)
    if not html:
        return None
    content = _extract_stable_content(html)
    return hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# БД — вспомогательные запросы
# ══════════════════════════════════════════════════════════════════════════════

def get_monitored_tenders(limit: int = 100) -> list[dict]:
    """
    Активные тендеры с высоким скором и незакончившимся дедлайном.
    Только те, у кого уже есть сохранённый хеш ИЛИ те, кто проходил Stage2.
    """
    import psycopg2.extras
    with db._conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT purchase_number, title, url, documents_hash,
                   filter_total, customer, customer_inn, deadline
            FROM tenders
            WHERE status NOT IN ('result_checked', 'cancelled', 'rejected')
              AND filter_total >= %(min_score)s
              AND url IS NOT NULL AND url <> ''
              AND (
                    detail_checked_at IS NOT NULL   -- уже прошёл Stage2
                    OR documents_hash IS NOT NULL   -- или уже мониторится
              )
            ORDER BY filter_total DESC
            LIMIT %(limit)s
        """, {"min_score": MIN_SCORE, "limit": limit})
        return [dict(r) for r in cur.fetchall()]


def _save_hash(purchase_number: str, new_hash: str) -> None:
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenders SET documents_hash = %s, updated_at = NOW() WHERE purchase_number = %s",
            (new_hash, purchase_number),
        )


def _mark_needs_refresh(purchase_number: str) -> None:
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenders SET needs_detail_refresh = TRUE, updated_at = NOW() WHERE purchase_number = %s",
            (purchase_number,),
        )


def _log_change(purchase_number: str, old_hash: str, new_hash: str) -> None:
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tender_changes (purchase_number, change_type, old_value, new_value)
            VALUES (%s, 'documents_hash', %s, %s)
            """,
            (purchase_number, old_hash, new_hash),
        )


# ══════════════════════════════════════════════════════════════════════════════
# Telegram уведомление
# ══════════════════════════════════════════════════════════════════════════════

def _notify_change(tender: dict) -> None:
    title    = (tender.get("title") or "—")[:120]
    customer = (tender.get("customer") or "—")[:80]
    url      = tender.get("url", "")
    score    = tender.get("filter_total", 0)

    msg = (
        f"📝 <b>ТЗ ИЗМЕНИЛОСЬ</b>\n\n"
        f"📌 {title}\n"
        f"🏛 {customer}\n"
        f"🔢 Скор: {score}\n\n"
        f"<i>Документация обновлена — нужно перечитать ТЗ.</i>\n"
        f"Stage2 будет перезапущен автоматически.\n\n"
        f'🔗 <a href="{url}">Открыть на ЕИС</a>'
    )
    send_message(msg, config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)


# ══════════════════════════════════════════════════════════════════════════════
# Основной цикл
# ══════════════════════════════════════════════════════════════════════════════

def check_once() -> tuple[int, int]:
    """
    Один прогон проверки.
    Возвращает (проверено, изменений).
    """
    tenders   = get_monitored_tenders()
    checked   = 0
    changed   = 0

    logger.info("Детектор изменений: проверяю %d тендеров (скор≥%d)", len(tenders), MIN_SCORE)

    for t in tenders:
        pnum     = t["purchase_number"]
        url      = t["url"]
        old_hash = t.get("documents_hash")

        try:
            new_hash = compute_hash(url)
        except Exception as e:
            logger.warning("Ошибка хеширования %s: %s", pnum, e)
            continue

        if not new_hash:
            continue

        checked += 1

        if not old_hash:
            # Первое сохранение хеша — просто запоминаем, без уведомления
            _save_hash(pnum, new_hash)
            logger.debug("Хеш инициализирован для %s", pnum)
        elif old_hash != new_hash:
            changed += 1
            logger.info("ИЗМЕНЕНИЕ ОБНАРУЖЕНО: %s (%s)", pnum, t.get("title", "")[:60])
            _log_change(pnum, old_hash, new_hash)
            _save_hash(pnum, new_hash)
            _mark_needs_refresh(pnum)
            _notify_change(t)

        time.sleep(getattr(config, "REQUEST_DELAY", 3.0) * 0.5)   # чуть быстрее чем обычный поиск

    logger.info(
        "Детектор завершён: проверено %d, изменений %d",
        checked, changed,
    )
    return checked, changed


def run_daemon() -> None:
    """Запускает периодическую проверку по расписанию."""
    import schedule

    logger.info("Детектор изменений запущен: каждые %d ч.", CHECK_HOURS)
    check_once()
    schedule.every(CHECK_HOURS).hours.do(check_once)
    while True:
        schedule.run_pending()
        time.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    db.init_db()

    ap = argparse.ArgumentParser()
    ap.add_argument("--once",   action="store_true", help="Один прогон и выход")
    ap.add_argument("--daemon", action="store_true", help="Запустить как демон")
    args = ap.parse_args()

    if args.once or not args.daemon:
        checked, changed = check_once()
        print(f"Проверено: {checked}, изменений: {changed}")
    else:
        run_daemon()
