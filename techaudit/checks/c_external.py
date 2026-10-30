"""Проверки через внешние сервисы: Google Search Console, PageSpeed (мобилопригодность, Core Web Vitals), карты в GSC.

Работают только при включённых внешних запросах (python run_checks.py --external или EXTERNAL_CHECKS=1), потому что
тратят квоту. Нет ключа — «Пропущено» с причиной; ошибка сервиса не роняет прогон. Ключи берутся списком и по кругу
(см. techaudit/keys.py), результаты кешируются в БД (EXT_CACHE_HOURS). Эти функции заменяют заглушки из c_render.
"""
from __future__ import annotations

import os

from .. import external as ext
from ..keys import describe
from .core import NA, SKIPPED, Item, Result, check, expects_noindex, from_items, page_noindex, role_of

_POOLS: dict[str, object] = {}  # один пул на процесс: исчерпанный ключ остаётся исчерпанным для всех сетей


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _off() -> Result | None:
    if not ext.enabled():
        return Result(SKIPPED, "внешние запросы выключены (они тратят квоту): запустите с --external или отметьте "
                               "«Внешние сервисы» в форме запуска", source="external")
    return None


def _psi() -> ext.PageSpeed:
    if "psi" not in _POOLS:
        _POOLS["psi"] = ext.PageSpeed()
    return _POOLS["psi"]  # type: ignore[return-value]


def _gsc(ctx) -> ext.Gsc:
    key = f"gsc:{ctx.network}"
    if key not in _POOLS:
        _POOLS[key] = ext.Gsc(ctx.network, ctx.base_url)
    return _POOLS[key]  # type: ignore[return-value]


def reset() -> None:
    """Для тестов: забыть пулы (исчерпанные ключи)."""
    _POOLS.clear()


def _sample(ctx, per_key, limit: int) -> list:
    """По одной странице на ключ (тип/роль), не больше limit, в порядке справочника."""
    seen, out = set(), []
    for p in ctx.primary():
        if page_noindex(p, ctx.cs):
            continue
        k = per_key(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _run_items(ctx, pages, fn, what: str):
    """Выполняет fn(page) → (ok, msg); ExternalError превращает в «не удалось проверить». Возвращает (items, errors, skipped)."""
    items, errors = [], []
    for p in pages:
        try:
            ok, msg = fn(p)
            items.append(Item(p.url, ok, msg, p.page_type))
        except ext.Skipped as e:
            return items, errors, str(e)
        except ext.ExternalError as e:
            errors.append(str(e))
            items.append(Item(p.url, False, f"не удалось проверить: {e}", p.page_type))
            if "исчерпаны" in str(e) or "нет доступного ресурса" in str(e):
                break  # дальше нет смысла: квота кончилась
    return items, errors, None


# ---------------------------------------------------------------- Search Console

@check("gsc", "проверка url в google search console", source="external")
def gsc(ctx):
    off = _off()
    if off:
        return off
    g = _gsc(ctx)
    pages = _sample(ctx, lambda p: p.page_type, _env_int("GSC_MAX_URLS", 10))
    if not pages:
        return Result(NA, "нет собранных страниц: сначала python collect_html.py", source="external")

    def one(p):
        data = ext.cache_get(ctx.db, "gsc", p.url)
        cached = data is not None
        if data is None:
            data = g.inspect(p.url)
            ext.cache_put(ctx.db, "gsc", p.url, data)
        can_ok = not data.get("user_canonical") or not data.get("google_canonical") or \
            data["user_canonical"].rstrip("/") == data["google_canonical"].rstrip("/")
        ok = data.get("verdict") == "PASS" and can_ok
        msg = (f"{data.get('verdict')} · {data.get('coverage') or '—'} · robots: {data.get('robots') or '—'} · "
               f"canonical Google: {data.get('google_canonical') or '—'}"
               + ("" if can_ok else f" ≠ заявленный {data.get('user_canonical')}")
               + f" · ресурс {data.get('property')}, аккаунт #{data.get('account_no')}" + (" · из кеша" if cached else ""))
        return ok, msg

    items, errors, skipped = _run_items(ctx, pages, one, "GSC")
    if skipped and not items:
        return Result(SKIPPED, skipped, source="external")
    good = [i for i in items if not i.msg.startswith("не удалось")]
    if not good:
        return Result(SKIPPED, f"GSC не ответил: {errors[0] if errors else 'нет данных'}", items, "external")
    res = from_items(good + [i for i in items if i.msg.startswith("не удалось")], what="URL (выборка по типам страниц)",
                     source="external")
    res.summary += " · " + g.summary()
    return res


# ---------------------------------------------------------------- PageSpeed

def _psi_pages(ctx):
    return _sample(ctx, lambda p: role_of(p.page_type, ctx.cs), _env_int("PSI_MAX_URLS", 3))


def _psi_result(ctx, strategy: str, url: str) -> tuple[dict, bool]:
    ident = f"{url}|{strategy}"
    data = ext.cache_get(ctx.db, "psi", ident)
    if data is not None:
        return data, True
    data = _psi().run(url, strategy)
    ext.cache_put(ctx.db, "psi", ident, data)
    return data, False


@check("mobile_friendly", "мобилопригодность", source="external")
def mobile_friendly(ctx):
    off = _off()
    if off:
        return off
    pages = _psi_pages(ctx)
    if not pages:
        return Result(NA, "нет собранных страниц: сначала python collect_html.py", source="external")

    def one(p):
        d, cached = _psi_result(ctx, "mobile", p.url)
        checks = {"viewport": d.get("viewport"), "размер шрифта": d.get("font_size"), "размер кнопок/ссылок": d.get("tap_targets")}
        bad = [k for k, v in checks.items() if v is not None and v < 1]
        local = bool(p.meta1("viewport"))
        if not local:
            bad.append("нет meta viewport в HTML")
        return (not bad, ("не пройдено: " + ", ".join(bad)) if bad else "viewport, шрифт и кнопки ок" +
                f" · Lighthouse mobile: производительность {round((d.get('perf') or 0) * 100)}, SEO {round((d.get('seo') or 0) * 100)}"
                f" · ключ #{d.get('key_no')}" + (" · из кеша" if cached else ""))

    items, errors, skipped = _run_items(ctx, pages, one, "PageSpeed")
    if skipped and not items:
        return Result(SKIPPED, skipped, source="external")
    if not [i for i in items if not i.msg.startswith("не удалось")]:
        return Result(SKIPPED, f"PageSpeed не ответил: {errors[0] if errors else 'нет данных'}", items, "external")
    res = from_items(items, what="страниц (выборка по ролям)", source="external")
    res.summary += " · " + _psi().pool.summary("ключи PageSpeed")
    return res


@check("cwv", "core web vitals", source="external")
def cwv(ctx):
    off = _off()
    if off:
        return off
    pages = _psi_pages(ctx)
    if not pages:
        return Result(NA, "нет собранных страниц: сначала python collect_html.py", source="external")
    lcp_max, cls_max, tbt_max = _env_float("CWV_LCP_MS", 2500), _env_float("CWV_CLS", 0.1), _env_float("CWV_TBT_MS", 200)

    def one(p):
        d, cached = _psi_result(ctx, "mobile", p.url)
        bad = []
        if d.get("lcp_ms") is not None and d["lcp_ms"] > lcp_max:
            bad.append(f"LCP {d['lcp_ms']:.0f} мс > {lcp_max:.0f}")
        if d.get("cls") is not None and d["cls"] > cls_max:
            bad.append(f"CLS {d['cls']:.3f} > {cls_max}")
        if d.get("tbt_ms") is not None and d["tbt_ms"] > tbt_max:
            bad.append(f"TBT {d['tbt_ms']:.0f} мс > {tbt_max:.0f}")
        field = (d.get("field") or {}).get("overall")
        if field == "SLOW":
            bad.append("поле (CrUX): SLOW")
        lab = f"LCP {d.get('lcp_ms')} мс, CLS {d.get('cls')}, TBT {d.get('tbt_ms')} мс"
        return (not bad, (", ".join(bad) if bad else lab) + (f" · поле (CrUX): {field}" if field else " · данных CrUX нет")
                + f" · ключ #{d.get('key_no')}" + (" · из кеша" if cached else ""))

    items, errors, skipped = _run_items(ctx, pages, one, "PageSpeed")
    if skipped and not items:
        return Result(SKIPPED, skipped, source="external")
    if not [i for i in items if not i.msg.startswith("не удалось")]:
        return Result(SKIPPED, f"PageSpeed не ответил: {errors[0] if errors else 'нет данных'}", items, "external")
    res = from_items(items, what="страниц (lab, mobile)", warn_only=True, source="external")
    res.summary += (" · сравнение с «текущим продом» — по истории запусков на дашборде; пороги CWV_LCP_MS / CWV_CLS / CWV_TBT_MS · "
                    + _psi().pool.summary("ключи PageSpeed"))
    return res


# ---------------------------------------------------------------- карты в GSC

@check("sitemap_submitted", "новые карты добавлены", source="external")
def sitemap_submitted(ctx):
    off = _off()
    if off:
        return off
    files = ctx.sitemap["files"]
    if not files:
        return Result(SKIPPED, "карты сайта не собирались: python collect_html.py --probes discovery", source="external")
    g = _gsc(ctx)
    try:
        listed = g.sitemaps()
    except ext.Skipped as e:
        return Result(SKIPPED, str(e), source="external")
    except ext.ExternalError as e:
        return Result(SKIPPED, str(e), source="external")
    by_path = {s["path"].rstrip("/"): s for s in listed if s.get("path")}
    items = []
    for f in files:
        u = f["url"].rstrip("/")
        s = by_path.get(u)
        if not s:
            items.append(Item(f["url"], False, "карты нет в списке Search Console", "sitemap"))
        elif s["errors"]:
            items.append(Item(f["url"], False, f"добавлена, но ошибок: {s['errors']}, предупреждений: {s['warnings']}", "sitemap"))
        elif s["pending"]:
            items.append(Item(f["url"], False, "добавлена, ещё не обработана (pending)", "sitemap"))
        else:
            items.append(Item(f["url"], True, f"обработана, скачана {s.get('last_downloaded') or '—'}, ресурс {s['property']}", "sitemap"))
    res = from_items(items, what="карт сайта", source="external")
    res.summary += f" · {g.summary()} · Яндекс Вебмастер: клиент не подключён — проверьте добавление карт вручную"
    return res
