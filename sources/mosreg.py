"""
sources/mosreg.py — коннектор к Электронному магазину Московской области
(market.mosreg.ru), закупки малого объёма.

API снят с cURL из DevTools пользователя 15.07.2026:

    POST https://api.market.mosreg.ru/api/Trade/GetTradesForParticipantOrAnonymous
    Заголовки: Content-Type: application/json + ОБЯЗАТЕЛЬНЫЙ "XXX-TenantId-Header: 2"
    Тело: {"page":1, "itemsPerPage":10, "tradeName":"<слово>", "tradeState":"15",
           "filterPriceMin":"1000", "filterPriceMax":"999000", ...}
    tradeState "15" = «подача заявок».

Структура ответа на момент написания не зафиксирована, поэтому маппинг
защитный: берёт первое подходящее из синонимичных имён полей, а если не смог
собрать ни одной карточки — печатает в лог сырой первый item (по нему легко
доуточнить маппинг). Самотест: python -m sources.mosreg "сайт"

purchase_number = "MOSREG-<id>". Доступно только с российских IP.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Optional

import requests

import config

logger = logging.getLogger(__name__)

API_URL = "https://api.market.mosreg.ru/api/Trade/GetTradesForParticipantOrAnonymous"
BASE_URL = "https://market.mosreg.ru"
PAGE_SIZE = 25
TRADE_STATE_ACCEPTING = "15"   # «подача заявок»

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "XXX-TenantId-Header": "2",
}

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")
_RU_DT_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?")


def _sleep(multiplier: float = 1.0) -> None:
    time.sleep(max(0.4, config.REQUEST_DELAY * multiplier + random.uniform(0.2, 0.9)))


def _to_local_dt(raw: Any) -> str:
    """ISO «2026-07-16T20:59:00…» или «16.07.2026 20:59» → «16.07.2026 20:59»."""
    s = str(raw or "")
    m = _ISO_RE.match(s)
    if m:
        y, mo, d, hh, mm = m.groups()
        return f"{d}.{mo}.{y} {hh}:{mm}"
    m = _RU_DT_RE.search(s)
    if m:
        return f"{m.group(1)} {m.group(2)}" if m.group(2) else m.group(1)
    return ""


def _pick(item: dict, *names, default=None):
    """Первое непустое значение из синонимичных полей (регистр не важен)."""
    lowered = {str(k).lower(): v for k, v in item.items()}
    for name in names:
        v = lowered.get(name.lower())
        if v not in (None, "", []):
            return v
    return default


def _request_body(keyword: str, price_from: int | None, price_to: int | None,
                  page: int, size: int) -> dict[str, Any]:
    """Тело как в живом запросе SPA; пользовательские фильтры UI — нейтральные."""
    return {
        "page": page,
        "itemsPerPage": size,
        "Id": "",
        "tradeName": keyword or "",
        "tradeState": TRADE_STATE_ACCEPTING,
        "OnlyTradesWithMyApplications": False,
        "sortingParams": [],
        "filterPriceMin": str(price_from) if price_from else "",
        "filterPriceMax": str(price_to) if price_to else "",
        "filterDateFrom": None,
        "filterDateTo": None,
        "filterFillingApplicationEndDateFrom": None,
        "FilterFillingApplicationEndDateTo": None,
        "filterTradeEasuzNumber": "",
        "showOnlyOwnTrades": False,
        "showApprovementTrades": False,
        "IsImmediate": False,
        "UsedClassificatorType": 20,
        "classificatorCodes": [],
        "CustomerFullNameOrInn": "",
        "CustomerAddress": "",
        "Koz2Value": "",
        "ParticipantHasApplicationsOnTrade": "",
        "ProductPriceMin": "",
        "ProductPriceMax": "",
    }


def _post(body: dict[str, Any], retries: int = 3) -> Optional[Any]:
    for attempt in range(retries):
        try:
            resp = requests.post(API_URL, json=body, headers=HEADERS, timeout=25,
                                 proxies=config.source_proxies())
            if resp.status_code == 200:
                return resp.json()
            logger.warning("ЭМ МО HTTP %s: %s", resp.status_code, resp.text[:200])
            if resp.status_code in (401, 403):
                return None
        except (requests.RequestException, ValueError) as e:
            logger.warning("ЭМ МО ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _extract_items(data: Any) -> list[dict]:
    """Достаёт массив торгов из ответа, не полагаясь на точное имя обёртки."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "Items", "trades", "Trades", "data", "Data", "result", "Result"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # одноуровневая обёртка: {"...": {"items": [...]}}
        for v in data.values():
            if isinstance(v, dict):
                inner = _extract_items(v)
                if inner:
                    return inner
    return []


def _map_item(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    uid = _pick(item, "id", "tradeId", "number", "tradeNumber")
    title = str(_pick(item, "tradeName", "name", "subject", "title", default="")).strip()
    if uid in (None, "") or not title:
        return None

    customer = str(_pick(item, "customerFullName", "customerName", "customer",
                         "organizationName", default="")).strip()
    if isinstance(_pick(item, "customer"), dict):
        c = _pick(item, "customer")
        customer = str(c.get("fullName") or c.get("name") or customer).strip()

    price = _pick(item, "price", "startPrice", "nmck", "sum", "tradeSum", "maxPrice")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is not None and price <= 0:
        price = None

    deadline = _to_local_dt(_pick(
        item, "fillingApplicationEndDate", "applicationEndDate",
        "endDate", "dateEnd", "fillingEndDate",
    ))
    published = _to_local_dt(_pick(
        item, "publicationDate", "publishDate", "createDate", "beginDate", "dateBegin",
    ))

    inn = str(_pick(item, "customerInn", "inn", default="") or "")
    easuz = str(_pick(item, "tradeEasuzNumber", "easuzNumber", default="") or "")

    primary_text = "\n".join(filter(None, [
        title,
        customer,
        f"Номер: {uid}" + (f" / ЕАСУЗ {easuz}" if easuz else ""),
        "Регион: Московская область",
        "Закупка малого объёма (Электронный магазин МО)",
    ]))[:4000]

    return {
        "purchase_number": f"MOSREG-{uid}",
        "title": title[:500],
        "customer": customer[:300],
        "customer_inn": inn,
        "price": price,
        "law_type": "ЗМО",
        "deadline": deadline,
        "published_at": published,
        "url": f"{BASE_URL}/Trade/ViewTrade?id={uid}",
        "platform": "ЭМ МО",
        "primary_text": primary_text,
        "region": "Московская область",
    }


def search_mosreg(
    keyword: str,
    price_from: int | None = None,
    price_to: int | None = None,
    pages: int = 1,
    days_back: int | None = None,   # для совместимости с сигнатурой других каналов
) -> list[dict]:
    """Ищет активные ЗМО Электронного магазина МО по ключевому слову.

    Возвращает список dict в формате scraper._parse_card.
    """
    tenders: list[dict] = []
    seen: set[str] = set()
    raw_logged = False

    for page in range(1, pages + 1):
        logger.info("ЭМ МО ищу '%s', страница %d", keyword, page)
        data = _post(_request_body(keyword, price_from, price_to, page, PAGE_SIZE))
        if data is None:
            break

        items = _extract_items(data)
        if not items:
            if page == 1 and not raw_logged:
                logger.info("ЭМ МО: пустая выдача; верхние ключи ответа: %s",
                            list(data.keys())[:10] if isinstance(data, dict) else type(data))
            break

        added = 0
        for item in items:
            card = _map_item(item)
            if not card:
                if not raw_logged:
                    raw_logged = True
                    logger.warning("ЭМ МО: не смог смаппить item — сырой JSON для доуточнения:\n%s",
                                   json.dumps(item, ensure_ascii=False)[:1500])
                continue
            if card["purchase_number"] in seen:
                continue
            seen.add(card["purchase_number"])
            card["matched_keywords"] = [f"mosreg:{keyword}"]
            tenders.append(card)
            added += 1

        if added == 0 or len(items) < PAGE_SIZE:
            break
        _sleep()

    logger.info("ЭМ МО '%s' → %d закупок", keyword, len(tenders))
    return tenders


if __name__ == "__main__":
    # Самотест (с российского IP): python -m sources.mosreg "сайт"
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    kw = sys.argv[1] if len(sys.argv) > 1 else "сайт"

    # Первым делом — сырая структура первого item (для проверки маппинга).
    raw = _post(_request_body(kw, 20000, 590000, 1, 3))
    items = _extract_items(raw)
    if items:
        print("── Сырой первый item (ключи и значения, обрезано) ──")
        for k, v in items[0].items():
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:100]}")
    elif isinstance(raw, dict):
        print("Ответ без распознанных items; верхние ключи:", list(raw.keys())[:15])

    rows = search_mosreg(kw, price_from=20000, price_to=590000, pages=1)
    print(f"\nНайдено: {len(rows)}")
    for r in rows[:8]:
        print("─" * 60)
        print("номер  :", r["purchase_number"])
        print("назв.  :", r["title"][:100])
        print("заказ. :", r["customer"][:80] or "—")
        print("опубл. :", r["published_at"] or "—", "| до:", r["deadline"] or "—")
        print("НМЦК   :", r["price"] or "—")
        print("url    :", r["url"])
