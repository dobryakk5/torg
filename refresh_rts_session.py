#!/usr/bin/env python3
"""
refresh_rts_session.py — разовый слепок сессии витрины ЗМО РТС-Тендер (отладка).

Витрина market.rts-tender.ru сидит за DDoS-Guard, а её API
(zmo-new-webapi.rts-tender.ru) требует куки + заголовок `token`, которые
(проверено живьём 18.07.2026):
  - ПРИВЯЗАНЫ К IP, с которого выданы — браузер ОБЯЗАН ходить через тот же
    прокси, что и коннектор (SOURCES_PROXY_URL);
  - токен живёт МЕНЬШЕ МИНУТЫ вне браузера — SPA его постоянно перевыпускает.

Поэтому ОСНОВНОЙ канал (sources/rts_tender.py) этим скриптом больше не
пользуется: он держит собственный браузер открытым и делает поиск fetch'ем из
контекста страницы (см. _browser_fetch там). Отсюда коннектор импортирует
только _playwright_proxy.

Этот скрипт остаётся для отладки: снять слепок сессии в .env
(RTS_COOKIE_STRING / RTS_TOKEN) и тут же, в первые секунды, проверить запрос
руками (например, curl'ом).

Запуск:
    python3 refresh_rts_session.py            # тихо, headless
    python3 refresh_rts_session.py --headed   # с видимым окном (если капча)
    python3 refresh_rts_session.py --check    # после обновления проверить канал
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")
SEARCH_URL = (
    "https://market.rts-tender.ru/search/sell?s=4&q=%D1%81%D0%B0%D0%B9%D1%82"
    "&sts=1&sts=2&smp=0&dsoswo=false&isHomeReg=false&tst=0&sort=-PublicationDate"
)
API_MARKER = "zmo-new-webapi.rts-tender.ru"


def _playwright_proxy() -> dict | None:
    """Прокси площадок (config.SOURCES_PROXY_URL) в формате Playwright.

    Браузер ОБЯЗАН ходить через тот же прокси, что и запросы канала: сессия
    привязана к IP, и токен/куки, полученные с «чужого» IP, работать не будут.
    """
    from urllib.parse import urlparse

    try:
        import config
        url = str(getattr(config, "SOURCES_PROXY_URL", "") or "").strip()
    except Exception:
        url = ""
    if not url:
        return None
    p = urlparse(url)
    if not p.hostname:
        return None
    proxy: dict = {"server": f"{p.scheme or 'http'}://{p.hostname}:{p.port or 80}"}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def refresh_session(headed: bool = False, timeout_s: int = 120) -> tuple[str, str] | None:
    """Возвращает (cookie_string, token) или None.

    Сессия считается добытой, когда SPA сам выполнил запрос к publicsearch2
    (или любому API zmo-new-webapi) с непустым заголовком token.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright не установлен. Один раз выполни:\n"
              "    pip install playwright && playwright install chromium",
              file=sys.stderr)
        return None

    proxy = _playwright_proxy()
    captured: dict = {"token": ""}

    with sync_playwright() as p:
        browser = None
        # Родной Chrome предпочтительнее: настоящий отпечаток браузера.
        # Без AutomationControlled анти-бот распознаёт headless и после
        # JS-челленджа не пускает дальше (проверено 18.07.2026).
        launch_args = ["--disable-blink-features=AutomationControlled"]
        for launch_kwargs in (
            {"channel": "chrome", "headless": not headed},
            {"headless": not headed},
        ):
            try:
                browser = p.chromium.launch(proxy=proxy, args=launch_args, **launch_kwargs)
                break
            except Exception:
                continue
        if browser is None:
            print("Не удалось запустить браузер (нет ни Chrome, ни chromium Playwright).",
                  file=sys.stderr)
            return None

        context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.new_page()

        def on_request(request) -> None:
            if API_MARKER in request.url and not captured["token"]:
                token = request.headers.get("token", "")
                if token:
                    captured["token"] = token

        page.on("request", on_request)

        # Челлендж иногда завершается сетевой ошибкой перезагрузки (прокси
        # флапает) — тогда повторный goto проходит уже с выданными куками.
        deadline = time.time() + timeout_s
        captcha_warned = False
        nav_attempts = 0
        while time.time() < deadline and not captured["token"]:
            nav_attempts += 1
            try:
                page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45_000)
            except Exception as e:
                print(f"goto (попытка {nav_attempts}): {str(e)[:100]}", file=sys.stderr)

            while time.time() < deadline and not captured["token"]:
                title = ""
                body_head = ""
                try:
                    title = page.title()
                    body_head = page.evaluate("document.body.innerText.slice(0,120)")
                except Exception:
                    pass
                # Chrome-страница сетевой ошибки → новый goto (куки уже могли выдать).
                if "Не удается" in body_head or "ERR_" in body_head:
                    break
                # Интерактивная капча (не JS-челлендж «Anti-DDoS защита», тот
                # проходит сам) — в headless не решается.
                if "captcha" in title.lower() and not captcha_warned:
                    captcha_warned = True
                    if headed:
                        print("Показана капча — пройди её в открытом окне, "
                              "дальше всё сохранится само…")
                    else:
                        print("Анти-бот показал интерактивную капчу — headless её не пройдёт.\n"
                              "Запусти с видимым окном:  python3 refresh_rts_session.py --headed",
                              file=sys.stderr)
                        browser.close()
                        return None
                page.wait_for_timeout(1000)

        # Куки собираем по обоим доменам: challenge-куки market + сессионные webapi.
        cookie_pairs: dict[str, str] = {}
        for origin in ("https://market.rts-tender.ru", "https://zmo-new-webapi.rts-tender.ru"):
            try:
                for c in context.cookies(origin):
                    cookie_pairs[c["name"]] = c["value"]
            except Exception:
                pass
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_pairs.items())
        browser.close()

    if not captured["token"]:
        print("Не дождался запроса SPA с заголовком token — сессия не добыта.",
              file=sys.stderr)
        return None
    return cookie_str, captured["token"]


def update_env(cookie: str, token: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []

    def upsert(key: str, value: str) -> None:
        nonlocal lines
        prefix = f"{key}="
        for i, ln in enumerate(lines):
            if ln.startswith(prefix):
                lines[i] = prefix + value
                return
        lines.append(prefix + value)

    upsert("RTS_COOKIE_STRING", cookie)
    upsert("RTS_TOKEN", token)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_and_save(headed: bool = False) -> str | None:
    """Обновляет сессию и пишет её в .env. Возвращает token или None."""
    result = refresh_session(headed=headed)
    if not result:
        return None
    cookie_str, token = result

    update_env(cookie_str, token)
    print("✅ .env обновлён:")
    print("   RTS_TOKEN =", token[:40] + ("…" if len(token) > 40 else ""))
    print("   RTS_COOKIE_STRING =", cookie_str[:80] + ("…" if len(cookie_str) > 80 else ""))
    return token


def main() -> None:
    ap = argparse.ArgumentParser(description="Автообновление сессии РТС-Тендер через браузер")
    ap.add_argument("--headed", action="store_true",
                    help="видимое окно браузера (если анти-бот требует капчу)")
    ap.add_argument("--check", action="store_true",
                    help="после обновления проверить канал запросом")
    args = ap.parse_args()

    token = refresh_and_save(headed=args.headed)
    if not token:
        sys.exit(1)

    if args.check:
        print("\nПроверяю канал (python -m sources.rts_tender 'сайт')…")
        subprocess.run([sys.executable, "-m", "sources.rts_tender", "сайт"],
                       cwd=str(Path(__file__).parent))


if __name__ == "__main__":
    main()
