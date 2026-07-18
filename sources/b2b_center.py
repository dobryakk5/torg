"""
sources/b2b_center.py — коннектор к открытой витрине торгов B2B-Center.

Коммерческие закупки и 223-ФЗ, которых нет в ЕИС. Витрина отдаётся server-side
HTML (без регистрации), парсится requests + BeautifulSoup, как и ЕИС.

Поиск:
    https://www.b2b-center.ru/market/?f_keyword=<слово>&searching=1&trade=all
       [&price_start=<руб>&price_end=<руб>&price_currency=RUR]
       [&from=<offset>]      # пагинация шагом 20

Возвращает список dict в ТОМ ЖЕ формате, что scraper.search_eis (см. _parse_card),
чтобы main._process_stage1_tenders и весь движок фильтров работали без изменений.

purchase_number = "B2B-<id>" — не пересекается с 11–22-значными номерами ЕИС.
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

logger = logging.getLogger(__name__)

BASE_URL = "https://www.b2b-center.ru"
SEARCH_URL = f"{BASE_URL}/market/"
PAGE_SIZE = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": f"{BASE_URL}/market/",
}

_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?")
_ORG_RE = re.compile(
    r"(?:ООО|АО|ПАО|ОАО|ЗАО|ИП|АНО|ГБУ|ФГБУ|МУП|ГУП|ФГУП|ПК|НКО|ТСЖ)\s*[«\"][^»\"]{2,140}[»\"]"
)
_TENDER_HREF_RE = re.compile(r"/tender-(\d+)/")


def _sleep(multiplier: float = 1.0) -> None:
    time.sleep(max(0.4, config.REQUEST_DELAY * multiplier + random.uniform(0.2, 0.9)))


def _get(url: str, params: dict | None = None, retries: int = 3) -> Optional[requests.Response]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=25,
                                proxies=config.source_proxies())
            if resp.status_code == 200:
                return resp
            logger.warning("B2B HTTP %s для %s", resp.status_code, url)
            if resp.status_code in (403, 429):
                logger.warning("B2B возможный анти-бот (%s)", resp.status_code)
                return None
        except requests.RequestException as e:
            logger.warning("B2B ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _card_container(anchor):
    """Поднимается от ссылки на торг до блока-карточки (с 2 датами, короткий текст)."""
    node = anchor
    for _ in range(8):
        if node.parent is None:
            break
        node = node.parent
        text = node.get_text(" ", strip=True)
        if len(text) < 1500 and len(_DATE_RE.findall(text)) >= 1:
            return node
    return anchor.parent or anchor


def _parse_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tenders: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        m = _TENDER_HREF_RE.search(a["href"])
        if not m:
            continue
        tid = m.group(1)
        if tid in seen:
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        # Берём как заголовок только осмысленную ссылку (название процедуры), не «Добавить в избранное»
        if not title or "избранно" in title.lower():
            continue
        seen.add(tid)

        href = a["href"].split("#")[0]
        url = href if href.startswith("http") else BASE_URL + href

        card = _card_container(a)
        card_text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))

        dates = _DATE_RE.findall(card_text)
        published_at = dates[0] if dates else ""
        deadline = dates[1] if len(dates) >= 2 else (dates[0] if dates else "")

        org = _ORG_RE.search(card_text)
        customer = org.group(0) if org else ""

        # Номер процедуры из текста («№ 4470130») либо из id ссылки
        num_m = re.search(r"№\s*(\d{5,})", title) or re.search(r"№\s*(\d{5,})", card_text)
        proc_number = num_m.group(1) if num_m else tid

        law_type = "223-ФЗ" if re.search(r"223[\- ]?фз|223-ФЗ", card_text, re.I) else "коммерческий"

        tenders.append({
            "purchase_number": f"B2B-{tid}",
            "title": title[:500],
            "customer": customer[:300],
            "price": None,                       # цена обычно внутри карточки торга; уточняется на Stage 2
            "law_type": law_type,
            "deadline": deadline,
            "published_at": published_at,
            "url": url,
            "platform": "B2B-Center",
            "primary_text": card_text[:4000],
            "external_number": proc_number,
        })

    return tenders


def search_b2b(
    keyword: str,
    price_from: int | None = None,
    price_to: int | None = None,
    pages: int = 1,
    days_back: int | None = None,   # принимается для совместимости; B2B фильтрует по периоду иначе
) -> list[dict]:
    """Ищет открытые торги B2B-Center по ключевому слову.

    Возвращает список dict в формате scraper._parse_card.
    """
    tenders: list[dict] = []
    seen: set[str] = set()

    params_base = {
        "f_keyword": keyword,
        "searching": "1",
        "trade": "all",
    }
    if price_from:
        params_base["price_start"] = str(price_from)
        params_base["price_currency"] = "RUR"
    if price_to:
        params_base["price_end"] = str(price_to)
        params_base["price_currency"] = "RUR"

    for page in range(pages):
        params = dict(params_base)
        if page > 0:
            params["from"] = str(page * PAGE_SIZE)

        logger.info("B2B ищу '%s', страница %d", keyword, page + 1)
        resp = _get(SEARCH_URL, params=params)
        if not resp:
            break

        page_tenders = _parse_results(resp.text)
        if not page_tenders:
            break

        # Защита от «отката» к базовой витрине при выходе за пределы результатов:
        # если все id уже встречались — дальше не листаем.
        new_on_page = [t for t in page_tenders if t["purchase_number"] not in seen]
        if not new_on_page:
            break
        for t in new_on_page:
            seen.add(t["purchase_number"])
            t["matched_keywords"] = [f"b2b:{keyword}"]
            tenders.append(t)

        # Если страница неполная — следующих страниц нет.
        if len(page_tenders) < PAGE_SIZE:
            break
        _sleep()

    logger.info("B2B '%s' → %d торгов", keyword, len(tenders))
    return tenders


if __name__ == "__main__":
    # Самотест: python -m sources.b2b_center "сайт"
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    kw = sys.argv[1] if len(sys.argv) > 1 else "сопровождение сайта"
    rows = search_b2b(kw, pages=1)
    print(f"\nНайдено: {len(rows)}")
    for r in rows[:8]:
        print("─" * 60)
        print("номер  :", r["purchase_number"], "/", r.get("external_number"))
        print("назв.  :", r["title"][:100])
        print("заказ. :", r["customer"] or "—")
        print("опубл. :", r["published_at"] or "—", "| до:", r["deadline"] or "—")
        print("закон  :", r["law_type"])
        print("url    :", r["url"])
