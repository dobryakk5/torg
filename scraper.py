"""
scraper.py — парсинг открытого поиска ЕИС (zakupki.gov.ru).

Работает через публичный поиск. Не запускай слишком часто: раз в 2–4 часа достаточно.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://zakupki.gov.ru/",
}

BASE_URL = "https://zakupki.gov.ru"
SEARCH_URL = f"{BASE_URL}/epz/order/extendedsearch/results.html"
REQUEST_DELAY = config.REQUEST_DELAY


def _sleep(multiplier: float = 1.0) -> None:
    time.sleep(max(0.5, REQUEST_DELAY * multiplier + random.uniform(0.2, 1.1)))


def _get(url: str, params: dict | None = None, retries: int = 3) -> Optional[requests.Response]:
    """GET с повторами при ошибке и лёгким backoff."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=25)
            if resp.status_code == 200:
                return resp
            logger.warning("HTTP %s для %s", resp.status_code, url)
            if resp.status_code in {403, 429}:
                _save_debug_response(resp.text, "blocked")
        except requests.RequestException as e:
            logger.warning("Ошибка запроса (попытка %d): %s", attempt + 1, e)
        _sleep(attempt + 1)
    return None


def _save_debug_response(html: str, name: str) -> None:
    try:
        path = config.DATA_DIR / f"debug_{name}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8", errors="ignore")
    except Exception:
        pass


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.replace("\xa0", " ")
    candidates = re.findall(r"\d[\d\s]*(?:[,.]\d{1,2})?", text)
    if not candidates:
        return None
    raw = max(candidates, key=len).replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def to_common_info_url(url_or_number: str) -> str:
    """Build the public common-info notice URL from any EIS URL or regNumber."""
    value = str(url_or_number or "")
    match = re.search(r"\b\d{11,22}\b", value)
    if not match:
        return value
    return (
        "https://zakupki.gov.ru/epz/order/notice/zk20/view/common-info.html"
        f"?regNumber={match.group(0)}"
    )


def search_eis(
    keyword: str,
    price_from: int | None = None,
    price_to: int | None = None,
    fz44: bool = True,
    fz223: bool = True,
    pages: int = 2,
    days_back: int | None = None,          # ← фильтр по дате публикации
) -> list[dict]:
    """
    days_back: искать только закупки опубликованные за последние N дней.
    None → берёт значение из config.PUBLISH_DAYS_BACK (по умолчанию 30).
    0 или меньше → не ставит publishDateFrom, но оставляет af=on (только активные).
    Без этого параметра ЕИС отдаёт закупки за все годы включая 2014.
    """
    from datetime import datetime, timedelta

    tenders: list[dict] = []
    seen_numbers: set[str] = set()

    n_days = days_back if days_back is not None else getattr(config, "PUBLISH_DAYS_BACK", 30)
    date_from = (datetime.now() - timedelta(days=n_days)).strftime("%d.%m.%Y") if n_days and n_days > 0 else ""

    params_base = {
        "searchString":       keyword,
        "morphology":         "on",
        "search-filter":      "Дате размещения",
        "sortDirection":      "false",
        "recordsPerPage":     "_20",
        "showLotsInfoHidden": "false",
        "sortBy":             "UPDATE_DATE",
        "currencyIdGeneral":  "-1",
        "af":                 "on",          # только активные (приём заявок)
    }
    if date_from:
        params_base["publishDateFrom"] = date_from
    if fz44:
        params_base["fz44"] = "on"
    if fz223:
        params_base["fz223"] = "on"
    if price_from:
        params_base["priceFrom"] = str(price_from)
    if price_to:
        params_base["priceTo"] = str(price_to)

    for page in range(1, pages + 1):
        params = {**params_base, "pageNumber": str(page)}
        period = f"с {date_from}" if date_from else "все активные"
        logger.info("Ищу '%s', страница %d (%s)", keyword, page, period)

        resp = _get(SEARCH_URL, params=params)
        if not resp:
            logger.error("Не удалось получить страницу %d для '%s'", page, keyword)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.registry-entry__form") or soup.select("div.search-registry-entry-block")

        if not cards:
            logger.info("Нет карточек на странице %d для '%s'", page, keyword)
            _save_debug_response(resp.text, f"no_cards_{keyword}_{page}")
            break

        for card in cards:
            tender = _parse_card(card)
            if not tender:
                continue
            tender["matched_keywords"] = [keyword]

            # Мягкий региональный фильтр, если задан.
            if config.REGIONS:
                haystack = f"{tender.get('title','')} {tender.get('customer','')} {tender.get('region','')}".lower()
                if not any(region.lower() in haystack for region in config.REGIONS):
                    continue

            num = tender.get("purchase_number", "")
            if num and num not in seen_numbers:
                seen_numbers.add(num)
                tenders.append(tender)

        _sleep()

    logger.info("Найдено %d закупок по запросу '%s'", len(tenders), keyword)
    return tenders


def _parse_card(card) -> Optional[dict]:
    try:
        text_all = card.get_text("\n", strip=True)

        num_el = card.select_one(
            "div.registry-entry__header-mid__number a, "
            "div.registry-entry__body-value a, "
            "a[href*='noticeId'], a[href*='regNumber']"
        )
        if not num_el:
            return None

        href = num_el.get("href", "")
        source_url = BASE_URL + href if href.startswith("/") else href
        num_text = num_el.get_text(" ", strip=True)
        number_match = re.search(r"\d{11,22}", num_text + " " + href + " " + text_all)
        purchase_number = number_match.group(0) if number_match else num_text
        url = to_common_info_url(purchase_number or source_url)

        title = ""
        title_candidates = [
            card.select_one("div.registry-entry__body-value"),
            card.select_one("span.registry-entry__body-href a"),
            card.select_one("a.registry-entry__body-href"),
        ]
        for el in title_candidates:
            if el:
                candidate = el.get_text(" ", strip=True)
                if candidate and not re.fullmatch(r"\d{11,22}", candidate):
                    title = candidate
                    break

        customer = ""
        for label in ["Заказчик", "Организация", "Размещено"]:
            found = re.search(label + r"\s*:?\s*([^\n]{5,220})", text_all, re.I)
            if found:
                customer = found.group(1).strip()
                break
        if not customer:
            for el in card.select("div.registry-entry__body-href"):
                candidate = el.get_text(" ", strip=True)
                if candidate and candidate != title and len(candidate) > 5:
                    customer = candidate
                    break

        price = None
        for el in card.select("div.price-block__value, span.price-block__value"):
            price = _parse_price(el.get_text(" ", strip=True))
            if price:
                break
        if price is None:
            price_match = re.search(r"(?:Начальная|Максимальная|НМЦК)[^\n]{0,120}", text_all, re.I)
            if price_match:
                price = _parse_price(price_match.group(0))

        law_type = "223-ФЗ" if "223-ФЗ" in text_all else "44-ФЗ"

        deadline = ""
        deadline_patterns = [
            r"(?:Окончание подачи заявок|Дата и время окончания срока подачи)[^\d]*(\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?)",
            r"(\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?)",
        ]
        for pattern in deadline_patterns:
            m = re.search(pattern, text_all)
            if m:
                deadline = m.group(1)
                break

        published_at = ""
        pub_match = re.search(r"(?:Размещено|Дата размещения)[^\d]*(\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?)", text_all)
        if pub_match:
            published_at = pub_match.group(1)

        return {
            "purchase_number": purchase_number,
            "title": title[:500],
            "customer": customer[:300],
            "price": price,
            "law_type": law_type,
            "deadline": deadline,
            "published_at": published_at,
            "url": url,
            "platform": "ЕИС",
            "primary_text": text_all[:4000],
        }
    except Exception as e:
        logger.warning("Ошибка парсинга карточки: %s", e)
        return None


def get_tender_page(url: str) -> tuple[str, str]:
    resp = _get(url)
    if not resp:
        return "", ""
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines() if l.strip()]
    return resp.text, "\n".join(lines)


def get_tender_document_text(url: str, max_chars: int = 8000) -> str:
    _, text = get_tender_page(url)
    return text[:max_chars]


def parse_result_info(page_text: str, initial_price: float | None = None) -> dict:
    """
    Best-effort извлечение результата закупки со страницы ЕИС.

    Это не заменяет нормальный парсинг протоколов, но даёт Stage 3 первичную аналитику:
    статус, победитель, ИНН победителя, финальная цена, число участников и снижение.
    Если ЕИС поменял верстку или данные лежат только в документах протокола, поля могут быть пустыми.
    """
    text = (page_text or "").replace("\xa0", " ")
    result: dict = {"status": "result_checked"}

    status_patterns = [
        r"Статус\s*(?:закупки)?\s*:?\s*([^\n]{3,120})",
        r"Этап\s*(?:закупки)?\s*:?\s*([^\n]{3,120})",
        r"(Закупка отменена|Закупка завершена|Контракт заключен|Определение поставщика завершено|Не состоялась)",
    ]
    for pat in status_patterns:
        m = re.search(pat, text, re.I)
        if m:
            result["status"] = m.group(1).strip()[:120]
            break

    winner_patterns = [
        r"(?:Победитель|Поставщик|Исполнитель|Подрядчик)\s*:?\s*([^\n]{5,220})",
        r"(?:Наименование участника закупки|Участник закупки)\s*:?\s*([^\n]{5,220})",
    ]
    for pat in winner_patterns:
        m = re.search(pat, text, re.I)
        if m:
            winner = re.sub(r"\s+", " ", m.group(1)).strip(" :-")
            result["winner_name"] = winner[:220]
            break

    inn_match = re.search(r"ИНН\s*:?\s*(\d{10}|\d{12})", text, re.I)
    if inn_match:
        result["winner_inn"] = inn_match.group(1)

    price_patterns = [
        r"(?:Цена контракта|Итоговая цена|Предложение о цене|Цена договора)[^\d]{0,80}(\d[\d\s]*(?:[,.]\d{1,2})?)",
    ]
    for pat in price_patterns:
        m = re.search(pat, text, re.I)
        if m:
            price = _parse_price(m.group(1))
            if price:
                result["final_price"] = price
                break

    part_match = re.search(r"(?:Количество поданных заявок|Количество участников|Подано заявок)[^\d]{0,80}(\d{1,4})", text, re.I)
    if part_match:
        try:
            result["participants_count"] = int(part_match.group(1))
        except ValueError:
            pass

    if initial_price and result.get("final_price"):
        try:
            initial = float(initial_price)
            final = float(result["final_price"])
            if initial > 0 and final > 0:
                result["price_drop_percent"] = round((initial - final) / initial * 100, 2)
        except (TypeError, ValueError):
            pass

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Поиск по ОКПД2 кодам
# ══════════════════════════════════════════════════════════════════════════════

def search_eis_by_okpd2(
    okpd2_codes: list[str],
    price_from: int | None = None,
    price_to: int | None = None,
    fz44: bool = True,
    fz223: bool = True,
    pages: int = 2,
    days_back: int | None = None,
) -> list[dict]:
    """
    Ищет закупки по кодам ОКПД2 вместо ключевых слов.
    Ловит тендеры, где нет наших ключевых слов в названии,
    но код деятельности соответствует ИТ.

    Пример:
        search_eis_by_okpd2(["62.01", "62.02", "62.09"])

    Параметр ЕИС: okpd2IDs (через запятую).
    days_back <= 0 отключает publishDateFrom, но оставляет af=on.
    """
    from datetime import datetime, timedelta

    tenders: list[dict] = []
    seen:    set[str]   = set()

    n_days    = days_back if days_back is not None else getattr(config, "PUBLISH_DAYS_BACK", 30)
    date_from = (datetime.now() - timedelta(days=n_days)).strftime("%d.%m.%Y") if n_days and n_days > 0 else ""

    params_base = {
        "okpd2IDs":           ",".join(okpd2_codes),
        "search-filter":      "Дате размещения",
        "sortDirection":      "false",
        "recordsPerPage":     "_20",
        "showLotsInfoHidden": "false",
        "sortBy":             "UPDATE_DATE",
        "currencyIdGeneral":  "-1",
        "af":                 "on",
    }
    if date_from:
        params_base["publishDateFrom"] = date_from
    if fz44:
        params_base["fz44"] = "on"
    if fz223:
        params_base["fz223"] = "on"
    if price_from:
        params_base["priceFrom"] = str(price_from)
    if price_to:
        params_base["priceTo"] = str(price_to)

    for page in range(1, pages + 1):
        params = {**params_base, "pageNumber": str(page)}
        period = f"с {date_from}" if date_from else "все активные"
        logger.info("ОКПД2 %s, страница %d (%s)", okpd2_codes, page, period)

        resp = _get(SEARCH_URL, params=params)
        if not resp:
            break

        soup  = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.registry-entry__form") or soup.select("div.search-registry-entry-block")
        if not cards:
            logger.info("Нет карточек на стр. %d для ОКПД2 %s", page, okpd2_codes)
            break

        for card in cards:
            tender = _parse_card(card)
            if not tender:
                continue
            tender["matched_keywords"] = [f"okpd2:{','.join(okpd2_codes)}"]
            num = tender.get("purchase_number", "")
            if num and num not in seen:
                seen.add(num)
                tenders.append(tender)

        _sleep()

    logger.info("ОКПД2 %s → %d закупок", okpd2_codes, len(tenders))
    return tenders
