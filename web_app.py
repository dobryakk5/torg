"""
web_app.py — FastAPI-приложение: веб-интерфейс + контрольная панель.

Запуск (после python init_db.py):
    uvicorn web_app:app --host 0.0.0.0 --port 8000

Страницы:
    /           — дашборд тендеров
    /analytics  — ценовые коридоры, заказчики
    /control    — управление: настройки, запуск задач, статус, логи
    /kb         — база знаний
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

import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import zipfile

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import config
import database as db

logger = logging.getLogger(__name__)

app = FastAPI(title="Тендерный монитор", version="3.0.0")
templates = Jinja2Templates(directory="templates")

# ── MVP4a: стадии работы по тендеру (Kanban-доска) ───────────────────────────
WORK_STAGES = [
    {"key": "lead",      "label": "Интересно",      "color": "neutral", "active": True},
    {"key": "preparing", "label": "Готовлю заявку",  "color": "blue",    "active": True},
    {"key": "submitted", "label": "Заявка подана",   "color": "purple",  "active": True},
    {"key": "bidding",   "label": "Идут торги",      "color": "orange",  "active": True},
    {"key": "won",       "label": "Выиграл",         "color": "green",   "active": True},
    {"key": "executing", "label": "Исполнение",      "color": "green",   "active": True},
    {"key": "done",      "label": "Закрыт",          "color": "gray",    "active": False},
    {"key": "lost",      "label": "Проиграл",        "color": "red",     "active": False},
]
WORK_STAGE_KEYS = {s["key"] for s in WORK_STAGES}


# ══════════════════════════════════════════════════════════════════════════════
# Фоновые задачи — простой менеджер потоков
# ══════════════════════════════════════════════════════════════════════════════

class JobRunner:
    """Запускает именованные задачи в фоновых потоках и хранит статус."""

    _jobs: dict[str, dict] = {}
    _lock  = threading.Lock()

    JOBS = {
        "stage1":    "Stage 1 — поиск карточек",
        "pipeline":  "Конвейер (поиск + документы + пересчёт)",
        "triage":    "LLM-триаж карточек (по кнопке)",
        "rescore":   "Пересчёт скоринга (без сети/LLM)",
        "stage2":    "Stage 2 — анализ ТЗ",
        "stage3":    "Stage 3 — результаты",
        "all":       "Полный цикл (1+2)",
        "analytics": "Аналитика (коридоры + заказчики + детектор)",
    }

    @classmethod
    def start(cls, name: str, params: dict | None = None) -> tuple[bool, str]:
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
                "params":      params or {},
            }

        fn = cls._resolve(name, params or {})
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
    def _resolve(name: str, params: dict):
        """Возвращает функцию для запуска задачи."""
        # Импортируем лениво, чтобы не тащить всё при старте веб-сервера
        from main import (
            run_stage1, run_triage, run_rescore, run_stage2, run_stage3,
            run_once, run_analytics,
        )

        # Каждая задача читает runtime-settings перед запуском
        def _with_runtime_config(fn, **kwargs):
            def wrapper():
                _reload_runtime_config()
                return fn(**kwargs)
            return wrapper

        if name == "stage1":
            return _with_runtime_config(
                run_stage1,
                keywords=params.get("keywords") or None,
                price_min=params.get("price_min") or None,
                price_max=params.get("price_max") or None,
                days_back=params.get("days_back") or None,
                date_from=params.get("date_from") or None,
                date_to=params.get("date_to") or None,
                fz44=params.get("fz44", None),
                fz223=params.get("fz223", None),
                okpd2=params.get("okpd2", None),
                b2b=params.get("b2b", None),
            )

        if name == "pipeline":
            # Полный конвейер отдельным процессом: поиск (сегодня+вчера) → документы → пересчёт.
            import subprocess, sys as _sys
            from pathlib import Path as _Path
            def _run_pipeline():
                script = _Path(__file__).resolve().parent / "pipeline.py"
                proc = subprocess.run(
                    [_sys.executable, str(script)],
                    capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    raise RuntimeError((proc.stderr or proc.stdout or "pipeline failed")[-600:])
                tail = [l for l in (proc.stdout or "").splitlines() if l.strip()][-3:]
                return " | ".join(tail) or "ok"
            return _run_pipeline

        mapping = {
            "triage":    _with_runtime_config(run_triage),
            "rescore":   _with_runtime_config(run_rescore),
            "stage2":    _with_runtime_config(run_stage2),
            "stage3":    _with_runtime_config(run_stage3),
            "all":       _with_runtime_config(run_once),
            "analytics": _with_runtime_config(run_analytics),
        }
        fn = mapping.get(name)
        if not fn:
            raise ValueError(f"Неизвестная задача: {name}")
        return fn


class SearchRunner:
    """
    Фоновый поиск по списку поисковых фраз с прогрессом по каждой фразе.

    В отличие от JobRunner.stage1, этот раннер:
      • принимает произвольный список фраз из экрана поиска;
      • отмечает статус каждой фразы (pending → running → done/error) — для галочек в UI;
      • поддерживает остановку (Stop) через threading.Event;
      • для лотов с первичным скором ≥ порога сразу подгружает детальные блоки
        страницы common-info (сроки/финансирование/объект/требования).
    """

    _lock  = threading.Lock()
    _stop  = threading.Event()
    _state: dict = {"status": "idle", "phrases": [], "started_at": None,
                    "finished_at": None, "found": 0, "saved": 0, "error": None}

    @classmethod
    def start(cls, phrases: list[str], params: dict | None = None) -> tuple[bool, str]:
        params = params or {}
        phrases = [p.strip() for p in phrases if p and p.strip()]
        if not phrases:
            return False, "no_phrases"
        with cls._lock:
            if cls._state.get("status") == "running":
                return False, "already_running"
            cls._stop.clear()
            cls._state = {
                "status":      "running",
                "started_at":  datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "found":       0,
                "saved":       0,
                "error":       None,
                "params":      params,
                "phrases": [
                    {"phrase": p, "status": "pending", "found": 0,
                     "saved": 0, "candidates": 0, "error": None}
                    for p in phrases
                ],
            }

        threading.Thread(target=cls._worker, args=(params,),
                         daemon=True, name="search-runner").start()
        return True, "started"

    @classmethod
    def stop(cls) -> None:
        cls._stop.set()

    @classmethod
    def status(cls) -> dict:
        with cls._lock:
            return json.loads(json.dumps(cls._state, default=str))

    @classmethod
    def _set_phrase(cls, idx: int, **kw) -> None:
        with cls._lock:
            try:
                cls._state["phrases"][idx].update(kw)
            except (IndexError, KeyError):
                pass

    @classmethod
    def _worker(cls, params: dict) -> None:
        from scraper import search_eis, fetch_tender_details
        from filter_engine import run_stage1_filters

        _reload_runtime_config()
        price_min = params.get("price_min") or config.PRICE_MIN
        price_max = params.get("price_max") or config.PRICE_MAX
        days_back = params.get("days_back")
        if days_back in (None, ""):
            days_back = config.PUBLISH_DAYS_BACK
        date_from = str(params.get("date_from") or getattr(config, "PUBLISH_DATE_FROM", "") or "").strip() or None
        date_to = str(params.get("date_to") or getattr(config, "PUBLISH_DATE_TO", "") or "").strip() or None
        fz44  = params.get("fz44",  config.SEARCH_44FZ)
        fz223 = params.get("fz223", config.SEARCH_223FZ)
        pages = int(params.get("pages") or config.SEARCH_PAGES)
        detail_threshold = config.MIN_PRIMARY_SCORE_FOR_DETAIL

        with cls._lock:
            phrases = [p["phrase"] for p in cls._state["phrases"]]

        total_found = total_saved = 0
        for idx, phrase in enumerate(phrases):
            if cls._stop.is_set():
                cls._set_phrase(idx, status="stopped")
                continue
            cls._set_phrase(idx, status="running")
            try:
                tenders = search_eis(
                    keyword=phrase, price_from=price_min, price_to=price_max,
                    fz44=fz44, fz223=fz223, pages=pages, days_back=days_back,
                    date_from=date_from, date_to=date_to,
                )
                db.reconnect_db()
                saved = candidates = 0
                for tender in tenders:
                    if cls._stop.is_set():
                        break
                    pnum = tender.get("purchase_number", "")
                    if not pnum:
                        continue
                    primary_text = tender.get("primary_text", "")
                    fr = run_stage1_filters(tender, primary_text)
                    score = fr.total_score
                    tender["filter_decision"] = fr.decision
                    tender["filter_scores"]   = fr.to_filter_scores()
                    tender["filter_stop"]     = " | ".join(fr.stop_factors)
                    mk = sorted(set(tender.get("matched_keywords") or []) | {phrase})
                    db.upsert_primary(tender, score, fr.to_reasons(), mk)
                    db.save_filter_result(fr, stage="stage1")
                    saved += 1
                    if score >= detail_threshold:
                        candidates += 1
                        # Тянем детали только если их ещё нет — на повторном
                        # прогоне дособираем недостающие, уже собранные не трогаем.
                        if db.get_tender_details(pnum) is None:
                            try:
                                det = fetch_tender_details(tender.get("url", ""), price=tender.get("price"))
                                if det:
                                    db.save_tender_details(pnum, det)
                            except Exception as exc:
                                logger.warning("Детали %s не собраны: %s", pnum, exc)
                total_found += len(tenders)
                total_saved += saved
                cls._set_phrase(idx, status="done", found=len(tenders),
                               saved=saved, candidates=candidates)
                with cls._lock:
                    cls._state["found"] = total_found
                    cls._state["saved"] = total_saved
            except Exception as exc:
                logger.exception("Поиск по '%s' упал", phrase)
                cls._set_phrase(idx, status="error", error=str(exc))

        with cls._lock:
            cls._state["status"] = "stopped" if cls._stop.is_set() else "done"
            cls._state["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _reload_runtime_config() -> None:
    """Перечитывает все runtime-настройки из БД перед запуском задачи."""
    config.PRICE_MIN                     = config.get_runtime("PRICE_MIN",                    config.PRICE_MIN)
    config.PRICE_MAX                     = config.get_runtime("PRICE_MAX",                    config.PRICE_MAX)
    config.PUBLISH_DAYS_BACK             = config.get_runtime("PUBLISH_DAYS_BACK",            config.PUBLISH_DAYS_BACK)
    config.PUBLISH_DATE_FROM             = config.get_runtime("PUBLISH_DATE_FROM",            config.PUBLISH_DATE_FROM)
    config.PUBLISH_DATE_TO               = config.get_runtime("PUBLISH_DATE_TO",              config.PUBLISH_DATE_TO)
    config.SCHEDULE_HOURS                = config.get_runtime("SCHEDULE_HOURS",               config.SCHEDULE_HOURS)
    config.MIN_PRIMARY_SCORE_FOR_DETAIL  = config.get_runtime("MIN_PRIMARY_SCORE_FOR_DETAIL", config.MIN_PRIMARY_SCORE_FOR_DETAIL)
    config.MIN_DETAILED_SCORE_FOR_NOTIFY = config.get_runtime("MIN_DETAILED_SCORE_FOR_NOTIFY",config.MIN_DETAILED_SCORE_FOR_NOTIFY)
    config.DOCUMENT_DOWNLOAD_MIN_SCORE   = config.get_runtime("DOCUMENT_DOWNLOAD_MIN_SCORE",  config.DOCUMENT_DOWNLOAD_MIN_SCORE)
    config.SEARCH_KEYWORDS               = config.get_runtime("SEARCH_KEYWORDS",              config.SEARCH_KEYWORDS)
    config.OKPD2_SEARCH_ENABLED          = config.get_runtime("OKPD2_SEARCH_ENABLED",         config.OKPD2_SEARCH_ENABLED)
    config.OKPD2_CODES                   = config.get_runtime("OKPD2_CODES",                  config.OKPD2_CODES)
    config.BACKFILL_SEARCH_PAGES         = config.get_runtime("BACKFILL_SEARCH_PAGES",        config.BACKFILL_SEARCH_PAGES)
    config.SOURCE_B2B_ENABLED            = config.get_runtime("SOURCE_B2B_ENABLED",           config.SOURCE_B2B_ENABLED)
    config.B2B_SEARCH_PAGES              = config.get_runtime("B2B_SEARCH_PAGES",             config.B2B_SEARCH_PAGES)
    config.LLM_PROVIDER                  = config.get_runtime("LLM_PROVIDER",                 config.LLM_PROVIDER)
    config.OPENROUTER_TRIAGE_MODEL       = config.get_runtime("OPENROUTER_TRIAGE_MODEL",      config.OPENROUTER_TRIAGE_MODEL)
    config.OPENROUTER_DEEP_MODEL         = config.get_runtime("OPENROUTER_DEEP_MODEL",        config.OPENROUTER_DEEP_MODEL)
    config.LLM_TRIAGE_ENABLED            = config.get_runtime("LLM_TRIAGE_ENABLED",           config.LLM_TRIAGE_ENABLED)


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def on_startup():
    db.connect_db()    # только открываем пул, без DDL
    db.check_db()      # проверяем, что init_db.py был запущен
    db.ensure_extra_columns()   # идемпотентный ALTER: details_json и пр.
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

def zakupki_common_info_url(url: str, purchase_number: str = "") -> str:
    value = f"{url or ''} {purchase_number or ''}"
    match = re.search(r"\b\d{11,22}\b", value)
    if not match:
        return url or ""
    reg_number = match.group(0)
    lower_value = value.lower()
    if "notice223" in lower_value or "/223/" in lower_value or re.fullmatch(r"3\d{10}", reg_number):
        return (
            "https://zakupki.gov.ru/epz/order/notice/notice223/common-info.html"
            f"?regNumber={reg_number}"
        )
    return (
        "https://zakupki.gov.ru/epz/order/notice/zk20/view/common-info.html"
        f"?regNumber={reg_number}"
    )


def zakupki_documents_url(url: str, purchase_number: str = "") -> str:
    value = f"{url or ''} {purchase_number or ''}"
    match = re.search(r"\b\d{11,22}\b", value)
    reg_number = match.group(0) if match else (purchase_number or "")
    source = url or ""
    lower_source = source.lower()
    parsed = urlparse(source) if source else None
    is_223 = "notice223" in lower_source or "/223/" in lower_source or re.fullmatch(r"3\d{10}", reg_number)
    if "documents.html" in lower_source:
        if is_223 and parsed:
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if "purchaseNoticeNumber" not in query:
                query["purchaseNoticeNumber"] = query.pop("regNumber", reg_number)
                return urlunparse(parsed._replace(query=urlencode(query)))
        return source
    if source and "common-info.html" in lower_source:
        parsed = urlparse(re.sub(r"common-info\.html", "documents.html", source, flags=re.I))
        if is_223:
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if "purchaseNoticeNumber" not in query:
                query["purchaseNoticeNumber"] = query.pop("regNumber", reg_number)
            return urlunparse(parsed._replace(query=urlencode(query)))
        return urlunparse(parsed)
    if source and "view.html" in lower_source:
        parsed = urlparse(source)
        path = re.sub(r"view\.html$", "documents.html", parsed.path, flags=re.I)
        return urlunparse(parsed._replace(path=path))
    if is_223:
        query = dict(parse_qsl(urlparse(source).query, keep_blank_values=True)) if source else {}
        query.setdefault("purchaseNoticeNumber", reg_number)
        return (
            "https://zakupki.gov.ru/epz/order/notice/notice223/documents.html?"
            + urlencode(query)
        )
    return (
        "https://zakupki.gov.ru/epz/order/notice/zk20/view/documents.html"
        f"?regNumber={reg_number}"
    )

for name, fn in [("fmt_price",fmt_price),("fmt_date",fmt_date),
                  ("score_color",score_color),("decision_class",decision_class),
                  ("parse_signals",parse_signals),("parse_stop",parse_stop),
                  ("zakupki_common_info_url",zakupki_common_info_url)]:
    templates.env.filters[name] = fn


# ══════════════════════════════════════════════════════════════════════════════
# Страницы
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    decision: Optional[str]   = None,
    law:      Optional[str]   = None,
    kw:       Optional[str]   = None,
    q:        Optional[str]   = None,
    exclude_kw: list[str] = Query(default=[]),
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
    sort_by: str = "score",
    order: str = "desc",
    limit: int = Query(300,ge=1,le=1000),
):
    stats = db.get_stats_extended()
    sort_by = sort_by if sort_by in {"score", "price", "phrase", "date", "deadline"} else "score"
    order = order if order in {"asc", "desc"} else "desc"
    fkw = dict(price_min=price_from, price_max=price_to, law_type=law,
               matched_keyword=kw or None,
               q=q or None,
               exclude_keywords=exclude_kw,
               f1_min=f1_min, f2_min=f2_min, f3_min=f3_min, f4_min=f4_min,
               f5_min=f5_min, f6_min=f6_min, f7_min=f7_min, f8_min=f8_min,
               sort_by=sort_by, order=order, limit=limit)
    groups = {k: db.get_top_tenders(decision=k, **fkw) for k in ("GO","CAUTION","NO-GO")}
    rejected_tenders = db.get_top_tenders(
        decision=None,
        manual_decision="rejected",
        include_rejected=True,
        **fkw,
    )
    all_tenders = [t for rows in groups.values() for t in rows]
    reverse = order == "desc"
    if sort_by == "price":
        all_tenders.sort(key=lambda t: (t.get("price") is None, t.get("price") or 0), reverse=reverse)
    elif sort_by == "phrase":
        all_tenders.sort(key=lambda t: (t.get("matched_keywords") or "").lower(), reverse=reverse)
    else:
        all_tenders.sort(key=lambda t: (t.get("filter_total") or 0), reverse=reverse)
    fmins = {f"f{n}_min": locals().get(f"f{n}_min") or 1 for n in range(1,9)}
    search_phrases = config.get_runtime("SEARCH_KEYWORDS", config.SEARCH_KEYWORDS)
    return templates.TemplateResponse(request, "index.html", {
        "stats": stats, "groups": groups,
        "all_tenders": all_tenders,
        "rejected_tenders": rejected_tenders,
        "active": (decision or "ALL").upper(),
        "search_phrases": search_phrases if isinstance(search_phrases, list) else [],
        "active_kw": kw or "",
        "active_q": q or "",
        "excluded_keywords": exclude_kw,
        "sort_by": sort_by,
        "sort_order": order,
        "current_filters": dict(
            decision=(decision or "ALL").upper(), law=law or "", kw=kw or "",
            exclude_kw=exclude_kw,
            price_from=int(price_from) if price_from else "",
            price_to=int(price_to)   if price_to   else "",
            sort_by=sort_by, order=order,
            **fmins,
        ),
    })


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    return templates.TemplateResponse(request, "analytics.html", {
        "corridors": db.get_all_price_corridors(),
        "customers": db.get_customers_list(limit=50),
        "risky":     db.get_customers_list(limit=20, only_risky=True),
    })


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    keywords = config.get_runtime("SEARCH_KEYWORDS", config.SEARCH_KEYWORDS)
    okpd2    = config.get_runtime("OKPD2_CODES", config.OKPD2_CODES)
    return templates.TemplateResponse(request, "search.html", {
        "keywords": keywords if isinstance(keywords, list) else [],
        "okpd2":    okpd2 if isinstance(okpd2, list) else [],
        "defaults": {
            "price_min": config.get_runtime("PRICE_MIN", config.PRICE_MIN),
            "price_max": config.get_runtime("PRICE_MAX", config.PRICE_MAX),
            "days_back": config.get_runtime("PUBLISH_DAYS_BACK", config.PUBLISH_DAYS_BACK),
            "fz44":      config.SEARCH_44FZ,
            "fz223":     config.SEARCH_223FZ,
        },
    })


@app.get("/control", response_class=HTMLResponse)
async def control_page(request: Request):
    settings = db.get_all_settings()
    stats    = db.get_stats_extended()
    job_status = JobRunner.status()

    # Ключевые слова — для удобного редактирования по одной фразе на строку.
    kw_list = config.get_runtime("SEARCH_KEYWORDS", config.SEARCH_KEYWORDS)
    if not isinstance(kw_list, list):
        kw_list = []
    search_keywords_text = "\n".join(kw_list)

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

    return templates.TemplateResponse(request, "control.html", {
        "settings":   settings,
        "search_keywords_text": search_keywords_text,
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
    return templates.TemplateResponse(request, "customer.html",
        {"customer": customer, "tenders": tenders})


@app.get("/tender/{purchase_number}", response_class=HTMLResponse)
async def tender_detail(request: Request, purchase_number: str):
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    if db.deadline_is_expired(tender.get("deadline")):
        raise HTTPException(410, "Срок подачи заявок истёк")
    card = None
    criteria = None
    spec = []
    spec_rollup = {"total": 0, "priced": 0, "count": 0}
    try:
        import decision_aid
        import document_processor as dp
        text = _decide_text(tender)
        card = decision_aid.build_card(tender, text=text)
        criteria = dp.extract_evaluation_criteria(text)
    except Exception:
        logger.exception("decision_aid/criteria build failed for %s", purchase_number)
    try:
        spec = db.list_tender_items(purchase_number)
        spec_rollup = _spec_rollup(spec)
    except Exception:
        logger.exception("list_tender_items failed for %s", purchase_number)
    return templates.TemplateResponse(request, "detail.html",
        {"tender": tender, "card": card, "criteria": criteria,
         "work_stages": WORK_STAGES, "spec": spec, "spec_rollup": spec_rollup})


@app.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    import filter_engine as fe
    rules = db.scoring_rules_list()
    # Группируем по номеру фильтра для шаблона
    by_dim: dict[int, list[dict]] = {}
    for r in rules:
        by_dim.setdefault(int(r.get("dim") or 0), []).append(r)
    return templates.TemplateResponse(request, "rules.html", {
        "rules":         rules,
        "by_dim":        by_dim,
        "filter_names":  fe.FILTER_NAMES,
        "bucket_labels": fe.BUCKET_LABELS,
        "default_count": sum(len(v) for v in fe.DEFAULT_BUCKETS.values()),
        "is_empty":      len(rules) == 0,
    })


@app.get("/kb", response_class=HTMLResponse)
async def kb_page(request: Request, section: str = "contracts"):
    return templates.TemplateResponse(request, "kb.html", {
        "section":      section,
        "stats":        db.kb_stats(),
        "contracts":    db.kb_contracts_list(),
        "competencies": db.kb_competencies_list(),
        "equipment":    db.kb_equipment_list(),
        "risk_rules":   db.kb_risk_rules_list(),
        "templates":    db.kb_templates_list(),
    })


# ══════════════════════════════════════════════════════════════════════════════
# API — Управление задачами
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/run/{job_name}")
async def api_run_job(job_name: str, request: Request):
    if job_name not in JobRunner.JOBS:
        raise HTTPException(400, f"Неизвестная задача: {job_name}")
    try:
        params = await request.json()
    except Exception:
        params = {}
    if not isinstance(params, dict):
        params = {}

    started, reason = JobRunner.start(job_name, params)
    if not started:
        return JSONResponse({"ok": False, "reason": reason}, status_code=409)
    return {"ok": True, "job": job_name, "params": params}


@app.get("/api/status")
async def api_status():
    return JobRunner.status()


@app.get("/api/search/options")
async def api_search_options():
    """Возвращает актуальные настройки поиска для панели Stage 1."""
    return {
        "keywords": config.get_runtime("SEARCH_KEYWORDS", config.SEARCH_KEYWORDS),
        "okpd2": config.get_runtime("OKPD2_CODES", config.OKPD2_CODES),
        "price_min": config.get_runtime("PRICE_MIN", config.PRICE_MIN),
        "price_max": config.get_runtime("PRICE_MAX", config.PRICE_MAX),
        "days_back": config.get_runtime("PUBLISH_DAYS_BACK", config.PUBLISH_DAYS_BACK),
        "date_from": config.get_runtime("PUBLISH_DATE_FROM", config.PUBLISH_DATE_FROM),
        "date_to": config.get_runtime("PUBLISH_DATE_TO", config.PUBLISH_DATE_TO),
        "fz44": config.SEARCH_44FZ,
        "fz223": config.SEARCH_223FZ,
        "okpd2_enabled": config.get_runtime("OKPD2_SEARCH_ENABLED", config.OKPD2_SEARCH_ENABLED),
        "b2b_enabled": config.get_runtime("SOURCE_B2B_ENABLED", config.SOURCE_B2B_ENABLED),
    }


@app.post("/api/search/start")
async def api_search_start(request: Request):
    """Запускает фоновый поиск по набору фраз с прогрессом по каждой фразе."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    phrases = body.get("phrases") or []
    if not isinstance(phrases, list):
        phrases = []
    params = {
        "price_min": body.get("price_min"),
        "price_max": body.get("price_max"),
        "days_back": body.get("days_back"),
        "date_from": body.get("date_from"),
        "date_to": body.get("date_to"),
        "pages":     body.get("pages"),
        "fz44":      bool(body.get("fz44", True)),
        "fz223":     bool(body.get("fz223", True)),
    }
    started, reason = SearchRunner.start(phrases, params)
    if not started:
        return JSONResponse({"ok": False, "reason": reason}, status_code=409)
    return {"ok": True}


@app.post("/api/search/stop")
async def api_search_stop():
    SearchRunner.stop()
    return {"ok": True}


@app.get("/api/search/progress")
async def api_search_progress():
    return JSONResponse(content=SearchRunner.status())


@app.get("/api/tender/{purchase_number}/details")
async def api_tender_details(purchase_number: str):
    """
    Возвращает детальные блоки лота. Если ещё не собирали (например, у лота
    низкий скор и фоновый поиск их не трогал) — грузит страницу common-info,
    парсит и кэширует в БД на лету.
    """
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    details = db.get_tender_details(purchase_number)
    if details is None:
        from scraper import fetch_tender_details
        try:
            details = fetch_tender_details(tender.get("url", ""), price=tender.get("price"))
        except Exception as exc:
            logger.warning("Не удалось собрать детали %s: %s", purchase_number, exc)
            details = {}
        if details:
            db.save_tender_details(purchase_number, details)
    return JSONResponse(content={"purchase_number": purchase_number, "details": details or {}})


def _display_download_name(path: Path) -> str:
    try:
        return path.name.encode("latin1").decode("utf-8")
    except Exception:
        return path.name


def _zip_documents(files: list[Path], purchase_number: str) -> Path:
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    zip_path = downloads / f"{purchase_number}_documents.zip"
    used_names: set[str] = set()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, path in enumerate(files, start=1):
            if not path.exists() or not path.is_file():
                continue
            arcname = _display_download_name(path)
            if arcname in used_names:
                arcname = f"{i}_{arcname}"
            used_names.add(arcname)
            zf.write(path, arcname=arcname)
    return zip_path


@app.post("/api/tender/{purchase_number}/documents/zip")
async def api_tender_documents_zip(purchase_number: str):
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")

    from document_processor import download_documents, hash_files
    from scraper import get_tender_page

    docs_url = zakupki_documents_url(tender.get("url") or "", purchase_number)
    html, _ = get_tender_page(docs_url)
    docs = download_documents(purchase_number, html, docs_url) if html else {"files": []}

    if not docs.get("files"):
        common_url = zakupki_common_info_url(tender.get("url") or "", purchase_number)
        html, _ = get_tender_page(common_url)
        docs = download_documents(purchase_number, html, common_url) if html else {"files": []}

    files = [Path(p) for p in docs.get("files", []) if Path(p).is_file()]
    if not files:
        raise HTTPException(502, "Документы не скачались с ЕИС")

    zip_path = _zip_documents(files, purchase_number)
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders SET
                document_count = %s,
                documents_dir = %s,
                documents_hash = %s,
                updated_at = NOW()
            WHERE purchase_number = %s
            """,
            (len(files), docs.get("dir", ""), hash_files(files), purchase_number),
        )
    return JSONResponse(content={
        "ok": True,
        "count": len(files),
        "zip_path": str(zip_path),
        "documents_url": docs_url,
    })


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
        "PUBLISH_DATE_FROM", "PUBLISH_DATE_TO",
        "MIN_PRIMARY_SCORE_FOR_DETAIL", "MIN_DETAILED_SCORE_FOR_NOTIFY",
        "DOCUMENT_DOWNLOAD_MIN_SCORE",
        "OKPD2_SEARCH_ENABLED", "OKPD2_CODES", "SEARCH_KEYWORDS",
        "CHANGE_CHECK_HOURS", "CHANGE_MIN_SCORE", "WINNER_ANALYTICS_PAGES",
        "SOURCE_B2B_ENABLED", "B2B_SEARCH_PAGES",
        "BACKFILL_SEARCH_PAGES",
        "LLM_PROVIDER", "OPENROUTER_TRIAGE_MODEL", "OPENROUTER_DEEP_MODEL",
        "LLM_TRIAGE_ENABLED",
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
    matched_keyword: Optional[str] = None,
    q: Optional[str] = None,
    exclude_keyword: list[str] = Query(default=[]),
    f1_min: Optional[int] = Query(None,ge=1,le=5),
    f2_min: Optional[int] = Query(None,ge=1,le=5),
    f3_min: Optional[int] = Query(None,ge=1,le=5),
    f4_min: Optional[int] = Query(None,ge=1,le=5),
    f5_min: Optional[int] = Query(None,ge=1,le=5),
    f6_min: Optional[int] = Query(None,ge=1,le=5),
    f7_min: Optional[int] = Query(None,ge=1,le=5),
    f8_min: Optional[int] = Query(None,ge=1,le=5),
    sort_by: str = "score",
    order:   str = "desc",
    limit: int = 50,
):
    filter_decision = decision
    manual_decision = None
    include_rejected = False
    if decision and decision.upper() in {"REJECTED", "ОТКАЗ"}:
        filter_decision = None
        manual_decision = "rejected"
        include_rejected = True
    return JSONResponse(content=db.get_top_tenders(
        decision=filter_decision, limit=limit,
        price_min=price_min, price_max=price_max, law_type=law_type,
        matched_keyword=matched_keyword, q=q,
        exclude_keywords=exclude_keyword,
        f1_min=f1_min, f2_min=f2_min, f3_min=f3_min, f4_min=f4_min,
        f5_min=f5_min, f6_min=f6_min, f7_min=f7_min, f8_min=f8_min,
        sort_by=sort_by, order=order,
        manual_decision=manual_decision, include_rejected=include_rejected,
    ))


@app.get("/api/bid/{purchase_number}")
async def api_bid(purchase_number: str):
    from winner_analytics import classify_category, recommend_bid as _calc_bid
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    if db.deadline_is_expired(tender.get("deadline")):
        raise HTTPException(410, "Срок подачи заявок истёк")
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


# ══════════════════════════════════════════════════════════════════════════════
# API — База знаний
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/kb/stats")
async def api_kb_stats():
    return db.kb_stats()


@app.get("/api/kb/contracts")
async def api_kb_contracts_list():
    return JSONResponse(content=db.kb_contracts_list())


@app.post("/api/kb/contracts")
async def api_kb_contract_save(request: Request):
    data = await request.json()
    row_id = db.kb_contract_save(data)
    from knowledge_base import invalidate_profile_cache
    invalidate_profile_cache()
    return {"ok": True, "id": row_id}


@app.delete("/api/kb/contracts/{row_id}")
async def api_kb_contract_delete(row_id: int):
    db.kb_contract_delete(row_id)
    return {"ok": True}


@app.get("/api/kb/competencies")
async def api_kb_competencies_list():
    return JSONResponse(content=db.kb_competencies_list())


@app.post("/api/kb/competencies")
async def api_kb_competency_save(request: Request):
    data = await request.json()
    row_id = db.kb_competency_save(data)
    from knowledge_base import invalidate_profile_cache
    invalidate_profile_cache()
    return {"ok": True, "id": row_id}


@app.delete("/api/kb/competencies/{row_id}")
async def api_kb_competency_delete(row_id: int):
    db.kb_competency_delete(row_id)
    from knowledge_base import invalidate_profile_cache
    invalidate_profile_cache()
    return {"ok": True}


@app.get("/api/kb/equipment")
async def api_kb_equipment_list():
    return JSONResponse(content=db.kb_equipment_list())


@app.post("/api/kb/equipment")
async def api_kb_equipment_save(request: Request):
    data = await request.json()
    row_id = db.kb_equipment_save(data)
    return {"ok": True, "id": row_id}


@app.delete("/api/kb/equipment/{row_id}")
async def api_kb_equipment_delete(row_id: int):
    db.kb_equipment_delete(row_id)
    return {"ok": True}


@app.get("/api/kb/risks")
async def api_kb_risks_list():
    return JSONResponse(content=db.kb_risk_rules_list())


@app.post("/api/kb/risks")
async def api_kb_risk_save(request: Request):
    data = await request.json()
    row_id = db.kb_risk_rule_save(data)
    from knowledge_base import invalidate_profile_cache
    invalidate_profile_cache()
    return {"ok": True, "id": row_id}


@app.delete("/api/kb/risks/{row_id}")
async def api_kb_risk_delete(row_id: int):
    db.kb_risk_rule_delete(row_id)
    from knowledge_base import invalidate_profile_cache
    invalidate_profile_cache()
    return {"ok": True}


@app.get("/api/kb/templates")
async def api_kb_templates_list():
    return JSONResponse(content=db.kb_templates_list())


@app.post("/api/kb/templates")
async def api_kb_template_save(request: Request):
    data = await request.json()
    row_id = db.kb_template_save(data)
    return {"ok": True, "id": row_id}


@app.delete("/api/kb/templates/{row_id}")
async def api_kb_template_delete(row_id: int):
    db.kb_template_delete(row_id)
    return {"ok": True}


@app.get("/api/kb/templates/{row_id}/use")
async def api_kb_template_use(row_id: int):
    template = db.kb_template_use(row_id)
    if not template:
        raise HTTPException(404, "Шаблон не найден")
    return JSONResponse(content=template)


@app.get("/api/kb/templates/search")
async def api_kb_templates_search(q: str = "", category: str = ""):
    from knowledge_base import find_templates
    return JSONResponse(content=find_templates(q, category))


# ══════════════════════════════════════════════════════════════════════════════
# API — Словарь правил фильтрации (scoring_rules)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/rules")
async def api_rules_list():
    return JSONResponse(content=db.scoring_rules_list())


@app.post("/api/rules")
async def api_rule_save(request: Request):
    data = await request.json()
    try:
        row_id = db.scoring_rule_save(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    import filter_engine as fe
    fe.invalidate_rules_cache()
    return {"ok": True, "id": row_id}


@app.delete("/api/rules/{row_id}")
async def api_rule_delete(row_id: int):
    db.scoring_rule_delete(row_id)
    import filter_engine as fe
    fe.invalidate_rules_cache()
    return {"ok": True}


@app.post("/api/rules/seed")
async def api_rules_seed(request: Request):
    try:
        params = await request.json()
    except Exception:
        params = {}
    force = bool(isinstance(params, dict) and params.get("force"))
    n = db.scoring_rules_seed(force=force)
    import filter_engine as fe
    fe.invalidate_rules_cache()
    return {"ok": True, "inserted": n}


@app.get("/api/decide/{purchase_number}")
async def api_decide(purchase_number: str):
    """Быстрая детерминированная карточка решения (без LLM)."""
    import decision_aid
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    return JSONResponse(content=decision_aid.build_card(tender))


def _decide_text(tender: dict) -> str:
    return (
        tender.get("document_text")
        or tender.get("document_text_excerpt")
        or tender.get("primary_text")
        or tender.get("description")
        or tender.get("title")
        or ""
    )


def _explain_cache_key(purchase_number: str, text: str, card: dict) -> str:
    """Ключ кеша: тендер + текст + карта + профиль + версия промпта."""
    import llm_analyzer
    try:
        from knowledge_base import get_profile
        profile_repr = json.dumps(get_profile(), ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        profile_repr = ""
    card_repr = json.dumps(card, ensure_ascii=False, sort_keys=True, default=str)
    parts = [
        purchase_number,
        hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
        hashlib.sha256(card_repr.encode("utf-8")).hexdigest(),
        hashlib.sha256(profile_repr.encode("utf-8")).hexdigest(),
        getattr(llm_analyzer, "EXPLAIN_PROMPT_VERSION", "v1"),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@app.post("/api/decide/{purchase_number}/explain")
async def api_decide_explain(purchase_number: str):
    """LLM-объяснение карточки простым языком (дорогой, кешируется по hash).

    Возвращает {card, explain, cached, error}. Карточка считается всегда; explain
    может быть null, если LLM недоступна/вернула мусор — страница при этом не ломается.
    """
    import decision_aid
    import llm_analyzer
    import llm_provider

    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")

    text = _decide_text(tender)
    card = decision_aid.build_card(tender, text=text)
    cache_key = _explain_cache_key(purchase_number, text, card)

    # 1. Кеш актуален?
    try:
        cached = db.get_novice_explain(purchase_number)
    except Exception:
        cached = None
    if cached and cached.get("hash") == cache_key and cached.get("explain"):
        logger.info("decision_aid_llm_explain_cached: %s", purchase_number)
        return JSONResponse(content={"card": card, "explain": cached["explain"],
                                     "cached": True, "error": None})

    # 2. LLM доступна?
    if not llm_provider.is_configured():
        return JSONResponse(content={"card": card, "explain": None, "cached": False,
                                     "error": "LLM недоступна — добавьте ключ API в /control."})

    # 3. Вызов LLM
    logger.info("decision_aid_llm_explain_started: %s", purchase_number)
    explain = llm_analyzer.explain_for_novice(tender, card, text)
    if not explain:
        logger.info("decision_aid_llm_explain_failed: %s", purchase_number)
        return JSONResponse(content={"card": card, "explain": None, "cached": False,
                                     "error": "Не удалось получить объяснение (LLM вернула пустой ответ)."})

    # 4. Сохранить в кеш
    try:
        db.save_novice_explain(purchase_number, explain, cache_key, llm_provider.deep_model())
    except Exception:
        logger.exception("save_novice_explain failed for %s", purchase_number)

    return JSONResponse(content={"card": card, "explain": explain, "cached": False, "error": None})


@app.post("/api/decide/{purchase_number}/criteria")
async def api_decide_criteria(purchase_number: str):
    """LLM-разбор критериев оценки по фрагментам текста (без кеша). Эвристика — всегда."""
    import document_processor as dp
    import llm_analyzer
    import llm_provider

    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")

    text = _decide_text(tender)
    heuristic = dp.extract_evaluation_criteria(text)

    if not llm_provider.is_configured():
        return JSONResponse(content={"heuristic": heuristic, "llm": None,
                                     "error": "LLM недоступна — добавьте ключ API в /control."})
    chunks = dp._windows_around(text, dp._CRITERIA_MARKERS)
    if not chunks:
        return JSONResponse(content={"heuristic": heuristic, "llm": None,
                                     "error": "В тексте не найден раздел критериев оценки."})
    llm = llm_analyzer.extract_criteria_llm(chunks)
    if not llm:
        return JSONResponse(content={"heuristic": heuristic, "llm": None,
                                     "error": "Не удалось разобрать критерии (LLM вернула пустой ответ)."})
    return JSONResponse(content={"heuristic": heuristic, "llm": llm, "error": None})


_CLARIFY_BUILTIN_HEADER = (
    "Прошу предоставить разъяснения положений документации о закупке "
    "№{pnum} «{title}»:"
)
_CLARIFY_DEADLINE_WARNING = (
    "Проверьте срок подачи запроса на разъяснение на площадке — если срок истёк, "
    "отправить запрос может быть нельзя."
)


@app.post("/api/decide/{purchase_number}/clarification")
async def api_decide_clarification(purchase_number: str):
    """Черновик запроса на разъяснение: вопросы (кеш MVP2 → LLM → ambiguities), шапка
    (kb_templates → builtin), опц. LLM-полировка. Возвращает {draft, questions, ...}."""
    import decision_aid
    import document_processor as dp
    import filter_engine as fe
    import llm_analyzer
    import llm_provider

    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")

    text = _decide_text(tender)
    card = decision_aid.build_card(tender, text=text)
    criteria = dp.extract_evaluation_criteria(text)
    flags = fe.detect_flags(text)

    # 1. Источник вопросов: кеш MVP2 → LLM → детерминированные ambiguities
    questions: list[str] = []
    question_source = "gates"
    try:
        cached = db.get_novice_explain(purchase_number)
    except Exception:
        cached = None
    if cached and cached.get("explain"):
        cq = cached["explain"].get("questions_to_customer") or []
        if cq:
            questions = list(cq)
            question_source = "explain_cache"
    if not questions and llm_provider.is_configured():
        explain = llm_analyzer.explain_for_novice(tender, card, text)
        if explain and explain.get("questions_to_customer"):
            questions = list(explain["questions_to_customer"])
            question_source = "llm"
    if not questions:
        questions = [a["question"] for a in decision_aid.build_ambiguities(card, criteria, flags)]
        question_source = "gates"

    questions = questions[:5]  # лимит 3–5
    if not questions:
        questions = ["Просим подтвердить требования к участнику и составу заявки по данной закупке."]

    # 2. Шапка: kb_templates категории «разъяснение» → builtin
    template_source = "builtin"
    header = _CLARIFY_BUILTIN_HEADER.format(
        pnum=tender.get("purchase_number", "—"), title=tender.get("title", "—"))
    try:
        for t in db.kb_templates_list():
            if "разъясн" in (t.get("category") or "").lower() and (t.get("content") or "").strip():
                header = t["content"].strip()
                template_source = "kb_template"
                break
    except Exception:
        pass

    # 3. Текст: LLM-полировка или детерминированная сборка
    polished_by_llm = False
    draft = None
    if llm_provider.is_configured():
        draft = llm_analyzer.draft_clarification(tender, questions, card=card, criteria=criteria)
        polished_by_llm = bool(draft)
    if not draft:
        body = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
        draft = f"{header}\n\n{body}"

    return JSONResponse(content={
        "draft": draft,
        "questions": questions,
        "question_source": question_source,
        "template_source": template_source,
        "polished_by_llm": polished_by_llm,
        "warning": _CLARIFY_DEADLINE_WARNING,
    })


def _work_due_state(work_due) -> str:
    """overdue|soon|ok|none по внутреннему сроку (work_due, ISO-строка/None)."""
    if not work_due:
        return "none"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(work_due))
    if not m:
        return "none"
    from datetime import date
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return "none"
    days = (d - date.today()).days
    if days < 0:
        return "overdue"
    if days <= 1:
        return "soon"
    return "ok"


@app.post("/api/tender/{purchase_number}/workflow")
async def api_tender_workflow(purchase_number: str, request: Request):
    """Обновляет lifecycle-поля тендера (стадия/срок/заметка). Стадия валидируется."""
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    data = await request.json()

    fields: dict = {}
    if "work_stage" in data:
        stage = (data.get("work_stage") or "").strip()
        if stage and stage not in WORK_STAGE_KEYS:
            raise HTTPException(400, f"Неизвестная стадия: {stage}")
        fields["work_stage"] = stage  # "" → снять с доски (NULL в БД)
    if "work_due" in data:
        due = (data.get("work_due") or "").strip()
        if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            raise HTTPException(400, "Некорректная дата work_due (нужен формат ГГГГ-ММ-ДД)")
        fields["work_due"] = due or None
    if "work_note" in data:
        fields["work_note"] = (data.get("work_note") or "")[:2000]

    if not fields:
        raise HTTPException(400, "Нет полей для обновления")

    db.set_workflow(purchase_number, fields)
    updated = db.get_tender(purchase_number) or {}
    return JSONResponse(content={"ok": True, "workflow": {
        "work_stage": updated.get("work_stage"),
        "work_due": updated.get("work_due"),
        "work_note": updated.get("work_note"),
        "work_updated_at": updated.get("work_updated_at"),
    }})


@app.post("/api/tender/{purchase_number}/reject")
async def api_tender_reject(purchase_number: str):
    """Помечает тендер как отказной и убирает его из обычных списков/доски."""
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")

    db.set_decision(purchase_number, "rejected", "Отказ через карточку тендера")
    db.set_workflow(purchase_number, {"work_stage": "", "work_due": None})
    updated = db.get_tender(purchase_number) or {}
    return JSONResponse(content={"ok": True, "decision": updated.get("decision")})


@app.get("/board", response_class=HTMLResponse)
async def board(request: Request):
    """Kanban-доска тендеров в работе (lifecycle, MVP4a)."""
    tenders = db.list_workflow_tenders()
    # Колонки в порядке WORK_STAGES + «Другое» для неизвестных стадий.
    columns = [{**s, "tenders": []} for s in WORK_STAGES]
    by_key = {c["key"]: c for c in columns}
    other = {"key": "_other", "label": "Другое", "color": "gray", "active": False, "tenders": []}
    for t in tenders:
        t["work_due_state"] = _work_due_state(t.get("work_due"))
        stage = t.get("work_stage")
        if stage in by_key:
            by_key[stage]["tenders"].append(t)
        else:
            logger.warning("board: неизвестная стадия %r у %s", stage, t.get("purchase_number"))
            other["tenders"].append(t)
    if other["tenders"]:
        columns.append(other)
    total = len(tenders)
    return templates.TemplateResponse(request, "board.html", {
        "columns": columns, "total": total,
    })


# ── MVP4b-1: спецификация позиций ────────────────────────────────────────────

def _spec_rollup(items: list) -> dict:
    """Грубый итог себестоимости: только строки, где есть И qty, И unit_price."""
    total = 0.0
    priced = 0
    for it in items:
        q, p = it.get("qty"), it.get("unit_price")
        if q is not None and p is not None:
            try:
                total += float(q) * float(p)
                priced += 1
            except (TypeError, ValueError):
                pass
    return {"total": round(total) if priced else 0, "priced": priced, "count": len(items)}


def _match_one(item: dict) -> Optional[dict]:
    """Предварительный подбор товара из каталога по названию+характеристикам."""
    probe = f"{item.get('name') or ''} {item.get('requirements') or ''}".strip()
    if not probe:
        return None
    try:
        cands = db.kb_equipment_find_analogs(probe)
    except Exception:
        return None
    if not cands:
        return None
    best = cands[0]
    bn = set(re.findall(r"\w+", (best.get("name") or "").lower()))
    pn = set(re.findall(r"\w+", probe.lower()))
    score = round(len(bn & pn) / max(len(bn), 1), 2) if bn else 0.0
    return {
        "matched_equipment_id": best.get("id"),
        "unit_price": best.get("price_rub"),
        "lead_days": best.get("lead_days"),
        "match_score": score,
        "match_note": "Автоподбор по названию/характеристикам — проверьте вручную",
    }


@app.get("/api/tender/{purchase_number}/spec")
async def api_spec_get(purchase_number: str):
    if not db.get_tender(purchase_number):
        raise HTTPException(404, "Тендер не найден")
    items = db.list_tender_items(purchase_number)
    return JSONResponse(content={"items": items, "rollup": _spec_rollup(items)})


@app.post("/api/tender/{purchase_number}/spec/extract")
async def api_spec_extract(purchase_number: str, request: Request):
    """Извлечь позиции (heuristic|llm). Если спецификация уже есть — нужен replace=true."""
    import document_processor as dp
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    body = await request.json()
    mode = (body.get("mode") or "heuristic").lower()
    replace = bool(body.get("replace"))

    if db.list_tender_items(purchase_number) and not replace:
        return JSONResponse(status_code=409, content={
            "error": "Спецификация уже заполнена. Подтвердите замену.", "need_confirm": True})

    if mode == "llm":
        text = _decide_text(tender)
        import llm_provider
        import llm_analyzer
        if not llm_provider.is_configured():
            # НЕ трогаем текущие позиции
            return JSONResponse(content={"error": "LLM недоступна — добавьте ключ API в /control."})
        chunks = dp._windows_around(text, dp.SPEC_MARKERS) or [text[:4000]]
        res = llm_analyzer.extract_spec_llm(chunks)
        if not res or not res.get("items"):
            warn = (res or {}).get("warning") or "Позиции не распознаны LLM."
            return JSONResponse(content={"items": [], "note": warn, "error": None})
        items, note = res["items"], res.get("warning", "")
    else:
        docs_dir = tender.get("documents_dir") or ""
        files = []
        if docs_dir:
            try:
                from pathlib import Path
                files = [p for p in Path(docs_dir).iterdir() if p.is_file()]
            except Exception:
                files = []
        res = dp.extract_object_description_items(files)
        if not res.get("items"):
            text = _decide_text(tender)
            res = dp.extract_spec_items(text)
        items, note = res["items"], res.get("note", "")

    db.replace_tender_items(purchase_number, items)
    saved = db.list_tender_items(purchase_number)
    return JSONResponse(content={"items": saved, "rollup": _spec_rollup(saved),
                                 "note": note, "error": None})


@app.post("/api/tender/{purchase_number}/spec/match")
async def api_spec_match(purchase_number: str):
    """Подобрать товары из каталога для непривязанных позиций (предварительно)."""
    if not db.get_tender(purchase_number):
        raise HTTPException(404, "Тендер не найден")
    items = db.list_tender_items(purchase_number)
    matched = 0
    for it in items:
        if it.get("matched_equipment_id"):
            continue
        mr = _match_one(it)
        if mr:
            db.update_tender_item(it["id"], mr)
            matched += 1
    saved = db.list_tender_items(purchase_number)
    return JSONResponse(content={"items": saved, "rollup": _spec_rollup(saved), "matched": matched})


@app.post("/api/tender/{purchase_number}/spec/item")
async def api_spec_item_save(purchase_number: str, request: Request):
    if not db.get_tender(purchase_number):
        raise HTTPException(404, "Тендер не найден")
    data = await request.json()
    item_id = data.get("id")
    if item_id:
        existing = db.get_tender_item(int(item_id))
        if not existing or existing.get("purchase_number") != purchase_number:
            raise HTTPException(404, "Позиция не найдена в этом тендере")
        db.update_tender_item(int(item_id), data)
    else:
        item_id = db.add_tender_item(purchase_number, data)
    saved = db.list_tender_items(purchase_number)
    return JSONResponse(content={"ok": True, "id": item_id, "items": saved,
                                 "rollup": _spec_rollup(saved)})


@app.post("/api/tender/{purchase_number}/spec/item/{item_id}/delete")
async def api_spec_item_delete(purchase_number: str, item_id: int):
    existing = db.get_tender_item(item_id)
    if not existing or existing.get("purchase_number") != purchase_number:
        raise HTTPException(404, "Позиция не найдена в этом тендере")
    db.delete_tender_item(item_id)
    saved = db.list_tender_items(purchase_number)
    return JSONResponse(content={"ok": True, "items": saved, "rollup": _spec_rollup(saved)})


@app.get("/api/kb/match/{purchase_number}")
async def api_kb_match(purchase_number: str):
    from knowledge_base import build_llm_context, match_competencies
    from knowledge_base import build_llm_context, match_competencies
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    tz = (
        tender.get("document_text")
        or tender.get("document_text_excerpt")
        or tender.get("primary_text")
        or ""
    )
    return JSONResponse(content={
        "competency_match": match_competencies(tz),
        "kb_context": build_llm_context(tender, tz),
    })
