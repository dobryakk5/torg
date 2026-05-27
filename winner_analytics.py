"""
winner_analytics.py — анализ победителей и ценовой коридор.

Два источника данных:
  1. Реестр контрактов ЕИС — активно скрейпим для пополнения статистики
  2. Результаты Stage3 уже в БД — пересчёт коридоров из накопленных данных

Что даёт:
  - Ценовой коридор по категории: медианное снижение, P25/P75, число участников
  - Детектор монополизации: заказчик, где 1 победитель выигрывает 60%+ торгов
  - Рекомендованная ставка для конкретного тендера с объяснением

Запуск отдельно:
    python winner_analytics.py --update           # скрейп + пересчёт коридоров
    python winner_analytics.py --bid 780000 bitrix 44-ФЗ  # рассчитать ставку
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config
import database as db

logger = logging.getLogger(__name__)

# ── Категории тендеров ──────────────────────────────────────────────────────
# Порядок важен: первое совпадение wins.
CATEGORIES: list[tuple[str, list[str]]] = [
    ("bitrix",      ["битрикс", "bitrix", "1с-битрикс"]),
    ("crm",         ["битрикс24", "bitrix24", "crm", "срм"]),
    ("integration", ["интеграция", "обмен данными", "api", "эдо", "смэв"]),
    ("website",     ["сайт", "портал", "веб-"]),
    ("server",      ["сервер", "vps", "linux", "резервное копирование", "бэкап"]),
    ("hardware",    ["тсд", "терминал сбора", "сканер штрихкода", "принтер этикеток",
                     "видеонаблюдение", "скуд", "сетевое оборудование", "nas"]),
    ("it_support",  ["техническая поддержка", "сопровождение", "администрирование"]),
    ("other_it",    ["программное обеспечение", "информационная система", "по "]),
]

_DEFAULT_CATEGORY = "other_it"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

BASE_URL      = "https://zakupki.gov.ru"
CONTRACTS_URL = f"{BASE_URL}/epz/contract/search/results.html"


# ══════════════════════════════════════════════════════════════════════════════
# Категоризация
# ══════════════════════════════════════════════════════════════════════════════

def classify_category(title: str) -> str:
    t = title.lower()
    for cat, keywords in CATEGORIES:
        if any(kw in t for kw in keywords):
            return cat
    return _DEFAULT_CATEGORY


# ══════════════════════════════════════════════════════════════════════════════
# Скрейпинг реестра контрактов ЕИС
# ══════════════════════════════════════════════════════════════════════════════

def _get(url: str, params: dict) -> Optional[requests.Response]:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r
            logger.warning("HTTP %s: %s", r.status_code, url)
        except requests.RequestException as e:
            logger.warning("Attempt %d failed: %s", attempt + 1, e)
        time.sleep(3 * (attempt + 1))
    return None


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    clean = re.sub(r"[^\d,.]", "", text).replace(",", ".")
    try:
        return float(clean)
    except ValueError:
        return None


def scrape_contracts(
    keyword: str,
    pages: int = 3,
    date_from: str = "01.01.2024",
    fz44: bool = True,
    fz223: bool = True,
) -> list[dict]:
    """
    Скрейпит реестр контрактов ЕИС по ключевому слову.
    Возвращает список контрактов с полями:
      title, customer_name, customer_inn, winner_name, winner_inn,
      initial_price, final_price, drop_pct, sign_date, law_type
    """
    contracts = []
    params_base = {
        "searchString":      keyword,
        "morphology":        "on",
        "search-filter":     "Дате заключения",
        "sortBy":            "SIGN_DATE",
        "sortDirection":     "false",
        "recordsPerPage":    "_20",
        "contractDateFrom":  date_from,
    }
    if fz44:
        params_base["fz44"] = "on"
    if fz223:
        params_base["fz223"] = "on"

    for page in range(1, pages + 1):
        resp = _get(CONTRACTS_URL, {**params_base, "pageNumber": str(page)})
        if not resp:
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = (
            soup.select("div.registry-entry__form") or
            soup.select("div.search-registry-entry-block")
        )
        if not cards:
            logger.info("Нет карточек контрактов на стр. %d для '%s'", page, keyword)
            break

        for card in cards:
            c = _parse_contract_card(card)
            if c:
                contracts.append(c)

        time.sleep(getattr(config, "REQUEST_DELAY", 3.0))

    logger.info("Контрактов по '%s': %d", keyword, len(contracts))
    return contracts


def _parse_contract_card(card) -> Optional[dict]:
    try:
        text = card.get_text(" ", strip=True)

        # Название
        title_el = card.select_one("a.registry-entry__body-href, div.registry-entry__body-value a")
        title = title_el.get_text(strip=True) if title_el else ""
        href  = (title_el.get("href", "") if title_el else "")
        url   = (BASE_URL + href) if href.startswith("/") else href

        # Цена контракта (финальная)
        final_price = None
        for el in card.select("div.price-block__value, span.price-block__value"):
            final_price = _parse_price(el.get_text())
            if final_price:
                break

        # Начальная цена (НМЦК) — бывает в отдельном блоке
        initial_price = None
        m = re.search(r"(?:НМЦК|начальная[^:]{0,20}цена)[:\s]*([\d\s,]+(?:[,.]\d{1,2})?)", text, re.I)
        if m:
            initial_price = _parse_price(m.group(1))

        # Дата заключения
        sign_date = ""
        m_date = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
        if m_date:
            sign_date = m_date.group(0)

        # Закон
        law_type = "223-ФЗ" if "223-ФЗ" in text else "44-ФЗ"

        # Поставщик (победитель)
        winner_name = ""
        winner_inn  = ""
        supplier_el = card.select_one("div.registry-entry__body--supplier")
        if supplier_el:
            winner_name = supplier_el.get_text(" ", strip=True)[:200]
        inn_m = re.search(r"ИНН\s*:?\s*(\d{10}|\d{12})", text)
        if inn_m:
            winner_inn = inn_m.group(1)

        # Заказчик
        customer_name = ""
        customer_inn  = ""
        customer_el = card.select_one("div.registry-entry__body--customer, div.registry-entry__body-href")
        if customer_el:
            customer_name = customer_el.get_text(" ", strip=True)[:200]

        # drop
        drop_pct = None
        if initial_price and final_price and initial_price > 0:
            drop_pct = round((1 - final_price / initial_price) * 100, 2)

        if not title and not final_price:
            return None

        return {
            "title":         title[:300],
            "url":           url,
            "customer_name": customer_name,
            "customer_inn":  customer_inn,
            "winner_name":   winner_name,
            "winner_inn":    winner_inn,
            "initial_price": initial_price,
            "final_price":   final_price,
            "drop_pct":      drop_pct,
            "sign_date":     sign_date,
            "law_type":      law_type,
        }
    except Exception as e:
        logger.debug("Ошибка парсинга карточки контракта: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Пересчёт ценовых коридоров
# ══════════════════════════════════════════════════════════════════════════════

def rebuild_corridors(extra_contracts: list[dict] | None = None) -> dict[str, int]:
    """
    Пересчитывает price_corridors из двух источников:
      1. Данные Stage3 уже в БД (tenders.final_price / price_drop_percent)
      2. extra_contracts — свежескрейпленные контракты (опционально)

    Возвращает dict {category: sample_count}.
    """
    import statistics

    # Собираем из БД
    samples: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)

    n_from_db = db.rebuild_corridors_from_results()
    logger.info("Коридоры пересчитаны из БД: %d категорий", n_from_db)

    if not extra_contracts:
        return {}

    # Добавляем свежие контракты
    for c in extra_contracts:
        drop = c.get("drop_pct")
        if drop is None or not (0 <= drop <= 60):
            continue
        cat  = classify_category(c.get("title", ""))
        law  = c.get("law_type", "all")
        parts = 0  # из контрактов число участников обычно недоступно
        samples[(cat, law)].append((drop, parts))
        samples[(cat, "all")].append((drop, parts))

    count = {}
    for (cat, law), items in samples.items():
        drops = sorted(d for d, _ in items)
        n = len(drops)
        if n < 5:   # минимум 5 точек для коридора
            continue

        def pct(lst: list[float], p: int) -> float:
            idx = max(0, int(len(lst) * p / 100) - 1)
            return round(lst[idx], 2)

        db.upsert_price_corridor({
            "category":         cat,
            "law_type":         law,
            "sample_count":     n,
            "avg_drop_pct":     round(statistics.mean(drops), 2),
            "p25_drop_pct":     pct(drops, 25),
            "p50_drop_pct":     pct(drops, 50),
            "p75_drop_pct":     pct(drops, 75),
            "min_drop_pct":     round(min(drops), 2),
            "max_drop_pct":     round(max(drops), 2),
            "avg_participants": None,
        })
        count[f"{cat}/{law}"] = n

    logger.info("Доп. коридоры из скрейпа: %d категорий", len(count))
    return count


# ══════════════════════════════════════════════════════════════════════════════
# Детектор монополизации
# ══════════════════════════════════════════════════════════════════════════════

def detect_monopoly(contracts: list[dict]) -> dict:
    """
    Принимает список контрактов одного заказчика.
    Возвращает:
      repeat_winner_inn, repeat_winner_name, repeat_winner_share,
      monopoly_flag (True если >60% побед у одного поставщика)
    """
    if not contracts:
        return {}

    winner_counter: Counter = Counter()
    winner_names: dict[str, str] = {}
    for c in contracts:
        inn  = c.get("winner_inn", "")
        name = c.get("winner_name", "")
        if inn:
            winner_counter[inn] += 1
            winner_names[inn] = name
        elif name:
            winner_counter[name] += 1

    if not winner_counter:
        return {}

    top_key, top_count = winner_counter.most_common(1)[0]
    total = len(contracts)
    share = round(top_count / total * 100, 1)

    return {
        "repeat_winner_inn":   top_key if top_key.isdigit() else "",
        "repeat_winner_name":  winner_names.get(top_key, top_key),
        "repeat_winner_share": share,
        "monopoly_flag":       share >= 60 and top_count >= 3,
    }


def scrape_customer_contracts(customer_inn: str, pages: int = 4) -> list[dict]:
    """
    Скрейпит реестр контрактов ЕИС, отфильтрованный по ИНН заказчика.
    """
    params_base = {
        "customerINN":    customer_inn,
        "morphology":     "on",
        "search-filter":  "Дате заключения",
        "sortBy":         "SIGN_DATE",
        "sortDirection":  "false",
        "recordsPerPage": "_20",
        "fz44":           "on",
        "fz223":          "on",
    }
    contracts = []
    for page in range(1, pages + 1):
        resp = _get(CONTRACTS_URL, {**params_base, "pageNumber": str(page)})
        if not resp:
            break
        soup  = BeautifulSoup(resp.text, "html.parser")
        cards = (
            soup.select("div.registry-entry__form") or
            soup.select("div.search-registry-entry-block")
        )
        if not cards:
            break
        for card in cards:
            c = _parse_contract_card(card)
            if c:
                contracts.append(c)
        time.sleep(getattr(config, "REQUEST_DELAY", 3.0))
    return contracts


# ══════════════════════════════════════════════════════════════════════════════
# Калькулятор ставки
# ══════════════════════════════════════════════════════════════════════════════

def recommend_bid(
    nmck: float,
    category: str,
    law_type: str = "44-ФЗ",
    target_margin: float = 0.25,
    cost_rate: float = 0.60,
) -> dict:
    """
    Рекомендует оптимальную ставку на основе ценового коридора.

    Args:
        nmck:          начальная максимальная цена контракта
        category:      категория (из classify_category)
        law_type:      "44-ФЗ" или "223-ФЗ"
        target_margin: желаемая чистая маржа (0.25 = 25%)
        cost_rate:     доля себестоимости (0.60 = 60%)

    Returns:
        dict с ключами: recommended, aggressive, min_profitable,
        corridor_p50, corridor_p75, competitors, source
    """
    corridor = db.get_price_corridor(category, law_type)

    # Минимальная прибыльная ставка исходя из себестоимости
    min_profitable = round(nmck * cost_rate / (1 - target_margin))

    if not corridor or corridor.get("sample_count", 0) < 3:
        # Нет данных — консервативный дефолт: снижение на 8%
        return {
            "recommended":   round(nmck * 0.92),
            "aggressive":    round(nmck * 0.88),
            "min_profitable":min_profitable,
            "corridor_p50":  None,
            "corridor_p75":  None,
            "competitors":   None,
            "sample_count":  0,
            "source":        "default (нет данных по категории)",
            "category":      category,
        }

    p50  = corridor["p50_drop_pct"]  # медиана
    p75  = corridor["p75_drop_pct"]  # агрессивно

    recommended = round(nmck * (1 - (p50 + 2) / 100))   # чуть агрессивнее медианы
    aggressive  = round(nmck * (1 - (p75 + 1) / 100))   # у нижней границы
    recommended = max(recommended, min_profitable)
    aggressive  = max(aggressive,  min_profitable)

    # unit-экономика для рекомендованной ставки
    gross       = recommended * (1 - cost_rate)
    sec_exec    = recommended * 0.07          # обеспечение исполнения ~7%
    guar_cost   = sec_exec * 0.02 * 0.25     # банк. гарантия ~2% год / квартал
    net_margin  = gross - guar_cost
    net_pct     = round(net_margin / recommended * 100, 1)

    return {
        "recommended":    recommended,
        "aggressive":     aggressive,
        "min_profitable": min_profitable,
        "corridor_p50":   p50,
        "corridor_p75":   p75,
        "avg_drop":       corridor.get("avg_drop_pct"),
        "competitors":    corridor.get("avg_participants"),
        "sample_count":   corridor.get("sample_count", 0),
        "net_margin_pct": net_pct,
        "net_margin_rub": round(net_margin),
        "source":         f"коридор по {sample_count_label(corridor)} контрактам",
        "category":       category,
    }


def sample_count_label(corridor: dict) -> str:
    n = corridor.get("sample_count", 0)
    if n >= 50:
        return f"{n}"
    if n >= 10:
        return f"{n}"
    return f"{n} (мало данных)"


# ══════════════════════════════════════════════════════════════════════════════
# Точка входа
# ══════════════════════════════════════════════════════════════════════════════

def run_update(keywords: list[str] | None = None, pages: int = 3) -> None:
    """
    Полный цикл обновления:
      1. Скрейп контрактов по ключевым словам
      2. Пересчёт price_corridors
    """
    kws = keywords or config.SEARCH_KEYWORDS[:8]  # берём первые 8, чтобы не перегружать
    all_contracts: list[dict] = []
    for kw in kws:
        try:
            contracts = scrape_contracts(kw, pages=pages)
            all_contracts.extend(contracts)
        except Exception as e:
            logger.error("Ошибка при скрейпе контрактов по '%s': %s", kw, e)

    logger.info("Всего контрактов собрано: %d", len(all_contracts))
    stats = rebuild_corridors(all_contracts)
    logger.info("Ценовые коридоры обновлены: %s", stats)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()

    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="Скрейп + пересчёт коридоров")
    ap.add_argument("--bid",    nargs=3, metavar=("NMCK", "CATEGORY", "LAW"),
                    help="Рассчитать ставку: --bid 780000 bitrix 44-ФЗ")
    args = ap.parse_args()

    if args.update:
        run_update()
    elif args.bid:
        nmck, cat, law = float(args.bid[0]), args.bid[1], args.bid[2]
        result = recommend_bid(nmck, cat, law)
        print(f"\nРекомендованная ставка для НМЦК {nmck:,.0f} ₽ / {cat} / {law}")
        print(f"  Рекомендуем:       {result['recommended']:>10,.0f} ₽")
        print(f"  Агрессивно:        {result['aggressive']:>10,.0f} ₽")
        print(f"  Минимум (маржа):   {result['min_profitable']:>10,.0f} ₽")
        print(f"  Медиана drop:      {result['corridor_p50']} %")
        print(f"  Чистая маржа:      {result['net_margin_pct']} %  ({result['net_margin_rub']:,.0f} ₽)")
        print(f"  Источник:          {result['source']}")
