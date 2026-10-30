"""Хранилище: HTML-файлы на диске + SQLite (из неё потом строится дашборд)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  env         TEXT NOT NULL,
  options     TEXT,      -- JSON: режим, UA, флаги (без секретов)
  summary     TEXT       -- JSON: итоговая сводка
);
CREATE TABLE IF NOT EXISTS pages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  network     TEXT NOT NULL,
  page_type   TEXT NOT NULL,
  variant     TEXT,            -- plain | bot | browser | browser-plain
  ua_name     TEXT,            -- профиль User-Agent этого варианта
  mode        TEXT NOT NULL,   -- raw | rendered
  source_url  TEXT NOT NULL,   -- как записано в справочнике
  url         TEXT NOT NULL,   -- реально запрошенный адрес
  status      INTEGER,
  final_url   TEXT,
  chain       TEXT,            -- JSON: цепочка редиректов
  headers     TEXT,            -- JSON: заголовки ответа (raw), без set-cookie
  ttfb_ms     REAL,
  total_ms    REAL,
  bytes       INTEGER,
  sha256      TEXT,
  file_path   TEXT,            -- относительно data_dir
  has_title   INTEGER,
  has_h1      INTEGER,
  suspect     TEXT,            -- причины подозрительного ответа (пусто = ок)
  error       TEXT,
  fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pages_run ON pages(run_id);
CREATE TABLE IF NOT EXISTS diffs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  network     TEXT NOT NULL,
  page_type   TEXT NOT NULL,
  url         TEXT NOT NULL,
  variant_a   TEXT NOT NULL,   -- «до» (например plain — без рендеринга)
  variant_b   TEXT NOT NULL,   -- «после» (например bot — с рендерингом)
  verdict     TEXT,            -- same | partial | major | empty_b | skipped
  blocks_appeared    INTEGER,
  blocks_disappeared INTEGER,
  blocks_changed     INTEGER,
  links_appeared     INTEGER,
  links_disappeared  INTEGER,
  text_only_b_chars  INTEGER,
  text_only_a_chars  INTEGER,
  coverage    REAL,            -- доля текста B, уже присутствующая в A
  details     TEXT,            -- JSON: блоки, ссылки, head, разметка
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_diffs_run ON diffs(run_id);
CREATE TABLE IF NOT EXISTS probes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  network     TEXT NOT NULL,
  kind        TEXT NOT NULL,   -- slash | upper | alt_host | http | combined | index_php | index_html |
                               -- not_found | robots | sitemap | favicon | apple_icon | manifest | manifest_icon | og_image
  page_type   TEXT,            -- тип страницы из справочника (если проба от неё)
  source_url  TEXT,            -- URL из справочника, от которого построена проба
  url         TEXT NOT NULL,   -- реально запрошенный адрес
  expected    TEXT,            -- JSON: что ожидаем (статус, цель редиректа, число хопов)
  status      INTEGER,
  final_url   TEXT,
  chain       TEXT,            -- JSON: цепочка редиректов
  headers     TEXT,            -- JSON: заголовки финального ответа
  content_type TEXT,
  ttfb_ms     REAL,
  total_ms    REAL,
  bytes       INTEGER,
  file_path   TEXT,            -- сохранённое тело (404-страницы, robots, sitemap, manifest)
  verdict     TEXT,            -- pass | fail
  reason      TEXT,            -- причины fail через запятую (пусто = pass)
  meta        TEXT,            -- JSON: подробности (число URL в карте, размеры картинки…)
  fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_probes_run ON probes(run_id, network, kind);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(url: str) -> str:
    p = urlsplit(url)
    s = (p.path + (f"?{p.query}" if p.query else "")).strip("/") or "index"
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:70].strip("_") or "page"
    # хэш от точного URL: страницы, отличающиеся регистром/слешем, не затирают друг друга
    # (важно для Windows, где имена файлов нечувствительны к регистру)
    return f"{s}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "techaudit.db")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(pages)")}
        for c in ("variant", "ua_name"):
            if c not in cols:
                self.db.execute(f"ALTER TABLE pages ADD COLUMN {c} TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_pages_net_type ON pages(network, page_type, variant)")
        self.db.commit()

    def unique_run_id(self, base: str) -> str:
        """Два запуска в одну секунду не должны конфликтовать."""
        rid, n = base, 1
        while self.db.execute("SELECT 1 FROM runs WHERE run_id=?", (rid,)).fetchone():
            n += 1
            rid = f"{base}-{n}"
        return rid

    def start_run(self, run_id: str, env: str, options: dict) -> None:
        self.db.execute(
            "INSERT INTO runs(run_id, started_at, env, options) VALUES (?,?,?,?)",
            (run_id, now_iso(), env, json.dumps(options, ensure_ascii=False)),
        )
        self.db.commit()

    def finish_run(self, run_id: str, summary: dict) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, summary=? WHERE run_id=?",
            (now_iso(), json.dumps(summary, ensure_ascii=False), run_id),
        )
        self.db.commit()

    def save_html(self, run_id: str, network: str, ptype: str, url: str, variant: str, content: bytes) -> str:
        rel = Path("html") / run_id / network / re.sub(r"[^\w.-]+", "_", ptype) / f"{slugify(url)}.{variant}.html"
        full = self.data_dir / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
        return rel.as_posix()

    def save_blob(self, run_id: str, network: str, subdir: str, url: str, ext: str, content: bytes) -> str:
        rel = Path("html") / run_id / network / re.sub(r"[^\w./-]+", "_", subdir) / f"{slugify(url)}.{ext}"
        full = self.data_dir / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
        return rel.as_posix()

    def add_probe(self, row: dict) -> None:
        cols = ", ".join(row)
        self.db.execute(f"INSERT INTO probes({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()

    def add_page(self, row: dict) -> None:
        cols = ", ".join(row)
        self.db.execute(f"INSERT INTO pages({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
