"""Мелкие сетевые утилиты без зависимостей."""
from __future__ import annotations

from urllib.parse import urlsplit


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def same_site(host: str | None, base_host: str) -> bool:
    """Тот же сайт: совпадает хост (с точностью до www) или это его поддомен.

    Токен и куку отправляем ТОЛЬКО на такие хосты, чтобы не утекали в
    счётчики, CDN и прочие сторонние запросы.
    """
    if not host:
        return False
    h, b = _strip_www(host.lower()), _strip_www(base_host.lower())
    return h == b or h.endswith("." + b)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
