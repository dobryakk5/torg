#!/usr/bin/env python3
"""
Тестовая выгрузка первых N торгов из Tenderplan вместе с документацией.

Что делает:
1. Проверяет Bearer-токен через /api/info/user.
2. Запрашивает список торгов через /api/tenders/getlist.
3. Берёт первые TENDERPLAN_LIMIT записей.
4. Сохраняет JSON каждой записи.
5. Запрашивает список вложений через /api/tenders/attachments.
6. Пытается скачать всю документацию тендера через /api/tenders/archive.

Если API требует дополнительные параметры фильтра, передайте их JSON-объектом
через TENDERPLAN_LIST_PARAMS_JSON.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv


COMMON_LIST_KEYS = (
    "items",
    "data",
    "result",
    "results",
    "tenders",
    "list",
    "rows",
    "documents",
)

TENDER_ID_KEYS = (
    "_id",
    "id",
    "tenderId",
    "tenderID",
    "tender_id",
    "resourceId",
)

ID_PARAM_CANDIDATES = (
    "tenderId",
    "id",
    "_id",
    "tenderID",
    "tender_id",
)


class TenderplanError(RuntimeError):
    pass


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_json_env(name: str, default: dict[str, Any]) -> dict[str, Any]:
    raw = os.getenv(name)
    if not raw:
        return default

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TenderplanError(
            f"{name} содержит некорректный JSON: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise TenderplanError(f"{name} должен содержать JSON-объект.")

    return value


def safe_name(value: Any, fallback: str = "unknown") -> str:
    text = str(value or fallback).strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:140] or fallback


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def response_excerpt(response: requests.Response, size: int = 1200) -> str:
    try:
        return json.dumps(
            response.json(),
            ensure_ascii=False,
            indent=2,
        )[:size]
    except (ValueError, json.JSONDecodeError):
        return response.text[:size]


def extract_list(payload: Any) -> list[Any]:
    """Ищет основной массив записей в типичных форматах API-ответа."""
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    for key in COMMON_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_list(value)
            if nested:
                return nested

    # Последний резервный вариант: ищем первый непустой массив объектов.
    for value in payload.values():
        if isinstance(value, list) and value:
            if all(isinstance(item, dict) for item in value):
                return value

    return []


def unwrap_tender(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"raw": item}

    for key in ("tender", "resource", "data"):
        nested = item.get(key)
        if isinstance(nested, dict) and any(k in nested for k in TENDER_ID_KEYS):
            merged = dict(item)
            merged["_tender"] = nested
            return merged

    return item


def find_tender_id(item: dict[str, Any]) -> str | None:
    candidates: list[dict[str, Any]] = [item]

    for key in ("_tender", "tender", "resource", "data"):
        nested = item.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for candidate in candidates:
        for key in TENDER_ID_KEYS:
            value = candidate.get(key)
            if value not in (None, ""):
                return str(value)

    return None


def find_tender_title(item: dict[str, Any], tender_id: str) -> str:
    candidates: list[dict[str, Any]] = [item]

    for key in ("_tender", "tender", "resource", "data"):
        nested = item.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for candidate in candidates:
        for key in (
            "name",
            "title",
            "orderName",
            "subject",
            "purchaseName",
            "objectInfo",
        ):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return tender_id


def guess_extension(response: requests.Response, default: str = ".bin") -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition, re.I)
    if match:
        filename = match.group(1).strip().strip('"')
        suffix = Path(filename).suffix
        if suffix:
            return suffix

    content_type = response.headers.get("Content-Type", "").lower()
    if "zip" in content_type:
        return ".zip"
    if "pdf" in content_type:
        return ".pdf"
    if "json" in content_type:
        return ".json"
    if "octet-stream" in content_type:
        return ".zip"

    if response.content.startswith(b"PK\x03\x04"):
        return ".zip"

    return default


class TenderplanClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int,
        verify_ssl: bool,
    ) -> None:
        if not token:
            raise TenderplanError("Не указан TENDERPLAN_TOKEN.")

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "tenderplan-test-export/1.0",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        stream: bool = False,
        accept: str | None = None,
    ) -> requests.Response:
        headers = {}
        if accept:
            headers["Accept"] = accept

        response = self.session.request(
            method,
            urljoin(f"{self.base_url}/", path.lstrip("/")),
            params=params,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
            stream=stream,
        )
        return response

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = self.request("GET", path, params=params)
        if not response.ok:
            raise TenderplanError(
                f"GET {path} -> HTTP {response.status_code}\n"
                f"{response_excerpt(response)}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise TenderplanError(
                f"GET {path} вернул не JSON. "
                f"Content-Type: {response.headers.get('Content-Type')}"
            ) from exc


def get_tenders(
    client: TenderplanClient,
    params: dict[str, Any],
) -> tuple[list[Any], Any, str]:
    """
    Основной официальный метод — /api/tenders/getlist.
    v2 оставлен резервным вариантом на случай различий версии API.
    """
    endpoints = (
        "/api/tenders/getlist",
        "/api/tenders/v2/getlist",
    )
    errors: list[str] = []

    for endpoint in endpoints:
        response = client.request("GET", endpoint, params=params)

        if not response.ok:
            errors.append(
                f"{endpoint}: HTTP {response.status_code}: "
                f"{response_excerpt(response, 500)}"
            )
            continue

        try:
            payload = response.json()
        except ValueError:
            errors.append(f"{endpoint}: сервер вернул не JSON")
            continue

        items = extract_list(payload)
        if items:
            return items, payload, endpoint

        errors.append(
            f"{endpoint}: запрос успешен, но массив торгов не найден. "
            f"Корневой тип: {type(payload).__name__}"
        )

    raise TenderplanError(
        "Не удалось получить список торгов.\n\n"
        + "\n".join(errors)
        + "\n\nПроверьте параметры GET /api/tenders/getlist в Swagger "
          "и внесите их в TENDERPLAN_LIST_PARAMS_JSON."
    )


def try_tender_json_endpoint(
    client: TenderplanClient,
    path: str,
    tender_id: str,
) -> tuple[Any | None, str | None]:
    errors: list[str] = []

    for param_name in ID_PARAM_CANDIDATES:
        response = client.request(
            "GET",
            path,
            params={param_name: tender_id},
        )

        if response.ok:
            try:
                return response.json(), param_name
            except ValueError:
                errors.append(
                    f"{param_name}: HTTP 200, но ответ не является JSON"
                )
                continue

        errors.append(
            f"{param_name}: HTTP {response.status_code}"
        )

    logging.debug("%s: %s", path, "; ".join(errors))
    return None, None


def try_download_archive(
    client: TenderplanClient,
    tender_id: str,
    output_dir: Path,
) -> tuple[Path | None, str | None]:
    errors: list[str] = []

    for param_name in ID_PARAM_CANDIDATES:
        response = client.request(
            "GET",
            "/api/tenders/archive",
            params={param_name: tender_id},
            stream=True,
            accept="application/octet-stream, application/zip, */*",
        )

        if not response.ok:
            errors.append(
                f"{param_name}: HTTP {response.status_code}: "
                f"{response_excerpt(response, 300)}"
            )
            continue

        content_type = response.headers.get("Content-Type", "").lower()

        # Иногда endpoint может вернуть JSON с URL или описанием ошибки.
        if "json" in content_type:
            try:
                payload = response.json()
            except ValueError:
                errors.append(f"{param_name}: некорректный JSON")
                continue

            save_json(output_dir / "archive_response.json", payload)

            download_url = None
            if isinstance(payload, dict):
                for key in ("url", "downloadUrl", "downloadURL", "href", "link"):
                    value = payload.get(key)
                    if isinstance(value, str) and value:
                        download_url = value
                        break

            if not download_url:
                errors.append(
                    f"{param_name}: получен JSON без ссылки на архив"
                )
                continue

            response = client.session.get(
                urljoin(f"{client.base_url}/", download_url),
                timeout=client.timeout,
                verify=client.verify_ssl,
                stream=True,
            )
            if not response.ok:
                errors.append(
                    f"{param_name}: ссылка на архив вернула "
                    f"HTTP {response.status_code}"
                )
                continue

        content = response.content
        if not content:
            errors.append(f"{param_name}: получен пустой файл")
            continue

        extension = guess_extension(response, ".zip")
        archive_path = output_dir / f"documents{extension}"
        archive_path.write_bytes(content)
        return archive_path, param_name

    (output_dir / "archive_errors.txt").write_text(
        "\n".join(errors),
        encoding="utf-8",
    )
    return None, None


def main() -> int:
    load_dotenv()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    token = os.getenv("TENDERPLAN_TOKEN", "").strip()
    base_url = os.getenv(
        "TENDERPLAN_BASE_URL",
        "https://tenderplan.ru",
    ).strip()
    limit = int(os.getenv("TENDERPLAN_LIMIT", "10"))
    timeout = int(os.getenv("TENDERPLAN_TIMEOUT", "120"))
    verify_ssl = env_bool("TENDERPLAN_VERIFY_SSL", True)
    output_root = Path(
        os.getenv("TENDERPLAN_OUTPUT_DIR", "./tenderplan_export")
    ).expanduser()

    list_params = load_json_env(
        "TENDERPLAN_LIST_PARAMS_JSON",
        {"limit": limit},
    )

    client = TenderplanClient(
        base_url=base_url,
        token=token,
        timeout=timeout,
        verify_ssl=verify_ssl,
    )

    output_root.mkdir(parents=True, exist_ok=True)

    logging.info("Проверяю авторизацию...")
    user = client.get_json("/api/info/user")
    save_json(output_root / "_user.json", user)
    logging.info(
        "Авторизация успешна: %s",
        user.get("displayName", user.get("email", "пользователь")),
    )

    logging.info("Получаю список торгов...")
    items, raw_list, used_endpoint = get_tenders(client, list_params)
    save_json(output_root / "_raw_tender_list.json", raw_list)

    selected = items[:limit]
    logging.info(
        "Метод %s вернул %d записей; обрабатываю первые %d.",
        used_endpoint,
        len(items),
        len(selected),
    )

    index: list[dict[str, Any]] = []

    for number, raw_item in enumerate(selected, start=1):
        item = unwrap_tender(raw_item)
        tender_id = find_tender_id(item)

        if not tender_id:
            logging.warning(
                "[%d/%d] Не найден ID тендера. JSON сохранён отдельно.",
                number,
                len(selected),
            )
            folder = output_root / f"{number:02d}_unknown"
            folder.mkdir(parents=True, exist_ok=True)
            save_json(folder / "tender.json", raw_item)
            index.append(
                {
                    "number": number,
                    "tender_id": None,
                    "status": "id_not_found",
                }
            )
            continue

        title = find_tender_title(item, tender_id)
        folder = output_root / (
            f"{number:02d}_{safe_name(tender_id)}_"
            f"{safe_name(title, 'tender')[:70]}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        save_json(folder / "tender.json", raw_item)

        logging.info(
            "[%d/%d] %s — %s",
            number,
            len(selected),
            tender_id,
            title[:100],
        )

        attachments, attachments_param = try_tender_json_endpoint(
            client,
            "/api/tenders/attachments",
            tender_id,
        )
        attachment_count = None

        if attachments is not None:
            save_json(folder / "attachments.json", attachments)
            attachment_items = extract_list(attachments)
            attachment_count = len(attachment_items)
            logging.info(
                "  Вложения: %d; параметр API: %s",
                attachment_count,
                attachments_param,
            )
        else:
            logging.warning(
                "  Не удалось получить метаданные вложений."
            )

        archive_path, archive_param = try_download_archive(
            client,
            tender_id,
            folder,
        )

        if archive_path:
            logging.info(
                "  Документация сохранена: %s; параметр API: %s",
                archive_path,
                archive_param,
            )
            archive_status = "downloaded"
        else:
            logging.warning(
                "  Архив документов не скачан. "
                "Подробности: %s",
                folder / "archive_errors.txt",
            )
            archive_status = "failed"

        index.append(
            {
                "number": number,
                "tender_id": tender_id,
                "title": title,
                "folder": str(folder),
                "attachment_count": attachment_count,
                "archive_status": archive_status,
            }
        )

    save_json(output_root / "_index.json", index)

    print()
    print(f"Готово. Результаты: {output_root.resolve()}")
    print(f"Обработано торгов: {len(selected)}")
    print(
        "Индекс выгрузки:",
        (output_root / "_index.json").resolve(),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TenderplanError as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
    except requests.RequestException as exc:
        logging.error("Ошибка HTTP: %s", exc)
        raise SystemExit(2)
