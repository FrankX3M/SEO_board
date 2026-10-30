"""Проверки по краулу Screaming Frog (CSV, загружается в БД: раздел «Краул» в интерфейсе или import_crawl.py).

Если для сети есть выгрузка (ближайшая к запуску и не новее него, иначе самая свежая), эти проверки считаются по ВСЕМУ краулу,
а не по выборке справочника. Нет выгрузки — работает прежняя реализация (выборка/пробы/live), ничего не меняется.
Заменяемые пункты: Существующие страницы отдают 200, Нет ответов 5xx, Нет внутренних ссылок на 404, Перелинковка ведёт на новые URL,
Нет страниц-сирот, Все важные URL присутствуют (карта ↔ краул), Canonical не ведёт на 404/редирект/noindex.
"""
from __future__ import annotations

from .. import crawl
from .core import FAIL, PASS, REGISTRY, WARN, CheckDef, Item, Result
from .extras import xcheck

CAP = 500  # сколько проблемных URL сохраняем в результат (полный список — в разделе «Краул»)


def _imp(ctx):
    if not hasattr(ctx, "_crawl_imp"):
        crawl.ensure_schema(ctx.db)
        r = ctx.db.execute("SELECT started_at FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
        ctx._crawl_imp = crawl.latest_for(ctx.db, ctx.network, r["started_at"] if r else None)
    return ctx._crawl_imp


def _note(imp) -> str:
    return f"краул Screaming Frog от {(imp['taken_at'] or '')[:10]}, {imp['rows']} URL" + (f" «{imp['label']}»" if imp.get("label") else "")


def _count(ctx, imp, cond: str, *args) -> int:
    return ctx.db.execute(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {cond}", (imp["id"], *args)).fetchone()[0]


def _rows(ctx, imp, cond: str, order: str = "COALESCE(inlinks,0) DESC, address", limit: int = CAP):
    return ctx.db.execute(f"SELECT * FROM crawl_pages WHERE import_id=? AND {cond} ORDER BY {order} LIMIT ?",
                          (imp["id"], limit)).fetchall()


def _sample_ok(ctx, imp, cond: str, msg: str, n: int = 8) -> list[Item]:
    return [Item(r["address"], True, msg, "crawl") for r in _rows(ctx, imp, cond, "address", n)]


def _result(imp, total: int, bad: list[Item], bad_total: int, ok_sample: list[Item], what: str, *, warn_only=False,
            ok_text: str = "") -> Result:
    note = _note(imp)
    if total == 0:
        return Result("na", f"в краулe нет подходящих URL · {note}", [], "crawl", {"total": 0, "bad": 0})
    stats = {"total": total, "bad": bad_total}
    if not bad_total:
        return Result(PASS, (ok_text or f"{total} {what}: нарушений нет") + f" · {note}", ok_sample, "crawl", stats)
    extra = f" (в результате первые {len(bad)})" if bad_total > len(bad) else ""
    return Result(WARN if warn_only else FAIL, f"{bad_total} из {total} {what} не соответствуют{extra} · {note}",
                  bad + ok_sample, "crawl", stats)


HTML = "content_type LIKE 'text/html%'"
OK200 = f"({HTML} AND status=200)"
INDEXABLE = f"({OK200} AND indexability='Indexable')"


@xcheck("crawl_overview", "Итоговый краул", "Краул Screaming Frog: охват и декомпозиция показателей",
        "Отдельная сводка всего импортированного краула; alt проверяется по выборке HTML отдельно", "Средний", source="crawl")
def overview(ctx):
    imp = _imp(ctx)
    if not imp:
        return Result("na", "выгрузка Screaming Frog не загружена", source="crawl")
    values = crawl.metrics(ctx.db, imp["id"])
    items = [Item("", True, f"{title}: {values.get(mid, 'нет данных')}", "crawl")
             for mid, (title, unit, good) in crawl.CATALOG.items()]
    return Result("na", f"{_note(imp)} · информационная сводка без оценки; подробности — в отдельных проверках. Alt: только выборка справочника, в CSV не измеряется.",
                  items, "crawl", {"total": 0, "bad": 0, "coverage": imp["rows"]})


def status_200(ctx, imp):
    total = _count(ctx, imp, HTML)
    r4 = _rows(ctx, imp, f"{HTML} AND (status>=400 OR status IS NULL OR status=0)")
    n4 = _count(ctx, imp, f"{HTML} AND (status>=400 OR status IS NULL OR status=0)")
    n3 = _count(ctx, imp, f"{HTML} AND status BETWEEN 300 AND 399")
    bad = [Item(r["address"], False, f"статус {r['status']}, входящих ссылок: {r['inlinks'] or 0}", "crawl") for r in r4]
    if not n4 and n3:
        res = _result(imp, total, [], 0, _sample_ok(ctx, imp, OK200, "200"), "HTML-страниц")
        res.status = WARN
        res.summary = (f"{n3} из {total} HTML-URL отвечают редиректом 3xx (нужно вести ссылки сразу на конечные адреса — см. пункт про "
                       f"перелинковку); ошибок 4xx/5xx нет · {_note(imp)}")
        res.stats = {"total": total, "bad": n3}
        return res
    return _result(imp, total, bad, n4, _sample_ok(ctx, imp, OK200, "200"), "HTML-страниц")


def no_5xx(ctx, imp):
    cond = "(status BETWEEN 500 AND 599)"
    total = _count(ctx, imp, "1=1")
    bad = [Item(r["address"], False, f"статус {r['status'] or 'нет ответа'}", "crawl") for r in _rows(ctx, imp, cond)]
    return _result(imp, total, bad, _count(ctx, imp, cond), _sample_ok(ctx, imp, OK200, "200"), "URL")


@xcheck("crawl_no_response", "Коды ответа", "URL краула без HTTP-ответа",
        "Код 0 и отсутствие ответа учитываются отдельно от HTTP 5xx", "Средний", source="crawl")
def no_response(ctx):
    imp = _imp(ctx)
    if not imp:
        return Result("na", "нет краула для проверки HTTP-ответов", source="crawl")
    cond = "status IS NULL OR status=0"
    bad = [Item(r["address"], False, "HTTP-ответ не получен; уточните причину в исходной выгрузке краулера", "crawl")
           for r in _rows(ctx, imp, cond)]
    return _result(imp, _count(ctx, imp, "1=1"), bad, _count(ctx, imp, cond), [], "URL", warn_only=True)


def links_404(ctx, imp):
    cond = "status IN (404,410) AND COALESCE(inlinks,0)>0"
    total = _count(ctx, imp, HTML)
    bad = [Item(r["address"], False, f"{r['status']}: внутренних ссылок на этот URL — {r['inlinks']}", "crawl") for r in _rows(ctx, imp, cond)]
    return _result(imp, total, bad, _count(ctx, imp, cond), _sample_ok(ctx, imp, OK200, "200"), "HTML-URL",
                   ok_text="внутренних ссылок на 404/410 нет")


def links_redirects(ctx, imp):
    cond = "status BETWEEN 300 AND 399 AND COALESCE(inlinks,0)>0"
    total = _count(ctx, imp, "COALESCE(inlinks,0)>0")
    bad = [Item(r["address"], False, f"{r['status']} → {r['redirect_url'] or '?'}; внутренних ссылок на редирект — {r['inlinks']}", "crawl")
           for r in _rows(ctx, imp, cond)]
    res = _result(imp, total, bad, _count(ctx, imp, cond), [], "целей внутренних ссылок",
                  ok_text="внутренних ссылок на редиректы нет")
    links = ctx.db.execute(f"SELECT COALESCE(SUM(inlinks),0) FROM crawl_pages WHERE import_id=? AND {cond}",
                           (imp["id"],)).fetchone()[0]
    res.stats["inlinks"] = links
    res.summary += f" · сумма Inlinks на редиректы: {links}"
    return res


def orphans(ctx, imp):
    cond = f"{INDEXABLE} AND COALESCE(inlinks,0)=0 AND (depth IS NULL OR depth>0)"
    total = _count(ctx, imp, INDEXABLE)
    bad = [Item(r["address"], False, "нет входящих внутренних ссылок", "crawl") for r in _rows(ctx, imp, cond, "address")]
    n_bad = _count(ctx, imp, cond)
    note = ""
    sm = ctx.sitemap["urls"] if hasattr(ctx, "sitemap") else {}
    if sm:  # URL из карты, которых краул не нашёл ни по ссылкам, ни вообще
        known = {r[0] for r in ctx.db.execute("SELECT address FROM crawl_pages WHERE import_id=?", (imp["id"],))}
        missing = [u for u in sm if u not in known]
        n_bad += len(missing)
        total += len(missing)
        bad += [Item(u, False, "кандидат в сироты: есть в sitemap, но краулом не найден; требуется проверка доступности и полноты обхода", "sitemap") for u in missing[:max(0, CAP - len(bad))]]
        note = f" · учтено {len(sm)} URL из карты сайта"
    res = _result(imp, total, bad, n_bad, _sample_ok(ctx, imp, INDEXABLE + " AND COALESCE(inlinks,0)>0", "есть входящие ссылки"),
                  "индексируемых URL", ok_text="в доступном крауле и sitemap кандидатов в сироты не найдено")
    res.summary += note + " · отсутствие в крауле не доказывает отсутствие внутренних ссылок"
    return res


def sitemap_complete(ctx, imp):
    sm = ctx.sitemap["urls"] if hasattr(ctx, "sitemap") else {}
    if not sm:
        return None  # нет карты — пусть отработает прежняя проверка
    in_map = set(sm)
    pages = ctx.db.execute(f"SELECT address, title FROM crawl_pages WHERE import_id=? AND {INDEXABLE} AND address NOT LIKE '%?%'",
                           (imp["id"],)).fetchall()
    miss = [r["address"] for r in pages if r["address"] not in in_map]
    bad = [Item(u, False, "индексируемая страница есть в краулe, но нет в sitemap", "crawl") for u in miss[:CAP]]
    ok = [Item(r["address"], True, "есть в sitemap", "crawl") for r in pages if r["address"] in in_map][:8]
    return _result(imp, len(pages), bad, len(miss), ok, "индексируемых URL без параметров", ok_text="все есть в карте сайта")


def canonical_target_ok(ctx, imp):
    base = ("FROM crawl_pages x LEFT JOIN crawl_pages y ON y.import_id=x.import_id AND y.address=x.canonical "
            "WHERE x.import_id=? AND x.content_type LIKE 'text/html%' AND x.status=200 AND COALESCE(x.canonical,'')!=''")
    total = ctx.db.execute(f"SELECT COUNT(*) {base}", (imp["id"],)).fetchone()[0]
    cond = " AND (y.status!=200 OR COALESCE(y.indexability_status,'') LIKE '%oindex%' OR y.address IS NULL OR y.status IS NULL)"
    n_bad = ctx.db.execute(f"SELECT COUNT(*) {base}{cond}", (imp["id"],)).fetchone()[0]
    rows = ctx.db.execute(f"SELECT x.address, x.canonical, y.status, y.indexability_status {base}{cond} LIMIT ?", (imp["id"], CAP)).fetchall()
    bad = [Item(r["address"], False, f"canonical → {r['canonical']}: {r['status']}" +
                (f", {r['indexability_status']}" if r["indexability_status"] else ""), "crawl") for r in rows]
    if not total:
        return Result("na", f"в краулe нет страниц с canonical на другой найденный URL · {_note(imp)}", [], "crawl",
                      {"total": 0, "bad": 0})
    unknown = ctx.db.execute(f"SELECT COUNT(*) {base} AND (y.address IS NULL OR y.status IS NULL)", (imp["id"],)).fetchone()[0]
    res = _result(imp, total, bad, n_bad, [], "целей canonical", ok_text="цели canonical отвечают 200 и не имеют признака noindex в крауле")
    res.stats["unknown"] = unknown
    if unknown:
        res.summary += f" · не проверены по краулу: {unknown}"
        if unknown == n_bad:
            res.status = WARN
    return res


def robots_directive(ctx, imp, directive):
    cond = f"{OK200} AND (lower(COALESCE(meta_robots,'')) LIKE ? OR lower(COALESCE(x_robots,'')) LIKE ?)"
    pattern = '%' + directive + '%'
    n = _count(ctx, imp, cond, pattern, pattern)
    rows = ctx.db.execute(f"SELECT address FROM crawl_pages WHERE import_id=? AND {cond} LIMIT ?",
                          (imp['id'], pattern, pattern, CAP)).fetchall()
    bad = [Item(r['address'], False, f"обнаружен {directive}; подтвердите намеренность запрета", 'crawl') for r in rows]
    return _result(imp, _count(ctx, imp, OK200), bad, n, [], 'HTML-страниц с ответом 200', warn_only=True)


WRAP = {"status_200": status_200, "status_no5xx": no_5xx, "links_404": links_404, "links_redirects": links_redirects,
        "orphans": orphans, "sitemap_complete": sitemap_complete, "canonical_target_ok": canonical_target_ok,
        "noindex_accidental": lambda ctx, imp: robots_directive(ctx, imp, 'noindex'),
        "nofollow_accidental": lambda ctx, imp: robots_directive(ctx, imp, 'nofollow')}


def install() -> None:
    for cid, fn in WRAP.items():
        d = REGISTRY.get(cid)
        if d is None or getattr(d.fn, "_crawl_wrapped", False):
            continue

        def make(orig, crawl_fn):
            def wrapper(ctx):
                imp = _imp(ctx)
                if imp:
                    res = crawl_fn(ctx, imp)
                    if res is not None:
                        return res
                return orig(ctx)
            wrapper._crawl_wrapped = True  # type: ignore[attr-defined]
            return wrapper

        REGISTRY[cid] = CheckDef(cid, d.titles, make(d.fn, fn), d.source)


install()
