"""
show_details.py — показать распарсенные детальные блоки одного лота ЕИС.

Использование:
    python show_details.py                      # лот по умолчанию (пример из задачи)
    python show_details.py 0318500001226000005  # по номеру закупки (regNumber)
    python show_details.py "https://zakupki.gov.ru/epz/order/notice/zk20/view/common-info.html?regNumber=0318500001226000005"
    python show_details.py 0318500001226000005 --json   # сырой JSON
    python show_details.py 0318500001226000005 --raw     # + полный сырой текст секций

Скрипт тянет страницу common-info, прогоняет через scraper.parse_common_info_details
и печатает результат. Требует доступа к zakupki.gov.ru (запускать на сервере/машине,
где сеть к рунету есть — из песочницы не работает).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import fetch_tender_details, to_common_info_url

DEFAULT = "0318500001226000005"


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _line(label: str, value) -> None:
    print(f"  {label:<28} {value}")


def _yesno(v) -> str:
    return "Да" if v is True else "Нет" if v is False else "—"


def render(details: dict, show_raw: bool = False) -> None:
    if not details:
        print("⚠️  Детали не получены (страница не загрузилась или вёрстка не распознана).")
        return

    ex  = details.get("execution") or {}
    fin = details.get("financing") or {}
    obj = details.get("object") or {}
    pref = details.get("preferences") or {}
    sec  = details.get("contract_security") or {}

    print("\n━━ 1. СРОКИ ИСПОЛНЕНИЯ И ИСТОЧНИКИ ФИНАНСИРОВАНИЯ ━━")
    _line("Дата начала исполнения:", f"{ex.get('start_days', '—')} кал. дней")
    _line("Срок исполнения:", f"{ex.get('duration_days', '—')} кал. дней")
    _line("Собственные средства:", _yesno(ex.get("own_funds")))

    print("\n━━ 2. ФИНАНСОВОЕ ОБЕСПЕЧЕНИЕ — ЗА КАКИЕ ПЕРИОДЫ И СКОЛЬКО ━━")
    # Нулевые периоды не показываем — финансирования по ним нет (вне контракта).
    for y in (fin.get("years") or []):
        if y.get("amount"):
            _line(f"  {y.get('label')}:", _fmt_price(y.get("amount")))
    _line("Всего:", _fmt_price(fin.get("total")))

    print("\n━━ 3. ОБЪЕКТ ЗАКУПКИ — ТИП ЛОТА, УСЛУГА, ЦЕНА ━━")
    if obj.get("type_comment"):
        _line("Тип лота:", obj["type_comment"])
    elif obj.get("unit_price_contract") is False:
        _line("Тип лота:", "Фиксированная цена контракта")
    for p in (obj.get("positions") or []):
        _line("Код позиции:", p.get("code", "—"))
        print("  Услуга:")
        for ln in (p.get("name") or "—").splitlines():
            print(f"    {ln}")
        if p.get("unit") or p.get("unit_price") is not None:
            _line("Цена за ед.:", f"{p.get('unit','—')} · {_fmt_price(p.get('unit_price'))}")
    _line("НМЦК (всего):", _fmt_price(fin.get("total")))

    print("\n━━ 4. ПРЕИМУЩЕСТВА / ТРЕБОВАНИЯ К УЧАСТНИКАМ ━━")
    _line("Преимущества установлены:", _yesno(pref.get("established")))
    _line("  (текст):", pref.get("text") or "—")
    special = details.get("requirements_special")
    _line("Спец. требования:", _yesno(special))
    # Типовые требования ст. 31 не показываем — их наличие и есть «спец. требований нет».
    if special:
        reqs = details.get("requirements") or []
        if reqs:
            print("  Требования:")
            for i, r in enumerate(reqs, 1):
                print(f"    {i}. {r}")

    print("\n━━ 5. ОБЕСПЕЧЕНИЕ ИСПОЛНЕНИЯ КОНТРАКТА ━━")
    _line("Требуется:", _yesno(sec.get("required")))
    amt = _fmt_price(sec.get("amount"))
    if sec.get("percent") is not None:
        amt += f"  ({sec['percent']:g} %)"
    _line("Размер:", amt if sec.get("amount") is not None else "—")

    if show_raw:
        print("\n━━ СЫРОЙ ТЕКСТ СЕКЦИЙ ━━")
        for name, block in (("execution", ex), ("financing", fin), ("object", obj)):
            raw = (block or {}).get("raw")
            if raw:
                print(f"\n[{name}]\n{raw}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Показать детальные блоки лота ЕИС")
    ap.add_argument("target", nargs="?", default=DEFAULT,
                    help="regNumber или полный URL common-info (по умолчанию — пример из задачи)")
    ap.add_argument("--json", action="store_true", help="вывести сырой JSON деталей")
    ap.add_argument("--raw", action="store_true", help="показать сырой текст секций")
    args = ap.parse_args()

    url = to_common_info_url(args.target)
    print(f"Лот: {url}", file=sys.stderr)

    details = fetch_tender_details(url)

    if args.json:
        print(json.dumps(details, ensure_ascii=False, indent=2))
    else:
        render(details, show_raw=args.raw)


if __name__ == "__main__":
    main()
