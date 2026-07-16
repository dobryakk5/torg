"""search_profiles.py — поисковые профили Stage 1 (плюс/минус-фразы).

Заменяет плоский config.SEARCH_KEYWORDS системой профилей: несколько
независимых наборов фраз с балльным скорингом карточки. Поиск в Stage 1
идёт ТОЛЬКО по включённым профилям (см. main.run_stage1); Tenderplan в эту
систему не входит — у него свой фильтр на стороне сервиса.

Синтаксис фразы (см. docs/tz-search-profiles.md, §3):
    * в конце слова      — усечение (стемминг): "разработк*" матчит "разработка/разработку/…"
    | внутри слова        — варианты: "битрикс|bitrix"
    (слово слово)~N       — окно близости N лишних слов между словами фразы
                            (без ~N окно по умолчанию = 1)

Матчинг токенный (целые слова), а не подстроковый — это одновременно
устраняет старый класс ложных срабатываний вида "обмен" → "теплообменник"
(см. комментарий в config.py): токен "теплообменник" не начинается с "обмен".

Кеш активных профилей — 60 секунд, инвалидируется invalidate_cache()
после сохранения в UI (по образцу filter_engine._load_rules()).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# ══════════════════════════════════════════════════════════════════════════════
# Нормализация и токенизация
# ══════════════════════════════════════════════════════════════════════════════

_WORD_RE = re.compile(r"[a-z0-9а-я]+(?:-[a-z0-9а-я]+)*")


def _normalize_text(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def tokenize(text: str) -> list[tuple[int, str]]:
    """Токенизирует текст: (позиция, слово). Дефисные слова дают доп. подтокены
    той же позиции — "1с-битрикс" → [(0,"1с-битрикс"), (0,"1с"), (0,"битрикс")],
    чтобы фраза из одного слова "битрикс" находила дефисные конструкции тоже.
    """
    text = _normalize_text(text)
    tokens: list[tuple[int, str]] = []
    pos = 0
    for m in _WORD_RE.finditer(text):
        word = m.group(0)
        tokens.append((pos, word))
        if "-" in word:
            for part in word.split("-"):
                if part:
                    tokens.append((pos, part))
        pos += 1
    return tokens


def build_tender_text(tender: dict[str, Any]) -> str:
    parts = [str(tender.get("title") or ""), str(tender.get("primary_text") or "")]
    return " ".join(p for p in parts if p)


# ══════════════════════════════════════════════════════════════════════════════
# Разбор синтаксиса фразы
# ══════════════════════════════════════════════════════════════════════════════

_WINDOW_RE = re.compile(r"^\((?P<inner>.+)\)~(?P<window>\d+)$")
_DEFAULT_WINDOW = 1


def parse_phrase(raw: str) -> tuple[list[str], int]:
    """"(строительн* работ*)~1" → (["строительн*", "работ*"], 1).
    Без явных скобок+~N окно по умолчанию = 1 (см. ТЗ §3).
    """
    raw = (raw or "").strip()
    m = _WINDOW_RE.match(raw)
    if m:
        inner, window = m.group("inner"), int(m.group("window"))
    else:
        inner, window = raw, _DEFAULT_WINDOW
    words = [_normalize_text(w) for w in inner.split() if w.strip()]
    return words, window


def _word_matches(token: str, pattern_word: str) -> bool:
    for alt in pattern_word.split("|"):
        alt = alt.strip()
        if not alt:
            continue
        if alt.endswith("*"):
            base = alt[:-1]
            if not base or token.startswith(base):
                return True
        elif token == alt:
            return True
    return False


def _positions_for_word(tokens: list[tuple[int, str]], pattern_word: str) -> list[int]:
    return sorted({pos for pos, tok in tokens if _word_matches(tok, pattern_word)})


def _find_span(positions_per_word: list[list[int]], window: int) -> bool:
    """True, если существуют позиции p1<=p2<=...<=pk (по одной на слово фразы,
    в заданном порядке), укладывающиеся в окно (k-1)+window позиций.
    """
    if not positions_per_word or any(not p for p in positions_per_word):
        return False
    k = len(positions_per_word)
    # states: start_position -> минимально достижимый last_position
    states: dict[int, int] = {p: p for p in positions_per_word[0]}
    for i in range(1, k):
        next_states: dict[int, int] = {}
        candidates = positions_per_word[i]
        for start, last in states.items():
            best_q = None
            for q in candidates:
                if q >= last and (best_q is None or q < best_q):
                    best_q = q
            if best_q is not None and (start not in next_states or best_q < next_states[start]):
                next_states[start] = best_q
        states = next_states
        if not states:
            return False
    max_span = (k - 1) + window
    return any((last - start) <= max_span for start, last in states.items())


def phrase_hits(tokens: list[tuple[int, str]], words: list[str], window: int) -> bool:
    if not words:
        return False
    positions_per_word = [_positions_for_word(tokens, w) for w in words]
    return _find_span(positions_per_word, window)


# ══════════════════════════════════════════════════════════════════════════════
# Модель данных
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Phrase:
    id: int | None
    kind: str  # plus | minus_hard | minus_soft
    phrase: str
    weight: int = 3
    query_only: bool = False
    local_only: bool = False
    query_text: str | None = None
    enabled: bool = True
    note: str = ""
    words: list[str] = field(default_factory=list, repr=False)
    window: int = field(default=_DEFAULT_WINDOW, repr=False)

    def __post_init__(self) -> None:
        self.words, self.window = parse_phrase(self.phrase)

    @property
    def signed_weight(self) -> int:
        return abs(self.weight) if self.kind == "plus" else -abs(self.weight)


@dataclass
class Profile:
    id: int | None
    name: str
    description: str = ""
    enabled: bool = True
    is_default: bool = False
    priority: int = 100
    min_score: int = 3
    okpd2_codes: list[str] = field(default_factory=list)
    phrases: list[Phrase] = field(default_factory=list)
    # Ценовой коридор Ф2 (filter_engine._filter_finance) для ЭТОГО профиля.
    # None у любого поля = наследовать глобальный config.PRICE_TARGET_LO/HI/PRICE_HARD_MAX.
    price_target_lo: int | None = None
    price_target_hi: int | None = None
    price_hard_max: int | None = None


@dataclass
class MatchResult:
    profile_id: int | None
    profile_name: str
    accepted: bool
    score: int
    hits: list[tuple[str, int]] = field(default_factory=list)
    hard_hit: str | None = None
    min_score: int = 3
    price_target_lo: int | None = None
    price_target_hi: int | None = None
    price_hard_max: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "score": self.score,
            "min_score": self.min_score,
            "phrases": [{"phrase": p, "weight": w} for p, w in self.hits],
        }


# ══════════════════════════════════════════════════════════════════════════════
# Кеш активных профилей (60с) с откатом на DEFAULT_PROFILES
# ══════════════════════════════════════════════════════════════════════════════

_PROFILES_CACHE: list[Profile] | None = None
_PROFILES_CACHE_TS = 0.0
_CACHE_TTL = 60.0


def invalidate_cache() -> None:
    """Сбросить кеш профилей — вызывать после сохранения/удаления через UI."""
    global _PROFILES_CACHE, _PROFILES_CACHE_TS
    _PROFILES_CACHE = None
    _PROFILES_CACHE_TS = 0.0


def _phrase_from_row(p: dict[str, Any]) -> Phrase:
    return Phrase(
        id=p.get("id"),
        kind=str(p.get("kind") or "plus"),
        phrase=str(p.get("phrase") or ""),
        weight=int(p.get("weight") or 3),
        query_only=bool(p.get("query_only")),
        local_only=bool(p.get("local_only")),
        query_text=p.get("query_text") or None,
        enabled=bool(p.get("enabled", True)),
        note=str(p.get("note") or ""),
    )


def _profile_from_row(r: dict[str, Any]) -> Profile:
    phrases = [
        _phrase_from_row(p) for p in (r.get("phrases") or [])
        if p.get("enabled", True)
    ]
    return Profile(
        id=r.get("id"),
        name=str(r.get("name") or ""),
        description=str(r.get("description") or ""),
        enabled=bool(r.get("enabled", True)),
        is_default=bool(r.get("is_default")),
        priority=int(r.get("priority") or 100),
        min_score=int(r.get("min_score") or 3),
        okpd2_codes=list(r.get("okpd2_codes") or []),
        phrases=phrases,
        price_target_lo=r.get("price_target_lo"),
        price_target_hi=r.get("price_target_hi"),
        price_hard_max=r.get("price_hard_max"),
    )


def _default_profiles_as_objects() -> list[Profile]:
    result = []
    for d in DEFAULT_PROFILES:
        if not d.get("enabled", True):
            continue
        phrases = [
            Phrase(
                id=None, kind=ph["kind"], phrase=ph["phrase"],
                weight=int(ph.get("weight", 3)),
                query_only=bool(ph.get("query_only", False)),
                local_only=bool(ph.get("local_only", False)),
                query_text=ph.get("query_text"),
                enabled=True, note=str(ph.get("note", "")),
            )
            for ph in d["phrases"]
        ]
        result.append(Profile(
            id=None, name=d["name"], description=d.get("description", ""),
            enabled=True, is_default=bool(d.get("is_default", False)),
            priority=int(d.get("priority", 100)), min_score=int(d.get("min_score", 3)),
            okpd2_codes=list(d.get("okpd2_codes", [])), phrases=phrases,
            price_target_lo=d.get("price_target_lo"),
            price_target_hi=d.get("price_target_hi"),
            price_hard_max=d.get("price_hard_max"),
        ))
    return result


def load_profiles(force: bool = False) -> list[Profile]:
    """Активные (enabled) профили, отсортированные по priority.

    Источник — database.search_profiles_active(); при недоступной БД или
    отсутствии включённых профилей — откат на DEFAULT_PROFILES (та же логика,
    что у filter_engine._load_rules() → DEFAULT_BUCKETS).
    """
    global _PROFILES_CACHE, _PROFILES_CACHE_TS
    now = time.monotonic()
    if not force and _PROFILES_CACHE is not None and (now - _PROFILES_CACHE_TS) < _CACHE_TTL:
        return _PROFILES_CACHE

    profiles: list[Profile] = []
    try:
        import database as db
        rows = db.search_profiles_active()
        profiles = [_profile_from_row(r) for r in rows]
    except Exception:
        profiles = []

    if not profiles:
        profiles = _default_profiles_as_objects()

    profiles.sort(key=lambda p: p.priority)
    _PROFILES_CACHE = profiles
    _PROFILES_CACHE_TS = now
    return profiles


# ══════════════════════════════════════════════════════════════════════════════
# Скоринг
# ══════════════════════════════════════════════════════════════════════════════

def match_profile(profile: Profile, tokens: list[tuple[int, str]]) -> MatchResult:
    total = 0
    hits: list[tuple[str, int]] = []
    hard_hit: str | None = None
    for ph in profile.phrases:
        if ph.query_only or not ph.words:
            continue
        if not phrase_hits(tokens, ph.words, ph.window):
            continue
        if ph.kind == "minus_hard":
            if hard_hit is None:
                hard_hit = ph.phrase
            continue
        total += ph.signed_weight
        hits.append((ph.phrase, ph.signed_weight))
    accepted = hard_hit is None and total >= profile.min_score
    return MatchResult(
        profile_id=profile.id, profile_name=profile.name,
        accepted=accepted, score=total, hits=hits, hard_hit=hard_hit,
        min_score=profile.min_score,
        price_target_lo=profile.price_target_lo,
        price_target_hi=profile.price_target_hi,
        price_hard_max=profile.price_hard_max,
    )


def score_tender(tender: dict[str, Any], profiles: list[Profile] | None = None) -> list[MatchResult]:
    """Прогоняет карточку тендера через все активные профили."""
    text = build_tender_text(tender)
    tokens = tokenize(text)
    return [match_profile(p, tokens) for p in (profiles if profiles is not None else load_profiles())]


def best_match(results: list[MatchResult]) -> MatchResult | None:
    accepted = [r for r in results if r.accepted]
    if not accepted:
        return None
    return max(accepted, key=lambda r: r.score)


def filter_and_tag(
    tenders: list[dict[str, Any]],
    profiles: list[Profile] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Прогоняет карточки через профили и помечает принятые.

    Принятая карточка получает profile_id/profile_score/matched_profiles
    (профиль с лучшим скором). Непринятая отбрасывается — это и есть
    локальный отсев мусора (см. docs/tz-search-profiles.md, §7.1).
    Используется и в main.run_stage1, и в web_app.SearchRunner — единая
    точка правды для пост-фильтра. Tenderplan через неё не проходит.
    """
    active_profiles = profiles if profiles is not None else load_profiles()
    kept: list[dict[str, Any]] = []
    dropped = 0
    for tender in tenders:
        results = score_tender(tender, profiles=active_profiles)
        best = best_match(results)
        if best is None:
            dropped += 1
            continue
        tender["profile_id"] = best.profile_id
        tender["profile_score"] = best.score
        tender["matched_profiles"] = [r.to_dict() for r in results if r.accepted]
        tender["profile_price_lo"] = best.price_target_lo
        tender["profile_price_hi"] = best.price_target_hi
        tender["profile_price_hard_max"] = best.price_hard_max
        kept.append(tender)
    return kept, dropped


# ══════════════════════════════════════════════════════════════════════════════
# Генерация запросов к внешним источникам
# ══════════════════════════════════════════════════════════════════════════════

def _auto_query_text(words: list[str]) -> str:
    """Черновой откат, если у фразы нет ручного query_text: первый вариант
    из "|", маска "*" срезается. Годится для однословных масок/акронимов;
    для многословных стеммов лучше задавать query_text вручную (см. сиды)."""
    parts = []
    for w in words:
        w = w.split("|")[0]
        w = w[:-1] if w.endswith("*") else w
        if w:
            parts.append(w)
    return " ".join(parts)


def eis_queries(profile: Profile) -> list[str]:
    """Плюс-фразы профиля → поисковые запросы ЕИС (без масок/операторов)."""
    out: list[str] = []
    for ph in profile.phrases:
        if ph.kind != "plus" or not ph.enabled or ph.local_only:
            continue
        needs_manual = "~" in ph.phrase or "|" in ph.phrase
        query = ph.query_text or ("" if needs_manual else _auto_query_text(ph.words))
        query = (query or "").strip()
        if query and query not in out:
            out.append(query)
    return out


def storefront_queries(profile: Profile, max_words: int = 2) -> list[str]:
    """Короткие точные фразы для витрин с полнотекстовым поиском (СПб, ЕАТ, mos...)."""
    return [q for q in eis_queries(profile) if len(q.split()) <= max_words]


# ══════════════════════════════════════════════════════════════════════════════
# Дефолтные сид-профили (см. docs/tz-search-profiles.md, §5)
# ══════════════════════════════════════════════════════════════════════════════

def _p(phrase: str, weight: int = 3, query_text: str | None = None,
       note: str = "", local_only: bool = False, query_only: bool = False) -> dict[str, Any]:
    return {"kind": "plus", "phrase": phrase, "weight": weight, "query_text": query_text,
            "note": note, "local_only": local_only, "query_only": query_only}


def _mh(phrase: str, note: str = "") -> dict[str, Any]:
    return {"kind": "minus_hard", "phrase": phrase, "weight": 0, "note": note}


def _ms(phrase: str, weight: int = 3, note: str = "") -> dict[str, Any]:
    return {"kind": "minus_soft", "phrase": phrase, "weight": weight, "note": note}


# Общий минус-блок «опасные, но полезные слова» — только мягкий штраф, не отброс
# (см. разбор: поставка/ремонт/стройка НЕ должны быть hard-исключением).
_RESALE_CONSTRUCTION_SOFT = [
    _ms("(поставк* оборудован*)~2", weight=4, note="поставка железа без работ — штраф, не отброс"),
    _ms("(приобретен* лиценз*)~2", weight=5, note="чистая покупка лицензий"),
    _ms("(строительн* работ*)~1", weight=4, note="стройка — штраф, вдруг это ИС стройконтроля"),
    _ms("(капитальн* ремонт*)~1", weight=4, note="капремонт — штраф, вдруг это система учёта ремонтов"),
    _ms("проектно-сметн*", weight=4, note="ПСД/строительное проектирование"),
    _ms("ПСД", weight=3),
    _ms("ПИР", weight=3),
]

DEFAULT_PROFILES: list[dict[str, Any]] = [
    {
        "name": "Сайты и веб-разработка",
        "description": "Заказные сайты, веб-порталы, личные кабинеты, мобильные приложения",
        "enabled": True, "is_default": True, "priority": 10, "min_score": 3,
        "okpd2_codes": [],
        "phrases": [
            _p("разработк* сайт*", query_text="разработка сайта"),
            _p("создан* сайт*", query_text="создание сайта"),
            _p("модернизац* сайт*", query_text="модернизация сайта"),
            _p("доработк* сайт*", query_text="доработка сайта"),
            _p("развити* сайт*", query_text="развитие сайта"),
            _p("обновлен* сайт*", query_text="обновление сайта"),
            _p("редизайн* сайт*", query_text="редизайн сайта"),
            _p("реконструкц* сайт*", query_text="реконструкция сайта"),
            _p("разработк* веб-сайт*|web-сайт*", query_text="разработка веб-сайта"),
            _p("техническ* поддержк* сайт*", weight=2, query_text="техническая поддержка сайта"),
            _p("создан* интернет-портал*", query_text="создание интернет-портала"),
            _p("разработк* интернет-портал*", query_text="разработка интернет-портала"),
            _p("разработк* веб-портал*|web-портал*", query_text="разработка веб-портала"),
            _p("разработк* веб-приложен*|web-приложен*", query_text="разработка веб-приложения"),
            _p("разработк* личн* кабинет*", query_text="разработка личного кабинета"),
            _p("создан* личн* кабинет*", query_text="создание личного кабинета"),
            _p("разработк* интернет-магазин*", query_text="разработка интернет-магазина"),
            _p("создан* интернет-магазин*", query_text="создание интернет-магазина"),
            _p("разработк* мобильн* приложен*", query_text="разработка мобильного приложения"),
            _p("доработк* мобильн* приложен*", query_text="доработка мобильного приложения"),
            _p("разработк* электрон* сервис*", query_text="разработка электронного сервиса"),
            _p("создан* посадочн* страниц*", query_text="создание посадочной страницы"),
            _p("1С-Битрикс", weight=4, query_text="1С-Битрикс"),
            _p("битрикс|bitrix", query_text="битрикс"),
            _mh("(образовательн* программ*)~1", note="образовательная программа — не наш профиль"),
            _mh("(приобретен* лиценз*)~2", note="чистая покупка лицензий"),
            _mh("(приобретен* неисключительн* прав*)~3", note="покупка прав на ПО без работ"),
            _ms("поставк*", weight=3),
            _ms("сопровожден*", weight=2, note="чистое сопровождение без разработки — см. профиль П4"),
            _ms("(строительн* работ*)~1", weight=5),
            _ms("(капитальн* ремонт*)~1", weight=5),
            _ms("лиценз*", weight=3),
        ],
    },
    {
        "name": "Разработка ПО и информационных систем",
        "description": "Заказная разработка/доработка ПО, информационных систем, АИС/ГИС",
        "enabled": True, "is_default": False, "priority": 20, "min_score": 3,
        "okpd2_codes": ["62.01", "62.02", "62.02.1", "62.09", "63.11"],
        "phrases": [
            _p("услуг* программирован*", query_text="услуги программирования"),
            _p("разработк* программн* обеспечен*", query_text="разработка программного обеспечения"),
            _p("создан* программн* обеспечен*", query_text="создание программного обеспечения"),
            _p("доработк* программн* обеспечен*", query_text="доработка программного обеспечения"),
            _p("модернизац* программн* обеспечен*", query_text="модернизация программного обеспечения"),
            _p("развити* программн* обеспечен*", query_text="развитие программного обеспечения"),
            _p("разработк* программн* продукт*", query_text="разработка программного продукта"),
            _p("доработк* программн* продукт*", query_text="доработка программного продукта"),
            _p("разработк* прикладн* программ*", query_text="разработка прикладной программы",
               note="безопасный вариант разработк*+программ* — только рядом с 'прикладн*'"),
            _p("разработк* программн* комплекс*", query_text="разработка программного комплекса"),
            _p("разработк* информацион* систем*", query_text="разработка информационной системы"),
            _p("создан* информацион* систем*", query_text="создание информационной системы"),
            _p("проектирован* информацион* систем*", query_text="проектирование информационной системы"),
            _p("внедрен* информацион* систем*", query_text="внедрение информационной системы"),
            _p("доработк* информацион* систем*", query_text="доработка информационной системы"),
            _p("модернизац* информацион* систем*", query_text="модернизация информационной системы"),
            _p("развити* информацион* систем*", query_text="развитие информационной системы"),
            _p("модификац* информацион* систем*", query_text="модификация информационной системы"),
            _p("разработк* автоматизирован* систем*", query_text="разработка автоматизированной системы"),
            _p("создан* автоматизирован* систем*", query_text="создание автоматизированной системы"),
            _p("доработк* автоматизирован* систем*", query_text="доработка автоматизированной системы"),
            _p("модернизац* автоматизирован* систем*", query_text="модернизация автоматизированной системы"),
            _p("развити* автоматизирован* систем*", query_text="развитие автоматизированной системы"),
            _p("разработк* АИС", query_text="разработка АИС"),
            _p("доработк* АИС", query_text="доработка АИС"),
            _p("модернизац* АИС", query_text="модернизация АИС"),
            _p("развити* АИС", query_text="развитие АИС"),
            _p("разработк* ГИС", query_text="разработка ГИС"),
            _p("доработк* ГИС", query_text="доработка ГИС"),
            _p("модернизац* ГИС", query_text="модернизация ГИС"),
            _p("развити* ГИС", query_text="развитие ГИС"),
            _p("программн* модул*", query_text="программный модуль",
               note="только квалифицированный вариант, не голое 'модуль'"),
            _p("модул* информацион* систем*", query_text="модуль информационной системы"),
            _p("разработк* цифров* платформ*", query_text="разработка цифровой платформы"),
            _p("разработк* информацион* платформ*", query_text="разработка информационной платформы"),
            _p("разработк* программн* платформ*", query_text="разработка программной платформы"),
            _p("модернизац* цифров* платформ*", query_text="модернизация цифровой платформы"),
            _p("доработк* функционал* информацион* систем*", weight=2,
               query_text="доработка функционала информационной системы"),
            _p("разработк* ИТ-систем*|IT-систем*", query_text="разработка ИТ-систем"),
            _p("разработк* ИТ-решен*|IT-решен*", query_text="разработка ИТ-решений"),
            _p("проектирован* баз* данн*", query_text="проектирование базы данных"),
            _p("разработк* баз* данн*", query_text="разработка базы данных"),
            _p("модернизац* баз* данн*", query_text="модернизация базы данных"),
            _mh("(государственн* программ*)~1"),
            _mh("(муниципальн* программ*)~1"),
            _mh("(образовательн* программ*)~1"),
            _mh("(телевизионн* программ*)~1"),
            _mh("(программ* газификац*)~1"),
            _mh("(программ* энергосбережен*)~1"),
            _mh("в рамках подпрограммы"),
            *_RESALE_CONSTRUCTION_SOFT,
        ],
    },
    {
        "name": "Интеграции и автоматизация",
        "description": "Интеграция систем, API, обмен данными, автоматизация процессов",
        "enabled": True, "is_default": False, "priority": 30, "min_score": 3,
        "okpd2_codes": [],
        "phrases": [
            _p("разработк* интеграцион* решен*", query_text="разработка интеграционного решения"),
            _p("создан* интеграцион* решен*", query_text="создание интеграционного решения"),
            _p("интеграц* информацион* систем*", query_text="интеграция информационных систем"),
            _p("интеграц* программн* обеспечен*", query_text="интеграция программного обеспечения"),
            _p("интеграц* баз* данн*", query_text="интеграция баз данных"),
            _p("миграц* баз* данн*", query_text="миграция базы данных"),
            _p("интеграц* АИС", query_text="интеграция АИС"),
            _p("интеграц* ГИС", query_text="интеграция ГИС"),
            _p("разработк* API", query_text="разработка API"),
            _p("интеграц* API", query_text="интеграция API"),
            _p("разработк* веб-сервис*|web-сервис*", query_text="разработка веб-сервиса"),
            _p("автоматизац* бизнес-процесс*", query_text="автоматизация бизнес-процессов"),
            _p("автоматизац* процесс*", weight=2, query_text="автоматизация процессов"),
            _p("интеграц* 1С", weight=4, query_text="интеграция 1С"),
            _p("интеграц* с 1С", weight=4, query_text="интеграция с 1С"),
            _p("обмен* данн* 1С", query_text="обмен данными 1С",
               note="токенный матчинг не даёт ложных 'теплообменник' — можно смело включать"),
            _p("интеграц* CRM", query_text="интеграция CRM"),
            _p("интеграц* ERP", query_text="интеграция ERP"),
            _p("обмен* данн* информацион* систем*", query_text="обмен данными информационных систем"),
            _mh("сенсорн* интеграц*", note="сенсорная интеграция — реабилитация, не ИТ"),
            *_RESALE_CONSTRUCTION_SOFT,
        ],
    },
    {
        "name": "Сопровождение и техподдержка",
        "description": "Сопровождение и техподдержка без явной доработки — смотреть по желанию",
        "enabled": False, "is_default": False, "priority": 40, "min_score": 3,
        "okpd2_codes": [],
        "phrases": [
            _p("сопровождени* информацион* систем*", query_text="сопровождение информационной системы"),
            _p("техническ* поддержк* информацион* систем*", query_text="техническая поддержка информационной системы"),
            _p("сопровождени* программн* обеспечен*", query_text="сопровождение программного обеспечения"),
            _p("техническ* поддержк* программн* обеспечен*", query_text="техническая поддержка программного обеспечения"),
            _p("эксплуатац* информацион* систем*", query_text="эксплуатация информационной системы"),
            _p("администрирован* информацион* систем*", query_text="администрирование информационной системы"),
            _p("сопровождени* сайт*", weight=3, query_text="сопровождение сайта"),
            _p("администрирован* сайт*", query_text="администрирование сайта"),
            _p("администрирован* сервер*", query_text="администрирование сервера"),
            _p("техническ* сопровожден* информацион* систем*", query_text="техническое сопровождение информационной системы"),
            _p("резервн* копирован*", weight=2, query_text="резервное копирование"),
            # Бонус «внутри поддержки есть доработка» — только локальный скоринг.
            _p("доработк*", weight=2, local_only=True),
            _p("модернизац*", weight=2, local_only=True),
            _p("развити*", weight=2, local_only=True),
            _p("адаптац*", weight=2, local_only=True),
            _p("модификац*", weight=2, local_only=True),
            _ms("круглосуточн*", weight=4),
            _ms("24/7", weight=4),
            _ms("выезд* специалист*", weight=3),
        ],
    },
    {
        "name": "Маркетинг: SEO/SMM/контент",
        "description": "Продвижение сайтов и контент — другая экономика исполнения",
        "enabled": False, "is_default": False, "priority": 50, "min_score": 3,
        "okpd2_codes": [],
        "phrases": [
            _p("SEO-продвижен*", query_text="SEO-продвижение"),
            _p("продвижен* сайт*", query_text="продвижение сайта"),
            _p("SMM", query_text="SMM"),
            _p("контентн* сопровожден*", query_text="контентное сопровождение"),
            _p("наполнен* сайт*", query_text="наполнение сайта"),
            _p("размещен* информац* сайт*", weight=1, query_text="размещение информации на сайте"),
        ],
    },
    {
        # Фразы этого профиля заполняются при сидировании из config.SEARCH_KEYWORDS
        # (см. database.search_profiles_seed) — чтобы не потерять текущий охват
        # при переходе на профили. Пользователь разбирает вручную и удаляет профиль.
        "name": "Импорт: текущий список",
        "description": "Автоимпорт прежнего SEARCH_KEYWORDS при миграции — разобрать и удалить",
        "enabled": False, "is_default": False, "priority": 90, "min_score": 1,
        "okpd2_codes": [],
        "phrases": [],
    },
]
