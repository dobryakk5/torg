"""llm_provider.py — единый шлюз к LLM (OpenRouter по умолчанию, либо Anthropic).

Зачем отдельный модуль: и быстрый триаж (Stage 1), и детальный разбор (Stage 2)
ходят к модели через одну функцию `complete()`, а провайдер/модель выбираются
настройками (config.LLM_PROVIDER + слаги моделей). OpenRouter — OpenAI-совместимый
HTTP API, поэтому ходим обычным requests, без лишних SDK.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


def provider() -> str:
    return (getattr(config, "LLM_PROVIDER", "openrouter") or "openrouter").strip().lower()


def is_configured() -> bool:
    """True, если у выбранного провайдера есть ключ — иначе LLM-шаги пропускаются."""
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


def complete(
    system: str,
    user: str,
    model: str,
    max_tokens: int = 1200,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> Optional[str]:
    """Один запрос к LLM. Возвращает текст ответа или None при ошибке/без ключа."""
    if not is_configured():
        logger.info("LLM не сконфигурирован (нет ключа для провайдера %s) — шаг пропущен", provider())
        return None
    try:
        if provider() == "anthropic":
            return _complete_anthropic(system, user, model, max_tokens, temperature)
        return _complete_openrouter(system, user, model, max_tokens, temperature, json_mode)
    except Exception as exc:
        logger.error("Ошибка LLM (%s, %s): %s", provider(), model, exc)
        return None


def _complete_openrouter(
    system: str, user: str, model: str, max_tokens: int, temperature: float, json_mode: bool
) -> Optional[str]:
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
    resp = requests.post(
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
    if resp.status_code != 200:
        logger.error("OpenRouter %s: %s", resp.status_code, resp.text[:300])
        return None
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        logger.error("OpenRouter: пустой ответ: %s", str(data)[:300])
        return None
    return (choices[0].get("message") or {}).get("content", "").strip() or None


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
