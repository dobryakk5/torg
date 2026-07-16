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

import json
import logging
import random
import re
import time
from typing import Any, Optional

import requests

import config

logger = logging.getLogger(__name__)

API_URL = "https://tender-cache-api.agregatoreat.ru/api/TradeLot/list-published-trade-lots"
# Детальная карточка лота: полная спецификация + условия поставки (как на сайте
# в блоках «Спецификация» / «Условия поставки»). GUID = поле id из списка.
API_DETAIL_URL = "https://tender-cache-api.agregatoreat.ru/api/TradeLot/{id}"
CARD_URL = "https://agregatoreat.ru/purchases/announcement/{id}"
PAGE_SIZE = 50          # у API есть нижний предел size (size=1 даёт 400 "Size is invalid")
LOT_STATE_ACCEPTING = 2

# Анти-бот сверяет отпечаток запроса с браузером, которому выдал куки
# (__rhash_/__hash_/__lhash_), поэтому заголовки копируют реальный Chrome на macOS.
# Если куки взяты из другого браузера — задай его User-Agent в EAT_USER_AGENT.
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

def _headers() -> dict[str, str]:
    ua = str(getattr(config, "EAT_USER_AGENT", "") or "").strip() or _DEFAULT_UA
    chrome_m = re.search(r"Chrome/(\d+)", ua)
    major = chrome_m.group(1) if chrome_m else "149"
    platform = '"macOS"' if "Mac" in ua else '"Windows"'
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": "https://agregatoreat.ru",
        "Referer": "https://agregatoreat.ru/",
        "sec-ch-ua": f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
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


def _via_curl(url: str, headers: dict[str, str], body: dict[str, Any] | None = None) -> Optional[Any]:
    """Фолбэк через системный curl: у него другой TLS-отпечаток, чем у python-requests,
    и анти-бот, сверяющий TLS-fingerprint, его обычно пропускает (как браузерный cURL).
    body=None → GET, иначе POST."""
    import shutil
    import subprocess

    curl_bin = shutil.which("curl")
    if not curl_bin:
        return None

    cmd = [curl_bin, "-s", "--max-time", "30", url]
    for key, value in headers.items():
        if key.lower() == "cookie":
            cmd += ["-b", value]
        else:
            cmd += ["-H", f"{key}: {value}"]
    if body is not None:
        cmd += ["--data-raw", json.dumps(body, ensure_ascii=False)]

    try:
        out = subprocess.run(cmd, capture_output=True, timeout=40).stdout
        text = out.decode("utf-8", errors="ignore").strip()
        if text.startswith("{") or text.startswith("["):
            logger.info("ЕАТ: ответ получен через curl-фолбэк")
            return json.loads(text)
        logger.warning("ЕАТ curl-фолбэк: не-JSON ответ (%s...)", text[:80])
    except Exception as e:
        logger.warning("ЕАТ curl-фолбэк не сработал: %s", e)
    return None


def _request(url: str, body: dict[str, Any] | None = None, retries: int = 3) -> Optional[Any]:
    """GET (body=None) или POST к API ЕАТ: куки, браузерные заголовки, ретраи,
    curl-фолбэк при отказе анти-бота."""
    cookie = str(getattr(config, "EAT_COOKIE", "") or "").strip()
    headers = _headers()
    if cookie:
        headers["Cookie"] = cookie

    curl_tried = False
    for attempt in range(retries):
        try:
            if body is None:
                resp = requests.get(url, headers=headers, timeout=25)
            else:
                resp = requests.post(url, json=body, headers=headers, timeout=25)
            ctype = resp.headers.get("content-type", "").lower()
            text_head = (resp.text or "")[:300].lower()
            if resp.status_code == 200 and "json" in ctype:
                return resp.json()
            # Анти-бот отдаёт либо страницу капчи, либо JS-челлендж (HTML вместо
            # JSON) — значит requests не прошёл. Пробуем тот же запрос системным curl.
            if "captcha" in text_head or resp.status_code == 200:
                if not curl_tried:
                    curl_tried = True
                    logger.info("ЕАТ: анти-бот отклонил python-requests, пробую системный curl")
                    data = _via_curl(url, headers, body)
                    if data is not None:
                        return data
                logger.warning(
                    "ЕАТ: анти-бот не пропустил запрос (HTTP %s, content-type=%s). "
                    "Обнови куки: пройди проверку в браузере на agregatoreat.ru и "
                    "положи свежие __rhash_/__hash_/__lhash_ в EAT_COOKIE (.env). "
                    "Если куки из другого браузера — задай его UA в EAT_USER_AGENT.",
                    resp.status_code, ctype or "—",
                )
                return None
            logger.warning("ЕАТ HTTP %s: %s", resp.status_code, resp.text[:150])
        except (requests.RequestException, ValueError) as e:
            logger.warning("ЕАТ ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _post(body: dict[str, Any], retries: int = 3) -> Optional[dict[str, Any]]:
    return _request(API_URL, body=body, retries=retries)


def get_lot_details(guid: str) -> Optional[dict[str, Any]]:
    """Полная карточка лота (GET /api/TradeLot/<guid>) — источник блоков
    «Спецификация» и «Условия поставки» на сайте. Работает по тем же кукам,
    что и список; авторизация (Bearer) не требуется для опубликованных лотов."""
    guid = str(guid or "").strip()
    if not guid:
        return None
    data = _request(API_DETAIL_URL.format(id=guid))
    return data if isinstance(data, dict) else None


def _pick(obj: dict, *names, default=None):
    """Первое непустое значение из синонимичных полей (регистр не важен)."""
    if not isinstance(obj, dict):
        return default
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for name in names:
        v = lowered.get(name.lower())
        if v not in (None, "", []):
            return v
    return default


def _format_spec(item: dict[str, Any]) -> list[str]:
    """Спецификация из lotItems: наименование, ОКПД2/КТРУ, кол-во, ед., цена, страна.

    Тот же контент, что в блоке «Спецификация» карточки на сайте ЕАТ.
    """
    lines: list[str] = []
    for li in (item.get("lotItems") or [])[:20]:
        if not isinstance(li, dict):
            continue
        name = str(_pick(li, "name", "itemName", "productName", default="")).strip()
        if not name:
            continue
        parts = [name]
        code = _pick(li, "okpd2Code", "ktruCode", "code")
        if code:
            parts.append(f"[ОКПД2/КТРУ {code}]")
        qty = _pick(li, "quantity", "count", "amount")
        unit = _pick(li, "okeiTitle", "okeiName", "unitName", "measure", "okeiSymbol")
        if qty is not None:
            parts.append(f"кол-во {qty}" + (f" {unit}" if unit else ""))
        unit_price = _pick(li, "unitPrice", "price", "startPrice", "cost")
        if unit_price is not None:
            parts.append(f"цена {unit_price} ₽")
        country = _pick(li, "countryOfOrigin", "countryName", "country", "originCountry")
        if country:
            parts.append(f"страна: {country}")
        # Уточняющее описание позиции (адрес ресурса, детали) — полезно для LLM.
        desc = str(_pick(li, "description", default="")).strip()
        if desc and desc.lower() not in name.lower():
            parts.append(desc)
        lines.append(" · ".join(str(p) for p in parts))
    return lines


def _format_delivery(item: dict[str, Any]) -> list[str]:
    """Условия поставки из deliveryInfos / полей верхнего уровня: адрес, регион, сроки."""
    lines: list[str] = []
    infos = item.get("deliveryInfos") or item.get("deliveryInfo") or []
    if isinstance(infos, dict):
        infos = [infos]
    for di in (infos or [])[:5]:
        if not isinstance(di, dict):
            continue
        # Адрес может быть вложенным объектом (address/deliveryAddress) с готовым
        # полем formattedFullInfo, либо развёрнут прямо в di.
        addr_obj = _pick(di, "address", "deliveryAddress", "fullAddress")
        if isinstance(addr_obj, dict):
            addr = str(_pick(addr_obj, "formattedFullInfo", "fullAddress", "address", default="")).strip()
            region = str(_pick(addr_obj, "regionName", "region", default="")).strip()
        else:
            addr = str(_pick(di, "formattedFullInfo", "fullAddress", default=addr_obj or "")).strip()
            region = str(_pick(di, "regionName", "region", "deliveryRegion", default="")).strip()
        # formattedFullInfo уже включает регион — не дублируем.
        piece = addr or region
        if piece:
            lines.append(piece)
    # Сроки поставки/оказания услуг (часто на верхнем уровне item).
    d_start = _iso_to_local_fmt(_pick(item, "deliveryDateStart", "deliveryStartDate"))
    d_end = _iso_to_local_fmt(_pick(item, "deliveryDate", "deliveryDateEnd", "deliveryEndDate"))
    period = _pick(item, "deliveryPeriod")
    if d_start or d_end:
        lines.append("Срок: " + " – ".join(x for x in (d_start, d_end) if x))
    elif period:
        unit = "раб. дн." if item.get("isDeliveryDaysWorking") else "дн."
        lines.append(f"Срок поставки: {period} {unit}")
    return lines


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

    spec_lines = _format_spec(item)
    delivery_lines = _format_delivery(item)

    title = subject or (spec_lines[0].split(" · ")[0] if spec_lines else "")
    if not title:
        return None

    guarantee = item.get("applicationGuarantee")

    spec_block = ""
    if spec_lines:
        spec_block = "Спецификация:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(spec_lines, 1))
    delivery_block = ""
    if delivery_lines:
        delivery_block = "Условия поставки:\n" + "\n".join(f"  {d}" for d in delivery_lines)

    primary_text = "\n".join(filter(None, [
        title,
        customer,
        f"Номер ЕАТ: {trade_number}",
        f"НМЦК: {price} ₽" if price else "",
        f"Обеспечение заявки: {guarantee} ₽" if guarantee else "",
        spec_block,
        delivery_block,
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

        fetch_details = bool(getattr(config, "EAT_FETCH_DETAILS", True))
        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            # Детальная карточка даёт полную спецификацию и условия поставки;
            # совпадений на ключ обычно единицы, поэтому один лишний GET на лот
            # не напрягает ни нас, ни площадку.
            if fetch_details and item.get("id"):
                detail = get_lot_details(item["id"])
                if detail:
                    item = {**item, **{k: v for k, v in detail.items() if v not in (None, "", [])}}
                _sleep(0.3)
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


def find_file_refs(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    """Рекурсивно ищет в структуре всё, что похоже на ссылку/описание файла:
    ключи с file/document/attachment/contract или значения с расширением документа.

    Разведочный инструмент: по его выводу пишется скачивание документов лота.
    """
    hits: list[tuple[str, Any]] = []
    key_re = re.compile(r"file|document|attach|contract|scan|blob", re.I)
    val_re = re.compile(r"\.(docx?|pdf|xlsx?|rtf|odt|zip|rar)\b", re.I)

    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)):
                if key_re.search(str(k)) and v:
                    hits.append((p, v))
                hits.extend(find_file_refs(v, p))
            elif v not in (None, "", []):
                if key_re.search(str(k)) or val_re.search(str(v)):
                    hits.append((p, v))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:10]):
            hits.extend(find_file_refs(v, f"{path}[{i}]"))
    return hits


if __name__ == "__main__":
    # Самотест:         python -m sources.eat "сайт"
    # Сырой дамп полей: python -m sources.eat "сайт" --raw
    # Поиск документов: python -m sources.eat --docs <guid лота из URL карточки>
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    kw = args[0] if args else "программное обеспечение"

    if "--docs" in sys.argv:
        guid = args[0] if args else ""
        if not guid:
            print("Укажи GUID лота: python -m sources.eat --docs 2c177788-3374-4094-8ef7-0d0968c256b2")
            sys.exit(2)
        detail = get_lot_details(guid)
        if not detail:
            print("Детальная карточка не получена (антибот/куки?)")
            sys.exit(1)
        print("── Верхние ключи детали ──"); print(", ".join(detail.keys()))
        trade = detail.get("trade") if isinstance(detail.get("trade"), dict) else {}
        if trade:
            print("\n── Ключи trade ──"); print(", ".join(trade.keys()))
        lot = trade.get("lot") if isinstance(trade.get("lot"), dict) else {}
        docs = lot.get("documents") if isinstance(lot, dict) else None
        if docs:
            print(f"\n── trade.lot.documents (полностью, {len(docs)} шт.) ──")
            print(json.dumps(docs, ensure_ascii=False, indent=1))
        print("\n── Похожее на файлы/документы (путь → значение) ──")
        hits = find_file_refs(detail)
        if not hits:
            print("  (ничего не найдено)")
        for p, v in hits[:40]:
            s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            print(f"  {p} = {s[:300]}")
        sys.exit(0)

    if "--raw" in sys.argv:
        # size=1 API отвергает ("The field Size is invalid") — берём валидный размер
        data = _post(_request_body(kw, 20000, 590000, 1, 10))
        items = (data or {}).get("items") or []
        if not items:
            print("Пусто/не пропустил антибот; ответ:", str(data)[:300])
        else:
            it = items[0]
            print("── Ключи item (список) ──"); print(", ".join(it.keys()))
            print("\n── lotItems[0] из списка ──")
            print(json.dumps((it.get("lotItems") or [{}])[0], ensure_ascii=False, indent=1)[:1500])
            detail = get_lot_details(it.get("id"))
            if detail:
                print("\n── Ключи детальной карточки ──"); print(", ".join(detail.keys()))
                print("\n── lotItems[0] из деталей ──")
                print(json.dumps((detail.get("lotItems") or [{}])[0], ensure_ascii=False, indent=1)[:2000])
                print("\n── deliveryInfos из деталей ──")
                print(json.dumps(detail.get("deliveryInfos") or detail.get("deliveryInfo"),
                                 ensure_ascii=False, indent=1)[:1500])
            else:
                print("\nДетальная карточка не получена (антибот/авторизация?)")
        sys.exit(0)

    rows = search_eat(kw, price_from=20000, price_to=590000, pages=1)
    print(f"\nНайдено: {len(rows)}")
    for r in rows[:8]:
        print("─" * 60)
        print("номер  :", r["purchase_number"])
        print("назв.  :", r["title"][:100])
        print("заказ. :", r["customer"][:80] or "—")
        print("опубл. :", r["published_at"] or "—", "| до:", r["deadline"] or "—")
        print("НМЦК   :", r["price"] or "—", "| обесп.:", r.get("application_security_amount", "—"))
        print("primary_text:\n   " + (r["primary_text"].replace("\n", "\n   ")))
        print("url    :", r["url"])
