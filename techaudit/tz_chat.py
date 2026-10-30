"""Чат под ТЗ: сборка сообщений для модели и разбор ответа (чистые функции, без сети)."""
from __future__ import annotations

import json
import re

from .tz_prompt import TZ_CHAT_DATA

MAX_USER_CHARS = 4000     # длина одного сообщения пользователя
MAX_TZ_CHARS = 12000      # текст ТЗ из редактора, уходящий в модель
MAX_HISTORY = 16          # сколько последних реплик помним (без первого сообщения с данными)
MAX_STORED = 60           # сколько реплик храним в БД

_TZ_RE = re.compile(r"<TZ>(.*?)</TZ>", re.S | re.I)
_TZ_OPEN_RE = re.compile(r"<TZ>(.*)$", re.S | re.I)   # модель могла не закрыть тег (обрыв по лимиту токенов)


def parse_reply(text: str) -> tuple[str, str | None]:
    """(что сказать в чате, новый текст ТЗ или None). Если ТЗ внутри тегов нет — это просто ответ."""
    text = (text or "").strip()
    m = _TZ_RE.search(text)
    truncated = False
    if not m:
        m = _TZ_OPEN_RE.search(text)
        truncated = bool(m)
    if not m:
        return text, None
    tz = m.group(1).strip()
    say = (text[:m.start()] + " " + text[m.end():]).strip() if not truncated else text[:m.start()].strip()
    if not tz:
        return say or text, None
    if truncated:
        say = (say + "\n" if say else "") + "⚠ Ответ модели оборвался — ТЗ в редакторе не заменено. Попросите ещё раз или сократить."
        return say, None
    return say or "ТЗ обновлено.", tz


def build_messages(payload: dict, history: list[dict], tz_current: str, user_msg: str) -> list[dict]:
    """Первое сообщение — данные блока; затем последние реплики; в конце — актуальное ТЗ из редактора + вопрос."""
    msgs: list[dict] = [{"role": "user", "content": TZ_CHAT_DATA + json.dumps(payload, ensure_ascii=False)},
                        {"role": "assistant", "content": "Данные получил. Что нужно сделать с ТЗ?"}]
    for h in history[-MAX_HISTORY:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        content = str(h.get("content") or "")
        if role == "assistant" and h.get("tz"):
            content += "\n[Текст ТЗ в редакторе был обновлён по этому ответу]"
        if msgs[-1]["role"] == role:          # API ждёт чередование ролей
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})
    tz_current = (tz_current or "").strip()[:MAX_TZ_CHARS]
    tail = f"ТЕКУЩЕЕ ТЗ:\n{tz_current or '(пока пусто)'}\n\nСООБЩЕНИЕ:\n{user_msg[:MAX_USER_CHARS]}"
    if msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n\n" + tail
    else:
        msgs.append({"role": "user", "content": tail})
    return msgs


def clean_history(raw) -> list[dict]:
    out = []
    for h in raw or []:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            out.append({"role": h["role"], "content": h["content"], "tz": bool(h.get("tz")), "err": bool(h.get("err"))})
    return out[-MAX_STORED:]
