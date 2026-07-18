"""HTML-поиск активных запросов доставки на Бизнес-площадке ZakazRF."""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

BASE_URL = "https://bp.zakazrf.ru"
SEARCH_URL = f"{BASE_URL}/DeliveryRequest/Index"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": f"{BASE_URL}/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}

_ID_RE = re.compile(r"/DeliveryRequest/id/(\d+)", re.I)
_DT_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_dt(value: Any) -> str:
    match = _DT_RE.search(_clean(value))
    return f"{match.group(1)} {match.group(2)}" if match else ""


def _deadline_passed(value: Any) -> bool:
    normalized = _normalize_dt(value)
    if not normalized:
        return False
    try:
        return datetime.strptime(normalized, "%d.%m.%Y %H:%M") < datetime.now()
    except ValueError:
        return False


def _parse_price(value: Any) -> float | None:
    cleaned = re.sub(r"[^\d,.-]", "", _clean(value)).replace(",", ".")
    try:
        price = float(cleaned)
    except ValueError:
        return None
    return price if price > 0 else None


def _find_results_table(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        if str(table.get("objecttype") or "").lower() == "deliveryrequest":
            return table
        if table.find("a", href=_ID_RE):
            return table
    return None


def _parse_html(html: str | bytes, keyword: str = "") -> list[dict[str, Any]]:
    """Преобразует таблицу выдачи в общий формат карточек Stage 1."""
    soup = BeautifulSoup(html, "html.parser")
    table = _find_results_table(soup)
    if table is None:
        return []
    rows = table.find_all("tr")
    if not rows:
        return []

    headers = [_clean(cell.get_text(" ", strip=True)) for cell in rows[0].find_all(["th", "td"])]
    indexes = {name: i for i, name in enumerate(headers)}

    def value(cells: list, name: str) -> str:
        idx = indexes.get(name)
        return _clean(cells[idx].get_text(" ", strip=True)) if idx is not None and idx < len(cells) else ""

    tenders: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows[1:]:
        cells = row.find_all("td")
        link = row.find("a", href=_ID_RE)
        if not cells or link is None:
            continue
        href = str(link.get("href") or "")
        id_match = _ID_RE.search(href)
        if not id_match:
            continue
        request_id = id_match.group(1)
        purchase_number = f"ZAKAZRF-{request_id}"
        if purchase_number in seen:
            continue

        trade_status = value(cells, "Статус торгов")
        notice_status = value(cells, "Статус извещения")
        deadline_raw = value(cells, "Окончание определения лучшего предложения")
        if trade_status.lower() != "опубликовано" or notice_status.lower() not in {"идут торги", "торги продлены"}:
            continue
        if _deadline_passed(deadline_raw):
            continue

        title = value(cells, "Название позиции")
        if not title:
            continue
        number = value(cells, "Номер") or request_id
        customer = value(cells, "Организатор")
        region = value(cells, "Регион поставки")
        ktru = value(cells, "КТРУ+/КСР+")
        trading_mode = value(cells, "Торговый режим")
        location = value(cells, "Место поставки")
        published_at = _normalize_dt(value(cells, "Дата начала подачи предложений"))
        deadline = _normalize_dt(deadline_raw)
        primary_text = "\n".join(filter(None, [
            title,
            customer,
            f"Номер: {number}",
            f"КТРУ/КСР: {ktru}" if ktru else "",
            f"Торговый режим: {trading_mode}" if trading_mode else "",
            f"Регион: {region}" if region else "",
            # Не пишем служебное «место поставки»: маска поставк* является
            # профильным штрафом и не должна срабатывать на подписи колонки.
            f"Адрес исполнения: {location}" if location else "",
            f"Статусы: {trade_status}; {notice_status}",
            "Запрос доставки Бизнес-площадки ZakazRF",
        ]))[:4000]

        tenders.append({
            "purchase_number": purchase_number,
            "title": title[:500],
            "customer": customer[:300],
            "customer_inn": "",
            "price": _parse_price(value(cells, "Начальная цена")),
            "law_type": "ЗМО",
            "deadline": deadline,
            "published_at": published_at,
            "url": urljoin(BASE_URL, href),
            "platform": "БП ZakazRF",
            "primary_text": primary_text,
            "region": region,
            "matched_keywords": [f"zakazrf:{keyword}"] if keyword else [],
        })
        seen.add(purchase_number)
    return tenders


def search_zakazrf(keyword: str, pages: int = 1) -> list[dict[str, Any]]:
    """Ищет активные запросы доставки по обычной HTML-выдаче площадки."""
    keyword = _clean(keyword)
    params = {
        "Filter": "1",
        "FastFilter": keyword,
        "SelectedTabPage": "trade",
        "FilterExpand": "1",
    }
    ticks = str(getattr(config, "ZAKAZRF_PLANED_DATE_FROM_TICKS", "") or "").strip()
    if ticks:
        params["PlanedPublishDateFrom"] = ticks

    for attempt in range(3):
        try:
            response = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=30,
                                    proxies=config.source_proxies())
            response.raise_for_status()
            # Передаём bytes: BeautifulSoup сам прочитает meta charset=UTF-8;
            # requests для text/html без charset иначе может выбрать latin-1.
            rows = _parse_html(response.content, keyword=keyword)
            logger.info("БП ZakazRF '%s' → %d активных запросов", keyword, len(rows))
            return rows
        except requests.RequestException as exc:
            logger.warning("БП ZakazRF ошибка запроса (попытка %d): %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(max(0.5, config.REQUEST_DELAY + random.uniform(0.2, 0.8)))
    return []


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    query = sys.argv[1] if len(sys.argv) > 1 else "сайт"
    results = search_zakazrf(query)
    print(f"\nНайдено: {len(results)}")
    for item in results[:10]:
        print("─" * 60)
        print("номер  :", item["purchase_number"])
        print("назв.  :", item["title"])
        print("заказ. :", item["customer"] or "—")
        print("регион :", item["region"] or "—")
        print("до     :", item["deadline"] or "—")
        print("цена   :", item["price"] or "—")
        print("url    :", item["url"])
