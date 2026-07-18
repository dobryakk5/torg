#!/usr/bin/env python3
"""
test_proxy_sources.py — проверка, что все тендерные площадки работают через прокси.

Что делает:
  1. Показывает прямой IP и IP через прокси (SOURCES_PROXY_URL из .env) —
     убеждаемся, что прокси жив и выходной IP российский.
  2. Прогоняет по одному лёгкому запросу к каждой площадке (1 ключ, 1 страница)
     через штатные функции каналов — те самые, что использует main.py, — и
     печатает сводную таблицу OK/FAIL с количеством найденных карточек.
  3. Отдельно проверяет, что LLM-провайдер ходит НАПРЯМУЮ (мимо прокси).

БД не трогает, в Telegram не пишет. Запуск:
    python test_proxy_sources.py            # все площадки
    python test_proxy_sources.py eat mos    # только выбранные
"""

from __future__ import annotations

import sys
import time

import requests

import config

# Ключ для тестового поиска: короткий, наверняка даёт результаты на витринах ЗМО.
TEST_KEYWORD = "сайт"
IP_CHECK_URL = "https://api.ipify.org?format=json"


def _get_ip(proxies: dict | None) -> str:
    try:
        resp = requests.get(IP_CHECK_URL, timeout=20, proxies=proxies)
        return resp.json().get("ip", "?")
    except Exception as e:
        return f"ошибка: {e.__class__.__name__}: {str(e)[:80]}"


def check_ips() -> bool:
    """Печатает прямой IP и IP через прокси. False, если прокси не работает."""
    proxies = config.source_proxies()
    print("── Проверка IP ──")
    direct_ip = _get_ip(None)
    print(f"  прямой IP        : {direct_ip}")
    if not proxies:
        print("  ❌ SOURCES_PROXY_URL не задан в .env — площадки пойдут напрямую")
        return False
    proxy_ip = _get_ip(proxies)
    print(f"  IP через прокси  : {proxy_ip}")
    if "ошибка" in proxy_ip:
        print("  ❌ прокси не отвечает")
        return False
    if proxy_ip == direct_ip:
        print("  ⚠️  IP совпадают — похоже, прокси не применяется")
        return False
    print("  ✅ прокси работает, потоки разделены")
    return True


def check_llm_direct() -> None:
    """LLM должен ходить мимо прокси: в llm_provider нет proxies=, а переменная
    SOURCES_PROXY_URL не входит в стандартные HTTP_PROXY/HTTPS_PROXY, которые
    requests подхватывает из окружения. Здесь просто убеждаемся, что стандартные
    прокси-переменные окружения не выставлены."""
    import os
    print("\n── LLM: прямое подключение ──")
    leaked = [v for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy") if os.getenv(v)]
    if leaked:
        print(f"  ⚠️  в окружении заданы {leaked} — requests уведёт в прокси и LLM-трафик!")
    else:
        print("  ✅ HTTP_PROXY/HTTPS_PROXY не заданы — LLM и Telegram идут напрямую")


# ── Тесты площадок: (слаг, название, вызов) ────────────────────────────────
# Каждый вызов возвращает list[dict] карточек — как в main.run_stage1.

def _t_eis():
    from scraper import search_eis
    return search_eis(TEST_KEYWORD, price_from=100_000, price_to=500_000, pages=1, days_back=7)


def _t_okpd2():
    from scraper import search_eis_by_okpd2
    return search_eis_by_okpd2(["62.01"], price_from=100_000, price_to=500_000, pages=1, days_back=7)


def _t_b2b():
    from sources.b2b_center import search_b2b
    return search_b2b(TEST_KEYWORD, pages=1)


def _t_spb():
    from sources.spb_estore import search_spb
    return search_spb(TEST_KEYWORD, pages=1)


def _t_eat():
    from sources.eat import search_eat
    return search_eat(TEST_KEYWORD, price_from=20_000, price_to=590_000, pages=1)


def _t_mos():
    from sources.mos_supplier import search_mos
    return search_mos(TEST_KEYWORD, pages=1)


def _t_mosreg():
    from sources.mosreg import search_mosreg
    return search_mosreg(TEST_KEYWORD, pages=1)


def _t_zakazrf():
    from sources.zakazrf import search_zakazrf
    return search_zakazrf(TEST_KEYWORD, pages=1)


def _t_tenderplan():
    from sources.tenderplan import search_tenderplan
    token = str(getattr(config, "TENDERPLAN_TOKEN", "") or "")
    if not token:
        raise RuntimeError("TENDERPLAN_TOKEN не задан — пропуск")
    return search_tenderplan(token=token)


TESTS: list[tuple[str, str, object]] = [
    ("eis",        "ЕИС (zakupki.gov.ru)",      _t_eis),
    ("okpd2",      "ЕИС по ОКПД2",              _t_okpd2),
    ("b2b",        "B2B-Center",                _t_b2b),
    ("spb",        "ЭМ СПб (estore.gz-spb.ru)", _t_spb),
    ("eat",        "ЕАТ «Берёзка»",             _t_eat),
    ("mos",        "ПП Москвы (zakupki.mos.ru)", _t_mos),
    ("mosreg",     "ЭМ МО (market.mosreg.ru)",  _t_mosreg),
    ("zakazrf",    "БП ZakazRF",                _t_zakazrf),
    ("tenderplan", "Tenderplan",                _t_tenderplan),
]


def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    only = {a.lower() for a in sys.argv[1:]}

    proxy_ok = check_ips()
    check_llm_direct()
    if not proxy_ok:
        print("\nПрокси не работает — тест площадок пойдёт напрямую. Продолжаю, но результат не показателен.")

    print(f"\n── Площадки (ключ «{TEST_KEYWORD}», 1 страница) ──")
    results: list[tuple[str, str, int | None, str]] = []
    for slug, title, fn in TESTS:
        if only and slug not in only:
            continue
        print(f"\n▶ {title}")
        started = time.monotonic()
        try:
            rows = fn()
            took = time.monotonic() - started
            n = len(rows or [])
            status = "OK" if n > 0 else "ПУСТО"
            results.append((slug, title, n, f"{status} ({took:.1f}с)"))
            for r in (rows or [])[:2]:
                print(f"    · {r.get('purchase_number', '?')}  {str(r.get('title', ''))[:70]}")
        except Exception as e:
            took = time.monotonic() - started
            results.append((slug, title, None, f"FAIL: {e.__class__.__name__}: {str(e)[:60]} ({took:.1f}с)"))

    print("\n── Итог ──")
    print(f"{'площадка':28} {'карточек':>9}  статус")
    print("─" * 70)
    fails = 0
    for slug, title, n, status in results:
        if n is None or "FAIL" in status:
            fails += 1
        print(f"{title:28} {(str(n) if n is not None else '—'):>9}  {status}")
    print("─" * 70)
    if fails:
        print(f"❌ Проблемных площадок: {fails}")
    else:
        print("✅ Все площадки отработали через прокси")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
