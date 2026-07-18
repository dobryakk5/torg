"""
sources/sberb2b.py — коннектор к витрине публичных заявок SberB2B (sberb2b.ru).

API снят с cURL из DevTools пользователя:

    POST https://sberb2b.ru/request/get-public-requests
    Заголовки: Content-Type: application/json + X-Requested-With: XMLHttpRequest
    Кука: SFSESSID (обычная PHP/Symfony-сессия).
    Тело: полнотекстовый like_concat по r.number/r.name/customer.shortName/inn,
          greater: total_catalogue_price, where: status = receiving_kp,
          extra.subjectDomain — предполагаемый фильтр по региону (поддомену).

⚠️  ПРО СЕССИЮ (замечание разработчика):
    SFSESSID похож на обычный сессионный id, поэтому сначала пробуем добыть его
    автоматически простым GET (см. _bootstrap_session). Если сайт требует
    JS/анти-бот — вставьте свежий SBERB2B_SESSION_ID из DevTools в /control/.env.
    Гипотеза про subjectDomain ("rostov", "novosibirsk"…) не подтверждена —
    пустое значение = все регионы; задаётся через SBERB2B_SUBJECT_DOMAIN.

Структура ответа на момент написания НЕ зафиксирована, поэтому маппинг
защитный: берёт первое подходящее из синонимичных имён полей (в т.ч. вложенных
r./customer_company.), а если не смог собрать ни одной карточки — печатает в
лог сырой первый item. Самотест: python -m sources.sberb2b "сайт"

purchase_number = "SBER-<number|id>". Доступно только с российских IP.
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

API_URL = "https://sberb2b.ru/request/get-public-requests"
BASE_URL = "https://sberb2b.ru"
BOOTSTRAP_URL = "https://sberb2b.ru/request/public-requests"
PAGE_SIZE = 10

_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")
_RU_DT_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _sleep(multiplier: float = 1.0) -> None:
    time.sleep(max(0.4, config.REQUEST_DELAY * multiplier + random.uniform(0.2, 0.9)))


def _to_local_dt(raw: Any) -> str:
    """ISO «2026-07-16 20:59…»/«…T…» или «16.07.2026 20:59» → «16.07.2026 20:59»."""
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
    """Первое непустое значение из синонимичных полей (в т.ч. с точкой в имени:
    'r.name' пробуется и как 'r.name', и как вложенное item['r']['name'].
    Регистр не важен."""
    lowered = {str(k).lower(): v for k, v in item.items()}
    for name in names:
        low = name.lower()
        if low in lowered and lowered[low] not in (None, "", []):
            return lowered[low]
        if "." in low:
            head, tail = low.split(".", 1)
            nested = lowered.get(head)
            if isinstance(nested, dict):
                v = _pick(nested, tail, default=None)
                if v not in (None, "", []):
                    return v
    return default


def _bootstrap_session() -> Optional[str]:
    """Пробует получить SFSESSID простым GET (без DevTools). Может не сработать,
    если сайт требует JS/анти-бот — тогда вернёт None."""
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA})
        s.get(BOOTSTRAP_URL, timeout=20, proxies=config.source_proxies())
        return s.cookies.get("SFSESSID")
    except requests.RequestException as e:
        logger.warning("SberB2B: не удалось добыть SFSESSID автоматически: %s", e)
        return None


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": BOOTSTRAP_URL,
        "User-Agent": _UA,
        "X-Requested-With": "XMLHttpRequest",
    }


def _request_body(keyword: str, price_from: int | None, page: int, size: int,
                  subject_domain: str) -> dict[str, Any]:
    """Тело как в живом запросе фронтенда."""
    select_by: dict[str, Any] = {
        "like_concat": {
            "r_name": [
                keyword or "",
                [
                    {"name": "r.number", "is_varchar": False},
                    {"name": "r.name", "is_varchar": True},
                    {"name": "customer_company.shortName", "is_varchar": True},
                    {"name": "customer_company.inn", "is_varchar": True},
                ],
            ]
        },
        "where": {"r_public_request_status": "receiving_kp"},
    }
    if price_from:
        select_by["greater"] = {"total_catalogue_price": price_from}
    return {
        "orderBy": {"r_published_at": "desc"},
        "selectBy": select_by,
        "pagination": {"page": page, "size": size},
        "extra": {"subjectDomain": subject_domain or ""},
    }


def _post(body: dict[str, Any], session_id: str, retries: int = 3) -> Optional[Any]:
    cookies = {"SFSESSID": session_id}
    for attempt in range(retries):
        try:
            resp = requests.post(
                API_URL, json=body, headers=_headers(), cookies=cookies,
                timeout=25, proxies=config.source_proxies(),
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("SberB2B HTTP %s: %s", resp.status_code, resp.text[:200])
            if resp.status_code in (401, 403):
                logger.warning("SberB2B %s — вероятно протух SFSESSID; возьмите свежий",
                               resp.status_code)
                return None
        except (requests.RequestException, ValueError) as e:
            logger.warning("SberB2B ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _extract_items(data: Any) -> list[dict]:
    """Достаёт массив заявок из ответа, не полагаясь на точное имя обёртки."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "Items", "requests", "Requests", "rows", "Rows",
                    "data", "Data", "result", "Result", "list", "List"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        for v in data.values():
            if isinstance(v, dict):
                inner = _extract_items(v)
                if inner:
                    return inner
    return []


def _map_item(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    # Реальная структура item (снято живьём через прокси 18.07.2026):
    #   number, id(guid), name, customer{short_name,inn?}, need_condition{total_price},
    #   published_at, send_kp_until_at, public_request_status.
    uid = _pick(item, "number", "r.number", "r_number", "id", "r.id", "r_id")
    title = str(_pick(item, "name", "r.name", "r_name", "title", default="")).strip()
    if uid in (None, "") or not title:
        return None

    # customer — вложенный объект {short_name, inn, ...}; держим и старые синонимы.
    customer = _pick(item, "customer.short_name", "customer.shortName", "customer.name",
                     "customer_company.shortName", "customer_company.name",
                     "customerName", "shortName", default="")
    if isinstance(customer, dict):
        customer = (customer.get("short_name") or customer.get("shortName")
                    or customer.get("name") or "")
    customer = str(customer).strip()

    inn = str(_pick(item, "customer.inn", "customer_company.inn", "customerInn",
                    "inn", default="") or "")

    price = _pick(item, "need_condition.total_price", "total_catalogue_price",
                  "totalCataloguePrice", "price", "sum", "totalPrice")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is not None and price <= 0:
        price = None

    deadline = _to_local_dt(_pick(
        item, "send_kp_until_at", "r.receiving_kp_end", "receiving_kp_end",
        "r_receiving_kp_end", "endDate", "dateEnd",
    ))
    published = _to_local_dt(_pick(
        item, "published_at", "r.published_at", "r_published_at",
        "publishDate", "createDate",
    ))

    primary_text = "\n".join(filter(None, [
        title,
        customer,
        f"Номер: {uid}",
        "Публичная заявка (сбор КП)",
        "Закупка малого объёма (SberB2B, sberb2b.ru)",
    ]))[:4000]

    return {
        "purchase_number": f"SBER-{uid}",
        "title": title[:500],
        "customer": customer[:300],
        "customer_inn": inn,
        "price": price,
        "law_type": "ЗМО",
        "deadline": deadline,
        "published_at": published,
        "url": f"{BASE_URL}/request/public-requests/{uid}",
        "platform": "SberB2B",
        "primary_text": primary_text,
    }


def _resolve_session_id() -> Optional[str]:
    session_id = str(getattr(config, "SBERB2B_SESSION_ID", "") or "").strip()
    if session_id:
        return session_id
    logger.info("SberB2B: нет SBERB2B_SESSION_ID — пробую добыть SFSESSID автоматически")
    return _bootstrap_session()


def search_sberb2b(
    keyword: str,
    price_from: int | None = None,
    price_to: int | None = None,   # у API нет верхней границы; для совместимости
    pages: int = 1,
    days_back: int | None = None,  # для совместимости с сигнатурой других каналов
) -> list[dict]:
    """Ищет активные публичные заявки SberB2B (статус «сбор КП») по ключевому слову.

    Пытается использовать SBERB2B_SESSION_ID, иначе — bootstrap SFSESSID.
    Если сессии нет — возвращает [] (канал не роняет весь Stage 1).
    Возвращает список dict в формате scraper._parse_card.
    """
    session_id = _resolve_session_id()
    if not session_id:
        logger.warning(
            "SberB2B пропущен: нет SFSESSID (задайте SBERB2B_SESSION_ID из "
            "DevTools в /control или .env)"
        )
        return []

    subject_domain = str(getattr(config, "SBERB2B_SUBJECT_DOMAIN", "") or "").strip()

    tenders: list[dict] = []
    seen: set[str] = set()
    raw_logged = False

    for page in range(1, pages + 1):
        logger.info("SberB2B ищу '%s', страница %d", keyword, page)
        data = _post(_request_body(keyword, price_from, page, PAGE_SIZE, subject_domain), session_id)
        if data is None:
            break

        items = _extract_items(data)
        if not items:
            if page == 1 and not raw_logged:
                logger.info("SberB2B: пустая выдача; верхние ключи ответа: %s",
                            list(data.keys())[:10] if isinstance(data, dict) else type(data))
            break

        added = 0
        for item in items:
            card = _map_item(item)
            if not card:
                if not raw_logged:
                    raw_logged = True
                    logger.warning("SberB2B: не смог смаппить item — сырой JSON:\n%s",
                                   json.dumps(item, ensure_ascii=False)[:1500])
                continue
            if card["purchase_number"] in seen:
                continue
            seen.add(card["purchase_number"])
            card["matched_keywords"] = [f"sberb2b:{keyword}"]
            tenders.append(card)
            added += 1

        if added == 0 or len(items) < PAGE_SIZE:
            break
        _sleep()

    logger.info("SberB2B '%s' → %d закупок", keyword, len(tenders))
    return tenders


if __name__ == "__main__":
    # Самотест (с российского IP): python -m sources.sberb2b "сайт"
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    kw = sys.argv[1] if len(sys.argv) > 1 else "сайт"

    sid = _resolve_session_id()
    if sid:
        raw = _post(_request_body(kw, 10000, 1, PAGE_SIZE, ""), sid)
        items = _extract_items(raw)
        if items:
            print("── Сырой первый item (ключи и значения, обрезано) ──")
            for k, v in items[0].items():
                print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:100]}")
        elif isinstance(raw, dict):
            print("Ответ без распознанных items; верхние ключи:", list(raw.keys())[:15])

    rows = search_sberb2b(kw, price_from=10000, pages=1)
    print(f"\nНайдено: {len(rows)}")
    for r in rows[:8]:
        print("─" * 60)
        print("номер  :", r["purchase_number"])
        print("назв.  :", r["title"][:100])
        print("заказ. :", r["customer"][:80] or "—")
        print("опубл. :", r["published_at"] or "—", "| до:", r["deadline"] or "—")
        print("НМЦК   :", r["price"] or "—")
        print("url    :", r["url"])
