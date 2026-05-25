"""
web_app.py — FastAPI веб-интерфейс тендерного монитора.

Запуск:
    uvicorn web_app:app --reload --port 8000

Страницы:
    /                  — дашборд: все тендеры, фильтрация по решению и 8 фильтрам
    /tender/{number}   — детали тендера: все 8 фильтров, LLM-анализ

API (JSON):
    /api/stats         — расширенная статистика
    /api/tenders       — список тендеров (?decision=GO&f3_min=3&f5_min=4&limit=50)
"""

from __future__ import annotations

import re
import logging
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import database as db          # ← новый database.py (двухэтапная схема)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Тендерный монитор",
    description="Веб-интерфейс двухэтапного монитора с 8-фильтровым скорингом",
    version="2.0.0",
)

templates = Jinja2Templates(directory="templates")


# ── Jinja2 фильтры ─────────────────────────────────────────────────────────

def fmt_price(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.0f} ₽".replace(",", "\u2009")
    except (TypeError, ValueError):
        return str(value)


def fmt_date(value: str) -> str:
    if not value:
        return "—"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    if m:
        y, mo, d = m.groups()
        return f"{d}.{mo}.{y}"
    return str(value)[:10]


def score_color(score: int) -> str:
    mapping = {1: "#EF4444", 2: "#F97316", 3: "#EAB308", 4: "#84CC16", 5: "#22C55E"}
    return mapping.get(int(score or 0), "#3A3A55")


def decision_class(decision: str) -> str:
    return {"GO": "go", "CAUTION": "caution", "NO-GO": "nogo"}.get(decision or "", "unknown")


def parse_signals(signals_str: str) -> list[str]:
    if not signals_str:
        return []
    return [s.strip() for s in signals_str.split("|") if s.strip()]


templates.env.filters["fmt_price"]      = fmt_price
templates.env.filters["fmt_date"]       = fmt_date
templates.env.filters["score_color"]    = score_color
templates.env.filters["decision_class"] = decision_class
templates.env.filters["parse_signals"]  = parse_signals


# ── Lifecycle ──────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    db.init_db()
    logger.info("БД подключена")


@app.on_event("shutdown")
async def on_shutdown():
    db.close_db()


# ── Страницы ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(
    request:    Request,
    decision:   Optional[str]   = Query(None),
    law:        Optional[str]   = Query(None),
    price_from: Optional[float] = Query(None),
    price_to:   Optional[float] = Query(None),
    # Per-filter минимальные оценки (1–5)
    f1_min: Optional[int] = Query(None, ge=1, le=5),
    f2_min: Optional[int] = Query(None, ge=1, le=5),
    f3_min: Optional[int] = Query(None, ge=1, le=5),
    f4_min: Optional[int] = Query(None, ge=1, le=5),
    f5_min: Optional[int] = Query(None, ge=1, le=5),
    f6_min: Optional[int] = Query(None, ge=1, le=5),
    f7_min: Optional[int] = Query(None, ge=1, le=5),
    f8_min: Optional[int] = Query(None, ge=1, le=5),
    limit:  int           = Query(300, ge=1, le=1000),
):
    stats = db.get_stats_extended()

    filter_kwargs = dict(
        price_min=price_from, price_max=price_to, law_type=law,
        f1_min=f1_min, f2_min=f2_min, f3_min=f3_min, f4_min=f4_min,
        f5_min=f5_min, f6_min=f6_min, f7_min=f7_min, f8_min=f8_min,
        limit=limit,
    )

    groups = {
        "GO":      db.get_top_tenders(decision="GO",      **filter_kwargs),
        "CAUTION": db.get_top_tenders(decision="CAUTION", **filter_kwargs),
        "NO-GO":   db.get_top_tenders(decision="NO-GO",   **filter_kwargs),
    }

    all_tenders = []
    for rows in groups.values():
        all_tenders.extend(rows)
    all_tenders.sort(key=lambda t: (t.get("filter_total") or 0), reverse=True)

    active = (decision or "ALL").upper()

    # Текущие значения фильтров для шаблона
    fmins = {f"f{n}_min": locals().get(f"f{n}_min") or 1 for n in range(1, 9)}
    current_filters = dict(
        decision=active, law=law or "",
        price_from=int(price_from) if price_from else "",
        price_to=int(price_to)     if price_to   else "",
        **fmins,
    )

    return templates.TemplateResponse("index.html", {
        "request":         request,
        "stats":           stats,
        "groups":          groups,
        "all_tenders":     all_tenders,
        "active":          active,
        "current_filters": current_filters,
    })


@app.get("/tender/{purchase_number}", response_class=HTMLResponse)
async def tender_detail(request: Request, purchase_number: str):
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(status_code=404, detail="Тендер не найден")
    return templates.TemplateResponse("detail.html", {
        "request": request,
        "tender":  tender,
    })


# ── JSON API ───────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    return db.get_stats_extended()


@app.get("/api/tenders")
async def api_tenders(
    decision:   Optional[str]   = None,
    price_min:  Optional[float] = None,
    price_max:  Optional[float] = None,
    law_type:   Optional[str]   = None,
    f1_min: Optional[int] = Query(None, ge=1, le=5),
    f2_min: Optional[int] = Query(None, ge=1, le=5),
    f3_min: Optional[int] = Query(None, ge=1, le=5),
    f4_min: Optional[int] = Query(None, ge=1, le=5),
    f5_min: Optional[int] = Query(None, ge=1, le=5),
    f6_min: Optional[int] = Query(None, ge=1, le=5),
    f7_min: Optional[int] = Query(None, ge=1, le=5),
    f8_min: Optional[int] = Query(None, ge=1, le=5),
    limit:  int           = 50,
):
    rows = db.get_top_tenders(
        decision=decision, limit=limit,
        price_min=price_min, price_max=price_max, law_type=law_type,
        f1_min=f1_min, f2_min=f2_min, f3_min=f3_min, f4_min=f4_min,
        f5_min=f5_min, f6_min=f6_min, f7_min=f7_min, f8_min=f8_min,
    )
    return JSONResponse(content=rows)
