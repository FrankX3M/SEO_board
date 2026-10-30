"""Внешние источники (Google, Яндекс Вебмастер, key.so, PageSpeed).

Правило: нет ключа — проверка получает статус «пропущено», скрипт не падает.
Любая ошибка внешнего сервиса тоже не роняет прогон: проверка получает статус «ошибка».
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .config import INTEGRATION_VARS, Settings


class Skipped(Exception):
    """Проверку нельзя выполнить (нет ключа/доступа). Не ошибка."""


@dataclass
class Outcome:
    status: str  # ok | skipped | error
    value: Any = None
    reason: str = ""


def available(settings: Settings, name: str) -> bool:
    return bool(settings.integrations.get(name))


def require(settings: Settings, name: str) -> str:
    """Возвращает ключ или бросает Skipped."""
    if name not in INTEGRATION_VARS:
        raise KeyError(f"Неизвестная интеграция '{name}'")
    val = settings.integrations.get(name)
    if not val:
        var, label = INTEGRATION_VARS[name]
        raise Skipped(f"не задан {var} — {label}")
    return val


def guarded(settings: Settings, name: str, fn: Callable[[str], Any]) -> Outcome:
    """Выполняет fn(ключ) так, чтобы ни отсутствие ключа, ни сбой сервиса не роняли скрипт."""
    try:
        key = require(settings, name)
    except Skipped as e:
        return Outcome("skipped", reason=str(e))
    try:
        return Outcome("ok", value=fn(key))
    except Skipped as e:
        return Outcome("skipped", reason=str(e))
    except Exception as e:  # noqa: BLE001 — внешний сервис не должен ронять прогон
        return Outcome("error", reason=f"{type(e).__name__}: {str(e)[:200]}")


def status_lines(settings: Settings) -> list[str]:
    out = []
    for name, (var, label) in INTEGRATION_VARS.items():
        mark = "есть ключ" if available(settings, name) else f"нет {var} — проверки будут пропущены"
        out.append(f"{label}: {mark}")
    return out
