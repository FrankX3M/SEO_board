"""Проверки по пробам и ответам сервера: редиректы, коды ответа, TTFB, sitemap, favicon.

Редиректы, 404, robots/sitemap и ресурсы берутся из таблицы probes (collect_html.py --probes …).
Нет проб — проверка получает статус «пропущено» с подсказкой, какую команду выполнить.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import datetime, timezone

import lxml.html

from .core import FAIL, NA, PASS, SKIPPED, WARN, Item, Result, check, from_items

HINT_REDIR = "тесты адресов не собирались: python collect_html.py --probes redirects"
HINT_404 = "тесты адресов не собирались: python collect_html.py --probes not_found"
HINT_DISC = "тесты адресов не собирались: python collect_html.py --probes discovery"
HINT_RES = "тесты адресов не собирались: python collect_html.py --probes resources"


def _skip(msg: str) -> Result:
    return Result(SKIPPED, msg, source="probe")


def _redirect_check(ctx, *kinds: str) -> Result:
    rows = ctx.probes(*kinds)
    if not rows:
        return _skip(HINT_REDIR)
    items = [Item(r["url"], r["verdict"] == "pass", r["reason"] or "один 301 на нужный адрес", r["kind"]) for r in rows]
    res = from_items(items, what="проб", source="probe")
    if res.status == FAIL:
        reasons = Counter(x.split(":")[0] for i in items if not i.ok for x in i.msg.split(","))
        res.summary += " (" + ", ".join(f"{k} — {v}" for k, v in reasons.most_common(4)) + ")"
    return res


@check("redirect_slash", "со слеша", source="probe")
def redirect_slash(ctx): return _redirect_check(ctx, "slash")


@check("redirect_upper", "с заглавных букв", source="probe")
def redirect_upper(ctx): return _redirect_check(ctx, "upper")


@check("redirect_www", "без www на www", source="probe")
def redirect_www(ctx): return _redirect_check(ctx, "alt_host")


@check("redirect_http", "с http на https", source="probe")
def redirect_http(ctx): return _redirect_check(ctx, "http")


@check("redirect_combined", "комбинированный кейс", source="probe")
def redirect_combined(ctx): return _redirect_check(ctx, "combined")


@check("redirect_index", "index.php и index.html", source="probe")
def redirect_index(ctx): return _redirect_check(ctx, "index_php", "index_html")


# ---------------------------------------------------------------- коды ответа

@check("status_200", "существующие страницы отдают 200")
def status_200(ctx):
    rows = [p for v in ("plain", "bot") for p in ctx.pages(v, ok_only=False)]
    items = [Item(p.url, p.status == 200, f"{p.variant}: {p.status}", p.page_type) for p in rows]
    res = from_items(items, what="страниц справочника")
    res.summary += " · по справочнику; полный краул сайта не выполняется"
    return res


@check("status_404", "удаленные страницы отдают 404", source="probe")
def status_404(ctx):
    rows = ctx.probes("not_found")
    if not rows:
        return _skip(HINT_404)
    items = [Item(r["url"], not any(x.startswith("status_") or x == "fetch_error" for x in (r["reason"] or "").split(",")),
                  f"статус {r['status']}" + (f" ({r['reason']})" if r["reason"] else ""), r["page_type"]) for r in rows]
    return from_items(items, what="несуществующих URL", source="probe")


@check("status_no5xx", "нет ответов 5xx")
def status_no5xx(ctx):
    items = [Item(p.url, not (p.status and p.status >= 500), f"{p.variant}: {p.status}", p.page_type)
             for v in ("plain", "bot", "browser") for p in ctx.pages(v, ok_only=False)]
    items += [Item(r["url"], not (r["status"] and r["status"] >= 500), f"проба {r['kind']}: {r['status']}", r["kind"])
              for r in ctx.db.execute("SELECT * FROM probes WHERE run_id=? AND network=?", (ctx.run_id, ctx.network))]
    res = from_items(items, what="ответов")
    res.summary += " · по справочнику и пробам, не по краулу"
    return res


@check("status_no404_redirect", "нет редиректа на отдельную страницу 404", source="probe")
def status_no404_redirect(ctx):
    rows = ctx.probes("not_found")
    if not rows:
        return _skip(HINT_404)
    items = [Item(r["url"], not (r["reason"] or "").startswith("redirected"),
                  "прямой 404" if not (r["reason"] or "").startswith("redirected") else
                  f"редирект {r['reason'].split(':')[-1].split(',')[0]} на {r['final_url']}", r["page_type"]) for r in rows]
    return from_items(items, what="несуществующих URL", source="probe")


@check("template_404", "корректный шаблон страницы 404", source="probe")
def template_404(ctx):
    rows = [r for r in ctx.probes("not_found") if r["status"] == 404]
    if not rows:
        return _skip("нет сохранённых 404-страниц: python collect_html.py --probes not_found")
    items = []
    for r in rows:
        body = ctx.probe_body(r)
        text = body.decode("utf-8", "replace")
        try:
            doc = lxml.html.fromstring(text)
        except Exception:  # noqa: BLE001
            items.append(Item(r["url"], False, "тело 404 не разобрано", r["page_type"]))
            continue
        links = len(doc.xpath("//a[@href]"))
        miss = []
        if not (doc.xpath("//header") or doc.xpath("//nav") or doc.xpath("//*[contains(@data-qa,'header')]")):
            miss.append("шапка/меню")
        if not (doc.xpath("//input[@type='search']") or doc.xpath("//*[@role='search']")
                or doc.xpath("//input[contains(@name,'text') or contains(@name,'q') or contains(@placeholder,'оиск')]")):
            miss.append("поиск")
        if links < 10:
            miss.append(f"ссылки на разделы (найдено {links})")
        if not re.search(r"mc\.yandex|metrika|googletagmanager|gtag\(|google-analytics|top-fwz|counter", text, re.I):
            miss.append("счётчики аналитики")
        items.append(Item(r["url"], not miss, ("нет: " + ", ".join(miss)) if miss else "шапка, поиск, ссылки, счётчики есть",
                          r["page_type"]))
    return from_items(items, what="404-страниц", source="probe")


@check("ttfb", "время ответа сервера")
def ttfb(ctx):
    by: dict[str, list[float]] = {}
    for v in ("plain", "bot"):
        for p in ctx.pages(v):
            if p.ttfb_ms:
                by.setdefault(p.page_type, []).append(p.ttfb_ms)
    if not by:
        return Result(NA, "нет замеров TTFB")
    limit = ctx.cs.ttfb_max_ms
    items = []
    for t, vals in sorted(by.items()):
        med = statistics.median(vals)
        items.append(Item(t, med <= limit, f"медиана {med:.0f} мс (макс. {max(vals):.0f}, замеров {len(vals)})", t))
    res = from_items(items, what="типов страниц")
    res.summary += f" · норма ≤ {limit:.0f} мс; замер с сервера запуска, одиночные запросы"
    return res


# ---------------------------------------------------------------- sitemap

@check("sitemap_available", "карта доступна и отдает 200", source="probe")
def sitemap_available(ctx):
    robots = ctx.probes("robots")
    files = ctx.probes("sitemap")
    if not robots and not files:
        return _skip(HINT_DISC)
    items = [Item(r["url"], r["verdict"] == "pass", r["reason"] or "robots.txt содержит Sitemap", "robots") for r in robots]
    items += [Item(r["url"], r["status"] == 200 and "fetch_error" not in (r["reason"] or ""),
                   f"статус {r['status']}", "sitemap") for r in files]
    return from_items(items, what="файлов", source="probe")


@check("sitemap_valid", "валидный xml и соблюдены лимиты", source="probe")
def sitemap_valid(ctx):
    files = ctx.probes("sitemap")
    if not files:
        return _skip(HINT_DISC)
    bad = {"invalid_xml", "not_a_sitemap", "too_large", "too_many_urls"}
    items = []
    for r in files:
        m = r["meta_d"]
        reasons = [x for x in (r["reason"] or "").split(",") if x in bad]
        info = f"{m.get('urls', m.get('children', '?'))} URL, {(m.get('bytes') or 0) / 1024:.0f} КБ"
        items.append(Item(r["url"], not reasons, ", ".join(reasons) if reasons else info, "sitemap"))
    return from_items(items, what="файлов", source="probe")



@check("sitemap_canonical_200", "только канонические страницы", source="live")
def sitemap_canonical_200(ctx):
    urls = ctx.sitemap["urls"]
    if not urls:
        return _skip(HINT_DISC)
    rows = ctx.live("sitemap_url")
    if not rows:
        return _skip("нужна выборка URL из карты: python run_checks.py --live")
    items = []
    for r in rows:
        m = r["meta_d"]
        bad = []
        if r["status"] != 200:
            bad.append(f"статус {r['status']}")
        if r["hops"] and r["hops"] > 1:
            bad.append("редирект")
        if m.get("noindex"):
            bad.append("noindex")
        if m.get("canonical") and m["canonical"] != r["url"]:
            bad.append("canonical ведёт на другой URL")
        items.append(Item(r["url"], not bad, ", ".join(bad) or "200, canonical = URL, индексируется", "sitemap"))
    res = from_items(items, what="URL из карты (выборка)", source="live")
    return res


@check("sitemap_complete", "все важные url присутствуют", source="probe")
def sitemap_complete(ctx):
    urls = ctx.sitemap["urls"]
    if not urls:
        return _skip(HINT_DISC)
    from .core import page_noindex
    loc = set(urls)
    loc_norm = {u.lower() for u in loc}
    items = []
    for p in ctx.primary():
        if page_noindex(p, ctx.cs):
            continue
        can = p.canonicals[0].get("href") if len(p.canonicals) == 1 else p.final_url
        ok = can in loc
        msg = "есть в карте" if ok else ("есть, но в другом регистре" if can.lower() in loc_norm else "нет в карте")
        items.append(Item(can, ok, msg, p.page_type))
    res = from_items(items, what="важных URL справочника", source="probe")
    res.summary += " · сверка со справочником, не с полным краулом"
    return res


@check("sitemap_cache", "закешированные и статичные карты", source="probe")
def sitemap_cache(ctx):
    """Сам сброс кеша снаружи не виден, поэтому оцениваем свежесть: самый свежий lastmod в картах и заголовок Last-Modified файлов."""
    import os
    from datetime import date, datetime
    files = ctx.probes("sitemap")
    if not files:
        return _skip(HINT_DISC)
    limit = int(os.environ.get("SITEMAP_MAX_AGE_DAYS") or 14)
    today = date.today()
    items, dates = [], []
    lm = [v for v in ctx.sitemap["urls"].values() if v]
    if lm:
        try:
            d = datetime.fromisoformat(max(lm)[:10]).date()
            dates.append(d)
            age = (today - d).days
            items.append(Item("lastmod в картах", age <= limit, f"самый свежий lastmod {d.isoformat()} — {age} дн. назад (норма ≤ {limit})", "sitemap"))
        except ValueError:
            items.append(Item("lastmod в картах", False, f"нечитаемая дата: {max(lm)[:25]}", "sitemap"))
    else:
        items.append(Item("lastmod в картах", False, "в картах нет lastmod — свежесть оценить нельзя", "sitemap"))
    for f in files:
        h = {str(k).lower(): v for k, v in (_json_h(f) or {}).items()}
        if h.get("last-modified"):
            try:
                from email.utils import parsedate_to_datetime
                d = parsedate_to_datetime(h["last-modified"]).date()
                age = (today - d).days
                items.append(Item(f["url"], age <= limit, f"Last-Modified {d.isoformat()} — {age} дн. назад (норма ≤ {limit})", "sitemap"))
            except (TypeError, ValueError):
                pass
    res = from_items(items, what="признаков свежести карт", source="probe")
    res.summary += f" · эвристика: пересборку кеша напрямую не увидеть, порог SITEMAP_MAX_AGE_DAYS={limit}"
    return res


def _json_h(f):
    import json
    h = f["headers"] if "headers" in f.keys() else None
    try:
        return json.loads(h) if isinstance(h, str) else (h or {})
    except ValueError:
        return {}


@check("sitemap_lastmod", "корректный параметр lastmod", source="probe")
def sitemap_lastmod(ctx):
    urls = ctx.sitemap["urls"]
    if not urls:
        return _skip(HINT_DISC)
    vals = [v for v in urls.values() if v]
    total = len(urls)
    now = datetime.now(timezone.utc)
    bad_fmt = future = 0
    for v in vals:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            future += d > now
        except ValueError:
            bad_fmt += 1
    uniq = len(set(vals))
    items = [
        Item("lastmod", len(vals) == total, f"указан у {len(vals)} из {total} URL"),
        Item("формат", bad_fmt == 0, f"неверный формат: {bad_fmt}"),
        Item("будущее", future == 0, f"даты в будущем: {future}"),
        Item("разнообразие", uniq > 1 or total <= 1, f"уникальных дат: {uniq} (одна дата у всех — признак «обновлено при сборке»)"),
    ]
    res = from_items(items, what="критериев", source="probe")
    res.summary += " · обновляется ли дата при изменении страницы — проверить вручную"
    if res.status == PASS:
        res.status = WARN
    return res


# ---------------------------------------------------------------- favicon

@check("favicon", "favicon.ico отдает 200", source="probe")
def favicon(ctx):
    rows = ctx.probes("favicon", "apple_icon", "manifest", "manifest_icon")
    if not rows:
        return _skip(HINT_RES)
    items = [Item(r["url"], r["verdict"] == "pass", r["reason"] or f"{r['status']} {r['content_type'] or ''}", r["kind"])
             for r in rows]
    kinds = {r["kind"] for r in rows}
    res = from_items(items, what="ресурсов", source="probe")
    miss = [k for k in ("favicon", "apple_icon") if k not in kinds]
    if miss and res.status == PASS:
        res.status, res.summary = WARN, "не объявлено в <head>: " + ", ".join(miss)
    return res


@check("sitemap_submitted", "новые карты добавлены", source="external")
def sitemap_submitted(ctx):
    from .c_render import _external
    a, b = _external(ctx, "gsc", "Search Console"), _external(ctx, "yandex_webmaster", "Яндекс Вебмастер")
    return Result(SKIPPED, f"GSC: {a.summary}; Вебмастер: {b.summary}", source="external")
