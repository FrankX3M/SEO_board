"""Отрендеренный DOM (после JS) через Playwright/Chromium."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Access
from .httpheaders import merge_headers
from .netutil import host_of, same_site

BLOCKED_TYPES = {"image", "media", "font"}

_AUTOSCROLL_JS = """
async () => {
  await new Promise(resolve => {
    let y = 0;
    const t = setInterval(() => {
      window.scrollBy(0, 600); y += 600;
      if (y >= document.body.scrollHeight || y > 20000) { clearInterval(t); resolve(); }
    }, 150);
  });
  window.scrollTo(0, 0);
}
"""


@dataclass
class RenderResult:
    requested_url: str
    final_url: str
    status: int | None = None
    chain: list[dict] = field(default_factory=list)
    html: str = ""
    total_ms: float | None = None
    error: str | None = None


class BrowserSession:
    """Один браузер на запуск; отдельный контекст на сеть."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self.browser = None

    async def __aenter__(self) -> "BrowserSession":
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Не установлен playwright: pip install playwright && playwright install chromium") from e
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=self.headless)
        return self

    async def __aexit__(self, *exc) -> None:
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def new_renderer(self, access: Access, *, mobile: bool, block_assets: bool, verify_tls: bool) -> "Renderer":
        viewport = {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900}
        ctx = await self.browser.new_context(
            user_agent=access.ua,
            viewport=viewport,
            is_mobile=mobile,
            has_touch=mobile,
            locale="ru-RU",
            ignore_https_errors=not verify_tls,
        )
        auth = access.auth_headers()
        cookie = next((v for k, v in auth.items() if k.lower() == "cookie"), "")
        if cookie:
            domain = access.host.removeprefix("www.")
            cookies = []
            for part in cookie.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name:
                    cookies.append(dict(name=name, value=value, domain="." + domain, path="/"))
            if cookies:
                await ctx.add_cookies(cookies)
        auth = {k: v for k, v in auth.items() if k.lower() != "cookie"}

        async def handler(route, request):
            if block_assets and request.resource_type in BLOCKED_TYPES:
                return await route.abort()
            # токен — только на запросы к этому же сайту, не на сторонние счётчики/CDN
            if auth and same_site(host_of(request.url), access.host):
                return await route.continue_(headers=merge_headers(request.headers, auth))
            return await route.continue_()

        await ctx.route("**/*", handler)
        return Renderer(ctx)


class Renderer:
    def __init__(self, ctx):
        self.ctx = ctx

    async def render(
        self, url: str, *, timeout_ms: int = 30000, idle_ms: int = 10000, settle_ms: int = 0, scroll: bool = False
    ) -> RenderResult:
        from playwright.async_api import TimeoutError as PWTimeout

        res = RenderResult(requested_url=url, final_url=url)
        t0 = time.perf_counter()
        page = await self.ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=idle_ms)
            except PWTimeout:
                pass  # фоновые запросы (аналитика) могут не затихать — это не ошибка
            if scroll:
                await page.evaluate(_AUTOSCROLL_JS)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            res.html = await page.content()
            res.final_url = page.url
            if resp:
                res.status = resp.status
                chain, r = [], resp.request
                while r.redirected_from:
                    r = r.redirected_from
                    rr = await r.response()
                    chain.insert(0, {"url": r.url, "status": rr.status if rr else None})
                chain.append({"url": resp.url, "status": resp.status})
                res.chain = chain
        except Exception as e:  # noqa: BLE001 — любая ошибка навигации фиксируется, прогон идёт дальше
            res.error = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
        finally:
            await page.close()
        res.total_ms = round((time.perf_counter() - t0) * 1000, 1)
        return res
