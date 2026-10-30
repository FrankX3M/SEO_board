"""Сырой HTML без JS. Редиректы проходим вручную и записываем всю цепочку."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from .config import Access
from .netutil import host_of, same_site

SKIP_HEADERS = {"set-cookie", "authorization", "proxy-authorization"}


@dataclass
class RawResult:
    requested_url: str
    final_url: str
    status: int | None = None
    chain: list[dict] = field(default_factory=list)  # все хопы; последний — финальный
    headers: dict[str, str] = field(default_factory=dict)  # заголовки финального ответа
    body: bytes = b""
    ttfb_ms: float | None = None  # до заголовков финального ответа
    total_ms: float | None = None
    error: str | None = None


def make_client(timeout: float, verify: bool) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        verify=verify,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=20),
    )
    # Не копим cookie между запросами: каждая страница запрашивается «с чистого листа».
    from http.cookiejar import DefaultCookiePolicy

    client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return client


def request_headers(access: Access, url: str) -> dict[str, str]:
    h = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }
    if access.ua:
        h["User-Agent"] = access.ua
    if same_site(host_of(url), access.host):
        h.update(access.auth_headers())
    return h


async def fetch_raw(
    client: httpx.AsyncClient,
    url: str,
    access: Access,
    *,
    retries: int = 2,
    max_hops: int = 10,
) -> RawResult:
    t_start = time.perf_counter()
    res = RawResult(requested_url=url, final_url=url)
    cur, seen = url, {url}
    for _ in range(max_hops + 1):
        attempt = 0
        while True:
            try:
                t0 = time.perf_counter()
                async with client.stream("GET", cur, headers=request_headers(access, cur)) as resp:
                    ttfb = (time.perf_counter() - t0) * 1000
                    body = await resp.aread()
                break
            except httpx.TransportError as e:  # таймауты и сетевые сбои; HTTP-статусы НЕ ретраим
                attempt += 1
                if attempt > retries:
                    res.final_url, res.error = cur, f"{type(e).__name__}: {e}"
                    res.total_ms = (time.perf_counter() - t_start) * 1000
                    return res
                await asyncio.sleep(1.5 * attempt)
        loc = resp.headers.get("location")
        res.chain.append({"url": cur, "status": resp.status_code, "location": loc, "ttfb_ms": round(ttfb, 1)})
        if 300 <= resp.status_code < 400 and loc:
            nxt = urljoin(cur, loc)
            if nxt in seen:
                res.final_url, res.status, res.error = cur, resp.status_code, "redirect_loop"
                break
            seen.add(nxt)
            cur = nxt
            continue
        hdrs: dict[str, list[str]] = {}
        for k, v in resp.headers.multi_items():
            if k.lower() not in SKIP_HEADERS:
                hdrs.setdefault(k.lower(), []).append(v)
        res.headers = {k: ", ".join(v) for k, v in hdrs.items()}
        res.final_url, res.status, res.body, res.ttfb_ms = cur, resp.status_code, body, round(ttfb, 1)
        break
    else:
        res.final_url, res.error = cur, "too_many_redirects"
    res.total_ms = round((time.perf_counter() - t_start) * 1000, 1)
    return res
