"""
probe_sources.py — B2B-Center: финальная проверка поиска и пагинации.

Поиск: /market/?f_keyword=<слово>&searching=1&trade=all
Проверяем: 1) фильтрует ли (IT-карточки), 2) параметр пагинации, 3) фильтр цены.

Запускать на машине с доступом в рунет:
    python probe_sources.py
"""

from __future__ import annotations

import re
import urllib.parse
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://www.b2b-center.ru/market/",
}
BASE = "https://www.b2b-center.ru"


def _get(path):
    return requests.get(BASE + path, headers=HEADERS, timeout=30)


def _ids(html):
    return re.findall(r"/tender-(\d+)/", html)


def _titles(html, limit=6):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen = set()
    for a in soup.find_all("a", href=True):
        if re.search(r"/tender-\d+/", a["href"]):
            t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            if t and t not in seen:
                seen.add(t)
                out.append(t[:90])
        if len(out) >= limit:
            break
    return out


def main():
    kw = urllib.parse.quote("сайт")
    search = f"/market/?f_keyword={kw}&searching=1&trade=all"

    print("=" * 70)
    print("1) Поиск по ключевому слову (f_keyword + searching=1)")
    print(f"   {search}")
    r = _get(search)
    s_ids = _ids(r.text)
    base_ids = _ids(_get("/market/").text)
    print(f"   HTTP {r.status_code}, карточек: {len(set(s_ids))}")
    print(f"   отличается от базовой витрины: {'✅ ДА' if set(s_ids) != set(base_ids[:20]) else '⚠ НЕТ'}")
    print("   Примеры названий (проверь, что это про сайты/IT):")
    for t in _titles(r.text):
        print(f"     • {t}")

    print("\n" + "=" * 70)
    print("2) Пагинация на отфильтрованной выдаче")
    for pag in ["&page=2", "&start=20", "&from=20", "&p=2", "&pn=2", "&page_num=2", "&offset=20"]:
        try:
            ids2 = _ids(_get(search + pag).text)
        except requests.RequestException as e:
            print(f"   {pag:14} ❌ {e}")
            continue
        if not ids2:
            print(f"   {pag:14} 0 карточек")
        elif set(ids2[:20]) == set(s_ids[:20]):
            print(f"   {pag:14} ·· та же страница")
        else:
            print(f"   {pag:14} ✅ ДРУГАЯ страница! ids[:4]={ids2[:4]}")

    print("\n" + "=" * 70)
    print("3) Фильтр цены (price_start/price_end)")
    pr = _get(search + "&price_start=100000&price_end=3000000&price_currency=RUR")
    pr_ids = _ids(pr.text)
    print(f"   HTTP {pr.status_code}, карточек: {len(set(pr_ids))} "
          f"({'✅ сузил' if set(pr_ids) != set(s_ids) else '⚠ без изменений'})")

    print("\n" + "=" * 70)
    print("Пришли: фильтрует ли поиск (п.1 + названия), рабочий параметр пагинации (п.2), цену (п.3).\n")


if __name__ == "__main__":
    main()
