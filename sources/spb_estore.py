"""
sources/spb_estore.py — коннектор к «Электронному магазину» Санкт-Петербурга
(закупки малого объёма, estore.gz-spb.ru).

ЗМО (до 600 тыс. по п.4/5 ч.1 ст.93 44-ФЗ) не публикуются в ЕИС как извещения —
только на таких витринах. Сроки подачи оферт короткие (часто 1–2 дня), поэтому
канал предназначен для частого опроса.

Витрина server-side HTML (движок МТЭ), без регистрации и без капчи
(проверено 15.07.2026). Поиск:
    https://estore.gz-spb.ru/electronicshop/catalog/procedure/index/
        ?keywords=<слово>&status_id=3            # 3 = «Подача оферт»
        &max_sum[from]=<руб>&max_sum[to]=<руб>
        &page=<N>
Результаты — table.mte-grid-table, 8 колонок:
    0 название (+ссылка /electronicshop/procedure/form/view/<id>/)
    1 реестровый номер · 2 заказчик · 3 дата публикации «HH:MM DD.MM.YYYY»
    4 срок подачи оферт · 5 НМЦК «30 000.00» · 6 статус · 7 действия

Возвращает список dict в формате scraper._parse_card.
purchase_number = "SPB-<реестровый номер>" — не пересекается с номерами ЕИС.
"""

from __future__ import annotations

import logging
import random
import re
import time
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config
# Витрина СПб использует сертификат Russian Trusted CA (как ЕИС) — берём
# готовую verify-логику скрейпера (truststore/EIS_CA_BUNDLE/certifi).
from scraper import REQUEST_VERIFY

logger = logging.getLogger(__name__)

BASE_URL = "https://estore.gz-spb.ru"
SEARCH_URL = f"{BASE_URL}/electronicshop/catalog/procedure/index/"
STATUS_ACCEPTING_OFFERS = "3"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": f"{BASE_URL}/electronicshop/catalog/procedure/",
}

# «13:03 15.07.2026» (витрина показывает время первым)
_TIME_DATE_RE = re.compile(r"(\d{2}:\d{2})\s+(\d{2}\.\d{2}\.\d{4})")
_VIEW_HREF_RE = re.compile(r"/electronicshop/procedure/form/view/(\d+)/")
_TOTAL_RE = re.compile(r"Всего:\s*(\d+)")

# Слишком короткий ключ («1С») витрина молча игнорирует и отдаёт ВЕСЬ реестр
# (сотни тысяч записей) — мусор в БД. Минимальная длина + порог «Всего».
MIN_KEYWORD_LEN = 3
MAX_PLAUSIBLE_TOTAL = 5000


def _sleep(multiplier: float = 1.0) -> None:
    time.sleep(max(0.4, config.REQUEST_DELAY * multiplier + random.uniform(0.2, 0.9)))


def _get(url: str, params: dict | None = None, retries: int = 3) -> Optional[requests.Response]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=25, verify=REQUEST_VERIFY,
                                proxies=config.source_proxies())
            if resp.status_code == 200:
                return resp
            logger.warning("ЭМ СПб HTTP %s для %s", resp.status_code, url)
            if resp.status_code in (403, 429):
                logger.warning("ЭМ СПб возможный анти-бот (%s)", resp.status_code)
                return None
        except requests.RequestException as e:
            logger.warning("ЭМ СПб ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _normalize_dt(cell_text: str) -> str:
    """«13:03 15.07.2026» → «15.07.2026 13:03» (общий формат deadline в проекте)."""
    m = _TIME_DATE_RE.search(cell_text or "")
    if m:
        return f"{m.group(2)} {m.group(1)}"
    # запасной вариант: голая дата
    m2 = re.search(r"\d{2}\.\d{2}\.\d{4}", cell_text or "")
    return m2.group(0) if m2 else ""


def _parse_price(cell_text: str) -> float | None:
    cleaned = (cell_text or "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    try:
        value = float(m.group(0)) if m else None
    except ValueError:
        return None
    return value if value and value > 0 else None


def _parse_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="mte-grid-table")
    if not table:
        return []

    tenders: list[dict] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue

        link = cells[0].find("a", href=_VIEW_HREF_RE)
        title = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True)).strip()
        reg_number = cells[1].get_text(strip=True)
        if not title or not reg_number:
            continue

        url = ""
        if link:
            m = _VIEW_HREF_RE.search(link["href"])
            if m:
                # без backurl-хвоста — стабильная прямая ссылка
                url = f"{BASE_URL}/electronicshop/procedure/form/view/{m.group(1)}/"

        customer = cells[2].get_text(" ", strip=True)[:300]
        published_at = _normalize_dt(cells[3].get_text(" ", strip=True))
        deadline = _normalize_dt(cells[4].get_text(" ", strip=True))
        price = _parse_price(cells[5].get_text(strip=True))
        status = cells[6].get_text(strip=True)

        primary_text = "\n".join(filter(None, [
            title,
            customer,
            f"Реестровый номер: {reg_number}",
            f"Статус: {status}" if status else "",
            "Регион: Санкт-Петербург",
            "Закупка малого объёма (электронный магазин СПб)",
        ]))[:4000]

        tenders.append({
            "purchase_number": f"SPB-{reg_number}",
            "title": title[:500],
            "customer": customer,
            "price": price,
            "law_type": "ЗМО",
            "deadline": deadline,
            "published_at": published_at,
            "url": url,
            "platform": "ЭМ СПб",
            "primary_text": primary_text,
            "region": "Санкт-Петербург",
        })

    return tenders


def search_spb(
    keyword: str,
    price_from: int | None = None,
    price_to: int | None = None,
    pages: int = 1,
    days_back: int | None = None,   # для совместимости с сигнатурой других каналов
) -> list[dict]:
    """Ищет ЗМО СПб в статусе «Подача оферт» по ключевому слову.

    Поиск витрины полнотекстовый: ловит и названия закупок, и позиции спецификации.
    Возвращает список dict в формате scraper._parse_card.
    """
    keyword = (keyword or "").strip()
    if len(keyword) < MIN_KEYWORD_LEN:
        logger.warning(
            "ЭМ СПб: ключ '%s' короче %d символов — витрина игнорирует такой фильтр "
            "и отдаёт весь реестр; пропускаю", keyword, MIN_KEYWORD_LEN,
        )
        return []

    tenders: list[dict] = []
    seen: set[str] = set()

    params_base = {
        "keywords": keyword,
        "status_id": STATUS_ACCEPTING_OFFERS,
    }
    if price_from:
        params_base["max_sum[from]"] = str(price_from)
    if price_to:
        params_base["max_sum[to]"] = str(price_to)

    for page in range(1, pages + 1):
        params = dict(params_base)
        if page > 1:
            params["page"] = str(page)

        logger.info("ЭМ СПб ищу '%s', страница %d", keyword, page)
        resp = _get(SEARCH_URL, params=params)
        if not resp:
            break

        total_m = _TOTAL_RE.search(resp.text)
        if total_m and int(total_m.group(1)) > MAX_PLAUSIBLE_TOTAL:
            logger.warning(
                "ЭМ СПб: по ключу '%s' витрина вернула %s записей — фильтр не применился, "
                "пропускаю ключ", keyword, total_m.group(1),
            )
            break

        page_tenders = _parse_results(resp.text)
        if not page_tenders:
            break

        new_on_page = [t for t in page_tenders if t["purchase_number"] not in seen]
        if not new_on_page:
            break
        for t in new_on_page:
            seen.add(t["purchase_number"])
            t["matched_keywords"] = [f"spb:{keyword}"]
            tenders.append(t)

        # Неполная страница (по умолчанию 10 строк) — дальше пусто.
        if len(page_tenders) < 10:
            break
        _sleep()

    logger.info("ЭМ СПб '%s' → %d закупок", keyword, len(tenders))
    return tenders


if __name__ == "__main__":
    # Самотест: python -m sources.spb_estore "сайт"
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    kw = sys.argv[1] if len(sys.argv) > 1 else "программное обеспечение"
    rows = search_spb(kw, price_from=20000, price_to=590000, pages=2)
    print(f"\nНайдено: {len(rows)}")
    for r in rows[:8]:
        print("─" * 60)
        print("номер  :", r["purchase_number"])
        print("назв.  :", r["title"][:100])
        print("заказ. :", r["customer"] or "—")
        print("опубл. :", r["published_at"] or "—", "| до:", r["deadline"] or "—")
        print("НМЦК   :", r["price"] or "—")
        print("url    :", r["url"])
