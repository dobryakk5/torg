"""
sources/mos_supplier.py — коннектор к Порталу поставщиков (zakupki.mos.ru),
активные котировочные сессии малого объёма.

API снят с живого фильтра zakupki.mos.ru:

    GET https://old.zakupki.mos.ru/api/Cssp/Purchase/Query?queryDto=<URL-encoded JSON>
        queryDto = {"filter": {...}, "order": [...], "withCount": true,
                    "take": N, "skip": N}
    Ответ: {"count": N, "items": [...]}

Поля item: id ("Need6135537"/"Auction..."), number, name, customers[].{name,inn},
stateId/stateName («Прием предложений»: need=20000002, auction=19000002),
startPrice, regionName, regionPath (Москва ".1.504."), beginDate/endDate
("DD.MM.YYYY HH:MM:SS", endDate — дедлайн подачи), federalLawName, externalUrl.

Особенности:
  · Реестр ОБЩЕРОССИЙСКИЙ (Сургут, ХМАО и т.д.) — регион режется MOS_REGION_PATHS.
  · Поиск котировочных сессий требует typeIn={"values":[1]} и полную форму
    фильтра из фронтенда, включая пустые вложенные объекты.
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
# Фронтенд портала запрашивает выдачу порциями по 10 карточек.
PAGE_SIZE = 10

# Тип 1 — котировочные сессии; статус «Прием предложений».
PURCHASE_TYPE_AUCTION = 1
STATE_AUCTION_ACTIVE = 19000002
ACTIVE_STATE_IDS = {STATE_AUCTION_ACTIVE}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Cache-Control": "no-cache",
    "Origin": BASE_URL,
    "Pragma": "no-cache",
    "Referer": f"{BASE_URL}/",
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
    """Фильтр котировочных сессий в точной форме фронтенда портала."""
    flt: dict[str, Any] = {
        "typeIn": {"values": [PURCHASE_TYPE_AUCTION]},
        "nameLike": {"contains": True},
        "regionPaths": {},
        "customerInnOrName": {"contains": True},
        "numberLike": {"contains": True},
        "externalNumberLike": {"contains": True},
        "auctionSpecificFilter": {
            "stateIdIn": [STATE_AUCTION_ACTIVE],
            "initialDuration": [3, 6, 24],
            "supplierTotalPoints": {},
        },
        "needSpecificFilter": {"supplierTotalPoints": {}},
        "tenderSpecificFilter": {},
        "ptkrSpecificFilter": {},
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


def _build_query_dto(flt: dict[str, Any], take: int, skip: int) -> dict[str, Any]:
    return {
        "filter": flt,
        "order": [{"field": "relevance", "desc": True}],
        "withCount": True,
        "take": take,
        "skip": skip,
    }


def _query(flt: dict[str, Any], take: int, skip: int, retries: int = 3) -> Optional[dict[str, Any]]:
    dto = _build_query_dto(flt, take, skip)
    encoded_dto = urllib.parse.quote(
        json.dumps(dto, ensure_ascii=False, separators=(",", ":")), safe=""
    )
    url = API_URL + "?queryDto=" + encoded_dto
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=25,
                                proxies=config.source_proxies())
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
    """Ищет активные котировочные сессии по ключевому слову.

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


# ── Документы котировочной сессии ────────────────────────────────────────────
# Карточка аукциона отдаёт список файлов, качаются по id из хранилища:
#   GET newapi/api/Auction/Get?auctionId=<id>      → {"files":[{id,name}], ...}
#   GET newapi/api/FileStorage/Download?id=<fileId> → бинарь файла
AUCTION_GET_URL   = f"{BASE_URL}/newapi/api/Auction/Get"
FILE_DOWNLOAD_URL = f"{BASE_URL}/newapi/api/FileStorage/Download"


def _auction_id(purchase_number: str, url: str = "") -> str:
    """Числовой id котировочной сессии из url (/auction/<id>) или из
    purchase_number ('MOS-Auction10253141' → 10253141)."""
    m = re.search(r"/auction/(\d+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"(\d{4,})", purchase_number or "")
    return m.group(1) if m else ""


def _get_auction(auction_id: str, retries: int = 3) -> Optional[dict[str, Any]]:
    for attempt in range(retries):
        try:
            resp = requests.get(AUCTION_GET_URL, params={"auctionId": auction_id},
                                headers=HEADERS, timeout=25, proxies=config.source_proxies())
            if resp.status_code == 200:
                return resp.json()
            logger.warning("ПП Москвы Auction/Get %s HTTP %s", auction_id, resp.status_code)
        except (requests.RequestException, ValueError) as e:
            logger.warning("ПП Москвы Auction/Get ошибка (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _download_file(file_id: Any, retries: int = 3) -> Optional[bytes]:
    for attempt in range(retries):
        try:
            resp = requests.get(FILE_DOWNLOAD_URL, params={"id": file_id},
                                headers=HEADERS, timeout=40, proxies=config.source_proxies())
            if resp.status_code == 200 and resp.content:
                return resp.content
            logger.info("ПП Москвы файл %s HTTP %s", file_id, resp.status_code)
        except requests.RequestException as e:
            logger.warning("ПП Москвы скачивание %s ошибка (попытка %d): %s", file_id, attempt + 1, e)
        _sleep(attempt + 1)
    return None


def download_documents(purchase_number: str, target_dir=None, url: str = "") -> list:
    """Скачивает документы котировочной сессии (ТЗ, проект контракта, описание)
    в target_dir. Возвращает список сохранённых Path. Пустой список — не ошибка
    (у сессии может не быть вложений или id не извлёкся)."""
    from pathlib import Path
    from document_processor import safe_filename

    auction_id = _auction_id(purchase_number, url)
    if not auction_id:
        logger.info("ПП Москвы %s: не извлёк id аукциона (url=%s)", purchase_number, url)
        return []

    detail = _get_auction(auction_id)
    # files — основные документы, licenseFiles — лицензионные (если есть).
    files = list((detail or {}).get("files") or []) + list((detail or {}).get("licenseFiles") or [])
    if not files:
        logger.info("ПП Москвы %s: документов на карточке нет", purchase_number)
        return []

    target_dir = Path(target_dir) if target_dir else (config.DOCUMENTS_DIR / safe_filename(purchase_number))
    target_dir.mkdir(parents=True, exist_ok=True)

    saved: list = []
    seen_ids: set = set()
    for f in files:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if fid in (None, "") or fid in seen_ids:
            continue
        seen_ids.add(fid)
        content = _download_file(fid)
        if not content:
            continue
        name = str(f.get("name") or f"file_{fid}").strip()
        path = target_dir / (safe_filename(name) or f"file_{fid}")
        path.write_bytes(content)
        saved.append(path)
        logger.info("ПП Москвы: скачан документ %s (%d байт)", name, len(content))
        _sleep(0.3)
    return saved


if __name__ == "__main__":
    # Самотест (с российского IP): python -m sources.mos_supplier "сайт"
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    kw = sys.argv[1] if len(sys.argv) > 1 else "сайт"
    rows = search_mos(kw, pages=2)
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
