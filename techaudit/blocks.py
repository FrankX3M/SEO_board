"""Блоки чек-листа («Блок» в техаудит.xlsx) и привязка к ним проверок и метрик — для группировки дашборда и проверок."""
from __future__ import annotations

from pathlib import Path

from .config import ROOT

SUMMARY = "Итоги"           # сводные метрики (доля пройденных проверок и т.п.)
OTHER = "Прочее"

_cache: dict = {}


def checklist_rows(explicit: str | None = None) -> list[dict]:
    """Строки чек-листа (block, title, criterion, priority, check_id). Нет файла — пункты из реестра проверок одним блоком."""
    from .checks import checklist
    from .checks.runner import load_all
    load_all()  # без загруженного реестра проверок match_check() не сопоставляет пункты, и всё выглядит «ручным»
    from .checks import plan as planmod
    from .checks.extras import fallback_rows, with_extras
    path = checklist.find_checklist(ROOT, explicit)
    key = (str(path), path.stat().st_mtime if path else 0)
    if _cache.get("key") == key:
        return _cache["rows"]
    if path:
        try:
            rows = with_extras(checklist.read_rows(path))  # + дополнительные проверки, которых нет в Excel
        except Exception:  # noqa: BLE001 — битый/открытый в Excel файл не должен ронять интерфейс
            rows = []
    else:
        rows = []
    if not rows:
        rows = fallback_rows()
    rows = planmod.enrich(rows)  # нормализованный приоритет, роль, часы
    _cache.update(key=key, rows=rows)
    return rows


def block_names(rows: list[dict] | None = None) -> list[str]:
    seen: list[str] = []
    for r in rows if rows is not None else checklist_rows():
        b = r.get("block") or OTHER
        if b not in seen:
            seen.append(b)
    return seen


def check_index(rows: list[dict] | None = None) -> dict[str, dict]:
    """check_id -> строка чек-листа (блок, заголовок пункта, критерий, приоритет)."""
    out: dict[str, dict] = {}
    for r in rows if rows is not None else checklist_rows():
        if r.get("check_id") and r["check_id"] not in out:
            out[r["check_id"]] = {"block": r.get("block") or OTHER, "title": r["title"], "criterion": r.get("criterion") or "",
                                  "priority": r.get("priority") or "", "role": r.get("role_label") or "", "hours": r.get("hours")}
    return out


# «сырые» метрики запусков -> блок чек-листа, к которому они относятся
_PROBE_BLOCK = {
    "slash": "Редиректы", "upper": "Редиректы", "alt_host": "Редиректы", "http": "Редиректы", "combined": "Редиректы",
    "index_php": "Редиректы", "index_html": "Редиректы", "not_found": "Коды ответа",
    "robots": "Sitemap.xml", "sitemap": "Sitemap.xml",
    "favicon": "Favicon", "apple_icon": "Favicon", "manifest": "Favicon", "manifest_icon": "Favicon",
    "og_image": "Open Graph",
}
_METRIC_BLOCK = {
    "checks_na": SUMMARY, "checks_coverage": SUMMARY, "pages_unique": "Коды ответа",
    "checks_score": SUMMARY, "checks_pass": SUMMARY, "checks_warn": SUMMARY, "checks_fail": SUMMARY, "checks_skipped": SUMMARY,
    "probes_total": SUMMARY, "probes_pass_share": SUMMARY,
    "pages_total": "Коды ответа", "pages_ok_share": "Коды ответа", "pages_http_err": "Коды ответа",
    "pages_net_err": "Коды ответа", "pages_suspect": "Коды ответа", "ttfb_avg": "Коды ответа", "ttfb_p95": "Коды ответа",
    "bytes_avg": "Коды ответа", "title_share": "Прочие мета", "h1_share": "Прочие мета",
    "diff_major_share": "Рендеринг", "diff_coverage_avg": "Рендеринг",
}


def metric_block(mid: str) -> str:
    from .checks.guidance import block_name
    if mid.startswith("probe:"):
        return block_name(_PROBE_BLOCK.get(mid[6:], "Коды ответа"))
    if mid.startswith("crawl_"):
        return "Итоговый краул"
    if mid.startswith("block:"):
        return block_name(mid[6:])
    if mid.startswith("check:"):
        return (check_index().get(mid[6:]) or {}).get("block") or OTHER
    return block_name(_METRIC_BLOCK.get(mid, OTHER))
