"""Справочник страниц в БД: сети, типы страниц, URL, ответственные.

БД — источник правды. Сборщик (collect_html.py) читает URL отсюда; registry/<сеть>.csv
создаётся автоматически как зеркало (для совместимости и просмотра в git).
Если для сети в БД нет ни одной строки, сборщик читает старый CSV.
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .registry import RegistryEntry, load_registry, path_qs
from .webdb import Rollback, connect, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_sites (
  site TEXT PRIMARY KEY, name_ru TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ref_page_types (
  slug TEXT PRIMARY KEY, name_ru TEXT NOT NULL DEFAULT '', grp TEXT NOT NULL DEFAULT 'aux', note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ref_urls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site TEXT NOT NULL, page_type TEXT NOT NULL,
  url TEXT NOT NULL,            -- как ввёл пользователь (путь или полный адрес)
  path_qs TEXT NOT NULL,        -- путь + query: по нему сборщик строит адрес от NET_<СЕТЬ>_URL
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',   -- active | gone
  noindex INTEGER NOT NULL DEFAULT 0,      -- 1: на странице noindex — норма (не ошибка)
  nofollow INTEGER NOT NULL DEFAULT 0,     -- 1: на странице nofollow — норма
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(site, page_type, path_qs)
);
CREATE INDEX IF NOT EXISTS ix_ref_urls_site ON ref_urls(site, status);
CREATE TABLE IF NOT EXISTS ref_owners (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  page_type TEXT NOT NULL, email TEXT NOT NULL, area TEXT NOT NULL DEFAULT '', coverage TEXT NOT NULL DEFAULT '',
  UNIQUE(page_type, email, area)
);
CREATE TABLE IF NOT EXISTS ref_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL, detail TEXT
);
"""

def migrate(db: sqlite3.Connection) -> None:
    """Старые БД: добавить в ref_urls флажки noindex / nofollow."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(ref_urls)")}
    for c in ("noindex", "nofollow"):
        if cols and c not in cols:
            db.execute(f"ALTER TABLE ref_urls ADD COLUMN {c} INTEGER NOT NULL DEFAULT 0")


_FLAG_TRUE = {"1", "да", "yes", "y", "+", "true", "x", "✓", "v", "on", "noindex", "nofollow"}


def norm_flag(value) -> int:
    return 1 if str(value if value is not None else "").strip().lower() in _FLAG_TRUE else 0


STATUSES = ("active", "gone")
STATUS_LABELS = {"active": "активна", "gone": "удалена"}
_STATUS_ALIASES = {
    "active": "active", "активна": "active", "активный": "active", "активно": "active", "1": "active",
    "да": "active", "yes": "active", "+": "active", "true": "active",
    "gone": "gone", "удалена": "gone", "удалён": "gone", "удален": "gone", "неактивна": "gone",
    "inactive": "gone", "0": "gone", "нет": "gone", "no": "gone", "-": "gone", "false": "gone",
}

URL_ALIASES = {
    "site": "site", "network": "site", "сеть": "site", "сайт": "site",
    "type": "type", "page_type": "type", "slug": "type", "тип": "type", "тип страницы": "type",
    "url": "url", "адрес": "url", "ссылка": "url",
    "note": "note", "комментарий": "note", "примечание": "note", "описание": "note",
    "status": "status", "статус": "status",
    "noindex": "noindex", "nofollow": "nofollow",
}
SITE_ALIASES = {"site": "site", "сеть": "site", "ключ": "site", "name_ru": "name_ru", "название": "name_ru",
                "name": "name_ru", "host": "host", "хост": "host", "домен": "host"}
TYPE_ALIASES = {"slug": "slug", "type": "slug", "page_type": "slug", "тип": "slug", "name_ru": "name_ru",
                "название": "name_ru", "group": "grp", "grp": "grp", "группа": "grp", "note": "note",
                "комментарий": "note"}
OWNER_ALIASES = {"page_type": "page_type", "type": "page_type", "тип": "page_type", "slug": "page_type",
                 "email": "email", "почта": "email", "area": "area", "область": "area", "зона": "area",
                 "coverage": "coverage", "покрытие": "coverage"}

ENTITIES = {  # описание «малых» справочников для универсального CRUD
    "sites": {"table": "ref_sites", "key": ["site"], "fields": ["site", "name_ru", "host"], "aliases": SITE_ALIASES,
              "title": "Сети", "label": {"site": "Ключ", "name_ru": "Название", "host": "Хост"}},
    "types": {"table": "ref_page_types", "key": ["slug"], "fields": ["slug", "name_ru", "grp", "note"],
              "aliases": TYPE_ALIASES, "title": "Типы страниц",
              "label": {"slug": "Код типа", "name_ru": "Название", "grp": "Группа", "note": "Комментарий"}},
    "owners": {"table": "ref_owners", "key": ["page_type", "email", "area"],
               "fields": ["page_type", "email", "area", "coverage"], "aliases": OWNER_ALIASES, "title": "Ответственные",
               "label": {"page_type": "Тип страницы", "email": "Почта", "area": "Область", "coverage": "Покрытие"}},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_change(db: sqlite3.Connection, action: str, entity: str, detail: str) -> None:
    db.execute("INSERT INTO ref_changes(at, action, entity, detail) VALUES (?,?,?,?)",
               (now_iso(), action, entity, detail[:500]))


# ------------------------------------------------------------------ разбор URL / CSV

def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def normalize_url(raw: str, site_host: str | None = None) -> tuple[str, str]:
    """Возвращает (url как введён, путь+query). Бросает ValueError с понятным текстом."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("пустой URL")
    if re.search(r"\s", raw):
        raise ValueError("в URL есть пробелы")
    p = urlsplit(raw)
    if p.scheme or p.netloc:
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("некорректный адрес: нужен путь вида /catalog/ или https://хост/путь")
        if site_host and _strip_www(p.netloc.lower()) != _strip_www(site_host.lower()):
            raise ValueError(f"хост {p.netloc} не совпадает с хостом сети ({site_host})")
    else:
        if not raw.startswith("/"):
            first = raw.split("/", 1)[0]
            if "." in first:
                raise ValueError("укажите путь, начинающийся с «/», или полный адрес с https://")
            raw = "/" + raw
    return raw, path_qs(raw)


def norm_status(value: str | None) -> str:
    v = (value or "").strip().lower()
    if not v:
        return "active"
    if v not in _STATUS_ALIASES:
        raise ValueError(f"неизвестный статус «{value}» (active / gone)")
    return _STATUS_ALIASES[v]


def parse_table(text: str, aliases: dict[str, str]) -> tuple[list[tuple[int, dict]], list[str]]:
    """CSV с разделителем ; , или табом. Возвращает ([(номер строки, {поле: значение})], неизвестные колонки)."""
    text = text.lstrip("﻿")
    lines = [(i, ln) for i, ln in enumerate(text.splitlines(), 1) if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise ValueError("файл пуст")
    first = lines[0][1]
    delim = max((";", ",", "\t"), key=first.count)
    reader = list(csv.reader([ln for _, ln in lines], delimiter=delim))
    header = [aliases.get(h.strip().lower().lstrip("﻿")) for h in reader[0]]
    unknown = [h.strip() for h, k in zip(reader[0], header) if k is None and h.strip()]
    if not any(header):
        raise ValueError("не найден заголовок. Ожидаются колонки: " + ", ".join(sorted(set(aliases.values()))))
    out = []
    for (lineno, _), row in zip(lines[1:], reader[1:]):
        rec: dict[str, str] = {}
        for key, val in zip(header, row):
            if key:
                rec[key] = val.strip()
        out.append((lineno, rec))
    return out, unknown


# ------------------------------------------------------------------ URL: чтение

URL_COLS = "u.id, u.site, u.page_type, u.url, u.path_qs, u.note, u.status, u.noindex, u.nofollow, u.updated_at, " \
           "COALESCE(t.name_ru,'') AS type_name, COALESCE(s.name_ru,'') AS site_name"
_URL_FROM = "FROM ref_urls u LEFT JOIN ref_page_types t ON t.slug=u.page_type LEFT JOIN ref_sites s ON s.site=u.site"


def _url_where(site=None, page_type=None, status=None, q=None, robots=None):
    where, args = [], []
    if robots == "noindex":
        where.append("u.noindex=1")
    elif robots == "nofollow":
        where.append("u.nofollow=1")
    elif robots == "any":
        where.append("(u.noindex=1 OR u.nofollow=1)")
    elif robots == "none":
        where.append("u.noindex=0 AND u.nofollow=0")
    if site:
        where.append("u.site=?")
        args.append(site)
    if page_type:
        where.append("u.page_type=?")
        args.append(page_type)
    if status:
        where.append("u.status=?")
        args.append(status)
    if q:
        like = f"%{q.lower()}%"
        where.append("(pylower(u.url) LIKE ? OR pylower(u.note) LIKE ? OR pylower(u.page_type) LIKE ?)")
        args += [like, like, like]
    return (" WHERE " + " AND ".join(where)) if where else "", args


def list_urls(db, *, site=None, page_type=None, status=None, q=None, robots=None, limit=100, offset=0):
    where, args = _url_where(site, page_type, status, q, robots)
    total = db.execute(f"SELECT COUNT(*) {_URL_FROM}{where}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT {URL_COLS} {_URL_FROM}{where} ORDER BY u.site, COALESCE(t.rowid, 1e9), u.id LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    return [dict(r) for r in rows], total


def get_url(db, url_id: int) -> dict | None:
    r = db.execute(f"SELECT {URL_COLS} {_URL_FROM} WHERE u.id=?", (url_id,)).fetchone()
    return dict(r) if r else None


def _site_host(db, site: str) -> str | None:
    r = db.execute("SELECT host FROM ref_sites WHERE site=?", (site,)).fetchone()
    return (r["host"] or None) if r else None


def _require_site_type(db, site: str, page_type: str, create_type: bool = False) -> bool:
    """Проверяет, что сеть и тип существуют. Возвращает True, если тип был создан."""
    if not db.execute("SELECT 1 FROM ref_sites WHERE site=?", (site,)).fetchone():
        raise ValueError(f"сеть «{site}» не заведена в справочнике сетей")
    if not db.execute("SELECT 1 FROM ref_page_types WHERE slug=?", (page_type,)).fetchone():
        if not create_type:
            raise ValueError(f"тип страницы «{page_type}» не заведён в справочнике типов")
        db.execute("INSERT INTO ref_page_types(slug, name_ru, grp, note) VALUES (?,?,?,?)",
                   (page_type, page_type, "aux", "создан при импорте"))
        return True
    return False


# ------------------------------------------------------------------ URL: изменение

def add_url(db, site: str, page_type: str, url: str, note: str = "", status: str = "active",
            noindex=0, nofollow=0) -> int:
    site, page_type = (site or "").strip(), (page_type or "").strip()
    if not site or not page_type:
        raise ValueError("укажите сеть и тип страницы")
    status = norm_status(status)
    with tx(db):
        _require_site_type(db, site, page_type)
        raw, pq = normalize_url(url, _site_host(db, site))
        if db.execute("SELECT 1 FROM ref_urls WHERE site=? AND page_type=? AND path_qs=?", (site, page_type, pq)).fetchone():
            raise ValueError("такой URL уже есть в этой сети и типе")
        ts = now_iso()
        cur = db.execute(
            "INSERT INTO ref_urls(site,page_type,url,path_qs,note,status,noindex,nofollow,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (site, page_type, raw, pq, (note or "").strip(), status, norm_flag(noindex), norm_flag(nofollow), ts, ts))
        log_change(db, "add", "url", f"{site}/{page_type} {pq}")
        return cur.lastrowid


def update_url(db, url_id: int, *, site: str, page_type: str, url: str, note: str = "", status: str = "active",
               noindex=0, nofollow=0) -> None:
    status = norm_status(status)
    with tx(db):
        old = db.execute("SELECT * FROM ref_urls WHERE id=?", (url_id,)).fetchone()
        if not old:
            raise ValueError("запись не найдена (возможно, уже удалена)")
        _require_site_type(db, site, page_type)
        raw, pq = normalize_url(url, _site_host(db, site))
        dup = db.execute("SELECT id FROM ref_urls WHERE site=? AND page_type=? AND path_qs=? AND id!=?",
                         (site, page_type, pq, url_id)).fetchone()
        if dup:
            raise ValueError("такой URL уже есть в этой сети и типе")
        db.execute("UPDATE ref_urls SET site=?, page_type=?, url=?, path_qs=?, note=?, status=?, noindex=?, nofollow=?,"
                   " updated_at=? WHERE id=?",
                   (site, page_type, raw, pq, (note or "").strip(), status, norm_flag(noindex), norm_flag(nofollow),
                    now_iso(), url_id))
        log_change(db, "update", "url", f"{old['site']}/{old['page_type']} {old['path_qs']} → {site}/{page_type} {pq} [{status}]")


def delete_urls(db, ids: list[int]) -> int:
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with tx(db):
        marks = ",".join("?" * len(ids))
        rows = db.execute(f"SELECT site, page_type, path_qs FROM ref_urls WHERE id IN ({marks})", ids).fetchall()
        db.execute(f"DELETE FROM ref_urls WHERE id IN ({marks})", ids)
        log_change(db, "delete", "url", f"{len(rows)} шт.: " + "; ".join(f"{r['site']}{r['path_qs']}" for r in rows[:8]))
    return len(rows)


def set_status(db, ids: list[int], status: str) -> int:
    status = norm_status(status)
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with tx(db):
        marks = ",".join("?" * len(ids))
        cur = db.execute(f"UPDATE ref_urls SET status=?, updated_at=? WHERE id IN ({marks})", [status, now_iso(), *ids])
        log_change(db, "status", "url", f"{cur.rowcount} шт. → {status}")
        return cur.rowcount


def set_flag(db, ids: list[int], field: str, value) -> int:
    """Массово включить/выключить noindex или nofollow у выбранных URL."""
    if field not in ("noindex", "nofollow"):
        raise ValueError("неизвестный флажок")
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    v = norm_flag(value)
    with tx(db):
        marks = ",".join("?" * len(ids))
        cur = db.execute(f"UPDATE ref_urls SET {field}=?, updated_at=? WHERE id IN ({marks})", [v, now_iso(), *ids])
        log_change(db, "flag", "url", f"{cur.rowcount} шт. → {field}={v}")
        return cur.rowcount


# ------------------------------------------------------------------ массовый импорт URL

def import_urls(db, text: str, *, mode: str = "add", default_site: str | None = None,
                create_types: bool = True, dry_run: bool = False, preview_rows: int = 30) -> dict:
    """mode: add — только новые; upsert — новые + обновить note/status существующих;
    replace — сети из файла становятся ровно такими, как в файле (лишние URL удаляются)."""
    if mode not in ("add", "upsert", "replace"):
        raise ValueError("неизвестный режим импорта")
    rows, unknown = parse_table(text, URL_ALIASES)
    rep = {"added": 0, "updated": 0, "skipped": 0, "deleted": 0, "errors": [], "new_types": [], "preview": [],
           "unknown_columns": unknown, "total": len(rows), "dry_run": dry_run, "mode": mode, "sites": []}
    try:
        with tx(db):
            seen: set[tuple] = set()
            touched_sites: set[str] = set()
            file_keys: dict[str, set[tuple]] = {}

            def note_preview(line, action, site, ptype, pq, extra=""):
                if len(rep["preview"]) < preview_rows:
                    rep["preview"].append({"line": line, "action": action, "site": site, "type": ptype, "url": pq, "msg": extra})

            for line, r in rows:
                site = (r.get("site") or default_site or "").strip()
                ptype = (r.get("type") or "").strip()
                try:
                    if not site:
                        raise ValueError("не указана сеть (колонка site или выбор сети при загрузке)")
                    if not ptype:
                        raise ValueError("не указан тип страницы")
                    status = norm_status(r.get("status"))
                    if not db.execute("SELECT 1 FROM ref_sites WHERE site=?", (site,)).fetchone():
                        raise ValueError(f"сеть «{site}» не заведена в справочнике сетей")
                    raw, pq = normalize_url(r.get("url", ""), _site_host(db, site))
                    key = (site, ptype, pq)
                    if key in seen:
                        rep["skipped"] += 1
                        note_preview(line, "дубль", site, ptype, pq, "повтор в файле")
                        continue
                    seen.add(key)
                    touched_sites.add(site)
                    file_keys.setdefault(site, set()).add((ptype, pq))
                    if _require_site_type(db, site, ptype, create_types):
                        rep["new_types"].append(ptype)
                    note = (r.get("note") or "").strip()
                    ex = db.execute("SELECT id, note, status, noindex, nofollow FROM ref_urls"
                                    " WHERE site=? AND page_type=? AND path_qs=?", key).fetchone()
                    noindex, nofollow = norm_flag(r.get("noindex")), norm_flag(r.get("nofollow"))
                    ts = now_iso()
                    if ex is None:
                        db.execute("INSERT INTO ref_urls(site,page_type,url,path_qs,note,status,noindex,nofollow,"
                                   "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (site, ptype, raw, pq, note, status, noindex, nofollow, ts, ts))
                        rep["added"] += 1
                        note_preview(line, "добавлен", site, ptype, pq)
                    elif mode in ("upsert", "replace"):
                        new_note = note if "note" in r else ex["note"]
                        new_status = status if "status" in r else ex["status"]
                        new_ni = noindex if "noindex" in r else ex["noindex"]
                        new_nf = nofollow if "nofollow" in r else ex["nofollow"]
                        if (new_note, new_status, new_ni, new_nf) != (ex["note"], ex["status"], ex["noindex"], ex["nofollow"]):
                            db.execute("UPDATE ref_urls SET note=?, status=?, noindex=?, nofollow=?, updated_at=? WHERE id=?",
                                       (new_note, new_status, new_ni, new_nf, ts, ex["id"]))
                            rep["updated"] += 1
                            note_preview(line, "обновлён", site, ptype, pq)
                        else:
                            rep["skipped"] += 1
                            note_preview(line, "без изменений", site, ptype, pq)
                    else:
                        rep["skipped"] += 1
                        note_preview(line, "уже есть", site, ptype, pq)
                except ValueError as e:
                    rep["errors"].append({"line": line, "msg": str(e), "raw": (r.get("url") or "")[:120]})
            if mode == "replace" and not rep["errors"]:
                for site in touched_sites:
                    for ex in db.execute("SELECT id, page_type, path_qs FROM ref_urls WHERE site=?", (site,)).fetchall():
                        if (ex["page_type"], ex["path_qs"]) not in file_keys.get(site, set()):
                            db.execute("DELETE FROM ref_urls WHERE id=?", (ex["id"],))
                            rep["deleted"] += 1
            elif mode == "replace" and rep["errors"]:
                rep["skipped_replace"] = True  # из-за ошибок в файле ничего не удаляем
            rep["sites"] = sorted(touched_sites)
            rep["new_types"] = sorted(set(rep["new_types"]))
            if not dry_run:
                log_change(db, "import", "url", f"{mode}: +{rep['added']} ~{rep['updated']} -{rep['deleted']}, "
                                                f"ошибок {len(rep['errors'])}")
            else:
                raise Rollback()
    except Rollback:
        pass
    return rep


# ------------------------------------------------------------------ малые справочники (сети, типы, ответственные)

def entity_rows(db, name: str, q: str | None = None) -> list[dict]:
    e = ENTITIES[name]
    order = {"sites": "rowid", "types": "rowid", "owners": "page_type, email"}[name]
    rows = [dict(r) for r in db.execute(f"SELECT * FROM {e['table']} ORDER BY {order}")]
    if q:
        ql = q.lower()
        rows = [r for r in rows if any(ql in str(v).lower() for v in r.values())]
    return rows


def _entity_clean(name: str, data: dict) -> dict:
    e = ENTITIES[name]
    clean = {f: (data.get(f) or "").strip() for f in e["fields"]}
    for k in e["key"]:
        if not clean[k]:
            raise ValueError(f"поле «{e['label'][k]}» обязательно")
    if name == "sites" and not re.fullmatch(r"[A-Za-z0-9_-]+", clean["site"]):
        raise ValueError("ключ сети — латиница, цифры, _ и -")
    if name == "types" and not re.fullmatch(r"[A-Za-z0-9_]+", clean["slug"]):
        raise ValueError("код типа — латиница, цифры и _")
    if name == "types" and not clean["grp"]:
        clean["grp"] = "aux"
    if name == "sites":
        clean["host"] = re.sub(r"^https?://", "", clean["host"]).strip("/").lower()
    if name == "owners" and "@" not in clean["email"]:
        raise ValueError("некорректная почта")
    return clean


def entity_add(db, name: str, data: dict) -> None:
    e = ENTITIES[name]
    clean = _entity_clean(name, data)
    with tx(db):
        where = " AND ".join(f"{k}=?" for k in e["key"])
        if db.execute(f"SELECT 1 FROM {e['table']} WHERE {where}", [clean[k] for k in e["key"]]).fetchone():
            raise ValueError("такая запись уже есть")
        cols = e["fields"]
        db.execute(f"INSERT INTO {e['table']}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   [clean[c] for c in cols])
        log_change(db, "add", name, "/".join(clean[k] for k in e["key"]))


def entity_update(db, name: str, row_id: str, data: dict) -> None:
    e = ENTITIES[name]
    clean = _entity_clean(name, data)
    with tx(db):
        if name == "owners":
            if not db.execute("SELECT 1 FROM ref_owners WHERE id=?", (row_id,)).fetchone():
                raise ValueError("запись не найдена")
            dup = db.execute("SELECT id FROM ref_owners WHERE page_type=? AND email=? AND area=? AND id!=?",
                             (clean["page_type"], clean["email"], clean["area"], row_id)).fetchone()
            if dup:
                raise ValueError("такая запись уже есть")
            db.execute("UPDATE ref_owners SET page_type=?, email=?, area=?, coverage=? WHERE id=?",
                       (clean["page_type"], clean["email"], clean["area"], clean["coverage"], row_id))
        else:
            key = e["key"][0]
            if not db.execute(f"SELECT 1 FROM {e['table']} WHERE {key}=?", (row_id,)).fetchone():
                raise ValueError("запись не найдена")
            if clean[key] != row_id:
                # переименование ключа: переносим ссылки
                if db.execute(f"SELECT 1 FROM {e['table']} WHERE {key}=?", (clean[key],)).fetchone():
                    raise ValueError("такой ключ уже существует")
                if name == "sites":
                    db.execute("UPDATE ref_urls SET site=? WHERE site=?", (clean[key], row_id))
                else:
                    db.execute("UPDATE ref_urls SET page_type=? WHERE page_type=?", (clean[key], row_id))
                    db.execute("UPDATE ref_owners SET page_type=? WHERE page_type=?", (clean[key], row_id))
            sets = ", ".join(f"{f}=?" for f in e["fields"])
            db.execute(f"UPDATE {e['table']} SET {sets} WHERE {key}=?", [clean[f] for f in e["fields"]] + [row_id])
        log_change(db, "update", name, str(row_id))


def entity_delete(db, name: str, row_id: str) -> None:
    e = ENTITIES[name]
    with tx(db):
        if name == "sites":
            n = db.execute("SELECT COUNT(*) FROM ref_urls WHERE site=?", (row_id,)).fetchone()[0]
            if n:
                raise ValueError(f"в сети {n} URL — сначала удалите их или перенесите")
        if name == "types":
            n = db.execute("SELECT COUNT(*) FROM ref_urls WHERE page_type=?", (row_id,)).fetchone()[0]
            if n:
                raise ValueError(f"тип используется в {n} URL — сначала удалите их или смените тип")
            db.execute("DELETE FROM ref_owners WHERE page_type=?", (row_id,))
        key = "id" if name == "owners" else e["key"][0]
        db.execute(f"DELETE FROM {e['table']} WHERE {key}=?", (row_id,))
        log_change(db, "delete", name, str(row_id))


def import_entity(db, name: str, text: str, *, dry_run: bool = False) -> dict:
    """Массовое добавление/обновление малых справочников. Существующие записи обновляются."""
    e = ENTITIES[name]
    rows, unknown = parse_table(text, e["aliases"])
    rep = {"added": 0, "updated": 0, "skipped": 0, "errors": [], "unknown_columns": unknown, "total": len(rows),
           "dry_run": dry_run}
    try:
        with tx(db):
            for line, r in rows:
                try:
                    clean = _entity_clean(name, r)
                    where = " AND ".join(f"{k}=?" for k in e["key"])
                    ex = db.execute(f"SELECT * FROM {e['table']} WHERE {where}", [clean[k] for k in e["key"]]).fetchone()
                    if ex is None:
                        cols = e["fields"]
                        db.execute(f"INSERT INTO {e['table']}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                   [clean[c] for c in cols])
                        rep["added"] += 1
                    else:
                        changed = {f: clean[f] for f in e["fields"] if f not in e["key"] and f in r and clean[f] != ex[f]}
                        if changed:
                            sets = ", ".join(f"{f}=?" for f in changed)
                            db.execute(f"UPDATE {e['table']} SET {sets} WHERE {where}", [*changed.values(), *[clean[k] for k in e["key"]]])
                            rep["updated"] += 1
                        else:
                            rep["skipped"] += 1
                except ValueError as ex_:
                    rep["errors"].append({"line": line, "msg": str(ex_), "raw": ""})
            if dry_run:
                raise Rollback()
            log_change(db, "import", name, f"+{rep['added']} ~{rep['updated']}")
    except Rollback:
        pass
    return rep


def recent_changes(db, limit: int = 100) -> list[dict]:
    return [dict(r) for r in db.execute("SELECT * FROM ref_changes ORDER BY id DESC LIMIT ?", (limit,))]


def counts(db) -> dict:
    return {
        "urls": db.execute("SELECT COUNT(*) FROM ref_urls").fetchone()[0],
        "active": db.execute("SELECT COUNT(*) FROM ref_urls WHERE status='active'").fetchone()[0],
        "sites": db.execute("SELECT COUNT(*) FROM ref_sites").fetchone()[0],
        "types": db.execute("SELECT COUNT(*) FROM ref_page_types").fetchone()[0],
    }


# ------------------------------------------------------------------ экспорт

CSV_TEMPLATE = ("site;type;url;note;status;noindex;nofollow\n5ka;home;/;Главная;active;;\n"
                "5ka;cat_l1;/catalog/;Первый уровень каталога;active;;\n5ka;card;/card/;Страница с noindex;active;1;\n")


def export_csv(db, site: str | None = None, *, delimiter: str = ";") -> str:
    rows, _ = list_urls(db, site=site, limit=10**9)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    w.writerow(["site", "type", "url", "note", "status", "noindex", "nofollow"])
    for r in rows:
        w.writerow([r["site"], r["page_type"], r["url"], r["note"], r["status"],
                    1 if r["noindex"] else "", 1 if r["nofollow"] else ""])
    return buf.getvalue()


def sync_csv_files(db, registry_dir: Path, networks: list[str] | None = None) -> list[str]:
    """Зеркало БД → registry/<сеть>.csv (только активные URL). Оригиналы один раз копируются
    в registry/_backup_before_db/. Возвращает список записанных файлов."""
    registry_dir = Path(registry_dir)
    registry_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in registry_dir.glob("*.csv") if not p.name.startswith("_")}
    sites = {r["site"]: r for r in db.execute("SELECT * FROM ref_sites")}
    targets = [s for s in sites if (s in existing or (networks and s in networks))]
    backup = registry_dir / "_backup_before_db"
    written = []
    for site in targets:
        rows = db.execute(
            "SELECT u.page_type, u.url, u.note, u.noindex, u.nofollow FROM ref_urls u LEFT JOIN ref_page_types t ON t.slug=u.page_type "
            "WHERE u.site=? AND u.status='active' ORDER BY COALESCE(t.rowid,1e9), u.id", (site,)).fetchall()
        if not rows:
            continue
        path = registry_dir / f"{site}.csv"
        if path.exists() and not backup.exists():
            backup.mkdir(exist_ok=True)
        if path.exists() and backup.exists() and not (backup / path.name).exists():
            shutil.copy2(path, backup / path.name)
        buf = io.StringIO()
        buf.write(f"# {sites[site]['name_ru'] or site}. Файл создаётся веб-интерфейсом из БД — правьте справочник там. "
                  f"Хост подставляется из NET_{re.sub(r'[^A-Za-z0-9]+', '_', site).upper()}_URL.\n")
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["type", "url", "note", "noindex", "nofollow"])
        for r in rows:
            w.writerow([r["page_type"], r["url"], r["note"], 1 if r["noindex"] else "", 1 if r["nofollow"] else ""])
        tmp = path.with_suffix(".csv.tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        tmp.replace(path)
        written.append(path.name)
    return written


# ------------------------------------------------------------------ чтение для сборщика

def load_entries(db, site: str) -> list[RegistryEntry]:
    rows = db.execute(
        "SELECT u.page_type, u.url, u.path_qs, u.note, u.noindex, u.nofollow FROM ref_urls u LEFT JOIN ref_page_types t ON t.slug=u.page_type "
        "WHERE u.site=? AND u.status='active' ORDER BY COALESCE(t.rowid,1e9), u.id", (site,)).fetchall()
    seen, out = set(), []
    for r in rows:
        key = (r["page_type"], r["path_qs"])
        if key not in seen:
            seen.add(key)
            out.append(RegistryEntry(site, r["page_type"], r["url"], r["path_qs"], r["note"], bool(r["noindex"]), bool(r["nofollow"])))
    return out


def entries_for(settings, site: str) -> list[RegistryEntry]:
    """Список страниц сети для сборщика: из БД, а если для сети там пусто — из registry/<сеть>.csv."""
    db_path = Path(settings.data_dir) / "techaudit.db"
    if db_path.exists():
        db = connect(db_path)
        try:
            db.executescript(SCHEMA)
            migrate(db)
            if db.execute("SELECT 1 FROM ref_urls WHERE site=? LIMIT 1", (site,)).fetchone():
                return load_entries(db, site)
        finally:
            db.close()
    return load_registry(Path(settings.registry_dir) / f"{site}.csv", site)


# ------------------------------------------------------------------ первичная загрузка из файлов проекта

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def seed_from_files(db, refs_dir: Path, registry_dir: Path) -> dict:
    """Идемпотентный импорт: справочники/*.csv и registry/*.csv → БД. Уже существующее не перезаписывается."""
    refs_dir, registry_dir = Path(refs_dir), Path(registry_dir)
    rep = {"sites": 0, "types": 0, "owners": 0, "urls": 0, "errors": []}
    for name, fname in (("sites", "sites.csv"), ("types", "page_types.csv"), ("owners", "owners.csv")):
        p = refs_dir / fname
        if p.exists():
            r = import_entity(db, name, _read(p))
            rep[name] = r["added"]
            rep["errors"] += [f"{fname}: строка {e['line']}: {e['msg']}" for e in r["errors"]]
    for p in sorted(registry_dir.glob("*.csv")) if registry_dir.exists() else []:
        if p.name.startswith("_"):
            continue
        site = p.stem
        if not db.execute("SELECT 1 FROM ref_sites WHERE site=?", (site,)).fetchone():
            db.execute("INSERT INTO ref_sites(site, name_ru, host) VALUES (?,?,?)", (site, site, ""))
        r = import_urls(db, _read(p), mode="add", default_site=site, create_types=True)
        rep["urls"] += r["added"]
        rep["errors"] += [f"{p.name}: строка {e['line']}: {e['msg']}" for e in r["errors"]]
    p = refs_dir / "urls.csv"
    if p.exists():
        r = import_urls(db, _read(p), mode="add", create_types=True)
        rep["urls"] += r["added"]
        # строки «удалена» в urls.csv без адреса — норма, не ошибка
        rep["errors"] += [f"urls.csv: строка {e['line']}: {e['msg']}" for e in r["errors"] if e["msg"] != "пустой URL"]
    return rep
