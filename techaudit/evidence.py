"""Доказательства для проверки: какие запросы стоят за URL из результата и в каких условиях они делались (UA, токен, статус, цепочка).

Нужны, чтобы вручную перепроверить пункт на сайте. Значение токена нигде не хранится и не показывается:
в команде curl стоит переменная окружения $SP_TOKEN (задайте её из .env в своём терминале).
"""
from __future__ import annotations

import json
import shlex
import sqlite3

SHOW_HEADERS = ("content-type", "location", "x-robots-tag", "link", "cache-control", "server", "content-language", "last-modified")
ROLE_OF_VARIANT = {"plain": "plain", "browser-plain": "plain", "bot": "bot", "browser": "bot"}
VARIANT_NOTE = {
    "plain": "обычный UA, без рендеринга (сырой HTML)",
    "bot": "UA поискового бота, без рендеринга (сырой HTML)",
    "browser": "UA бота, страница отрисована в Chromium (после JS)",
    "browser-plain": "обычный UA, страница отрисована в Chromium (после JS)",
}


def _variants(url: str) -> list[str]:
    u = (url or "").strip()
    base = u.rstrip("/")
    return list(dict.fromkeys([u, base, base + "/"]))


def _json(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def _access_for(opts: dict, network: str, role: str) -> dict:
    d = ((opts.get("networks") or {}).get(network) or {}).get(role) or {}
    tok = d.get("token") or ""
    on = bool(tok) and not tok.startswith("ВЫКЛЮЧЕН")
    return {"ua_name": d.get("user_agent"), "token_on": on, "token_header": tok.split(":", 1)[0] if on and ":" in tok else None,
            "token_mask": tok.split(": ", 1)[1] if on and ": " in tok else "", "warnings": d.get("warnings") or []}


def curl_for(url: str, ua: str | None, token_header: str | None) -> str:
    parts = ["curl", "-sSIL", "--max-redirs", "10"]
    if ua:
        parts += ["-A", shlex.quote(ua)]
    cmd = " ".join(parts)
    if token_header:
        cmd += f' -H "{token_header}: $SP_TOKEN"'
    return f"{cmd} {shlex.quote(url)}"


def _conditions(opts: dict, settings, network: str, role: str, ua_name_row: str | None) -> dict:
    a = _access_for(opts, network, role)
    ua_name = ua_name_row or a["ua_name"]
    profiles = getattr(settings, "ua_profiles", {}) or {}
    ua = (opts.get("ua_strings") or {}).get(role) or profiles.get(str(ua_name or "").lower())
    return {"ua_name": ua_name, "ua": ua, "token_on": a["token_on"], "token_header": a["token_header"],
            "token_mask": a["token_mask"], "warnings": a["warnings"]}


def _short_headers(raw) -> dict:
    h = _json(raw, {}) if isinstance(raw, str) else (raw or {})
    low = {str(k).lower(): v for k, v in h.items()}
    return {k: low[k] for k in SHOW_HEADERS if k in low}


def _chain(raw) -> list:
    out = []
    for hop in _json(raw, []) or []:
        out.append(hop if isinstance(hop, dict) else {"url": str(hop)})
    return out


def for_url(db: sqlite3.Connection, settings, run_id: str, network: str, url: str) -> dict:
    run = db.execute("SELECT options, started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
    opts = _json(run["options"], {}) if run else {}
    vs = _variants(url)
    marks = ",".join("?" * len(vs))
    reqs = []
    for r in db.execute(
            f"SELECT * FROM pages WHERE run_id=? AND network=? AND (source_url IN ({marks}) OR url IN ({marks}) OR final_url IN ({marks})) "
            f"ORDER BY mode, variant LIMIT 12", [run_id, network, *vs, *vs, *vs]):
        role = ROLE_OF_VARIANT.get(r["variant"] or "", "bot")
        c = _conditions(opts, settings, network, role, r["ua_name"])
        reqs.append({"kind": "page", "variant": r["variant"], "note": VARIANT_NOTE.get(r["variant"] or "", ""), "mode": r["mode"],
                     "url": r["url"], "status": r["status"], "final_url": r["final_url"], "chain": _chain(r["chain"]),
                     "ttfb": round(r["ttfb_ms"]) if r["ttfb_ms"] is not None else None, "bytes": r["bytes"],
                     "suspect": r["suspect"] or "", "error": r["error"] or "", "headers": _short_headers(r["headers"]),
                     "cond": c, "curl": curl_for(r["url"], c["ua"], c["token_header"] if c["token_on"] else None),
                     "snapshot": r["file_path"] or ""})
    for r in db.execute(
            f"SELECT * FROM probes WHERE run_id=? AND network=? AND (source_url IN ({marks}) OR url IN ({marks}) OR final_url IN ({marks})) "
            f"ORDER BY kind LIMIT 12", [run_id, network, *vs, *vs, *vs]):
        c = _conditions(opts, settings, network, "plain", None)
        reqs.append({"kind": "probe", "variant": r["kind"], "note": "тест адреса: " + r["kind"], "mode": "raw", "url": r["url"],
                     "status": r["status"], "final_url": r["final_url"], "chain": _chain(r["chain"]),
                     "ttfb": round(r["ttfb_ms"]) if r["ttfb_ms"] is not None else None, "bytes": r["bytes"],
                     "suspect": "", "error": "", "headers": _short_headers(r["headers"]), "cond": c,
                     "expected": _json(r["expected"], {}), "verdict": r["verdict"], "reason": r["reason"] or "",
                     "curl": curl_for(r["url"], c["ua"], c["token_header"] if c["token_on"] else None), "snapshot": r["file_path"] or ""})
    for r in db.execute(
            f"SELECT * FROM live_urls WHERE run_id=? AND network=? AND (url IN ({marks}) OR final_url IN ({marks})) LIMIT 12",
            [run_id, network, *vs, *vs]):
        c = _conditions(opts, settings, network, "bot", None)
        reqs.append({"kind": "live", "variant": r["kind"], "note": "живой запрос к проду при проверке (" + r["kind"] + ")",
                     "mode": "raw", "url": r["url"], "status": r["status"], "final_url": r["final_url"],
                     "chain": [], "ttfb": None, "bytes": None, "suspect": "", "error": "", "headers": {},
                     "location": r["location"] or "", "hops": r["hops"], "cond": c,
                     "curl": curl_for(r["url"], c["ua"], c["token_header"] if c["token_on"] else None), "snapshot": ""})
    crawl_row = None
    try:
        from . import crawl
        imp = crawl.latest_for(db, network, run["started_at"] if run else None)
        if imp:
            row = db.execute(f"SELECT * FROM crawl_pages WHERE import_id=? AND address IN ({marks}) LIMIT 1", [imp["id"], *vs]).fetchone()
            if row:
                crawl_row = {"import_id": imp["id"], "taken_at": imp["taken_at"], "label": imp.get("label") or "",
                             **{k: row[k] for k in ("status", "indexability", "indexability_status", "canonical", "inlinks", "depth",
                                                    "response_ms", "redirect_url", "title", "h1", "meta_robots", "x_robots")}}
    except sqlite3.Error:
        pass
    return {"url": url, "run_id": run_id, "network": network, "requests": reqs, "crawl": crawl_row,
            "found": bool(reqs or crawl_row)}


def run_conditions(db: sqlite3.Connection, settings, run_id: str, network: str) -> dict:
    """Общие условия запуска: выборка, UA и токен по ролям, пороги проверок (если запуск был с --debug)."""
    run = db.execute("SELECT options FROM runs WHERE run_id=?", (run_id,)).fetchone()
    opts = _json(run["options"], {}) if run else {}
    roles = {r: _conditions(opts, settings, network, r, None) for r in ("plain", "bot")}
    dbg = {}
    try:
        dbg = {r["key"]: _json(r["value"], r["value"]) for r in db.execute("SELECT key, value FROM run_debug WHERE run_id=?", (run_id,))}
    except sqlite3.Error:
        pass
    return {"limit": opts.get("limit"), "types": opts.get("types"), "variants": opts.get("variants") or [],
            "probes": opts.get("probes") or [], "probe_limit": opts.get("probe_limit"), "roles": roles,
            "debug_collect": bool(opts.get("debug")), "debug": dbg}
