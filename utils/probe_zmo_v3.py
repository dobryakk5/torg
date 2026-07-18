#!/usr/bin/env python3
"""
probe_zmo_v3.py — добивание API Москвы/МО по результатам v2 (15.07.2026).

Зацепки из v2:
  · old.zakupki.mos.ru/api/Cssp/Purchase/Query существует (GET-only), но наш
    queryDto дал "An error has occurred" → перебираем минимальные формы.
  · Текущий портал использует паттерн /newapi/api/<Контроллер>/Query?query=<JSON>
    (подсмотрено в живом XHR MassMessage/Query) → перебираем контроллеры.
  · api.market.mosreg.ru: /api/Trade/GetTrades и /api/Trade/Query отвечают 405
    и на GET, и на POST → пробуем PUT + спрашиваем OPTIONS (заголовок Allow).

Запуск: python3 probe_zmo_v3.py
Прислать обратно: весь вывод + файлы probe_zmo_out/v3_*.txt с JSON-ответами.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

OUT = Path("probe_zmo_out")
TIMEOUT = 25
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def show(label: str, resp: requests.Response | None, save_as: str | None = None) -> None:
    if resp is None:
        print(f"  {label} → СЕТЕВАЯ ОШИБКА")
        return
    ct = resp.headers.get("content-type", "").split(";")[0]
    body = (resp.text or "").replace("\n", " ")
    is_json_list = ct == "application/json" and re.search(r'"(items|entities|data|result)"\s*:\s*\[', body[:2000])
    mark = "  ★ ПОХОЖЕ НА СПИСОК!" if is_json_list else ""
    print(f"  {label}\n      → HTTP {resp.status_code} [{ct}] {body[:220]}{mark}")
    if save_as and resp.status_code == 200 and "json" in ct:
        OUT.mkdir(exist_ok=True)
        (OUT / save_as).write_text(resp.text[:300_000], encoding="utf-8")
        print(f"      (сохранено в probe_zmo_out/{save_as})")


def req(method: str, url: str, body: dict | None = None) -> requests.Response | None:
    try:
        return requests.request(
            method, url, json=body if body is not None else None,
            headers=HEADERS, timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"  {method} {url} → ОШИБКА: {e}")
        return None
    finally:
        time.sleep(1.5)


def q(dto: dict) -> str:
    return requests.utils.quote(json.dumps(dto, ensure_ascii=False))


def probe_mos() -> None:
    print("\n" + "═" * 70)
    print("МОСКВА: old.zakupki.mos.ru (GET-only Cssp) + newapi/<X>/Query")
    print("═" * 70)

    old = "https://old.zakupki.mos.ru/api/Cssp/Purchase/Query"
    print("\n[1] OPTIONS старого эндпоинта (заголовок Allow):")
    r = req("OPTIONS", old)
    if r is not None:
        print(f"  OPTIONS → HTTP {r.status_code}, Allow={r.headers.get('Allow', '—')}")

    print("\n[2] Минимальные queryDto на old (от простого к сложному):")
    dtos = [
        {"take": 5, "skip": 0},
        {"filter": {}, "take": 5, "skip": 0},
        {"filter": {"nameLike": "сайт"}, "take": 5, "skip": 0},
        {"filter": {"auctionSpecificFilter": {"stateIdIn": [19000002]}}, "take": 5, "skip": 0},
        {"filter": {"typeIdIn": [2]}, "take": 5, "skip": 0},
    ]
    for i, dto in enumerate(dtos):
        show(f"GET queryDto={json.dumps(dto, ensure_ascii=False)[:90]}",
             req("GET", f"{old}?queryDto={q(dto)}"),
             save_as=f"v3_mos_old_{i}.json")

    print("\n[3] Новый паттерн: /newapi/api/<Контроллер>/Query?query=<JSON>:")
    base = "https://zakupki.mos.ru/newapi/api"
    query = {"filter": {"nameLike": "сайт"}, "take": 5, "skip": 0, "withCount": True}
    minimal = {"take": 5, "skip": 0, "withCount": True}
    for ctrl in ("Purchase", "Auction", "Need", "Offer", "Sku", "PurchaseRequest", "Tender"):
        show(f"GET {ctrl}/Query (минимальный)",
             req("GET", f"{base}/{ctrl}/Query?query={q(minimal)}"),
             save_as=f"v3_mos_new_{ctrl}_min.json")
        show(f"GET {ctrl}/Query (nameLike=сайт)",
             req("GET", f"{base}/{ctrl}/Query?query={q(query)}"),
             save_as=f"v3_mos_new_{ctrl}_kw.json")


def probe_mosreg() -> None:
    print("\n" + "═" * 70)
    print("МО: api.market.mosreg.ru — выясняем метод (OPTIONS/PUT)")
    print("═" * 70)

    api = "https://api.market.mosreg.ru"
    for path in ("/api/Trade/GetTrades", "/api/Trade/Query"):
        print(f"\n[{path}]")
        r = req("OPTIONS", api + path)
        if r is not None:
            print(f"  OPTIONS → HTTP {r.status_code}, Allow={r.headers.get('Allow', '—')}, "
                  f"Access-Control-Allow-Methods={r.headers.get('Access-Control-Allow-Methods', '—')}")
        for body in (
            {"page": 1, "itemsPerPage": 5},
            {"take": 5, "skip": 0},
            {"Page": 1, "ItemsPerPage": 5, "Filter": {}},
        ):
            show(f"PUT body={json.dumps(body)[:70]}",
                 req("PUT", api + path, body),
                 save_as=f"v3_mosreg_{path.split('/')[-1]}_{abs(hash(str(body))) % 1000}.json")


if __name__ == "__main__":
    print("Разведка v3. Время:", time.strftime("%Y-%m-%d %H:%M:%S"))
    probe_mos()
    probe_mosreg()
    print("\n" + "═" * 70)
    print("ГОТОВО. Пришли весь вывод; строки с «★ ПОХОЖЕ НА СПИСОК!» — самое ценное.")
    print("═" * 70)
