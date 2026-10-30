"""Ядро проверок: результат, реестр, настройки из env, разбор страницы (PageDoc), роли страниц."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import lxml.html

# ---------------------------------------------------------------- результат

PASS, FAIL, WARN, SKIPPED, NA = "pass", "fail", "warn", "skipped", "na"
STATUS_ORDER = {FAIL: 0, WARN: 1, SKIPPED: 2, NA: 3, PASS: 4}
STATUS_LABELS = {PASS: "Пройдено", FAIL: "Не пройдено", WARN: "Требует внимания", SKIPPED: "Пропущено", NA: "Нет данных"}


@dataclass
class Item:
    """Одна проверяемая единица: страница, проба, адрес."""
    url: str
    ok: bool
    msg: str = ""
    kind: str = ""  # page_type или вид пробы


@dataclass
class Result:
    status: str
    summary: str
    items: list[Item] = field(default_factory=list)
    source: str = "html"  # html | probe | live | external | manual
    stats: dict | None = None  # {'total': N, 'bad': M}: если не задано — считается по items

    @property
    def failed(self) -> list[Item]:
        return [i for i in self.items if not i.ok]


def from_items(items: list[Item], *, what: str = "страниц", ok_text: str = "", warn_only: bool = False,
               source: str = "html") -> Result:
    """Сводит список Item в Result: все ок → pass, иначе fail (или warn)."""
    if not items:
        return Result(NA, f"нет {what} для проверки", source=source)
    bad = [i for i in items if not i.ok]
    if not bad:
        return Result(PASS, ok_text or f"{len(items)} из {len(items)} {what} соответствуют", items, source)
    return Result(WARN if warn_only else FAIL, f"{len(bad)} из {len(items)} {what} не соответствуют", items, source)


# ---------------------------------------------------------------- реестр

@dataclass
class CheckDef:
    id: str
    titles: tuple[str, ...]  # подстроки названия пункта чек-листа (нижний регистр) для сопоставления со строкой Excel
    fn: Callable
    source: str


REGISTRY: dict[str, CheckDef] = {}


def check(check_id: str, *titles: str, source: str = "html"):
    def deco(fn):
        REGISTRY[check_id] = CheckDef(check_id, tuple(t.lower() for t in titles), fn, source)
        return fn
    return deco


# ---------------------------------------------------------------- настройки проверок (env)

@dataclass(frozen=True)
class CheckSettings:
    ttfb_max_ms: float = 600
    min_text_chars: int = 500
    min_links: int = 10
    alt_warn: float = 0.05
    alt_fail: float = 0.20
    live_limit: int = 60
    pagination_param: str = "page"
    og_min_w: int = 600
    og_min_h: int = 315
    old_hosts: tuple[str, ...] = ()
    role_overrides: dict = field(default_factory=dict)
    noindex_types: tuple[str, ...] = ()
    nav_types: tuple[str, ...] = ("catalog", "cat_l1")  # разводящие: товаров на странице не ожидается
    # --- дополнительные проверки (x_*.py)
    title_min: int = 10
    title_max: int = 70
    desc_min: int = 50
    desc_max: int = 200
    max_depth: int = 4
    html_warn_kb: int = 500
    html_fail_kb: int = 1500
    dom_warn: int = 1500
    dom_fail: int = 3000
    blocking_scripts_max: int = 2
    cert_min_days: int = 30
    hsts_min_age: int = 15552000
    dup_text_sim: float = 0.9

    @staticmethod
    def from_env() -> "CheckSettings":
        e = os.environ

        def num(name, default, cast=float):
            try:
                return cast(e.get(name) or default)
            except ValueError:
                return default

        def lst(name):
            return tuple(x.strip().lower() for x in (e.get(name) or "").split(",") if x.strip())

        overrides = {}
        for part in (e.get("ROLE_OVERRIDES") or "").split(","):
            if ":" in part:
                t, r = part.split(":", 1)
                overrides[t.strip().lower()] = r.strip().lower()
        return CheckSettings(
            ttfb_max_ms=num("TTFB_MAX_MS", 600), min_text_chars=num("MIN_TEXT_CHARS", 500, int),
            min_links=num("MIN_LINKS", 10, int), alt_warn=num("ALT_WARN_SHARE", 0.05),
            alt_fail=num("ALT_FAIL_SHARE", 0.20), live_limit=num("LIVE_LIMIT", 60, int),
            pagination_param=(e.get("PAGINATION_PARAM") or "page").strip(),
            og_min_w=num("OG_MIN_WIDTH", 600, int), og_min_h=num("OG_MIN_HEIGHT", 315, int),
            old_hosts=lst("OLD_HOSTS"), role_overrides=overrides, noindex_types=lst("NOINDEX_TYPES"),
            nav_types=lst("NAV_TYPES") or ("catalog", "cat_l1"),
            title_min=num("TITLE_MIN", 10, int), title_max=num("TITLE_MAX", 70, int),
            desc_min=num("DESC_MIN", 50, int), desc_max=num("DESC_MAX", 200, int), max_depth=num("MAX_DEPTH", 4, int),
            html_warn_kb=num("HTML_WARN_KB", 500, int), html_fail_kb=num("HTML_FAIL_KB", 1500, int),
            dom_warn=num("DOM_WARN", 1500, int), dom_fail=num("DOM_FAIL", 3000, int),
            blocking_scripts_max=num("BLOCKING_SCRIPTS_MAX", 2, int), cert_min_days=num("CERT_MIN_DAYS", 30, int),
            hsts_min_age=num("HSTS_MIN_AGE", 15552000, int), dup_text_sim=num("DUP_TEXT_SIM", 0.9),
        )


# ---------------------------------------------------------------- роли страниц

ROLE_PATTERNS = [  # (роль, regex по типу из справочника) — первое совпадение
    ("home", r"^(home|main|index)$"),
    ("search", r"^search"),
    ("product", r"^(product|card_product)(?!.*review)"),
    ("filter", r"filter|^tags?$"),
    ("listing", r"^(cat_|catalog|category|subcategory|discount_|brand_(sub)?(category|discount)|brands_index|calories_(section|category)|collection)"),
    ("info", r"."),
]


def role_of(page_type: str, cfg: CheckSettings | None = None) -> str:
    t = (page_type or "").lower()
    if cfg and t in cfg.role_overrides:
        return cfg.role_overrides[t]
    for role, pat in ROLE_PATTERNS:
        if re.search(pat, t):
            return role
    return "info"


def expects_noindex(page_type: str, url: str, cfg: CheckSettings) -> bool:
    t = (page_type or "").lower()
    if t in cfg.noindex_types or role_of(t, cfg) == "search":
        return True
    q = urlsplit(url).query.lower()
    return bool(re.search(r"(^|&)(sort|order|orderby)=", q)) or bool(re.search(r"/(cart|basket|lk|profile|personal|login)(/|$)", url.lower()))


def page_noindex(p, cfg: CheckSettings) -> bool:
    """noindex — норма для страницы: флажок в справочнике, тип/адрес (expects_noindex)."""
    return bool(getattr(p, "reg_noindex", False)) or expects_noindex(p.page_type, p.url, cfg)


# ---------------------------------------------------------------- страница

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


@dataclass
class PageDoc:
    network: str
    page_type: str
    variant: str
    source_url: str
    url: str
    final_url: str
    status: int | None
    headers: dict
    ttfb_ms: float | None
    raw: bytes
    base_url: str = ""
    reg_noindex: bool = False   # флажок справочника: noindex на странице — норма
    reg_nofollow: bool = False  # флажок справочника: nofollow на странице — норма

    @property
    def role(self) -> str:
        return role_of(self.page_type)

    @cached_property
    def text(self) -> str:
        for enc in ("utf-8", "cp1251"):
            try:
                return self.raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return self.raw.decode("utf-8", "replace")

    @cached_property
    def doc(self):
        if not self.raw.strip():
            return None
        t = re.sub(r"^\s*<\?xml[^>]*\?>", "", self.text)
        try:
            return lxml.html.fromstring(t)
        except Exception:  # noqa: BLE001
            return None

    def xp(self, expr: str):
        d = self.doc
        return d.xpath(expr) if d is not None else []

    @cached_property
    def visible_text(self) -> str:
        d0 = self.doc
        if d0 is None:
            return ""
        import copy
        d = copy.deepcopy(d0)  # не портим общий разобранный документ: другим проверкам нужны script/ld+json
        for bad in d.xpath("//script|//style|//noscript|//template"):
            bad.drop_tree()
        return _norm_ws(d.text_content())

    @cached_property
    def meta(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for el in self.xp("//meta"):
            key = (el.get("property") or el.get("name") or el.get("http-equiv") or "").strip().lower()
            if key:
                out.setdefault(key, []).append((el.get("content") or "").strip())
        return out

    def meta1(self, key: str) -> str | None:
        v = self.meta.get(key.lower())
        return v[0] if v else None

    @cached_property
    def canonicals(self) -> list:
        return [el for el in self.xp("//link[@href]") if "canonical" in (el.get("rel") or "").lower().split()]

    @cached_property
    def links(self) -> list[str]:
        """Абсолютные href всех <a>."""
        out = []
        for a in self.xp("//a[@href]"):
            h = (a.get("href") or "").strip()
            if h and not h.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
                out.append(urljoin(self.final_url or self.url, h))
        return out

    def internal(self, url: str) -> bool:
        from ..netutil import same_site
        return same_site(urlsplit(url).hostname, urlsplit(self.base_url or self.url).hostname or "")

    # ---- структурированные данные
    @cached_property
    def jsonld(self) -> tuple[list[dict], list[str]]:
        """(сущности, ошибки разбора). @graph раскрывается."""
        ents, errs = [], []
        for s in self.xp('//script[@type="application/ld+json"]'):
            raw = (s.text or "").strip()
            if not raw:
                errs.append("пустой блок ld+json")
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                errs.append(f"невалидный JSON: {str(e)[:60]}")
                continue
            stack = data if isinstance(data, list) else [data]
            while stack:
                d = stack.pop(0)
                if not isinstance(d, dict):
                    continue
                if "@graph" in d:
                    stack += d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]]
                    continue
                ents.append(d)
        return ents, errs

    @cached_property
    def microdata(self) -> list[dict]:
        """Плоский список сущностей microdata: {types:[...], props:{name:value}, el}."""
        out = []
        for el in self.xp("//*[@itemscope]"):
            types = [t.rsplit("/", 1)[-1] for t in (el.get("itemtype") or "").split()]
            props: dict[str, str] = {}
            for p in el.xpath(".//*[@itemprop]"):
                # свойства принадлежат ближайшему предку с itemscope
                owner = next((a for a in p.iterancestors() if a.get("itemscope") is not None), None)
                if owner is not el:
                    continue
                name = p.get("itemprop")
                if p.get("content") is not None:
                    val = p.get("content")
                elif p.tag in ("a", "link") and p.get("href"):
                    val = p.get("href")
                elif p.tag in ("img", "source", "meta") and (p.get("src") or p.get("content")):
                    val = p.get("src") or p.get("content")
                elif p.get("itemscope") is not None:
                    val = "[item]"
                else:
                    val = _norm_ws(p.text_content())
                props.setdefault(name, val)
            out.append({"types": types, "props": props, "el": el})
        return out

    def types_found(self) -> dict[str, set[str]]:
        """{тип: {'jsonld','microdata'}}"""
        res: dict[str, set[str]] = {}
        for e in self.jsonld[0]:
            for t in _as_list(e.get("@type")):
                res.setdefault(t, set()).add("jsonld")
        for m in self.microdata:
            for t in m["types"]:
                res.setdefault(t, set()).add("microdata")
        return res

    @cached_property
    def head_end(self) -> int:
        i = self.raw.lower().find(b"</head")
        return i if i >= 0 else len(self.raw)


def _as_list(v) -> list[str]:
    if v is None:
        return []
    return [str(x).rsplit("/", 1)[-1] for x in (v if isinstance(v, list) else [v])]


as_list = _as_list


def norm_path(url: str) -> str:
    p = urlsplit(url)
    return (p.path or "/").lower().rstrip("/") or "/"
