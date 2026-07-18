"""
sources/rts_tender.py — коннектор к витрине ЗМО РТС-Тендер
(market.rts-tender.ru → zmo-new-webapi.rts-tender.ru).

API снят с cURL из DevTools пользователя:

    POST https://zmo-new-webapi.rts-tender.ru/market/api/v1/trades/publicsearch2
    Заголовки: Content-Type: application/json + ОБЯЗАТЕЛЬНЫЕ:
        "XXX-TenantId-Header: 132"  — раздел/регион витрины,
        "token: <...>"              — токен сессии.
    Тело: {"Paging":..., "Filtering":[{KeyWords}, {MinPrice}, {MaxPrice}, ...]}.

⚠️  ВАЖНО ПРО СЕССИЮ (живая проверка 18.07.2026):
    1. Сессия ПРИВЯЗАНА К IP: валидные token+куки с другого IP дают
       400 «Повторите запрос через 5 секунд» (перепробованы: точная копия
       запроса, Chrome TLS-отпечаток через curl_cffi, curl CLI, повторы).
       Сам домен вне РФ-IP режет TLS-handshake.
    2. Заголовок `token` живёт МЕНЬШЕ МИНУТЫ вне браузера: SPA его постоянно
       перевыпускает, разовый слепок протухает почти сразу (умирал через ~30 с
       даже при постоянном использовании с «родного» IP).

    Поэтому ОСНОВНОЙ режим — управляемый headless-браузер (Playwright, как у
    ЕАТ refresh_eat_cookies): браузер ходит через SOURCES_PROXY_URL, проходит
    DDoS-Guard JS-челлендж сам (~30-60 с, один раз на процесс), сессия живёт,
    пока открыт браузер, а поиск выполняется fetch'ем ИЗ контекста страницы со
    свежеперехваченным токеном. Флаг RTS_AUTO_REFRESH=0 выключает браузер.

    ЗАПАСНОЙ режим (без playwright): статичные RTS_COOKIE_STRING/RTS_TOKEN из
    env — работает секунды, годится только для отладки одного запроса.

    TenantId 132 взят от market.rts-tender.ru, который сам агрегирует ~85
    региональных и 14 корпоративных магазинов, — то есть это, возможно, уже
    широкий срез, а не один регион. Охват не подтверждён эмпирически; можно
    перебрать несколько tenant_id через RTS_TENANT_IDS.

Маппинг полей защитный: берёт первое подходящее из синонимичных имён, а если
не смог собрать ни одной карточки — печатает в лог сырой первый item. Самотест:
    python -m sources.rts_tender "сайт"

purchase_number = "RTS-<id>".
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

try:
    # Витрина сидит за DDoS-Guard: с TLS-отпечатком Chrome шансов больше.
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

import config

logger = logging.getLogger(__name__)

API_URL = "https://zmo-new-webapi.rts-tender.ru/market/api/v1/trades/publicsearch2"
BASE_URL = "https://market.rts-tender.ru"
PAGE_SIZE = 12
DEFAULT_TENANT_ID = "132"

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


def parse_curl_cookie_string(cookie_str: str) -> dict:
    """'a=1; b=2; c=3' → {'a':'1','b':'2','c':'3'} — чтобы вставлять -b строку
    из curl целиком, без ручной разбивки."""
    cookies: dict[str, str] = {}
    for part in str(cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


# ══════════════════════════════════════════════════════════════════════════
# Управляемый браузер (основной режим): сессия живёт, пока открыт браузер.
# Один браузер на процесс, ленивый запуск при первом поиске, закрытие atexit.
# ══════════════════════════════════════════════════════════════════════════

SEARCH_PAGE_URL = (
    BASE_URL + "/search/sell?s=4&q=%D1%81%D0%B0%D0%B9%D1%82"
    "&sts=1&sts=2&smp=0&dsoswo=false&isHomeReg=false&tst=0&sort=-PublicationDate"
)

_browser: dict[str, Any] = {"pw": None, "browser": None, "page": None, "token": "", "dead": False}


def _browser_start() -> bool:
    """Ленивый запуск headless-браузера через прокси + прохождение челленджа.

    Возвращает True, когда SPA загрузился и токен перехвачен. Повторные вызовы
    после неудачи не пытаются снова (флаг dead) — иначе каждый keyword будет
    ждать челлендж заново.
    """
    if _browser["page"] is not None:
        return True
    if _browser["dead"] or not bool(getattr(config, "RTS_AUTO_REFRESH", True)):
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("РТС-Тендер: playwright не установлен — браузерный режим недоступен")
        _browser["dead"] = True
        return False

    from refresh_rts_session import _playwright_proxy

    logger.info("РТС-Тендер: запускаю браузер и прохожу анти-бот (~30-60 с)…")
    try:
        pw = sync_playwright().start()
        browser = None
        launch_args = ["--disable-blink-features=AutomationControlled"]
        for launch_kwargs in ({"channel": "chrome", "headless": True}, {"headless": True}):
            try:
                browser = pw.chromium.launch(proxy=_playwright_proxy(), args=launch_args,
                                             **launch_kwargs)
                break
            except Exception:
                continue
        if browser is None:
            logger.warning("РТС-Тендер: не удалось запустить Chromium/Chrome")
            pw.stop()
            _browser["dead"] = True
            return False

        context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.new_page()

        def on_request(request) -> None:
            if "zmo-new-webapi.rts-tender.ru" in request.url:
                token = request.headers.get("token", "")
                if token:
                    _browser["token"] = token

        page.on("request", on_request)

        # Челлендж может закончиться сетевой ошибкой перезагрузки (прокси
        # флапает) — повторный goto проходит уже с выданными куками.
        deadline = time.time() + 150
        while time.time() < deadline and not _browser["token"]:
            try:
                page.goto(SEARCH_PAGE_URL, wait_until="domcontentloaded", timeout=45_000)
            except Exception as e:
                logger.info("РТС-Тендер: goto: %s", str(e)[:100])
            while time.time() < deadline and not _browser["token"]:
                body_head = ""
                try:
                    body_head = page.evaluate("document.body.innerText.slice(0,120)")
                except Exception:
                    pass
                if "Не удается" in body_head or "ERR_" in body_head:
                    break   # страница-ошибка Chrome → новый goto
                page.wait_for_timeout(1000)

        if not _browser["token"]:
            logger.warning("РТС-Тендер: анти-бот не пройден за отведённое время")
            browser.close()
            pw.stop()
            _browser["dead"] = True
            return False

        _browser.update({"pw": pw, "browser": browser, "page": page})
        import atexit
        atexit.register(_browser_stop)
        logger.info("РТС-Тендер: браузерная сессия готова")
        return True
    except Exception as e:
        logger.warning("РТС-Тендер: сбой запуска браузера: %s", e)
        _browser["dead"] = True
        return False


def _browser_stop() -> None:
    try:
        if _browser["browser"] is not None:
            _browser["browser"].close()
        if _browser["pw"] is not None:
            _browser["pw"].stop()
    except Exception:
        pass
    _browser.update({"pw": None, "browser": None, "page": None})


def _browser_fetch(body: dict[str, Any], tenant_id: str) -> Optional[Any]:
    """POST publicsearch2 fetch'ем ИЗ контекста страницы браузера.

    Куки идут браузерные, токен — последний перехваченный у SPA. Если сервер
    отверг (токен успел протухнуть), перезагружаем страницу (SPA возьмёт свежий
    токен) и повторяем один раз.
    """
    if not _browser_start():
        return None
    page = _browser["page"]
    js = """async ({url, body, token, tenant}) => {
        const r = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
                'token': token,
                'XXX-TenantId-Header': String(tenant),
            },
            body: JSON.stringify(body),
        });
        return {status: r.status, text: await r.text()};
    }"""
    for attempt in range(2):
        try:
            res = page.evaluate(js, {"url": API_URL, "body": body,
                                     "token": _browser["token"], "tenant": tenant_id})
        except Exception as e:
            logger.warning("РТС-Тендер: fetch из браузера не удался: %s", str(e)[:120])
            return None
        status = res.get("status")
        text = res.get("text", "")
        if status == 200:
            try:
                return json.loads(text)
            except ValueError:
                logger.warning("РТС-Тендер: не-JSON ответ из браузера: %s", text[:120])
                return None
        logger.info("РТС-Тендер: браузерный fetch HTTP %s (%s), попытка %d",
                    status, text[:80], attempt + 1)
        if attempt == 0:
            # Токен протух — перезагрузка страницы заставит SPA взять свежий.
            old_token = _browser["token"]
            try:
                page.reload(wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                pass
            deadline = time.time() + 30
            while time.time() < deadline and _browser["token"] == old_token:
                page.wait_for_timeout(500)
    return None


def _tenant_ids() -> list[str]:
    """Список tenant_id из config.RTS_TENANT_IDS (или один DEFAULT_TENANT_ID)."""
    raw = getattr(config, "RTS_TENANT_IDS", None)
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    ids = [str(x).strip() for x in (raw or []) if str(x).strip()]
    return ids or [DEFAULT_TENANT_ID]


def _headers(tenant_id: str, token: str, keyword: str = "") -> dict[str, str]:
    # CurrentUrl шлёт живой SPA — оставляем для совместимости с анти-ботом.
    from urllib.parse import quote
    current_url = (
        f"{BASE_URL}/search/sell?s=4&q={quote(keyword or '')}"
        "&sts=1&sts=2&smp=0&dsoswo=false&isHomeReg=false&tst=0&sort=-PublicationDate"
    )
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Content-Type": "application/json",
        "CurrentUrl": current_url,
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
        "XXX-TenantId-Header": str(tenant_id),
        "token": token,
    }


def _request_body(keyword: str, price_from: int | None, price_to: int | None,
                  page: int, size: int, days_back: int | None) -> dict[str, Any]:
    """Тело как в живом запросе SPA (снято из браузера 18.07.2026).

    Statuses [1, 2] — активные закупки (в URL живого поиска sts=1&sts=2).
    PublicationDateFrom живой SPA не шлёт; добавляем только при days_back.
    """
    filtering: list[dict[str, Any]] = [
        {"Name": "KeyWords", "Type": 0, "Value": keyword or ""},
        {"Name": "Statuses", "Type": 1, "Value": [1, 2]},
        {"Name": "SignedOutsideEShopWithoutTradeNumber", "Type": 3, "Value": False},
        {"Name": "MarketSearchAction", "Type": 1, "Value": 1},
        {"Name": "OnlyHomeRegion", "Type": 3, "Value": False},
        {"Name": "TradeSearchType", "Type": 1, "Value": 0},
    ]
    if days_back:
        date_from = (
            datetime.now(timezone.utc) - timedelta(days=max(1, days_back))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        filtering.append({"Name": "PublicationDateFrom", "Type": 4, "Value": date_from})
    if price_from:
        filtering.append({"Name": "MinPrice", "Type": 2, "Value": price_from})
    if price_to:
        filtering.append({"Name": "MaxPrice", "Type": 2, "Value": price_to})
    return {
        "Paging": {"Page": page, "ItemsPerPage": size},
        "Filtering": filtering,
        "Sorting": [{"Direction": "Descending", "Field": "PublicationDate"}],
    }


class _SessionExpired(Exception):
    """Сессия отвергнута сервером — нужен свежий token/куки (авто или вручную)."""


def _post(body: dict[str, Any], tenant_id: str, token: str, cookies: dict,
          retries: int = 3, keyword: str = "") -> Optional[Any]:
    proxies = config.source_proxies()
    for attempt in range(retries):
        try:
            if cffi_requests is not None:
                resp = cffi_requests.post(
                    API_URL, json=body, headers=_headers(tenant_id, token, keyword),
                    cookies=cookies, timeout=25, proxies=proxies,
                    impersonate="chrome",
                )
            else:
                resp = requests.post(
                    API_URL, json=body, headers=_headers(tenant_id, token, keyword),
                    cookies=cookies, timeout=25, proxies=proxies,
                )
            if resp.status_code == 200:
                return resp.json()
            body_head = resp.text[:200]
            logger.warning("РТС-Тендер HTTP %s: %s", resp.status_code, body_head)
            # Без валидной сессии сервер отвечает 401/403 либо мягким 400
            # «Повторите запрос через N секунд» (анти-бот/сессионный шлюз, снято
            # живьём через прокси). Во всех случаях помогает только свежая сессия.
            if resp.status_code in (401, 403) or (
                resp.status_code == 400 and "Повторите запрос" in body_head
            ):
                raise _SessionExpired(f"HTTP {resp.status_code}: {body_head}")
        except _SessionExpired:
            raise
        except Exception as e:   # requests и curl_cffi кидают разные исключения
            logger.warning("РТС-Тендер ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _extract_items(data: Any) -> list[dict]:
    """Достаёт массив торгов из ответа, не полагаясь на точное имя обёртки."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("Trades", "trades", "Items", "items", "Data", "data",
                    "Result", "result", "Rows", "rows"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # одноуровневая обёртка: {"...": {"Trades": [...]}}
        for v in data.values():
            if isinstance(v, dict):
                inner = _extract_items(v)
                if inner:
                    return inner
    return []


def _map_item(item: dict[str, Any], tenant_id: str) -> Optional[dict[str, Any]]:
    # Реальная структура item (снята живьём 18.07.2026): Id, Name, Price,
    # PublicationDate, FillingApplicationEndDate, ZmoFzTypeString, StateString,
    # DeliveryKladrs[{Name}], ApplicationsCount. CustomerName/OrganizerName в
    # выдаче поиска обычно null — заказчик виден только в карточке лота.
    uid = _pick(item, "Id", "id", "TradeId", "tradeId", "Number", "number",
                "TradeNumber", "tradeNumber")
    title = str(_pick(item, "Name", "name", "TradeName", "tradeName",
                      "Subject", "subject", "title", default="")).strip()
    if uid in (None, "") or not title:
        return None

    customer = _pick(item, "CustomerName", "customerName", "OrganizerName",
                     "Customer", "customer", "OrganizationName",
                     "organizationName", "CustomerFullName", default="")
    if isinstance(customer, dict):
        customer = customer.get("FullName") or customer.get("fullName") \
            or customer.get("Name") or customer.get("name") or ""
    customer = str(customer).strip()

    region = ""
    kladrs = _pick(item, "DeliveryKladrs", "deliveryKladrs", default=None)
    if isinstance(kladrs, list) and kladrs and isinstance(kladrs[0], dict):
        region = str(kladrs[0].get("Name") or kladrs[0].get("name") or "").strip()

    fz = str(_pick(item, "ZmoFzTypeString", "zmoFzTypeString", default="") or "").strip()
    state = str(_pick(item, "StateString", "stateString", default="") or "").strip()
    apps_count = _pick(item, "ApplicationsCount", "applicationsCount", default=None)

    price = _pick(item, "Price", "price", "StartPrice", "startPrice",
                  "MaxPrice", "maxPrice", "Nmck", "nmck", "Sum", "sum")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is not None and price <= 0:
        price = None

    deadline = _to_local_dt(_pick(
        item, "ApplicationEndDate", "applicationEndDate", "EndDate", "endDate",
        "FillingApplicationEndDate", "DateEnd", "dateEnd",
    ))
    published = _to_local_dt(_pick(
        item, "PublicationDate", "publicationDate", "PublishDate", "publishDate",
        "CreateDate", "BeginDate", "dateBegin",
    ))
    inn = str(_pick(item, "CustomerInn", "customerInn", "Inn", "inn", default="") or "")

    primary_text = "\n".join(filter(None, [
        title,
        customer,
        f"Номер: {uid}",
        f"Регион поставки: {region}" if region else "",
        fz,
        f"Статус: {state}" if state else "",
        f"Заявок: {apps_count}" if apps_count else "",
        "Закупка малого объёма (РТС-Тендер, market.rts-tender.ru)",
    ]))[:4000]

    card = {
        "purchase_number": f"RTS-{uid}",
        "title": title[:500],
        "customer": customer[:300],
        "customer_inn": inn,
        "price": price,
        "law_type": "ЗМО",
        "deadline": deadline,
        "published_at": published,
        "url": f"{BASE_URL}/#/trades/{uid}",
        "platform": "РТС-Тендер",
        "primary_text": primary_text,
    }
    if region:
        card["region"] = region
    return card


def search_rts(
    keyword: str,
    price_from: int | None = None,
    price_to: int | None = None,
    pages: int = 1,
    days_back: int | None = None,
) -> list[dict]:
    """Ищет активные ЗМО на витрине РТС-Тендер по ключевому слову.

    Основной режим — управляемый headless-браузер (_browser_fetch): сессия
    поддерживается открытой страницей SPA, токен всегда свежий. Запасной —
    статичные RTS_TOKEN/RTS_COOKIE_STRING из env (живут секунды, для отладки).
    Любой отказ канала не роняет Stage 1 — возвращаем то, что успели собрать.
    Перебирает tenant_id из RTS_TENANT_IDS (по умолчанию только 132).
    Возвращает список dict в формате scraper._parse_card.
    """
    use_browser = _browser_start()
    token = str(getattr(config, "RTS_TOKEN", "") or "").strip()
    cookie_string = str(getattr(config, "RTS_COOKIE_STRING", "") or "").strip()
    if not use_browser and (not token or not cookie_string):
        logger.warning(
            "РТС-Тендер пропущен: браузерный режим недоступен (playwright/капча) "
            "и нет статичных RTS_TOKEN/RTS_COOKIE_STRING"
        )
        return []

    cookies = parse_curl_cookie_string(cookie_string) if cookie_string else {}
    days = days_back if days_back is not None else getattr(config, "PUBLISH_DAYS_BACK", 5)

    tenders: list[dict] = []
    seen: set[str] = set()
    raw_logged = False

    for tenant_id in _tenant_ids():
        for page in range(1, pages + 1):
            logger.info("РТС-Тендер (tenant %s) ищу '%s', страница %d", tenant_id, keyword, page)
            body = _request_body(keyword, price_from, price_to, page, PAGE_SIZE, days)
            if use_browser:
                data = _browser_fetch(body, tenant_id)
            else:
                try:
                    data = _post(body, tenant_id, token, cookies, keyword=keyword)
                except _SessionExpired:
                    logger.warning(
                        "РТС-Тендер: статичная сессия отвергнута (token живёт <1 мин) — "
                        "установите playwright для браузерного режима"
                    )
                    return tenders
            if data is None:
                break

            items = _extract_items(data)
            if not items:
                if page == 1 and not raw_logged:
                    logger.info("РТС-Тендер: пустая выдача; верхние ключи ответа: %s",
                                list(data.keys())[:10] if isinstance(data, dict) else type(data))
                break

            added = 0
            for item in items:
                card = _map_item(item, tenant_id)
                if not card:
                    if not raw_logged:
                        raw_logged = True
                        logger.warning("РТС-Тендер: не смог смаппить item — сырой JSON:\n%s",
                                       json.dumps(item, ensure_ascii=False)[:1500])
                    continue
                if card["purchase_number"] in seen:
                    continue
                seen.add(card["purchase_number"])
                card["matched_keywords"] = [f"rts:{keyword}"]
                tenders.append(card)
                added += 1

            if added == 0 or len(items) < PAGE_SIZE:
                break
            _sleep()

    logger.info("РТС-Тендер '%s' → %d закупок", keyword, len(tenders))
    return tenders


if __name__ == "__main__":
    # Самотест: python -m sources.rts_tender "сайт"
    # (браузерный режим сам пройдёт анти-бот, нужен только playwright + прокси)
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    kw = sys.argv[1] if len(sys.argv) > 1 else "сайт"

    if _browser_start():
        raw = _browser_fetch(_request_body(kw, None, None, 1, PAGE_SIZE, None),
                             _tenant_ids()[0])
        items = _extract_items(raw)
        if items:
            print("── Сырой первый item (ключи и значения, обрезано) ──")
            for k, v in items[0].items():
                print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:100]}")
        elif isinstance(raw, dict):
            print("Ответ без распознанных items; верхние ключи:", list(raw.keys())[:15])

    rows = search_rts(kw, price_from=20000, price_to=590000, pages=1)
    print(f"\nНайдено: {len(rows)}")
    for r in rows[:8]:
        print("─" * 60)
        print("номер  :", r["purchase_number"])
        print("назв.  :", r["title"][:100])
        print("заказ. :", r["customer"][:80] or "—")
        print("опубл. :", r["published_at"] or "—", "| до:", r["deadline"] or "—")
        print("НМЦК   :", r["price"] or "—")
        print("url    :", r["url"])
