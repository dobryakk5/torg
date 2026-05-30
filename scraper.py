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
    date_from: str | None = None,          # явная дата публикации «с» (дд.мм.гггг)
    date_to: str | None = None,            # явная дата публикации «по» (дд.мм.гггг)
) -> list[dict]:
    """
    Фильтр по дате публикации:
      • date_from/date_to — явный диапазон «дд.мм.гггг» (например, один конкретный день).
        Если заданы, days_back игнорируется.
      • days_back — искать за последние N дней (если date_from не задан).
        None → config.PUBLISH_DAYS_BACK (30). 0/меньше → без publishDateFrom, но af=on.
    Без фильтра по дате ЕИС отдаёт закупки за все годы включая 2014.
    """
    from datetime import datetime, timedelta

    tenders: list[dict] = []
    seen_numbers: set[str] = set()

    if date_from is None:
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
    if date_to:
        params_base["publishDateTo"] = date_to
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
        period = (f"{date_from}–{date_to}" if date_to else f"с {date_from}") if date_from else "все активные"
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
# Детальные блоки страницы common-info (сроки, финансирование, объект, требования)
# ══════════════════════════════════════════════════════════════════════════════

_MONEY = r"\d[\d  ]*(?:[.,]\d{2})?"

# Границы секций. Заголовки задаются как фразы — ищутся устойчиво к переносам
# строк (между словами допускается любой пробел/перенос).
_HEAD_EXECUTION = "Информация о сроках исполнения контракта и источниках финансирования"
_HEAD_FINANCING = "Финансовое обеспечение закупки"
_HEAD_OBJECT    = "Информация об объекте закупки"
_HEAD_PREFREQ   = "Преимущества, требования к участникам"
_HEAD_REQ       = "Требования к участникам"
_HEAD_SECURITY  = "Обеспечение исполнения контракта"

# Заголовки, на которых режем секцию (любой из них = конец предыдущей).
_BOUNDARIES = [
    _HEAD_EXECUTION, _HEAD_FINANCING, _HEAD_OBJECT, _HEAD_PREFREQ,
    "Преимущества", _HEAD_REQ, "Применение национального режима",
    _HEAD_SECURITY, "Обеспечение гарантийных", "Информация о порядке",
    "Условия контракта", "Дополнительная информация",
]


def _money(raw: str) -> Optional[float]:
    """'500 000,00' / '3 383,33' → float."""
    if not raw:
        return None
    cleaned = raw.replace("\xa0", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        try:
            return float(raw.replace("\xa0", "").replace(" ", "").replace(",", "."))
        except ValueError:
            return None


def _heading_pos(flat: str, heading: str, start: int = 0) -> int:
    """Ищет заголовок, допуская перенос строки/любой пробел между словами."""
    pat = r"\s+".join(re.escape(w) for w in heading.split())
    m = re.compile(pat).search(flat, start)
    return m.start() if m else -1


def _heading_end(flat: str, heading: str, start: int = 0) -> int:
    pat = r"\s+".join(re.escape(w) for w in heading.split())
    m = re.compile(pat).search(flat, start)
    return m.end() if m else -1


def _section_text(flat: str, start_heading: str, boundaries: list[str] | None = None) -> str:
    """Текст от заголовка start_heading до ближайшей следующей границы."""
    body_start = _heading_end(flat, start_heading)
    if body_start == -1:
        return ""
    end = len(flat)
    for h in (boundaries or _BOUNDARIES):
        if h == start_heading:
            continue
        j = _heading_pos(flat, h, body_start)
        if j != -1 and j < end:
            end = j
    return flat[body_start:end].strip()


def parse_common_info_details(text: str) -> dict:
    """
    Best-effort разбор детальных блоков страницы common-info.html (по очищенному тексту).

    Возвращает структурированные поля + сырой текст каждой секции (raw) для дословного
    показа. ЕИС меняет вёрстку — отдельные поля могут быть пустыми, но raw сохранится.
    """
    t = (text or "").replace("\xa0", " ")
    flat = "\n".join(l.strip() for l in t.splitlines() if l.strip())

    details: dict = {
        "execution":         {},
        "financing":         {},
        "object":            {},
        "preferences":       {},
        "requirements":      [],
        "contract_security": {},
        "advantages":        "",   # обратная совместимость = preferences.text
    }
    money_re = _MONEY

    # ── 1. Сроки исполнения ────────────────────────────────────────────────
    exec_raw = _section_text(flat, _HEAD_EXECUTION) or flat
    execution: dict = {"raw": exec_raw[:4000]}
    m = re.search(r"Дата начала исполнения контракта\s*(\d+)\s*(?:календарн|рабоч)", exec_raw, re.I)
    if m: execution["start_days"] = int(m.group(1))
    m = re.search(r"Срок исполнения контракта\s*(\d+)\s*(?:календарн|рабоч)", exec_raw, re.I)
    if m: execution["duration_days"] = int(m.group(1))
    m = re.search(r"Закупка за счет собственных средств организации\s*(Да|Нет)", exec_raw, re.I)
    if m: execution["own_funds"] = m.group(1).strip().lower() == "да"
    details["execution"] = execution

    # ── 2. Финансовое обеспечение: разбивка по периодам + всего ────────────
    fin_raw = _section_text(flat, _HEAD_FINANCING) or exec_raw
    financing: dict = {"raw": fin_raw[:4000], "years": []}
    # Заголовки колонок: "На 2026 год", ..., "На Последующие годы", "Всего"
    labels = re.findall(r"На (\d{4}) год", fin_raw)
    # Первая строка значений идёт сразу после шапки "...Всего, ₽"
    vm = re.search(r"Всего,\s*₽\s*((?:" + money_re + r"\s*){5})", fin_raw)
    if vm:
        vals = [_money(x) for x in re.findall(money_re, vm.group(1))][:5]
        col_labels = (labels[:3] + ["Последующие годы"])[:4]
        financing["years"] = [
            {"label": lbl, "amount": vals[i]}
            for i, lbl in enumerate(col_labels) if i < len(vals)
        ]
        if len(vals) >= 5:
            financing["total"] = vals[4]
    if "total" not in financing:
        totals = [v for v in (_money(x) for x in re.findall(money_re, fin_raw)) if v]
        if totals: financing["total"] = max(totals)
    if "За счет внебюджетных средств" in fin_raw:
        financing["source"] = "Внебюджетные средства"
    elif "За счет бюджетных средств" in fin_raw or "бюджетных ассигнований" in fin_raw:
        financing["source"] = "Бюджетные средства"
    details["financing"] = financing

    # ── 3. Объект закупки: тип лота + позиции (код/наименование/ед./цена) ──
    obj_raw = _section_text(flat, _HEAD_OBJECT)
    obj: dict = {"raw": obj_raw[:4000], "positions": []}
    if obj_raw:
        # Контракт «по цене единицы» (объём не определён, ч.24 ст.22 44-ФЗ)
        if "Невозможно определить количество" in obj_raw:
            obj["unit_price_contract"] = True
            obj["type_comment"] = (
                "Оплата по цене единицы товара/работы/услуги — объём не определён "
                "(ч. 24 ст. 22 Закона № 44-ФЗ), в пределах максимальной цены контракта."
            )
        else:
            obj["unit_price_contract"] = False
        # Единица измерения + цена за единицу + стоимость (тройка перед прайсом).
        # Её начало служит границей наименования товара/услуги.
        tm = re.search(r"\n([А-Яа-я][А-Яа-я .]{0,25})\n(" + money_re + r")\n(" + money_re + r")\b", obj_raw)
        # Код позиции + ПОЛНОЕ наименование (весь блок от кода до единицы измерения)
        cm = re.search(r"(\d{2}\.\d{2}(?:\.\d{2,3}){1,2})\n", obj_raw)
        position: dict = {}
        if cm:
            position["code"] = cm.group(1)
            name_end = tm.start() if tm else len(obj_raw)
            name_block = obj_raw[cm.end():name_end].strip()
            # значение характеристики "Соответствие" на отдельной строке —
            # подклеиваем к строке характеристики, как в карточке ЕИС
            name_block = re.sub(r"\n(Соответствие)\b", r" \1", name_block)
            position["name"] = name_block[:2000]
        if tm:
            position["unit"]       = tm.group(1).strip()
            position["unit_price"] = _money(tm.group(2))
            position["cost"]       = _money(tm.group(3))
        if position:
            obj["positions"].append(position)
        sum_m = re.search(r"Начальная сумма цен единиц.*?(" + money_re + r")\s*₽", obj_raw, re.I | re.S)
        if sum_m:
            obj["start_sum"] = _money(sum_m.group(1))
    details["object"] = obj

    # ── 4. Преимущества / требования к участникам ──────────────────────────
    adv_m = re.search(r"(?:^|\n)Преимущества\s*\n(.*?)(?:\nТребования к участникам|\Z)", flat, re.S)
    adv_text = adv_m.group(1).strip()[:600] if adv_m else ""
    established = bool(adv_text) and "не установлен" not in adv_text.lower()
    details["preferences"] = {"established": established, "text": adv_text or "Не установлены"}
    details["advantages"] = details["preferences"]["text"]

    # Требования: режем секцию по границам и берём пункты "N.\n<текст>"
    req_raw = _section_text(flat, _HEAD_REQ)
    reqs: list[str] = []
    if req_raw:
        reqs = [r.strip() for r in re.findall(r"(?:^|\n)\s*\d+\.\s*\n?([^\n]{8,300})", req_raw)]
        # отбрасываем мусор вида "62.02.30.000 ..." (строки, начинающиеся с кода/цифр)
        reqs = [r for r in reqs if not re.match(r"^\d", r)]
    details["requirements"] = reqs[:12]
    # Спец-требование = что-то сверх типовых ст. 31 / ст. 14
    details["requirements_special"] = any(
        not re.search(r"ст\.?\s*31|ст\.?\s*14", r) for r in details["requirements"]
    )

    # ── 5. Обеспечение исполнения контракта ────────────────────────────────
    sec_raw = _section_text(flat, _HEAD_SECURITY, boundaries=[
        "Обеспечение гарантийных", "Информация о порядке", "Условия контракта",
        "Дополнительная информация", "Документы", "Журнал событий",
    ])
    security: dict = {}
    rm = re.search(r"Требуется обеспечение исполнения контракта\s*(Да|Нет)", sec_raw, re.I)
    if rm:
        security["required"] = rm.group(1).strip().lower() == "да"
    am = re.search(
        r"Размер обеспечения исполнения контракта\s*(" + money_re + r")\s*₽\s*(?:\(\s*(\d+(?:[.,]\d+)?)\s*%\s*\))?",
        sec_raw, re.I,
    )
    if am:
        security["amount"] = _money(am.group(1))
        if am.group(2):
            security["percent"] = float(am.group(2).replace(",", "."))
    details["contract_security"] = security

    return details


def fetch_tender_details(url: str) -> dict:
    """Грузит страницу common-info лота и парсит детальные блоки."""
    common_url = to_common_info_url(url)
    _, text = get_tender_page(common_url)
    if not text:
        return {}
    return parse_common_info_details(text)


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
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """
    Ищет закупки по кодам ОКПД2 вместо ключевых слов.
    Ловит тендеры, где нет наших ключевых слов в названии,
    но код деятельности соответствует ИТ.

    Пример:
        search_eis_by_okpd2(["62.01", "62.02", "62.09"])

    Параметр ЕИС: okpd2IDs (через запятую).
    date_from/date_to — явный диапазон публикации (дд.мм.гггг); если заданы,
    days_back игнорируется. days_back <= 0 отключает publishDateFrom, но оставляет af=on.
    """
    from datetime import datetime, timedelta

    tenders: list[dict] = []
    seen:    set[str]   = set()

    if date_from is None:
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
    if date_to:
        params_base["publishDateTo"] = date_to
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
        period = (f"{date_from}–{date_to}" if date_to else f"с {date_from}") if date_from else "все активные"
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
