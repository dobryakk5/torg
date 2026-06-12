"""
Export Tenderland search results to JSON and mark tenders that are practical
to execute with heavy LLM assistance.

Tenderland is an authenticated JS app, so plain requests often see only the
landing page or an empty shell. This script uses Playwright with a persistent
browser profile. On first run, log in manually in the opened browser, then
rerun with the same --profile-dir.

Install optional dependency:
    python -m pip install playwright
    python -m playwright install chromium

Example:
    python tenderland_export.py \
      --url "https://tenderland.ru/Search/Entities?type=Sber&id=378049" \
      --limit 20 \
      --details \
      --out data/tenderland_378049.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_URL = "https://tenderland.ru/Search/Entities?type=Sber&id=378049"

GOOD_LLM_PATTERNS = [
    ("разработка сайта", 5, "разработка сайта"),
    ("разработка официального сайта", 5, "официальный сайт"),
    ("промо-сайт", 5, "промо-сайт"),
    ("редизайн", 5, "редизайн"),
    ("верстка", 4, "верстка"),
    ("интеграция", 2, "интеграция"),
    ("1с-битрикс", 3, "1С-Битрикс"),
    ("wordpress", 4, "WordPress"),
    ("создание и размещение информационных материалов", 4, "контент"),
    ("производству и размещению информации", 4, "контент/СМИ"),
    ("техническое задание", 2, "есть ТЗ"),
    ("описание объекта закупки", 2, "есть описание объекта закупки"),
]

BAD_LLM_PATTERNS = [
    ("лиценз", -6, "лицензии"),
    ("сертификат", -5, "сертификаты"),
    ("контентной фильтрации", -5, "контентная фильтрация"),
    ("хостинг", -2, "хостинг"),
    ("сопровождение", -1, "сопровождение без разработки"),
    ("техническая поддержка", -2, "поддержка"),
    ("единственного поставщика", -6, "единственный поставщик"),
    ("закупка у единственного", -6, "единственный поставщик"),
    ("мотивационный трафик", -4, "трафик/маркетинг"),
]


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def parse_price(text: str) -> float | None:
    value = clean(text).replace("₽", "").replace(" ", "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else None


def parse_deadline(text: str) -> datetime | None:
    value = clean(text)
    match = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})", value)
    if not match:
        return None
    return datetime.strptime(" ".join(match.groups()), "%d.%m.%Y %H:%M")


def score_llm_fit(item: dict[str, Any]) -> tuple[int, list[str]]:
    blob = " ".join(
        str(item.get(key, "") or "")
        for key in ("title", "category", "type", "module", "docs_text", "detail_text")
    ).lower()
    score = 0
    reasons: list[str] = []

    for pattern, points, label in GOOD_LLM_PATTERNS:
        if pattern in blob:
            score += points
            reasons.append(f"+{points}: {label}")

    for pattern, points, label in BAD_LLM_PATTERNS:
        if pattern in blob:
            score += points
            reasons.append(f"{points}: {label}")

    price = parse_price(str(item.get("price", "")))
    if price is not None:
        if 100_000 <= price <= 700_000:
            score += 3
            reasons.append("+3: нормальный бюджет")
        elif 30_000 <= price < 100_000:
            score += 1
            reasons.append("+1: небольшой бюджет")
        elif price == 0:
            reasons.append("0: цена не раскрыта")
        elif price < 30_000:
            score -= 3
            reasons.append("-3: слишком маленький бюджет")

    deadline = parse_deadline(str(item.get("deadline") or item.get("end") or ""))
    if deadline:
        days_left = (deadline - datetime.now()).days
        if days_left < 0:
            score -= 8
            reasons.append("-8: срок подачи прошел")
        elif days_left < 2:
            score -= 4
            reasons.append("-4: меньше 2 дней на заявку")
        elif days_left >= 5:
            score += 2
            reasons.append("+2: есть время на заявку")

    return score, reasons


async def extract_rows(page, limit: int) -> list[dict[str, Any]]:
    return await page.evaluate(
        """
        (limit) => {
          const clean = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
          const anchors = Array.from(document.querySelectorAll('a[href*="/Entity/Tender?id="]'));
          const seen = new Set();
          const items = [];
          for (const a of anchors) {
            const href = new URL(a.getAttribute('href'), location.origin).href;
            if (seen.has(href)) continue;
            seen.add(href);
            const row = a.closest('[role="row"]') || a.closest('tr') || a.parentElement;
            const rowText = clean(row?.textContent || '');
            const field = (label) => {
              const labels = [
                'Реестровый номер', 'Начальная цена', 'Дата публикации',
                'Дата окончания подачи заявок', 'Категория лота',
                'Наименование заказчика', 'Регион', 'Тип тендера',
                'Модуль', 'Ссылка на площадку'
              ];
              const escaped = label.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
              const rest = labels.filter(x => x !== label).map(x => x.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')).join('|');
              const re = new RegExp(escaped + '(.+?)(?=' + rest + '|$)');
              const m = rowText.match(re);
              return clean(m?.[1] || '');
            };
            items.push({
              title: clean(a.textContent),
              href,
              notice: field('Реестровый номер'),
              price: field('Начальная цена'),
              published: field('Дата публикации'),
              deadline: field('Дата окончания подачи заявок'),
              category: field('Категория лота'),
              customer: field('Наименование заказчика'),
              region: field('Регион'),
              type: field('Тип тендера'),
              module: field('Модуль'),
              platform: field('Ссылка на площадку'),
            });
            if (items.length >= limit) break;
          }
          return items;
        }
        """,
        limit,
    )


async def enrich_details(page, item: dict[str, Any]) -> dict[str, Any]:
    await page.goto(item["href"], wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)
    detail = await page.evaluate(
        """
        () => {
          const clean = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
          const lines = (document.body?.innerText || '').split(/\\n+/).map(clean).filter(Boolean);
          const after = (label) => {
            const i = lines.findIndex(x => x === label || x.startsWith(label + ' '));
            if (i < 0) return '';
            if (lines[i] !== label) return clean(lines[i].slice(label.length));
            return lines[i + 1] || '';
          };
          const links = Array.from(document.querySelectorAll('a')).map(a => ({
            text: clean(a.textContent),
            href: a.href
          })).filter(a => a.text);
          const docs = links.filter(a => /docx|pdf|html|Техничес|Задан|Потребност|Извещ|Описание/i.test(a.text));
          return {
            status: after('Статус закупки'),
            end: after('Окончание подачи заявок'),
            organizer: after('Размещение осуществляет'),
            customer_full: after('Заказчик'),
            inn: after('ИНН'),
            platform_full: after('Площадка'),
            place: after('Место доставки товара, выполнения работы или оказания услуги'),
            docs,
            docs_text: docs.map(x => x.text).join(' | '),
            detail_text: lines.slice(0, 80).join(' | ')
          };
        }
        """
    )
    item.update(detail)
    item["llm_fit_score"], item["llm_fit_reasons"] = score_llm_fit(item)
    return item


async def run(args: argparse.Namespace) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: playwright. Install with "
            "`python -m pip install playwright && python -m playwright install chromium`."
        ) from exc

    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=args.headless,
            viewport={"width": 1400, "height": 900},
            locale="ru-RU",
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(args.url, wait_until="domcontentloaded")
        await page.wait_for_timeout(args.wait_ms)

        rows = await extract_rows(page, args.limit)
        if not rows:
            print("No rows found. If login is required, log in in the opened browser and rerun.")

        for row in rows:
            row["llm_fit_score"], row["llm_fit_reasons"] = score_llm_fit(row)

        if args.details:
            for idx, row in enumerate(rows, start=1):
                print(f"[{idx}/{len(rows)}] {row.get('notice')}: {row.get('title')[:90]}")
                await enrich_details(page, row)

        rows.sort(key=lambda x: x.get("llm_fit_score", 0), reverse=True)

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved {len(rows)} tenders to {out}")

        await context.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--out", default="data/tenderland_export.json")
    parser.add_argument("--profile-dir", default=".tenderland_profile")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--wait-ms", type=int, default=3000)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
