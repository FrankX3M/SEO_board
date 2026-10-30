"""Краул сети из Screaming Frog (CSV «Internal → All»): импорт в БД, метрики, сравнение выгрузок.

Краул не запускается самим инструментом: вы выгружаете его в Screaming Frog и загружаете CSV (кнопка в интерфейсе
или `python import_crawl.py --network 5ka файл.csv`). Из ~70 колонок хранятся нужные для проверок:
Address, Content Type, Status Code, Indexability (+Status), Title/H1/Meta Description, Meta Robots, X-Robots-Tag,
Canonical Link Element 1, Size, Word Count, Crawl Depth, Inlinks/Unique Inlinks, Outlinks, Response Time,
Redirect URL/Type, Last Modified, Crawl Timestamp. Числа с запятой («1,965») понимаются; Response Time в секундах.

Каждая выгрузка — точка на дашборде (метрики crawl_*), проверки чек-листа про краул (5xx, ссылки на 404 и редиректы,
сироты, canonical на 404/редирект, карта сайта) используют ближайшую к запуску выгрузку сети.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

from .netutil import host_of, same_site

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, network TEXT NOT NULL, label TEXT, source_file TEXT,
  taken_at TEXT NOT NULL, uploaded_at TEXT NOT NULL, rows INTEGER, html_rows INTEGER, skipped_external INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_crawl_imports ON crawl_imports(network, taken_at);
CREATE TABLE IF NOT EXISTS crawl_pages (
  import_id INTEGER NOT NULL, address TEXT NOT NULL, content_type TEXT, status INTEGER,
  indexability TEXT, indexability_status TEXT, title TEXT, title_len INTEGER, meta_desc TEXT, meta_desc_len INTEGER,
  h1 TEXT, h1_2 TEXT, meta_robots TEXT, x_robots TEXT, canonical TEXT, size INTEGER, word_count INTEGER,
  depth INTEGER, inlinks INTEGER, unique_inlinks INTEGER, outlinks INTEGER, response_ms REAL,
  redirect_url TEXT, redirect_type TEXT, last_modified TEXT, crawled_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_crawl_pages_imp ON crawl_pages(import_id, status);
CREATE INDEX IF NOT EXISTS ix_crawl_pages_addr ON crawl_pages(import_id, address);
"""

COLUMNS = {  # колонка Screaming Frog -> поле таблицы
    "Address": "address", "Content Type": "content_type", "Status Code": "status", "Indexability": "indexability",
    "Indexability Status": "indexability_status", "Title 1": "title", "Title 1 Length": "title_len",
    "Meta Description 1": "meta_desc", "Meta Description 1 Length": "meta_desc_len", "H1-1": "h1", "H1-2": "h1_2",
    "Meta Robots 1": "meta_robots", "X-Robots-Tag 1": "x_robots", "Canonical Link Element 1": "canonical",
    "Size (bytes)": "size", "Word Count": "word_count", "Crawl Depth": "depth", "Inlinks": "inlinks",
    "Unique Inlinks": "unique_inlinks", "Outlinks": "outlinks", "Response Time": "response_ms",
    "Redirect URL": "redirect_url", "Redirect Type": "redirect_type", "Last Modified": "last_modified",
    "Crawl Timestamp": "crawled_at",
}
INT_FIELDS = {"status", "title_len", "meta_desc_len", "size", "word_count", "depth", "inlinks", "unique_inlinks", "outlinks"}
FIELDS = list(COLUMNS.values())

# метрики, считаемые по выгрузке: id -> (название, единица, лучше up/down)
CATALOG = {
    "crawl_pages_total": ("Краул: URL в выгрузке", "шт", None),
    "crawl_html_total": ("Краул: HTML-страниц", "шт", None),
    "crawl_2xx_share": ("Краул: HTML с ответом 2xx", "%", "up"),
    "crawl_3xx": ("Краул: HTML-URL с редиректом (3xx)", "шт", "down"),
    "crawl_4xx": ("Краул: HTML-URL с 4xx", "шт", "down"),
    "crawl_5xx": ("Краул: HTML-URL с 5xx", "шт", "down"),
    "crawl_indexable_share": ("Краул: индексируемые среди 200", "%", "up"),
    "crawl_noindex": ("Краул: страниц с noindex", "шт", None),
    "crawl_links_to_404": ("Краул: сумма Inlinks на 404/410", "шт", "down"),
    "crawl_links_to_redirect": ("Краул: сумма Inlinks на редиректы", "шт", "down"),
    "crawl_orphans": ("Краул: кандидаты в сироты (0 входящих)", "шт", "down"),
    "crawl_no_response": ("Краул: URL без HTTP-ответа", "шт", "down"),
    "crawl_canonical_missing": ("Краул: индексируемых HTML без canonical", "шт", "down"),
    "crawl_title_missing": ("Краул: без <title>", "шт", "down"),
    "crawl_title_dup": ("Краул: с дублем <title>", "шт", "down"),
    "crawl_h1_missing": ("Краул: без H1", "шт", "down"),
    "crawl_h1_multi": ("Краул: с несколькими H1", "шт", "down"),
    "crawl_meta_missing": ("Краул: без meta description", "шт", "down"),
    "crawl_canonical_other": ("Краул: canonical указывает на другой URL", "шт", None),
    "crawl_resp_avg_ms": ("Краул: время ответа среднее", "мс", "down"),
    "crawl_resp_p95_ms": ("Краул: время ответа p95", "мс", "down"),
    "crawl_depth_avg": ("Краул: глубина среднее", "ур.", "down"),
    "crawl_depth_max": ("Краул: глубина максимум", "ур.", "down"),
}


class CrawlError(ValueError):
    pass


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def _num(s: str | None):
    s = (s or "").strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _int(s):
    v = _num(s)
    return int(v) if v is not None else None


def _ts(s: str | None) -> str | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).astimezone().isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


class _Raw(io.RawIOBase):
    """Приводит файлоподобный объект с read() к бинарному потоку."""

    def __init__(self, f):
        self._f = f

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._f.read(len(b))
        b[:len(data)] = data
        return len(data)


def import_csv(db: sqlite3.Connection, network: str, source: BinaryIO | Path | str, *, label: str = "",
               source_file: str = "", host: str | None = None, batch: int = 2000) -> dict:
    """Читает CSV потоково (файлы Screaming Frog бывают в сотни МБ). host — домен сети: чужие адреса отбрасываются."""
    ensure_schema(db)
    close = False
    if isinstance(source, (str, Path)):
        source_file = source_file or Path(source).name
        source, close = open(source, "rb"), True
    try:
        if not hasattr(source, "readable"):  # SpooledTemporaryFile из загрузки (Python < 3.11) не умеет readable()
            source = _Raw(source)
        text = io.TextIOWrapper(io.BufferedReader(source) if isinstance(source, _Raw) else source,
                                encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text)
        try:
            header = next(reader)
        except StopIteration:
            raise CrawlError("файл пуст") from None
        header = [h.strip().lstrip("﻿") for h in header]
        idx = {COLUMNS[h]: i for i, h in enumerate(header) if h in COLUMNS}
        if "address" not in idx or "status" not in idx:
            raise CrawlError("не похоже на выгрузку Screaming Frog «Internal → All»: нет колонок Address и Status Code. "
                             "Найдены: " + ", ".join(header[:8]) + ("…" if len(header) > 8 else ""))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = db.execute("INSERT INTO crawl_imports(network, label, source_file, taken_at, uploaded_at, rows, html_rows) "
                         "VALUES (?,?,?,?,?,0,0)", (network, label, source_file, now, now))
        imp = cur.lastrowid
        ins = f"INSERT INTO crawl_pages(import_id, {', '.join(FIELDS)}) VALUES ({','.join('?' * (len(FIELDS) + 1))})"
        rows = html = skipped = 0
        stamps: list[str] = []
        buf: list[tuple] = []

        def flush():
            if buf:
                db.executemany(ins, buf)
                buf.clear()

        db.execute("BEGIN")
        try:
            for r in reader:
                if not r or not any(r):
                    continue
                rec = {f: (r[i].strip() if i < len(r) else "") for f, i in idx.items()}
                addr = rec["address"]
                if not addr:
                    continue
                if host and not same_site(host_of(addr), host):
                    skipped += 1
                    continue
                row = []
                for f in FIELDS:
                    v = rec.get(f, "")
                    if f in INT_FIELDS:
                        row.append(_int(v))
                    elif f == "response_ms":
                        n = _num(v)
                        row.append(round(n * 1000, 1) if n is not None else None)  # SF отдаёт секунды
                    else:
                        row.append(v or None)
                buf.append((imp, *row))
                rows += 1
                if (rec.get("content_type") or "").lower().startswith("text/html"):
                    html += 1
                if rec.get("crawled_at") and len(stamps) < 50000:
                    stamps.append(rec["crawled_at"])
                if len(buf) >= batch:
                    flush()
            flush()
            if rows == 0:
                raise CrawlError("в файле нет строк с адресами" + (f" сети (хост {host}); возможно, выбрана не та сеть" if skipped else ""))
            taken = _ts(max(stamps)) if stamps else None
            db.execute("UPDATE crawl_imports SET rows=?, html_rows=?, skipped_external=?, taken_at=? WHERE id=?",
                       (rows, html, skipped, taken or now, imp))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            db.execute("DELETE FROM crawl_imports WHERE id=?", (imp,))
            raise
        return {"import_id": imp, "rows": rows, "html_rows": html, "skipped_external": skipped,
                "taken_at": taken or now, "missing_columns": [h for h in COLUMNS if h not in header]}
    finally:
        if close:
            source.close()


def delete_import(db: sqlite3.Connection, import_id: int) -> None:
    db.execute("BEGIN")
    db.execute("DELETE FROM crawl_pages WHERE import_id=?", (import_id,))
    db.execute("DELETE FROM crawl_imports WHERE id=?", (import_id,))
    db.execute("COMMIT")


def imports(db: sqlite3.Connection, network: str | None = None, limit: int = 100) -> list[dict]:
    ensure_schema(db)
    q = "SELECT * FROM crawl_imports" + (" WHERE network=?" if network else "") + " ORDER BY taken_at DESC, id DESC LIMIT ?"
    return [dict(r) for r in db.execute(q, ([network] if network else []) + [limit])]


def latest_for(db: sqlite3.Connection, network: str, around_iso: str | None = None) -> dict | None:
    """Последняя выгрузка не позже запуска, с приведением времени к UTC."""
    ensure_schema(db)
    if around_iso:
        cutoff = datetime.fromisoformat(around_iso).astimezone(timezone.utc)
        candidates = [dict(r) for r in db.execute("SELECT * FROM crawl_imports WHERE network=?", (network,))]
        eligible = [r for r in candidates if r["taken_at"] and datetime.fromisoformat(r["taken_at"]).astimezone(timezone.utc) <= cutoff]
        return max(eligible, key=lambda r: (datetime.fromisoformat(r["taken_at"]).astimezone(timezone.utc), r["id"])) if eligible else None
    r = db.execute("SELECT * FROM crawl_imports WHERE network=? ORDER BY taken_at DESC, id DESC LIMIT 1", (network,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- метрики

_HTML = "(content_type LIKE 'text/html%')"
_OK = f"({_HTML} AND status=200)"
_IDX = f"({_OK} AND indexability='Indexable')"


def _pct(values: list[float], p: float):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def metrics(db: sqlite3.Connection, import_id: int) -> dict[str, float | None]:
    def one(sql, *a):
        r = db.execute(sql, (import_id, *a)).fetchone()
        return r[0] if r else None

    m: dict[str, float | None] = {}
    total = one("SELECT COUNT(*) FROM crawl_pages WHERE import_id=?")
    html = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_HTML}")
    m["crawl_pages_total"], m["crawl_html_total"] = total, html
    ok = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_OK}")
    success = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_HTML} AND status BETWEEN 200 AND 299")
    m["crawl_2xx_share"] = round(100 * success / html, 2) if html else None
    for key, cond in (("crawl_3xx", "status BETWEEN 300 AND 399"), ("crawl_4xx", "status BETWEEN 400 AND 499"),
                      ("crawl_5xx", "status BETWEEN 500 AND 599")):
        m[key] = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_HTML} AND {cond}")
    idx = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_IDX}")
    m["crawl_indexable_share"] = round(100 * idx / ok, 2) if ok else None
    m["crawl_noindex"] = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_OK} AND indexability_status LIKE '%noindex%'")
    m["crawl_no_response"] = one("SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND (status IS NULL OR status=0)")
    m["crawl_canonical_missing"] = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_IDX} AND trim(COALESCE(canonical,''))=''")
    m["crawl_links_to_404"] = one("SELECT COALESCE(SUM(inlinks),0) FROM crawl_pages WHERE import_id=? AND status IN (404,410)")
    m["crawl_links_to_redirect"] = one("SELECT COALESCE(SUM(inlinks),0) FROM crawl_pages WHERE import_id=? AND status BETWEEN 300 AND 399")
    m["crawl_orphans"] = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_IDX} AND COALESCE(inlinks,0)=0 "
                             f"AND (depth IS NULL OR depth>0)")
    m["crawl_title_missing"] = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_IDX} AND COALESCE(title,'')=''")
    titles = Counter(re.sub(r"\s+", " ", r[0] or "").strip().lower()
                     for r in db.execute(f"SELECT title FROM crawl_pages WHERE import_id=? AND {_IDX}", (import_id,)))
    m["crawl_title_dup"] = sum(n for title, n in titles.items() if title and n > 1)
    m["crawl_h1_missing"] = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_IDX} AND COALESCE(h1,'')=''")
    m["crawl_h1_multi"] = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_IDX} AND COALESCE(h1_2,'')!=''")
    m["crawl_meta_missing"] = one(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_IDX} AND COALESCE(meta_desc,'')=''")
    m["crawl_canonical_other"] = one(
        f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {_OK} AND COALESCE(canonical,'')!='' AND canonical!=address")
    resp = [r[0] for r in db.execute(f"SELECT response_ms FROM crawl_pages WHERE import_id=? AND {_OK} AND response_ms IS NOT NULL",
                                     (import_id,))]
    m["crawl_resp_avg_ms"] = round(sum(resp) / len(resp), 1) if resp else None
    p95 = _pct(resp, 0.95)
    m["crawl_resp_p95_ms"] = round(p95, 1) if p95 is not None else None
    d = one(f"SELECT AVG(depth) FROM crawl_pages WHERE import_id=? AND {_OK} AND depth IS NOT NULL")
    m["crawl_depth_avg"] = round(d, 2) if d is not None else None
    m["crawl_depth_max"] = one(f"SELECT MAX(depth) FROM crawl_pages WHERE import_id=? AND {_OK}")
    return m


def status_distribution(db: sqlite3.Connection, import_id: int) -> list[dict]:
    rows = db.execute("SELECT status, COUNT(*) n FROM crawl_pages WHERE import_id=? GROUP BY status ORDER BY n DESC", (import_id,))
    return [{"status": r["status"], "n": r["n"]} for r in rows]


# ---------------------------------------------------------------- просмотр и сравнение

FILTERS = {
    "all": ("1=1", "Все URL"),
    "ok": (f"{_OK}", "HTML 200"),
    "3xx": (f"{_HTML} AND status BETWEEN 300 AND 399", "Редиректы 3xx"),
    "4xx": ("status BETWEEN 400 AND 499", "Ошибки 4xx"),
    "5xx": ("status BETWEEN 500 AND 599", "Ошибки 5xx"),
    "noindex": (f"{_OK} AND indexability_status LIKE '%noindex%'", "noindex"),
    "orphans": (f"{_IDX} AND COALESCE(inlinks,0)=0 AND (depth IS NULL OR depth>0)", "Сироты"),
    "no_title": (f"{_IDX} AND COALESCE(title,'')=''", "Без title"),
    "no_h1": (f"{_IDX} AND COALESCE(h1,'')=''", "Без H1"),
    "canon_other": (f"{_OK} AND COALESCE(canonical,'')!='' AND canonical!=address", "Canonical на другой URL"),
}


def rows(db: sqlite3.Connection, import_id: int, flt: str = "all", q: str | None = None, limit: int = 100, offset: int = 0):
    cond = FILTERS.get(flt, FILTERS["all"])[0]
    args: list = [import_id]
    where = f"import_id=? AND {cond}"
    if q:
        where += " AND address LIKE ?"
        args.append(f"%{q}%")
    total = db.execute(f"SELECT COUNT(*) FROM crawl_pages WHERE {where}", args).fetchone()[0]
    out = db.execute(f"SELECT * FROM crawl_pages WHERE {where} ORDER BY COALESCE(inlinks,0) DESC, address LIMIT ? OFFSET ?",
                     args + [limit, offset]).fetchall()
    return [dict(r) for r in out], total


def diff(db: sqlite3.Connection, a: int, b: int, limit: int = 200) -> dict:
    """Что изменилось между выгрузками a (старая) и b (новая)."""
    new = db.execute("SELECT address, status FROM crawl_pages WHERE import_id=? AND address NOT IN "
                     "(SELECT address FROM crawl_pages WHERE import_id=?) LIMIT ?", (b, a, limit)).fetchall()
    gone = db.execute("SELECT address, status FROM crawl_pages WHERE import_id=? AND address NOT IN "
                      "(SELECT address FROM crawl_pages WHERE import_id=?) LIMIT ?", (a, b, limit)).fetchall()
    changed = db.execute(
        "SELECT x.address, x.status AS old, y.status AS new FROM crawl_pages x JOIN crawl_pages y "
        "ON x.address=y.address AND y.import_id=? WHERE x.import_id=? AND COALESCE(x.status,0)!=COALESCE(y.status,0) "
        "ORDER BY y.status DESC LIMIT ?", (b, a, limit)).fetchall()
    cnt = lambda sql, *p: db.execute(sql, p).fetchone()[0]  # noqa: E731
    return {
        "new": [dict(r) for r in new], "gone": [dict(r) for r in gone], "changed": [dict(r) for r in changed],
        "new_total": cnt("SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND address NOT IN (SELECT address FROM crawl_pages WHERE import_id=?)", b, a),
        "gone_total": cnt("SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND address NOT IN (SELECT address FROM crawl_pages WHERE import_id=?)", a, b),
        "changed_total": cnt("SELECT COUNT(*) FROM crawl_pages x JOIN crawl_pages y ON x.address=y.address AND y.import_id=? "
                             "WHERE x.import_id=? AND COALESCE(x.status,0)!=COALESCE(y.status,0)", b, a),
    }
