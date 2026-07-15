"""
sources/mos_supplier.py — коннектор к Порталу поставщиков (zakupki.mos.ru),
закупки малого объёма: котировочные сессии и потребности.

API снят разведкой 15.07.2026 (probe_zmo_v3 + живой фильтр из браузера):

    GET https://old.zakupki.mos.ru/api/Cssp/Purchase/Query?queryDto=<URL-encoded JSON>
        queryDto = {"filter": {...}, "take": N, "skip": N}
    Ответ: {"count": N, "items": [...]}

Поля item: id ("Need6135537"/"Auction..."), number, name, customers[].{name,inn},
stateId/stateName («Прием предложений»: need=20000002, auction=19000002),
startPrice, regionName, regionPath (Москва ".1.504."), beginDate/endDate
("DD.MM.YYYY HH:MM:SS", endDate — дедлайн подачи), federalLawName, externalUrl.

Особенности:
  · Реестр ОБЩЕРОССИЙСКИЙ (Сургут, ХМАО и т.д.) — регион режется MOS_REGION_PATHS.
  · nameLike строкой старый API не принимает (HTTP 500); фронт шлёт объект
    {"value": "...", "contains": true} — пробуем его, при 500 откатываемся на
    выборку без nameLike с клиентской фильтрацией по названию.
  · Окна подачи у сессий — часы (3/6/24), поэтому канал для частого опроса.
  · Активность гарантируем клиентской пост-фильтрацией stateId + endDate > now.

purchase_number = "MOS-<id>" (id уже содержит тип: Need/Auction/Tender).
Доступно только с российских IP.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any, Optional

import requests

import config

logger = logging.getLogger(__name__)

API_URL = "https://old.zakupki.mos.ru/api/Cssp/Purchase/Query"
BASE_URL = "https://zakupki.mos.ru"
PAGE_SIZE = 50

# Статусы «Прием предложений»
STATE_AUCTION_ACTIVE = 19000002
STATE_NEED_ACTIVE = 20000002
STATE_TENDER_ACTIVE = 5
ACTIVE_STATE_IDS = {STATE_AUCTION_ACTIVE, STATE_NEED_ACTIVE, STATE_TENDER_ACTIVE}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": f"{BASE_URL}/purchases",
}

_DT_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})")


def _sleep(multiplier: float = 1.0) -> None:
    time.sleep(max(0.4, config.REQUEST_DELAY * multiplier + random.uniform(0.2, 0.9)))


def _normalize_dt(raw: str) -> str:
    """«20.07.2026 08:00:00» → «20.07.2026 08:00» (общий формат проекта)."""
    m = _DT_RE.search(str(raw or ""))
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _deadline_passed(raw: str) -> bool:
    m = _DT_RE.search(str(raw or ""))
    if not m:
        return False
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M")
        return dt < datetime.now()
    except ValueError:
        return False


def _build_filter(keyword: str | None, price_from: int | None, price_to: int | None,
                  region_paths: list[str]) -> dict[str, Any]:
    """Фильтр в форме, которую шлёт фронт zakupki.mos.ru/purchase/list."""
    flt: dict[str, Any] = {
        "auctionSpecificFilter": {"stateIdIn": [STATE_AUCTION_ACTIVE]},
        "needSpecificFilter": {"stateIdIn": [STATE_NEED_ACTIVE]},
        "tenderSpecificFilter": {"stateIdIn": [STATE_TENDER_ACTIVE]},
    }
    if keyword:
        flt["nameLike"] = {"value": keyword, "contains": True}
    if price_from:
        flt["startPriceGreatEqual"] = int(price_from)
    if price_to:
        flt["startPriceLessEqual"] = int(price_to)
    if region_paths:
        flt["regionPaths"] = {"values": region_paths}
    return flt


def _query(flt: dict[str, Any], take: int, skip: int, retries: int = 3) -> Optional[dict[str, Any]]:
    dto = {"filter": flt, "take": take, "skip": skip, "withCount": True}
    url = API_URL + "?queryDto=" + urllib.parse.quote(json.dumps(dto, ensure_ascii=False))
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=25)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 500:
                # Скорее всего API не понял часть фильтра — сигналим вызывающему.
                logger.info("ПП Москвы HTTP 500 на фильтр %s", json.dumps(flt, ensure_ascii=False)[:120])
                return {"__error500__": True}
            logger.warning("ПП Москвы HTTP %s: %s", resp.status_code, resp.text[:150])
        except (requests.RequestException, ValueError) as e:
            logger.warning("ПП Москвы ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _card_url(item: dict[str, Any]) -> str:
    ext = str(item.get("externalUrl") or "").strip()
    if ext.startswith("http"):
        return ext
    if item.get("auctionId"):
        return f"{BASE_URL}/auction/{item['auctionId']}"
    if item.get("needId"):
        return f"{BASE_URL}/need/{item['needId']}"
    if item.get("tenderId"):
        return f"{BASE_URL}/tender/{item['tenderId']}"
    return BASE_URL + "/purchases"


def _map_item(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    uid = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    if not uid or not name:
        return None

    customers = item.get("customers") or []
    cust = customers[0] if customers and isinstance(customers[0], dict) else {}
    customer = str(cust.get("name") or "").strip()[:300]

    price = item.get("startPrice")
    if not isinstance(price, (int, float)) or price <= 0:
        price = None

    region = str(item.get("regionName") or "").strip()
    state = str(item.get("stateName") or "").strip()
    law = str(item.get("federalLawName") or "").strip()

    primary_text = "\n".join(filter(None, [
        name,
        customer,
        f"Номер: {item.get('number', '')}",
        f"Статус: {state}" if state else "",
        f"Регион: {region}" if region else "",
        f"Закон: {law}" if law else "",
        "Закупка малого объёма (Портал поставщиков)",
    ]))[:4000]

    return {
        "purchase_number": f"MOS-{uid}",
        "title": name[:500],
        "customer": customer,
        "customer_inn": str(cust.get("inn") or ""),
        "price": price,
        "law_type": "ЗМО",
        "deadline": _normalize_dt(item.get("endDate")),
        "published_at": _normalize_dt(item.get("beginDate")),
        "url": _card_url(item),
        "platform": "ПП Москвы",
        "primary_text": primary_text,
        "region": region,
    }


def search_mos(
    keyword: str,
    price_from: int | None = None,
    price_to: int | None = None,
    pages: int = 1,
    days_back: int | None = None,   # для совместимости с сигнатурой других каналов
) -> list[dict]:
    """Ищет активные ЗМО Портала поставщиков по ключевому слову.

    Возвращает список dict в формате scraper._parse_card.
    """
    keyword = (keyword or "").strip()
    region_paths = [
        str(x).strip() for x in getattr(config, "MOS_REGION_PATHS", []) if str(x).strip()
    ]

    tenders: list[dict] = []
    seen: set[str] = set()
    # Деградация фильтра при HTTP 500: сервер → клиент. Каждая следующая ступень
    # убирает из queryDto ещё один потенциально непонятый API параметр, а его
    # работу берёт на себя клиентская пост-фильтрация.
    client_side_kw = False
    client_side_region = False
    flt = _build_filter(keyword, price_from, price_to, region_paths)

    for page in range(pages):
        logger.info("ПП Москвы ищу '%s', страница %d", keyword, page + 1)
        data = _query(flt, take=PAGE_SIZE, skip=page * PAGE_SIZE)

        if data is not None and data.get("__error500__") and not client_side_kw and keyword:
            logger.info("ПП Москвы: nameLike не принят, фильтрую по названию на клиенте")
            client_side_kw = True
            flt = _build_filter(None, price_from, price_to, region_paths)
            data = _query(flt, take=PAGE_SIZE, skip=page * PAGE_SIZE)

        if data is not None and data.get("__error500__") and not client_side_region and region_paths:
            logger.info("ПП Москвы: regionPaths не принят, фильтрую по региону на клиенте")
            client_side_region = True
            flt = _build_filter(None if client_side_kw else keyword, price_from, price_to, [])
            data = _query(flt, take=PAGE_SIZE, skip=page * PAGE_SIZE)

        if not data or data.get("__error500__"):
            break

        items = data.get("items") or []
        if not items:
            break

        kw_lower = keyword.lower()
        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            # Только реально активные: статус «Прием предложений» и дедлайн впереди.
            if item.get("stateId") not in ACTIVE_STATE_IDS:
                continue
            if _deadline_passed(item.get("endDate")):
                continue
            if client_side_kw and kw_lower not in str(item.get("name", "")).lower():
                continue
            if client_side_region and region_paths and \
                    str(item.get("regionPath") or "") not in region_paths:
                continue
            card = _map_item(item)
            if not card or card["purchase_number"] in seen:
                continue
            seen.add(card["purchase_number"])
            card["matched_keywords"] = [f"mos:{keyword}"]
            tenders.append(card)
            added += 1

        if len(items) < PAGE_SIZE:
            break
        # При клиентской фильтрации листаем дальше, даже если на странице не было совпадений.
        if added == 0 and not (client_side_kw or client_side_region):
            break
        _sleep()

    logger.info("ПП Москвы '%s' → %d закупок (в реестре всего %s)",
                keyword, len(tenders), (data or {}).get("count", "?"))
    return tenders


if __name__ == "__main__":
    # Самотест (с российского IP): python -m sources.mos_supplier "сайт"
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    kw = sys.argv[1] if len(sys.argv) > 1 else "сайт"
    rows = search_mos(kw, price_from=20000, price_to=590000, pages=2)
    print(f"\nНайдено: {len(rows)}")
    for r in rows[:10]:
        print("─" * 60)
        print("номер  :", r["purchase_number"])
        print("назв.  :", r["title"][:100])
        print("заказ. :", r["customer"][:80] or "—")
        print("регион :", r.get("region") or "—")
        print("опубл. :", r["published_at"] or "—", "| до:", r["deadline"] or "—")
        print("НМЦК   :", r["price"] or "—")
        print("url    :", r["url"])
