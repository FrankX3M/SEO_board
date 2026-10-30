"""Страницы-сироты: URL, которые поисковики держат в индексе (Яндекс Вебмастер, Google Search Console), но краул сайта до них не дошёл
или добирается слишком долго.

Сравнение: выгрузка(и) индекса (indexed.py) × краул Screaming Frog (crawl.py), ключ — нормализованный адрес (indexed.normalize).
Каждому URL из индекса присваивается ОДНА категория (первая подходящая):

  not_in_crawl   в индексе, но в краулe адреса нет совсем: на него не ведёт ни одна ссылка, достижимая от стартовой страницы (сирота в строгом смысле);
  stale          то же, но «живая» проверка показала, что сайт уже не отдаёт 200 (404 / редирект) — это не сирота, а устаревшая запись в индексе;
  bad_status     краул нашёл URL, но он отвечает 3xx / 4xx / 5xx — индекс держит неактуальный адрес;
  no_inlinks     краулер видит страницу (например, из sitemap), но внутренних ссылок на неё нет;
  deep           путь от главной длиннее MAX_DEPTH кликов — «витиеватый»;
  non_indexable  страница в индексе, а краул считает её неиндексируемой (noindex, canonical на другой адрес) — расхождение сигналов;
  ok             найдена краулом, ссылки есть, глубина в норме.

«Сироты» (в узком смысле задачи) — not_in_crawl, no_inlinks и deep: ORPHAN_CATS.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import crawl as cr
from . import indexed as ix

CATEGORIES: dict[str, tuple[str, str]] = {
    "not_in_crawl": ("Нет в краулe", "В индексе есть, краул адреса не нашёл: нет ссылок на страницу (или краул неполный)"),
    "stale": ("Устарел в индексе", "Нет в краулe, и сайт уже не отдаёт 200 (404 или редирект) — убрать из индекса, а не линковать"),
    "bad_status": ("Не 200 в краулe", "Краул нашёл URL, но он отвечает 3xx / 4xx / 5xx"),
    "no_inlinks": ("Нет входящих ссылок", "Краулер видит страницу, но внутренних ссылок на неё нет (найдена, например, через sitemap)"),
    "deep": ("Слишком глубоко", "Путь от главной длиннее порога MAX_DEPTH"),
    "non_indexable": ("Расхождение сигналов", "В индексе, а краул считает страницу неиндексируемой (noindex / canonical на другой адрес)"),
    "ok": ("В порядке", "Найдена краулом, ссылки есть, глубина в норме"),
}
ORPHAN_CATS = ("not_in_crawl", "no_inlinks", "deep")
PROBLEM_CATS = ("not_in_crawl", "stale", "bad_status", "no_inlinks", "deep", "non_indexable")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orphan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, network TEXT NOT NULL, created_at TEXT NOT NULL,
  crawl_import_id INTEGER, ywm_import_id INTEGER, gsc_import_id INTEGER, max_depth INTEGER, summary TEXT
);
CREATE INDEX IF NOT EXISTS ix_orphan_runs ON orphan_runs(network, created_at);
CREATE TABLE IF NOT EXISTS orphan_rows (
  run_id INTEGER NOT NULL, url TEXT NOT NULL, norm TEXT NOT NULL, section TEXT, in_ywm INTEGER, in_gsc INTEGER,
  category TEXT NOT NULL, crawl_url TEXT, crawl_status INTEGER, indexability TEXT, indexability_status TEXT, redirect_url TEXT,
  depth INTEGER, inlinks INTEGER, unique_inlinks INTEGER, title TEXT, last_access TEXT, clicks INTEGER, impressions INTEGER,
  probe_status INTEGER, probe_final TEXT
);
CREATE INDEX IF NOT EXISTS ix_orphan_rows ON orphan_rows(run_id, category);
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    ix.ensure_schema(db)
    cr.ensure_schema(db)
    db.executescript(SCHEMA)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- сравнение

def _is_html(ct: str | None) -> bool:
    return not ct or ct.lower().startswith("text/html")


def _crawl_rank(r) -> tuple:
    """Если одному ключу соответствуют несколько адресов краула (/a и /a/), берём «лучший»: HTML 200, потом меньшая глубина."""
    ok = 0 if (r["status"] == 200 and _is_html(r["content_type"])) else 1
    return ok, r["depth"] if r["depth"] is not None else 999, r["address"]


def compare(db: sqlite3.Connection, crawl_import_id: int, index_import_ids: dict[str, int], *, max_depth: int = 4,
            probes: dict[str, tuple[int | None, str | None]] | None = None) -> dict:
    """index_import_ids: {'ywm': id, 'gsc': id} (любой набор). Возвращает {'rows': [...], 'summary': {...}}; в БД ничего не пишет.

    probes — результаты живой проверки {norm: (status, final_url)} для адресов, которых нет в краулe."""
    ensure_schema(db)
    imp = db.execute("SELECT * FROM crawl_imports WHERE id=?", (crawl_import_id,)).fetchone()
    if not imp:
        raise ix.IndexedError("краул не найден")
    probes = probes or {}

    # адреса из индекса: norm -> запись
    pages: dict[str, dict] = {}
    totals: dict[str, int] = {}
    for src, iid in index_import_ids.items():
        n = 0
        for r in db.execute("SELECT url, norm, title, last_access, clicks, impressions FROM index_pages WHERE import_id=?", (iid,)):
            n += 1
            p = pages.setdefault(r["norm"], {"url": r["url"], "norm": r["norm"], "title": None, "last_access": None,
                                              "clicks": None, "impressions": None, "ywm": 0, "gsc": 0})
            p[src] = 1
            if src == "ywm":  # заголовок и дата обхода есть у Вебмастера, показы и клики — у GSC
                p["url"] = r["url"]
                p["title"] = r["title"] or p["title"]
                p["last_access"] = r["last_access"] or p["last_access"]
            else:
                p["clicks"] = r["clicks"] if r["clicks"] is not None else p["clicks"]
                p["impressions"] = r["impressions"] if r["impressions"] is not None else p["impressions"]
        totals[src] = n

    # краул: оставляем только то, что есть в индексе
    found: dict[str, sqlite3.Row] = {}
    crawl_rows = 0
    for r in db.execute("SELECT address, content_type, status, indexability, indexability_status, canonical, redirect_url, depth, "
                        "inlinks, unique_inlinks FROM crawl_pages WHERE import_id=?", (crawl_import_id,)):
        crawl_rows += 1
        k = ix.normalize(r["address"])
        if k in pages:
            cur = found.get(k)
            if cur is None or _crawl_rank(r) < _crawl_rank(cur):
                found[k] = r

    rows = []
    for k, p in pages.items():
        c = found.get(k)
        row = {"url": p["url"], "norm": k, "section": ix.section_of(k), "in_ywm": p["ywm"], "in_gsc": p["gsc"],
               "title": p["title"], "last_access": p["last_access"], "clicks": p["clicks"], "impressions": p["impressions"],
               "crawl_url": None, "crawl_status": None, "indexability": None, "indexability_status": None, "redirect_url": None,
               "depth": None, "inlinks": None, "unique_inlinks": None, "probe_status": None, "probe_final": None}
        if k in probes:
            row["probe_status"], row["probe_final"] = probes[k]
        if c is None:
            ps = row["probe_status"]
            row["category"] = "stale" if ps is not None and ps != 200 else "not_in_crawl"
        else:
            row.update(crawl_url=c["address"], crawl_status=c["status"], indexability=c["indexability"],
                       indexability_status=c["indexability_status"], redirect_url=c["redirect_url"], depth=c["depth"],
                       inlinks=c["inlinks"], unique_inlinks=c["unique_inlinks"])
            st = c["status"]
            if st is not None and st != 200:
                row["category"] = "bad_status"
            elif not (c["inlinks"] or 0) and (c["depth"] is None or c["depth"] > 0):
                row["category"] = "no_inlinks"
            elif c["depth"] is not None and c["depth"] > max_depth:
                row["category"] = "deep"
            elif c["indexability"] and c["indexability"] != "Indexable":
                row["category"] = "non_indexable"
            else:
                row["category"] = "ok"
        rows.append(row)

    by_cat = {k: 0 for k in CATEGORIES}
    by_src = {s: {k: 0 for k in CATEGORIES} for s in ("ywm", "gsc")}
    for r in rows:
        by_cat[r["category"]] += 1
        for s in ("ywm", "gsc"):
            if r[f"in_{s}"]:
                by_src[s][r["category"]] += 1
    n = len(rows)
    in_crawl = sum(1 for r in rows if r["crawl_url"])
    orphans = sum(by_cat[c] for c in ORPHAN_CATS)
    summary = {
        "index_total": n, "per_source_total": totals, "in_both": sum(1 for r in rows if r["in_ywm"] and r["in_gsc"]),
        "in_crawl": in_crawl, "coverage": round(100 * in_crawl / n, 1) if n else None,
        "by_category": by_cat, "by_source": by_src, "orphans": orphans, "orphans_share": round(100 * orphans / n, 1) if n else None,
        "crawl": {"id": crawl_import_id, "rows": crawl_rows, "taken_at": imp["taken_at"], "label": imp["label"]},
        "max_depth": max_depth, "warnings": [],
    }
    if n and summary["coverage"] is not None and summary["coverage"] < 50:
        summary["warnings"].append(
            f"Краул нашёл только {summary['coverage']}% URL из индекса. Прежде чем линковать «сирот», убедитесь, что краул полный: "
            "без лимита URL, с рендерингом JS, с учётом sitemap и со стартом с главной.")
    if n and crawl_rows and n > crawl_rows:
        summary["warnings"].append(f"В индексе ({n}) больше URL, чем в краулe ({crawl_rows}) — краул, вероятно, неполный.")
    if any(t >= ix.Ywm.MAX_URLS for s, t in totals.items() if s == "ywm"):
        summary["warnings"].append(f"Из Вебмастера получено {ix.Ywm.MAX_URLS} URL — это предел API; на большом сайте часть страниц "
                                   "не попала в сравнение. Дополните выгрузку файлом из интерфейса Вебмастера.")
    if "gsc" in index_import_ids and "ywm" not in index_import_ids:
        summary["warnings"].append("Для Google использованы страницы с показами в поиске: это нижняя граница индекса, "
                                   "проиндексированные страницы без показов в список не попали.")
    return {"rows": rows, "summary": summary}


# ---------------------------------------------------------------- хранение результата

def store(db: sqlite3.Connection, network: str, crawl_import_id: int, index_import_ids: dict[str, int], res: dict) -> int:
    ensure_schema(db)
    db.execute("BEGIN")
    try:
        cur = db.execute("INSERT INTO orphan_runs(network, created_at, crawl_import_id, ywm_import_id, gsc_import_id, max_depth, summary) "
                         "VALUES (?,?,?,?,?,?,?)", (network, now_iso(), crawl_import_id, index_import_ids.get("ywm"),
                                                     index_import_ids.get("gsc"), res["summary"]["max_depth"],
                                                     json.dumps(res["summary"], ensure_ascii=False)))
        rid = cur.lastrowid
        cols = ["url", "norm", "section", "in_ywm", "in_gsc", "category", "crawl_url", "crawl_status", "indexability",
                "indexability_status", "redirect_url", "depth", "inlinks", "unique_inlinks", "title", "last_access", "clicks",
                "impressions", "probe_status", "probe_final"]
        db.executemany(f"INSERT INTO orphan_rows(run_id, {', '.join(cols)}) VALUES ({','.join('?' * (len(cols) + 1))})",
                       ((rid, *(r[c] for c in cols)) for r in res["rows"]))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return rid


def delete_run(db: sqlite3.Connection, run_id: int) -> None:
    db.execute("BEGIN")
    db.execute("DELETE FROM orphan_rows WHERE run_id=?", (run_id,))
    db.execute("DELETE FROM orphan_runs WHERE id=?", (run_id,))
    db.execute("COMMIT")


def runs(db: sqlite3.Connection, network: str | None = None, limit: int = 30) -> list[dict]:
    ensure_schema(db)
    q = "SELECT * FROM orphan_runs" + (" WHERE network=?" if network else "") + " ORDER BY created_at DESC, id DESC LIMIT ?"
    out = []
    for r in db.execute(q, ([network] if network else []) + [limit]):
        d = dict(r)
        d["summary"] = json.loads(d["summary"] or "{}")
        out.append(d)
    return out


def get_run(db: sqlite3.Connection, run_id: int) -> dict | None:
    ensure_schema(db)
    r = db.execute("SELECT * FROM orphan_runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["summary"] = json.loads(d["summary"] or "{}")
    return d


def filter_sql(cat: str, src: str, q: str | None) -> tuple[str, list]:
    where, args = ["run_id=?"], []
    if cat == "orphans":
        where.append("category IN (%s)" % ",".join("?" * len(ORPHAN_CATS)))
        args += list(ORPHAN_CATS)
    elif cat == "problems":
        where.append("category IN (%s)" % ",".join("?" * len(PROBLEM_CATS)))
        args += list(PROBLEM_CATS)
    elif cat in CATEGORIES:
        where.append("category=?")
        args.append(cat)
    if src == "ywm":
        where.append("in_ywm=1")
    elif src == "gsc":
        where.append("in_gsc=1")
    elif src == "both":
        where.append("in_ywm=1 AND in_gsc=1")
    if q:
        where.append("url LIKE ?")
        args.append(f"%{q}%")
    return " AND ".join(where), args


def rows(db: sqlite3.Connection, run_id: int, cat: str = "orphans", src: str = "", q: str | None = None, limit: int = 100,
         offset: int = 0) -> tuple[list[dict], int]:
    where, args = filter_sql(cat, src, q)
    total = db.execute(f"SELECT COUNT(*) FROM orphan_rows WHERE {where}", [run_id, *args]).fetchone()[0]
    out = db.execute(f"SELECT * FROM orphan_rows WHERE {where} ORDER BY COALESCE(impressions,0) DESC, COALESCE(clicks,0) DESC, url "
                     f"LIMIT ? OFFSET ?", [run_id, *args, limit, offset]).fetchall()
    return [dict(r) for r in out], total


def sections(db: sqlite3.Connection, run_id: int, limit: int = 15) -> list[dict]:
    """Где сосредоточены сироты: разделы сайта (первый сегмент пути)."""
    ph = ",".join("?" * len(ORPHAN_CATS))
    q = (f"SELECT section, COUNT(*) AS n, SUM(category='not_in_crawl') AS not_in_crawl, SUM(category='no_inlinks') AS no_inlinks, "
         f"SUM(category='deep') AS deep FROM orphan_rows WHERE run_id=? AND category IN ({ph}) GROUP BY section ORDER BY n DESC LIMIT ?")
    return [dict(r) for r in db.execute(q, [run_id, *ORPHAN_CATS, limit])]


EXPORT_HEAD = ["URL", "Категория", "Яндекс Вебмастер", "Google (показы)", "Раздел", "Статус в краулe", "Глубина", "Входящих ссылок",
               "Уник. входящих", "Индексируемость в краулe", "Редирект в краулe", "Проверка сайта (код)", "Проверка сайта (куда)",
               "Показы Google", "Клики Google", "Заголовок", "Последний обход"]


def export_row(r: dict) -> list:
    return [r["url"], CATEGORIES[r["category"]][0], "да" if r["in_ywm"] else "", "да" if r["in_gsc"] else "", r["section"],
            r["crawl_status"] if r["crawl_status"] is not None else "нет в краулe", r["depth"], r["inlinks"], r["unique_inlinks"],
            r["indexability"] or "", r["redirect_url"] or "", r["probe_status"], r["probe_final"] or "", r["impressions"], r["clicks"],
            r["title"] or "", r["last_access"] or ""]


# ---------------------------------------------------------------- живая проверка адресов, которых нет в краулe

def probe_missing(db: sqlite3.Connection, settings, network: str, urls: list[tuple[str, str]], *, limit: int = 300,
                  progress=None) -> dict[str, tuple[int | None, str | None]]:
    """GET (без JS) для URL «нет в краулe» — с токеном сети и UA бота. Возвращает {norm: (статус первого ответа, куда ведёт)}.

    Статус 200 — страница жива (настоящая сирота); 3xx / 404 / 410 — запись в индексе устарела."""
    from .config import resolve_access
    from .rawfetch import fetch_raw, make_client

    net = settings.networks.get(network)
    if not net or not net.url:
        raise ix.IndexedError(f"для сети {network} не задан адрес прода")
    access = resolve_access(settings, net)
    todo = urls[:limit]
    out: dict[str, tuple[int | None, str | None]] = {}

    async def run():
        sem = asyncio.Semaphore(max(1, int(settings.concurrency or 4)))
        client = make_client(float(settings.timeout or 30), bool(settings.verify_tls))
        done = 0

        async def one(norm: str, url: str):
            nonlocal done
            async with sem:
                res = await fetch_raw(client, url, access)
            first = res.chain[0]["status"] if res.chain else res.status
            to = res.chain[0].get("location") if res.chain and 300 <= (first or 0) < 400 else None
            out[norm] = (first, to or (res.error if res.error else None))
            done += 1
            if progress:
                progress(done, len(todo))
        try:
            await asyncio.gather(*(one(n, u) for n, u in todo))
        finally:
            await client.aclose()

    asyncio.run(run())
    now = now_iso()
    db.executemany("INSERT OR REPLACE INTO orphan_probe(network, norm, url, status, final_url, error, checked_at) VALUES (?,?,?,?,?,?,?)",
                   [(network, n, dict(todo).get(n), s, f, None, now) for n, (s, f) in out.items()])
    return out


def saved_probes(db: sqlite3.Connection, network: str, max_age_days: int = 14) -> dict[str, tuple[int | None, str | None]]:
    ensure_schema(db)
    lim = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    return {r["norm"]: (r["status"], r["final_url"]) for r in
            db.execute("SELECT norm, status, final_url FROM orphan_probe WHERE network=? AND checked_at>=?", (network, lim))}


# ---------------------------------------------------------------- сценарий «целиком» (для интерфейса, консоли и проверки)

def run_compare(db: sqlite3.Connection, settings, network: str, *, crawl_id: int | None = None, ywm_id: int | None = None,
                gsc_id: int | None = None, probe: int = 0, max_depth: int | None = None, save: bool = True, progress=None) -> dict:
    """Берёт последние выгрузки сети (если id не заданы), сравнивает, при probe>0 проверяет живые ответы «сирот», сохраняет."""
    ensure_schema(db)
    if crawl_id is None:
        c = cr.latest_for(db, network)
        if not c:
            raise ix.IndexedError(f"для сети {network} нет краула Screaming Frog: загрузите CSV в разделе «Краул»")
        crawl_id = c["id"]
    ids = {}
    for src, given in (("ywm", ywm_id), ("gsc", gsc_id)):
        if given == 0:  # явно «не использовать»
            continue
        if given:
            ids[src] = given
        else:
            last = ix.latest(db, network, src)
            if last:
                ids[src] = last["id"]
    if not ids:
        raise ix.IndexedError(f"для сети {network} нет выгрузок индекса: получите их из Вебмастера / Search Console или загрузите файл")
    md = max_depth if max_depth is not None else _env_int("MAX_DEPTH", 4)
    probes = saved_probes(db, network)
    res = compare(db, crawl_id, ids, max_depth=md, probes=probes)
    if probe > 0:
        todo = [(r["norm"], r["url"]) for r in res["rows"] if r["category"] in ("not_in_crawl", "stale") and r["norm"] not in probes]
        if todo:
            probes.update(probe_missing(db, settings, network, todo, limit=probe, progress=progress))
            res = compare(db, crawl_id, ids, max_depth=md, probes=probes)
    res["index_ids"], res["crawl_id"] = ids, crawl_id
    if save:
        res["run_id"] = store(db, network, crawl_id, ids, res)
    return res


def _env_int(name: str, default: int) -> int:
    import os
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default
