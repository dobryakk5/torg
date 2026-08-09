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
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import zipfile

# Включаем системное TLS-хранилище до сетевых библиотек фоновых задач.
from tls_bootstrap import NATIVE_TRUSTSTORE_ACTIVE

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

import config
import database as db

logger = logging.getLogger(__name__)

app = FastAPI(title="Тендерный монитор", version="3.0.0")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="12" fill="#06060B"/>
<text x="32" y="46" text-anchor="middle" font-family="Arial, sans-serif" font-size="44" font-weight="800" fill="#EADDC8">Т</text>
</svg>"""

# ── MVP4a: стадии работы по тендеру (Kanban-доска) ───────────────────────────
WORK_STAGES = [
    {"key": "lead",      "label": "Интересно",      "color": "neutral", "active": True},
    {"key": "preparing", "label": "Готовлю заявку",  "color": "blue",    "active": True},
    {"key": "submitted", "label": "Заявка подана",   "color": "purple",  "active": True},
    {"key": "won",       "label": "Выиграл",         "color": "green",   "active": True},
    {"key": "executing", "label": "Исполнение",      "color": "green",   "active": True},
    {"key": "done",      "label": "Закрыт",          "color": "gray",    "active": False},
    {"key": "failed",    "label": "Не прошел",       "color": "red",     "active": False},
]
WORK_STAGE_KEYS = {s["key"] for s in WORK_STAGES}

# Причины отказа (используются в /api/tender/{n}/reject и на доске при переходе в «Не прошел»)
REJECTION_REASONS = {
    "not_my_profile": "Не мой профиль",
    "manual_decline": "Решил не брать",
    "failed":         "Не прошел",
}


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
        "stage2":    "Stage 2 — сбор ТЗ и деталей в БД",
        "llm":       "Stage 2.5 — LLM-разбор и отправка (из БД)",
        "stage3":    "Stage 3 — результаты",
        "all":       "Полный цикл (1+2+LLM)",
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
            run_stage1, run_triage, run_rescore, run_stage2, run_llm, run_stage3,
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
            "llm":       _with_runtime_config(run_llm),
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
    Фоновый поиск по выбранным поисковым профилям с прогрессом по каждой фразе.

    Поиск с экрана /control/search идёт ТОЛЬКО по профилям (search_profiles.py) —
    так же, как автоматический Stage 1 (main.run_stage1). Пользователь выбирает
    один или несколько профилей; фактические ЕИС-запросы строятся из их
    плюс-фраз (search_profiles.eis_queries), а найденные карточки прогоняются
    через тот же локальный пост-фильтр (search_profiles.filter_and_tag) —
    непринятые ни одним профилем в БД не попадают.

    В отличие от JobRunner.stage1, этот раннер:
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
    def start(cls, profile_ids: list[int] | None = None, params: dict | None = None) -> tuple[bool, str]:
        import search_profiles as sp
        params = params or {}
        profile_ids = [int(pid) for pid in (profile_ids or [])]
        if not profile_ids:
            return False, "no_profiles"

        all_rows = db.search_profiles_list()
        chosen_rows = [r for r in all_rows if r["id"] in set(profile_ids)]
        if not chosen_rows:
            return False, "no_profiles"
        chosen_profiles = [sp._profile_from_row(r) for r in chosen_rows]

        # (label для UI/лога, реальный запрос к ЕИС) — профиль может дать
        # несколько плюс-фраз, каждая — своя строка прогресса.
        query_items: list[tuple[str, str]] = [
            (f"{profile.name}: {q}", q)
            for profile in chosen_profiles
            for q in sp.eis_queries(profile)
        ]
        if not query_items:
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
                "profile_ids": profile_ids,
                "phrases": [
                    {"phrase": label, "query": query, "status": "pending", "found": 0,
                     "saved": 0, "candidates": 0, "error": None}
                    for label, query in query_items
                ],
            }

        threading.Thread(target=cls._worker, args=(params, chosen_profiles),
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
    def _worker(cls, params: dict, profiles: list) -> None:
        import search_profiles as sp
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
            entries = [(p["phrase"], p.get("query") or p["phrase"]) for p in cls._state["phrases"]]

        total_found = total_saved = 0
        for idx, (label, query) in enumerate(entries):
            if cls._stop.is_set():
                cls._set_phrase(idx, status="stopped")
                continue
            cls._set_phrase(idx, status="running")
            try:
                tenders = search_eis(
                    keyword=query, price_from=price_min, price_to=price_max,
                    fz44=fz44, fz223=fz223, pages=pages, days_back=days_back,
                    date_from=date_from, date_to=date_to,
                )
                db.reconnect_db()
                tenders, dropped = sp.filter_and_tag(tenders, profiles=profiles)
                if dropped:
                    logger.info("Профильный фильтр отсеял %d карточек по '%s'", dropped, label)
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
                    mk = sorted(set(tender.get("matched_keywords") or []) | {label})
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
                logger.exception("Поиск по '%s' упал", label)
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
    config.NOTIFY_ONLY_ZMO               = config.get_runtime("NOTIFY_ONLY_ZMO",              config.NOTIFY_ONLY_ZMO)
    config.DOCUMENT_DOWNLOAD_MIN_SCORE   = config.get_runtime("DOCUMENT_DOWNLOAD_MIN_SCORE",  config.DOCUMENT_DOWNLOAD_MIN_SCORE)
    config.MIN_SCORE_FOR_LLM             = config.get_runtime("MIN_SCORE_FOR_LLM",            config.MIN_SCORE_FOR_LLM)
    config.STAGE2_LIMIT                  = config.get_runtime("STAGE2_LIMIT",                 config.STAGE2_LIMIT)
    config.SEARCH_KEYWORDS               = config.get_runtime("SEARCH_KEYWORDS",              config.SEARCH_KEYWORDS)
    config.OKPD2_SEARCH_ENABLED          = config.get_runtime("OKPD2_SEARCH_ENABLED",         config.OKPD2_SEARCH_ENABLED)
    config.OKPD2_CODES                   = config.get_runtime("OKPD2_CODES",                  config.OKPD2_CODES)
    config.BACKFILL_SEARCH_PAGES         = config.get_runtime("BACKFILL_SEARCH_PAGES",        config.BACKFILL_SEARCH_PAGES)
    config.SOURCE_B2B_ENABLED            = config.get_runtime("SOURCE_B2B_ENABLED",           config.SOURCE_B2B_ENABLED)
    config.B2B_SEARCH_PAGES              = config.get_runtime("B2B_SEARCH_PAGES",             config.B2B_SEARCH_PAGES)
    config.SOURCE_ZAKAZRF_ENABLED        = config.get_runtime("SOURCE_ZAKAZRF_ENABLED",       config.SOURCE_ZAKAZRF_ENABLED)
    config.ZAKAZRF_SEARCH_PAGES          = config.get_runtime("ZAKAZRF_SEARCH_PAGES",         config.ZAKAZRF_SEARCH_PAGES)
    config.ZAKAZRF_SEARCH_KEYWORDS       = config.get_runtime("ZAKAZRF_SEARCH_KEYWORDS",      config.ZAKAZRF_SEARCH_KEYWORDS)
    config.ZAKAZRF_PLANED_DATE_FROM_TICKS = config.get_runtime(
        "ZAKAZRF_PLANED_DATE_FROM_TICKS", config.ZAKAZRF_PLANED_DATE_FROM_TICKS,
    )
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


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


# ══════════════════════════════════════════════════════════════════════════════
# Jinja2 фильтры
# ══════════════════════════════════════════════════════════════════════════════

def fmt_price(v) -> str:
    if v is None: return "—"
    try: return f"{round(float(v) / 1000):,.0f}".replace(",", "\u2009")
    except: return str(v)

def fmt_rub(v) -> str:
    """Полная сумма в рублях («640 000 ₽») — там, где важна цена, а не масштаб в тыс."""
    if v is None or v == "": return "—"
    try:
        return f"{round(float(v)):,.0f}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return str(v)

def fmt_date(v: str) -> str:
    if not v: return "—"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else str(v)[:10]

def fmt_datetime(v: str) -> str:
    if not v: return "—"
    dt = db.parse_deadline(str(v))
    return dt.strftime("%d.%m.%Y %H:%M") if dt else str(v)

def deadline_iso(v: str) -> str:
    """ГГГГ-ММ-ДД для value у <input type="date">; пусто, если дата не распознана."""
    if not v: return ""
    dt = db.parse_deadline(str(v))
    return dt.strftime("%Y-%m-%d") if dt else ""


# Заголовки секций детального разбора (llm_analyzer._SYSTEM_TEMPLATE).
_LLM_SECTIONS = (
    "ВЕРДИКТ:", "ПОДХОДИТ ПО ПРОФИЛЮ:", "ФИНАНСЫ:", "ОБЪЕМ:", "РИСКИ:",
    'ПРИЗНАКИ "ПОД СВОЕГО":', "СТОП-ФАКТОРЫ:", "РАЗБОР АВТО-СТОПОВ:",
    "ВОПРОСЫ ЗАКАЗЧИКУ:", "ИТОГ:",
)
# Секции, которые не показываем в карточке: финансы дублируют блок «Обеспечение»
# и info-карточки (НМЦК/обеспечение берутся из БД, а не из пересказа модели).
_LLM_HIDDEN_SECTIONS = ("ФИНАНСЫ:",)

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)


def _llm_strip_sections(text: str, hidden: tuple[str, ...]) -> str:
    """Вырезает целые секции разбора (заголовок + тело до следующего заголовка)."""
    if not hidden:
        return text
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        head = line.strip().upper()
        if any(head.startswith(h) for h in _LLM_SECTIONS):
            skipping = any(head.startswith(h) for h in hidden)
        if not skipping:
            out.append(line)
    return "\n".join(out).strip()


def llm_html(v: str) -> Markup:
    """Готовит текст детального разбора к выводу в HTML.

    Экранирует текст (иначе разметка «вылезает» наружу — Jinja экранирует
    результат replace-цепочки целиком), убирает скрытые секции, превращает
    markdown-выделение **жирным** в <strong> и подсвечивает заголовки секций.
    """
    if not v:
        return Markup("")
    text = _llm_strip_sections(str(v), _LLM_HIDDEN_SECTIONS)
    html = str(escape(text))
    # **жирный** → <strong> (модель размечает так подзаголовки рисков)
    html = _MD_BOLD_RE.sub(r"<strong>\1</strong>", html)
    for title in _LLM_SECTIONS:
        if title in _LLM_HIDDEN_SECTIONS:
            continue
        safe_title = str(escape(title))
        html = html.replace(safe_title, f'<span class="llm-section-title">{safe_title}</span>')
    return Markup(html)

def days_left(v: str) -> Optional[int]:
    """Сколько дней осталось до срока подачи заявок (deadline). None — не распознано/не задано.

    Дедлайн хранится строкой в форматах DD.MM.YYYY [HH:MM] или ISO — разбор
    через database.parse_deadline (единый парсер проекта). Отрицательное значение
    (срок уже прошёл) не показываем — истёкшие лоты и так скрыты из активных списков.
    """
    if not v: return None
    dt = db.parse_deadline(v)
    if dt is None:
        return None
    return (dt.date() - datetime.now(dt.tzinfo).date()).days


# Платформа-источник → короткий (≤3 символа) уникальный код для столбца ЕИС.
PLATFORM_SHORT = {
    "ЕИС": "ЕИС",
    "ЕАТ": "ЕАТ",
    "B2B-Center": "B2B",
    "Tenderplan": "ТПл",
    "ЭМ СПб": "СПб",
    "ЭМ МО": "ЭМО",
    "ПП Москвы": "ППМ",
    "БП ZakazRF": "ЗРФ",
}

def platform_short(v: str) -> str:
    """Короткий код источника для столбца ссылки (ЕИС/ЕАТ/B2B/…). Пусто → ЕИС."""
    s = str(v or "").strip()
    if not s:
        return "ЕИС"
    return PLATFORM_SHORT.get(s, s[:3])

def score_color(s: int) -> str:
    return {1:"#EF4444",2:"#F97316",3:"#EAB308",4:"#84CC16",5:"#22C55E"}.get(int(s or 0),"#3A3A55")

def decision_class(d: str) -> str:
    return {"GO":"go","CAUTION":"caution","NO-GO":"nogo"}.get(d or "","unknown")

def parse_signals(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split("|") if x.strip()]

def parse_stop(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split("|") if x.strip()]

def _web_extract_reg_number(value: str) -> str:
    match = re.search(r"\b\d{11,22}\b", str(value or ""))
    return match.group(0) if match else ""


def _web_notice_type(source: str) -> str:
    if "://" not in str(source or ""):
        return ""
    parsed = urlparse(source)
    if "zakupki.gov.ru" not in parsed.netloc.lower():
        return ""
    match = re.search(r"/epz/order/notice/([^/]+)/", parsed.path, flags=re.I)
    if not match:
        return ""
    notice_type = match.group(1)
    # Отсекаем служебные разделы (printForm, notice223) — у них нет /view/
    if not re.fullmatch(r"[a-z]{2}\d{2}", notice_type, flags=re.I):
        return ""
    return notice_type


def zakupki_common_info_url(url: str, purchase_number: str = "") -> str:
    source = str(url or "").strip()
    reg_number = _web_extract_reg_number(source) or _web_extract_reg_number(purchase_number)
    if not reg_number:
        return source

    lower_source = source.lower()
    is_223 = (
        "notice223" in lower_source
        or "/223/" in lower_source
        or bool(re.fullmatch(r"3\d{10}", reg_number))
    )

    query = dict(parse_qsl(urlparse(source).query, keep_blank_values=True)) if source else {}
    if is_223:
        query.pop("purchaseNoticeNumber", None)
        query["regNumber"] = reg_number
        return (
            "https://zakupki.gov.ru/epz/order/notice/notice223/common-info.html?"
            + urlencode(query)
        )

    notice_type = _web_notice_type(source) or str(
        getattr(config, "EIS_DEFAULT_NOTICE_TYPE", "ea20") or "ea20"
    ).strip()
    query["regNumber"] = reg_number
    return (
        f"https://zakupki.gov.ru/epz/order/notice/{notice_type}/view/common-info.html?"
        + urlencode(query)
    )


def zakupki_documents_url(url: str, purchase_number: str = "", notice_guid: str = "") -> str:
    source = str(url or "").strip()
    reg_number = _web_extract_reg_number(source) or _web_extract_reg_number(purchase_number)
    if not reg_number:
        return source

    lower_source = source.lower()
    is_223 = (
        "notice223" in lower_source
        or "/223/" in lower_source
        or bool(re.fullmatch(r"3\d{10}", reg_number))
    )

    query = dict(parse_qsl(urlparse(source).query, keep_blank_values=True)) if source else {}
    if is_223:
        source_guid = notice_guid or query.get("noticeGuid") or query.get("purchaseNoticeGuid") or ""
        query.pop("regNumber", None)
        query.pop("purchaseNoticeGuid", None)
        query["purchaseNoticeNumber"] = reg_number
        if source_guid:
            query["noticeGuid"] = source_guid
        return (
            "https://zakupki.gov.ru/epz/order/notice/notice223/documents.html?"
            + urlencode(query)
        )

    notice_type = _web_notice_type(source) or str(
        getattr(config, "EIS_DEFAULT_NOTICE_TYPE", "ea20") or "ea20"
    ).strip()
    query.pop("purchaseNoticeNumber", None)
    query["regNumber"] = reg_number
    return (
        f"https://zakupki.gov.ru/epz/order/notice/{notice_type}/view/documents.html?"
        + urlencode(query)
    )

for name, fn in [("fmt_price",fmt_price),("fmt_rub",fmt_rub),("fmt_date",fmt_date),("fmt_datetime",fmt_datetime),("deadline_iso",deadline_iso),("llm_html",llm_html),("days_left",days_left),
                  ("platform_short",platform_short),
                  ("score_color",score_color),("decision_class",decision_class),
                  ("parse_signals",parse_signals),("parse_stop",parse_stop),
                  ("zakupki_common_info_url",zakupki_common_info_url)]:
    templates.env.filters[name] = fn


# ══════════════════════════════════════════════════════════════════════════════
# Страницы
# ══════════════════════════════════════════════════════════════════════════════

def _unsent_reason(t: dict, notify_min: int) -> str:
    """Почему обработанный ЗМО-лот НЕ ушёл в Telegram (для списка «не отправлены»)."""
    if db.deadline_is_expired(t.get("deadline")):
        return "⏳ просрочен (дедлайн прошёл до отправки)"
    verdict = str(t.get("llm_verdict") or "").upper()
    if "ПРОПУСТИТЬ" in verdict:
        return "🛑 LLM по ТЗ: не наш профиль (ПРОПУСТИТЬ)"
    score = t.get("detail_score")
    if score is not None and int(score) < int(notify_min):
        return f"📉 детальный скор {score} < порога {notify_min}"
    if not verdict:
        return "⏱ ждёт LLM-разбора (в очереди)"
    # Разобран (СМОТРЕТЬ/ОСТОРОЖНО), не просрочен, скор ок — но не отправлен:
    # отправка сорвалась и ждёт переотправки следующим прогоном.
    return "⚠️ отправка не удалась — в очереди на переотправку"


# Этап воронки лота (столбец СТАТУС в единой таблице + фильтр по нему).
FUNNEL_STATUS_LABELS = {
    "raw":       "не обработан",
    "processed": "обработан, не отправлен",
    "sent":      "отправлен",
    "rejected":  "отказ",
}


def _funnel_status(t: dict) -> str:
    """Этап воронки: rejected > sent > processed > raw (первое подходящее)."""
    if (t.get("decision") or "") == "rejected":
        return "rejected"
    if t.get("notified_at"):
        return "sent"
    if t.get("detail_checked_at"):
        return "processed"
    return "raw"


def _tenders_context(
    decision: list[str], status: list[str], law: Optional[str], kw: Optional[str],
    profile_id: Optional[int], platform: list[str], q: Optional[str], exclude_kw: list[str],
    price_from: Optional[float], price_to: Optional[float],
    f1_min: Optional[int], f2_min: Optional[int], f3_min: Optional[int], f4_min: Optional[int],
    f5_min: Optional[int], f6_min: Optional[int], f7_min: Optional[int], f8_min: Optional[int],
    score_min: Optional[int], score_max: Optional[int],
    days_min: Optional[int], days_max: Optional[int],
    sort_by: str, order: str, limit: int,
) -> dict:
    """Общая выборка + контекст для единой таблицы тендеров (страница `/` и партиал `/partials/tenders`)."""
    decisions = [d.upper() for d in decision if d]
    statuses = [s for s in status if s]
    law = law or None
    kw = kw or None
    q = q or None
    sort_by = sort_by if sort_by in {"score", "price", "phrase", "date", "deadline"} else "score"
    order = order if order in {"asc", "desc"} else "desc"

    # Просроченные по умолчанию скрыты; явный отрицательный days_min — способ их вернуть.
    active_only = not (days_min is not None and days_min < 0)

    tenders = db.get_top_tenders(
        decision=None, decisions=decisions or None,
        price_min=price_from, price_max=price_to, law_type=law,
        matched_keyword=kw, profile_id=profile_id,
        platforms=platform, q=q, exclude_keywords=exclude_kw,
        f1_min=f1_min, f2_min=f2_min, f3_min=f3_min, f4_min=f4_min,
        f5_min=f5_min, f6_min=f6_min, f7_min=f7_min, f8_min=f8_min,
        score_min=score_min, score_max=score_max,
        days_min=days_min, days_max=days_max,
        statuses=statuses or None,
        active_only=active_only,
        sort_by=sort_by, order=order, limit=limit,
    )
    # Главная — очередь на разбор. Лоты, уже взятые в работу, живут на доске
    # и не должны возвращаться в левый список после обновления страницы.
    tenders = [t for t in tenders if not t.get("work_stage")]
    notify_min = config.get_runtime("MIN_DETAILED_SCORE_FOR_NOTIFY", config.MIN_DETAILED_SCORE_FOR_NOTIFY)
    for t in tenders:
        t["funnel_status"] = _funnel_status(t)
        if t["funnel_status"] == "processed" and (t.get("law_type") or "") == "ЗМО":
            t["unsent_reason"] = _unsent_reason(t, notify_min)
        # Подготовленные на сервере подписи держат шаблон очереди простым и
        # позволяют одинаково показывать старый скоринг и новый LLM-триаж.
        decision = (t.get("filter_decision") or "").upper()
        t["decision_label"] = {
            "GO": "БЕРИ",
            "CAUTION": "ПОДУМАЙ",
            "NO-GO": "ПРОПУСТИ",
        }.get(decision, "НА РАЗБОР")
        t["decision_tone"] = {
            "GO": "go",
            "CAUTION": "caution",
            "NO-GO": "nogo",
        }.get(decision, "unknown")
        t["decision_reason"] = (
            t.get("llm_triage_reason")
            or t.get("llm_verdict")
            or t.get("unsent_reason")
            or "Откройте карточку, чтобы увидеть оценку требований и рисков."
        )
        t["risk_tags"] = _risk_tags(t)

    # Хвост очереди из ПРОПУСТИ прячем под «развернуть»: при сортировке по вердикту
    # он всегда в конце и только мешает разбирать живые лоты.
    nogo_tail = 0
    if not decisions and (sort_by, order) == ("score", "desc"):
        for t in reversed(tenders):
            if t["decision_tone"] != "nogo":
                break
            nogo_tail += 1
        if nogo_tail == len(tenders):   # весь список — ПРОПУСТИ, прятать нечего
            nogo_tail = 0

    search_phrases = config.get_runtime("SEARCH_KEYWORDS", config.SEARCH_KEYWORDS)
    return {
        "tenders": tenders,
        "nogo_tail": nogo_tail,
        # «Разобрать очередь · N» — сколько лотов ещё не разобрано (не отправлены и не отказ).
        "queue_pending": sum(1 for t in tenders if t["funnel_status"] in ("raw", "processed")),
        "funnel_status_labels": FUNNEL_STATUS_LABELS,
        "search_phrases": search_phrases if isinstance(search_phrases, list) else [],
        "search_profiles": db.search_profiles_list(),
        "all_platforms": db.get_distinct_platforms(),
        "active_platforms": platform,
        "active_kw": kw or "",
        "active_profile_id": profile_id or "",
        "active_q": q or "",
        "excluded_keywords": exclude_kw,
        "active_decisions": decisions,
        "active_statuses": statuses,
        "active_law": law or "",
        "active_score_min": score_min,
        "active_score_max": score_max,
        "active_days_min": days_min,
        "active_days_max": days_max,
        "sort_by": sort_by,
        "sort_order": order,
    }


def _decision_reasons(tender: dict[str, Any]) -> list[dict[str, Any]]:
    """Три коротких аргумента для первого экрана карточки решения."""
    reasons: list[dict[str, Any]] = []
    scored_filters = sorted(
        tender.get("filter_scores") or [],
        key=lambda row: int(row.get("score") or 0),
        reverse=True,
    )
    reason_slots = [
        ("positive", "ЗА", scored_filters[:1]),
        ("negative", "ПРОТИВ", list(reversed(scored_filters[-1:]))),
        ("check", "ПРОВЕРИТЬ", scored_filters[1:2]),
    ]
    seen_filter_numbers: set[Any] = set()
    for tone, label, rows in reason_slots:
        for row in rows:
            number = row.get("filter_number")
            if number in seen_filter_numbers:
                continue
            seen_filter_numbers.add(number)
            signals = _signal_list(row)
            reasons.append({
                "tone": tone,
                "label": label,
                "filter_number": number,
                "score": row.get("score"),
                "title": row.get("filter_name") or f"Фильтр {number}",
                "text": _short_phrase(signals[0], 160) if signals else "Проверьте доказательства в полной карточке.",
            })
    return reasons


# Короткие подписи фильтров для водопада «почему такой балл» (Ф1–Ф8).
FILTER_SHORT_NAMES = {
    1: "Профиль", 2: "Финвход", 3: "Объём", 4: "SLA",
    5: "Требования", 6: "Заточка", 7: "Перекуп", 8: "Договор",
}

# Нейтральный балл одного фильтра: от него считается вклад ± в водопаде.
FILTER_NEUTRAL_SCORE = 3


def _signal_list(row: dict[str, Any]) -> list[str]:
    """Сигналы фильтра списком: в БД строка через `|`, в агрегате — уже список."""
    signals = row.get("signals")
    if isinstance(signals, str):
        return parse_signals(signals)
    if isinstance(signals, dict):
        signals = list(signals.values())
    return [str(v).strip() for v in (signals or []) if str(v).strip()]


def _short_phrase(text: str, limit: int = 44) -> str:
    s = " ".join(str(text or "").split()).lstrip("✓✗⚠!↳ ").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _risk_tags(tender: dict[str, Any]) -> list[dict[str, str]]:
    """Метки риска для карточки очереди: стоп-факторы, затем просевшие фильтры."""
    tags: list[dict[str, str]] = []
    for stop in parse_stop(tender.get("filter_stop") or ""):
        tags.append({"tone": "stop", "text": "СТОП · " + _short_phrase(stop, 40)})
    for row in tender.get("filter_scores") or []:
        score = int(row.get("score") or 0)
        if score == 0 or score > 2:
            continue
        signals = _signal_list(row)
        bad = [s for s in signals if s.startswith(("✗", "⚠", "!"))] or signals
        number = int(row.get("filter_number") or 0)
        label = _short_phrase(bad[0]) if bad else FILTER_SHORT_NAMES.get(number, f"Ф{number}")
        tags.append({"tone": "warn", "text": label})
    return tags[:3]


def _score_breakdown(tender: dict[str, Any]) -> list[dict[str, Any]]:
    """Вклад каждого фильтра относительно нейтральных 3 баллов (водопад 24 → итог)."""
    rows: list[dict[str, Any]] = []
    for row in tender.get("filter_scores") or []:
        number = int(row.get("filter_number") or 0)
        score = int(row.get("score") or 0)
        signals = _signal_list(row)
        rows.append({
            "filter_number": number,
            "short_name":    FILTER_SHORT_NAMES.get(number, f"Ф{number}"),
            "filter_name":   row.get("filter_name") or f"Фильтр {number}",
            "score":         score,
            "delta":         score - FILTER_NEUTRAL_SCORE,
            "signal":        signals[0] if signals else "",
            "signals":       signals,
            "stop_factor":   bool(row.get("stop_factor")),
        })
    return rows


def _score_disputes(breakdown: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Только спорные места: фильтры, которые утопили балл (или дали стоп-фактор)."""
    disputes = [
        dict(row, tone=("nogo" if row["stop_factor"] or row["delta"] <= -2 else "caution"))
        for row in breakdown
        if row["delta"] < 0 or row["stop_factor"]
    ]
    disputes.sort(key=lambda row: (row["delta"], -row["filter_number"]))
    return disputes


# Сколько лотов просматриваем, чтобы найти позицию текущего в очереди по умолчанию.
QUEUE_NAV_LIMIT = 300


def _queue_nav(purchase_number: str) -> Optional[dict[str, Any]]:
    """Позиция лота в очереди (сорт по умолчанию: вердикт ↓) и соседи для ↑/↓."""
    try:
        rows = db.get_top_tenders(
            decision=None, sort_by="score", order="desc", limit=QUEUE_NAV_LIMIT,
        )
    except Exception:
        logger.exception("queue nav lookup failed for %s", purchase_number)
        return None
    numbers = [r.get("purchase_number") for r in rows]
    if purchase_number not in numbers:
        return None
    index = numbers.index(purchase_number)
    return {
        "position": index + 1,
        "total":    len(numbers),
        "prev":     numbers[index - 1] if index > 0 else None,
        "next":     numbers[index + 1] if index + 1 < len(numbers) else None,
    }


def _stored_work_scope(purchase_number: str) -> dict[str, Any]:
    """Разобранный объём работ из БД (без парсинга документов — превью должно быть быстрым)."""
    try:
        details = db.get_tender_details(purchase_number) or {}
    except Exception:
        logger.exception("get_tender_details failed for %s", purchase_number)
        return {}
    scope = details.get("work_scope")
    return scope if isinstance(scope, dict) else {}


def _tender_preview_context(purchase_number: str) -> dict[str, Any]:
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    card = None
    criteria = None
    spec: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    try:
        import decision_aid
        import document_processor as dp
        text = _decide_text(tender)
        card = decision_aid.build_card(tender, text=_decide_text(tender))
        criteria = dp.extract_evaluation_criteria(text)
    except Exception:
        logger.exception("preview decision card failed for %s", purchase_number)
    try:
        spec = db.list_tender_items(purchase_number)
    except Exception:
        logger.exception("preview tender items failed for %s", purchase_number)
    try:
        details = db.get_tender_details(purchase_number) or {}
    except Exception:
        logger.exception("preview tender details failed for %s", purchase_number)
    work_scope = details.get("work_scope") or {}
    if not spec and not work_scope:
        work_scope = _build_work_scope(tender)
    docs_files: list[str] = []
    docs_dir = _tender_documents_dir(tender)
    if docs_dir:
        try:
            docs_files = [_display_download_name(p) for p in sorted(docs_dir.iterdir()) if p.is_file()]
        except Exception:
            logger.exception("preview documents failed for %s", purchase_number)
    customer = None
    if tender.get("customer_inn"):
        try:
            customer = db.get_customer(tender["customer_inn"])
        except Exception:
            logger.exception("preview customer failed for %s", purchase_number)
    breakdown = _score_breakdown(tender)
    return {
        "tender": tender,
        "card": card,
        "criteria": criteria,
        "spec": spec,
        "spec_rollup": _spec_rollup(spec),
        "decision_reasons": _decision_reasons(tender),
        "breakdown": breakdown,
        "disputes": _score_disputes(breakdown),
        "work_scope": work_scope,
        "docs_files": docs_files,
        "documents_url": zakupki_documents_url(
            tender.get("url") or "", purchase_number, tender.get("notice_guid") or ""
        ),
        "customer": customer,
        "work_stages": WORK_STAGES,
        "stops": parse_stop(tender.get("filter_stop") or ""),
    }


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    selected: Optional[str] = None,
    decision: list[str] = Query(default=[]),
    status:   list[str] = Query(default=[]),
    law:      Optional[str]   = None,
    kw:       Optional[str]   = None,
    profile_id: Optional[int] = None,
    platform: list[str] = Query(default=[]),
    q:        Optional[str]   = None,
    exclude_kw: list[str] = Query(default=[]),
    price_from: Optional[float] = None,
    price_to:   Optional[float] = None,
    f1_min: Optional[int] = Query(None, ge=1, le=5),
    f2_min: Optional[int] = Query(None, ge=1, le=5),
    f3_min: Optional[int] = Query(None, ge=1, le=5),
    f4_min: Optional[int] = Query(None, ge=1, le=5),
    f5_min: Optional[int] = Query(None, ge=1, le=5),
    f6_min: Optional[int] = Query(None, ge=1, le=5),
    f7_min: Optional[int] = Query(None, ge=1, le=5),
    f8_min: Optional[int] = Query(None, ge=1, le=5),
    score_min: Optional[int] = Query(None, ge=0, le=40),
    score_max: Optional[int] = Query(None, ge=0, le=40),
    days_min: Optional[int] = None,
    days_max: Optional[int] = None,
    sort_by: str = "score",
    order:   str = "desc",
    limit:   int = Query(300, ge=1, le=1000),
):
    ctx = _tenders_context(
        decision, status, law, kw, profile_id, platform, q, exclude_kw,
        price_from, price_to,
        f1_min, f2_min, f3_min, f4_min, f5_min, f6_min, f7_min, f8_min,
        score_min, score_max, days_min, days_max,
        sort_by, order, limit,
    )
    ctx["stats"] = db.get_stats_extended()
    default_number = selected or (ctx["tenders"][0]["purchase_number"] if ctx["tenders"] else None)
    ctx["active_selected"] = default_number
    ctx["preview"] = _tender_preview_context(default_number) if default_number else None
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/partials/tenders", response_class=HTMLResponse)
async def partial_tenders(
    request: Request,
    selected: Optional[str] = None,
    decision: list[str] = Query(default=[]),
    status:   list[str] = Query(default=[]),
    law:      Optional[str]   = None,
    kw:       Optional[str]   = None,
    profile_id: Optional[int] = None,
    platform: list[str] = Query(default=[]),
    q:        Optional[str]   = None,
    exclude_kw: list[str] = Query(default=[]),
    price_from: Optional[float] = None,
    price_to:   Optional[float] = None,
    f1_min: Optional[int] = Query(None, ge=1, le=5),
    f2_min: Optional[int] = Query(None, ge=1, le=5),
    f3_min: Optional[int] = Query(None, ge=1, le=5),
    f4_min: Optional[int] = Query(None, ge=1, le=5),
    f5_min: Optional[int] = Query(None, ge=1, le=5),
    f6_min: Optional[int] = Query(None, ge=1, le=5),
    f7_min: Optional[int] = Query(None, ge=1, le=5),
    f8_min: Optional[int] = Query(None, ge=1, le=5),
    score_min: Optional[int] = Query(None, ge=0, le=40),
    score_max: Optional[int] = Query(None, ge=0, le=40),
    days_min: Optional[int] = None,
    days_max: Optional[int] = None,
    sort_by: str = "score",
    order:   str = "desc",
    limit:   int = Query(300, ge=1, le=1000),
):
    ctx = _tenders_context(
        decision, status, law, kw, profile_id, platform, q, exclude_kw,
        price_from, price_to,
        f1_min, f2_min, f3_min, f4_min, f5_min, f6_min, f7_min, f8_min,
        score_min, score_max, days_min, days_max,
        sort_by, order, limit,
    )
    ctx["active_selected"] = selected
    return templates.TemplateResponse(request, "_tenders_table.html", ctx)


@app.get("/partials/tender-preview/{purchase_number}", response_class=HTMLResponse)
async def tender_preview(request: Request, purchase_number: str):
    return templates.TemplateResponse(
        request, "_tender_preview.html", _tender_preview_context(purchase_number)
    )


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    return templates.TemplateResponse(request, "analytics.html", {
        "feedback":  db.get_filter_feedback_analytics(),
        "corridors": db.get_all_price_corridors(),
        "customers": db.get_customers_list(limit=50),
        "risky":     db.get_customers_list(limit=20, only_risky=True),
    })


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    profiles = db.search_profiles_list()
    return templates.TemplateResponse(request, "search.html", {
        "profiles": profiles,
        "defaults": {
            "price_min": config.get_runtime("PRICE_MIN", config.PRICE_MIN),
            "price_max": config.get_runtime("PRICE_MAX", config.PRICE_MAX),
            "days_back": config.get_runtime("PUBLISH_DAYS_BACK", config.PUBLISH_DAYS_BACK),
            "fz44":      config.SEARCH_44FZ,
            "fz223":     config.SEARCH_223FZ,
        },
    })


RUNS_PAGE_SIZE = 30


@app.get("/control", response_class=HTMLResponse)
async def control_page(request: Request, run_page: int = 1):
    settings = db.get_all_settings()
    stats    = db.get_stats_extended()
    job_status = JobRunner.status()

    # История запусков — с пагинацией (после агрегации Stage1 по каналам строк
    # стало на порядок меньше на прогон, но история за много дней всё равно
    # длинная, а 20 записей без пагинации отрезали её слишком быстро).
    run_page = max(1, run_page)
    runs: list[dict] = []
    runs_total = 0
    try:
        import psycopg2.extras
        with db._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM runs")
            runs_total = cur.fetchone()[0]
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT %s OFFSET %s",
                (RUNS_PAGE_SIZE, (run_page - 1) * RUNS_PAGE_SIZE),
            )
            runs = [dict(r) for r in cur.fetchall()]
    except Exception:
        runs = []
    runs_total_pages = max(1, (runs_total + RUNS_PAGE_SIZE - 1) // RUNS_PAGE_SIZE)

    return templates.TemplateResponse(request, "control.html", {
        "settings":   settings,
        "stats":      stats,
        "jobs":       JobRunner.JOBS,
        "job_status": job_status,
        "runs":       runs,
        "runs_page":  run_page,
        "runs_total_pages": runs_total_pages,
        "runs_total": runs_total,
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


@app.get("/tender/{purchase_number}", response_class=RedirectResponse)
async def tender_detail(request: Request, purchase_number: str):
    if not db.get_tender(purchase_number):
        raise HTTPException(404, "Тендер не найден")
    return RedirectResponse(url=f"/?selected={purchase_number}", status_code=302)


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


@app.get("/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request):
    import llm_provider
    return templates.TemplateResponse(request, "profiles.html", {
        "profiles": db.search_profiles_list(),
        "llm_available": llm_provider.is_configured(),
        "default_price_lo": config.get_runtime("PRICE_TARGET_LO", config.PRICE_TARGET_LO),
        "default_price_hi": config.get_runtime("PRICE_TARGET_HI", config.PRICE_TARGET_HI),
        "default_price_hard_max": config.get_runtime("PRICE_HARD_MAX", config.PRICE_HARD_MAX),
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
    profiles = db.search_profiles_list()
    return {
        "profiles": [
            {"id": p["id"], "name": p["name"], "enabled": p["enabled"],
             "is_default": p["is_default"], "phrase_count": len(p.get("phrases") or [])}
            for p in profiles
        ],
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
    """Запускает фоновый поиск по выбранным поисковым профилям (не по произвольным фразам)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    profile_ids = body.get("profile_ids") or []
    if not isinstance(profile_ids, list):
        profile_ids = []
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
    started, reason = SearchRunner.start(profile_ids, params)
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


def _tender_documents_dir(tender: dict) -> Path | None:
    """Находит документы локально, даже если путь в БД записан на другой машине."""
    import document_processor as dp

    purchase_number = str(tender.get("purchase_number") or "")
    candidates = [config.DOCUMENTS_DIR / dp.safe_filename(purchase_number)]
    stored = str(tender.get("documents_dir") or "").strip()
    if stored:
        candidates.append(Path(stored))
    for candidate in candidates:
        if candidate.is_dir() and any(path.is_file() for path in candidate.iterdir()):
            return candidate
    return None


def _zip_documents(files: list[Path], purchase_number: str) -> Path:
    """Собирает ZIP из уже скачанных файлов в кеш-каталог (не в per-tender docs_dir,
    чтобы архив не попадал в списки/парсинг документов)."""
    import document_processor as dp
    archive_dir = config.DOCUMENTS_DIR / "_archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    zip_path = archive_dir / f"{dp.safe_filename(purchase_number)}.zip"
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


def _fetch_tender_documents(tender: dict, progress_cb: Callable[..., None] | None = None) -> dict:
    """Скачивает документы тендера с ЕИС, извлекает текст и сохраняет его в БД.

    Возвращает {"files": [Path...], "dir": str, "text": str}. Бросает HTTPException(502),
    если с ЕИС ничего не скачалось. progress_cb(stage, **data), если задан, зовётся на
    каждом переходе (connecting/connected/downloading/extracting/saving) — для UI-прогресса.
    """
    def _cb(stage: str, **data) -> None:
        if progress_cb:
            try:
                progress_cb(stage, **data)
            except Exception:
                pass

    purchase_number = tender["purchase_number"]
    from document_processor import download_documents, hash_files, collect_document_text
    from scraper import extract_notice_guid, get_tender_page

    cached_dir = _tender_documents_dir(tender)
    cached_files = [p for p in cached_dir.iterdir() if p.is_file()] if cached_dir else []
    if cached_files:
        _cb("cached", current=len(cached_files), total=len(cached_files))
        cached_text = collect_document_text(cached_files)
        return {"files": cached_files, "dir": str(cached_dir), "text": cached_text}

    # SberB2B: документы блока «Документы» карточки /needs/<номер>, не с ЕИС.
    if (tender.get("platform") or "") == "SberB2B":
        import document_processor as dp
        from sources.sberb2b import download_documents as _download_sber_docs
        _cb("connecting", url=tender.get("url") or "")
        sber_dir = config.DOCUMENTS_DIR / dp.safe_filename(purchase_number)
        files = _download_sber_docs(purchase_number, sber_dir)
        files = [Path(p) for p in files if Path(p).is_file()]
        if not files:
            raise HTTPException(502, detail={
                "message": "SberB2B не отдал документы (нет вложений или протух SFSESSID)",
                "documents_url": tender.get("url") or "",
            })
        _cb("extracting")
        text = collect_document_text(files)
        _cb("saving")
        with db._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE tenders SET
                    document_count = %s,
                    documents_dir = %s,
                    documents_hash = %s,
                    document_text_full = %s,
                    document_text_excerpt = %s,
                    updated_at = NOW()
                WHERE purchase_number = %s
                """,
                (len(files), str(sber_dir), hash_files(files), text, text[:4000], purchase_number),
            )
        return {"files": files, "dir": str(sber_dir), "text": text}

    def _fetch_from(url: str) -> dict:
        _cb("connecting", url=url)
        html, _ = get_tender_page(url, retries=1, timeout=20)
        if not html:
            return {"files": []}
        _cb("connected")
        return download_documents(
            purchase_number, html, url,
            on_progress=lambda i, total, name: _cb("downloading", current=i, total=total, name=name),
        )

    source_url = tender.get("url") or ""
    is_223 = "notice223" in source_url.lower() or bool(re.fullmatch(r"3\d{10}", purchase_number))
    notice_guid = tender.get("notice_guid") or extract_notice_guid(source_url)
    common_url = zakupki_common_info_url(source_url, purchase_number)
    common_html = ""

    if is_223 and not notice_guid:
        _cb("resolving")
        common_html, _ = get_tender_page(common_url, retries=1, timeout=20)
        notice_guid = extract_notice_guid(common_html)
        if notice_guid:
            tender["notice_guid"] = notice_guid
            with db._conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE tenders SET notice_guid = %s, updated_at = NOW() WHERE purchase_number = %s",
                    (notice_guid, purchase_number),
                )

    docs_url = zakupki_documents_url(source_url, purchase_number, notice_guid)
    docs = _fetch_from(docs_url)

    if not docs.get("files"):
        if not common_html:
            _cb("connecting", url=common_url)
            common_html, _ = get_tender_page(common_url, retries=1, timeout=20)
        if common_html:
            _cb("connected")
            docs = download_documents(
                purchase_number, common_html, common_url,
                on_progress=lambda i, total, name: _cb("downloading", current=i, total=total, name=name),
            )

    files = [Path(p) for p in docs.get("files", []) if Path(p).is_file()]
    if not files:
        raise HTTPException(502, detail={
            "message": "ЕИС не отдала документы серверу",
            "documents_url": docs_url,
        })

    _cb("extracting")
    text = collect_document_text(files)
    excerpt = text[:4000]

    _cb("saving")
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE tenders SET
                document_count = %s,
                documents_dir = %s,
                documents_hash = %s,
                document_text_full = %s,
                document_text_excerpt = %s,
                updated_at = NOW()
            WHERE purchase_number = %s
            """,
            (len(files), docs.get("dir", ""), hash_files(files), text, excerpt, purchase_number),
        )
    return {"files": files, "dir": docs.get("dir", ""), "text": text}


# Прогресс скачивания документов по одному тендеру, для поллинга с фронта
# (тот же паттерн, что SearchRunner._state — фоновый поток + словарь под локом).
_DOC_PROGRESS: dict[str, dict] = {}
_DOC_PROGRESS_LOCK = threading.Lock()


def _set_doc_progress(purchase_number: str, **kw) -> None:
    with _DOC_PROGRESS_LOCK:
        state = _DOC_PROGRESS.setdefault(purchase_number, {})
        state.update(kw)


@app.post("/api/tender/{purchase_number}/documents/fetch")
async def api_tender_documents_fetch(purchase_number: str):
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    if (tender.get("platform") or "ЕИС") != "ЕИС":
        raise HTTPException(400, "Скачивание документов пока поддерживается только для ЕИС")

    with _DOC_PROGRESS_LOCK:
        existing = _DOC_PROGRESS.get(purchase_number)
        if existing and existing.get("status") == "running":
            return JSONResponse(content={"ok": True, "started": False})
        _DOC_PROGRESS[purchase_number] = {"status": "running", "stage": "connecting",
                                           "current": 0, "total": 0, "name": "", "error": None}

    def worker() -> None:
        try:
            result = _fetch_tender_documents(
                tender, progress_cb=lambda stage, **data: _set_doc_progress(purchase_number, stage=stage, **data))
            files = result["files"]
            _set_doc_progress(
                purchase_number, status="done", stage="done",
                files=[_display_download_name(p) for p in files],
                excerpt=result["text"][:4000],
                zip_url=f"/api/tender/{purchase_number}/documents.zip",
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            _set_doc_progress(
                purchase_number,
                status="error",
                error=detail.get("message") or "Документы не скачались с ЕИС",
                documents_url=detail.get("documents_url") or zakupki_documents_url(
                    tender.get("url") or "",
                    purchase_number,
                    tender.get("notice_guid") or "",
                ),
            )
        except Exception as exc:
            logger.exception("Фоновое скачивание документов упало для %s", purchase_number)
            _set_doc_progress(purchase_number, status="error", error=str(exc))

    threading.Thread(target=worker, daemon=True, name=f"docs-fetch-{purchase_number}").start()
    return JSONResponse(content={"ok": True, "started": True})


@app.get("/api/tender/{purchase_number}/documents/progress")
async def api_tender_documents_progress(purchase_number: str):
    with _DOC_PROGRESS_LOCK:
        state = dict(_DOC_PROGRESS.get(purchase_number) or {"status": "idle"})
    return JSONResponse(content=state)


@app.get("/api/tender/{purchase_number}/documents.zip")
async def api_tender_documents_zip_download(purchase_number: str):
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    docs_dir = _tender_documents_dir(tender)
    files = [p for p in docs_dir.iterdir() if p.is_file()] if docs_dir else []
    if not files:
        raise HTTPException(404, "Документы ещё не скачаны")
    zip_path = _zip_documents(files, purchase_number)
    return FileResponse(zip_path, media_type="application/zip",
                         filename=f"{purchase_number}_documents.zip")


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
        "PUBLISH_DATE_FROM", "PUBLISH_DATE_TO", "STAGE1_WATERMARK_OVERLAP_DAYS",
        "MIN_PRIMARY_SCORE_FOR_DETAIL", "MIN_DETAILED_SCORE_FOR_NOTIFY", "NOTIFY_ONLY_ZMO",
        "DOCUMENT_DOWNLOAD_MIN_SCORE", "MIN_SCORE_FOR_LLM",
        "OKPD2_SEARCH_ENABLED", "OKPD2_CODES", "SEARCH_KEYWORDS",
        "CHANGE_CHECK_HOURS", "CHANGE_MIN_SCORE", "WINNER_ANALYTICS_PAGES",
        "SOURCE_B2B_ENABLED", "B2B_SEARCH_PAGES",
        "SOURCE_SPB_ENABLED", "SPB_SEARCH_PAGES", "SPB_SEARCH_KEYWORDS",
        "SOURCE_EAT_ENABLED", "EAT_SEARCH_PAGES", "EAT_SEARCH_KEYWORDS",
        "SOURCE_MOS_ENABLED", "MOS_SEARCH_PAGES", "MOS_SEARCH_KEYWORDS", "MOS_REGION_PATHS",
        "SOURCE_MOSREG_ENABLED", "MOSREG_SEARCH_PAGES", "MOSREG_SEARCH_KEYWORDS",
        "SOURCE_ZAKAZRF_ENABLED", "ZAKAZRF_SEARCH_PAGES", "ZAKAZRF_SEARCH_KEYWORDS",
        "ZAKAZRF_PLANED_DATE_FROM_TICKS",
        "SOURCE_RTS_ENABLED", "RTS_SEARCH_PAGES", "RTS_SEARCH_KEYWORDS",
        "RTS_COOKIE_STRING", "RTS_TOKEN", "RTS_TENANT_IDS",
        "SOURCE_SBERB2B_ENABLED", "SBERB2B_SEARCH_PAGES", "SBERB2B_SEARCH_KEYWORDS",
        "SBERB2B_SESSION_ID", "SBERB2B_SUBJECT_DOMAIN",
        "BACKFILL_SEARCH_PAGES",
        "LLM_PROVIDER", "OPENROUTER_TRIAGE_MODEL", "OPENROUTER_DEEP_MODEL",
        "LLM_TRIAGE_ENABLED",
        "OPENROUTER_FALLBACK_MODELS", "LLM_DAILY_REQUEST_LIMIT",
        "STAGE2_LIMIT",
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


@app.post("/api/tenders/manual")
async def api_create_manual_tender(request: Request):
    data = await request.json()
    title = (data.get("title") or "").strip()
    source_url = (data.get("source_url") or "").strip()
    deadline = (data.get("deadline") or "").strip()
    nmck_value = data.get("nmck")
    raw_nmck = "" if nmck_value is None else str(nmck_value).strip()

    if not title:
        raise HTTPException(400, "Название обязательно")
    parsed_url = urlparse(source_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise HTTPException(400, "Источник должен быть ссылкой http(s)")
    if deadline and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
        raise HTTPException(400, "Срок должен быть в формате ГГГГ-ММ-ДД")

    nmck = None
    if raw_nmck:
        normalized = raw_nmck.replace("\u00a0", "").replace(" ", "").replace(",", ".")
        try:
            nmck = float(normalized)
        except ValueError as exc:
            raise HTTPException(400, "НМЦК должна быть числом") from exc
        if nmck < 0:
            raise HTTPException(400, "НМЦК не может быть отрицательной")

    try:
        purchase_number = db.create_manual_tender(title, source_url, nmck, deadline)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(content={"ok": True, "purchase_number": purchase_number})


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


# ══════════════════════════════════════════════════════════════════════════════
# API — Поисковые профили (/profiles)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/profiles")
async def api_profiles_list():
    return JSONResponse(content=db.search_profiles_list())


@app.post("/api/profiles")
async def api_profile_create(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        data = {}
    try:
        new_id = db.search_profile_save(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    import search_profiles as sp
    sp.invalidate_cache()
    return {"ok": True, "id": new_id}


@app.patch("/api/profiles/{profile_id}")
async def api_profile_update(profile_id: int, request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        data = {}
    current = db.search_profile_get(profile_id)
    if not current:
        raise HTTPException(404, "Профиль не найден")
    merged = {**current, **data, "id": profile_id}
    want_default = bool(data.get("is_default"))
    try:
        db.search_profile_save(merged)
        if want_default:
            db.search_profile_set_default(profile_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    import search_profiles as sp
    sp.invalidate_cache()
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/copy")
async def api_profile_copy(profile_id: int):
    try:
        new_id = db.search_profile_copy(profile_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    import search_profiles as sp
    sp.invalidate_cache()
    return {"ok": True, "id": new_id}


@app.delete("/api/profiles/{profile_id}")
async def api_profile_delete(profile_id: int):
    try:
        db.search_profile_delete(profile_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    import search_profiles as sp
    sp.invalidate_cache()
    return {"ok": True}


@app.put("/api/profiles/{profile_id}/phrases")
async def api_profile_phrases_replace(profile_id: int, request: Request):
    data = await request.json()
    phrases = data.get("phrases") if isinstance(data, dict) else None
    if not isinstance(phrases, list):
        raise HTTPException(400, "Ожидается {phrases: [...]}")
    db.search_phrases_replace(profile_id, phrases)
    import search_profiles as sp
    sp.invalidate_cache()
    return {"ok": True, "count": len(phrases)}


@app.post("/api/profiles/{profile_id}/preview")
async def api_profile_preview(profile_id: int, request: Request):
    """Прогоняет профиль по последним N тендерам в БД — без похода в ЕИС."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    limit = int((body or {}).get("limit") or 500) if isinstance(body, dict) else 500

    row = db.search_profile_get(profile_id)
    if not row:
        raise HTTPException(404, "Профиль не найден")

    import search_profiles as sp
    profile = sp._profile_from_row(row)
    tenders = db.get_recent_tenders_for_preview(limit)

    accepted: list[dict] = []
    rejected_hard: list[dict] = []
    for t in tenders:
        tokens = sp.tokenize(sp.build_tender_text(t))
        result = sp.match_profile(profile, tokens)
        entry = {
            "purchase_number": t.get("purchase_number"),
            "title": t.get("title"),
            "score": result.score,
            "phrases": [h[0] for h in result.hits],
        }
        if result.accepted:
            accepted.append(entry)
        elif result.hard_hit:
            entry["hard_hit"] = result.hard_hit
            rejected_hard.append(entry)

    return {
        "total_checked": len(tenders),
        "accepted": accepted[:200],
        "rejected_hard": rejected_hard[:200],
    }


@app.post("/api/profiles/{profile_id}/ai-suggest")
async def api_profile_ai_suggest(profile_id: int, request: Request):
    """ИИ-черновик плюс/минус-фраз по описанию услуг. Ничего не сохраняет —
    UI применяет только отмеченные пользователем строки через PUT /phrases."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    description = str((body or {}).get("description") or "").strip()
    if not description:
        raise HTTPException(400, "Опишите услуги для генерации фраз")

    row = db.search_profile_get(profile_id)
    if not row:
        raise HTTPException(404, "Профиль не найден")
    existing = [p.get("phrase") for p in (row.get("phrases") or []) if p.get("phrase")]

    from llm_analyzer import suggest_profile_phrases
    result = suggest_profile_phrases(description, existing)
    if result is None:
        return JSONResponse({"ok": False, "reason": "llm_unavailable"}, status_code=503)
    return {"ok": True, "phrases": result}


@app.post("/api/profiles/{profile_id}/ai-review")
async def api_profile_ai_review(profile_id: int):
    """ИИ-проверка текущих фраз профиля на риск ложных срабатываний."""
    row = db.search_profile_get(profile_id)
    if not row:
        raise HTTPException(404, "Профиль не найден")

    from llm_analyzer import review_profile_phrases
    result = review_profile_phrases(row.get("name", ""), row.get("phrases") or [])
    if result is None:
        return JSONResponse({"ok": False, "reason": "llm_unavailable"}, status_code=503)
    return {"ok": True, "reviews": result}


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
        tender.get("document_text_full")
        or tender.get("document_text_excerpt")
        or tender.get("primary_text")
        or tender.get("description")
        or tender.get("title")
        or ""
    )


def _readable_doc_name(path: Path) -> str:
    try:
        return path.name.encode("latin1").decode("utf-8")
    except Exception:
        return path.name


def _work_scope_doc_score(path: Path) -> int:
    name = _readable_doc_name(path).lower().replace("ё", "е")
    score = 0
    if "техническ" in name:
        score += 80
    if "тз" in name or "техническое задание" in name:
        score += 80
    if "описание" in name and "объект" in name:
        score += 70
    if "услуг" in name or "работ" in name:
        score += 45
    if "прилож" in name:
        score += 20
    if any(x in name for x in ("контракт", "договор", "нмц", "расчет", "обоснован", "проект")):
        score -= 80
    return score


def _work_scope_source_text(tender: dict) -> tuple[str, str]:
    docs_dir = tender.get("documents_dir") or ""
    if docs_dir:
        try:
            import document_processor as dp
            files = [p for p in Path(docs_dir).iterdir() if p.is_file()]
            files.sort(key=lambda p: (_work_scope_doc_score(p), -p.stat().st_size), reverse=True)
            chunks: list[str] = []
            source = ""
            for path in files[:8]:
                text = dp.extract_text_from_file(path)
                if not text or len(text.strip()) < 80:
                    continue
                chunks.append(text)
                if not source:
                    source = _readable_doc_name(path)
                if sum(len(c) for c in chunks) >= 30000:
                    break
            if chunks:
                return "\n\n".join(chunks), source
        except Exception:
            logger.exception("work_scope document text failed for %s", tender.get("purchase_number"))
    return _decide_text(tender), ""


def _build_work_scope(tender: dict) -> dict:
    docs_dir = tender.get("documents_dir") or ""
    if docs_dir:
        try:
            import document_processor as dp
            files = [p for p in Path(docs_dir).iterdir() if p.is_file()]
            scope = dp.extract_work_scope_from_files(files)
            if scope:
                if not scope.get("text"):
                    scope["text"] = (tender.get("title") or "").strip()
                return scope
        except Exception:
            logger.exception("work_scope document parse failed for %s", tender.get("purchase_number"))

    text, source = _work_scope_source_text(tender)
    import document_processor as dp
    scope = dp.extract_work_scope(text, source=source)
    if not scope.get("text"):
        scope["text"] = (tender.get("title") or "").strip()
    if source and not scope.get("source"):
        scope["source"] = source
    return scope


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

    # Веб-процесс может жить дольше, чем ключ в .env — перечитываем перед проверкой.
    config.reload_llm_secrets()

    # Для объяснения нужен текст документации, а не только карточка. Если её ещё
    # не скачивали — тянем с ЕИС на лету (сбой не блокирует, работаем с тем, что есть).
    if not tender.get("document_text_full") and (tender.get("platform") or "ЕИС") == "ЕИС":
        try:
            _fetch_tender_documents(tender)
            tender = db.get_tender(purchase_number)
        except HTTPException:
            logger.info("decide_explain: документы не скачались для %s, работаем без них", purchase_number)
        except Exception:
            logger.exception("decide_explain: сбой автоскачивания документов для %s", purchase_number)

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
                                     "error": "LLM недоступна — добавьте ключ OPENROUTER_API_KEY (или ANTHROPIC_API_KEY) в .env и перезапустите веб-панель."})

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

    # Web-процесс может жить дольше, чем ключ в .env. Stage 2.5 запускается
    # отдельным процессом и всегда читает актуальный файл при старте.
    config.reload_llm_secrets()
    if not llm_provider.is_configured():
        return JSONResponse(content={"heuristic": heuristic, "llm": None,
                                     "error": "LLM недоступна — проверьте LLM_PROVIDER и API-ключ в .env."})
    if not text.strip():
        return JSONResponse(content={"heuristic": heuristic, "llm": None,
                                     "error": "В БД нет текста документации для разбора."})
    chunks = dp._windows_around(text, dp._CRITERIA_MARKERS) or [text[:config.LLM_TEXT_CHARS]]
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
    """Обновляет lifecycle-поля тендера (стадия/срок/заметка). Стадия валидируется.

    Смена стадии логируется в decisions (аудит переходов на доске). Переход в
    стадию «failed» дополнительно помечает тендер как отказной (третий вид
    отказа, наряду с not_my_profile/manual_decline). Уход со стадии «failed»
    на любую другую снимает этот отказ.
    """
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    data = await request.json()
    old_stage = tender.get("work_stage")

    fields: dict = {}
    new_stage: str | None = None
    stage_changed = False
    if "work_stage" in data:
        stage = (data.get("work_stage") or "").strip()
        if stage and stage not in WORK_STAGE_KEYS:
            raise HTTPException(400, f"Неизвестная стадия: {stage}")
        fields["work_stage"] = stage  # "" → снять с доски (NULL в БД)
        new_stage = stage or None
        stage_changed = new_stage != old_stage

    comment = (data.get("comment") or "").strip()
    if "work_due" in data:
        due = (data.get("work_due") or "").strip()
        if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            raise HTTPException(400, "Некорректная дата work_due (нужен формат ГГГГ-ММ-ДД)")
        fields["work_due"] = due or None
    if "work_note" in data:
        fields["work_note"] = (data.get("work_note") or "")[:2000]
    elif stage_changed and comment:
        fields["work_note"] = comment[:2000]

    if not fields:
        raise HTTPException(400, "Нет полей для обновления")

    db.set_workflow(purchase_number, fields)

    # Комментарий для аудита: явный comment (с доски) важнее work_note (из карточки тендера)
    effective_comment = comment or fields.get("work_note", "")

    if stage_changed:
        if new_stage == "failed":
            db.set_decision(purchase_number, "rejected", effective_comment or REJECTION_REASONS["failed"], rejection_reason="failed")
        else:
            db.log_stage_change(purchase_number, new_stage or "", effective_comment)
            if old_stage == "failed" and tender.get("rejection_reason") == "failed":
                db.clear_decision(purchase_number)

    updated = db.get_tender(purchase_number) or {}
    return JSONResponse(content={"ok": True, "workflow": {
        "work_stage": updated.get("work_stage"),
        "work_due": updated.get("work_due"),
        "work_note": updated.get("work_note"),
        "work_updated_at": updated.get("work_updated_at"),
    }})


@app.post("/api/tender/{purchase_number}/deadline")
async def api_tender_deadline(purchase_number: str, request: Request):
    """Ручная правка срока подачи заявок — источники иногда публикуют дату
    неточно, либо заказчик продлевает срок без изменения текста карточки."""
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")
    data = await request.json()
    raw = (data.get("deadline") or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if not m:
        raise HTTPException(400, "Нужен формат ГГГГ-ММ-ДД")
    y, mo, d = m.groups()
    db.update_tender_deadline(purchase_number, f"{d}.{mo}.{y}")
    updated = db.get_tender(purchase_number) or {}
    return JSONResponse(content={"ok": True, "deadline": updated.get("deadline")})


@app.post("/api/tender/{purchase_number}/reject")
async def api_tender_reject(purchase_number: str, request: Request):
    """Помечает тендер как отказной и убирает его из обычных списков/доски."""
    tender = db.get_tender(purchase_number)
    if not tender:
        raise HTTPException(404, "Тендер не найден")

    try:
        data = await request.json()
    except Exception:
        data = {}
    reason = data.get("reason") or "manual_decline"
    if reason not in REJECTION_REASONS:
        raise HTTPException(400, "Неизвестная причина отказа")

    db.set_decision(
        purchase_number,
        "rejected",
        REJECTION_REASONS[reason],
        rejection_reason=reason,
    )
    db.set_workflow(purchase_number, {"work_stage": "", "work_due": None})
    updated = db.get_tender(purchase_number) or {}
    return JSONResponse(content={
        "ok": True,
        "decision": updated.get("decision"),
        "rejection_reason": updated.get("rejection_reason"),
    })


@app.get("/board", response_class=HTMLResponse)
async def board(request: Request):
    """Список тендеров в работе с фильтрами по стадии."""
    tenders = db.list_workflow_tenders()
    stages = [{**s, "count": 0} for s in WORK_STAGES]
    by_key = {s["key"]: s for s in stages}
    unknown_count = 0
    for t in tenders:
        t["work_due_state"] = _work_due_state(t.get("work_due"))
        stage = t.get("work_stage")
        if stage in by_key:
            by_key[stage]["count"] += 1
            t["work_stage_label"] = by_key[stage]["label"]
            t["work_stage_filter"] = stage
        else:
            logger.warning("board: неизвестная стадия %r у %s", stage, t.get("purchase_number"))
            unknown_count += 1
            t["work_stage_label"] = "Другое"
            t["work_stage_filter"] = "_other"

        decision = (t.get("filter_decision") or "").upper()
        t["decision_label"] = {
            "GO": "БЕРИ", "CAUTION": "ПОДУМАЙ", "NO-GO": "ПРОПУСТИ",
        }.get(decision, "В РАБОТЕ")
        t["decision_tone"] = {
            "GO": "go", "CAUTION": "caution", "NO-GO": "nogo",
        }.get(decision, "unknown")
        t["decision_reason"] = (
            t.get("work_note")
            or t.get("llm_triage_reason")
            or t.get("llm_verdict")
            or "Откройте карточку, чтобы продолжить работу."
        )
        t["risk_tags"] = _risk_tags(t)

    if unknown_count:
        stages.append({
            "key": "_other", "label": "Другое", "color": "gray",
            "active": False, "count": unknown_count,
        })
    return templates.TemplateResponse(request, "board.html", {
        "stages": stages, "tenders": tenders, "total": len(tenders),
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
            return JSONResponse(content={"error": "LLM недоступна — добавьте ключ OPENROUTER_API_KEY (или ANTHROPIC_API_KEY) в .env и перезапустите веб-панель."})
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
