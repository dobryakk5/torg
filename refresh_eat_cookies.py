#!/usr/bin/env python3
"""
refresh_eat_cookies.py — автоматическое обновление анти-бот куков ЕАТ («Берёзка»).

Анти-бот agregatoreat.ru (ServicePipe) выдаёт короткоживущие куки
__hash_/__lhash_ (TTL ~6 часов, см. куку __js_p_=...,21600,...) после
JS-челленджа, который обычный браузер проходит сам, без участия человека.
Скрипт запускает Chromium через Playwright, заходит на площадку, ждёт
прохождения челленджа и сохраняет куки + User-Agent в .env
(EAT_COOKIE / EAT_USER_AGENT).

Если вместо JS-челленджа показана интерактивная слайдер-капча (бывает у
«подозрительных» IP) — headless-режим её пройти не может и НЕ пытается:
запусти с --headed, перетащи слайдер один раз руками, куки сохранятся сами.

Установка (один раз):
    pip install playwright
    playwright install chromium     # не нужно, если есть Google Chrome — используем его

Запуск:
    python3 refresh_eat_cookies.py            # тихо, headless
    python3 refresh_eat_cookies.py --headed   # с видимым окном (для слайдера)
    python3 refresh_eat_cookies.py --check    # после обновления проверить канал

Также вызывается автоматически из sources/eat.py при отказе анти-бота
(флаг EAT_AUTO_REFRESH=1 в .env).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

TARGET_URL = "https://agregatoreat.ru/purchases/new"
WANTED = ("__hash_", "__lhash_", "__rhash_")


def _playwright_proxy() -> dict | None:
    """Прокси площадок (config.SOURCES_PROXY_URL) в формате Playwright.

    Браузер ОБЯЗАН ходить через тот же прокси, что и запросы канала: куки
    анти-бота привязаны к IP, и полученные с «чужого» IP работать не будут
    (а с не-российского вместо JS-челленджа показывается слайдер-капча).
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


def refresh_cookies(headed: bool = False, timeout_s: int = 90) -> tuple[str, str] | None:
    """Возвращает (cookie_string, user_agent) или None.

    Челлендж считается пройденным, когда в контексте появились __hash_ и __lhash_.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright не установлен. Один раз выполни:\n"
              "    pip install playwright && playwright install chromium",
              file=sys.stderr)
        return None

    proxy = _playwright_proxy()
    with sync_playwright() as p:
        browser = None
        # Родной Chrome предпочтительнее: настоящий отпечаток браузера.
        for launch_kwargs in (
            {"channel": "chrome", "headless": not headed},
            {"headless": not headed},
        ):
            try:
                browser = p.chromium.launch(proxy=proxy, **launch_kwargs)
                break
            except Exception:
                continue
        if browser is None:
            print("Не удалось запустить браузер (нет ни Chrome, ни chromium Playwright).",
                  file=sys.stderr)
            return None

        context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        page = context.new_page()
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            print(f"Страница не загрузилась: {e}", file=sys.stderr)
            browser.close()
            return None

        deadline = time.time() + timeout_s
        cookie_str = ""
        captcha_warned = False
        while time.time() < deadline:
            cookies = {c["name"]: c["value"] for c in context.cookies("https://agregatoreat.ru")}
            if "__hash_" in cookies and "__lhash_" in cookies:
                cookie_str = "; ".join(
                    f"{name}={cookies[name]}" for name in WANTED if name in cookies
                )
                break
            title = ""
            try:
                title = page.title()
            except Exception:
                pass
            if "captcha" in title.lower() and not captcha_warned:
                captcha_warned = True
                if headed:
                    print("Показана слайдер-капча — перетащи ползунок в открытом окне, "
                          "дальше всё сохранится само…")
                else:
                    print("Анти-бот показал интерактивную капчу — headless её не пройдёт.\n"
                          "Запусти с видимым окном:  python3 refresh_eat_cookies.py --headed",
                          file=sys.stderr)
                    browser.close()
                    return None
            page.wait_for_timeout(1000)

        ua = ""
        try:
            ua = page.evaluate("navigator.userAgent")
        except Exception:
            pass
        browser.close()

    if not cookie_str:
        print("Не дождался куков __hash_/__lhash_ — челлендж не прошёл.", file=sys.stderr)
        return None
    return cookie_str, ua


def refresh_and_save(headed: bool = False) -> str | None:
    """Обновляет куки и пишет их в .env. Возвращает cookie-строку или None."""
    result = refresh_cookies(headed=headed)
    if not result:
        return None
    cookie_str, ua = result

    from update_eat_cookie import update_env
    update_env(cookie_str, ua)
    print("✅ .env обновлён:")
    print("   EAT_COOKIE =", cookie_str[:80] + ("…" if len(cookie_str) > 80 else ""))
    if ua:
        print("   EAT_USER_AGENT =", ua[:70] + ("…" if len(ua) > 70 else ""))
    return cookie_str


def main() -> None:
    ap = argparse.ArgumentParser(description="Автообновление куков ЕАТ через браузер")
    ap.add_argument("--headed", action="store_true",
                    help="видимое окно браузера (если анти-бот требует слайдер)")
    ap.add_argument("--check", action="store_true",
                    help="после обновления проверить канал запросом")
    args = ap.parse_args()

    cookie = refresh_and_save(headed=args.headed)
    if not cookie:
        sys.exit(1)

    if args.check:
        print("\nПроверяю канал (python -m sources.eat 'сайт')…")
        subprocess.run([sys.executable, "-m", "sources.eat", "сайт"],
                       cwd=str(Path(__file__).parent))


if __name__ == "__main__":
    main()
