"""
web_app.py — FastAPI-приложение: веб-интерфейс + контрольная панель.

Запуск (после python init_db.py):
    uvicorn web_app:app --host 0.0.0.0 --port 8000

Страницы:
    /           — дашборд тендеров
    /analytics  — ценовые коридоры, заказчики
    /control    — управление: настройки, запуск задач, статус, логи
    /customer/{inn}
    /tender/{number}

API:
    /api/run/{job}            — запустить задачу фоном (POST)
    /api/status               — статус всех задач (GET)
    /api/settings             — получить настройки (GET)
    /api/settings             — сохранить настройки (POST)
    /api/tenders, /api/stats, /api/bid/{pnum}, ...
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import config
import database as db

logger = logging.getLogger(__name__)

app = FastAPI(title="Тендерный монитор", version="3.0.0")
templates = Jinja2Templates(directory="templates")


# ══════════════════════════════════════════════════════════════════════════════
# Фоновые задачи — простой менеджер потоков
# ══════════════════════════════════════════════════════════════════════════════

class JobRunner:
    """Запускает именованные задачи в фоновых потоках и хранит статус."""

    _jobs: dict[str, dict] = {}
    _lock  = threading.Lock()

    JOBS = {
        "stage1":    "Stage 1 — поиск карточек",
        "stage2":    "Stage 2 — анализ ТЗ",
        "stage3":    "Stage 3 — результаты",
        "all":       "Полный цикл (1+2)",
        "analytics": "Аналитика (коридоры + заказчики + детектор)",
    }

    @classmethod
    def start(cls, name: str) -> tuple[bool, str]:
        with cls._lock:
            job = cls._jobs.get(name, {})
            if job.get("status") == "running":
                return False, "already_running"
            cls._jobs[name] = {
                "status":      "running",
                "started_at":  datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "result":      None,
                "error":       None,
            }

        fn = cls._resolve(name)
        def _worker():
            try:
                result = fn()
                with cls._lock:
                    cls._jobs[name].update(
                        status="done",
                        finished_at=datetime.now().isoformat(timespec="seconds"),
                        result=str(result) if result is not None else "ok",
                    )
            except Exception as exc:
                logger.exception("Job %s failed", name)
                with cls._lock:
                    cls._jobs[name].update(
                        status="error",
                        finished_at=datetime.now().isoformat(timespec="seconds"),
                        error=str(exc),
                    )

        threading.Thread(target=_worker, daemon=True, name=f"job-{name}").start()
        return True, "started"

    @classmethod
    def status(cls) -> dict:
        with cls._lock:
            return {
                name: dict(cls._jobs.get(name, {"status": "idle"}))
                for name in cls.JOBS
            }

    @staticmethod
    def _resolve(name: str):
        """Возвращает функцию для запуска задачи."""
        # Импортируем лениво, чтобы не тащить всё при старте веб-сервера
        from main import (
            run_stage1, run_stage2, run_stage3,
            run_once, run_analytics,
        )

        # Каждая задача читает runtime-settings перед запуском
        def _with_runtime_config(fn):
            def wrapper():
                _reload_runtime_config()
                return fn()
            return wrapper

        mapping = {
            "stage1":    _with_runtime_config(run_stage1),
            "stage2":    _with_runtime_config(run_stage2),
            "stage3":    _with_runtime_config(run_stage3),
            "all":       _with_runtime_config(run_once),
            "analytics": _with_runtime_config(run_analytics),
        }
        fn = mapping.get(name)
        if not fn:
            raise ValueError(f"Неизвестная задача: {name}")
        return fn


def _reload_runtime_config() -> None:
    """Перечитывает все runtime-настройки из БД перед запуском задачи."""
    config.PRICE_MIN                     = config.get_runtime("PRICE_MIN",                    config.PRICE_MIN)
    config.PRICE_MAX                     = config.get_runtime("PRICE_MAX",                    config.PRICE_MAX)
    config.PUBLISH_DAYS_BACK             = config.get_runtime("PUBLISH_DAYS_BACK",            config.PUBLISH_DAYS_BACK)
    config.SCHEDULE_HOURS                = config.get_runtime("SCHEDULE_HOURS",               config.SCHEDULE_HOURS)
    config.MIN_PRIMARY_SCORE_FOR_DETAIL  = config.get_runtime("MIN_PRIMARY_SCORE_FOR_DETAIL", config.MIN_PRIMARY_SCORE_FOR_DETAIL)
    config.MIN_DETAILED_SCORE_FOR_NOTIFY = config.get_runtime("MIN_DETAILED_SCORE_FOR_NOTIFY",config.MIN_DETAILED_SCORE_FOR_NOTIFY)
    config.SEARCH_KEYWORDS               = config.get_runtime("SEARCH_KEYWORDS",              config.SEARCH_KEYWORDS)
    config.OKPD2_SEARCH_ENABLED          = config.get_runtime("OKPD2_SEARCH_ENABLED",         config.OKPD2_SEARCH_ENABLED)
    config.OKPD2_CODES                   = config.get_runtime("OKPD2_CODES",                  config.OKPD2_CODES)


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def on_startup():
    db.connect_db()    # только открываем пул, без DDL
    db.check_db()      # проверяем, что init_db.py был запущен
    _reload_runtime_config()
    logger.info("Веб-приложение запущено")


@app.on_event("shutdown")
async def on_shutdown():
    db.close_db()


# ══════════════════════════════════════════════════════════════════════════════
# Jinja2 фильтры
# ══════════════════════════════════════════════════════════════════════════════

def fmt_price(v) -> str:
    if v is None: return "—"
    try: return f"{float(v):,.0f} ₽".replace(",", "\u2009")
    except: return str(v)

def fmt_date(v: str) -> str:
    if not v: return "—"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else str(v)[:10]

def score_color(s: int) -> str:
    return {1:"#EF4444",2:"#F97316",3:"#EAB308",4:"#84CC16",5:"#22C55E"}.get(int(s or 0),"#3A3A55")

def decision_class(d: str) -> str:
    return {"GO":"go","CAUTION":"caution","NO-GO":"nogo"}.get(d or "","unknown")

def parse_signals(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split("|") if x.strip()]

def parse_stop(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split("|") if x.strip()]

for name, fn in [("fmt_price",fmt_price),("fmt_date",fmt_date),
                  ("score_color",score_color),("decision_class",decision_class),
                  ("parse_signals",parse_signals),("parse_stop",parse_stop)]:
    templates.env.filters[name] = fn


# ══════════════════════════════════════════════════════════════════════════════
# Страницы
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    decision: Optional[str]   = None,
    law:      Optional[str]   = None,
    price_from: Optional[float] = None,
    price_to:   Optional[float] = None,
    f1_min: Optional[int] = Query(None,ge=1,le=5),
    f2_min: Optional[int] = Query(None,ge=1,le=5),
    f3_min: Optional[int] = Query(None,ge=1,le=5),
    f4_min: Optional[int] = Query(None,ge=1,le=5),
    f5_min: Optional[int] = Query(None,ge=1,le=5),
    f6_min: Optional[int] = Query(None,ge=1,le=5),
    f7_min: Optional[int] = Query(None,ge=1,le=5),
    f8_min: Optional[int] = Query(None,ge=1,le=5),
    limit: int = Query(300,ge=1,le=1000),
):
    stats = db.get_stats_extended()
    fkw = dict(price_min=price_from, price_max=price_to, law_type=law,
               f1_min=f1_min, f2_min=f2_min, f3_min=f3_min, f4_min=f4_min,
               f5_min=f5_min, f6_min=f6_min, f7_min=f7_min, f8_min=f8_min, limit=limit)
    groups = {k: db.get_top_tenders(decision=k, **fkw) for k in ("GO","CAUTION","NO-GO")}
    all_tenders = sorted(
        [t for rows in groups.values() for t in rows],
        key=lambda t: (t.get("filter_total") or 0), reverse=True,
    )
    fmins = {f"f{n}_min": locals().get(f"f{n}_min") or 1 for n in range(1,9)}
    return templates.TemplateResponse("index.html", {
        "request": request, "stats": stats, "groups": groups,
        "all_tenders": all_tenders,
        "active": (decision or "ALL").upper(),
        "current_filters": dict(
            decision=(decision or "ALL").upper(), law=law or "",
            price_from=int(price_from) if price_from else "",
            price_to=int(price_to)   if price_to   else "",
            **fmins,
        ),
    })


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    return templates.TemplateResponse("analytics.html", {
        "request":   request,
        "corridors": db.get_all_price_corridors(),
        "customers": db.get_customers_list(limit=50),
        "risky":     db.get_customers_list(limit=20, only_risky=True),
    })


@app.get("/control", response_class=HTMLResponse)
async def control_page(request: Request):
    settings = db.get_all_settings()
    stats    = db.get_stats_extended()
    job_status = JobRunner.status()

    # Последние 20 запусков из таблицы runs
    try:
        import psycopg2.extras
        with db._conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT 20"
            )
            runs = [dict(r) for r in cur.fetchall()]
    except Exception:
        runs = []

    return templates.TemplateResponse("control.html", {
        "request":    request,
        "settings":   settings,
        "stats":      stats,
        "jobs":       JobRunner.JOBS,
        "job_status": job_status,
        "runs":       runs,
    })


@app.get("/customer/{inn}", response_class=HTMLResponse)
async def customer_page(request: Request, inn: str):
    customer = db.get_customer(inn)
    if not customer:
        raise HTTPException(404, "Заказчик не найден")
    import psycopg2.extras
    with db._conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT purchase_number, title, price, law_type, filter_total,
                   filter_decision, final_price, price_drop_percent,
                   winner_name, deadline, status
            FROM tenders WHERE customer_inn = %s ORDER BY created_at DESC LIMIT 50
        """, (inn,))
        tenders = [dict(r) for r in cur.fetchall()]
    return templates.TemplateResponse("customer.html",
        {"request": request, "customer": customer, "tenders": tenders})


@app.get("/tender/{purchase_number}", response_class=HTMLResponse)
async def tender_detail(request: Request, purchase_number: str):
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    return templates.TemplateResponse("detail.html",
        {"request": request, "tender": tender})


# ══════════════════════════════════════════════════════════════════════════════
# API — Управление задачами
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/run/{job_name}")
async def api_run_job(job_name: str):
    if job_name not in JobRunner.JOBS:
        raise HTTPException(400, f"Неизвестная задача: {job_name}")
    started, reason = JobRunner.start(job_name)
    if not started:
        return JSONResponse({"ok": False, "reason": reason}, status_code=409)
    return {"ok": True, "job": job_name, "message": f"Задача {job_name} запущена"}


@app.get("/api/status")
async def api_status():
    return JobRunner.status()


# ══════════════════════════════════════════════════════════════════════════════
# API — Настройки
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/settings")
async def api_get_settings():
    return db.get_all_settings()


@app.post("/api/settings")
async def api_save_settings(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Ожидается JSON-объект {key: value}")

    allowed = {
        "PRICE_MIN", "PRICE_MAX", "PUBLISH_DAYS_BACK", "SCHEDULE_HOURS",
        "MIN_PRIMARY_SCORE_FOR_DETAIL", "MIN_DETAILED_SCORE_FOR_NOTIFY",
        "OKPD2_SEARCH_ENABLED", "OKPD2_CODES", "SEARCH_KEYWORDS",
        "CHANGE_CHECK_HOURS", "CHANGE_MIN_SCORE", "WINNER_ANALYTICS_PAGES",
    }
    saved = {}
    for key, value in body.items():
        if key not in allowed:
            continue
        str_val = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
        db.set_setting(key, str_val)
        saved[key] = str_val

    # Применяем немедленно в текущем процессе
    _reload_runtime_config()
    return {"ok": True, "saved": saved}


# ══════════════════════════════════════════════════════════════════════════════
# API — Данные
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/stats")
async def api_stats():
    return db.get_stats_extended()


@app.get("/api/tenders")
async def api_tenders(
    decision: Optional[str] = None,
    price_min: Optional[float] = None, price_max: Optional[float] = None,
    law_type: Optional[str] = None,
    f1_min: Optional[int] = Query(None,ge=1,le=5),
    f2_min: Optional[int] = Query(None,ge=1,le=5),
    f3_min: Optional[int] = Query(None,ge=1,le=5),
    f4_min: Optional[int] = Query(None,ge=1,le=5),
    f5_min: Optional[int] = Query(None,ge=1,le=5),
    f6_min: Optional[int] = Query(None,ge=1,le=5),
    f7_min: Optional[int] = Query(None,ge=1,le=5),
    f8_min: Optional[int] = Query(None,ge=1,le=5),
    limit: int = 50,
):
    return JSONResponse(content=db.get_top_tenders(
        decision=decision, limit=limit,
        price_min=price_min, price_max=price_max, law_type=law_type,
        f1_min=f1_min, f2_min=f2_min, f3_min=f3_min, f4_min=f4_min,
        f5_min=f5_min, f6_min=f6_min, f7_min=f7_min, f8_min=f8_min,
    ))


@app.get("/api/bid/{purchase_number}")
async def api_bid(purchase_number: str):
    from winner_analytics import classify_category, recommend_bid as _calc_bid
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    nmck = tender.get("price") or 0
    if not nmck:
        return JSONResponse({"error": "НМЦК не указана"}, status_code=400)
    result = _calc_bid(nmck, classify_category(tender.get("title","")), tender.get("law_type","44-ФЗ"))
    result["purchase_number"] = purchase_number
    result["nmck"] = nmck
    return JSONResponse(content=result)


@app.get("/api/customers")
async def api_customers(only_risky: bool = False, limit: int = 100):
    return JSONResponse(content=db.get_customers_list(limit=limit, only_risky=only_risky))


@app.get("/api/corridors")
async def api_corridors():
    return JSONResponse(content=db.get_all_price_corridors())
