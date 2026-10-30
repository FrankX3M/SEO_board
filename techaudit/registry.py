"""Справочник страниц: по одному CSV на сеть (registry/<сеть>.csv).

Формат (заголовок обязателен, строки с # игнорируются):

    type,url,note
    main,/,Главная
    category,https://5ka.ru/catalog/molochnye-produkty-yaytsa--251C51940/,Категория

url — путь или абсолютный адрес. Из него берётся только путь+query, а хост
подставляется по выбранному окружению (prod/preprod), поэтому один справочник
работает для обоих. Регистр и слеш в конце сохраняются как есть.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RegistryEntry:
    network: str
    type: str
    source_url: str  # как записано в справочнике
    path_qs: str  # путь + query
    note: str = ""
    noindex: bool = False  # noindex на странице — норма (флажок справочника)
    nofollow: bool = False


def path_qs(raw: str) -> str:
    p = urlsplit(raw.strip())
    path = p.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path + (f"?{p.query}" if p.query else "")


def load_registry(path: Path, network: str) -> list[RegistryEntry]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    rows = csv.DictReader(lines)
    if not rows.fieldnames or not {"type", "url"} <= {h.strip() for h in rows.fieldnames}:
        raise ValueError(f"{path}: нужен заголовок 'type,url[,note]'")
    seen, out = set(), []
    for r in rows:
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        if not r.get("url"):
            continue
        flag = lambda k: (r.get(k) or "").strip().lower() in ("1", "да", "yes", "true", "+", "x")
        entry = RegistryEntry(network, r.get("type") or "unknown", r["url"], path_qs(r["url"]), r.get("note", ""),
                              flag("noindex"), flag("nofollow"))
        key = (entry.type, entry.path_qs)
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


def build_url(base_url: str, entry: RegistryEntry) -> str:
    return base_url.rstrip("/") + entry.path_qs
