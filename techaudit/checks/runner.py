"""Запуск проверок по сети и запуску; запись результатов в БД."""
from __future__ import annotations

import json
import traceback

from ..storage import Store, now_iso
from .core import REGISTRY, SKIPPED, CheckSettings, Result
from .ctx import Ctx, ensure_schema


def load_all() -> None:
    """Импорт модулей проверок (регистрация через декоратор)."""
    from . import c_http, c_markup, c_render, c_seo  # noqa: F401
    from . import x_meta, x_robots, x_server, x_urls  # noqa: F401  # дополнительные проверки (нет в Excel-чек-листе)
    from . import c_crawl, c_external  # noqa: F401  # заменяют заглушки/выборочные проверки; импорт строго после остальных
    c_crawl.install()


def networks_in_run(store: Store, run_id: str) -> list[str]:
    return [r[0] for r in store.db.execute(
        "SELECT DISTINCT network FROM pages WHERE run_id=? UNION SELECT DISTINCT network FROM probes WHERE run_id=?",
        (run_id, run_id))]


def run_checks(store: Store, settings, run_id: str, network: str, *, only: set[str] | None = None,
               baseline: str | None = None, mobile_run: str | None = None) -> dict[str, Result]:
    load_all()
    ensure_schema(store)
    cfg = settings.networks.get(network)
    base = cfg.base_url() if cfg else ""
    ctx = Ctx(store, settings, run_id, network, CheckSettings.from_env(), base, baseline, mobile_run)
    results: dict[str, Result] = {}
    for cid, d in REGISTRY.items():
        if only and cid not in only:
            continue
        try:
            res = d.fn(ctx)
        except Exception as e:  # noqa: BLE001 — одна упавшая проверка не должна ронять остальные
            res = Result(SKIPPED, f"ошибка проверки: {type(e).__name__}: {str(e)[:160]}", source=d.source)
            res.items = []
            traceback.print_exc()
        results[cid] = res
    save_results(store, run_id, network, results)
    return results


def save_results(store: Store, run_id: str, network: str, results: dict[str, Result]) -> None:
    # заменяем только пересчитанные проверки: --only не стирает результаты остальных
    store.db.executemany("DELETE FROM check_results WHERE run_id=? AND network=? AND check_id=?",
                         [(run_id, network, cid) for cid in results])
    for cid, r in results.items():
        items = [{"url": i.url, "ok": i.ok, "msg": i.msg, "kind": i.kind} for i in r.items]
        stats = r.stats or {"total": len(items), "bad": sum(1 for i in items if not i["ok"])}
        store.db.execute(
            "INSERT INTO check_results(run_id,network,check_id,title,status,summary,items,source,created_at,stats) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, network, cid, REGISTRY[cid].titles[0], r.status, r.summary,
             json.dumps(items, ensure_ascii=False), r.source, now_iso(), json.dumps(stats)))
    store.db.commit()


def load_results(store: Store, run_id: str) -> dict[str, dict[str, dict]]:
    """{network: {check_id: {status, summary, items, source}}}"""
    import sqlite3
    store.db.row_factory = sqlite3.Row
    out: dict[str, dict[str, dict]] = {}
    for r in store.db.execute("SELECT * FROM check_results WHERE run_id=?", (run_id,)):
        out.setdefault(r["network"], {})[r["check_id"]] = {
            "status": r["status"], "summary": r["summary"], "items": json.loads(r["items"] or "[]"), "source": r["source"]}
    return out
