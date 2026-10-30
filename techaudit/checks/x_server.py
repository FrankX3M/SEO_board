"""Дополнительные проверки: заголовки ответа (сжатие, кеш, Vary, 304, безопасность), TLS, паритет мета для бота, вес и разбор HTML."""
from __future__ import annotations

import re
import os

from .core import FAIL, NA, PASS, SKIPPED, WARN, Item, Result, norm_path
from .extras import CAP, graded, short, ws, xcheck
from .x_meta import _desc, _h1s, _title

B_SRV, B_PERF = "Сервер и заголовки", "Производительность HTML"
HINT_LIVE = "нужна живая выборка: python run_checks.py --live"

CACHE_KNOWN = {"public", "private", "no-cache", "no-store", "no-transform", "must-revalidate", "proxy-revalidate", "max-age", "s-maxage",
               "immutable", "stale-while-revalidate", "stale-if-error", "must-understand", "only-if-cached", "max-stale", "min-fresh"}
CACHE_VALUED = {"max-age", "s-maxage", "stale-while-revalidate", "stale-if-error"}


def _pages(ctx):
    return ctx.primary()


@xcheck("compression", B_SRV, "HTML отдаётся со сжатием gzip или brotli", "Заголовок Content-Encoding: gzip, br или zstd у HTML-страниц", "Высокий")
def compression(ctx):
    pages = _pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        enc = (p.headers.get("content-encoding") or "").lower()
        size = len(p.raw)
        if enc and enc != "identity":
            items.append((Item(p.url, True, f"{enc}, {size // 1024} КБ после распаковки", p.page_type), "ok"))
        elif size < 1500:
            items.append((Item(p.url, True, f"без сжатия, но страница мала ({size} байт)", p.page_type), "ok"))
        else:
            items.append((Item(p.url, False, f"без сжатия, {size // 1024} КБ", p.page_type), "fail"))
    return graded(items)


def parse_cache_control(v: str) -> list[str]:
    """Список замечаний к значению Cache-Control (пусто — корректно)."""
    probs, seen = [], set()
    for tok in (t.strip() for t in v.split(",") if t.strip()):
        name, _, val = tok.partition("=")
        name = name.strip().lower()
        if " " in name:
            probs.append(f"директивы слиты в одну: «{short(tok, 40)}»")
            continue
        if name not in CACHE_KNOWN:
            probs.append(f"неизвестная директива «{short(tok, 30)}»")
            continue
        if name in seen:
            probs.append(f"повтор «{name}»")
        seen.add(name)
        if name in CACHE_VALUED and not re.fullmatch(r"\"?\d+\"?", val.strip()):
            probs.append(f"{name} без числа" if not val.strip() else f"{name}: нечисловое значение «{val.strip()[:15]}»")
    if "no-store" in seen and ({"max-age", "s-maxage"} & seen):
        probs.append("no-store вместе с max-age")
    if {"public", "private"} <= seen:
        probs.append("public и private одновременно")
    return probs


@xcheck("cache_headers", B_SRV, "Заголовок Cache-Control корректен", "Cache-Control без синтаксических ошибок и противоречий (public/private, no-store/max-age)", "Средний")
def cache_headers(ctx):
    pages = _pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        v = p.headers.get("cache-control")
        if not v:
            items.append((Item(p.url, False, "Cache-Control отсутствует", p.page_type), "warn"))
            continue
        probs = parse_cache_control(v)
        items.append((Item(p.url, not probs, "; ".join(probs) + f" | {short(v, 70)}" if probs else v, p.page_type), "fail" if probs else "ok"))
    return graded(items)


FIELDS = (("title", _title, "warn"), ("description", _desc, "warn"), ("H1", lambda p: " | ".join(_h1s(p)), "warn"),
          ("canonical", lambda p: p.canonicals[0].get("href", "").strip() if p.canonicals else "", "fail"),
          ("meta robots", lambda p: " ".join(p.meta.get("robots", [])).lower(), "fail"))


def _diff_fields(a, b) -> list[tuple[str, str, str]]:
    out = []
    for name, fn, sev in FIELDS:
        va, vb = fn(a), fn(b)
        if name == "canonical":
            va, vb = norm_path(va) if va else "", norm_path(vb) if vb else ""
        if va != vb:
            out.append((name, sev, f"{name}: «{short(va, 35)}» ≠ «{short(vb, 35)}»"))
    return out


def _variant_pairs(ctx, v1: str, v2: str):
    b = {p.source_url: p for p in ctx.pages(v2)}
    return [(p, b[p.source_url]) for p in ctx.pages(v1) if p.source_url in b]


@xcheck("bot_head_parity", B_SRV, "Мета-теги одинаковы для бота и обычного пользователя",
        "title, description, H1, canonical и meta robots совпадают в вариантах plain, bot и после рендеринга (browser)", "Высокий")
def bot_head_parity(ctx):
    items = []
    dynamic = os.environ.get("BOT_RENDER_ONLY", "1" if getattr(ctx, "network", "") == "5ka" else "0") == "1"
    pairs = (("browser-plain", "bot"), ("bot", "browser")) if dynamic else (("plain", "bot"), ("bot", "browser"))
    for v1, v2 in pairs:
        for a, b in _variant_pairs(ctx, v1, v2):
            d = _diff_fields(a, b)
            if not d:
                items.append((Item(a.url, True, f"{v1} = {v2}", a.page_type), "ok"))
            else:
                sev = "fail" if any(x[1] == "fail" for x in d) else "warn"
                items.append((Item(a.url, False, f"{v1} vs {v2}: " + "; ".join(x[2] for x in d), a.page_type), sev))
    if not items:
        return Result(NA, "нет сопоставимых пар; при рендеринге только для ботов нужны bot/browser или bot/browser-plain")
    return graded(items, what="пар страниц", note=" · рендеринг только для ботов: различия plain/bot ожидаемы; сравниваем отрисованные версии" if dynamic else "")


@xcheck("vary_user_agent", B_SRV, "Vary: User-Agent там, где ответ зависит от UA",
        "Если для бота и пользователя отдаётся разный HTML, ответ содержит Vary: User-Agent (иначе CDN отдаст чужую версию)", "Средний")
def vary_user_agent(ctx):
    items = []
    for a, b in _variant_pairs(ctx, "plain", "bot"):
        d = _diff_fields(a, b)
        la, lb = len(a.visible_text), len(b.visible_text)
        differs = bool(d) or (max(la, lb) > 0 and abs(la - lb) / max(la, lb) > 0.2)
        if not differs:
            items.append((Item(a.url, True, "HTML одинаков — Vary не требуется", a.page_type), "ok"))
            continue
        vary = lambda p: (p.headers.get("vary") or "").lower()  # noqa: E731
        ok = all("user-agent" in vary(p) or vary(p).strip() == "*" for p in (a, b))
        items.append((Item(a.url, ok, "ответ зависит от UA, Vary: " + (short(vary(a) or "нет", 40) if not ok else "User-Agent есть"), a.page_type),
                      "ok" if ok else "warn"))
    if not items:
        return Result(NA, "нет пар plain и bot: python collect_html.py --variants plain,bot")
    return graded(items, what="пар страниц")


@xcheck("security_headers", B_SRV, "Заголовки безопасности",
        "HSTS (max-age от HSTS_MIN_AGE), X-Content-Type-Options: nosniff, Referrer-Policy, защита от фреймов (X-Frame-Options или frame-ancestors)", "Низкий")
def security_headers(ctx):
    pages = _pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    min_age = ctx.cs.hsts_min_age

    def hsts(p):
        m = re.search(r"max-age=(\d+)", p.headers.get("strict-transport-security") or "", re.I)
        return bool(m) and int(m.group(1)) >= min_age

    reqs = (
        (f"Strict-Transport-Security (max-age ≥ {min_age})", hsts),
        ("X-Content-Type-Options: nosniff", lambda p: "nosniff" in (p.headers.get("x-content-type-options") or "").lower()),
        ("Referrer-Policy", lambda p: bool(p.headers.get("referrer-policy"))),
        ("защита от фреймов", lambda p: bool(p.headers.get("x-frame-options")) or "frame-ancestors" in (p.headers.get("content-security-policy") or "").lower()),
    )
    items = []
    for name, fn in reqs:
        bad = [p for p in pages if not fn(p)]
        items.append((Item(bad[0].url if bad else pages[0].url, not bad,
                           f"{name}: нет у {len(bad)} из {len(pages)} страниц" if bad else f"{name}: есть у всех", "заголовок"), "warn" if bad else "ok"))
    return graded(items, what="заголовков")


@xcheck("conditional_304", B_SRV, "ETag или Last-Modified и ответ 304",
        "HTML отдаёт валидатор кеша, а условный запрос (If-None-Match, If-Modified-Since) получает 304 — так Яндекс экономит бюджет обхода", "Низкий", source="live")
def conditional_304(ctx):
    items = []
    for p in _pages(ctx):
        has = bool(p.headers.get("etag") or p.headers.get("last-modified"))
        items.append((Item(p.url, has, "ETag/Last-Modified есть" if has else "нет ни ETag, ни Last-Modified", p.page_type), "ok" if has else "warn"))
    live = ctx.live("conditional")
    for r in live:
        m = r["meta_d"]
        ok = r["status"] == 304
        items.append((Item(r["url"], ok, f"условный запрос ({m.get('validator')}) → {r['status']}", "304"), "ok" if ok else "warn"))
    if not items:
        return Result(NA, "нет страниц")
    res = graded(items, what="проверок")
    if not live:
        res.summary += " · ответ 304 не проверялся (--live)"
    return res


@xcheck("tls_cert", B_SRV, "Действующий TLS-сертификат на основном и втором хосте",
        "Сертификат доверенный, не самоподписанный, соответствует имени хоста и действует ещё не менее CERT_MIN_DAYS дней (хост с www и без)", "Критично", source="live")
def tls_cert(ctx):
    rows = ctx.live("tls")
    if not rows:
        if ctx.has_live():
            return Result(NA, "у сети нет доменного имени (адрес по IP) — сертификат не проверялся", source="live")
        return Result(SKIPPED, HINT_LIVE, source="live")
    items = []
    for r in rows:
        m = r["meta_d"]
        host = m.get("host") or r["url"]
        if m.get("ok"):
            days = m.get("days_left")
            if days is not None and days < 7:
                sev, msg = "fail", f"сертификат истекает через {days} дн."
            elif days is not None and days < ctx.cs.cert_min_days:
                sev, msg = "warn", f"сертификат истекает через {days} дн."
            else:
                sev, msg = "ok", f"действителен ещё {days} дн., выдан: {short(m.get('issuer') or '', 40)}"
        elif m.get("error_kind") == "connect":
            sev, msg = "ok", f"хост не отвечает на 443 ({short(m.get('error') or '', 60)}) — если он не используется, это норма"
        else:
            sev, msg = "fail", short(m.get("error") or "сертификат не прошёл проверку", 120) + (f"; CN={m.get('subject')}" if m.get("subject") else "")
        items.append((Item(host, sev == "ok", msg, "tls"), sev))
    return graded(items, what="хостов", source="live")


# ---------------------------------------------------------------- вес и разбор HTML

@xcheck("html_weight", B_PERF, "Вес HTML и размер DOM в норме",
        "HTML до HTML_WARN_KB КБ, DOM до DOM_WARN элементов, встроенные скрипты и JSON не раздувают страницу", "Средний")
def html_weight(ctx):
    pages = _pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    cs, items = ctx.cs, []
    for p in pages:
        kb = len(p.raw) / 1024
        dom = len(p.xp("//*"))
        inline = sum(len(s.text or "") for s in p.xp("//script[not(@src)]")) / 1024
        probs, sev = [], "ok"
        if kb > cs.html_fail_kb:
            probs.append(f"HTML {kb:.0f} КБ > {cs.html_fail_kb}"); sev = "fail"
        elif kb > cs.html_warn_kb:
            probs.append(f"HTML {kb:.0f} КБ > {cs.html_warn_kb}"); sev = "warn"
        if dom > cs.dom_fail:
            probs.append(f"DOM {dom} эл. > {cs.dom_fail}"); sev = "fail"
        elif dom > cs.dom_warn:
            probs.append(f"DOM {dom} эл. > {cs.dom_warn}"); sev = "warn" if sev == "ok" else sev
        if inline > max(300, cs.html_warn_kb * 0.6):
            probs.append(f"встроенные скрипты {inline:.0f} КБ"); sev = "warn" if sev == "ok" else sev
        items.append((Item(p.url, sev == "ok", "; ".join(probs) or f"HTML {kb:.0f} КБ, DOM {dom}", p.page_type), sev))
    return graded(items)


@xcheck("render_blocking", B_PERF, "Нет лишних блокирующих скриптов в head", "Скрипты в head с async, defer или type=module; блокирующих не больше BLOCKING_SCRIPTS_MAX", "Средний")
def render_blocking(ctx):
    pages = _pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    mx, items = ctx.cs.blocking_scripts_max, []
    for p in pages:
        blocking = [s for s in p.xp("//head//script[@src]")
                    if s.get("async") is None and s.get("defer") is None and (s.get("type") or "").lower() != "module"]
        css = [l for l in p.xp("//head//link[@rel='stylesheet']") if (l.get("media") or "all").lower() not in ("print",)]
        ok = len(blocking) <= mx
        msg = f"блокирующих скриптов: {len(blocking)}" + (f" (напр. {short(blocking[0].get('src') or '', 60)})" if blocking else "") + f"; стилей: {len(css)}"
        items.append((Item(p.url, ok, msg, p.page_type), "ok" if ok else "warn"))
    return graded(items)


@xcheck("images_perf", B_PERF, "Изображения: размеры, lazy-loading, современные форматы",
        "У <img> заданы width и height, неглавные картинки с loading=lazy, преобладают webp/avif/svg", "Средний")
def images_perf(ctx):
    pages = _pages(ctx)
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        imgs = [i for i in p.xp("//img") if not (i.get("src") or "").startswith("data:")]
        if not imgs:
            continue
        n = len(imgs)
        no_dim = sum(1 for i in imgs if not (i.get("width") and i.get("height")))
        legacy = sum(1 for i in imgs if re.search(r"\.(jpe?g|png|gif)(\?|$)", (i.get("src") or i.get("data-src") or ""), re.I))
        rest = imgs[3:]
        no_lazy = sum(1 for i in rest if (i.get("loading") or "").lower() != "lazy" and not i.get("data-src"))
        probs = []
        if no_dim / n > 0.3:
            probs.append(f"без width/height: {no_dim} из {n}")
        if legacy / n > 0.5:
            probs.append(f"jpg/png/gif вместо webp/avif: {legacy} из {n}")
        if rest and no_lazy / len(rest) > 0.5:
            probs.append(f"без loading=lazy: {no_lazy} из {len(rest)} ниже первых трёх")
        items.append((Item(p.url, not probs, "; ".join(probs) or f"{n} изображений в порядке", p.page_type), "warn" if probs else "ok"))
    return graded(items, what="страниц с изображениями")
