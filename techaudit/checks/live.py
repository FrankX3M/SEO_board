"""Живая выборка (--live): точечные запросы к проду, которых нет в HTML-выгрузке.

Не краул сайта: берётся ограниченная выборка (LIVE_LIMIT на вид) —
  link             внутренние ссылки со страниц справочника (редиректы / 404)
  image            изображения со страниц (битые)
  canonical        цели canonical, не совпадающие со своей страницей
  sitemap_url      URL из карт сайта (200 / canonical / noindex)
  param_canonical  страница с ?utm_source=…&sort=… — canonical должен остаться чистым
  page2            листинг с ?page=2 — логика canonical пагинации
  tls              TLS-сертификат основного хоста и второго (с www / без www): срок, самоподписанный, имя хоста
  llms             /llms.txt
  conditional      условный запрос (If-None-Match / If-Modified-Since) к страницам с валидатором кеша: ожидается 304
Токен и UA — те же, что у остальных скриптов (config.resolve_access); токен уходит только на хосты сети.
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import ssl
import tempfile
import time
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import lxml.html

from ..collect import RateLimiter
from ..config import resolve_access
from ..rawfetch import fetch_raw, make_client, request_headers
from ..storage import now_iso
from .core import page_noindex, role_of

SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|pdf|zip|mp4|css|js|xml|json|txt)(\?|$)", re.I)


def _spread(seq: list, n: int) -> list:
    if len(seq) <= n:
        return list(seq)
    step = len(seq) / n
    return [seq[int(i * step)] for i in range(n)]


def _page_info(body: bytes) -> dict:
    try:
        doc = lxml.html.fromstring(body)
    except Exception:  # noqa: BLE001
        return {}
    can = [e.get("href") for e in doc.xpath("//link[@href]") if "canonical" in (e.get("rel") or "").lower().split()]
    robots = " ".join((m.get("content") or "") for m in doc.xpath("//meta[@name]")
                      if (m.get("name") or "").lower() in ("robots", "googlebot", "yandex")).lower()
    return {"canonical": can[0].strip() if can else None, "noindex": "noindex" in robots}


_TLS_RU = (("self-signed", "самоподписанный сертификат"), ("expired", "сертификат просрочен"),
           ("hostname mismatch", "имя хоста не совпадает с сертификатом"), ("ip address mismatch", "имя хоста не совпадает с сертификатом"),
           ("unable to get local issuer", "недоверенный центр сертификации или неполная цепочка"),
           ("unable to get issuer", "недоверенный центр сертификации или неполная цепочка"))


def _cert_fields(der: bytes) -> dict:
    """Субъект, издатель и срок из DER-сертификата (без проверки цепочки); пусто, если разобрать не удалось."""
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(pem)
            name = f.name
        try:
            c = ssl._ssl._test_decode_cert(name)  # type: ignore[attr-defined]
        finally:
            import os
            os.unlink(name)
    except Exception:  # noqa: BLE001
        return {}

    def cn(t):
        d = dict(x[0] for x in t or ())
        return d.get("commonName") or d.get("organizationName") or ""

    out = {"subject": cn(c.get("subject")), "issuer": cn(c.get("issuer")), "not_after": c.get("notAfter")}
    out["self_signed"] = bool(c.get("subject")) and c.get("subject") == c.get("issuer")
    if c.get("notAfter"):
        out["days_left"] = int((ssl.cert_time_to_seconds(c["notAfter"]) - time.time()) // 86400)
    return out


def tls_info(host: str, port: int = 443, timeout: float = 10.0) -> dict:
    """Рукопожатие TLS с проверкой цепочки и имени; при ошибке проверки сертификат читается повторно без неё."""
    out: dict = {"host": host, "ok": False}
    try:
        import certifi
        cx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        cx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, cx.wrap_socket(sock, server_hostname=host) as s:
            out.update(ok=True, **_cert_fields(s.getpeercert(binary_form=True) or b""))
            return out
    except ssl.SSLCertVerificationError as e:
        msg = getattr(e, "verify_message", "") or str(e)
        out.update(error=next((ru for en, ru in _TLS_RU if en in msg.lower()), msg), error_kind="verify")
    except (socket.gaierror, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
        out.update(error=f"{type(e).__name__}: {str(e)[:80]}", error_kind="connect")
        return out
    except ssl.SSLError as e:
        out.update(error=f"ошибка TLS: {str(e)[:80]}", error_kind="verify")
    try:  # сведения о сертификате без проверки — чтобы показать CN и срок
        raw = ssl.create_default_context()
        raw.check_hostname, raw.verify_mode = False, ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as sock, raw.wrap_socket(sock, server_hostname=host) as s:
            out.update(_cert_fields(s.getpeercert(binary_form=True) or b""))
    except Exception:  # noqa: BLE001
        pass
    return out


def plan(ctx) -> list[dict]:
    """Список запросов {kind, url, meta}."""
    n = ctx.cs.live_limit
    pages = ctx.primary()
    out: list[dict] = []
    seen: set[tuple] = set()

    def add(kind, url, **meta):
        if (kind, url) not in seen:
            seen.add((kind, url))
            out.append({"kind": kind, "url": url, "meta": meta})

    # ссылки: по кругу со всех страниц, чтобы выборка не состояла из одного меню
    pools = []
    own = {p.final_url for p in pages}
    for p in pages:
        pool = []
        for h in dict.fromkeys(p.links):
            h = h.split("#")[0]
            if p.internal(h) and h not in own and not SKIP_EXT.search(h):
                pool.append((h, p.source_url))
        pools.append(pool)
    taken = 0
    for i in range(max((len(x) for x in pools), default=0)):
        for pool in pools:
            if i < len(pool) and taken < n:
                # из каждого пула берём элементы с шагом, чтобы захватить не только шапку
                add("link", pool[i][0], found_on=pool[i][1])
                taken += 1
    # изображения
    imgs, seen_i = [], set()
    for p in pages:
        for el in p.xp("//img"):
            src = (el.get("src") or el.get("data-src") or "").strip()
            if src and not src.startswith("data:"):
                u = urljoin(p.final_url, src)
                if u not in seen_i:
                    seen_i.add(u)
                    imgs.append((u, p.source_url))
    for u, found in _spread(imgs, n):
        add("image", u, found_on=found)
    # canonical-цели
    for p in pages:
        c = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
        if c and c != p.final_url and c.startswith("http"):
            add("canonical", c, found_on=p.source_url)
    # TLS: основной хост и второй (с www / без www), llms.txt, условные запросы
    host = (urlsplit(ctx.base_url).hostname or "").lower()
    if host and "." in host and not re.fullmatch(r"[\d.]+", host):
        alt = host[4:] if host.startswith("www.") else "www." + host
        add("tls", f"https://{host}/", host=host)
        add("tls", f"https://{alt}/", host=alt)
    if ctx.base_url:
        add("llms", ctx.base_url.rstrip("/") + "/llms.txt")
    done_cond: set[str] = set()
    for p in pages:
        if p.page_type in done_cond or len(done_cond) >= 5:
            continue
        etag, lm = p.headers.get("etag"), p.headers.get("last-modified")
        if etag or lm:
            done_cond.add(p.page_type)
            add("conditional", p.final_url, validator="ETag" if etag else "Last-Modified", etag=etag, last_modified=lm, page_type=p.page_type)
    # карта сайта
    sm = list(ctx.sitemap["urls"])
    for u in _spread(sm, n):
        add("sitemap_url", u)
    # canonical с параметрами и пагинацией
    done_types: set[str] = set()
    for p in pages:
        if p.page_type in done_types or urlsplit(p.url).query or page_noindex(p, ctx.cs):
            continue
        role = role_of(p.page_type, ctx.cs)
        if role in ("search", "filter"):
            continue
        done_types.add(p.page_type)
        clean = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
        u = urlsplit(p.url)
        q = urlencode({"utm_source": "techaudit", "sort": "price_asc"})
        add("param_canonical", urlunsplit((u.scheme, u.netloc, u.path, q, "")), clean_canonical=clean, page_type=p.page_type)
        if role == "listing":
            add("page2", urlunsplit((u.scheme, u.netloc, u.path, urlencode({ctx.cs.pagination_param: 2}), "")),
                clean_canonical=clean, page_type=p.page_type)
    return out


async def collect_live(ctx, net_cfg, *, token=None, ua=None, quiet=False) -> int:
    """Выполняет план и пишет в live_urls. Возвращает число запросов."""
    ctx.db.execute("DELETE FROM live_urls WHERE run_id=? AND network=?", (ctx.run_id, ctx.network))
    ctx.db.commit()
    access = resolve_access(ctx.settings, net_cfg, token=token, ua=ua)
    todo = plan(ctx)
    sem = asyncio.Semaphore(ctx.settings.concurrency)
    limiter = RateLimiter(ctx.settings.delay)
    client = make_client(ctx.settings.timeout, ctx.settings.verify_tls)

    def insert(item, status, final_url, location, hops, meta):
        ctx.db.execute("INSERT INTO live_urls(run_id,network,kind,url,status,final_url,location,hops,meta,fetched_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (ctx.run_id, ctx.network, item["kind"], item["url"], status, final_url, location, hops,
                        json.dumps(meta, ensure_ascii=False), now_iso()))

    async def one(item):
        kind, meta = item["kind"], dict(item["meta"])
        if kind == "tls":
            info = await asyncio.to_thread(tls_info, meta["host"], 443, min(15.0, ctx.settings.timeout))
            meta.update(info)
            insert(item, 200 if info.get("ok") else None, item["url"], None, 0, meta)
            return
        if kind == "conditional":
            hdr = request_headers(access, item["url"])
            if meta.get("etag"):
                hdr["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                hdr["If-Modified-Since"] = meta["last_modified"]
            async with sem:
                await limiter.wait()
                try:
                    r = await client.get(item["url"], headers=hdr)
                    status, err = r.status_code, None
                except Exception as e:  # noqa: BLE001
                    status, err = None, f"{type(e).__name__}: {e}"
            if err:
                meta["error"] = err
            insert(item, status, item["url"], None, 1, meta)
            return
        async with sem:
            await limiter.wait()
            res = await fetch_raw(client, item["url"], access)
        first = res.chain[0] if res.chain else {}
        status = first.get("status") if kind == "link" else res.status
        if kind in ("canonical", "sitemap_url", "param_canonical", "page2") and res.status == 200 and res.body:
            meta.update(_page_info(res.body))
        if kind == "llms" and res.status == 200:
            meta.update(head=res.body[:300].decode("utf-8", "replace"), bytes=len(res.body),
                        content_type=(res.headers.get("content-type") or "").split(";")[0])
        meta["final_status"] = res.status
        if res.error:
            meta["error"] = res.error
        insert(item, status, res.final_url, first.get("location"), len(res.chain), meta)

    try:
        await asyncio.gather(*(one(i) for i in todo))
    finally:
        await client.aclose()
    ctx.db.commit()
    if not quiet:
        from collections import Counter
        c = Counter(i["kind"] for i in todo)
        print(f"  [live] {ctx.network}: " + ", ".join(f"{k}={v}" for k, v in c.items()), flush=True)
    return len(todo)
