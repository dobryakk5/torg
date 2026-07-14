#!/usr/bin/env python3
"""
Диагностика авторизации Tenderplan API.

Проверяет:
1. Видит ли Python токен из .env.
2. Работают ли /api/info/user и /api/info/firm с Bearer.
3. Является ли /api/info/v2/rights публичным справочником.
4. Что возвращает /auth/client/register.
5. Если регистрация успешна и получены client_id/client_secret,
   пробует получить access_token через /auth/client/token.
6. Пробует /api/tenders/getlist с исходным токеном и с OAuth-токеном.

Установка:
    pip install requests python-dotenv

Запуск:
    python tenderplan_api_check.py

В .env:
    TENDERPLAN_TOKEN=...
    TENDERPLAN_BASE_URL=https://tenderplan.ru

Чтобы реально попробовать зарегистрировать OAuth-клиент:
    TENDERPLAN_TRY_REGISTER=true

Опционально:
    TENDERPLAN_REGISTER_REDIRECT_URI=https://lebedeve.ru/tenderplan/oauth/callback
    TENDERPLAN_REGISTER_SCOPE=resources:external
    TENDERPLAN_LIST_PARAMS_JSON={"limit":10}
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


RESULT_FILE = Path("tenderplan_api_check_result.json")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_json_env(name: str, default: dict[str, Any]) -> dict[str, Any]:
    raw = os.getenv(name)
    if not raw:
        return default

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} содержит некорректный JSON: {exc}") from exc

    if not isinstance(value, dict):
        raise SystemExit(f"{name} должен быть JSON-объектом.")

    return value


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip()
    if len(value) <= 12:
        return "*" * len(value)

    return f"{value[:6]}...{value[-6:]} len={len(value)}"


def print_section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def safe_json(response: requests.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        return None


def excerpt_response(response: requests.Response, limit: int = 1200) -> str:
    body = safe_json(response)
    if body is not None:
        return json.dumps(body, ensure_ascii=False, indent=2)[:limit]
    return response.text[:limit]


def call(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    data: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    try:
        response = session.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            data=data,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "error": str(exc),
            "method": method,
            "url": url,
        }

    parsed = safe_json(response)
    return {
        "ok": response.ok,
        "status_code": response.status_code,
        "method": method,
        "url": response.url,
        "request_url": url,
        "response_headers": {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"set-cookie"}
        },
        "json": parsed,
        "text_excerpt": None if parsed is not None else response.text[:1200],
    }


def log_result(label: str, result: dict[str, Any]) -> None:
    print()
    print(f"{label}")
    print("-" * len(label))

    if "error" in result:
        print(f"ERROR: {result['error']}")
        return

    print(f"{result['method']} {result['url']}")
    print(f"HTTP {result['status_code']}")

    body = result.get("json")
    if body is not None:
        preview = json.dumps(body, ensure_ascii=False, indent=2)
        print(preview[:2000])
        if len(preview) > 2000:
            print("...обрезано...")
    else:
        text = result.get("text_excerpt") or ""
        if text:
            print(text[:2000])
        else:
            print("<пустое тело ответа>")


def find_client_credentials(payload: Any) -> tuple[str | None, str | None]:
    """
    Ищет client_id/client_secret в разных возможных форматах ответа.
    """
    if not isinstance(payload, dict):
        return None, None

    candidates = [payload]

    for key in ("client", "data", "result", "application", "oauthClient"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    id_keys = ("client_id", "clientId", "id", "_id")
    secret_keys = ("client_secret", "clientSecret", "secret")

    client_id = None
    client_secret = None

    for candidate in candidates:
        if client_id is None:
            for key in id_keys:
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    client_id = value
                    break

        if client_secret is None:
            for key in secret_keys:
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    client_secret = value
                    break

    return client_id, client_secret


def find_access_token(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    candidates = [payload]

    for key in ("data", "result", "token"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for candidate in candidates:
        for key in ("access_token", "accessToken", "token"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value

    return None


def explain(results: dict[str, Any]) -> list[str]:
    messages: list[str] = []

    user_auth = results.get("auth_token", {}).get("info_user", {})
    firm_auth = results.get("auth_token", {}).get("info_firm", {})
    rights_no_auth = results.get("no_auth", {}).get("rights", {})
    rights_auth = results.get("auth_token", {}).get("rights", {})
    register = results.get("register", {}).get("response")

    if user_auth.get("status_code") == 200:
        messages.append(
            "Текущий TENDERPLAN_TOKEN работает как пользовательский Bearer-токен: "
            "/api/info/user вернул 200."
        )
    elif user_auth.get("status_code") == 401:
        messages.append(
            "Текущий TENDERPLAN_TOKEN НЕ принят как пользовательский Bearer-токен: "
            "/api/info/user вернул 401."
        )

    if firm_auth.get("status_code") == 200:
        messages.append("/api/info/firm с Bearer-токеном работает.")
    elif firm_auth.get("status_code") == 401:
        messages.append("/api/info/firm с Bearer-токеном тоже вернул 401.")

    if rights_no_auth.get("status_code") == 200:
        messages.append(
            "/api/info/v2/rights доступен без токена, значит это, вероятно, "
            "публичный справочник прав, а не проверка авторизации."
        )
    elif rights_auth.get("status_code") == 200:
        messages.append(
            "/api/info/v2/rights вернул 200 только с токеном, но это всё равно "
            "похоже на справочник ролей, а не на проверку user access token."
        )

    if register:
        code = register.get("status_code")
        if code in (200, 201):
            messages.append(
                "/auth/client/register успешно создал OAuth-клиент. "
                "Проверьте client_id/client_secret в результате."
            )
        elif code == 401:
            messages.append(
                "/auth/client/register вернул 401: нет подходящей авторизации "
                "для регистрации OAuth-клиента."
            )
        elif code == 403:
            messages.append(
                "/auth/client/register вернул 403: пользователь распознан, "
                "но создание API/OAuth-клиента запрещено или scope/параметры не разрешены."
            )
        elif code:
            messages.append(f"/auth/client/register вернул HTTP {code}.")

    oauth_token = results.get("oauth_token", {}).get("access_token_masked")
    if oauth_token:
        messages.append(
            "Access token через OAuth получен. Его можно использовать в TENDERPLAN_TOKEN "
            "или отдельной переменной для сборщика."
        )

    tenders_original = results.get("auth_token", {}).get("tenders_getlist", {})
    tenders_oauth = results.get("oauth_token", {}).get("tenders_getlist", {})

    if tenders_original.get("status_code") == 200:
        messages.append("GET /api/tenders/getlist работает с исходным TENDERPLAN_TOKEN.")
    elif tenders_oauth.get("status_code") == 200:
        messages.append("GET /api/tenders/getlist работает с OAuth access_token.")
    else:
        if tenders_original.get("status_code"):
            messages.append(
                "GET /api/tenders/getlist пока не подтвердился. "
                "Смотрите HTTP-код и тело ответа в JSON-отчёте."
            )

    return messages


def main() -> int:
    # override=True важно: .env перезапишет старый export TENDERPLAN_TOKEN в shell.
    load_dotenv(override=True)

    base_url = os.getenv("TENDERPLAN_BASE_URL", "https://tenderplan.ru").rstrip("/")
    token = os.getenv("TENDERPLAN_TOKEN", "").strip()
    timeout = int(os.getenv("TENDERPLAN_TIMEOUT", "30"))
    try_register = env_bool("TENDERPLAN_TRY_REGISTER", False)
    register_redirect_uri = os.getenv(
        "TENDERPLAN_REGISTER_REDIRECT_URI",
        "https://lebedeve.ru/tenderplan/oauth/callback",
    )
    register_scope = os.getenv(
        "TENDERPLAN_REGISTER_SCOPE",
        "resources:external",
    )
    register_display_name = os.getenv(
        "TENDERPLAN_REGISTER_DISPLAY_NAME",
        "Torg Monitor",
    )
    list_params = load_json_env("TENDERPLAN_LIST_PARAMS_JSON", {"limit": 10})

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "tenderplan-api-check/1.0",
        }
    )

    results: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "env": {
            "TENDERPLAN_TOKEN_present": bool(token),
            "TENDERPLAN_TOKEN_masked": mask_secret(token),
            "TENDERPLAN_TRY_REGISTER": try_register,
            "TENDERPLAN_REGISTER_REDIRECT_URI": register_redirect_uri,
            "TENDERPLAN_REGISTER_SCOPE": register_scope,
            "TENDERPLAN_LIST_PARAMS_JSON": list_params,
        },
        "no_auth": {},
        "auth_token": {},
        "register": {},
        "oauth_token": {},
    }

    print_section("0. ENV")
    print("base_url:", base_url)
    print("token:", mask_secret(token))
    print("try_register:", try_register)
    print("redirect_uri:", register_redirect_uri)
    print("scope:", register_scope)
    print("list_params:", json.dumps(list_params, ensure_ascii=False))

    print_section("1. Проверка публичности /api/info/v2/rights без токена")
    rights_no_auth = call(
        session,
        "GET",
        f"{base_url}/api/info/v2/rights",
        timeout=timeout,
    )
    results["no_auth"]["rights"] = rights_no_auth
    log_result("NO AUTH: GET /api/info/v2/rights", rights_no_auth)

    if not token:
        print_section("Нет TENDERPLAN_TOKEN")
        print("Укажите TENDERPLAN_TOKEN в .env и повторите запуск.")
        save_json = json.dumps(results, ensure_ascii=False, indent=2)
        RESULT_FILE.write_text(save_json, encoding="utf-8")
        return 1

    auth_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    print_section("2. Проверка Bearer-токена")
    info_user = call(
        session,
        "GET",
        f"{base_url}/api/info/user",
        headers=auth_headers,
        timeout=timeout,
    )
    results["auth_token"]["info_user"] = info_user
    log_result("AUTH: GET /api/info/user", info_user)

    info_firm = call(
        session,
        "GET",
        f"{base_url}/api/info/firm",
        headers=auth_headers,
        timeout=timeout,
    )
    results["auth_token"]["info_firm"] = info_firm
    log_result("AUTH: GET /api/info/firm", info_firm)

    rights_auth = call(
        session,
        "GET",
        f"{base_url}/api/info/v2/rights",
        headers=auth_headers,
        timeout=timeout,
    )
    results["auth_token"]["rights"] = rights_auth
    log_result("AUTH: GET /api/info/v2/rights", rights_auth)

    print_section("3. Проверка GET /api/tenders/getlist с исходным Bearer")
    tenders_original = call(
        session,
        "GET",
        f"{base_url}/api/tenders/getlist",
        headers=auth_headers,
        params=list_params,
        timeout=timeout,
    )
    results["auth_token"]["tenders_getlist"] = tenders_original
    log_result("AUTH: GET /api/tenders/getlist", tenders_original)

    if try_register:
        print_section("4. POST /auth/client/register")

        register_body = {
            "displayName": register_display_name,
            "clientType": "confidential",
            "clientDomain": "user",
            "redirectURIs": [register_redirect_uri] if register_redirect_uri else [],
            "scope": register_scope,
        }

        results["register"]["request_body"] = register_body

        register_response = call(
            session,
            "POST",
            f"{base_url}/auth/client/register",
            headers=auth_headers,
            json_body=register_body,
            timeout=timeout,
        )
        results["register"]["response"] = register_response
        log_result("AUTH: POST /auth/client/register", register_response)

        client_id, client_secret = find_client_credentials(
            register_response.get("json")
        )
        results["register"]["client_id_masked"] = mask_secret(client_id)
        results["register"]["client_secret_masked"] = mask_secret(client_secret)

        if client_id and client_secret:
            print()
            print("Найдены client_id/client_secret в ответе.")
            print("client_id:", mask_secret(client_id))
            print("client_secret:", mask_secret(client_secret))

            print_section("5. POST /auth/client/token через client_credentials")

            # Стандартный OAuth-вариант: x-www-form-urlencoded.
            token_response = call(
                session,
                "POST",
                f"{base_url}/auth/client/token",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": register_scope,
                },
                timeout=timeout,
            )
            results["oauth_token"]["response_form"] = token_response
            log_result(
                "OAUTH FORM: POST /auth/client/token",
                token_response,
            )

            access_token = find_access_token(token_response.get("json"))

            # Если form-вариант не сработал, пробуем JSON-вариант.
            if not access_token:
                token_response_json = call(
                    session,
                    "POST",
                    f"{base_url}/auth/client/token",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json_body={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": register_scope,
                    },
                    timeout=timeout,
                )
                results["oauth_token"]["response_json"] = token_response_json
                log_result(
                    "OAUTH JSON: POST /auth/client/token",
                    token_response_json,
                )
                access_token = find_access_token(token_response_json.get("json"))

            results["oauth_token"]["access_token_masked"] = mask_secret(access_token)

            if access_token:
                oauth_headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                }

                print_section("6. Проверка API с OAuth access_token")

                oauth_user = call(
                    session,
                    "GET",
                    f"{base_url}/api/info/user",
                    headers=oauth_headers,
                    timeout=timeout,
                )
                results["oauth_token"]["info_user"] = oauth_user
                log_result("OAUTH: GET /api/info/user", oauth_user)

                oauth_firm = call(
                    session,
                    "GET",
                    f"{base_url}/api/info/firm",
                    headers=oauth_headers,
                    timeout=timeout,
                )
                results["oauth_token"]["info_firm"] = oauth_firm
                log_result("OAUTH: GET /api/info/firm", oauth_firm)

                oauth_tenders = call(
                    session,
                    "GET",
                    f"{base_url}/api/tenders/getlist",
                    headers=oauth_headers,
                    params=list_params,
                    timeout=timeout,
                )
                results["oauth_token"]["tenders_getlist"] = oauth_tenders
                log_result("OAUTH: GET /api/tenders/getlist", oauth_tenders)
            else:
                print()
                print("Access token через /auth/client/token не найден.")
        else:
            print()
            print("client_id/client_secret в ответе register не найдены.")
    else:
        print_section("4. Регистрация OAuth-клиента пропущена")
        print("Чтобы попробовать POST /auth/client/register, добавьте в .env:")
        print("TENDERPLAN_TRY_REGISTER=true")
        print()
        print("Тело запроса будет таким:")
        print(json.dumps({
            "displayName": register_display_name,
            "clientType": "confidential",
            "clientDomain": "user",
            "redirectURIs": [register_redirect_uri] if register_redirect_uri else [],
            "scope": register_scope,
        }, ensure_ascii=False, indent=2))

    print_section("Итоговая диагностика")
    messages = explain(results)
    results["diagnosis"] = messages

    for message in messages:
        print("-", message)

    RESULT_FILE.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print(f"Полный отчёт сохранён: {RESULT_FILE.resolve()}")
    print("В отчёте токены замаскированы, но всё равно не отправляйте его наружу без проверки.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())