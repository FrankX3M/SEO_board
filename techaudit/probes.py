"""Пробы: дополнительные запросы к проду, которых нет в HTML-выгрузке.

Из справочника строятся «неправильные» варианты адреса (другой слеш, регистр, схема, хост,
index.php…) и несуществующие страницы; проверяется, как сайт на них отвечает. Отдельно
забираются robots.txt и карты сайта. Результат — строки в таблице probes с вердиктом
pass/fail и причинами (reason), а не просто статусами.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import ipaddress
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from .config import Access
from .rawfetch import RawResult, fetch_raw
from .registry import RegistryEntry, build_url
from .storage import Store, now_iso

REDIRECT_KINDS = ("slash", "upper", "alt_host", "http", "combined", "index_php", "index_html")
RESOURCE_KINDS = ("favicon", "apple_icon", "manifest", "manifest_icon", "og_image")
KIND_GROUPS = {
    "redirects": REDIRECT_KINDS,
    "not_found": ("not_found",),
    "discovery": ("robots", "sitemap"),
    "resources": RESOURCE_KINDS,
}
ALL_KINDS = tuple(k for g in KIND_GROUPS.values() for k in g)
KIND_LABELS = {
    "slash": "слеш в конце", "upper": "заглавные буквы", "alt_host": "www / без www", "http": "http → https",
    "combined": "всё сразу", "index_php": "/index.php", "index_html": "/index.html",
    "not_found": "несуществующая страница", "robots": "robots.txt", "sitemap": "карта сайта",
    "favicon": "favicon", "apple_icon": "apple-touch-icon", "manifest": "манифест",
    "manifest_icon": "иконка манифеста", "og_image": "og:image / twitter:image",
}
SITEMAP_MAX_URLS, SITEMAP_MAX_BYTES, SITEMAP_MAX_FILES = 50_000, 50 * 1024 * 1024, 100


def parse_kinds(spec: str | None) -> tuple[str, ...]:
    """'all' | 'redirects,not_found' | 'slash,upper,sitemap' (группы и отдельные виды)."""
    if not spec or spec.strip().lower() == "all":
        return ALL_KINDS
    if spec.strip().lower() in {"none", "off"}:
        return ()
    out: list[str] = []
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        if part in KIND_GROUPS:
            out += KIND_GROUPS[part]
        elif part in ALL_KINDS:
            out.append(part)
        else:
            raise ValueError(f"Неизвестная проба '{part}'. Доступны: {', '.join(list(KIND_GROUPS) + list(ALL_KINDS))}")
    return tuple(dict.fromkeys(out))


@dataclass(frozen=True)
class ProbeSpec:
    kind: str
    page_type: str
    source_url: str
    url: str
    expected: dict


def _is_domain(host: str) -> bool:
    if not host or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def _flip_slash(path: str) -> str | None:
    if path in ("", "/"):
        return None
    return path[:-1] if path.endswith("/") else path + "/"


def redirect_specs(base_url: str, entry: RegistryEntry, kinds, skip_index_types=()) -> list[ProbeSpec]:
    """Адрес из справочника считается правильным (канонической формой); пробы — его искажения."""
    canonical = build_url(base_url, entry)
    p = urlsplit(canonical)
    host = p.hostname or ""
    port = f":{p.port}" if p.port else ""
    alt_host = None
    if _is_domain(host):
        alt_host = (host[4:] if host.startswith("www.") else "www." + host) + port
    flipped = _flip_slash(p.path)
    upper = p.path.upper() if p.path.upper() != p.path else None

    def url(scheme=None, netloc=None, path=None) -> str:
        return urlunsplit((scheme or p.scheme, netloc or p.netloc, path if path is not None else p.path, p.query, ""))

    exp = {"first_status": 301, "hops": 2, "status": 200, "target": canonical}
    cand: list[tuple[str, str | None]] = [
        ("slash", url(path=flipped) if flipped else None),
        ("upper", url(path=upper) if upper else None),
        ("alt_host", url(netloc=alt_host) if alt_host else None),
        ("http", url(scheme="http") if p.scheme == "https" else None),
    ]
    # всё сразу: схема + хост + регистр + слеш — должно привести на канонический адрес за один шаг
    if p.scheme == "https" and alt_host and (upper or flipped):
        cpath = upper or p.path
        if flipped:
            cpath = _flip_slash(cpath)
        cand.append(("combined", url(scheme="http", netloc=alt_host, path=cpath)))
    if entry.type not in skip_index_types:
        d = p.path if p.path.endswith("/") else p.path + "/"
        cand.append(("index_php", urlunsplit((p.scheme, p.netloc, d + "index.php", "", ""))))
        cand.append(("index_html", urlunsplit((p.scheme, p.netloc, d + "index.html", "", ""))))
    out = []
    for kind, u in cand:
        if u and kind in kinds and u != canonical:
            out.append(ProbeSpec(kind, entry.type, entry.source_url, u, dict(exp)))
    return out


def not_found_spec(base_url: str, entry: RegistryEntry) -> ProbeSpec:
    """Тот же раздел, но последний сегмент пути заменён на несуществующий слаг."""
    canonical = build_url(base_url, entry)
    p = urlsplit(canonical)
    trailing = p.path.endswith("/")
    segs = [s for s in p.path.split("/") if s]
    slug = "ta-404-" + hashlib.sha1(canonical.encode()).hexdigest()[:8]
    if segs:
        segs[-1] = slug
    else:
        segs = [slug]
    path = "/" + "/".join(segs) + ("/" if trailing or not p.path.strip("/") else "")
    return ProbeSpec("not_found", entry.type, entry.source_url, urlunsplit((p.scheme, p.netloc, path, "", "")),
                     {"status": 404, "hops": 1})


def plan_specs(base_url: str, entries: list[RegistryEntry], kinds, limit: int, skip_index_types=()) -> list[ProbeSpec]:
    """Пробы по справочнику; не более `limit` URL каждого типа."""
    per_type: dict[str, int] = defaultdict(int)
    specs: list[ProbeSpec] = []
    for e in entries:
        per_type[e.type] += 1
        if limit and per_type[e.type] > limit:
            continue
        specs += redirect_specs(base_url, e, kinds, skip_index_types)
        if "not_found" in kinds:
            specs.append(not_found_spec(base_url, e))
    seen, out = set(), []
    for s in specs:
        if (s.kind, s.url) not in seen:
            seen.add((s.kind, s.url))
            out.append(s)
    return out


# ---------- вердикты ----------

def evaluate_redirect(res: RawResult, exp: dict) -> list[str]:
    if res.error:
        return ["fetch_error"]
    reasons = []
    n = len(res.chain)
    if n == 1:
        reasons.append("no_redirect_200_duplicate" if res.status == 200 else
                       "not_found_no_redirect" if res.status == 404 else f"no_redirect_status_{res.status}")
        return reasons
    if res.chain[0]["status"] != exp["first_status"]:
        reasons.append(f"not_301:{res.chain[0]['status']}")
    if n > exp["hops"]:
        reasons.append(f"multi_hop:{n - 1}")
    if res.final_url != exp["target"]:
        reasons.append("wrong_target")
    if res.status != exp["status"]:
        reasons.append(f"final_status_{res.status}")
    return reasons


def evaluate_not_found(res: RawResult, exp: dict) -> list[str]:
    if res.error:
        return ["fetch_error"]
    reasons = []
    if len(res.chain) > 1:
        reasons.append(f"redirected:{res.chain[0]['status']}")  # редирект на отдельную страницу ошибки
    if res.status != 404:
        reasons.append(f"status_{res.status}")
    return reasons


# ---------- выполнение ----------

class ProbeContext:
    """Запросы и запись проб одной сети."""

    def __init__(self, store: Store, run_id: str, network: str, client, access: Access, sem: asyncio.Semaphore,
                 limiter, quiet: bool = False):
        self.store, self.run_id, self.network = store, run_id, network
        self.client, self.access, self.sem, self.limiter, self.quiet = client, access, sem, limiter, quiet
        self.rows: list[dict] = []

    async def fetch(self, url: str) -> RawResult:
        async with self.sem:
            await self.limiter.wait()
            return await fetch_raw(self.client, url, self.access)

    def record(self, kind: str, url: str, res: RawResult, reasons: list[str], *, page_type: str = "",
               source_url: str = "", expected: dict | None = None, meta: dict | None = None,
               save_ext: str | None = None) -> dict:
        file_path = None
        if save_ext and res.body:
            file_path = self.store.save_blob(self.run_id, self.network, f"_probes/{kind}", url, save_ext, res.body)
        row = dict(
            run_id=self.run_id, network=self.network, kind=kind, page_type=page_type, source_url=source_url, url=url,
            expected=json.dumps(expected, ensure_ascii=False) if expected else None, status=res.status,
            final_url=res.final_url, chain=json.dumps(res.chain, ensure_ascii=False),
            headers=json.dumps(res.headers, ensure_ascii=False) if res.headers else None,
            content_type=(res.headers.get("content-type") or "").split(";")[0].strip().lower() or None,
            ttfb_ms=res.ttfb_ms, total_ms=res.total_ms, bytes=len(res.body), file_path=file_path,
            verdict="fail" if reasons else "pass", reason=",".join(reasons),
            meta=json.dumps(meta, ensure_ascii=False) if meta else None, fetched_at=now_iso(),
        )
        self.store.add_probe(row)
        self.rows.append(row)
        if not self.quiet:
            flag = "ok  " if not reasons else "FAIL"
            print(f"  [проба] {self.network:<8} {kind:<13} {flag} {res.status or 'ERR'}  {url}"
                  f"{'  ← ' + ','.join(reasons) if reasons else ''}", flush=True)
        return row

    async def run_spec(self, spec: ProbeSpec) -> None:
        res = await self.fetch(spec.url)
        if spec.kind == "not_found":
            reasons = evaluate_not_found(res, spec.expected)
            self.record(spec.kind, spec.url, res, reasons, page_type=spec.page_type, source_url=spec.source_url,
                        expected=spec.expected, save_ext="html" if res.status == 404 else None)
        else:
            reasons = evaluate_redirect(res, spec.expected)
            self.record(spec.kind, spec.url, res, reasons, page_type=spec.page_type, source_url=spec.source_url,
                        expected=spec.expected)

    # ----- robots.txt и карты сайта -----

    async def run_discovery(self, kinds) -> None:
        """robots.txt и карты сайта. Карты берутся из robots.txt (если не отключено в настройках сети)
        и из списка, заданного вручную; если не нашлось ни одной — запасной /sitemap.xml (если не отключено)."""
        if "robots" not in kinds and "sitemap" not in kinds:
            return
        base = self.access.base_url
        robots_url = base + "/robots.txt"
        want_robots_maps = "sitemap" in kinds and self.access.sitemaps_robots
        sitemaps: list[str] = []
        if "robots" in kinds or want_robots_maps:
            res = await self.fetch(robots_url)
            if res.status == 200:
                for line in res.body.decode("utf-8", "replace").splitlines():
                    if line.lower().startswith("sitemap:"):
                        u = line.split(":", 1)[1].strip()
                        if u:
                            sitemaps.append(urljoin(base + "/", u))
            reasons = [] if (res.status == 200 and sitemaps and not res.error) else (
                ["fetch_error"] if res.error else [f"status_{res.status}"] if res.status != 200 else ["no_sitemap_directive"])
            if "robots" in kinds:
                self.record("robots", robots_url, res, reasons, meta={"sitemaps": sitemaps}, save_ext="txt")
        if "sitemap" in kinds:
            origin: dict[str, str] = {}
            if self.access.sitemaps_robots:
                origin.update({u: "robots.txt" for u in sitemaps})
            for u in self.access.sitemaps:
                origin.setdefault(u, "настройки сети")
            if not origin and self.access.sitemaps_fallback:
                origin[base + "/sitemap.xml"] = "fallback /sitemap.xml"
            await self._sitemaps(origin)

    async def _sitemaps(self, roots: dict[str, str]) -> None:
        """roots: {URL карты: откуда взят}. Вложенные карты индекса наследуют источник родителя."""
        queue = [(u, 0, src) for u, src in roots.items()]
        seen, files = set(), 0
        while queue and files < SITEMAP_MAX_FILES:
            url, depth, src = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            files += 1
            res = await self.fetch(url)
            info = parse_sitemap(res.body) if res.status == 200 else {"type": "none"}
            reasons = []
            if res.error:
                reasons.append("fetch_error")
            elif res.status != 200:
                reasons.append(f"status_{res.status}")
            elif info["type"] == "invalid":
                reasons.append("invalid_xml")
            elif info["type"] == "unknown":
                reasons.append("not_a_sitemap")
            if info.get("bytes", 0) > SITEMAP_MAX_BYTES:
                reasons.append("too_large")
            if info.get("urls", 0) > SITEMAP_MAX_URLS:
                reasons.append("too_many_urls")
            meta = {k: v for k, v in info.items() if k != "locs"}
            meta.update(depth=depth, discovered=src)
            self.record("sitemap", url, res, reasons, meta=meta, save_ext="xml")
            if info["type"] == "index" and depth < 2:
                queue += [(u, depth + 1, src) for u in info.get("locs", [])]


def parse_sitemap(body: bytes) -> dict:
    from lxml import etree

    raw = body
    try:
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        root = etree.fromstring(raw, etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True))
    except Exception as e:  # noqa: BLE001
        return {"type": "invalid", "error": str(e)[:120], "bytes": len(raw)}
    tag = etree.QName(root).localname
    kind = {"sitemapindex": "index", "urlset": "urlset"}.get(tag, "unknown")
    locs, lastmod = [], 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = etree.QName(el).localname
        if name == "loc" and el.text:
            locs.append(el.text.strip())
        elif name == "lastmod" and el.text and el.text.strip():
            lastmod += 1
    out = {"type": kind, "bytes": len(raw), "urls" if kind == "urlset" else "children": len(locs), "lastmod": lastmod}
    if kind == "index":
        out["locs"] = locs
    return out


def summarize_probes(rows: list[dict]) -> list[dict]:
    g: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        g[(r["network"], r["kind"])].append(r)
    out = []
    order = {k: i for i, k in enumerate(ALL_KINDS)}
    for (net, kind), rs in sorted(g.items(), key=lambda kv: (kv[0][0], order.get(kv[0][1], 99))):
        reasons = Counter(x.split(":")[0] for r in rs for x in (r["reason"] or "").split(",") if x)
        out.append({"network": net, "kind": kind, "total": len(rs), "pass": sum(r["verdict"] == "pass" for r in rs),
                    "fail": sum(r["verdict"] == "fail" for r in rs), "reasons": dict(reasons.most_common(4))})
    return out


def print_probe_summary(summary: list[dict]) -> None:
    if not summary:
        return
    print("\n=== ПРОБЫ (прошло / не прошло) ===")
    for s in summary:
        why = "  " + ", ".join(f"{k}×{v}" for k, v in s["reasons"].items()) if s["reasons"] else ""
        print(f"{s['network']:<10} {KIND_LABELS.get(s['kind'], s['kind']):<26} {s['pass']:>3} / {s['total']:<3}{why}")
