#!/usr/bin/env python3
"""
update_eat_cookie.py — обновляет куки ЕАТ («Берёзка») в .env из cURL-команды.

Анти-бот agregatoreat.ru завязан на короткоживущие куки (__hash_/__lhash_/__rhash_),
поэтому их приходится периодически освежать. Этот скрипт вытаскивает куки и
User-Agent из cURL-команды браузера и прописывает их в .env (EAT_COOKIE,
EAT_USER_AGENT) — руками ничего парсить не нужно.

Как пользоваться (macOS):
  1. Открой agregatoreat.ru → раздел закупок → DevTools (F12) → Network →
     найди запрос list-published-trade-lots → правой кнопкой → Copy → Copy as cURL.
  2. Запусти:  python3 update_eat_cookie.py
     Скрипт возьмёт cURL из буфера обмена (pbpaste), обновит .env и,
     если хочешь, сразу проверит канал.

Другие источники cURL:
  python3 update_eat_cookie.py --file curl.txt     # из файла
  pbpaste | python3 update_eat_cookie.py --stdin    # из stdin
  python3 update_eat_cookie.py --check              # обновить и проверить запросом
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Скрипт живёт в utils/, а config.py читает .env из корня проекта.
# with_name(".env") ошибочно писал в utils/.env, поэтом канал
# продолжал видеть старые cookie.
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

# -b '...'  /  --cookie '...'  (кавычки одинарные или двойные)
_COOKIE_RE = re.compile(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", re.S)
# -H 'cookie: ...'  (иногда куки лежат в заголовке, а не в -b)
_COOKIE_HEADER_RE = re.compile(r"-H\s+(['\"])cookie:\s*(.*?)\1", re.I | re.S)
_UA_RE = re.compile(r"-H\s+(['\"])user-agent:\s*(.*?)\1", re.I | re.S)

# Из всего cookie-набора оставляем полный комплект ServicePipe.
_WANTED = ("__js_p_", "__jhash_", "__jua_", "__hash_", "__lhash_", "__rhash_")


def read_curl(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8", errors="ignore")
    if args.stdin:
        return sys.stdin.read()
    # По умолчанию — буфер обмена macOS.
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        print("Не удалось прочитать буфер обмена (pbpaste). Передай cURL через "
              "--file <путь> или --stdin.", file=sys.stderr)
        sys.exit(2)


def extract_cookie(curl: str) -> str:
    m = _COOKIE_RE.search(curl) or _COOKIE_HEADER_RE.search(curl)
    if not m:
        return ""
    raw = m.group(2)
    # Оставляем только нужные анти-бот токены (без яндекс-метрики _ym_* и прочего).
    kept = []
    for part in raw.split(";"):
        name = part.strip().split("=", 1)[0].strip()
        if name in _WANTED and part.strip():
            kept.append(part.strip())
    return "; ".join(kept)


def extract_ua(curl: str) -> str:
    m = _UA_RE.search(curl)
    return m.group(2).strip() if m else ""


def update_env(cookie: str, ua: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []

    def upsert(key: str, value: str) -> None:
        nonlocal lines
        prefix = f"{key}="
        for i, ln in enumerate(lines):
            if ln.startswith(prefix):
                lines[i] = prefix + value
                return
        lines.append(prefix + value)

    upsert("EAT_COOKIE", cookie)
    if ua:
        upsert("EAT_USER_AGENT", ua)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # config._load_dotenv использует setdefault, поэтому простой записи файла
    # недостаточно: subprocess наследует старые значения. Обновляем
    # окружение и уже импортированный config как единую пару.
    os.environ["EAT_COOKIE"] = cookie
    if ua:
        os.environ["EAT_USER_AGENT"] = ua
    try:
        import config

        config.EAT_COOKIE = cookie
        if ua:
            config.EAT_USER_AGENT = ua
    except ImportError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Обновить куки ЕАТ в .env из cURL")
    ap.add_argument("--file", help="прочитать cURL из файла")
    ap.add_argument("--stdin", action="store_true", help="прочитать cURL из stdin")
    ap.add_argument("--check", action="store_true", help="после обновления проверить канал запросом")
    args = ap.parse_args()

    curl = read_curl(args)
    if "agregatoreat" not in curl:
        print("В переданном тексте нет cURL к agregatoreat.ru — проверь, что "
              "скопировал 'Copy as cURL' нужного запроса.", file=sys.stderr)
        sys.exit(2)

    cookie = extract_cookie(curl)
    ua = extract_ua(curl)
    if not cookie:
        print("Не нашёл анти-бот куки (__hash_/__lhash_/__rhash_) в cURL.", file=sys.stderr)
        sys.exit(2)

    update_env(cookie, ua)
    print("✅ .env обновлён:")
    print("   EAT_COOKIE =", cookie)
    if ua:
        print("   EAT_USER_AGENT =", ua[:70] + ("…" if len(ua) > 70 else ""))

    if args.check:
        print("\nПроверяю канал (python -m sources.eat 'сайт')…")
        subprocess.run([sys.executable, "-m", "sources.eat", "сайт"])


if __name__ == "__main__":
    main()
