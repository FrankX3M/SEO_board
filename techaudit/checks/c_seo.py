"""Meta robots / X-Robots-Tag и canonical."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from .core import FAIL, NA, PASS, SKIPPED, WARN, Item, Result, check, expects_noindex, from_items, page_noindex, norm_path, role_of

BOT_META = ("robots", "googlebot", "yandex", "yandexbot", "bingbot")


def _robots_directives(p) -> str:
    return " ".join(v for k in BOT_META for v in p.meta.get(k, [])).lower()


def _xrobots(p) -> str:
    return (p.headers.get("x-robots-tag") or "").lower()


def _indexable_pages(ctx):
    return [p for p in ctx.primary() if not page_noindex(p, ctx.cs)]


@check("noindex_accidental", "нет случайного noindex")
def noindex_accidental(ctx):
    pages = _indexable_pages(ctx)
    if not pages:
        return Result(NA, "нет индексируемых страниц")
    items = []
    for p in pages:
        d = _robots_directives(p)
        items.append(Item(p.url, "noindex" not in d and "none" not in d.split(), f"meta robots: {d or 'не задан'}", p.page_type))
    return from_items(items, what="страниц")


@check("nofollow_accidental", "нет случайного nofollow")
def nofollow_accidental(ctx):
    pages = [p for p in _indexable_pages(ctx) if not getattr(p, "reg_nofollow", False)]  # nofollow отмечен как норма
    if not pages:
        return Result(NA, "нет индексируемых страниц")
    items = []
    for p in pages:
        d = _robots_directives(p)
        items.append(Item(p.url, "nofollow" not in d and "none" not in d.split(), f"meta robots: {d or 'не задан'}", p.page_type))
    return from_items(items, what="страниц")


@check("x_robots_tag", "x-robots-tag")
def x_robots_tag(ctx):
    pages = [p for v in ("bot", "plain") for p in ctx.pages(v)]
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        h = _xrobots(p)
        bad = ("noindex" in h or "none" in h.split(",")) and not page_noindex(p, ctx.cs)
        items.append(Item(p.url, not bad, f"{p.variant}: X-Robots-Tag: {h or 'нет'}", p.page_type))
    return from_items(items, what="ответов")


@check("noindex_where_needed", "директива noindex стоит там")
def noindex_where_needed(ctx):
    pages = [p for p in ctx.primary() if page_noindex(p, ctx.cs)]
    if not pages:
        return Result(NA, "в справочнике нет страниц, где noindex обязателен (поиск, сортировки, корзина, ЛК) — "
                          "добавьте type=search или перечислите типы в NOINDEX_TYPES")
    items = []
    for p in pages:
        d = _robots_directives(p) + " " + _xrobots(p)
        items.append(Item(p.url, "noindex" in d, f"directive: {d.strip() or 'не задана'}", p.page_type))
    return from_items(items, what="страниц")


@check("noindex_conflicts", "нет конфликта noindex")
def noindex_conflicts(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    sm = ctx.sitemap["urls"]
    items = []
    for p in pages:
        d = _robots_directives(p) + " " + _xrobots(p)
        if "noindex" not in d:
            continue
        can = p.canonicals[0].get("href") if p.canonicals else None
        in_sm = bool(sm) and (can in sm or p.final_url in sm)
        bad = []
        if can and norm_path(can) != norm_path(p.final_url):
            bad.append("noindex + canonical на другой URL")
        if in_sm:
            bad.append("страница с noindex есть в sitemap")
        items.append(Item(p.url, not bad, "; ".join(bad) or ("noindex без конфликтов" + ("" if sm else " (карта не собрана)")), p.page_type))
    if not items:
        return Result(PASS, "страниц с noindex в выборке нет — конфликтовать нечему")
    return from_items(items, what="страниц с noindex")


# ---------------------------------------------------------------- canonical

def _canon(p):
    return p.canonicals[0].get("href", "").strip() if p.canonicals else ""


@check("canonical_present", "присутствует на всех индексируемых")
def canonical_present(ctx):
    pages = _indexable_pages(ctx)
    if not pages:
        return Result(NA, "нет индексируемых страниц")
    items = []
    for p in pages:
        n = len(p.canonicals)
        in_head = all(any(a.tag == "head" for a in c.iterancestors()) for c in p.canonicals)
        if n == 1 and in_head:
            items.append(Item(p.url, True, "один canonical в head", p.page_type))
        else:
            why = "нет canonical" if n == 0 else f"canonical: {n} шт." if n > 1 else "canonical вне <head>"
            items.append(Item(p.url, False, why, p.page_type))
    return from_items(items, what="страниц")


@check("canonical_absolute", "абсолютный url с https")
def canonical_absolute(ctx):
    pages = [p for p in _indexable_pages(ctx) if p.canonicals]
    if not pages:
        return Result(NA, "нет страниц с canonical")
    base = urlsplit(ctx.base_url)
    items = []
    for p in pages:
        u = urlsplit(_canon(p))
        bad = []
        if u.scheme != "https":
            bad.append("не https" if u.scheme else "относительный URL")
        if u.hostname and u.hostname.lower() != (base.hostname or "").lower():
            bad.append(f"хост {u.hostname} ≠ {base.hostname}")
        if any(h in (u.hostname or "") for h in ctx.cs.old_hosts):
            bad.append("старый/тестовый домен")
        items.append(Item(p.url, not bad, "; ".join(bad) or _canon(p), p.page_type))
    return from_items(items, what="страниц")


@check("canonical_exact", "совпадает с фактическим url")
def canonical_exact(ctx):
    pages = [p for p in _indexable_pages(ctx) if p.canonicals and not urlsplit(p.url).query]
    if not pages:
        return Result(NA, "нет страниц без параметров")
    items = []
    for p in pages:
        c, f = _canon(p), p.final_url
        ok = c == f
        why = "совпадает" if ok else ("различается регистром" if c.lower() == f.lower() else
                                      "различается слешем" if c.rstrip("/") == f.rstrip("/") else "другой адрес")
        items.append(Item(p.url, ok, f"{why}: canonical={c} | url={f}" if not ok else why, p.page_type))
    return from_items(items, what="страниц")


@check("canonical_self", "обычные страницы ссылаются сами на себя")
def canonical_self(ctx):
    pages = [p for p in _indexable_pages(ctx) if p.canonicals and role_of(p.page_type, ctx.cs) in ("home", "product", "info", "listing")
             and not urlsplit(p.url).query]
    if not pages:
        return Result(NA, "нет подходящих страниц")
    items = []
    for p in pages:
        ok = norm_path(_canon(p)) == norm_path(p.final_url) and not urlsplit(_canon(p)).query
        items.append(Item(p.url, ok, "самоканоникал" if ok else f"ведёт на {_canon(p)}", p.page_type))
    return from_items(items, what="страниц")


def _canon_targets(ctx):
    """[(page, canonical_url)] только для страниц, чей canonical не совпадает с их собственным адресом."""
    out = []
    for p in _indexable_pages(ctx):
        c = _canon(p)
        if c and c != p.final_url:
            out.append((p, c))
    return out


def _canon_status(ctx, name: str, test):
    pages = [p for p in _indexable_pages(ctx) if p.canonicals]
    if not pages:
        return Result(NA, "нет страниц с canonical")
    live = {r["url"]: r for r in ctx.live("canonical")}
    items, need_live = [], 0
    for p in pages:
        c = _canon(p)
        if c == p.final_url:  # цель — сама страница, её статус уже известен
            bad = test(p.status, 1, "noindex" in _robots_directives(p) + _xrobots(p))
            items.append(Item(p.url, not bad, bad or "canonical = сама страница, 200", p.page_type))
        elif c in live:
            r = live[c]
            bad = test(r["status"], r["hops"] or 1, bool(r["meta_d"].get("noindex")))
            items.append(Item(p.url, not bad, bad or f"цель {c}: 200", p.page_type))
        else:
            need_live += 1
    if need_live and not items:
        return Result(SKIPPED, "canonical ведёт на другие адреса — нужна проверка целей: python run_checks.py --live", source="live")
    res = from_items(items, what="страниц")
    if need_live:
        res.summary += f" · {need_live} целей не проверено (запустите с --live)"
        if res.status == PASS:
            res.status = WARN
    return res


@check("canonical_new_url", "ведет на новый url, а не на старый", source="live")
def canonical_new_url(ctx):
    return _canon_status(ctx, "new", lambda st, hops, noidx: "" if st == 200 and hops <= 1 else
                         f"цель отвечает {st}" + (f" через {hops - 1} редирект(а)" if hops > 1 else ""))


@check("canonical_target_ok", "не ведет на 404, редирект", source="live")
def canonical_target_ok(ctx):
    def test(st, hops, noidx):
        if st != 200:
            return f"цель отвечает {st}"
        if hops > 1:
            return "цель редиректит"
        if noidx:
            return "цель с noindex"
        return ""
    return _canon_status(ctx, "target", test)


TRACK = re.compile(r"^(utm_|yclid|gclid|fbclid|_openstat|from$)", re.I)


@check("canonical_params", "страницы с параметрами ведут", source="live")
def canonical_params(ctx):
    rows = ctx.live("param_canonical")
    if not rows:
        return Result(SKIPPED, "нужны страницы с utm/sort: python run_checks.py --live", source="live")
    items = []
    for r in rows:
        m = r["meta_d"]
        c = m.get("canonical") or ""
        ok = bool(c) and not urlsplit(c).query and c == m.get("clean_canonical", c)
        items.append(Item(r["url"], ok, f"canonical={c or 'нет'}" + ("" if ok else f", ожидался {m.get('clean_canonical')}"), m.get("page_type", "")))
    return from_items(items, what="страниц с параметрами", source="live")


@check("canonical_pagination", "пагинация по утвержденной", source="live")
def canonical_pagination(ctx):
    rows = ctx.live("page2")
    if not rows:
        return Result(SKIPPED, "нужна загрузка ?page=2: python run_checks.py --live", source="live")
    items, modes = [], set()
    for r in rows:
        m = r["meta_d"]
        c, first = m.get("canonical") or "", m.get("clean_canonical") or ""
        if r["status"] != 200:
            items.append(Item(r["url"], True, f"страница 2 отвечает {r['status']} — пагинации нет", m.get("page_type", "")))
            continue
        if c == r["final_url"] or c == r["url"]:
            modes.add("самоканоникал"); ok = True
        elif c == first:
            modes.add("на первую страницу"); ok = True
        else:
            ok = False
        items.append(Item(r["url"], ok, f"canonical={c or 'нет'}", m.get("page_type", "")))
    res = from_items(items, what="листингов", source="live")
    if modes:
        res.summary += f" · логика: {', '.join(sorted(modes))}; сверьте с ТЗ"
        if len(modes) > 1 and res.status == PASS:
            res.status, res.summary = WARN, res.summary + " · логика НЕ единообразна"
    return res


@check("canonical_filters", "фильтры по утвержденной")
def canonical_filters(ctx):
    pages = [p for p in ctx.primary(roles=("filter",)) if p.canonicals]
    if not pages:
        return Result(NA, "в справочнике нет страниц-фильтров (type=filter_* или tags)")
    items = [Item(p.url, norm_path(_canon(p)) == norm_path(p.final_url), f"canonical={_canon(p)}", p.page_type) for p in pages]
    res = from_items(items, what="посадочных фильтров")
    res.summary += " · «мусорные» комбинации фильтров проверяются отдельным списком URL"
    return res
