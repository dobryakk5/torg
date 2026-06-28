"""
sources/tenderplan.py — источник данных Tenderplan API.

Возвращает тендеры в стандартном формате main.py (как ЕИС-канал и B2B-Center).
Вызывается из run_stage1() как Канал 4.

Особенности маппинга:
  - ЕИС-тендеры (number = 11–22 цифры):
      purchase_number = number (дедупликация с ЕИС-каналом автоматическая)
      url = to_common_info_url(number)
  - Коммерческие тендеры (number содержит тире/буквы):
      purchase_number = _id (MongoDB ObjectId, не коллизирует с ЕИС)
      url = "" (stage2 пропустит получение страницы)

INN и href доступны только в fullinfo-ответе, не в list — оставляем пустыми.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ЕИС: 11–22 цифры
_EIS_RE = re.compile(r"^\d{11,22}$")
# 223-ФЗ: ровно 11 цифр, начинается с 3
_EIS_223_RE = re.compile(r"^3\d{10}$")


def _is_eis_number(number: str) -> bool:
    return bool(_EIS_RE.match(str(number or "")))


def _law_type(number: str) -> str:
    s = str(number or "")
    if not _EIS_RE.match(s):
        return "коммерческая"
    if _EIS_223_RE.match(s):
        return "223-ФЗ"
    return "44-ФЗ"


def _ms_to_date_str(ms: Any) -> str:
    if not ms:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        return dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, OSError, TypeError):
        return ""


def _map_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Маппит одну запись Tenderplan list-ответа → стандартный формат main.py."""
    number = str(item.get("number") or "").strip()
    raw_id = str(item.get("_id") or "").strip()
    is_eis = _is_eis_number(number)

    purchase_number = number if is_eis else raw_id
    if not purchase_number:
        return None

    title = str(item.get("orderName") or "").strip()[:500]
    if not title:
        return None

    customers = item.get("customers") or []
    customer_name = ""
    if customers and isinstance(customers[0], dict):
        customer_name = str(customers[0].get("name") or "").strip()[:300]

    price = item.get("maxPrice")
    if not isinstance(price, (int, float)) or price <= 0:
        price = None

    region = str(item.get("region") or "")

    deadline_str = _ms_to_date_str(
        item.get("submissionCloseDateTime") or item.get("submissionCloseDate")
    )
    published_str = _ms_to_date_str(
        item.get("publicationDateTime") or item.get("publicationDate")
    )

    if is_eis:
        from scraper import to_common_info_url
        url = to_common_info_url(number)
    else:
        url = ""

    primary_text = "\n".join(filter(None, [
        title,
        customer_name,
        f"Регион: {region}" if region else "",
        f"Номер: {number}",
    ]))[:4000]

    return {
        "purchase_number": purchase_number,
        "title":           title,
        "customer":        customer_name,
        "customer_inn":    "",
        "price":           price,
        "law_type":        _law_type(number),
        "deadline":        deadline_str,
        "published_at":    published_str,
        "url":             url,
        "platform":        "Tenderplan",
        "primary_text":    primary_text,
        "region":          region,
        "matched_keywords": ["tenderplan"],
    }


def search_tenderplan(
    *,
    token: str,
    base_url: str = "https://tenderplan.ru",
    list_params: dict[str, Any] | None = None,
    timeout: int = 60,
    retries: int = 3,
    verify_ssl: bool = True,
) -> list[dict[str, Any]]:
    """
    Запрашивает тендеры из Tenderplan API и возвращает их
    в стандартном формате main.py.

    Параметры:
        token       — TENDERPLAN_TOKEN из .env
        base_url    — TENDERPLAN_BASE_URL (по умолчанию https://tenderplan.ru)
        list_params — параметры GET /api/tenders/getlist (например {"limit": 50})
        timeout     — таймаут HTTP-запросов
        retries     — число повторов при ошибке
        verify_ssl  — проверять SSL-сертификат
    """
    from tenderplan.tenderplan_collect import TenderplanClient, TenderplanError

    if not token:
        logger.warning("Tenderplan: TENDERPLAN_TOKEN не задан — пропускаю канал")
        return []

    client = TenderplanClient(
        base_url=base_url,
        token=token,
        timeout=timeout,
        retries=retries,
        verify_ssl=verify_ssl,
    )

    try:
        items, _raw, endpoint = client.get_tenders(params=dict(list_params or {}))
    except TenderplanError as exc:
        logger.error("Tenderplan API ошибка: %s", exc)
        return []

    logger.info("Tenderplan: получено %d записей (%s)", len(items), endpoint)

    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mapped = _map_item(item)
        if mapped:
            result.append(mapped)

    logger.info("Tenderplan: замаплено %d тендеров", len(result))
    return result
