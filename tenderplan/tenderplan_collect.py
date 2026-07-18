#!/usr/bin/env python3
"""
Единый тестовый сборщик Tenderplan.

Что делает:
1. Проверяет токен через GET /api/info/user.
2. Получает первые N торгов.
3. Сохраняет краткую карточку каждого тендера.
4. Пытается получить полную карточку.
5. Получает attachments.json.
6. Скачивает каждый документ по href/url в папку documents/.
7. Формирует общий index.json и манифест документов.

Запуск:
    python tenderplan_collect.py
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
from dotenv import load_dotenv


LIST_ENDPOINTS = (
    "/api/tenders/getlist",
    "/api/tenders/v2/getlist",
)

FULL_INFO_ENDPOINTS = (
    "/api/tenders/v2/fullinfo",
    "/api/tenders/fullinfo",
    "/api/tenders/info",
)

ATTACHMENTS_ENDPOINTS = (
    "/api/tenders/attachments",
)

TENDER_ID_KEYS = (
    "_id",
    "id",
    "tenderId",
    "tenderID",
    "tender_id",
    "resourceId",
)

TENDER_ID_PARAM_CANDIDATES = (
    "tenderId",
    "id",
    "_id",
    "tenderID",
    "tender_id",
)

LIST_KEYS = (
    "items",
    "data",
    "result",
    "results",
    "tenders",
    "list",
    "rows",
    "documents",
    "attachments",
    "files",
)


class TenderplanError(RuntimeError):
    pass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_json_env(name: str, default: dict[str, Any]) -> dict[str, Any]:
    value = os.getenv(name)
    if not value:
        return default

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise TenderplanError(
            f"{name} содержит некорректный JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise TenderplanError(f"{name} должен быть JSON-объектом.")

    return parsed


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def safe_filename(value: Any, fallback: str = "unknown") -> str:
    text = unquote(str(value or fallback)).strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:220] or fallback


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    for number in range(2, 10000):
        candidate = path.with_name(
            f"{path.stem}_{number}{path.suffix}"
        )
        if not candidate.exists():
            return candidate

    raise TenderplanError(f"Не удалось подобрать имя для {path}")


def response_excerpt(response: requests.Response, limit: int = 1500) -> str:
    try:
        return json.dumps(
            response.json(),
            ensure_ascii=False,
            indent=2,
        )[:limit]
    except ValueError:
        return response.text[:limit]


def extract_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    for key in LIST_KEYS:
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            nested = extract_list(value)
            if nested:
                return nested

    for value in payload.values():
        if isinstance(value, list) and value:
            if all(isinstance(item, dict) for item in value):
                return value

        if isinstance(value, dict):
            nested = extract_list(value)
            if nested:
                return nested

    return []


def candidate_objects(item: dict[str, Any]) -> list[dict[str, Any]]:
    result = [item]

    for key in ("tender", "_tender", "resource", "data", "item"):
        value = item.get(key)
        if isinstance(value, dict):
            result.append(value)

    return result


def find_tender_id(item: dict[str, Any]) -> str | None:
    for candidate in candidate_objects(item):
        for key in TENDER_ID_KEYS:
            value = candidate.get(key)
            if value not in (None, ""):
                return str(value)

    return None


def find_tender_title(item: dict[str, Any], fallback: str) -> str:
    title_keys = (
        "name",
        "title",
        "orderName",
        "subject",
        "purchaseName",
        "objectInfo",
        "displayName",
    )

    for candidate in candidate_objects(item):
        for key in title_keys:
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return fallback


def filename_from_headers(response: requests.Response) -> str | None:
    header = response.headers.get("Content-Disposition", "")

    match = re.search(
        r"filename\*=UTF-8''([^;]+)",
        header,
        flags=re.IGNORECASE,
    )
    if match:
        return safe_filename(match.group(1))

    match = re.search(
        r'filename="?([^";]+)"?',
        header,
        flags=re.IGNORECASE,
    )
    if match:
        return safe_filename(match.group(1))

    return None


def extension_from_response(response: requests.Response) -> str:
    content_type = (
        response.headers.get("Content-Type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )

    custom = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
    if content_type in custom:
        return custom[content_type]

    guessed = mimetypes.guess_extension(content_type)
    if guessed == ".jpe":
        return ".jpg"

    return guessed or ""


def looks_like_html(first_chunk: bytes, content_type: str) -> bool:
    stripped = first_chunk.lstrip().lower()
    return (
        "text/html" in content_type.lower()
        or stripped.startswith(b"<!doctype html")
        or stripped.startswith(b"<html")
    )


class TenderplanClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: int,
        retries: int,
        verify_ssl: bool,
    ) -> None:
        if not token:
            raise TenderplanError("В .env не указан TENDERPLAN_TOKEN.")

        self.base_url = base_url.rstrip("/")
        self.base_host = urlparse(self.base_url).netloc.lower()
        self.timeout = timeout
        self.retries = retries
        self.verify_ssl = verify_ssl

        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        )

        self.api_session = requests.Session()
        self.api_session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": user_agent,
            }
        )

        # Для внешних площадок токен Tenderplan не передаётся.
        self.external_session = requests.Session()
        self.external_session.headers.update(
            {
                "Accept": "*/*",
                "User-Agent": user_agent,
            }
        )

        # Прокси тендерных площадок (config.SOURCES_PROXY_URL): через него идут
        # обе сессии. Модуль остаётся работоспособным и без config (standalone).
        try:
            import config as _config
            _proxies = _config.source_proxies()
        except Exception:
            _proxies = None
        if _proxies:
            self.api_session.proxies.update(_proxies)
            self.external_session.proxies.update(_proxies)

    def api_get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
        stream: bool = False,
    ) -> requests.Response:
        return self.api_session.get(
            urljoin(f"{self.base_url}/", path.lstrip("/")),
            params=params,
            headers={"Accept": accept},
            timeout=self.timeout,
            verify=self.verify_ssl,
            stream=stream,
            allow_redirects=True,
        )

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = self.api_get(path, params=params)

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
                f"Content-Type={response.headers.get('Content-Type')}"
            ) from exc

    def get_tenders(
        self,
        *,
        params: dict[str, Any],
    ) -> tuple[list[Any], Any, str]:
        errors: list[str] = []

        for endpoint in LIST_ENDPOINTS:
            response = self.api_get(endpoint, params=params)

            if not response.ok:
                errors.append(
                    f"{endpoint}: HTTP {response.status_code}: "
                    f"{response_excerpt(response, 700)}"
                )
                continue

            try:
                payload = response.json()
            except ValueError:
                errors.append(f"{endpoint}: ответ не JSON")
                continue

            items = extract_list(payload)
            if items:
                return items, payload, endpoint

            errors.append(
                f"{endpoint}: HTTP 200, но массив торгов не найден."
            )

        raise TenderplanError(
            "Не удалось получить список торгов.\n\n"
            + "\n".join(errors)
            + "\n\nПроверьте параметры GET /api/tenders/getlist "
              "и переменную TENDERPLAN_LIST_PARAMS_JSON."
        )

    def try_tender_json(
        self,
        endpoints: tuple[str, ...],
        tender_id: str,
    ) -> tuple[Any | None, str | None, str | None]:
        errors: list[str] = []

        for endpoint in endpoints:
            for param_name in TENDER_ID_PARAM_CANDIDATES:
                response = self.api_get(
                    endpoint,
                    params={param_name: tender_id},
                )

                if response.ok:
                    try:
                        return response.json(), endpoint, param_name
                    except ValueError:
                        errors.append(
                            f"{endpoint}?{param_name}=...: "
                            "HTTP 200, но ответ не JSON"
                        )
                        continue

                errors.append(
                    f"{endpoint}?{param_name}=...: "
                    f"HTTP {response.status_code}"
                )

        logging.debug("Неудачные попытки: %s", "; ".join(errors))
        return None, None, None

    def download_document(
        self,
        attachment: dict[str, Any],
        output_dir: Path,
        number: int,
    ) -> dict[str, Any]:
        href = (
            attachment.get("href")
            or attachment.get("url")
            or attachment.get("downloadUrl")
            or attachment.get("downloadURL")
            or attachment.get("link")
        )

        if not isinstance(href, str) or not href.strip():
            return {
                "number": number,
                "status": "skipped",
                "error": "В записи документа отсутствует href/url.",
                "attachment": attachment,
            }

        url = href.strip()
        if not url.startswith(("http://", "https://")):
            url = urljoin(f"{self.base_url}/", url.lstrip("/"))

        requested_name = (
            attachment.get("realName")
            or attachment.get("fileName")
            or attachment.get("filename")
            or attachment.get("displayName")
            or f"document_{number:02d}"
        )
        requested_name = safe_filename(
            requested_name,
            f"document_{number:02d}",
        )

        target_host = urlparse(url).netloc.lower()
        session = (
            self.api_session
            if target_host == self.base_host
            else self.external_session
        )

        last_error = "Неизвестная ошибка"

        for attempt in range(1, self.retries + 1):
            try:
                with session.get(
                    url,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                    stream=True,
                    allow_redirects=True,
                ) as response:
                    if response.status_code == 429:
                        delay = response.headers.get(
                            "Retry-After",
                            str(attempt * 2),
                        )
                        try:
                            seconds = int(delay)
                        except ValueError:
                            seconds = attempt * 2
                        time.sleep(min(seconds, 30))
                        continue

                    response.raise_for_status()

                    final_name = (
                        filename_from_headers(response)
                        or requested_name
                    )

                    if not Path(final_name).suffix:
                        final_name += extension_from_response(response)

                    destination = unique_path(
                        output_dir / safe_filename(final_name)
                    )
                    destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    chunks = response.iter_content(
                        chunk_size=1024 * 256
                    )
                    first_chunk = next(chunks, b"")
                    content_type = response.headers.get(
                        "Content-Type",
                        "",
                    )

                    if not first_chunk:
                        raise TenderplanError(
                            "Сервер вернул пустой файл."
                        )

                    if looks_like_html(first_chunk, content_type):
                        preview = first_chunk[:500].decode(
                            "utf-8",
                            errors="replace",
                        )
                        raise TenderplanError(
                            "Вместо файла получена HTML-страница. "
                            "Внешняя площадка может требовать отдельную "
                            f"авторизацию. Начало ответа: {preview!r}"
                        )

                    with destination.open("wb") as file:
                        file.write(first_chunk)
                        for chunk in chunks:
                            if chunk:
                                file.write(chunk)

                    file_size = destination.stat().st_size
                    if file_size <= 0:
                        destination.unlink(missing_ok=True)
                        raise TenderplanError(
                            "Сохранённый файл пустой."
                        )

                    return {
                        "number": number,
                        "status": "downloaded",
                        "url": url,
                        "final_url": response.url,
                        "filename": destination.name,
                        "path": str(destination),
                        "size": file_size,
                        "content_type": content_type,
                        "external_host": target_host,
                    }

            except (
                requests.RequestException,
                TenderplanError,
                OSError,
                ValueError,
            ) as exc:
                last_error = str(exc)
                if attempt < self.retries:
                    time.sleep(min(attempt * 2, 10))

        return {
            "number": number,
            "status": "failed",
            "url": url,
            "requested_name": requested_name,
            "error": last_error,
        }


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
    timeout = int(os.getenv("TENDERPLAN_TIMEOUT", "180"))
    retries = int(os.getenv("TENDERPLAN_RETRIES", "3"))
    verify_ssl = env_bool("TENDERPLAN_VERIFY_SSL", True)
    output_root = Path(
        os.getenv(
            "TENDERPLAN_OUTPUT_DIR",
            "./tenderplan_export",
        )
    ).expanduser()

    list_params = load_json_env(
        "TENDERPLAN_LIST_PARAMS_JSON",
        {"limit": limit},
    )

    client = TenderplanClient(
        base_url=base_url,
        token=token,
        timeout=timeout,
        retries=retries,
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
    items, raw_list, list_endpoint = client.get_tenders(
        params=list_params,
    )
    save_json(output_root / "_raw_tender_list.json", raw_list)

    selected = items[:limit]
    logging.info(
        "Получено записей: %d. Обрабатываю первые: %d.",
        len(items),
        len(selected),
    )

    index: list[dict[str, Any]] = []
    total_documents = 0
    downloaded_documents = 0
    failed_documents = 0

    for position, raw_item in enumerate(selected, start=1):
        if not isinstance(raw_item, dict):
            raw_item = {"raw": raw_item}

        tender_id = find_tender_id(raw_item)

        if not tender_id:
            folder = output_root / f"{position:02d}_unknown"
            folder.mkdir(parents=True, exist_ok=True)
            save_json(folder / "tender_short.json", raw_item)

            logging.warning(
                "[%d/%d] В карточке не найден ID тендера.",
                position,
                len(selected),
            )
            index.append(
                {
                    "position": position,
                    "status": "tender_id_not_found",
                    "folder": str(folder),
                }
            )
            continue

        title = find_tender_title(raw_item, tender_id)
        folder = output_root / (
            f"{position:02d}_"
            f"{safe_filename(tender_id)}_"
            f"{safe_filename(title)[:80]}"
        )
        documents_dir = folder / "documents"
        folder.mkdir(parents=True, exist_ok=True)
        documents_dir.mkdir(parents=True, exist_ok=True)

        save_json(folder / "tender_short.json", raw_item)

        logging.info(
            "[%d/%d] %s — %s",
            position,
            len(selected),
            tender_id,
            title[:120],
        )

        full_info, full_endpoint, full_param = (
            client.try_tender_json(
                FULL_INFO_ENDPOINTS,
                tender_id,
            )
        )
        if full_info is not None:
            save_json(folder / "tender_full.json", full_info)
            logging.info(
                "  Полная карточка сохранена: %s, параметр %s",
                full_endpoint,
                full_param,
            )
        else:
            logging.warning(
                "  Полная карточка отдельным методом не получена. "
                "Краткая карточка сохранена."
            )

        attachments, attachments_endpoint, attachments_param = (
            client.try_tender_json(
                ATTACHMENTS_ENDPOINTS,
                tender_id,
            )
        )

        if attachments is None:
            logging.warning(
                "  Список документов не получен."
            )
            index.append(
                {
                    "position": position,
                    "tender_id": tender_id,
                    "title": title,
                    "folder": str(folder),
                    "documents_total": 0,
                    "documents_downloaded": 0,
                    "documents_failed": 0,
                    "attachments_status": "failed",
                }
            )
            continue

        save_json(folder / "attachments.json", attachments)
        attachment_items = extract_list(attachments)

        logging.info(
            "  Документов в attachments: %d; метод %s; параметр %s",
            len(attachment_items),
            attachments_endpoint,
            attachments_param,
        )

        manifest: list[dict[str, Any]] = []

        for document_number, attachment in enumerate(
            attachment_items,
            start=1,
        ):
            if not isinstance(attachment, dict):
                attachment = {"raw": attachment}

            document_name = (
                attachment.get("realName")
                or attachment.get("displayName")
                or f"document_{document_number:02d}"
            )

            logging.info(
                "  Скачиваю %d/%d: %s",
                document_number,
                len(attachment_items),
                document_name,
            )

            result = client.download_document(
                attachment,
                documents_dir,
                document_number,
            )
            manifest.append(result)
            total_documents += 1

            if result["status"] == "downloaded":
                downloaded_documents += 1
                logging.info(
                    "    Сохранено: %s (%d байт)",
                    result["filename"],
                    result["size"],
                )
            else:
                failed_documents += 1
                logging.error(
                    "    Не скачано: %s",
                    result.get("error"),
                )

        save_json(
            folder / "documents_download_manifest.json",
            manifest,
        )

        tender_downloaded = sum(
            1 for item in manifest
            if item.get("status") == "downloaded"
        )
        tender_failed = sum(
            1 for item in manifest
            if item.get("status") == "failed"
        )

        index.append(
            {
                "position": position,
                "tender_id": tender_id,
                "title": title,
                "folder": str(folder),
                "list_endpoint": list_endpoint,
                "full_info_endpoint": full_endpoint,
                "documents_total": len(attachment_items),
                "documents_downloaded": tender_downloaded,
                "documents_failed": tender_failed,
                "attachments_status": "ok",
            }
        )

    save_json(output_root / "_index.json", index)

    print()
    print("Сбор завершён.")
    print(f"Торгов обработано: {len(selected)}")
    print(f"Документов найдено: {total_documents}")
    print(f"Документов скачано: {downloaded_documents}")
    print(f"Ошибок скачивания: {failed_documents}")
    print(f"Результат: {output_root.resolve()}")
    print(f"Индекс: {(output_root / '_index.json').resolve()}")

    return 0 if failed_documents == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TenderplanError as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
    except requests.RequestException as exc:
        logging.error("Ошибка HTTP: %s", exc)
        raise SystemExit(2)
    except KeyboardInterrupt:
        logging.warning("Остановлено пользователем.")
        raise SystemExit(130)
