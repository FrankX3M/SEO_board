"""Дополнительные проверки: мета-теги, заголовки, дубли, цепочки canonical, hreflang, soft 404."""
from __future__ import annotations

import copy
import hashlib
import re
import lxml.html
from collections import defaultdict
from itertools import combinations
from urllib.parse import urlsplit

from .core import FAIL, NA, PASS, SKIPPED, WARN, Item, Result, norm_path, role_of
from .extras import CAP, content_pages, crawl_import, graded, indexable, short, ws, xcheck

B_IDX, B_META = "Индексация и дубли", "Мета-теги и заголовки"

PLACEHOLDER = re.compile(
    r"(\{\{|\}\}|\[object|\bundefined\b|\bnull\b|\bnan\b|lorem ipsum|%s\b|\$\{|metadescription|metatitle|meta description|"
    r"page title|^title$|^description$|^untitled|^document$|^default$|^заголовок$|^описание$)", re.I)


def _title(p) -> str:
    els = p.xp("//head/title") or p.xp("//title")
    return ws(els[0].text_content()) if els else ""


def _desc(p) -> str:
    return ws(p.meta1("description") or "")


def _h1s(p) -> list[str]:
    return [ws(e.text_content()) for e in p.xp("//h1")]


def _self_canonical(p) -> bool:
    c = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
    return not c or norm_path(c) == norm_path(p.final_url)


def _meta_rows(ctx) -> tuple[list[dict], str]:
    """[{url,title,desc,h1}] индексируемых страниц: весь краул Screaming Frog, если выгружен, иначе выборка справочника."""
    imp = crawl_import(ctx)
    if imp:
        rows = ctx.db.execute(
            "SELECT address, title, meta_desc, h1 FROM crawl_pages WHERE import_id=? AND content_type LIKE 'text/html%' "
            "AND status=200 AND indexability='Indexable'", (imp["id"],)).fetchall()
        return ([{"url": r["address"], "title": ws(r["title"]), "desc": ws(r["meta_desc"]), "h1": ws(r["h1"])} for r in rows],
                f" · по краулу Screaming Frog ({len(rows)} URL)")
    pages = [p for p in indexable(ctx) if _self_canonical(p)]
    return ([{"url": p.url, "title": _title(p), "desc": _desc(p), "h1": (_h1s(p) or [""])[0]} for p in pages],
            " · по выборке справочника")


def _dups(ctx, field: str, label: str) -> Result:
    rows, note = _meta_rows(ctx)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r[field]:
            groups[r[field].lower()].append(r)
    total = sum(len(g) for g in groups.values())
    if not total:
        return Result(NA, f"нет страниц с заполненным полем «{label}»{note}")
    items, bad = [], 0
    for g in groups.values():
        if len(g) > 1:
            bad += len(g)
            for r in g:
                other = next(x["url"] for x in g if x is not r)
                items.append(Item(r["url"], False, f"«{short(r[field], 60)}» совпадает ещё с {len(g) - 1} стр., например {other}", "дубль"))
    ok = [Item(r["url"], True, "уникально", "") for g in groups.values() if len(g) == 1 for r in g][:8]
    stats = {"total": total, "bad": bad}
    if bad:
        return Result(FAIL, f"{bad} из {total} страниц делят {label} с другой страницей ({sum(1 for g in groups.values() if len(g) > 1)} групп){note}",
                      items[:CAP] + ok, "html", stats)
    return Result(PASS, f"{total} страниц, {label} уникальны{note}", ok, "html", stats)


@xcheck("dup_title", B_IDX, "Уникальные title на индексируемых страницах", "Нет двух индексируемых страниц с одинаковым title", "Высокий")
def dup_title(ctx): return _dups(ctx, "title", "title")


@xcheck("dup_description", B_IDX, "Уникальные description на индексируемых страницах", "Нет двух страниц с одинаковым description", "Средний")
def dup_description(ctx): return _dups(ctx, "desc", "description")


@xcheck("dup_h1", B_IDX, "Уникальные H1 на индексируемых страницах", "Нет двух страниц с одинаковым H1", "Средний")
def dup_h1(ctx): return _dups(ctx, "h1", "H1")


# ---------------------------------------------------------------- мета-теги

@xcheck("meta_placeholders", B_META, "Нет заглушек в title, description, H1 и og:title",
        "В мета-тегах нет слов-заглушек: title, metaDescription, undefined, null, {{ }}, lorem ipsum", "Критично")
def meta_placeholders(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        fields = {"title": _title(p), "description": _desc(p), "H1": " ".join(_h1s(p)),
                  "og:title": ws(p.meta1("og:title") or ""), "og:description": ws(p.meta1("og:description") or "")}
        bad = [f"{k}: «{short(v, 50)}»" for k, v in fields.items() if v and PLACEHOLDER.search(v)]
        items.append((Item(p.url, not bad, "; ".join(bad) or "заглушек нет", p.page_type), "fail" if bad else "ok"))
    return graded(items)


@xcheck("title_quality", B_META, "Title заполнен и в пределах длины", "Title не пустой; длина в пределах TITLE_MIN–TITLE_MAX знаков", "Высокий")
def title_quality(ctx):
    pages = content_pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    cs, items = ctx.cs, []
    for p in pages:
        t = _title(p)
        n = len(t)
        if not t:
            items.append((Item(p.url, False, "title отсутствует или пуст", p.page_type), "fail"))
        elif n > cs.title_max:
            items.append((Item(p.url, False, f"{n} знаков > {cs.title_max}: «{short(t, 60)}»", p.page_type), "warn"))
        elif n < cs.title_min:
            items.append((Item(p.url, False, f"{n} знаков < {cs.title_min}: «{t}»", p.page_type), "warn"))
        else:
            items.append((Item(p.url, True, f"{n} знаков", p.page_type), "ok"))
    return graded(items)


@xcheck("description_quality", B_META, "Description заполнен и в пределах длины",
        "Description не пустой, не равен title; длина в пределах DESC_MIN–DESC_MAX знаков", "Средний")
def description_quality(ctx):
    pages = content_pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    cs, items = ctx.cs, []
    for p in pages:
        d, t = _desc(p), _title(p)
        n = len(d)
        if not d:
            items.append((Item(p.url, False, "description отсутствует или пуст", p.page_type), "fail"))
        elif n > cs.desc_max:
            items.append((Item(p.url, False, f"{n} знаков > {cs.desc_max}", p.page_type), "warn"))
        elif n < cs.desc_min:
            items.append((Item(p.url, False, f"{n} знаков < {cs.desc_min}: «{d}»", p.page_type), "warn"))
        elif d.lower() == t.lower():
            items.append((Item(p.url, False, "description совпадает с title", p.page_type), "warn"))
        else:
            items.append((Item(p.url, True, f"{n} знаков", p.page_type), "ok"))
    return graded(items)


@xcheck("h1_single", B_META, "Ровно один непустой H1", "На странице один H1 с текстом; нет страниц без H1", "Высокий")
def h1_single(ctx):
    pages = [p for p in content_pages(ctx) if role_of(p.page_type, ctx.cs) != "search"]
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        h = _h1s(p)
        non_empty = [x for x in h if x]
        if not non_empty:
            items.append((Item(p.url, False, "H1 отсутствует" if not h else "H1 пустой", p.page_type), "fail"))
        elif len(non_empty) > 1:
            items.append((Item(p.url, False, f"{len(non_empty)} H1: " + " | ".join(short(x, 30) for x in non_empty[:3]), p.page_type), "warn"))
        else:
            items.append((Item(p.url, True, f"«{short(non_empty[0], 60)}»", p.page_type), "ok"))
    return graded(items)


@xcheck("headings_structure", B_META, "Структура заголовков H2–H6", "На информационных страницах есть H2; нет пропусков уровней (H1 → H3)", "Низкий")
def headings_structure(ctx):
    pages = content_pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        levels = [int(e.tag[1]) for e in p.xp("//h1|//h2|//h3|//h4|//h5|//h6")]
        probs = []
        prev = 0
        for lv in levels:
            if prev and lv > prev + 1:
                probs.append(f"пропуск уровня: H{prev} → H{lv}")
                break
            prev = lv
        role = role_of(p.page_type, ctx.cs)
        if role in ("info", "home") and len(p.visible_text) >= ctx.cs.min_text_chars and 2 not in levels:
            probs.append("нет H2 на текстовой странице")
        items.append((Item(p.url, not probs, "; ".join(probs) or f"заголовки: {len(levels)}", p.page_type), "warn" if probs else "ok"))
    return graded(items)


# ---------------------------------------------------------------- canonical: цепочки, заголовок, карта

def _key(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


@xcheck("canonical_chain", B_IDX, "Canonical без цепочек и циклов",
        "Canonical ведёт сразу на конечный URL: цель сама ссылается на себя", "Высокий")
def canonical_chain(ctx):
    imp = crawl_import(ctx)
    if imp:
        base = ("FROM crawl_pages x JOIN crawl_pages y ON y.import_id=x.import_id AND y.address=x.canonical "
                "WHERE x.import_id=? AND x.content_type LIKE 'text/html%' AND x.canonical!='' AND x.canonical!=x.address "
                "AND COALESCE(y.canonical,'')!='' AND y.canonical!=y.address")
        n = ctx.db.execute(f"SELECT COUNT(*) {base}", (imp["id"],)).fetchone()[0]
        total = ctx.db.execute("SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND content_type LIKE 'text/html%' AND canonical!='' "
                               "AND canonical!=address", (imp["id"],)).fetchone()[0]
        rows = ctx.db.execute(f"SELECT x.address, x.canonical, y.canonical AS nxt {base} LIMIT ?", (imp["id"], CAP)).fetchall()
        note = f" · по краулу ({imp['rows']} URL)"
        if not total:
            return Result(PASS, "в краулe нет страниц с canonical на другой URL — цепочек нет" + note, [], "crawl", {"total": 0, "bad": 0})
        items = [Item(r["address"], False, f"{r['address']} → {r['canonical']} → {r['nxt']}", "цепочка") for r in rows]
        if n:
            return Result(FAIL, f"{n} из {total} canonical на другой URL ведут через цепочку или цикл{note}", items, "crawl",
                          {"total": total, "bad": n})
        return Result(PASS, f"{total} canonical на другой URL, цепочек нет{note}", [], "crawl", {"total": total, "bad": 0})
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    canon = {}
    for p in pages:
        c = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
        canon[_key(p.final_url)] = c
        canon.setdefault(_key(p.url), c)
    items = []
    for p in pages:
        c = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
        if not c or _key(c) == _key(p.final_url):
            continue
        nxt = canon.get(_key(c))
        if nxt and _key(nxt) != _key(c):
            loop = _key(nxt) == _key(p.final_url)
            items.append((Item(p.url, False, f"{p.final_url} → {c} → {nxt}" + (" (цикл)" if loop else ""), p.page_type), "fail"))
        else:
            items.append((Item(p.url, True, f"canonical → {c}", p.page_type), "ok"))
    if not items:
        return Result(PASS, "в выборке нет страниц с canonical на другой URL — цепочек нет")
    return graded(items, what="страниц с canonical на другой URL")


LINK_CANON = re.compile(r"<([^>]+)>\s*;[^,]*\brel=\"?canonical\"?", re.I)


@xcheck("canonical_header_conflict", B_IDX, "Canonical в HTTP-заголовке Link согласован с HTML",
        "Если сервер отдаёт Link: <…>; rel=\"canonical\", адрес совпадает с canonical в HTML", "Высокий")
def canonical_header_conflict(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        m = LINK_CANON.search(p.headers.get("link") or "")
        if not m:
            continue
        hdr = m.group(1).strip()
        html_c = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
        ok = not html_c or _key(hdr) == _key(html_c)
        items.append((Item(p.url, ok, "совпадает" if ok else f"заголовок: {hdr} | HTML: {html_c}", p.page_type), "ok" if ok else "fail"))
    if not items:
        return Result(PASS, "canonical в заголовке Link не используется — конфликтовать нечему")
    return graded(items, what="страниц с Link: canonical")


@xcheck("sitemap_canonical_match", B_IDX, "Адреса из sitemap совпадают с canonical",
        "Страница из карты сайта ссылается canonical сама на себя, а не на другой URL", "Высокий", source="probe")
def sitemap_canonical_match(ctx):
    sm = ctx.sitemap["urls"]
    if not sm:
        return Result(SKIPPED, "карта сайта не собрана: python collect_html.py --probes discovery", source="probe")
    keys = {_key(u) for u in sm}
    items = []
    for p in ctx.primary():
        if _key(p.final_url) not in keys and _key(p.url) not in keys:
            continue
        c = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
        ok = bool(c) and _key(c) == _key(p.final_url)
        items.append((Item(p.url, ok, "canonical = адрес из карты" if ok else (f"canonical → {c}" if c else "canonical отсутствует"),
                           p.page_type), "ok" if ok else "fail"))
    if not items:
        return Result(NA, "страниц из выборки справочника в карте сайта нет", source="probe")
    return graded(items, what="страниц из карты", source="probe")


HREFLANG_CODE = re.compile(r"^(x-default|[a-z]{2,3}(-[a-z0-9]{2,8})*)$", re.I)


@xcheck("hreflang_valid", B_IDX, "Корректные hreflang", "Коды языков валидны, адреса абсолютные, есть самоссылка", "Средний")
def hreflang_valid(ctx):
    items = []
    for p in ctx.primary():
        alts = [e for e in p.xp("//link[@hreflang]") if "alternate" in (e.get("rel") or "").lower().split()]
        if not alts:
            continue
        probs = []
        codes = [(e.get("hreflang") or "").strip() for e in alts]
        hrefs = [(e.get("href") or "").strip() for e in alts]
        if any(not HREFLANG_CODE.match(c) for c in codes):
            probs.append("невалидный код: " + ", ".join(c for c in codes if not HREFLANG_CODE.match(c))[:60])
        if any(not h.startswith("http") for h in hrefs):
            probs.append("относительный URL")
        if len({c.lower() for c in codes}) != len(codes):
            probs.append("повтор языка")
        selfs = {_key(p.final_url)} | ({_key(p.canonicals[0].get("href", ""))} if p.canonicals else set())
        if not any(_key(h) in selfs for h in hrefs):
            probs.append("нет самоссылки")
        items.append((Item(p.url, not probs, "; ".join(probs) or f"{len(alts)} вариантов", p.page_type), "fail" if probs else "ok"))
    if not items:
        return Result(NA, "hreflang на страницах выборки не используется (для одноязычного сайта это норма)")
    return graded(items, what="страниц с hreflang")


SOFT404 = re.compile(r"(\b404\b|не найден|не существует|страница удалена|page not found|not found)", re.I)


@xcheck("soft_404", B_IDX, "Нет «мягких» 404 с кодом 200",
        "Страницы с кодом 200 не содержат «404», «не найдено» в title и H1", "Высокий")
def soft_404(ctx):
    pages = [p for p in ctx.primary() if not re.search(r"404|not_?found|error", p.page_type or "", re.I)]
    items = []
    for p in pages:
        hit = [f"{k}: «{short(v, 50)}»" for k, v in (("title", _title(p)), ("H1", " ".join(_h1s(p)))) if SOFT404.search(v or "")]
        items.append((Item(p.url, not hit, "; ".join(hit) or "признаков ошибки нет", p.page_type), "fail" if hit else "ok"))
    for row in ctx.probes("not_found"):
        if row["status"] != 200:
            continue
        body = ctx.probe_body(row)
        try:
            doc = lxml.html.fromstring(body)
            text = " ".join(doc.xpath("//title//text()|//h1//text()"))
            confirmed = bool(SOFT404.search(text))
        except (ValueError, TypeError, lxml.etree.ParserError):
            confirmed = False
        msg = ("несуществующий URL отвечает 200 и содержит признаки страницы ошибки" if confirmed else
               "несуществующий URL отвечает 200: кандидат в soft 404, проверьте содержимое и рендеринг")
        items.append((Item(row["url"], False, msg, "not_found"), "fail" if confirmed else "warn"))
    return graded(items)


# ---------------------------------------------------------------- дубли контента

def _main_text(p) -> str:
    d0 = p.doc
    if d0 is None:
        return ""
    d = copy.deepcopy(d0)
    for bad in d.xpath("//script|//style|//noscript|//template|//header|//footer|//nav|//aside|//form"):
        bad.drop_tree()
    return ws(d.text_content()).lower()


def _shingles(text: str, k: int = 5, limit: int = 2500) -> set[int]:
    w = re.findall(r"\w+", text)[:limit]
    return {hash(" ".join(w[i:i + k])) for i in range(max(0, len(w) - k + 1))}


@xcheck("duplicate_content", B_IDX, "Нет дублей основного текста между страницами",
        "Основной текст (без шапки, меню и футера) не повторяется на разных индексируемых страницах", "Высокий")
def duplicate_content(ctx):
    pages = [p for p in indexable(ctx) if _self_canonical(p)]
    texts = []
    for p in pages:
        t = _main_text(p)
        if len(t) >= ctx.cs.min_text_chars:
            texts.append((p, t))
    if len(texts) < 2:
        return Result(NA, f"меньше двух страниц с текстом от {ctx.cs.min_text_chars} знаков — сравнивать нечего")
    texts = texts[:150]
    sh = {id(p): _shingles(t) for p, t in texts}
    exact = defaultdict(list)
    for p, t in texts:
        exact[hashlib.sha1(t.encode()).hexdigest()].append(p)
    dup_of: dict[str, tuple[str, float]] = {}
    for g in exact.values():
        for p in g[1:]:
            dup_of[p.url] = (g[0].url, 1.0)
    thr = ctx.cs.dup_text_sim
    for (p1, _), (p2, _) in combinations(texts, 2):
        if p1.url in dup_of and p2.url in dup_of:
            continue
        a, b = sh[id(p1)], sh[id(p2)]
        if not a or not b:
            continue
        sim = len(a & b) / len(a | b)
        if sim >= thr:
            dup_of.setdefault(p2.url, (p1.url, sim))
    items = []
    for p, _ in texts:
        d = dup_of.get(p.url)
        items.append((Item(p.url, not d, f"{d[1]:.0%} совпадения с {d[0]}" if d else "уникальный текст", p.page_type), "fail" if d else "ok"))
    return graded(items, what="страниц с текстом", note=f" · порог {thr:.0%}, сравнивается основной текст")
