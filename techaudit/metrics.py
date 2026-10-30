"""Метрики по запускам: ряды во времени для графиков, матрица проверок, история URL.

Всё считается из таблиц runs / pages / probes / diffs / check_results; отдельных снимков нет,
поэтому цифры всегда совпадают с отчётами. Запуски с разной выборкой (--limit, другие варианты)
сравнивать осторожно: параметры запуска отдаются вместе с рядом.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime

# id -> (название, единица, лучше «up»/«down»/None, группа)
CATALOG: dict[str, tuple[str, str, str | None, str]] = {
    "checks_score": ("Доля пройденных проверок", "%", "up", "Проверки чек-листа"),
    "checks_pass": ("Проверки: пройдено", "шт", "up", "Проверки чек-листа"),
    "checks_warn": ("Проверки: требует внимания", "шт", "down", "Проверки чек-листа"),
    "checks_fail": ("Проверки: не пройдено", "шт", "down", "Проверки чек-листа"),
    "checks_na": ("Проверки: нет данных", "шт", "down", "Проверки чек-листа"),
    "checks_coverage": ("Полнота выполнения проверок", "%", "up", "Проверки чек-листа"),
    "checks_skipped": ("Проверки: пропущено", "шт", "down", "Проверки чек-листа"),
    "pages_total": ("Запросов страниц", "шт", None, "Страницы"),
    "pages_ok_share": ("Страницы с ответом 2xx", "%", "up", "Страницы"),
    "pages_http_err": ("Страницы с кодом 4xx/5xx", "шт", "down", "Страницы"),
    "pages_net_err": ("Сетевые ошибки и таймауты", "шт", "down", "Страницы"),
    "pages_suspect": ("Подозрительные ответы", "шт", "down", "Страницы"),
    "pages_unique": ("Уникальных исходных URL", "шт", None, "Страницы"),
    "title_share": ("Есть <title> (среди запросов 2xx)", "%", "up", "Разметка"),
    "h1_share": ("Есть <h1> (среди запросов 2xx)", "%", "up", "Разметка"),
    "ttfb_avg": ("TTFB среднее", "мс", "down", "Скорость"),
    "ttfb_p95": ("TTFB p95", "мс", "down", "Скорость"),
    "bytes_avg": ("Размер исходного HTML, среднее", "КиБ", None, "Скорость"),
    "probes_total": ("Тестов адресов выполнено", "шт", None, "Тесты адресов"),
    "probes_pass_share": ("Доля пройденных тестов адресов", "%", "up", "Тесты адресов"),
    "diff_major_share": ("Страницы, где рендеринг сильно меняет HTML", "%", "down", "Рендеринг"),
    "diff_coverage_avg": ("Доля контента уже в HTML (до JS)", "%", "up", "Рендеринг"),
}
PROBE_PREFIX = "probe:"  # probe:<kind> — доля пройденных проб одного вида


def label(metric: str) -> str:
    if metric in CATALOG:
        return CATALOG[metric][0]
    if metric.startswith(PROBE_PREFIX):
        from .explain import probe_name
        return "Тест адреса «" + probe_name(metric[len(PROBE_PREFIX):]) + "»: доля пройденных"
    return metric


def meta(metric: str) -> dict:
    if metric in CATALOG:
        name, unit, good, group = CATALOG[metric]
        return {"id": metric, "label": name, "unit": unit, "good": good, "group": group}
    return {"id": metric, "label": label(metric), "unit": "%", "good": "up", "group": "Тесты адресов"}


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _ts_label(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        return iso or ""


def list_runs(db: sqlite3.Connection, limit: int = 40, network: str | None = None) -> list[dict]:
    """Последние запуски в хронологическом порядке (старые → новые)."""
    rows = db.execute("SELECT run_id, started_at, finished_at, env, options, summary FROM runs "
                      "ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit * 3 if network else limit,)).fetchall()
    out = []
    for r in rows:
        if str(r["run_id"]).endswith("-mobile"):  # служебный сбор для сверки версий: в динамике не участвует
            continue
        try:
            opts = json.loads(r["options"] or "{}")
        except ValueError:
            opts = {}
        nets = list((opts.get("networks") or {}).keys())
        if network and nets and network not in nets:
            continue
        out.append({"run_id": r["run_id"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                    "label": _ts_label(r["started_at"]), "limit": opts.get("limit"), "types": opts.get("types"),
                    "variants": opts.get("variants") or [], "probes": opts.get("probes") or [], "networks": nets})
        if len(out) >= limit:
            break
    out.reverse()
    return out


def _in(run_ids: list[str]) -> str:
    return "(" + ",".join("?" * len(run_ids)) + ")"


def collect_metrics(db: sqlite3.Connection, run_ids: list[str], network: str | None = None) -> dict[str, dict[str, float]]:
    """{run_id: {metric: value}} для набора запусков (одним проходом по таблицам)."""
    res: dict[str, dict[str, float]] = {rid: {} for rid in run_ids}
    if not run_ids:
        return res
    nw = " AND network=?" if network else ""
    extra = [network] if network else []
    marks = _in(run_ids)

    ok_expr = "status BETWEEN 200 AND 299 AND (error IS NULL OR error='')"
    for r in db.execute(
            f"SELECT run_id, COUNT(*) n, SUM(CASE WHEN {ok_expr} THEN 1 ELSE 0 END) ok, "
            f"SUM(CASE WHEN status>=400 THEN 1 ELSE 0 END) http_err, "
            f"SUM(CASE WHEN error IS NOT NULL AND error!='' THEN 1 ELSE 0 END) net_err, "
            f"SUM(CASE WHEN suspect IS NOT NULL AND suspect!='' THEN 1 ELSE 0 END) susp, "
            f"COUNT(DISTINCT source_url) unique_urls, AVG(CASE WHEN mode='raw' THEN bytes END) b, SUM(CASE WHEN {ok_expr} AND has_title=1 THEN 1 ELSE 0 END) t, "
            f"SUM(CASE WHEN {ok_expr} AND has_h1=1 THEN 1 ELSE 0 END) h "
            f"FROM pages WHERE run_id IN {marks}{nw} GROUP BY run_id", run_ids + extra):
        m = res[r["run_id"]]
        n, ok = r["n"], r["ok"] or 0
        m["pages_total"] = n
        m["pages_unique"] = r["unique_urls"]
        m["pages_ok_share"] = round(100 * ok / n, 2) if n else None
        m["pages_http_err"] = r["http_err"] or 0
        m["pages_net_err"] = r["net_err"] or 0
        m["pages_suspect"] = r["susp"] or 0
        if r["b"] is not None:
            m["bytes_avg"] = round(r["b"] / 1024, 1)
        if ok:
            m["title_share"] = round(100 * (r["t"] or 0) / ok, 2)
            m["h1_share"] = round(100 * (r["h"] or 0) / ok, 2)

    ttfb: dict[str, list[float]] = defaultdict(list)
    for r in db.execute(f"SELECT run_id, ttfb_ms FROM pages WHERE run_id IN {marks}{nw} AND mode='raw' "
                        f"AND ttfb_ms IS NOT NULL", run_ids + extra):
        ttfb[r["run_id"]].append(r["ttfb_ms"])
    for rid, vals in ttfb.items():
        res[rid]["ttfb_avg"] = round(sum(vals) / len(vals), 1)
        res[rid]["ttfb_p95"] = round(_percentile(vals, 0.95), 1)

    kinds: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in db.execute(f"SELECT run_id, kind, COUNT(*) n, SUM(CASE WHEN verdict='pass' THEN 1 ELSE 0 END) p "
                        f"FROM probes WHERE run_id IN {marks}{nw} GROUP BY run_id, kind", run_ids + extra):
        kinds[r["run_id"]][r["kind"]] = [r["n"], r["p"] or 0]
    for rid, ks in kinds.items():
        total = sum(v[0] for v in ks.values())
        passed = sum(v[1] for v in ks.values())
        res[rid]["probes_total"] = total
        res[rid]["probes_pass_share"] = round(100 * passed / total, 2) if total else None
        for k, (n, p) in ks.items():
            res[rid][PROBE_PREFIX + k] = round(100 * p / n, 2) if n else None

    for r in db.execute(
            f"SELECT run_id, COUNT(*) n, SUM(CASE WHEN verdict='major' THEN 1 ELSE 0 END) major, AVG(coverage) cov "
            f"FROM diffs WHERE run_id IN {marks}{nw} AND variant_a='bot' AND variant_b='browser' AND verdict NOT IN ('skipped') GROUP BY run_id", run_ids + extra):
        if r["n"]:
            res[r["run_id"]]["diff_major_share"] = round(100 * (r["major"] or 0) / r["n"], 2)
            if r["cov"] is not None:
                res[r["run_id"]]["diff_coverage_avg"] = round(100 * r["cov"], 2)

    cnt: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in db.execute(f"SELECT run_id, status, COUNT(*) n FROM check_results WHERE run_id IN {marks}{nw} "
                        f"GROUP BY run_id, status", run_ids + extra):
        cnt[r["run_id"]][r["status"]] = r["n"]
    for rid, c in cnt.items():
        res[rid]["checks_pass"], res[rid]["checks_warn"] = c.get("pass", 0), c.get("warn", 0)
        res[rid]["checks_fail"], res[rid]["checks_skipped"] = c.get("fail", 0), c.get("skipped", 0)
        res[rid]["checks_na"] = c.get("na", 0)
        judged = c.get("pass", 0) + c.get("warn", 0) + c.get("fail", 0)
        res[rid]["checks_coverage"] = round(100 * judged / sum(c.values()), 2) if sum(c.values()) else None
        res[rid]["checks_score"] = round(100 * c.get("pass", 0) / judged, 2) if judged else None
    return res


def all_metric_ids(db: sqlite3.Connection) -> list[str]:
    kinds = [r[0] for r in db.execute("SELECT DISTINCT kind FROM probes ORDER BY kind")]
    return list(CATALOG) + [PROBE_PREFIX + k for k in kinds]


def dashboard(db: sqlite3.Connection, network: str | None = None, limit: int = 30) -> dict:
    """Всё для главной: запуски, ряды всех метрик, последние значения и изменение к предыдущему запуску."""
    runs = list_runs(db, limit, network)
    ids = [r["run_id"] for r in runs]
    m = collect_metrics(db, ids, network)
    series: dict[str, list] = {}
    for mid in all_metric_ids(db):
        vals = [m[rid].get(mid) for rid in ids]
        if any(v is not None for v in vals):
            series[mid] = vals
    cards = []
    for mid, vals in series.items():
        present = [(i, v) for i, v in enumerate(vals) if v is not None]
        last_i, last = present[-1]
        prev = present[-2][1] if len(present) > 1 else None
        d = meta(mid)
        d.update({"value": last, "prev": prev, "delta": (round(last - prev, 2) if prev is not None else None),
                  "run_id": ids[last_i], "latest": last_i == len(ids) - 1})
        good = d["good"]
        d["trend"] = ("flat" if not d["delta"] else
                      "good" if good and ((d["delta"] > 0) == (good == "up")) else
                      "bad" if good else "neutral")
        cards.append(d)
    return {"runs": runs, "series": series, "cards": cards, "network": network}


# ------------------------------------------------------------------ проверки: матрица и изменения

def checks_matrix(db: sqlite3.Connection, network: str, limit: int = 12) -> dict:
    """Строки — проверки, столбцы — последние запуски, где проверки считались для сети."""
    run_ids = [r[0] for r in db.execute(
        "SELECT cr.run_id FROM check_results cr JOIN runs r ON r.run_id=cr.run_id WHERE cr.network=? "
        "GROUP BY cr.run_id ORDER BY r.started_at DESC LIMIT ?", (network, limit))]
    run_ids.reverse()
    if not run_ids:
        return {"runs": [], "rows": [], "changes": []}
    labels = {r["run_id"]: _ts_label(r["started_at"]) for r in db.execute(
        f"SELECT run_id, started_at FROM runs WHERE run_id IN {_in(run_ids)}", run_ids)}
    by_check: dict[str, dict] = {}
    for r in db.execute(f"SELECT run_id, check_id, title, status, summary FROM check_results "
                        f"WHERE network=? AND run_id IN {_in(run_ids)}", [network, *run_ids]):
        e = by_check.setdefault(r["check_id"], {"check_id": r["check_id"], "title": r["title"], "cells": {}})
        e["cells"][r["run_id"]] = {"status": r["status"], "summary": r["summary"]}
    order = {"fail": 0, "warn": 1, "skipped": 2, "na": 3, "pass": 4}
    last = run_ids[-1]
    rows = sorted(by_check.values(), key=lambda e: (order.get(e["cells"].get(last, {}).get("status", "na"), 3), e["title"] or ""))
    for e in rows:
        e["cells"] = [e["cells"].get(rid) for rid in run_ids]
    changes = []
    if len(run_ids) > 1:
        prev = run_ids[-2]
        for e in rows:
            a, b = e["cells"][-2], e["cells"][-1]
            if a and b and a["status"] != b["status"]:
                good = order.get(b["status"], 3) > order.get(a["status"], 3) and b["status"] in ("pass",) or \
                    (a["status"] in ("fail", "warn") and b["status"] == "pass")
                bad = b["status"] in ("fail", "warn") and a["status"] in ("pass",) or \
                    (a["status"] == "warn" and b["status"] == "fail")
                changes.append({"check_id": e["check_id"], "title": e["title"], "old": a["status"], "new": b["status"],
                                "kind": "fixed" if good else "regress" if bad else "other", "summary": b["summary"]})
        changes.sort(key=lambda c: {"regress": 0, "fixed": 1, "other": 2}[c["kind"]])
        prev_label = labels.get(prev, prev)
    else:
        prev_label = ""
    return {"runs": [{"run_id": rid, "label": labels.get(rid, rid)} for rid in run_ids], "rows": rows,
            "changes": changes, "prev_label": prev_label}


def check_detail(db: sqlite3.Connection, run_id: str, network: str, check_id: str) -> dict | None:
    r = db.execute("SELECT * FROM check_results WHERE run_id=? AND network=? AND check_id=?",
                   (run_id, network, check_id)).fetchone()
    if not r:
        return None
    items = json.loads(r["items"] or "[]")
    return {"run_id": run_id, "network": network, "check_id": check_id, "title": r["title"], "status": r["status"],
            "summary": r["summary"], "source": r["source"],
            "failed": [i for i in items if not i.get("ok")][:200], "ok_count": sum(1 for i in items if i.get("ok")),
            "total": len(items)}


def check_history(db: sqlite3.Connection, network: str, check_id: str, limit: int = 40) -> list[dict]:
    rows = db.execute(
        "SELECT cr.run_id, r.started_at, cr.status, cr.summary FROM check_results cr JOIN runs r ON r.run_id=cr.run_id "
        "WHERE cr.network=? AND cr.check_id=? ORDER BY r.started_at DESC LIMIT ?", (network, check_id, limit)).fetchall()
    return [{"run_id": r["run_id"], "label": _ts_label(r["started_at"]), "status": r["status"], "summary": r["summary"]}
            for r in reversed(rows)]


# ------------------------------------------------------------------ по типам страниц и история URL

def by_page_type(db: sqlite3.Connection, run_id: str, network: str | None = None) -> list[dict]:
    nw = " AND network=?" if network else ""
    args = [run_id] + ([network] if network else [])
    rows = db.execute(
        "SELECT network, page_type, COUNT(*) n, "
        "SUM(CASE WHEN status BETWEEN 200 AND 299 AND (error IS NULL OR error='') THEN 1 ELSE 0 END) ok, "
        "AVG(CASE WHEN mode='raw' THEN ttfb_ms END) ttfb, AVG(bytes) b, "
        "SUM(CASE WHEN suspect IS NOT NULL AND suspect!='' THEN 1 ELSE 0 END) susp "
        f"FROM pages WHERE run_id=?{nw} GROUP BY network, page_type ORDER BY network, page_type", args).fetchall()
    return [{"network": r["network"], "page_type": r["page_type"], "n": r["n"], "ok": r["ok"] or 0,
             "ok_share": round(100 * (r["ok"] or 0) / r["n"], 1) if r["n"] else None,
             "ttfb": round(r["ttfb"]) if r["ttfb"] is not None else None,
             "kb": round(r["b"] / 1024, 1) if r["b"] is not None else None, "susp": r["susp"] or 0} for r in rows]


def url_history(db: sqlite3.Connection, network: str, source_url: str, limit: int = 60) -> dict:
    """Статус/скорость/размер одного URL справочника по запускам (вариант raw → bot, иначе любой)."""
    rows = db.execute(
        "SELECT p.run_id, r.started_at, p.variant, p.status, p.ttfb_ms, p.total_ms, p.bytes, p.suspect, p.error, "
        "p.final_url, p.has_title, p.has_h1 FROM pages p JOIN runs r ON r.run_id=p.run_id "
        "WHERE p.network=? AND p.source_url=? ORDER BY r.started_at DESC, p.id DESC LIMIT ?",
        (network, source_url, limit * 4)).fetchall()
    per_run: dict[str, dict] = {}
    for r in rows:
        e = per_run.setdefault(r["run_id"], {"run_id": r["run_id"], "label": _ts_label(r["started_at"]),
                                             "started_at": r["started_at"], "variants": {}})
        e["variants"][r["variant"] or "?"] = {
            "status": r["status"], "ttfb": round(r["ttfb_ms"]) if r["ttfb_ms"] is not None else None,
            "kb": round(r["bytes"] / 1024, 1) if r["bytes"] is not None else None, "suspect": r["suspect"] or "",
            "error": r["error"] or "", "final_url": r["final_url"], "title": r["has_title"], "h1": r["has_h1"]}
    runs = sorted(per_run.values(), key=lambda e: e["started_at"])[-limit:]
    return {"network": network, "url": source_url, "runs": runs}


# ================================================================== новый дашборд: точки, блоки, период

def _dt(iso: str | None):
    """Момент времени (aware, локальная зона для «наивных» значений) или None."""
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def _in_period(d, date_from: str | None, date_to: str | None) -> bool:
    if d is None:
        return True
    day = d.astimezone().date().isoformat()
    return (not date_from or day >= date_from) and (not date_to or day <= date_to)


def points(db: sqlite3.Connection, network: str | None, date_from: str | None = None, date_to: str | None = None,
           limit: int = 200) -> list[dict]:
    """Точки времени: запуски сбора + (для выбранной сети) выгрузки краула Screaming Frog, по возрастанию времени."""
    from . import crawl
    pts = []
    for r in list_runs(db, limit, network):
        d = _dt(r["started_at"])
        if _in_period(d, date_from, date_to):
            pts.append({"key": "run:" + r["run_id"], "kind": "run", "id": r["run_id"], "ts": r["started_at"], "_d": d,
                        "label": r["label"], "limit": r["limit"], "types": r["types"], "variants": r["variants"],
                        "probes": r["probes"], "networks": r["networks"]})
    if network:
        for c in crawl.imports(db, network, limit):
            d = _dt(c["taken_at"])
            if _in_period(d, date_from, date_to):
                pts.append({"key": f"crawl:{c['id']}", "kind": "crawl", "id": c["id"], "ts": c["taken_at"], "_d": d,
                            "label": _ts_label(c["taken_at"]) + " ◆", "rows": c["rows"], "title": c.get("label") or "",
                            "source_file": c.get("source_file") or ""})
    pts.sort(key=lambda p: p["_d"].timestamp() if p["_d"] else 0)
    for p in pts:
        p.pop("_d", None)
    return pts


def _check_values(db: sqlite3.Connection, run_ids: list[str], network: str | None, idx: dict[str, dict]) -> dict:
    """{run_id: {"check:<id>": %, "block:<имя>": %}}: доля «хорошего» по пункту; балл блока = пройдено / оценённые."""
    out: dict[str, dict] = {rid: {} for rid in run_ids}
    if not run_ids:
        return out
    nw = " AND network=?" if network else ""
    per_check: dict = defaultdict(lambda: defaultdict(list))
    per_block: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in db.execute(f"SELECT run_id, check_id, status, stats FROM check_results WHERE run_id IN {_in(run_ids)}{nw}",
                        run_ids + ([network] if network else [])):
        v = None
        try:
            st = json.loads(r["stats"]) if r["stats"] else None
        except ValueError:
            st = None
        if st and st.get("total"):
            v = round(100 * (1 - min(st.get("bad", 0), st["total"]) / st["total"]), 2)
        elif r["status"] in ("pass", "warn", "fail"):
            v = {"pass": 100.0, "warn": 50.0, "fail": 0.0}[r["status"]]
        if v is not None:
            per_check[r["run_id"]][r["check_id"]].append(v)
        if r["status"] in ("pass", "warn", "fail"):
            b = (idx.get(r["check_id"]) or {}).get("block") or "Прочее"
            cell = per_block[r["run_id"]][b]
            cell[1] += 1
            cell[0] += 1 if r["status"] == "pass" else 0
    for rid in run_ids:
        for cid, vals in per_check[rid].items():
            out[rid]["check:" + cid] = round(sum(vals) / len(vals), 2)
        for b, (ok, n) in per_block[rid].items():
            out[rid]["block:" + b] = round(100 * ok / n, 2)
    return out


def meta2(mid: str, idx: dict[str, dict] | None = None) -> dict:
    """Описание метрики для дерева и карточек: id, название, единица, «лучше», блок."""
    from . import blocks, crawl
    idx = idx if idx is not None else blocks.check_index()
    if mid.startswith("block:"):
        return {"id": mid, "label": "Балл блока: доля пройденных проверок", "unit": "%", "good": "up", "group": mid[6:], "kind": "score"}
    if mid.startswith("check:"):
        c = idx.get(mid[6:]) or {}
        return {"id": mid, "label": c.get("title") or mid[6:], "unit": "%", "good": "up", "group": c.get("block") or "Прочее",
                "kind": "check", "priority": c.get("priority", "")}
    if mid in crawl.CATALOG:
        name, unit, good = crawl.CATALOG[mid]
        return {"id": mid, "label": name, "unit": unit, "good": good, "group": "Итоговый краул", "kind": "crawl"}
    d = meta(mid)
    d["group"], d["kind"] = blocks.metric_block(mid), "metric"
    return d


def board(db: sqlite3.Connection, network: str | None = None, date_from: str | None = None, date_to: str | None = None,
          limit: int = 200) -> dict:
    """Данные нового дашборда: точки (запуски + краулы), ряды всех метрик, дерево блоков и карточки с динамикой."""
    from . import blocks, crawl
    idx = blocks.check_index()
    pts = points(db, network, date_from, date_to, limit)
    run_ids = [p["id"] for p in pts if p["kind"] == "run"]
    base = collect_metrics(db, run_ids, network)
    chk = _check_values(db, run_ids, network, idx)
    series: dict[str, list] = {}

    def put(i: int, mid: str, v) -> None:
        if v is None:
            return
        series.setdefault(mid, [None] * len(pts))[i] = v

    for i, p in enumerate(pts):
        if p["kind"] == "run":
            for mid, v in base.get(p["id"], {}).items():
                put(i, mid, v)
            for mid, v in chk.get(p["id"], {}).items():
                put(i, mid, v)
        else:
            for mid, v in crawl.metrics(db, p["id"]).items():
                put(i, mid, v)
    metas = {mid: meta2(mid, idx) for mid in series}
    order = [blocks.SUMMARY, *blocks.block_names(), blocks.OTHER]
    groups: dict[str, dict] = {}
    for mid, m in metas.items():
        g = groups.setdefault(m["group"], {"name": m["group"], "score": None, "metrics": [], "checks": []})
        if m["kind"] == "score":
            g["score"] = m
        elif m["kind"] == "check":
            g["checks"].append(m)
        else:
            g["metrics"].append(m)
    for name in list(groups):
        if name not in order:
            order.append(name)
    tree = [groups[n] for n in order if n in groups]
    cards = {}
    for mid, vals in series.items():
        present = [(i, v) for i, v in enumerate(vals) if v is not None]
        li, last = present[-1]
        prev = present[-2][1] if len(present) > 1 else None
        d = dict(metas[mid])
        delta = round(last - prev, 2) if prev is not None else None
        good = d.get("good")
        d.update(value=last, prev=prev, delta=delta, point=pts[li]["key"], latest=li == len(pts) - 1,
                 trend="flat" if not delta else "good" if good and ((delta > 0) == (good == "up")) else "bad" if good else "neutral")
        from .metric_exports import download_url, download_label
        d.update(download_url=download_url(mid, pts[li]["key"], network), download_label=download_label(mid))
        cards[mid] = d
    return {"points": pts, "series": series, "tree": tree, "cards": cards, "network": network or "",
            "from": date_from or "", "to": date_to or ""}


def board_csv(data: dict) -> str:
    """CSV «метрика × точка» для выгрузки (разделитель ;, как в русском Excel)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Блок", "Метрика", "Единица", *[f"{p['label']} ({'краул' if p['kind'] == 'crawl' else 'запуск'})" for p in data["points"]]])
    for g in data["tree"]:
        for m in [*([g["score"]] if g["score"] else []), *g["metrics"], *g["checks"]]:
            vals = data["series"].get(m["id"], [])
            w.writerow([g["name"], m["label"], m["unit"], *["" if v is None else str(v).replace(".", ",") for v in vals]])
    return buf.getvalue()


# ================================================================== проверки по блокам (аккордеон)

STATUS_ORDER = ("fail", "warn", "skipped", "na", "pass")


def run_ids_with_checks(db: sqlite3.Connection, network: str, limit: int = 40) -> list[dict]:
    rows = db.execute("SELECT cr.run_id, r.started_at FROM check_results cr JOIN runs r ON r.run_id=cr.run_id "
                      "WHERE cr.network=? GROUP BY cr.run_id ORDER BY r.started_at DESC LIMIT ?", (network, limit)).fetchall()
    return [{"run_id": r["run_id"], "label": _ts_label(r["started_at"])} for r in rows]


def checks_view(db: sqlite3.Connection, network: str, run_id: str | None = None, hist: int = 12) -> dict:
    """Проверки выбранного запуска, сгруппированные по блокам чек-листа; к каждой — мини-история статусов."""
    from . import blocks
    avail = run_ids_with_checks(db, network)
    ids = [a["run_id"] for a in avail]
    if not ids:
        return {"run_id": None, "runs": [], "blocks": [], "changes": [], "totals": {}}
    run_id = run_id if run_id in ids else ids[0]
    matrix = checks_matrix(db, network, hist)
    hmap = {r["check_id"]: r["cells"] for r in matrix["rows"]}
    hruns = [r["run_id"] for r in matrix["runs"]]
    res = {}
    for r in db.execute("SELECT check_id, title, status, summary, source, stats FROM check_results WHERE run_id=? AND network=?",
                        (run_id, network)):
        try:
            st = json.loads(r["stats"]) if r["stats"] else {}
        except ValueError:
            st = {}
        res[r["check_id"]] = {"title": r["title"], "status": r["status"], "summary": r["summary"], "source": r["source"],
                              "total": st.get("total"), "bad": st.get("bad")}
    out: dict[str, list] = {}
    seen = set()
    for row in blocks.checklist_rows():
        cid = row.get("check_id")
        b = row.get("block") or blocks.OTHER
        if cid and cid in res:
            seen.add(cid)
            x = res[cid]
            cells = hmap.get(cid) or []
            out.setdefault(b, []).append({
                "check_id": cid, "title": row["title"], "criterion": row.get("criterion") or "", "priority": row.get("priority") or "",
                "role": row.get("role_label") or "", "hours": row.get("hours"), "status": x["status"], "summary": x["summary"], "source": x["source"], "total": x["total"], "bad": x["bad"],
                "hist": [(c or {}).get("status", "none") for c in cells], "manual": False})
        else:
            out.setdefault(b, []).append({
                "check_id": cid or "", "title": row["title"], "criterion": row.get("criterion") or "", "priority": row.get("priority") or "",
                "role": "", "hours": None, "status": "manual", "summary": "ручной пункт — автоматической проверки нет" if not cid else "проверка в этом запуске не считалась",
                "source": "", "total": None, "bad": None, "hist": [], "manual": True})
    for cid, x in res.items():
        if cid not in seen:
            out.setdefault(blocks.OTHER, []).append({"check_id": cid, "title": x["title"], "criterion": "", "priority": "",
                                                     "status": x["status"], "summary": x["summary"], "source": x["source"],
                                                     "total": x["total"], "bad": x["bad"], "hist": [], "manual": False})
    blist = []
    totals: dict[str, int] = defaultdict(int)
    for name in [*blocks.block_names(), blocks.OTHER]:
        items = out.get(name)
        if not items:
            continue
        cnt: dict[str, int] = defaultdict(int)
        for it in items:
            cnt[it["status"]] += 1
            totals[it["status"]] += 1
        judged = cnt["pass"] + cnt["warn"] + cnt["fail"]
        blist.append({"name": name, "rows": items, "counts": dict(cnt),
                      "score": round(100 * cnt["pass"] / judged) if judged else None,
                      "worst": next((s for s in ("fail", "warn") if cnt[s]), "pass" if judged else "na")})
    changes = matrix["changes"] if hruns and run_id == hruns[-1] else []
    return {"run_id": run_id, "runs": avail, "blocks": blist, "changes": changes, "totals": dict(totals),
            "prev_label": matrix.get("prev_label", ""), "hist_runs": matrix["runs"]}


# ================================================================== обзор и страница блока (дашборд v3)

_UNIT_ORDER = ("%", "шт", "мс", "КБ")
_NO_CHART = {"pages_total", "probes_total", "crawl_pages_total", "crawl_html_total"}  # объёмы: масштаб глушит остальные ряды, есть в таблице
_STATUS_KEYS = ("pass", "warn", "fail")


def _block_counts(db: sqlite3.Connection, run_ids: list[str], network: str | None, idx: dict[str, dict]) -> dict:
    """{run_id: {блок: {pass/warn/fail/skipped: n}}} по check_results."""
    out: dict = {rid: defaultdict(lambda: defaultdict(int)) for rid in run_ids}
    if not run_ids:
        return out
    nw = " AND network=?" if network else ""
    for r in db.execute(f"SELECT run_id, check_id, status FROM check_results WHERE run_id IN {_in(run_ids)}{nw}",
                        run_ids + ([network] if network else [])):
        b = (idx.get(r["check_id"]) or {}).get("block") or "Прочее"
        out[r["run_id"]][b][r["status"]] += 1
    return out


def _ds(label: str, data: list, color: str, *, kind: str = "line", axis: str = "y", stack: str | None = None,
        dash: bool = False) -> dict:
    return {"label": label[:70], "data": data, "color": color, "type": kind, "axis": axis, "stack": stack, "dash": dash}


def metric_charts(ms: list[dict], series: dict, title: str = "") -> list[dict]:
    """Графики для несовместимых единиц: % + шт на одном графике с двумя осями (шт — столбцами), мс + КБ — тоже.
    На одной оси не больше 4 рядов, остальное уходит в следующий график."""
    by_unit: dict[str, list[dict]] = {}
    for m in ms:
        if m["id"] in series and m["id"] not in _NO_CHART:
            by_unit.setdefault(m.get("unit") or "", []).append(m)
    by_unit = {u: v for u, v in by_unit.items() if len(v) <= 6}  # слишком много рядов — лучше таблица со спарклайнами
    units = sorted(by_unit, key=lambda u: _UNIT_ORDER.index(u) if u in _UNIT_ORDER else 99)
    pairs: list[list[str]] = []
    for a, b in (("%", "шт"), ("мс", "КБ")):
        if a in units and b in units:
            pairs.append([a, b])
    rest = [u for u in units if not any(u in p for p in pairs)]
    pairs += [rest[i:i + 2] for i in range(0, len(rest), 2)]
    cols = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"]
    charts: list[dict] = []
    for pair in pairs:
        left = by_unit[pair[0]]
        right = by_unit[pair[1]] if len(pair) > 1 else []
        n = max(-(-len(left) // 4), -(-len(right) // 4), 1)
        for k in range(n):
            L, R = left[k * 4:(k + 1) * 4], right[k * 4:(k + 1) * 4]
            dss = [_ds(m["label"], series[m["id"]], cols[i % 6]) for i, m in enumerate(L)]
            bars = len(pair) > 1 and pair[1] == "шт"
            dss += [_ds(m["label"] + (" (справа)" if not bars else ""), series[m["id"]], cols[(i + 3) % 6],
                        kind="bar" if bars else "line", axis="y1", dash=not bars) for i, m in enumerate(R)]
            name = pair[0] + (" и " + pair[1] if len(pair) > 1 else "")
            charts.append({"title": (title + ", " if title else "") + name + (f" ({k + 1})" if n > 1 else ""),
                           "datasets": [d for d in dss if d["data"]], "unit": pair[0], "unit1": pair[1] if len(pair) > 1 else "",
                           "max": 100 if pair[0] == "%" else None, "max1": 100 if len(pair) > 1 and pair[1] == "%" else None})
    return charts


def _spark(vals: list, n: int = 12) -> list:
    return [v for v in vals][-n:]


def _probe_block_series(db: sqlite3.Connection, pts: list[dict], network: str | None, kinds: list[str]) -> tuple[list, list]:
    """Доля пройденных тестов адресов блока по точкам + число непройденных (None там, где тестов нет, и для краулов)."""
    run_ids = [p["id"] for p in pts if p["kind"] == "run"]
    if not run_ids or not kinds:
        return [None] * len(pts), [None] * len(pts)
    nw = " AND network=?" if network else ""
    agg: dict[str, list[int]] = {}
    for r in db.execute(f"SELECT run_id, COUNT(*) n, SUM(CASE WHEN verdict='pass' THEN 1 ELSE 0 END) p FROM probes "
                        f"WHERE run_id IN {_in(run_ids)}{nw} AND kind IN {_in(kinds)} GROUP BY run_id",
                        run_ids + ([network] if network else []) + kinds):
        agg[r["run_id"]] = [r["n"], r["p"] or 0]
    share, bad = [], []
    for p in pts:
        a = agg.get(p["id"]) if p["kind"] == "run" else None
        share.append(round(100 * a[1] / a[0], 2) if a and a[0] else None)
        bad.append(a[0] - a[1] if a and a[0] else None)
    return share, bad


def _delta(vals: list) -> tuple:
    present = [v for v in vals if v is not None]
    if not present:
        return None, None
    return present[-1], (round(present[-1] - present[-2], 2) if len(present) > 1 else None)


def _worst_checks(db: sqlite3.Connection, run_id: str | None, network: str | None, block: str, idx: dict[str, dict], limit: int = 3) -> list[dict]:
    if not run_id:
        return []
    nw = " AND network=?" if network else ""
    rows = db.execute(f"SELECT check_id, status, summary FROM check_results WHERE run_id=?{nw} AND status IN ('fail','warn')",
                      [run_id] + ([network] if network else [])).fetchall()
    out = [{"id": r["check_id"], "title": (idx.get(r["check_id"]) or {}).get("title") or r["check_id"], "status": r["status"], "summary": r["summary"]}
           for r in rows if ((idx.get(r["check_id"]) or {}).get("block") or "Прочее") == block]
    out.sort(key=lambda x: (x["status"] != "fail", x["title"]))
    return out[:limit]


def overview(db: sqlite3.Connection, network: str | None = None, date_from: str | None = None, date_to: str | None = None) -> dict:
    """Главный экран: итоговые числа, плитки блоков (балл, динамика, проблемы), светофор «блок × запуск», 1–2 сводных графика."""
    from . import blocks, explain
    data = board(db, network, date_from, date_to)
    idx = blocks.check_index()
    pts, series, cards = data["points"], data["series"], data["cards"]
    run_pts = [(i, p) for i, p in enumerate(pts) if p["kind"] == "run"]
    run_ids = [p["id"] for _, p in run_pts]
    counts = _block_counts(db, run_ids, network, idx)
    last_rid = next((rid for rid in reversed(run_ids) if counts.get(rid)), None)
    prev_rid = next((rid for rid in reversed(run_ids[:run_ids.index(last_rid)]) if counts.get(rid)), None) if last_rid else None
    tiles = []
    for g in data["tree"]:
        if g["name"] == blocks.SUMMARY:
            continue
        head = g["score"] or next((m for m in g["metrics"] if m.get("unit") == "%" and m.get("good") == "up"), None) \
            or (g["metrics"][0] if g["metrics"] else None)
        if not head:
            continue
        c = cards.get(head["id"]) or {}
        cn = dict(counts.get(last_rid, {}).get(g["name"], {})) if last_rid else {}
        kinds = [k for k, b in blocks._PROBE_BLOCK.items() if b == g["name"]]
        t = {"name": g["name"], "help": explain.block_help(g["name"]), "metric_id": head["id"],
             "metric_label": "Балл блока" if head.get("kind") == "score" else head["label"], "unit": head.get("unit", ""),
             "is_score": head.get("kind") == "score", "value": c.get("value"), "delta": c.get("delta"), "trend": c.get("trend", "flat"),
             "good": head.get("good"), "heat": series.get(head["id"], []), "counts": cn, "n_checks": len(g["checks"]),
             "worst": _worst_checks(db, last_rid, network, g["name"], idx), "tests": bool(kinds)}
        if kinds and not g["score"] and all(m["id"].startswith(PROBE_PREFIX) for m in g["metrics"]):  # блок из тестов адресов: итог = доля пройденных по всем видам тестов блока
            share, bad = _probe_block_series(db, pts, network, kinds)
            v, d = _delta(share)
            if v is not None:
                t.update(metric_label="Тесты адресов: доля пройденных", unit="%", value=v, delta=d, good="up", heat=share,
                         trend="flat" if not d else ("good" if d > 0 else "bad"), counts={"fail": bad[-1] if bad[-1] is not None else 0})
        t["spark"] = _spark(t["heat"])
        if g["name"] == "Итоговый краул":
            from .metric_exports import download_url
            crawl_point = next((p["key"] for p in reversed(pts) if p["kind"] == "crawl"), None)
            t["crawl_download_url"] = download_url("crawl_problems", crawl_point, network)
        tiles.append(t)
    pct = [t for t in tiles if t["unit"] == "%" and t["value"] is not None]
    rest = [t for t in tiles if t not in pct]
    tiles = sorted(pct, key=lambda t: t["value"]) + rest
    total = defaultdict(int)
    if last_rid:
        for b, cn in counts[last_rid].items():
            for s, n in cn.items():
                total[s] += n
    top = {k: cards.get(k) for k in ("checks_score", "probes_pass_share", "pages_ok_share", "ttfb_p95") if cards.get(k)}
    changes = []
    if network:
        try:
            changes = checks_matrix(db, network)["changes"]
        except sqlite3.Error:
            changes = []
    for ch in changes:
        ch["block"] = (idx.get(ch["check_id"]) or {}).get("block") or "Прочее"
    # сводный график: баллы + «не пройдено» столбцами справа
    ds = []
    for key, col in (("checks_score", "--c1"), ("probes_pass_share", "--c2"), ("pages_ok_share", "--c6")):
        if key in series:
            ds.append(_ds(label(key) if key not in CATALOG else CATALOG[key][0], series[key], col))
    if "checks_fail" in series:
        ds.append(_ds("Проверки не пройдены, шт (справа)", series["checks_fail"], "--bad", kind="bar", axis="y1"))
    chart = {"title": "Общая динамика", "datasets": ds, "unit": "%", "unit1": "шт", "max": 100, "max1": None} if ds else None
    run_label = next((p["label"] for _, p in run_pts if p["id"] == last_rid), "")
    return {"board": data, "tiles": tiles, "top": top, "totals": dict(total), "run_id": last_rid, "prev_run_id": prev_rid,
            "run_label": run_label, "changes": changes, "chart": chart,
            "heat_labels": [p["label"] for p in pts][-8:], "points": pts, "network": network or "", "from": date_from or "", "to": date_to or ""}


def block_view(db: sqlite3.Connection, network: str, name: str, date_from: str | None = None, date_to: str | None = None) -> dict | None:
    """Детальный дашборд блока: пункты с динамикой, метрики с пояснениями, тесты адресов, графики с совмещёнными осями."""
    from . import blocks, explain
    data = board(db, network or None, date_from, date_to)
    g = next((x for x in data["tree"] if x["name"] == name), None)
    if not g:
        return None
    idx = blocks.check_index()
    pts, series, cards = data["points"], data["series"], data["cards"]
    run_ids = [p["id"] for p in pts if p["kind"] == "run"]
    counts = _block_counts(db, run_ids, network or None, idx)
    cnt_series = {s: [(counts.get(p["id"], {}).get(name, {}).get(s) if p["kind"] == "run" and counts.get(p["id"], {}).get(name) else None)
                      for p in pts] for s in _STATUS_KEYS}
    last_rid = run_ids[-1] if run_ids else None
    rows: list[dict] = []
    view_run = None
    if network and g["checks"]:
        view = checks_view(db, network, last_rid)
        view_run = view.get("run_id")
        b = next((x for x in view["blocks"] if x["name"] == name), None)
        order = {"fail": 0, "warn": 1, "skipped": 2, "na": 3, "manual": 4, "pass": 5}
        for r in (b["rows"] if b else []):
            sid = "check:" + r["check_id"]
            c = cards.get(sid) or {}
            r = dict(r)
            r.update(value=c.get("value"), delta=c.get("delta"), spark=_spark(series.get(sid, [])),
                     help=explain.metric_help(sid, r["title"], r.get("criterion", "")), has_urls=bool(r.get("total")))
            rows.append(r)
        rows.sort(key=lambda r: (order.get(r["status"], 9), r["value"] if r["value"] is not None else 101))
    metrics = []
    for m in [m for m in g["metrics"] if not m["id"].startswith(PROBE_PREFIX)]:
        c = cards.get(m["id"]) or {}
        metrics.append({**m, "value": c.get("value"), "delta": c.get("delta"), "trend": c.get("trend", "flat"),
                        "download_url": c.get("download_url"), "download_label": c.get("download_label"),
                        "spark": _spark(series.get(m["id"], [])), "help": explain.metric_help(m["id"], m["label"])})
    tests = []
    kinds = [k for k, b in blocks._PROBE_BLOCK.items() if b == name]
    if kinds and last_rid:
        nw = " AND network=?" if network else ""
        for r in db.execute(f"SELECT kind, COUNT(*) n, SUM(CASE WHEN verdict='pass' THEN 1 ELSE 0 END) p FROM probes "
                            f"WHERE run_id=?{nw} GROUP BY kind", [last_rid] + ([network] if network else [])):
            if r["kind"] in kinds:
                n, p = r["n"], r["p"] or 0
                tests.append({"kind": r["kind"], "name": explain.probe_name(r["kind"]), "what": explain.probe_what(r["kind"]),
                              "total": n, "passed": p, "failed": n - p, "share": round(100 * p / n, 1) if n else None,
                              "spark": _spark(series.get("probe:" + r["kind"], []))})
        tests.sort(key=lambda t: (t["share"] if t["share"] is not None else 101))
    pages_err = 0
    if name == "Коды ответа" and last_rid:
        nw = " AND network=?" if network else ""
        pages_err = db.execute(f"SELECT COUNT(*) FROM pages WHERE run_id=?{nw} AND (status>=400 OR (error IS NOT NULL AND error!='') "
                               f"OR (suspect IS NOT NULL AND suspect!=''))", [last_rid] + ([network] if network else [])).fetchone()[0]
    charts: list[dict] = []
    sc = g["score"]["id"] if g["score"] else None
    if sc and sc in series:
        ds = [_ds("Балл блока, %", series[sc], "--c1")]
        for s, col, lbl in (("fail", "--bad", "не пройдено"), ("warn", "--warn", "внимание"), ("pass", "--good", "пройдено")):
            if any(v is not None for v in cnt_series[s]):
                ds.append(_ds(f"Проверок: {lbl} (справа)", cnt_series[s], col, kind="bar", axis="y1", stack="s"))
        charts.append({"title": "Балл блока и статусы проверок", "datasets": ds, "unit": "%", "unit1": "шт", "max": 100, "max1": None,
                       "stacked1": True, "note": "Линия — балл (доля пройденных), столбцы — сколько проверок в каком статусе."})
    if kinds and len(tests) >= 2:
        share, bad = _probe_block_series(db, pts, network or None, kinds)
        if any(v is not None for v in share):
            charts.append({"title": "Тесты адресов: доля пройденных и число провалов", "unit": "%", "unit1": "шт", "max": 100, "max1": None,
                           "note": "Линия — доля тестов с ожидаемым ответом, столбцы — сколько тестов не прошло.",
                           "datasets": [_ds("Доля пройденных, %", share, "--c1"), _ds("Не прошло, шт (справа)", bad, "--bad", kind="bar", axis="y1")]})
        if tests:
            charts.append({"kind": "hbar", "title": "Доля пройденных по видам тестов, последний запуск",
                           "labels": [t["name"] for t in tests], "values": [t["share"] for t in tests], "unit": "%",
                           "note": "Красные — почти всё не проходит, жёлтые — часть, зелёные — норма."})
    charts += metric_charts([{**m} for m in metrics], series, "")
    weak = [r for r in rows if r.get("value") is not None and r["status"] in ("fail", "warn")][:5]
    if len(weak) > 1:
        charts.append({"title": "Самые слабые проверки, %", "unit": "%", "max": 100, "note": "Доля URL, прошедших проверку.",
                       "datasets": [_ds(r["title"], series["check:" + r["check_id"]], f"--c{i % 6 + 1}") for i, r in enumerate(weak)]})
    def _multi(c: dict) -> bool:
        return c.get("kind") == "hbar" or any(sum(1 for x in d["data"] if x is not None) >= 2 for d in c.get("datasets", []))

    charts = charts[:1] + [c for c in charts[1:] if _multi(c)]  # графики из одной точки бесполезны — оставляем только первый
    if charts and not _multi(charts[0]):
        charts[0]["note"] = "Динамика появится после следующих запусков: пока доступен один замер."
    last_counts = dict(counts.get(view_run or last_rid, {}).get(name, {})) if (view_run or last_rid) else {}
    from .metric_exports import download_url
    crawl_point = next((p["key"] for p in reversed(pts) if p["kind"] == "crawl"), None)
    return {"name": name, "help": explain.block_help(name), "network": network or "", "run_id": view_run or last_rid,
            "score": cards.get(sc) if sc else None, "counts": last_counts, "rows": rows, "metrics": metrics, "tests": tests,
            "pages_err": pages_err, "charts": charts, "points": pts, "n_checks": len(g["checks"]),
            "from": date_from or "", "to": date_to or "",
            "crawl_download_url": download_url("crawl_problems", crawl_point, network) if name == "Итоговый краул" else None}
