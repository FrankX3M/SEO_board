"""Site request headers; stored values are never included in run descriptions."""
import json
import re


def parse_headers(raw, previous=()):
    try:
        rows = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        raise ValueError("HTTP-заголовки: некорректный список") from None
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("HTTP-заголовки: нужен список, максимум 100 строк")
    old = {r["name"].lower(): r["value"] for r in previous}
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("HTTP-заголовки: некорректная строка")
        name, value = row.get("name", ""), row.get("value", "")
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("HTTP-заголовки: имя и значение должны быть строками")
        name = name.strip()
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("HTTP-заголовки: недопустимое имя")
        if name.lower() in seen:
            raise ValueError(f"HTTP-заголовок {name} указан дважды")
        seen.add(name.lower())
        if not value and row.get("keep"):
            value = old.get(name.lower(), "")
        if any(ord(c) < 32 and c != "\t" or ord(c) > 126 for c in value):
            raise ValueError(f"HTTP-заголовок {name}: используйте одну строку ASCII без управляющих символов")
        result.append(dict(name=name, value=value, enabled=row.get("enabled", True) is True))
    return result


def active_headers(rows):
    return {r["name"]: r["value"] for r in rows if r["enabled"]}


def merge_headers(*groups):
    result = {}
    for group in groups:
        for name, value in group.items():
            result = {k: v for k, v in result.items() if k.lower() != name.lower()}
            result[name] = value
    return result
