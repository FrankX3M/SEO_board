"""Контекст проверок одной сети в одном запуске: страницы, пробы, карта сайта, живые запросы."""
from __future__ import annotations

import gzip
import json
import sqlite3
from functools import cached_property
from pathlib import Path

from ..config import Settings
from ..storage import Store
from ..registry import path_qs
from .core import CheckSettings, PageDoc

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_urls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, network TEXT NOT NULL,
  kind TEXT NOT NULL,        -- link | image | canonical | sitemap_url | param_canonical | page2
  url TEXT NOT NULL, status INTEGER, final_url TEXT, location TEXT, hops INTEGER,
  meta TEXT,                 -- JSON: canonical цели, noindex, источники
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_live_run ON live_urls(run_id, network, kind);
CREATE TABLE IF NOT EXISTS check_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, network TEXT NOT NULL, check_id TEXT NOT NULL,
  title TEXT, status TEXT NOT NULL, summary TEXT, items TEXT, source TEXT, created_at TEXT NOT NULL, stats TEXT
);
CREATE INDEX IF NOT EXISTS ix_checks_run ON check_results(run_id, network);
"""


def migrate_checks(db) -> None:
    """Старые БД: добавить колонку stats в check_results."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(check_results)")}
    if cols and "stats" not in cols:
        db.execute("ALTER TABLE check_results ADD COLUMN stats TEXT")


def ensure_schema(store: Store) -> None:
    store.db.executescript(SCHEMA)
    migrate_checks(store.db)
    store.db.commit()


class Ctx:
    def __init__(self, store: Store, settings: Settings, run_id: str, network: str, cs: CheckSettings,
                 base_url: str, baseline_run: str | None = None, mobile_run: str | None = None):
        self.store, self.settings, self.run_id, self.network, self.cs = store, settings, run_id, network, cs
        self.base_url = base_url.rstrip("/")
        self.baseline_run, self.mobile_run = baseline_run, mobile_run
        self.db = store.db
        self.db.row_factory = sqlite3.Row
        self._pages: dict[tuple, list[PageDoc]] = {}

    # ---- флажки справочника (noindex / nofollow на уровне URL)
    @cached_property
    def reg_flags(self) -> dict:
        """{(тип, путь+query): (noindex, nofollow)} только для URL с флажками; нет таблицы/колонок — пусто."""
        try:
            rows = self.db.execute("SELECT page_type, path_qs, noindex, nofollow FROM ref_urls "
                                   "WHERE site=? AND (noindex=1 OR nofollow=1)", (self.network,)).fetchall()
        except sqlite3.Error:
            return {}
        return {(r[0], r[1]): (bool(r[2]), bool(r[3])) for r in rows}

    # ---- страницы
    def pages(self, variant: str, *, run_id: str | None = None, roles: tuple[str, ...] | None = None,
              ok_only: bool = True) -> list[PageDoc]:
        rid = run_id or self.run_id
        key = (rid, variant)
        if key not in self._pages:
            rows = self.db.execute(
                "SELECT * FROM pages WHERE run_id=? AND network=? AND variant=? ORDER BY page_type, source_url",
                (rid, self.network, variant)).fetchall()
            docs = []
            for r in rows:
                raw = b""
                if r["file_path"]:
                    f = self.store.data_dir / r["file_path"]
                    if f.exists():
                        raw = f.read_bytes()
                d = PageDoc(self.network, r["page_type"], variant, r["source_url"], r["url"], r["final_url"] or r["url"],
                            r["status"], json.loads(r["headers"] or "{}"), r["ttfb_ms"], raw, self.base_url)
                d.role_cfg = self.cs  # type: ignore[attr-defined]
                d.reg_noindex, d.reg_nofollow = self.reg_flags.get((r["page_type"], path_qs(r["source_url"])), (False, False))
                docs.append(d)
            self._pages[key] = docs
        docs = self._pages[key]
        if ok_only:
            docs = [d for d in docs if d.status == 200 and d.raw]
        if roles:
            from .core import role_of
            docs = [d for d in docs if role_of(d.page_type, self.cs) in roles]
        return docs

    def primary(self, roles=None) -> list[PageDoc]:
        """HTML, который отдаётся поисковому боту (bot); если его нет — plain."""
        for v in ("bot", "plain"):
            p = self.pages(v, roles=roles)
            if p:
                return p
        return []

    def has_variant(self, variant: str) -> bool:
        return bool(self.pages(variant))

    # ---- пробы
    def probes(self, *kinds: str) -> list[dict]:
        q = ",".join("?" * len(kinds))
        rows = self.db.execute(
            f"SELECT * FROM probes WHERE run_id=? AND network=? AND kind IN ({q})", (self.run_id, self.network, *kinds)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta_d"] = json.loads(d["meta"]) if d.get("meta") else {}
            d["expected_d"] = json.loads(d["expected"]) if d.get("expected") else {}
            out.append(d)
        return out

    def probe_body(self, probe: dict) -> bytes:
        f = probe.get("file_path")
        p = self.store.data_dir / f if f else None
        return p.read_bytes() if p and p.exists() else b""

    # ---- карта сайта
    @cached_property
    def sitemap(self) -> dict:
        """{'files': [probe], 'urls': {loc: lastmod|None}} по сохранённым файлам карт."""
        from lxml import etree
        files = self.probes("sitemap")
        urls: dict[str, str | None] = {}
        for p in files:
            raw = self.probe_body(p)
            if not raw:
                continue
            try:
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                root = etree.fromstring(raw, etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True))
            except Exception:  # noqa: BLE001
                continue
            if etree.QName(root).localname != "urlset":
                continue
            for u in root:
                loc = lm = None
                for ch in u:
                    if not isinstance(ch.tag, str):
                        continue
                    n = etree.QName(ch).localname
                    if n == "loc" and ch.text:
                        loc = ch.text.strip()
                    elif n == "lastmod" and ch.text:
                        lm = ch.text.strip()
                if loc:
                    urls[loc] = lm
        return {"files": files, "urls": urls}

    # ---- живые запросы
    def live(self, kind: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM live_urls WHERE run_id=? AND network=? AND kind=?",
                               (self.run_id, self.network, kind)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta_d"] = json.loads(d["meta"]) if d.get("meta") else {}
            out.append(d)
        return out

    def has_live(self) -> bool:
        return bool(self.db.execute("SELECT 1 FROM live_urls WHERE run_id=? AND network=? LIMIT 1",
                                    (self.run_id, self.network)).fetchone())
