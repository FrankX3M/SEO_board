"""Быстрая проверка карт сайта сети из веб-интерфейса: что найдётся в robots.txt, что задано вручную и как это отвечает.

Использует тот же доступ (токен, UA обычного клиента), что и сборщик проб, и тот же разбор карт (probes.parse_sitemap).
Ничего не пишет в БД. Вложенные карты индекса проверяются выборочно (не больше CHILD_LIMIT на индекс).
"""
from __future__ import annotations

import asyncio
from urllib.parse import urljoin

from .config import NetworkCfg, Settings, resolve_access
from .probes import parse_sitemap
from .rawfetch import fetch_raw, make_client

CHILD_LIMIT = 3


async def _check(settings: Settings, net: NetworkCfg) -> dict:
    access = resolve_access(settings, net, ua=settings.plain_ua)
    base = access.base_url
    out = {"network": net.name, "base": base, "robots": None, "rows": [], "notes": []}
    async with make_client(settings.timeout, settings.verify_tls) as client:
        res = await fetch_raw(client, base + "/robots.txt", access)
        in_robots: list[str] = []
        if res.error:
            out["robots"] = {"ok": False, "text": f"не удалось получить: {res.error}"}
        elif res.status != 200:
            out["robots"] = {"ok": False, "text": f"HTTP {res.status}"}
        else:
            for line in res.body.decode("utf-8", "replace").splitlines():
                if line.lower().startswith("sitemap:") and line.split(":", 1)[1].strip():
                    in_robots.append(urljoin(base + "/", line.split(":", 1)[1].strip()))
            out["robots"] = {"ok": bool(in_robots), "text": f"директив Sitemap: {len(in_robots)}" if in_robots
                             else "в robots.txt нет директивы Sitemap"}
        origin: dict[str, str] = {}
        if access.sitemaps_robots:
            origin.update({u: "robots.txt" for u in in_robots})
        for u in access.sitemaps:
            origin.setdefault(u, "настройки сети")
        if not origin and access.sitemaps_fallback:
            origin[base + "/sitemap.xml"] = "запасной вариант"
        if not origin:
            out["notes"].append("Карт для проверки нет: ни в robots.txt, ни в настройках, запасной вариант выключен.")

        async def one(url: str, src: str, depth: int) -> list[tuple[str, str, int, dict]]:
            r = await fetch_raw(client, url, access)
            info = parse_sitemap(r.body) if r.status == 200 and not r.error else {"type": "none"}
            problem = (f"ошибка: {r.error}" if r.error else f"HTTP {r.status}" if r.status != 200 else
                       "некорректный XML" if info["type"] == "invalid" else
                       "это не карта сайта" if info["type"] == "unknown" else "")
            row = {"url": url, "source": src, "depth": depth, "status": r.status, "type": info["type"],
                   "urls": info.get("urls"), "bytes": info.get("bytes"), "ttfb": r.ttfb_ms, "problem": problem,
                   "children": len(info.get("locs", [])) if info["type"] == "index" else 0}
            rows = [row]
            if info["type"] == "index" and depth < 1:
                for child in info.get("locs", [])[:CHILD_LIMIT]:
                    rows += await one(child, src, depth + 1)
            return rows

        for url, src in origin.items():
            out["rows"] += await one(url, src, 0)
    out["ok"] = bool(out["rows"]) and all(not r["problem"] for r in out["rows"])
    return out


def check_network(settings: Settings, net: NetworkCfg) -> dict:
    return asyncio.run(_check(settings, net))
