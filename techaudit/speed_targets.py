"""Per-network PageSpeed coverage, independent of HTML collection limits."""
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlsplit

from .checks.core import CheckSettings, expects_noindex, role_of
from .registry import load_registry, path_qs

SCHEMA = """
CREATE TABLE IF NOT EXISTS speed_target_settings (
  network TEXT PRIMARY KEY, mode TEXT NOT NULL, page_types TEXT NOT NULL, urls TEXT NOT NULL
);
"""
TRAFFIC_ROLES = {"home", "listing", "product"}


def config(db, network):
    try:
        row = db.execute("SELECT mode, page_types, urls FROM speed_target_settings WHERE network=?", (network,)).fetchone()
    except sqlite3.OperationalError as e:
        if 'no such table' not in str(e):
            raise
        row = None
    return {"mode": row[0], "page_types": json.loads(row[1]), "urls": json.loads(row[2])} if row else {
        "mode": "traffic", "page_types": [], "urls": []}


def entries(db, network, registry_dir=None):
    try:
        rows = db.execute("SELECT page_type, path_qs, noindex, status FROM ref_urls WHERE site=? "
                          "ORDER BY page_type, path_qs", (network,)).fetchall()
    except sqlite3.OperationalError as e:
        if 'no such table' not in str(e):
            raise
        rows = []
    if rows:
        return [dict(page_type=r[0], path=r[1], noindex=bool(r[2])) for r in rows if r[3] == 'active']
    if registry_dir is not None:
        return [dict(page_type=e.type, path=e.path_qs, noindex=e.noindex)
                for e in load_registry(registry_dir / f'{network}.csv', network)]
    return []


def select(db, network, base_url, cs=None, registry_dir=None):
    if not base_url:
        return []
    cs = cs or CheckSettings.from_env()
    cfg = config(db, network)
    out, seen = [], set()
    for row in entries(db, network, registry_dir):
        kind = row['page_type']
        chosen = role_of(kind, cs) in TRAFFIC_ROLES if cfg['mode'] == 'traffic' else kind in cfg['page_types']
        url = base_url.rstrip('/') + row['path']
        if not chosen or row['noindex'] or expects_noindex(kind, url, cs) or url in seen:
            continue
        seen.add(url)
        out.append(SimpleNamespace(url=url, page_type=kind))
    # Explicit URLs are intentional additions, including pages outside the registry.
    for raw in cfg['urls']:
        url = normalize_url(raw, base_url)
        if url not in seen:
            seen.add(url)
            out.append(SimpleNamespace(url=url, page_type='custom'))
    return out


def normalize_url(raw, base_url):
    raw = raw.strip()
    parsed, base = urlsplit(raw), urlsplit(base_url)
    if base.scheme not in ('http', 'https') or not base.netloc:
        raise ValueError('Сначала задайте адрес сайта для сети.')
    if not raw or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('Укажите адрес страницы без фрагмента и данных входа.')
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != base.netloc.lower():
            raise ValueError('Дополнительный URL должен относиться к адресу выбранной сети.')
    elif not raw.startswith('/') or raw.startswith('//'):
        raise ValueError('Укажите полный URL сети или путь, начинающийся с /.')
    return base_url.rstrip('/') + path_qs(raw)


def save(db, network, base_url, mode, page_types, urls):
    if mode not in ('traffic', 'custom'):
        raise ValueError('Неизвестный способ выбора страниц.')
    types = sorted(set(t.strip() for t in page_types if t.strip()))
    normalized = list(dict.fromkeys(path_qs(normalize_url(line, base_url)) for line in urls.splitlines() if line.strip()))
    db.execute('INSERT INTO speed_target_settings(network, mode, page_types, urls) VALUES (?,?,?,?) '
               'ON CONFLICT(network) DO UPDATE SET mode=excluded.mode, page_types=excluded.page_types, urls=excluded.urls',
               (network, mode, json.dumps(types), json.dumps(normalized)))


def view(db, network, base_url, registry_dir=None):
    cs = CheckSettings.from_env()
    cfg = config(db, network)
    counts = {}
    for row in entries(db, network, registry_dir):
        kind = row['page_type']
        counts[kind] = counts.get(kind, 0) + (not row['noindex'] and not expects_noindex(kind, row['path'], cs))
    options = [{"name": kind, "count": counts.get(kind, 0), "traffic": role_of(kind, cs) in TRAFFIC_ROLES}
               for kind in sorted(set(counts) | set(cfg['page_types']))]
    return {**cfg, "options": options, "count": len(select(db, network, base_url, cs, registry_dir))}
