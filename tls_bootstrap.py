"""Раннее подключение системного хранилища TLS-сертификатов.

На macOS pyenv/venv обычно использует OpenSSL и certifi, тогда как браузер и
системный curl используют Keychain. Для ЕИС это важно: отдельные backend-узлы
могут не отдавать полную цепочку промежуточных сертификатов.

Модуль нужно импортировать ДО requests/urllib3. Он безопасно включается один
раз на процесс. Управление: EIS_USE_SYSTEM_TRUSTSTORE=1/0 в окружении или .env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_TRUE_VALUES = {"1", "true", "yes", "on", "да"}
_FALSE_VALUES = {"0", "false", "no", "off", "нет", ""}


def _dotenv_value(key: str) -> str | None:
    """Читает один ключ из локального .env без импорта config.py."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return None
    try:
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _enabled() -> bool:
    raw = os.getenv("EIS_USE_SYSTEM_TRUSTSTORE")
    if raw is None:
        raw = _dotenv_value("EIS_USE_SYSTEM_TRUSTSTORE")
    if raw is None:
        return sys.platform == "darwin"

    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return sys.platform == "darwin"


def enable_native_truststore() -> bool:
    """Подключает нативное системное хранилище сертификатов один раз."""
    if os.getenv("EIS_NATIVE_TRUSTSTORE_ACTIVE") == "1":
        return True
    if not _enabled():
        return False

    try:
        import truststore

        truststore.inject_into_ssl()
    except (ImportError, OSError, RuntimeError) as exc:
        os.environ["EIS_NATIVE_TRUSTSTORE_ERROR"] = f"{type(exc).__name__}: {exc}"
        return False

    os.environ["EIS_NATIVE_TRUSTSTORE_ACTIVE"] = "1"
    os.environ.pop("EIS_NATIVE_TRUSTSTORE_ERROR", None)
    return True


NATIVE_TRUSTSTORE_ACTIVE = enable_native_truststore()
