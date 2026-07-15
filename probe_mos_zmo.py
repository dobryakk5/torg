#!/usr/bin/env python3
"""
probe_mos_zmo.py — разведка API Портала поставщиков Москвы (zakupki.mos.ru)
и Электронного магазина МО (market.mosreg.ru) для будущих ЗМО-коннекторов.

Запускать С СЕРВЕРА С РОССИЙСКИМ IP (из-за рубежа обе площадки на гео-блоке):

    python3 probe_mos_zmo.py                 # обе площадки
    python3 probe_mos_zmo.py mos             # только zakupki.mos.ru
    python3 probe_mos_zmo.py mosreg          # только market.mosreg.ru

Что делает (только чтение, ничего не отправляет и не публикует):
  1. Проверяет доступность главной страницы.
  2. Скачивает JS-бандлы SPA и вытаскивает из них пути API-эндпоинтов.
  3. Пробует известные эндпоинты-кандидаты (список закупок / котировочных сессий)
     с минимальными телами запросов и печатает статус + начало ответа.
  4. Всё сырьё складывает в ./probe_zmo_out/ — эти файлы и вывод консоли
     пришли обратно, по ним пишется коннектор.

Скрипт самодостаточный: нужен только `pip install requests`.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

OUT_DIR = Path("probe_zmo_out")
DELAY = 2.0
TIMEOUT = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Пути API в JS-бандлах: '/newapi/api/...', '/api/...', полные https-URL с 'api'
_API_PATH_RE = re.compile(r"""["'](/(?:newapi/)?api/[A-Za-z0-9_/\-\.]{3,80})["']""")
_API_URL_RE = re.compile(r"""["'](https://[a-z0-9\.\-]*api[a-z0-9\.\-]*\.[a-z]{2,6}[^"']{0,80})["']""")
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)


def _shorten(text: str, n: int = 300) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:n] + ("…" if len(text) > n else "")


def _save(name: str, content: str | bytes) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _get(url: str, **kw) -> requests.Response | None:
    try:
        return requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        print(f"    GET {url} → ОШИБКА: {e}")
        return None


def _post_json(url: str, body: dict) -> requests.Response | None:
    try:
        return requests.post(
            url, json=body, timeout=TIMEOUT,
            headers={**HEADERS, "Content-Type": "application/json"},
        )
    except requests.RequestException as e:
        print(f"    POST {url} → ОШИБКА: {e}")
        return None


def discover_api_paths(base: str, html: str, max_js: int = 12) -> set[str]:
    """Качает JS-бандлы страницы и собирает упомянутые в них API-пути."""
    found: set[str] = set()
    found.update(_API_PATH_RE.findall(html))
    found.update(_API_URL_RE.findall(html))

    scripts = _SCRIPT_SRC_RE.findall(html)[:max_js]
    print(f"  JS-бандлов на странице: {len(scripts)} (качаю до {max_js})")
    for i, src in enumerate(scripts):
        url = src if src.startswith("http") else base.rstrip("/") + "/" + src.lstrip("/")
        resp = _get(url)
        time.sleep(0.5)
        if not resp or resp.status_code != 200:
            continue
        js = resp.text
        hits = set(_API_PATH_RE.findall(js)) | set(_API_URL_RE.findall(js))
        if hits:
            print(f"    {src[:60]} → {len(hits)} api-путей")
            found.update(hits)
    return found


def try_endpoint(url: str, bodies: list[dict] | None = None) -> None:
    """GET + серия POST-кандидатов; печатает и сохраняет результат."""
    tag = re.sub(r"[^A-Za-z0-9]+", "_", url)[-60:]

    resp = _get(url)
    if resp is not None:
        ct = resp.headers.get("content-type", "")
        print(f"    GET  {url}\n         → HTTP {resp.status_code} [{ct.split(';')[0]}] {_shorten(resp.text, 200)}")
        _save(f"GET_{tag}.txt", resp.text[:200_000])
    time.sleep(DELAY)

    for body in bodies or []:
        resp = _post_json(url, body)
        if resp is None:
            continue
        ct = resp.headers.get("content-type", "")
        print(f"    POST {url}\n         body={json.dumps(body, ensure_ascii=False)[:120]}\n         → HTTP {resp.status_code} [{ct.split(';')[0]}] {_shorten(resp.text, 300)}")
        _save(f"POST_{tag}_{abs(hash(str(body))) % 10000}.txt", resp.text[:200_000])
        time.sleep(DELAY)


def probe_mos() -> None:
    print("\n" + "═" * 70)
    print("ПОРТАЛ ПОСТАВЩИКОВ МОСКВЫ — zakupki.mos.ru")
    print("═" * 70)

    base = "https://zakupki.mos.ru"
    resp = _get(base + "/")
    if resp is None or resp.status_code != 200:
        print(f"  Главная НЕДОСТУПНА (HTTP {resp.status_code if resp else '—'}) — дальше нет смысла")
        return
    title = re.search(r"<title>([^<]*)</title>", resp.text, re.I)
    print(f"  Главная OK: HTTP 200, title={title.group(1).strip() if title else '?'}")
    _save("mos_index.html", resp.text)

    paths = discover_api_paths(base, resp.text)
    _save("mos_api_paths.txt", "\n".join(sorted(paths)))
    print(f"  Найдено api-путей в бандлах: {len(paths)} (полный список в probe_zmo_out/mos_api_paths.txt)")
    interesting = sorted(p for p in paths if re.search(r"auction|need|tender|purchase|quotation|sku|offer", p, re.I))
    for p in interesting[:25]:
        print(f"    · {p}")

    # Известные кандидаты (публичный список котировочных сессий).
    # 19000002 — статус «Активна» (проверить по факту), фильтр по названию — nameLike.
    print("\n  Пробую известные эндпоинты:")
    try_endpoint(
        base + "/newapi/api/Auction/GetAuctionListForPublic",
        bodies=[
            {"filter": {}, "take": 5, "skip": 0},
            {
                "filter": {"nameLike": "сайт", "auctionSpecificFilter": {"stateIdIn": [19000002]}},
                "take": 5, "skip": 0,
                "order": [{"field": "relevance", "desc": True}],
            },
        ],
    )
    try_endpoint(
        base + "/newapi/api/Need/GetNeedListForPublic",
        bodies=[{"filter": {}, "take": 5, "skip": 0}],
    )


def probe_mosreg() -> None:
    print("\n" + "═" * 70)
    print("ЭЛЕКТРОННЫЙ МАГАЗИН МОСКОВСКОЙ ОБЛАСТИ — market.mosreg.ru")
    print("═" * 70)

    base = "https://market.mosreg.ru"
    resp = _get(base + "/")
    if resp is None or resp.status_code != 200:
        print(f"  Главная НЕДОСТУПНА (HTTP {resp.status_code if resp else '—'}) — дальше нет смысла")
        return
    title = re.search(r"<title>([^<]*)</title>", resp.text, re.I)
    print(f"  Главная OK: HTTP 200, title={title.group(1).strip() if title else '?'}")
    _save("mosreg_index.html", resp.text)

    paths = discover_api_paths(base, resp.text)
    _save("mosreg_api_paths.txt", "\n".join(sorted(paths)))
    print(f"  Найдено api-путей в бандлах: {len(paths)} (полный список в probe_zmo_out/mosreg_api_paths.txt)")
    interesting = sorted(p for p in paths if re.search(r"trade|purchase|tender|offer|catalog|search", p, re.I))
    for p in interesting[:25]:
        print(f"    · {p}")

    # Известные кандидаты (реестр закупок — точный путь уточняется по бандлам выше).
    print("\n  Пробую известные эндпоинты:")
    try_endpoint(
        base + "/api/Trade/GetTradesForPublicList",
        bodies=[{"page": 1, "itemsPerPage": 5}, {"take": 5, "skip": 0, "filter": {}}],
    )
    # Реестр закупок часто отдаётся и обычным HTML:
    try_endpoint(base + "/purchases")


def main() -> None:
    targets = set(sys.argv[1:]) or {"mos", "mosreg"}
    print("Разведка ЗМО-площадок Москвы/МО. Результаты также в ./probe_zmo_out/")
    print("Время:", time.strftime("%Y-%m-%d %H:%M:%S"))
    if "mos" in targets:
        probe_mos()
    if "mosreg" in targets:
        probe_mosreg()
    print("\n" + "═" * 70)
    print("ГОТОВО. Пришли обратно: весь вывод консоли + файлы probe_zmo_out/*_api_paths.txt")
    print("(если какой-то POST вернул JSON со списком закупок — этот файл тоже)")
    print("═" * 70)


if __name__ == "__main__":
    main()
