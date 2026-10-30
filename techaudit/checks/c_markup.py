"""Микроразметка (JSON-LD + microdata), Open Graph, Twitter, viewport, charset, lang."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from .core import FAIL, NA, PASS, SKIPPED, WARN, Item, Result, as_list, check, from_items, role_of
from .c_render import _breadcrumb_names


def _ents(p, *types):
    """Сущности нужных типов: [(формат, {свойство: значение})]."""
    out = []
    for e in p.jsonld[0]:
        if set(as_list(e.get("@type"))) & set(types):
            out.append(("jsonld", e))
    for m in p.microdata:
        if set(m["types"]) & set(types):
            out.append(("microdata", m["props"]))
    return out


def _val(v):
    if isinstance(v, dict):
        return v.get("@id") or v.get("url") or v.get("name") or ""
    if isinstance(v, list):
        return _val(v[0]) if v else ""
    return "" if v is None else str(v)


def _offers(p):
    """Все Offer: из JSON-LD (вложенные в Product) и microdata."""
    offers = []
    for fmt, e in _ents(p, "Product"):
        o = e.get("offers")
        if fmt == "jsonld":
            for x in (o if isinstance(o, list) else [o] if o else []):
                if isinstance(x, dict):
                    if "lowPrice" in x and "price" not in x:
                        x = {**x, "price": x["lowPrice"]}
                    offers.append((fmt, x))
    for fmt, e in _ents(p, "Offer", "AggregateOffer"):
        if fmt == "microdata":
            offers.append((fmt, e))
    return offers


# ---------------------------------------------------------------- 51..60

@check("schema_org_website", "organization и website")
def schema_org_website(ctx):
    pages = ctx.primary(roles=("home",))
    if not pages:
        return Result(NA, "в справочнике нет главной (type=home)")
    items = []
    for p in pages:
        t = p.types_found()
        miss = [x for x in ("Organization", "WebSite") if x not in t]
        nested = [x for x in miss if f'"@type": "{x}"' in json.dumps(p.jsonld[0], ensure_ascii=False)]
        info = ", ".join(f"{k}: {'+'.join(sorted(v))}" for k, v in t.items() if k in ("Organization", "WebSite"))
        msg = ("нет: " + ", ".join(miss)) if miss else info
        if nested:
            msg += f" (на верхнем уровне нет; {', '.join(nested)} есть только вложенным, напр. publisher)"
        items.append(Item(p.url, not miss, msg, p.page_type))
    return from_items(items, what="главных страниц")


@check("schema_breadcrumb", "breadcrumblist")
def schema_breadcrumb(ctx):
    pages = [p for p in ctx.primary() if role_of(p.page_type, ctx.cs) not in ("home", "search")]
    if not pages:
        return Result(NA, "нет внутренних страниц")
    items = []
    for p in pages:
        bc = _ents(p, "BreadcrumbList")
        visible = _breadcrumb_names(p)
        if not bc:
            items.append(Item(p.url, False, "BreadcrumbList отсутствует", p.page_type))
            continue
        names: list[str] = []
        for fmt, e in bc:
            if fmt == "jsonld":
                for it in e.get("itemListElement") or []:
                    if isinstance(it, dict):
                        item = it.get("item")
                        names.append((it.get("name") or (item.get("name") if isinstance(item, dict) else "") or "").strip())
        if not names and p.microdata:
            for m in p.microdata:
                if "ListItem" in m["types"] and m["props"].get("name"):
                    names.append(m["props"]["name"].strip())
        names = [n for n in names if n]
        if not names:
            items.append(Item(p.url, False, "в BreadcrumbList нет элементов с name", p.page_type))
            continue
        vis = {re.sub(r"\W+", "", v.lower()) for v in visible}
        missing = [n for n in names if re.sub(r"\W+", "", n.lower()) not in vis]
        # допускаем, что последний элемент (текущая страница) не является ссылкой
        if missing and len(missing) == 1 and names[-1] == missing[0]:
            missing = []
        ok = not missing and abs(len(names) - len(visible)) <= 1
        items.append(Item(p.url, ok, f"разметка: {len(names)} эл., видимых крошек: {len(visible)}" +
                          (f"; нет среди видимых: {', '.join(missing[:3])}" if missing else ""), p.page_type))
    return from_items(items, what="страниц")


@check("schema_product", "product и offer")
def schema_product(ctx):
    pages = ctx.primary(roles=("product",))
    if not pages:
        return Result(NA, "в справочнике нет карточек товара")
    items = []
    for p in pages:
        prods = _ents(p, "Product")
        if not prods:
            items.append(Item(p.url, False, "Product отсутствует", p.page_type))
            continue
        offers = _offers(p)
        flat = {}
        for _, o in offers:
            for k in ("price", "priceCurrency", "availability"):
                if _val(o.get(k)):
                    flat.setdefault(k, _val(o.get(k)))
        sku = next((_val(e.get("sku") or e.get("productID") or e.get("mpn")) for _, e in prods
                    if _val(e.get("sku") or e.get("productID") or e.get("mpn"))), "")
        miss = [k for k in ("price", "priceCurrency", "availability") if k not in flat]
        if not sku:
            miss.append("sku")
        warn = ""
        pr = re.sub(r"[^\d.,]", "", flat.get("price", "")).replace(",", ".")
        if pr:
            try:
                n = float(pr)
                digits = {f"{n:g}", f"{n:.2f}", f"{n:.2f}".replace(".", ","), f"{int(n)}"}
                text = p.visible_text.replace(" ", " ")
                if not any(d in text for d in digits):
                    warn = f"; цена {flat['price']} не найдена в видимом тексте"
            except ValueError:
                miss.append("price (не число)")
        items.append(Item(p.url, not miss and not warn,
                          (("нет: " + ", ".join(miss)) if miss else
                           f"price={flat['price']} {flat['priceCurrency']}, sku={sku}") + warn, p.page_type))
    return from_items(items, what="карточек")


@check("schema_rating", "aggregaterating и review")
def schema_rating(ctx):
    pages = ctx.primary(roles=("product",))
    if not pages:
        return Result(NA, "нет карточек товара")
    items = []
    for p in pages:
        rating = _ents(p, "AggregateRating")
        visible_rating = bool(p.xp("//*[contains(@data-qa,'rating') or contains(@class,'rating')]"))
        if not rating:
            items.append(Item(p.url, not visible_rating,
                              "на странице виден рейтинг, но AggregateRating нет" if visible_rating else
                              "разметки и видимого рейтинга нет", p.page_type))
            continue
        problems = []
        text = p.visible_text
        for _, e in rating:
            rv, rc = _val(e.get("ratingValue")), _val(e.get("reviewCount") or e.get("ratingCount"))
            for label, v in (("ratingValue", rv), ("reviewCount", rc)):
                if not v:
                    problems.append(f"нет {label}")
                elif v not in text and v.replace(".", ",") not in text:
                    problems.append(f"{label}={v} не найдено в видимом тексте")
        items.append(Item(p.url, not problems, "; ".join(problems) or "значения совпадают с видимыми", p.page_type))
    return from_items(items, what="карточек")


@check("schema_itemlist", "itemlist")
def schema_itemlist(ctx):
    pages = ctx.primary(roles=("listing",))
    if not pages:
        return Result(NA, "нет листингов")
    items = [Item(p.url, bool(_ents(p, "ItemList")) or any("ListItem" in m["types"] and
                  m["el"].xpath("ancestor::*[contains(@itemtype,'ItemList')]") for m in p.microdata),
                  "ItemList есть" if _ents(p, "ItemList") else "ItemList не найден", p.page_type) for p in pages]
    res = from_items(items, what="листингов", warn_only=True)
    res.summary += " · «если использовалась ранее»: сверьте с прежним продом"
    return res


@check("schema_faq", "faqpage")
def schema_faq(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        faq = _ents(p, "FAQPage")
        block = bool(p.xp("//*[contains(@class,'faq') or contains(@data-qa,'faq')]"))
        if faq:
            qs = [_val(q.get("name")) for _, e in faq if _ for q in (e.get("mainEntity") or []) if isinstance(q, dict)]
            text = p.visible_text.lower()
            missing = [q for q in qs if q and q.lower() not in text]
            ok = bool(qs) and not missing
            items.append(Item(p.url, ok, f"вопросов в разметке: {len(qs)}, не найдено на странице: {len(missing)}", p.page_type))
        elif block:
            items.append(Item(p.url, False, "на странице есть FAQ-блок, но FAQPage нет", p.page_type))
    if not items:
        return Result(NA, "FAQ не обнаружен в проверенной выборке; применимость разметки требует подтверждения")
    return from_items(items, what="страниц с FAQ")


@check("schema_valid", "валидация без ошибок")
def schema_valid(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    REQ = {"Product": ("name",), "Organization": ("name",), "WebSite": ("url",), "BreadcrumbList": ("itemListElement",),
           "Offer": ("price",), "AggregateRating": ("ratingValue",), "ItemList": ("itemListElement",)}
    items = []
    for p in pages:
        errs = list(p.jsonld[1])
        for e in p.jsonld[0]:
            ts = as_list(e.get("@type"))
            if not ts:
                errs.append("сущность без @type")
            for t in ts:
                for f in REQ.get(t, ()):
                    if not _val(e.get(f)) and not e.get(f):
                        errs.append(f"{t}: нет {f}")
        for m in p.microdata:
            if not m["types"]:
                errs.append("itemscope без itemtype")
        items.append(Item(p.url, not errs, "; ".join(dict.fromkeys(errs))[:300] or "локальная проверка без ошибок", p.page_type))
    res = from_items(items, what="страниц")
    if res.status == PASS:
        res.status, res.summary = WARN, "локальная проверка без ошибок · Rich Results Test и валидатор Яндекса не подключены — пройдите вручную"
    return res


def _snapshot(p) -> dict:
    return {t: sorted(f) for t, f in p.types_found().items()}


@check("schema_format_kept", "сохранен текущий формат")
def schema_format_kept(ctx):
    if not ctx.baseline_run:
        return Result(SKIPPED, "нужен эталон текущего прода: python run_checks.py --baseline <run_id прошлого запуска>")
    base = {p.source_url: p for p in ctx.pages("bot", run_id=ctx.baseline_run)}
    items = []
    for p in ctx.primary():
        b = base.get(p.source_url)
        if not b:
            continue
        cur, old = _snapshot(p), _snapshot(b)
        lost = [f"{t}({'+'.join(sorted(set(f) - set(cur.get(t, []))))})" for t, f in old.items() if set(f) - set(cur.get(t, []))]
        items.append(Item(p.url, not lost, ("потеряно: " + ", ".join(lost)) if lost else "состав разметки не урезан", p.page_type))
    return from_items(items, what="страниц")


@check("schema_duplicates", "нет дублирования сущностей")
def schema_duplicates(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    KEYS = ("name", "url", "price", "sku")
    items = []
    for p in pages:
        probs, notes = [], []
        by: dict[str, list] = {}
        for e in p.jsonld[0]:
            for t in as_list(e.get("@type")):
                by.setdefault(t, []).append(("jsonld", e))
        for m in p.microdata:
            if m["types"] and not m["el"].xpath("ancestor::*[@itemscope]"):  # только верхний уровень
                for t in m["types"]:
                    by.setdefault(t, []).append(("microdata", m["props"]))
        for t, lst in by.items():
            if t in ("ListItem", "Offer", "ImageObject", "Brand", "PostalAddress", "Answer", "Question", "Person"):
                continue
            fmts = [f for f, _ in lst]
            if t in ("Product", "BreadcrumbList", "Organization", "WebSite", "FAQPage") and len(lst) > 1:
                if "jsonld" in fmts and "microdata" in fmts:
                    a = next(e for f, e in lst if f == "jsonld"); b = next(e for f, e in lst if f == "microdata")
                    conflict = [k for k in KEYS if _val(a.get(k)) and _val(b.get(k)) and _val(a.get(k)).strip() != _val(b.get(k)).strip()]
                    if conflict:
                        probs.append(f"{t}: в двух форматах, расходятся {','.join(conflict)}")
                    else:
                        notes.append(f"{t}: в двух форматах без расхождений")
                else:
                    probs.append(f"{t}: {len(lst)} экземпляра(ов) одного формата")
        items.append(Item(p.url, not probs, "; ".join(probs) or ("; ".join(notes) or "дублей нет"), p.page_type))
    return from_items(items, what="страниц")


@check("schema_urls", "внутри разметки используются новые url")
def schema_urls(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    base_host = (urlsplit(ctx.base_url).hostname or "").lower()
    bad_marker = re.compile(r"preprod|stage|staging|\.test|localhost|dev\.", re.I)
    items = []
    for p in pages:
        vals: list[tuple[str, str]] = []

        def walk(o, key=""):
            if isinstance(o, dict):
                media = bool({"ImageObject", "VideoObject"} & set(as_list(o.get("@type"))))
                for k, v in o.items():
                    walk(v, "image" if media and k in ("url", "contentUrl") else k)
            elif isinstance(o, list):
                for v in o:
                    walk(v, key)
            elif isinstance(o, str) and key in ("url", "image", "sameAs", "@id", "logo", "contentUrl", "item"):
                vals.append((key, o))
        for e in p.jsonld[0]:
            walk(e)
        for m in p.microdata:
            for k in ("url", "image", "logo", "sameAs"):
                if m["props"].get(k):
                    vals.append((k, m["props"][k]))
        probs = []
        for k, v in vals:
            if not v.startswith(("http://", "https://")):
                if v.startswith("/"):
                    probs.append(f"{k}: относительный {v[:60]}")
                continue
            u = urlsplit(v)
            if u.scheme == "http":
                probs.append(f"{k}: http {v[:60]}")
            if bad_marker.search(u.hostname or "") or any(h in (u.hostname or "") for h in ctx.cs.old_hosts):
                probs.append(f"{k}: тестовый/старый хост {u.hostname}")
            if k == "url" and (u.hostname or "").lower() not in (base_host, "www." + base_host, base_host.removeprefix("www.")):
                probs.append(f"url ведёт на чужой хост {u.hostname}")
        if not vals:
            continue
        items.append(Item(p.url, not probs, "; ".join(dict.fromkeys(probs))[:300] or f"проверено полей: {len(vals)}", p.page_type))
    res = from_items(items, what="страниц с URL-полями разметки")
    res.summary += f" · без URL-полей: {len(pages) - len(items)} из {len(pages)} страниц"
    if len(items) < len(pages) and res.status == PASS:
        res.status = WARN
    return res


# ---------------------------------------------------------------- Open Graph / Twitter / head

@check("og_title_description", "og:title и og:description")
def og_title_description(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        miss = [k for k in ("og:title", "og:description") if not (p.meta1(k) or "").strip()]
        items.append(Item(p.url, not miss, ("пусто или нет: " + ", ".join(miss)) if miss else "заполнены", p.page_type))
    return from_items(items, what="страниц")


@check("og_url", "тег og:url")
def og_url(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        ogu = (p.meta1("og:url") or "").strip()
        can = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
        if not ogu:
            items.append(Item(p.url, False, "og:url отсутствует", p.page_type))
        elif not can:
            items.append(Item(p.url, False, "нет canonical для сверки", p.page_type))
        else:
            items.append(Item(p.url, ogu == can, "совпадает с canonical" if ogu == can else f"og:url={ogu} ≠ canonical={can}", p.page_type))
    return from_items(items, what="страниц")


@check("og_image", "тег og:image")
def og_image(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    probes = {r["url"]: r for r in ctx.probes("og_image")}
    items, unchecked = [], 0
    for p in pages:
        img = (p.meta1("og:image") or "").strip()
        if not img:
            items.append(Item(p.url, False, "og:image отсутствует", p.page_type))
            continue
        if not img.startswith("https://"):
            items.append(Item(p.url, False, f"og:image не абсолютный https: {img[:80]}", p.page_type))
            continue
        pr = probes.get(img)
        if not pr:
            unchecked += 1
            items.append(Item(p.url, True, f"задан {img[-60:]} (доступность не проверялась)", p.page_type))
            continue
        m = pr["meta_d"]
        w, h = m.get("width"), m.get("height")
        bad = [] if pr["verdict"] == "pass" else [pr["reason"]]
        if w and h and (w < ctx.cs.og_min_w or h < ctx.cs.og_min_h):
            bad.append(f"размер {w}×{h} меньше {ctx.cs.og_min_w}×{ctx.cs.og_min_h}")
        items.append(Item(p.url, not bad, "; ".join(bad) or f"200, {w}×{h}" if w else "200", p.page_type))
    res = from_items(items, what="страниц")
    if unchecked and res.status == PASS:
        res.status, res.summary = WARN, f"og:image задан, но доступность не проверена у {unchecked} страниц: python collect_html.py --probes resources"
    return res


@check("og_misc", "og:type, og:site_name")
def og_misc(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        miss = [k for k in ("og:type", "og:site_name", "og:locale") if not (p.meta1(k) or "").strip()]
        loc = p.meta1("og:locale") or ""
        if loc and not re.match(r"^[a-z]{2}_[A-Z]{2}$", loc):
            miss.append(f"og:locale «{loc}» не вида ru_RU")
        items.append(Item(p.url, not miss, ("нет/неверно: " + ", ".join(miss)) if miss else "заполнены", p.page_type))
    return from_items(items, what="страниц")


@check("twitter_card", "twitter:card")
def twitter_card(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        miss = [k for k in ("twitter:card", "twitter:title", "twitter:description", "twitter:image")
                if not (p.meta1(k) or p.meta1(k + ":src") or "").strip()]
        items.append(Item(p.url, not miss, ("нет: " + ", ".join(miss)) if miss else "есть", p.page_type))
    return from_items(items, what="страниц")


@check("viewport", "точная строка meta viewport")
def viewport(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        raw = p.meta1("viewport")
        if not raw:
            items.append(Item(p.url, False, "meta viewport отсутствует", p.page_type))
            continue
        kv = {k.strip().lower(): v.strip().lower() for k, _, v in (x.partition("=") for x in raw.split(",") if x.strip())}
        miss = []
        if kv.get("width") != "device-width":
            miss.append("width=device-width")
        if kv.get("initial-scale") not in ("1", "1.0"):
            miss.append("initial-scale=1")
        if kv.get("viewport-fit") != "cover":
            miss.append("viewport-fit=cover")
        extra = [k for k in kv if k not in ("width", "initial-scale", "viewport-fit")]
        items.append(Item(p.url, not miss and not extra,
                          (("нет: " + ", ".join(miss)) if miss else "параметры верны") +
                          (f"; лишние: {', '.join(extra)}" if extra else "") + f" | «{raw}»", p.page_type))
    return from_items(items, what="страниц")


@check("charset", "charset utf-8")
def charset(ctx):
    pages = [p for v in ("bot", "plain") for p in ctx.pages(v)]
    pages = pages[: len(ctx.pages("bot")) or len(pages)]
    if not pages:
        return Result(NA, "нет сырого HTML")
    items = []
    for p in pages:
        head = p.raw[:1024].lower()
        m = re.search(rb'<meta[^>]+charset\s*=\s*["\']?\s*([\w-]+)', head)
        ct = re.search(rb'charset=([\w-]+)', p.headers.get("content-type", "").encode().lower())
        if not m:
            items.append(Item(p.url, False, "meta charset не найден в первых 1024 байтах", p.page_type))
        else:
            enc = m.group(1).decode()
            first = head.find(b"<title") < 0 or m.start() < head.find(b"<title")
            ok = enc == "utf-8" and first
            items.append(Item(p.url, ok, f"charset={enc}, смещение {m.start()} байт" + ("" if first else ", после <title>"), p.page_type))
    return from_items(items, what="страниц")


@check("lang", "атрибут lang")
def lang(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        v = (p.xp("/html/@lang") or [""])[0].strip()
        items.append(Item(p.url, v.lower() == "ru", f"lang=«{v}»" if v else "lang не задан", p.page_type))
    return from_items(items, what="страниц")
