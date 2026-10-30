"""Клиент OpenAI-совместимого API (корпоративный контур) с кешем и лимитом запросов.

Настройки только из env (общий .env проекта):
  AI_API_KEY        ключ (без него этап ИИ пропускается, см. integrations.guarded)
  AI_API_URL        базовый адрес, по умолчанию https://api-copilot.x5.ru/v1
  AI_MODEL          по умолчанию x5-airun-medium (русский язык, контекст 65k)
  AI_MAX_REQUESTS   лимит запросов на запуск проверок (по умолчанию 20)
  AI_TIMEOUT        сек. на ответ (по умолчанию 180)
  AI_CONCURRENCY    параллельных запросов (по умолчанию 2; лимит модели — 8)
Ответы кешируются в SQLite по хешу (модель + версия промпта + содержимое): повторный запуск не тратит запросы.
Токен сайта и заголовки ответов в запросы к модели не попадают.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

import httpx

from .storage import Store, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_cache (
  key TEXT PRIMARY KEY, model TEXT, response TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, network TEXT NOT NULL,
  kind TEXT NOT NULL,   -- analysis | descriptions
  data TEXT NOT NULL,   -- JSON
  model TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ai_notes ON ai_notes(run_id, network, kind);
"""


class AIError(Exception):
    pass


class AIClient:
    def __init__(self, store: Store, api_key: str, *, verify: bool = True):
        e = os.environ
        self.key = api_key
        self.base = (e.get("AI_API_URL") or "https://api-copilot.x5.ru/v1").rstrip("/")
        self.model = e.get("AI_MODEL") or "x5-airun-medium"
        self.max_requests = int(e.get("AI_MAX_REQUESTS") or 20)
        self.timeout = float(e.get("AI_TIMEOUT") or 180)
        self.requests_made = 0
        self.cache_hits = 0
        self.db = store.db
        self.db.executescript(SCHEMA)
        self.http = httpx.Client(timeout=self.timeout, verify=verify)

    def close(self) -> None:
        self.http.close()

    def _key(self, system: str, user: str, version: str) -> str:
        return hashlib.sha256("\x00".join((self.model, version, system, user)).encode()).hexdigest()

    def chat(self, system: str, user: str, *, version: str = "v1", max_tokens: int = 3000) -> str:
        key = self._key(system, user, version)
        row = self.db.execute("SELECT response FROM ai_cache WHERE key=?", (key,)).fetchone()
        if row:
            self.cache_hits += 1
            return row[0]
        if self.requests_made >= self.max_requests:
            raise AIError(f"достигнут лимит AI_MAX_REQUESTS={self.max_requests}")
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        last = ""
        for attempt in range(3):
            self.requests_made += 1 if attempt == 0 else 0
            try:
                r = self.http.post(f"{self.base}/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    try:
                        text = r.json()["choices"][0]["message"]["content"] or ""
                    except (KeyError, IndexError, ValueError) as e:
                        raise AIError(f"неожиданный формат ответа: {e}") from e
                    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                    self.db.execute("INSERT OR REPLACE INTO ai_cache VALUES (?,?,?,?)", (key, self.model, text, now_iso()))
                    self.db.commit()
                    return text
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(2 * (attempt + 1))
        raise AIError(last or "нет ответа")

    def chat_messages(self, system: str, messages: list[dict], *, max_tokens: int = 3500) -> str:
        """Диалог: список {role: user|assistant, content}. Без кеша (каждая реплика уникальна), считается в лимит запросов."""
        if self.requests_made >= self.max_requests:
            raise AIError(f"достигнут лимит AI_MAX_REQUESTS={self.max_requests}")
        body = {"model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, *messages]}
        self.requests_made += 1
        last = ""
        for attempt in range(3):
            try:
                r = self.http.post(f"{self.base}/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    try:
                        text = r.json()["choices"][0]["message"]["content"] or ""
                    except (KeyError, IndexError, ValueError) as e:
                        raise AIError(f"неожиданный формат ответа: {e}") from e
                    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(2 * (attempt + 1))
        raise AIError(last or "нет ответа")

    def chat_json(self, system: str, user: str, **kw) -> dict:
        text = self.chat(system, user, **kw)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise AIError("в ответе нет JSON")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise AIError(f"JSON не разобран: {e}") from e
