"""
knowledge_base.py — движок базы знаний.

Собирает персонализированный контекст из БД и передаёт его в:
  - filter_engine.py  — персональные stop/warn/boost правила
  - llm_analyzer.py   — контекст «кто мы, что умеем, что боимся»

Разделы базы знаний:
  1. kb_contracts    — исполненные контракты (опыт для ст.31 44-ФЗ)
  2. kb_competencies — технологии и сертификаты команды
  3. kb_equipment    — каталог железа и аналогов
  4. kb_risk_rules   — персональные правила рисков
  5. kb_templates    — шаблоны запросов и претензий
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import database as db

logger = logging.getLogger(__name__)


_RISK_RULES_CACHE: list[dict[str, Any]] | None = None
_RISK_RULES_CACHE_TS = 0.0
_RISK_RULES_CACHE_TTL = 60.0


def _active_risk_rules(force: bool = False) -> list[dict[str, Any]]:
    """Активные персональные правила с коротким кешем между лотами."""
    global _RISK_RULES_CACHE, _RISK_RULES_CACHE_TS
    now = time.monotonic()
    if (not force and _RISK_RULES_CACHE is not None
            and now - _RISK_RULES_CACHE_TS < _RISK_RULES_CACHE_TTL):
        return _RISK_RULES_CACHE
    rules = db.kb_risk_rules_active()
    _RISK_RULES_CACHE = rules
    _RISK_RULES_CACHE_TS = now
    return rules


# ══════════════════════════════════════════════════════════════════════════════
# Контекст для LLM
# ══════════════════════════════════════════════════════════════════════════════

def build_llm_context(tender: dict, tz_text: str = "") -> str:
    """
    Собирает релевантный контекст из БЗ в текстовый блок для вставки в LLM-промпт.

    Включает:
      - похожие контракты из нашего опыта
      - наши компетенции по теме тендера
      - персональные stop-правила (из kb_risk_rules)
      - аналоги оборудования если тендер на поставку железа
    """
    from winner_analytics import classify_category
    category  = classify_category(tender.get("title", ""))
    combined  = (tender.get("title", "") + " " + tz_text).lower()
    parts: list[str] = []

    # 1. Релевантный опыт ────────────────────────────────────────────────────
    contracts = db.kb_contracts_by_category(category, limit=3)
    if not contracts:
        # Нет по категории — берём любые последние 2
        contracts = db.kb_contracts_list()[:2]

    if contracts:
        lines = ["НАШ ОПЫТ (похожие исполненные контракты):"]
        for c in contracts:
            ts = c.get("tech_stack") or []
            if isinstance(ts, str):
                import json
                try: ts = json.loads(ts)
                except Exception: ts = [ts]
            stack_str = ", ".join(ts) if ts else "—"
            lines.append(
                f"  • {c.get('title','?')} ({c.get('year','?')}, {_fmt_price(c.get('price'))})"
                f" — стек: {stack_str}"
            )
            if c.get("problems"):
                lines.append(f"    Сложности: {c['problems'][:150]}")
        parts.append("\n".join(lines))

    # 2. Компетенции ─────────────────────────────────────────────────────────
    competencies = db.kb_competencies_for_category(category)
    if competencies:
        certs  = [c for c in competencies if c.get("certified")]
        techs  = [c["tech"] for c in competencies if c.get("level", 0) >= 3]
        lines  = ["НАШИ КОМПЕТЕНЦИИ:"]
        if techs:
            lines.append(f"  Технологии (уровень 3+): {', '.join(techs[:15])}")
        if certs:
            lines.append(f"  Сертификаты: {', '.join(c['tech'] for c in certs[:5])}")
        parts.append("\n".join(lines))

    # 3. Персональные правила рисков ─────────────────────────────────────────
    rules   = _active_risk_rules()
    matched = []
    for r in rules:
        if r.get("pattern", "").lower() in combined:
            matched.append(f"  {'⛔' if r['rule_type']=='stop' else '⚠'} {r['reason']}"
                           f" (паттерн: «{r['pattern']}», вес: {r['weight']:+d})")
    if matched:
        parts.append("СРАБОТАВШИЕ ПЕРСОНАЛЬНЫЕ ПРАВИЛА:\n" + "\n".join(matched))

    # 4. Аналоги оборудования ────────────────────────────────────────────────
    if "поставка" in combined or "оборудование" in combined or "сервер" in combined:
        analogs = db.kb_equipment_find_analogs(combined)
        if analogs:
            lines = ["АНАЛОГИ ИЗ НАШЕГО КАТАЛОГА:"]
            for eq in analogs:
                lines.append(
                    f"  • {eq.get('vendor','')} {eq.get('model','')} — "
                    f"{_fmt_price(eq.get('price_rub'))}, срок {eq.get('lead_days','?')} дн."
                    f" (аналог для: {', '.join(eq.get('analog_for') or [])})"
                )
            parts.append("\n".join(lines))

    if not parts:
        return ""

    header = "=== КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ ==="
    footer = "=== КОНЕЦ КОНТЕКСТА ==="
    return header + "\n\n" + "\n\n".join(parts) + "\n\n" + footer


# ══════════════════════════════════════════════════════════════════════════════
# Персональные правила рисков для filter_engine
# ══════════════════════════════════════════════════════════════════════════════

def apply_custom_rules(combined_text: str) -> tuple[int, list[str]]:
    """
    Применяет активные kb_risk_rules к тексту тендера.
    Возвращает (суммарный_delta_score, список_сигналов).

    Вызывается из filter_engine.py в каждом фильтре.
    """
    try:
        rules = _active_risk_rules()
    except Exception:
        return 0, []

    delta   = 0
    signals = []
    text    = combined_text.lower()

    for rule in rules:
        pattern = (rule.get("pattern") or "").lower()
        if not pattern or pattern not in text:
            continue

        weight    = int(rule.get("weight") or -5)
        reason    = rule.get("reason", pattern)
        rule_type = rule.get("rule_type", "warn")

        delta += weight

        if rule_type == "stop":
            signals.append(f"✗ [БЗ-СТОП] {reason} ({weight:+d})")
        elif weight > 0:
            signals.append(f"✓ [БЗ] {reason} ({weight:+d})")
        else:
            signals.append(f"⚠ [БЗ] {reason} ({weight:+d})")

    return delta, signals


def has_stop_rule_match(combined_text: str) -> bool:
    """True если сработало хотя бы одно stop-правило."""
    try:
        rules = _active_risk_rules()
    except Exception:
        return False
    text = combined_text.lower()
    return any(
        r.get("rule_type") == "stop" and (r.get("pattern") or "").lower() in text
        for r in rules
    )


# ══════════════════════════════════════════════════════════════════════════════
# Матчинг компетенций под требования тендера
# ══════════════════════════════════════════════════════════════════════════════

def match_competencies(tz_text: str) -> dict[str, Any]:
    """
    Сравнивает требования ТЗ с нашими компетенциями.
    Возвращает: matched (что умеем), gaps (чего не хватает), score (0–100).
    """
    try:
        competencies = db.kb_competencies_list()
    except Exception:
        return {"matched": [], "gaps": [], "score": 50}

    text_lower  = tz_text.lower()
    our_techs   = {c["tech"].lower(): c for c in competencies}

    # Список технологий/требований из ТЗ (простая эвристика по ключевым словам)
    tech_patterns = [
        r"1с[- ]битрикс", r"bitrix24?", r"1с[: ]\w+", r"php\s*\d*",
        r"python\s*\d*", r"postgresql", r"mysql", r"linux",
        r"windows server", r"docker", r"kubernetes", r"react", r"vue",
        r"api[- ]интеграц", r"1с[- ]обмен", r"сканер\w*", r"тсд",
        r"видеонаблюден", r"скуд", r"nas\b", r"vps\b", r"vpn\b",
    ]

    found_reqs: list[str] = []
    for pat in tech_patterns:
        if re.search(pat, text_lower):
            found_reqs.append(re.sub(r"\\w\+|\\s\*\\d\*|\\b", "", pat).replace("\\", ""))

    if not found_reqs:
        return {"matched": [], "gaps": [], "score": 70, "note": "Специфических техтребований не найдено"}

    matched = [r for r in found_reqs if any(r[:5] in t for t in our_techs)]
    gaps    = [r for r in found_reqs if r not in matched]
    score   = int(len(matched) / max(len(found_reqs), 1) * 100)

    return {
        "matched":    matched,
        "gaps":       gaps,
        "score":      score,
        "total_reqs": len(found_reqs),
        "our_techs":  sorted(our_techs.keys()),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Профиль исполнителя из БЗ (для filter_engine)
# ══════════════════════════════════════════════════════════════════════════════

_PROFILE_CACHE: dict | None = None
_PROFILE_CACHE_TS: float     = 0.0
_CACHE_TTL = 300   # 5 минут


def get_profile() -> dict:
    """
    Возвращает профиль исполнителя, собранный из kb_competencies и kb_risk_rules.
    Кешируется на 5 минут.
    """
    import time
    global _PROFILE_CACHE, _PROFILE_CACHE_TS

    now = time.monotonic()
    if _PROFILE_CACHE and (now - _PROFILE_CACHE_TS) < _CACHE_TTL:
        return _PROFILE_CACHE

    try:
        competencies = db.kb_competencies_list()
        rules        = _active_risk_rules()
        import config
        profile = {
            "competencies": [c["tech"].lower() for c in competencies if c.get("level", 0) >= 2],
            "forbidden": [
                r["pattern"].lower() for r in rules
                if r.get("rule_type") == "stop"
            ],
            "price_min":      config.get_runtime("PRICE_MIN",      200_000),
            "price_max":      config.get_runtime("PRICE_MAX",    5_000_000),
            "price_target_lo":config.get_runtime("PRICE_MIN",      300_000),
            "price_target_hi":config.get_runtime("PRICE_MAX",    2_000_000),
            "cost_rate":        0.60,
            "guarantee_rate":   0.07,
            "guarantee_annual": 0.02,
            "exec_months":      3,
            "target_margin":    0.25,
        }
        if not profile["competencies"]:
            # Fallback если БЗ пустая — берём из filter_engine
            from filter_engine import PROFILE as _FP
            profile["competencies"] = _FP["competencies"]
        if not profile["forbidden"]:
            from filter_engine import PROFILE as _FP
            profile["forbidden"] = _FP["forbidden"]

        _PROFILE_CACHE    = profile
        _PROFILE_CACHE_TS = now
        return profile

    except Exception as e:
        logger.debug("KB profile fallback: %s", e)
        from filter_engine import PROFILE as _FP
        return _FP


def invalidate_profile_cache() -> None:
    """Сбрасывает кеш профиля — вызвать после сохранения компетенций/правил."""
    global _PROFILE_CACHE, _PROFILE_CACHE_TS, _RISK_RULES_CACHE, _RISK_RULES_CACHE_TS
    _PROFILE_CACHE    = None
    _PROFILE_CACHE_TS = 0.0
    _RISK_RULES_CACHE = None
    _RISK_RULES_CACHE_TS = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Поиск шаблонов
# ══════════════════════════════════════════════════════════════════════════════

def find_templates(query: str = "", category: str = "") -> list[dict]:
    """Ищет релевантные шаблоны по тексту запроса и/или категории."""
    templates = db.kb_templates_list()
    if category:
        templates = [t for t in templates if t.get("category") == category]
    if query:
        q = query.lower()
        templates = [
            t for t in templates
            if q in (t.get("title") or "").lower()
            or q in (t.get("content") or "").lower()
            or any(q in tag.lower() for tag in (t.get("tags") or []))
        ]
    return templates


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_price(price) -> str:
    if not price:
        return "—"
    try:
        p = float(price)
        return f"{p/1_000_000:.1f} млн ₽" if p >= 1_000_000 else f"{p/1000:.0f} тыс. ₽"
    except Exception:
        return str(price)


def seed_defaults() -> int:
    """
    Наполняет БЗ стартовыми правилами рисков — базовый stop-лист для ИТ.
    Вызывается из init_db.py --defaults. Не перезаписывает существующие записи.
    """
    existing = db.kb_risk_rules_list()
    if existing:
        logger.info("kb_risk_rules уже содержит %d записей, seed пропущен", len(existing))
        return 0

    default_rules = [
        # Stop-факторы
        ("stop", "лицензия фстэк",                     -10, "Требуется лицензия ФСТЭК — не имеем"),
        ("stop", "лицензия фсб",                       -10, "Требуется лицензия ФСБ — не имеем"),
        ("stop", "государственная тайна",               -10, "Работа с гостайной — не лицензированы"),
        ("stop", "членство в сро",                       -8, "Требуется СРО — нет допуска"),
        # Предупреждения
        ("warn", "24/7",                                  -4, "Круглосуточная поддержка — нет ресурса"),
        ("warn", "по заявкам без ограничения",            -4, "Безлимитный объём работ"),
        ("warn", "срок исполнения 3 рабочих дня",         -3, "Нереальный срок поставки"),
        ("warn", "без авансирования",                     -3, "Работа без аванса — кассовый разрыв"),
        ("warn", "штраф в размере 20%",                   -3, "Высокий штраф за нарушение"),
        ("warn", "штраф в размере 30%",                   -5, "Очень высокий штраф"),
        ("warn", "расторжение в одностороннем порядке",   -3, "Одностороннее расторжение заказчиком"),
        # Бустеры
        ("boost", "1с-битрикс",                            5, "Наша основная специализация"),
        ("boost", "bitrix24",                              5, "Наша основная специализация"),
        ("boost", "аванс 30%",                             3, "Нормальный аванс — хорошо"),
        ("boost", "аванс 50%",                             4, "Хороший аванс"),
    ]

    count = 0
    for rule_type, pattern, weight, reason in default_rules:
        db.kb_risk_rule_save({
            "rule_type": rule_type,
            "pattern":   pattern,
            "weight":    weight,
            "reason":    reason,
            "active":    True,
        })
        count += 1

    logger.info("kb_risk_rules: загружено %d стартовых правил", count)
    return count
