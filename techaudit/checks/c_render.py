"""Рендеринг, внутренние ссылки, внешние сервисы (GSC, Вебмастер, PageSpeed)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .. import integrations
from .core import FAIL, NA, PASS, SKIPPED, WARN, Item, Result, check, from_items, role_of

PRICE_RE = re.compile(r"\d[\d\s ]*[,.]?\d*\s*(₽|руб|р\.)", re.I)


def _need(ctx, variant: str, hint: str):
    return None if ctx.has_variant(variant) else Result(SKIPPED, hint)


@check("content_no_js", "контент доступен в html без выполнения js")
def content_no_js(ctx):
    pages = ctx.pages("plain") or ctx.pages("bot")
    if not pages:
        return Result(NA, "нет сырого HTML")
    items = []
    for p in pages:
        h1 = len(p.xp("//h1"))
        text = len(p.visible_text)
        links = len(p.links)
        miss = []
        if not (p.xp("//title/text()")):
            miss.append("title")
        if h1 == 0:
            miss.append("h1")
        if text < ctx.cs.min_text_chars:
            miss.append(f"текст {text} симв. < {ctx.cs.min_text_chars}")
        if links < ctx.cs.min_links:
            miss.append(f"ссылок {links} < {ctx.cs.min_links}")
        items.append(Item(p.url, not miss, ("нет: " + ", ".join(miss)) if miss else
                          f"h1: {h1}, текст {text} симв., ссылок {links}", p.page_type))
    return from_items(items, what="страниц")


@check("dynamic_rendered", "динамические блоки отрисовываются")
def dynamic_rendered(ctx):
    br = ctx.pages("browser") or ctx.pages("browser-plain")
    if not br:
        return Result(SKIPPED, "нужен вариант browser: python collect_html.py --variants plain,bot,browser")
    raw = {p.source_url: p for p in ctx.primary()}
    items = []
    for p in br:
        role = role_of(p.page_type, ctx.cs)
        if role not in ("product", "listing", "home"):
            continue
        if p.page_type.lower() in ctx.cs.nav_types:
            items.append(Item(p.url, True, "разводящая страница: товаров не ожидается (NAV_TYPES)", p.page_type))
            continue
        text = p.visible_text
        has_price = bool(PRICE_RE.search(text)) or bool(p.xp("//*[@itemprop='price'] | //meta[@itemprop='price']"))
        cards = len(p.xp("//*[contains(@data-qa,'product-card') or @itemtype='https://schema.org/Product']"))
        if role == "product":
            ok, what = has_price, "цена"
        else:
            ok, what = has_price and cards > 0, f"цены и карточки ({cards})"
        r = raw.get(p.source_url)
        in_raw = bool(r and PRICE_RE.search(r.visible_text))
        items.append(Item(p.url, ok, (f"{what}: {'есть' if ok else 'НЕТ'} в DOM" +
                                     (", в сыром HTML цен нет — блок дорисовывает JS" if ok and not in_raw else "")), p.page_type))
    res = from_items(items, what="страниц с динамикой")
    res.summary += " · отзывы и рекомендации автоматически не распознаются, проверьте их вручную"
    return res


@check("text_descriptions", "текстовые описания присутствуют")
def text_descriptions(ctx):
    pages = ctx.primary(roles=("listing", "product", "home"))
    if not pages:
        return Result(NA, "нет страниц")
    sel = ("//*[(contains(@class,'description') or contains(@data-qa,'description') or contains(@class,'seo') "
           "or contains(@data-qa,'seo') or contains(@data-qa,'about-text'))]")
    items = []
    for p in pages:
        found = []
        for el in p.xp(sel):
            txt = re.sub(r"\s+", " ", el.text_content() or "").strip()
            if len(txt) < 150:
                continue
            hidden = any(
                (a.get("hidden") is not None or "display:none" in (a.get("style") or "").replace(" ", "").lower()
                 or a.get("aria-hidden") == "true")
                for a in [el, *el.iterancestors()])
            found.append((len(txt), hidden))
        if not found:
            items.append(Item(p.url, False, "блок описания (≥150 симв.) не найден по эвристике — проверьте вручную", p.page_type))
        elif all(h for _, h in found):
            items.append(Item(p.url, False, "текст есть, но скрыт (hidden/display:none)", p.page_type))
        else:
            items.append(Item(p.url, True, f"виден блок {max(n for n, h in found if not h)} симв.", p.page_type))
    return from_items(items, what="страниц", warn_only=True)


def _external(ctx, name: str, what: str) -> Result:
    out = integrations.guarded(ctx.settings, name, lambda key: None)
    if out.status == "skipped":
        return Result(SKIPPED, out.reason, source="external")
    return Result(SKIPPED, f"ключ есть, но клиент API «{what}» ещё не подключён — проверка вручную", source="external")


@check("gsc", "проверка url в google search console", source="external")
def gsc(ctx): return _external(ctx, "gsc", "Search Console")


@check("yandex_webmaster", "проверка в яндекс вебмастере", source="external")
def ywm(ctx): return _external(ctx, "yandex_webmaster", "Яндекс Вебмастер")


@check("mobile_friendly", "мобилопригодность", source="external")
def mobile_friendly(ctx):
    r = _external(ctx, "pagespeed", "PageSpeed")
    pages = ctx.primary()
    if pages:
        no_vp = [p.url for p in pages if not p.meta1("viewport")]
        r.summary += f" · локально: meta viewport {'есть на всех страницах' if not no_vp else 'отсутствует на ' + str(len(no_vp))}"
    return r


@check("cwv", "core web vitals", source="external")
def cwv(ctx): return _external(ctx, "pagespeed", "PageSpeed")


@check("mobile_parity", "контент мобильной и десктопной версии")
def mobile_parity(ctx):
    if not ctx.mobile_run:
        return Result(SKIPPED, "нужен второй прогон: python collect_html.py --ua googlebot-mobile --variants bot, затем --mobile-run <id>")
    desk = {p.source_url: p for p in ctx.primary()}
    mob = {p.source_url: p for p in ctx.pages("bot", run_id=ctx.mobile_run)}
    if not mob:
        return Result(NA, f"в запуске {ctx.mobile_run} нет страниц сети {ctx.network}")
    items = []
    for src, m in mob.items():
        d = desk.get(src)
        if not d:
            continue
        diffs = []
        for key, a, b in (
            ("title", " ".join(d.xp("//title/text()")), " ".join(m.xp("//title/text()"))),
            ("description", d.meta1("description"), m.meta1("description")),
            ("canonical", d.canonicals[0].get("href") if d.canonicals else None,
             m.canonicals[0].get("href") if m.canonicals else None),
            ("h1", " ".join(d.xp("//h1//text()")).strip(), " ".join(m.xp("//h1//text()")).strip()),
        ):
            if (a or "") != (b or ""):
                diffs.append(key)
        ld, lm = len(set(d.links)), len(set(m.links))
        if ld and abs(ld - lm) / ld > 0.2:
            diffs.append(f"ссылки {ld}→{lm}")
        td, tm = len(d.visible_text), len(m.visible_text)
        if td and abs(td - tm) / td > 0.2:
            diffs.append(f"текст {td}→{tm}")
        items.append(Item(d.url, not diffs, ("расходится: " + ", ".join(diffs)) if diffs else "метатеги, h1, текст и ссылки совпадают", d.page_type))
    return from_items(items, what="страниц")


# ---------------------------------------------------------------- внутренние ссылки

NAV_JS = re.compile(r"location|href|window\.open|navigate|router", re.I)


@check("links_a_href", "ссылки оформлены тегом a")
def links_a_href(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        fake = [el for el in p.xp("//*[@onclick and not(self::a) and not(self::button) and not(self::input)]")
                if NAV_JS.search(el.get("onclick") or "")]
        empty_a = [a for a in p.xp("//a") if (a.get("href") or "").strip() in ("", "#") or
                   (a.get("href") or "").lower().startswith("javascript:")]
        bad = []
        if fake:
            bad.append(f"{len(fake)} элементов с onclick-переходом вместо <a>")
        if empty_a:
            bad.append(f"{len(empty_a)} тегов <a> без реального href")
        items.append(Item(p.url, not fake, ("; ".join(bad)) or "ссылки оформлены тегом a", p.page_type))
    res = from_items(items, what="страниц")
    n_empty = sum(1 for i in items if "без реального href" in i.msg)
    if res.status == PASS and n_empty:
        res.status, res.summary = WARN, f"{n_empty} страниц содержат <a> без реального href (кнопки-ссылки)"
    return res


@check("menu_footer", "главное меню и футер")
def menu_footer(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        head_links = len(p.xp("(//header|//nav|//*[contains(@data-qa,'header')])//a[@href]"))
        foot_links = len(p.xp("(//footer|//*[contains(@data-qa,'footer')])//a[@href]"))
        miss = []
        if head_links < 3:
            miss.append(f"меню/шапка ({head_links} ссылок)")
        if foot_links < 3:
            miss.append(f"футер ({foot_links} ссылок)")
        items.append(Item(p.url, not miss, ("нет в исходном HTML: " + ", ".join(miss)) if miss else
                          f"меню: {head_links} ссылок, футер: {foot_links}", p.page_type))
    return from_items(items, what="страниц")


def _breadcrumb_names(p) -> list[str]:
    els = p.xp("//*[contains(translate(@class,'BREADCUMB','breadcumb'),'breadcrumb') or contains(@data-qa,'breadcrumb') "
               "or contains(translate(@aria-label,'ХЛЕБ','хлеб'),'хлеб') or @itemtype='https://schema.org/BreadcrumbList']")
    names: list[str] = []
    for el in els:
        for a in el.xpath(".//a[@href]"):
            t = re.sub(r"\s+", " ", a.text_content() or "").strip()
            if t and t not in names:
                names.append(t)
    return names


@check("breadcrumbs", "хлебные крошки ссылками")
def breadcrumbs(ctx):
    pages = [p for p in ctx.primary() if role_of(p.page_type, ctx.cs) not in ("home", "search")]
    if not pages:
        return Result(NA, "нет внутренних страниц")
    items = []
    for p in pages:
        names = _breadcrumb_names(p)
        types = p.types_found()
        schema = "BreadcrumbList" in types
        miss = []
        if not names:
            miss.append("нет крошек ссылками <a href>")
        if not schema:
            miss.append("нет BreadcrumbList")
        items.append(Item(p.url, not miss, ("; ".join(miss)) if miss else
                          f"{len(names)} ссылок + BreadcrumbList ({'/'.join(sorted(types['BreadcrumbList']))})", p.page_type))
    return from_items(items, what="страниц")


@check("pagination_links", "пагинация реализована ссылками")
def pagination_links(ctx):
    pages = ctx.primary(roles=("listing",))
    if not pages:
        return Result(NA, "нет листингов в справочнике")
    prm = re.escape(ctx.cs.pagination_param)
    pat = re.compile(rf"([?&]{prm}=\d+)|(/{prm}/?\d+)|(/p\d+/?$)", re.I)
    items = []
    for p in pages:
        rel = p.xp("//link[@rel='next' or @rel='prev']")
        hrefs = [h for h in p.links if pat.search(h)]
        ctrl = p.xp("//*[contains(@class,'pagination') or contains(@data-qa,'pagination') or contains(@class,'pager') "
                    "or contains(@data-qa,'show-more') or contains(@data-qa,'load-more')]")
        if hrefs or rel:
            items.append(Item(p.url, True, f"ссылок пагинации: {len(set(hrefs))}, rel next/prev: {len(rel)}", p.page_type))
        elif ctrl:
            items.append(Item(p.url, False, "есть блок пагинации/«показать ещё», но без ссылок на страницы", p.page_type))
        else:
            items.append(Item(p.url, True, "пагинации на странице не найдено (если товаров больше одной страницы — проверьте)", p.page_type))
    res = from_items(items, what="листингов")
    return res


# ---------------------------------------------------------------- по живой выборке ссылок

LIVE_HINT = "нужна выборка ссылок: python run_checks.py --live"


@check("links_redirects", "перелинковка ведет на новые url", source="live")
def links_redirects(ctx):
    rows = ctx.live("link")
    if not rows:
        return Result(SKIPPED, LIVE_HINT, source="live")
    items = [Item(r["url"], not (r["status"] and 300 <= r["status"] < 400),
                  f"{r['status']} → {r['location']}" if r["status"] and 300 <= r["status"] < 400 else f"{r['status']}",
                  "link") for r in rows]
    res = from_items(items, what="ссылок (выборка)", source="live")
    res.summary += " · по выборке ссылок со страниц справочника, не по полному краулу"
    return res


@check("links_404", "нет внутренних ссылок на 404", source="live")
def links_404(ctx):
    rows = ctx.live("link")
    if not rows:
        return Result(SKIPPED, LIVE_HINT, source="live")
    items = [Item(r["url"], not (r["status"] in (404, 410) or (r["status"] or 0) >= 500 or r["status"] is None),
                  f"статус {r['status']}", "link") for r in rows]
    res = from_items(items, what="ссылок (выборка)", source="live")
    res.summary += " · по выборке, не по полному краулу"
    return res


@check("images_broken", "нет битых изображений", source="live")
def images_broken(ctx):
    rows = ctx.live("image")
    if not rows:
        return Result(SKIPPED, LIVE_HINT, source="live")
    items = [Item(r["url"], r["status"] == 200, f"статус {r['status']}", "image") for r in rows]
    res = from_items(items, what="изображений (выборка)", source="live")
    res.summary += " · по выборке, не по полному краулу"
    return res


@check("orphans", "нет страниц-сирот", source="manual")
def orphans(ctx):
    return Result(SKIPPED, "нужен краул сайта: загрузите CSV Screaming Frog на странице «Краул» (раздел веб-интерфейса) — тогда пункт считается автоматически по всему сайту", source="manual")


SESSION_RE = re.compile(r"(phpsessid|jsessionid|[?&;]sid=|[?&]session(id)?=|[?&]utm_|[?&]_openstat=|[?&]yclid=|[?&]gclid=)", re.I)


@check("session_params", "параметров сессии")
def session_params(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        bad = sorted({h for h in p.links if p.internal(h) and SESSION_RE.search(h)})
        items.append(Item(p.url, not bad, f"{len(bad)} ссылок с мусором, напр. {bad[0]}" if bad else "чисто", p.page_type))
    return from_items(items, what="страниц")


@check("alt", "атрибуты alt")
def alt_attrs(ctx):
    pages = ctx.primary(roles=("listing", "product", "home"))
    if not pages:
        return Result(NA, "нет страниц")
    items, worst = [], 0.0
    for p in pages:
        imgs = [i for i in p.xp("//img") if i.get("role") != "presentation" and i.get("aria-hidden") != "true"
                and not (i.get("src") or "").startswith("data:") and (i.get("src") or i.get("data-src"))]
        miss = [i for i in imgs if not (i.get("alt") or "").strip()]
        share = len(miss) / len(imgs) if imgs else 0
        worst = max(worst, share)
        ok = share <= ctx.cs.alt_warn
        items.append(Item(p.url, ok, f"без alt: {len(miss)} из {len(imgs)} ({share:.0%})", p.page_type))
    res = from_items(items, what="страниц")
    if res.status == FAIL and worst <= ctx.cs.alt_fail:
        res.status = WARN
    res.summary += f" · допуск {ctx.cs.alt_warn:.0%}, ошибка при >{ctx.cs.alt_fail:.0%}; по страницам справочника"
    return res
