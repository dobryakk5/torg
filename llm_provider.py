"""llm_provider.py — единый шлюз к LLM (OpenRouter по умолчанию, либо Anthropic).

Зачем отдельный модуль: и быстрый триаж (Stage 1), и детальный разбор (Stage 2)
ходят к модели через одну функцию `complete()`, а провайдер/модель выбираются
настройками (config.LLM_PROVIDER + слаги моделей). OpenRouter — OpenAI-совместимый
HTTP API, поэтому ходим обычным requests, без лишних SDK.

Для OpenRouter `complete()` дополнительно:
  - ретраит 429/5xx с backoff (уважая Retry-After);
  - при отказе основной модели пробует по очереди резервные
    (config.OPENROUTER_FALLBACK_MODELS) — актуально для бесплатных (:free)
    моделей с плавающими лимитами;
  - если модель не поддерживает response_format=json_object (HTTP 400),
    повторяет запрос без него;
  - config.LLM_DAILY_REQUEST_LIMIT — МЯГКИЙ ориентир (лог-предупреждение после
    превышения, не блокирует запросы); жёсткая остановка на сегодня — только
    при реальном отказе провайдера (429 после исчерпания ретраев), общая для
    всех процессов через settings (database.set_llm_rate_limited/is_llm_rate_limited).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

# OpenRouter edge/WAF may temporarily return 403 "Access denied by security
# policy" for a burst of otherwise valid requests. Retry the same model with
# backoff before switching models; immediate fallback only creates another
# burst and tends to receive the same temporary block.
_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
_RETRY_BACKOFF_SECONDS = (5, 20, 60)

# Модели, ответившие 400 на response_format=json_object — больше не шлём им
# этот параметр (иначе каждый вызов тратит лишний запрос из дневного лимита).
_JSON_MODE_UNSUPPORTED: set[str] = set()


def provider() -> str:
    return (getattr(config, "LLM_PROVIDER", "openrouter") or "openrouter").strip().lower()


def is_configured() -> bool:
    """True, если у выбранного провайдера есть ключ — иначе LLM-шаги пропускаются.

    Ключи живут ТОЛЬКО в .env (в /control их нет — там лишь выбор провайдера/моделей).
    Долгоживущий web-процесс мог стартовать раньше, чем ключ появился в .env, поэтому
    перечитываем секреты здесь — так все вызывающие эндпоинты видят актуальный ключ.
    """
    try:
        config.reload_llm_secrets()
    except Exception:
        pass
    if provider() == "anthropic":
        return bool(config.ANTHROPIC_API_KEY)
    return bool(getattr(config, "OPENROUTER_API_KEY", ""))


def triage_model() -> str:
    if provider() == "anthropic":
        return config.CLAUDE_MODEL
    return getattr(config, "OPENROUTER_TRIAGE_MODEL", "openai/gpt-4o-mini")


def deep_model() -> str:
    if provider() == "anthropic":
        return config.CLAUDE_MODEL
    return getattr(config, "OPENROUTER_DEEP_MODEL", "openai/gpt-4o")


def _fallback_models() -> list[str]:
    """Резервные модели OpenRouter из настроек (runtime → env → default)."""
    if provider() == "anthropic":
        return []
    try:
        models = config.get_runtime("OPENROUTER_FALLBACK_MODELS", config.OPENROUTER_FALLBACK_MODELS)
    except Exception:
        models = config.OPENROUTER_FALLBACK_MODELS
    return [str(m).strip() for m in (models or []) if str(m).strip()]


def _daily_limit() -> int:
    try:
        return int(config.get_runtime("LLM_DAILY_REQUEST_LIMIT", config.LLM_DAILY_REQUEST_LIMIT))
    except Exception:
        return config.LLM_DAILY_REQUEST_LIMIT


def _daily_key() -> str:
    return date.today().isoformat()


def daily_limit_reached() -> bool:
    """True, если наш ОРИЕНТИРОВОЧНЫЙ счётчик запросов перевалил за настроенный
    порог (0/отрицательный = без лимита). Это МЯГКИЙ сигнал: сам по себе он
    больше не блокирует запросы (см. complete()) — только логируется. Жёсткая
    остановка — только по факту реального отказа провайдера, см.
    _rate_limited_today().
    """
    limit = _daily_limit()
    if limit <= 0:
        return False
    try:
        from database import get_llm_daily_requests
        return get_llm_daily_requests(_daily_key()) >= limit
    except Exception:
        # БД недоступна — не блокируем LLM-шаги из-за отказа самого лимитера.
        return False


def _bump_daily_count() -> int:
    try:
        from database import increment_llm_daily_requests
        return increment_llm_daily_requests(_daily_key())
    except Exception:
        return 0


def _rate_limited_today(model: str) -> bool:
    """True, если ИМЕННО ЭТА модель сегодня уже реально отказала по лимиту
    (429, ретраи исчерпаны) хотя бы раз. Флаг per-модельный, а не общий на
    день: у бесплатных моделей на OpenRouter лимиты «плавающие» и независимые
    друг от друга — 429 у основной модели не означает, что резервные тоже
    исчерпаны, а фолбэки для того и настроены, чтобы обходить именно такие
    отказы конкретной модели.
    """
    try:
        from database import is_llm_rate_limited
        return is_llm_rate_limited(_daily_key(), model)
    except Exception:
        return False


def _mark_rate_limited_today(model: str) -> None:
    try:
        from database import set_llm_rate_limited
        set_llm_rate_limited(_daily_key(), model)
    except Exception:
        pass


# Лог о превышении мягкого порога — не чаще раза в процесс на дату, иначе на
# очереди из полусотни лотов одно и то же предупреждение засоряет журнал.
_soft_limit_warned_date: str | None = None


def _warn_soft_limit_once() -> None:
    global _soft_limit_warned_date
    today = _daily_key()
    if _soft_limit_warned_date == today:
        return
    _soft_limit_warned_date = today
    logger.warning(
        "LLM: превышен мягкий суточный ориентир (%d запросов) — продолжаю пробовать, "
        "остановлюсь только при реальном отказе провайдера (429)", _daily_limit(),
    )


def complete(
    system: str,
    user: str,
    model: str,
    max_tokens: int = 1200,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> Optional[str]:
    """Запрос к LLM. Возвращает текст ответа или None при ошибке/без ключа/лимите.

    Для OpenRouter при отказе `model` (после внутренних ретраев на 429/5xx)
    пробует по очереди модели из OPENROUTER_FALLBACK_MODELS.

    Дневной лимит (config.LLM_DAILY_REQUEST_LIMIT) — МЯГКИЙ ориентир: после
    него запросы не блокируются, а продолжаются (лишь предупреждение в лог).
    Жёсткая остановка на сегодня — реальный отказ провайдера (429, после
    исчерпания ретраев), причём ПЕР-МОДЕЛЬНО: если основная модель исчерпала
    лимит, фолбэки всё равно пробуются (для этого они и настроены — у
    бесплатных моделей независимые «плавающие» лимиты). Совсем не отвечаем,
    только если СЕГОДНЯ уже отказали по лимиту вообще все модели из списка.
    """
    if not is_configured():
        logger.info("LLM не сконфигурирован (нет ключа для провайдера %s) — шаг пропущен", provider())
        return None

    if daily_limit_reached():
        _warn_soft_limit_once()

    models_to_try = [model] + (
        [m for m in _fallback_models() if m != model] if provider() != "anthropic" else []
    )

    for idx, candidate_model in enumerate(models_to_try):
        if _rate_limited_today(candidate_model):
            logger.info("LLM: %s уже отказала по лимиту сегодня — пропускаю без сети", candidate_model)
            continue
        try:
            if provider() == "anthropic":
                text = _complete_anthropic(system, user, candidate_model, max_tokens, temperature)
            else:
                text = _complete_openrouter(system, user, candidate_model, max_tokens, temperature, json_mode)
        except Exception as exc:
            logger.error("Ошибка LLM (%s, %s): %s", provider(), candidate_model, exc)
            if getattr(exc, "status_code", None) == 429:
                # anthropic SDK бросает APIStatusError с .status_code вместо HTTP-ответа,
                # который можно было бы разобрать, как у OpenRouter (_complete_openrouter).
                _mark_rate_limited_today(candidate_model)
            text = None
        if text:
            if idx > 0:
                logger.info("LLM: ответила резервная модель %s (после %d неудачных)", candidate_model, idx)
            return text
        if idx < len(models_to_try) - 1:
            logger.warning("LLM: модель %s не дала результата, переключаюсь на резервную", candidate_model)
    return None


def _complete_openrouter(
    system: str, user: str, model: str, max_tokens: int, temperature: float, json_mode: bool
) -> Optional[str]:
    """Один вызов OpenRouter для конкретной модели: ретраи 429/5xx + фолбэк без json_mode."""
    use_json_mode = json_mode and model not in _JSON_MODE_UNSUPPORTED
    tokens_boosted = False
    max_attempts = len(_RETRY_BACKOFF_SECONDS) + 1

    for attempt in range(1, max_attempts + 1):
        _bump_daily_count()
        try:
            resp = _openrouter_post(system, user, model, max_tokens, temperature, use_json_mode)
        except requests.RequestException as exc:
            logger.warning("OpenRouter %s: сетевая ошибка (%s), попытка %d/%d", model, exc, attempt, max_attempts)
            if attempt < max_attempts:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
                continue
            return None

        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                logger.error("OpenRouter %s: пустой ответ: %s", model, str(data)[:300])
                return None
            message = choices[0].get("message") or {}
            # .get("content", "") НЕ подставляет дефолт, если ключ есть, но равен null
            # (reasoning-модели отдают content:null вместе с reasoning-полем) — отсюда `or ""`.
            content = (message.get("content") or "").strip()
            if content:
                return content
            # Reasoning-модель (hy3 и т.п.) съела весь max_tokens на размышление и
            # не дошла до ответа: finish=length + непустой reasoning. Одна повторная
            # попытка с увеличенным бюджетом (параметр reasoning:{enabled:false}
            # такие модели игнорируют — проверено на tencent/hy3:free).
            reasoning = (message.get("reasoning") or "").strip()
            if (
                not tokens_boosted
                and reasoning
                and choices[0].get("finish_reason") == "length"
                and attempt < max_attempts
            ):
                tokens_boosted = True
                max_tokens = min(max_tokens * 4, 8000)
                logger.info(
                    "OpenRouter %s: reasoning не уложился в лимит токенов, повторяю с max_tokens=%d",
                    model, max_tokens,
                )
                continue
            return None

        if resp.status_code == 400 and use_json_mode:
            logger.info("OpenRouter %s: HTTP 400 с response_format=json_object, повторяю без него", model)
            _JSON_MODE_UNSUPPORTED.add(model)
            use_json_mode = False
            continue

        if resp.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
            wait = _RETRY_BACKOFF_SECONDS[attempt - 1]
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            logger.warning(
                "OpenRouter %s: HTTP %s, повтор через %.0fс (попытка %d/%d)",
                model, resp.status_code, wait, attempt, max_attempts,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            # Ретраи исчерпаны, провайдер всё ещё отвечает 429 — это и есть
            # РЕАЛЬНЫЙ отказ по лимиту (в отличие от нашей грубой оценки
            # daily_limit_reached()). Фиксируем на весь день.
            logger.error("OpenRouter %s: HTTP 429 после %d попыток — реальный отказ по лимиту", model, max_attempts)
            _mark_rate_limited_today(model)
        else:
            logger.error("OpenRouter %s: HTTP %s: %s", model, resp.status_code, resp.text[:300])
        return None

    return None


def _openrouter_post(
    system: str, user: str, model: str, max_tokens: int, temperature: float, json_mode: bool
) -> requests.Response:
    base = getattr(config, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return requests.post(
        f"{base}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # OpenRouter рекомендует слать атрибуцию приложения (необязательно).
            "X-Title": "torg-tender-monitor",
        },
        data=json.dumps(payload),
        timeout=getattr(config, "LLM_HTTP_TIMEOUT", 60),
    )


def _complete_anthropic(
    system: str, user: str, model: str, max_tokens: int, temperature: float
) -> Optional[str]:
    try:
        import anthropic
    except ImportError:
        logger.error("Установи: pip install anthropic (или переключи LLM_PROVIDER=openrouter)")
        return None
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text.strip()


def parse_json(text: Optional[str]) -> Optional[dict]:
    """Достаёт JSON-объект из ответа модели (терпит ```json-обёртки и мусор по краям)."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(s[start : end + 1])
    except Exception:
        return None
