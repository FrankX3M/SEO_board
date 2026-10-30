"""Клиенты внешних сервисов: PageSpeed Insights и Google Search Console (URL Inspection, карты сайта).

Особенности:
  * несколько ключей/аккаунтов берутся по кругу; исчерпавший лимит (429 / quota) исключается до конца запуска;
  * результаты кешируются в БД (EXT_CACHE_HOURS, по умолчанию 24 ч) — повторный запуск не тратит квоту;
  * ни ключи, ни токены не попадают в вывод и БД (в сообщениях только номер ключа);
  * сбой сервиса не роняет прогон: исключение ExternalError превращается проверкой в статус «ошибка/пропущено».

Лимиты Google (проверьте актуальные в консоли): PageSpeed — на проект Google Cloud, поэтому ключи нужно брать из РАЗНЫХ
проектов; URL Inspection — на ресурс Search Console (≈2000 запросов/сутки), так что дополнительные аккаунты квоту
не увеличивают, а дополнительные РЕСУРСЫ (домен + префикс URL) — увеличивают.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .keys import gsc_accounts, gsc_properties, pagespeed_keys

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
GSC_INSPECT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
GSC_SITEMAPS = "https://www.googleapis.com/webmasters/v3/sites/{site}/sitemaps"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ext_cache (
  key TEXT PRIMARY KEY, kind TEXT, payload TEXT, fetched_at TEXT
);
"""


class ExternalError(Exception):
    pass


class Skipped(Exception):
    pass


def enabled() -> bool:
    """Внешние запросы расходуют квоту, поэтому включаются явно: --external или EXTERNAL_CHECKS=1."""
    return (os.environ.get("EXTERNAL_CHECKS") or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- кеш

def cache_get(db, kind: str, ident: str):
    ttl = float(os.environ.get("EXT_CACHE_HOURS") or 24)
    if ttl <= 0:
        return None
    db.executescript(SCHEMA)
    key = hashlib.sha1(f"{kind}|{ident}".encode()).hexdigest()
    r = db.execute("SELECT payload, fetched_at FROM ext_cache WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(r[1])).total_seconds() / 3600
    except ValueError:
        return None
    return json.loads(r[0]) if age <= ttl else None


def cache_put(db, kind: str, ident: str, payload) -> None:
    db.executescript(SCHEMA)
    key = hashlib.sha1(f"{kind}|{ident}".encode()).hexdigest()
    db.execute("INSERT OR REPLACE INTO ext_cache(key, kind, payload, fetched_at) VALUES (?,?,?,?)",
               (key, kind, json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.commit()


# ---------------------------------------------------------------- пул

class Pool:
    """Круговой перебор с исключением исчерпанных элементов. Потокобезопасен."""

    def __init__(self, items: list):
        self.items = list(items)
        self.dead: dict[int, str] = {}
        self.used: dict[int, int] = {}
        self._i = 0
        self._lock = threading.Lock()

    def take(self) -> tuple[int, Any] | None:
        with self._lock:
            n = len(self.items)
            for _ in range(n):
                i = self._i % n
                self._i += 1
                if i not in self.dead:
                    self.used[i] = self.used.get(i, 0) + 1
                    return i, self.items[i]
        return None

    def kill(self, i: int, reason: str) -> None:
        with self._lock:
            self.dead[i] = reason

    @property
    def alive(self) -> int:
        return len(self.items) - len(self.dead)

    def summary(self, what: str) -> str:
        if not self.items:
            return f"{what}: нет"
        s = f"{what}: использовано {len(self.used)} из {len(self.items)}"
        if self.dead:
            s += f", исчерпано/недоступно {len(self.dead)} (" + "; ".join(f"#{i + 1}: {r}" for i, r in sorted(self.dead.items())) + ")"
        return s


def _quota_error(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code in (403, 400):
        t = resp.text.lower()
        return "quota" in t or "ratelimit" in t or "rate limit" in t or "resource_exhausted" in t
    return False


def _err_text(resp: httpx.Response) -> str:
    try:
        e = resp.json().get("error", {})
        return f"{resp.status_code} {e.get('status', '')} {str(e.get('message', ''))[:200]}".strip()
    except ValueError:
        return f"{resp.status_code} {resp.text[:160]}"


# ---------------------------------------------------------------- PageSpeed

class PageSpeed:
    def __init__(self, keys: list[str] | None = None, timeout: float = 120):
        self.pool = Pool(keys if keys is not None else pagespeed_keys())
        self.timeout = timeout
        self.requests = 0

    def run(self, url: str, strategy: str = "mobile") -> dict:
        if not self.pool.items:
            raise Skipped("не задан PAGESPEED_API_KEYS (или PAGESPEED_API_KEY)")
        last = ""
        while True:
            got = self.pool.take()
            if got is None:
                raise ExternalError("все ключи PageSpeed исчерпаны или отклонены: " + (last or "нет доступных ключей"))
            i, key = got
            self.requests += 1
            try:
                r = httpx.get(PSI_URL, timeout=self.timeout, params=[
                    ("url", url), ("strategy", strategy), ("category", "performance"), ("category", "seo"), ("key", key)])
            except httpx.HTTPError as e:
                raise ExternalError(f"PageSpeed недоступен: {type(e).__name__}") from e
            if r.status_code == 200:
                data = parse_psi(r.json())
                data["key_no"] = i + 1
                return data
            last = _err_text(r)
            if _quota_error(r):
                self.pool.kill(i, "лимит")
                continue
            if r.status_code in (400, 403) and "api key" in r.text.lower():
                self.pool.kill(i, "ключ отклонён")
                continue
            raise ExternalError(f"PageSpeed ответил: {last}")  # проблема страницы (напр. FAILED_DOCUMENT_REQUEST), не ключа


def parse_psi(js: dict) -> dict:
    lh = js.get("lighthouseResult") or {}
    aud = lh.get("audits") or {}
    cats = lh.get("categories") or {}

    def num(k):
        v = (aud.get(k) or {}).get("numericValue")
        return round(v, 3) if isinstance(v, (int, float)) else None

    def score(k):
        return (aud.get(k) or {}).get("score")

    field = (js.get("loadingExperience") or {})
    fm = field.get("metrics") or {}

    def f(name):
        m = fm.get(name) or {}
        return {"p75": m.get("percentile"), "category": m.get("category")} if m else None

    err = lh.get("runtimeError")
    if err:
        raise ExternalError(f"Lighthouse: {err.get('code')} {str(err.get('message', ''))[:160]}")
    return {
        "perf": (cats.get("performance") or {}).get("score"), "seo": (cats.get("seo") or {}).get("score"),
        "lcp_ms": num("largest-contentful-paint"), "cls": num("cumulative-layout-shift"),
        "tbt_ms": num("total-blocking-time"), "fcp_ms": num("first-contentful-paint"),
        "viewport": score("viewport"), "font_size": score("font-size"), "tap_targets": score("tap-targets"),
        "field": {"overall": field.get("overall_category"), "lcp": f("LARGEST_CONTENTFUL_PAINT_MS"),
                  "cls": f("CUMULATIVE_LAYOUT_SHIFT_SCORE"), "inp": f("INTERACTION_TO_NEXT_PAINT")},
        "final_url": lh.get("finalUrl") or lh.get("finalDisplayedUrl"),
    }


# ---------------------------------------------------------------- Search Console

class Gsc:
    """URL Inspection и список карт через сервисные аккаунты. Комбинации (аккаунт × ресурс) перебираются по кругу."""

    def __init__(self, network: str, base_url: str | None, timeout: float = 40):
        self.network = network
        self.accounts, self.missing = gsc_accounts(network)
        self.properties = gsc_properties(network, base_url)
        self.timeout = timeout
        self.combos = Pool([(a, p) for a in self.accounts for p in self.properties])
        self._creds: dict[Path, Any] = {}
        self.requests = 0

    def _token(self, account: Path) -> str:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as e:
            raise Skipped("для GSC нужны пакеты: pip install google-auth requests") from e
        creds = self._creds.get(account)
        if creds is None:
            creds = service_account.Credentials.from_service_account_file(str(account), scopes=[GSC_SCOPE])
            self._creds[account] = creds
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception as e:  # noqa: BLE001
                raise ExternalError(f"не удалось получить токен GSC ({type(e).__name__})") from e
        return creds.token

    @staticmethod
    def applies(prop: str, url: str) -> bool:
        if prop.startswith("sc-domain:"):
            from urllib.parse import urlsplit
            host = (urlsplit(url).hostname or "").lower()
            dom = prop[len("sc-domain:"):].lower()
            return host == dom or host.endswith("." + dom)
        return url.startswith(prop)

    def _pick(self, url: str | None):
        """Следующая живая комбинация (аккаунт, ресурс), подходящая к URL."""
        for _ in range(len(self.combos.items)):
            cand = self.combos.take()
            if cand is None:
                return None
            if url is None or self.applies(cand[1][1], url):
                return cand
            self.combos.used[cand[0]] -= 1  # ресурс не подходит к этому URL — использованием не считаем
        return None

    def _need_accounts(self) -> None:
        if not self.accounts:
            hint = f" (не найдены файлы: {', '.join(map(str, self.missing))})" if self.missing else ""
            raise Skipped(f"для сети {self.network} не заданы сервисные аккаунты GSC — "
                          f"NET_<СЕТЬ>_GSC_FILES или GSC_SERVICE_ACCOUNT_FILES{hint}")

    def inspect(self, url: str) -> dict:
        self._need_accounts()
        last = ""
        while True:
            got = self._pick(url)
            if got is None:
                raise ExternalError("нет доступного ресурса Search Console для этого URL: " + (last or
                                    "проверьте NET_<СЕТЬ>_GSC_PROPERTIES и права аккаунта"))
            i, (account, prop) = got
            self.requests += 1
            try:
                r = httpx.post(GSC_INSPECT, headers={"Authorization": f"Bearer {self._token(account)}"}, timeout=self.timeout,
                               json={"inspectionUrl": url, "siteUrl": prop, "languageCode": "ru"})
            except httpx.HTTPError as e:
                raise ExternalError(f"GSC недоступен: {type(e).__name__}") from e
            if r.status_code == 200:
                res = r.json().get("inspectionResult") or {}
                idx = res.get("indexStatusResult") or {}
                return {"verdict": idx.get("verdict"), "coverage": idx.get("coverageState"),
                        "indexing": idx.get("indexingState"), "robots": idx.get("robotsTxtState"),
                        "fetch": idx.get("pageFetchState"), "google_canonical": idx.get("googleCanonical"),
                        "user_canonical": idx.get("userCanonical"), "last_crawl": idx.get("lastCrawlTime"),
                        "crawled_as": idx.get("crawledAs"), "account_no": i + 1, "property": prop}
            last = _err_text(r)
            if _quota_error(r):
                self.combos.kill(i, "лимит")
            elif r.status_code in (401, 403, 404):
                self.combos.kill(i, "нет доступа")  # аккаунт не добавлен в ресурс — пробуем следующую комбинацию
            else:
                raise ExternalError(f"GSC ответил: {last}")

    def sitemaps(self) -> list[dict]:
        """Карты сайта: {path, pending, index, last_downloaded, errors, warnings, property}."""
        self._need_accounts()
        last = ""
        while True:
            got = self._pick(None)
            if got is None:
                raise ExternalError("карты сайта GSC недоступны: " + (last or "нет доступных ресурсов"))
            i, (account, prop) = got
            self.requests += 1
            try:
                r = httpx.get(GSC_SITEMAPS.format(site=quote(prop, safe="")), timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self._token(account)}"})
            except httpx.HTTPError as e:
                raise ExternalError(f"GSC недоступен: {type(e).__name__}") from e
            if r.status_code == 200:
                return [{"path": s.get("path"), "pending": s.get("isPending"), "index": s.get("isSitemapsIndex"),
                         "last_downloaded": s.get("lastDownloaded"), "errors": int(s.get("errors") or 0),
                         "warnings": int(s.get("warnings") or 0), "property": prop} for s in r.json().get("sitemap", [])]
            last = _err_text(r)
            if r.status_code in (401, 403, 404) or _quota_error(r):
                self.combos.kill(i, "лимит" if r.status_code == 429 else "нет доступа")
            else:
                raise ExternalError(f"GSC ответил: {last}")

    def summary(self) -> str:
        return (f"GSC: аккаунтов {len(self.accounts)}, ресурсов {len(self.properties)}, запросов {self.requests}"
                + (f"; исчерпано/недоступно {len(self.combos.dead)} из {len(self.combos.items)} комбинаций" if self.combos.dead else ""))
