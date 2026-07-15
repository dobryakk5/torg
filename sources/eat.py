"""
sources/eat.py — коннектор к ЕАТ «Берёзка» (agregatoreat.ru), закупки малого объёма.

API снят с живого SPA 15.07.2026 (перехват XHR):
    POST https://tender-cache-api.agregatoreat.ru/api/TradeLot/list-published-trade-lots
    body: {"lotStates":[2], "searchText":"<слово>", "priceStart":N, "priceEnd":N,
           "page":1, "size":50, "sort":[{"fieldName":"publishDate","direction":2}], ...}
    ответ: {"items":[...], "totalCount":N, "errors":[], "isFail":false}

Ключевые поля item:
    tradeNumber                 — номер закупки (напр. 100304452126100011)
    id (GUID)                   — карточка: https://agregatoreat.ru/purchases/announcement/<id>
    subject / price             — предмет и НМЦК
    organizerInfo.{fullName,inn}
    publishDate                 — ISO
    applicationFillingEndDate   — дедлайн подачи (окно обычно 24 часа!)
    applicationGuarantee        — обеспечение заявки
    lotItems[].{name,okpd2Code} — позиции спецификации

Анти-бот: с не-российских IP выдаётся слайдер-капча (страница <title>Captcha</title>).
С российского IP сервера обычно проходит. Если капча всё же появляется — пройди её
в браузере и положи куки в .env: EAT_COOKIE="__hash_=...; __lhash_=..."

lotStates=2 — «Подача предложений». purchase_number = "EAT-<tradeNumber>".
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Optional

import requests

import config

logger = logging.getLogger(__name__)

API_URL = "https://tender-cache-api.agregatoreat.ru/api/TradeLot/list-published-trade-lots"
CARD_URL = "https://agregatoreat.ru/purchases/announcement/{id}"
PAGE_SIZE = 50
LOT_STATE_ACCEPTING = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://agregatoreat.ru",
    "Referer": "https://agregatoreat.ru/",
}

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")


def _sleep(multiplier: float = 1.0) -> None:
    time.sleep(max(0.4, config.REQUEST_DELAY * multiplier + random.uniform(0.2, 0.9)))


def _iso_to_local_fmt(iso: str) -> str:
    """«2026-07-16T06:13:00.345328» → «16.07.2026 06:13» (общий формат проекта)."""
    m = _ISO_RE.match(str(iso or ""))
    if not m:
        return ""
    y, mo, d, hh, mm = m.groups()
    return f"{d}.{mo}.{y} {hh}:{mm}"


def _request_body(keyword: str, price_from: int | None, price_to: int | None,
                  page: int, size: int) -> dict[str, Any]:
    """Тело запроса — как шлёт SPA; неиспользуемые фильтры оставлены дефолтными."""
    return {
        "lotStates": [LOT_STATE_ACCEPTING],
        "dealStates": [],
        "lotItemEatCodes": [],
        "okpd2Codes": [],
        "ktruCodes": [],
        "purchaseTypeIds": [],
        "types": [],
        "purchaseMethods": [],
        "organizerRegions": [],
        "deliveryAddressRegionCodes": [],
        "searchText": keyword or None,
        "priceStart": price_from,
        "priceEnd": price_to,
        "page": page,
        "size": size,
        "sort": [{"fieldName": "publishDate", "direction": 2}],
        "isEatOnly": True,
    }


def _post(body: dict[str, Any], retries: int = 3) -> Optional[dict[str, Any]]:
    cookie = str(getattr(config, "EAT_COOKIE", "") or "").strip()
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    for attempt in range(retries):
        try:
            resp = requests.post(API_URL, json=body, headers=headers, timeout=25)
            text_head = (resp.text or "")[:200].lower()
            if resp.status_code == 200 and "<title>captcha" not in text_head:
                return resp.json()
            if "captcha" in text_head:
                logger.warning(
                    "ЕАТ: API вернул капчу — IP не прошёл анти-бот. Пройди капчу в браузере "
                    "на agregatoreat.ru и задай EAT_COOKIE в .env (__hash_/__lhash_)",
                )
                return None
            logger.warning("ЕАТ HTTP %s: %s", resp.status_code, resp.text[:150])
        except (requests.RequestException, ValueError) as e:
            logger.warning("ЕАТ ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _map_item(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    trade_number = str(item.get("tradeNumber") or "").strip()
    guid = str(item.get("id") or "").strip()
    if not trade_number or not guid:
        return None

    subject = str(item.get("subject") or "").strip()
    org = item.get("organizerInfo") or {}
    customer = str(org.get("fullName") or org.get("name") or "").strip()[:300]

    price = item.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        price = None

    lot_names = []
    for li in (item.get("lotItems") or [])[:15]:
        if isinstance(li, dict) and li.get("name"):
            code = f" (ОКПД2 {li.get('okpd2Code')})" if li.get("okpd2Code") else ""
            lot_names.append(f"{li['name']}{code}")

    title = subject or (lot_names[0] if lot_names else "")
    if not title:
        return None

    guarantee = item.get("applicationGuarantee")

    primary_text = "\n".join(filter(None, [
        title,
        customer,
        f"Номер ЕАТ: {trade_number}",
        "Позиции: " + "; ".join(lot_names) if lot_names else "",
        "Закупка малого объёма (ЕАТ «Берёзка»)",
    ]))[:4000]

    card: dict[str, Any] = {
        "purchase_number": f"EAT-{trade_number}",
        "title": title[:500],
        "customer": customer,
        "customer_inn": str(org.get("inn") or ""),
        "price": price,
        "law_type": "ЗМО",
        "deadline": _iso_to_local_fmt(item.get("applicationFillingEndDate")),
        "published_at": _iso_to_local_fmt(item.get("publishDate")),
        "url": CARD_URL.format(id=guid),
        "platform": "ЕАТ",
        "primary_text": primary_text,
    }
    if isinstance(guarantee, (int, float)) and guarantee > 0:
        card["application_security_amount"] = float(guarantee)
    return card


def search_eat(
    keyword: str,
    price_from: int | None = None,
    price_to: int | None = None,
    pages: int = 1,
    days_back: int | None = None,   # для совместимости с сигнатурой других каналов
) -> list[dict]:
    """Ищет ЗМО на ЕАТ в статусе «Подача предложений» по ключевому слову.

    Возвращает список dict в формате scraper._parse_card.
    """
    tenders: list[dict] = []
    seen: set[str] = set()

    for page in range(1, pages + 1):
        logger.info("ЕАТ ищу '%s', страница %d", keyword, page)
        data = _post(_request_body(keyword, price_from, price_to, page, PAGE_SIZE))
        if not data or data.get("isFail"):
            if data and data.get("errors"):
                logger.warning("ЕАТ ошибки API: %s", str(data["errors"])[:200])
            break

        items = data.get("items") or []
        if not items:
            break

        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            card = _map_item(item)
            if not card or card["purchase_number"] in seen:
                continue
            seen.add(card["purchase_number"])
            card["matched_keywords"] = [f"eat:{keyword}"]
            tenders.append(card)
            added += 1

        if added == 0 or len(items) < PAGE_SIZE:
            break
        _sleep()

    logger.info("ЕАТ '%s' → %d закупок (всего в выдаче %s)",
                keyword, len(tenders), (data or {}).get("totalCount", "?"))
    return tenders


if __name__ == "__main__":
    # Самотест (с сервера в РФ): python -m sources.eat "сайт"
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    kw = sys.argv[1] if len(sys.argv) > 1 else "программное обеспечение"
    rows = search_eat(kw, price_from=20000, price_to=590000, pages=1)
    print(f"\nНайдено: {len(rows)}")
    for r in rows[:8]:
        print("─" * 60)
        print("номер  :", r["purchase_number"])
        print("назв.  :", r["title"][:100])
        print("заказ. :", r["customer"][:80] or "—")
        print("опубл. :", r["published_at"] or "—", "| до:", r["deadline"] or "—")
        print("НМЦК   :", r["price"] or "—", "| обесп.:", r.get("application_security_amount", "—"))
        print("url    :", r["url"])
