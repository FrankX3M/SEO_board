"""Однократно: подключает модуль «Сироты» (индекс Вебмастера/GSC против краула) к проекту. Запуск из корня проекта: python apply_orphans_patch.py

Меняет: techaudit/web/app.py (маршруты /orphans…), templates/base.html (пункт меню), techaudit/webdb.py (таблицы),
techaudit/appsettings.py (настройки), techaudit/checks/x_urls.py и plan.py (проверка «Страницы из индекса, до которых не дошёл краул»),
README_web.md. Повторный запуск безопасен (уже внесённое пропускается). Перед правкой кладёт копии в _backup_before_orphans/.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKUP = ROOT / "_backup_before_orphans"

ROUTES = '''
    # ================================================================== сироты: индекс поисковиков против краула

    def orphan_probe_default() -> int:
        return int(os.environ.get("ORPHAN_PROBE_LIMIT") or 300)

    def net_url(net: str) -> str:
        cfg = settings.networks.get(net)
        return (cfg.url or "").rstrip("/") if cfg else ""

    def ywm_token() -> str:
        return (settings.integrations.get("yandex_webmaster") or os.environ.get("YANDEX_WEBMASTER_TOKEN") or "").strip()

    def to_id(v) -> int | None:
        v = str(v or "").strip()
        return int(v) if v.isdigit() else None

    @app.get("/orphans", response_class=HTMLResponse)
    def orphans_page(request: Request, net: str = "", db=Depends(get_db)):
        orph.ensure_schema(db)
        net = default_net(db, net)
        cfg = settings.networks.get(net)
        return render(request, "orphans.html", "orphans", net=net, networks=networks_list(db), idx=ix.imports(db, net) if net else [],
                      crawls=cr.imports(db, net, 20) if net else [], runs=orph.runs(db, net) if net else [], sources=ix.SOURCES,
                      has_ywm=bool(ywm_token()), has_gsc=bool(net and keymod.gsc_accounts(net)[0]),
                      has_site=bool(cfg and cfg.url), gsc_days=int(os.environ.get("GSC_PAGES_DAYS") or 90),
                      probe_default=orphan_probe_default(), max_depth=int(os.environ.get("MAX_DEPTH") or 4))

    def _fetch_msg(res: dict, what: str) -> str:
        m = f"{what}: получено {res['rows']} страниц"
        if res.get("skipped_external"):
            m += f", чужих адресов пропущено: {res['skipped_external']}"
        if res.get("truncated"):
            m += f". В Вебмастере {res['total_reported']} страниц, API отдаёт не больше {ix.Ywm.MAX_URLS} — остальное добавьте файлом"
        return m

    @app.post("/orphans/fetch/ywm")
    async def orphans_fetch_ywm(request: Request):
        form = await request.form()
        net = (form.get("network") or "").strip()
        host_id = (form.get("host_id") or "").strip()
        base = net_url(net)
        if not base:
            return redirect(f"/orphans?net={quote(net)}", err="Для сети не задан адрес прода («Доступ → Сети»)")

        def work():
            db = connect(db_path)
            try:
                return ix.fetch_ywm(db, net, base, ywm_token(), host_id=host_id)
            finally:
                db.close()
        try:
            res = await run_in_threadpool(work)
        except ix.IndexedError as e:
            return redirect(f"/orphans?net={quote(net)}", err=str(e))
        return redirect(f"/orphans?net={quote(net)}", msg=_fetch_msg(res, "Яндекс Вебмастер"))

    @app.post("/orphans/fetch/gsc")
    async def orphans_fetch_gsc(request: Request):
        form = await request.form()
        net = (form.get("network") or "").strip()
        days = min(480, max(7, to_id(form.get("days")) or 90))
        base = net_url(net)
        if not base:
            return redirect(f"/orphans?net={quote(net)}", err="Для сети не задан адрес прода («Доступ → Сети»)")

        def work():
            db = connect(db_path)
            try:
                return ix.fetch_gsc(db, net, base, days=days)
            finally:
                db.close()
        try:
            res = await run_in_threadpool(work)
        except ix.IndexedError as e:
            return redirect(f"/orphans?net={quote(net)}", err=str(e))
        return redirect(f"/orphans?net={quote(net)}", msg=_fetch_msg(res, "Google Search Console"))

    @app.post("/orphans/upload")
    async def orphans_upload(request: Request):
        form = await request.form()
        net = (form.get("network") or "").strip()
        src = (form.get("source") or "").strip()
        up = form.get("file")
        if src not in ix.SOURCES:
            return redirect(f"/orphans?net={quote(net)}", err="Выберите источник файла")
        if up is None or not getattr(up, "filename", ""):
            return redirect(f"/orphans?net={quote(net)}", err="Выберите файл")
        label, fname, host = (form.get("label") or "").strip()[:120], up.filename, net_host(net)

        def work():
            db = connect(db_path)
            try:
                return ix.import_file(db, net, src, up.file, filename=fname, host=host, label=label)
            finally:
                db.close()
        try:
            res = await run_in_threadpool(work)
        except (ix.IndexedError, ValueError, UnicodeError) as e:
            return redirect(f"/orphans?net={quote(net)}", err=f"Файл не загружен: {e}")
        return redirect(f"/orphans?net={quote(net)}", msg=_fetch_msg(res, ix.SOURCES[src] + " (файл)"))

    @app.post("/orphans/index/{imp_id:int}/delete")
    def orphans_index_delete(imp_id: int, db=Depends(get_db)):
        ix.ensure_schema(db)
        r = db.execute("SELECT network FROM index_imports WHERE id=?", (imp_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        ix.delete_import(db, imp_id)
        return redirect(f"/orphans?net={quote(r['network'])}", msg="Выгрузка удалена")

    @app.post("/orphans/compare")
    async def orphans_compare(request: Request):
        form = await request.form()
        net = (form.get("network") or "").strip()
        crawl_id, ywm_id, gsc_id = to_id(form.get("crawl_id")), to_id(form.get("ywm_id")), to_id(form.get("gsc_id"))
        probe = min(5000, to_id(form.get("probe")) or 0)

        def work():
            db = connect(db_path)
            try:
                return orph.run_compare(db, settings, net, crawl_id=crawl_id, ywm_id=ywm_id, gsc_id=gsc_id, probe=probe)
            finally:
                db.close()
        try:
            res = await run_in_threadpool(work)
        except (ix.IndexedError, ConfigError) as e:
            return redirect(f"/orphans?net={quote(net)}", err=str(e))
        return redirect(f"/orphans/{res['run_id']}", msg=f"Сирот: {res['summary']['orphans']} из {res['summary']['index_total']} URL в индексе")

    ORPHAN_FILTERS = [("orphans", "Сироты"), ("problems", "Все проблемы"), ("not_in_crawl", "Нет в краулe"), ("no_inlinks", "Нет ссылок"),
                      ("deep", "Глубоко"), ("stale", "Устарели"), ("bad_status", "Не 200"), ("non_indexable", "Расхождение"),
                      ("ok", "В порядке"), ("all", "Все")]

    def _orphan_counts(db, run_id: int, src: str, q: str) -> dict:
        where, args = orph.filter_sql("all", src, q or None)
        by = {r[0]: r[1] for r in db.execute(f"SELECT category, COUNT(*) FROM orphan_rows WHERE {where} GROUP BY category", [run_id, *args])}
        c = {k: by.get(k, 0) for k in orph.CATEGORIES}
        c["orphans"] = sum(c[k] for k in orph.ORPHAN_CATS)
        c["problems"] = sum(c[k] for k in orph.PROBLEM_CATS)
        c["all"] = sum(by.values())
        return c

    @app.get("/orphans/{run_id:int}", response_class=HTMLResponse)
    def orphans_detail(request: Request, run_id: int, cat: str = "orphans", src: str = "", q: str = "", page: int = 1, db=Depends(get_db)):
        run = orph.get_run(db, run_id)
        if not run:
            raise HTTPException(404, "сравнение не найдено")
        valid = {k for k, _ in ORPHAN_FILTERS}
        cat = cat if cat in valid else "orphans"
        src = src if src in ("ywm", "gsc", "both") else ""
        per, page = 100, max(1, page)
        rows, total = orph.rows(db, run_id, cat, src, q.strip() or None, per, (page - 1) * per)
        return render(request, "orphans_detail.html", "orphans", run=run, rows=rows, total=total, page=page, pages=max(1, -(-total // per)),
                      cat=cat, src=src, q=q, filters=ORPHAN_FILTERS, counts=_orphan_counts(db, run_id, src, q.strip()),
                      cats=orph.CATEGORIES, sources=ix.SOURCES, secs=orph.sections(db, run_id))

    @app.post("/orphans/{run_id:int}/delete")
    def orphans_delete(run_id: int, db=Depends(get_db)):
        run = orph.get_run(db, run_id)
        if not run:
            raise HTTPException(404)
        orph.delete_run(db, run_id)
        return redirect(f"/orphans?net={quote(run['network'])}", msg="Результат удалён")

    @app.get("/orphans/{run_id:int}/export.csv")
    def orphans_export_csv(run_id: int, cat: str = "orphans", src: str = "", q: str = "", db=Depends(get_db)):
        rows, _ = orph.rows(db, run_id, cat, src, q.strip() or None, 1_000_000, 0)
        return csv_response([orph.export_row(r) for r in rows], orph.EXPORT_HEAD, f"orphans-{run_id}-{cat}.csv")

    @app.get("/orphans/{run_id:int}/export.xlsx")
    def orphans_export_xlsx(run_id: int, cat: str = "orphans", src: str = "", q: str = "", db=Depends(get_db)):
        import io
        from openpyxl import Workbook
        rows, _ = orph.rows(db, run_id, cat, src, q.strip() or None, 1_000_000, 0)
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Сироты")
        ws.append(orph.EXPORT_HEAD)
        for r in rows:
            ws.append(orph.export_row(r))
        buf = io.BytesIO()
        wb.save(buf)
        name = f"orphans-{run_id}-{cat}.xlsx"
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

'''

XCHECK = '''

@xcheck("index_orphans", B_URL, "Страницы из индекса поисковиков достижимы по ссылкам (нет сирот)",
        "Каждая страница из Яндекс Вебмастера / Search Console есть в краулe, на неё ведут внутренние ссылки и она не глубже MAX_DEPTH кликов",
        "Высокий", source="crawl")
def index_orphans(ctx):
    from .. import indexed as ix
    from .. import orphans as orph
    imp = crawl_import(ctx)
    if not imp:
        return Result(SKIPPED, "нужна выгрузка Screaming Frog: раздел «Краул» или import_crawl.py", source="crawl")
    ids = {}
    for src in ("ywm", "gsc"):
        last = ix.latest(ctx.db, ctx.network, src)
        if last:
            ids[src] = last["id"]
    if not ids:
        return Result(SKIPPED, "нет выгрузок индекса: раздел «Сироты» (Вебмастер / Search Console, API или файл)", source="crawl")
    res = orph.compare(ctx.db, imp["id"], ids, max_depth=ctx.cs.max_depth, probes=orph.saved_probes(ctx.db, ctx.network))
    s = res["summary"]
    bad = [r for r in res["rows"] if r["category"] in orph.ORPHAN_CATS]
    bad.sort(key=lambda r: (-(r["impressions"] or 0), r["url"]))
    labels = {"not_in_crawl": "нет в краулe", "no_inlinks": "нет входящих ссылок", "deep": "глубина %s > %s"}
    items = []
    for r in bad[:CAP]:
        msg = labels[r["category"]] % (r["depth"], ctx.cs.max_depth) if r["category"] == "deep" else labels[r["category"]]
        items.append(Item(r["url"], False, msg + (" · " + ", ".join(n for n, f in (("Яндекс", r["in_ywm"]), ("Google", r["in_gsc"])) if f)), "crawl"))
    total = s["index_total"]
    note = f" · индекс: {', '.join(ix.SOURCES[k] for k in ids)}; краул от {(imp['taken_at'] or '')[:10]}"
    stats = {"total": total, "bad": len(bad)}
    if not bad:
        return Result(PASS, f"все {total} страниц из индекса достижимы краулом{note}", [], "crawl", stats)
    share = len(bad) / total if total else 0
    warn = ("; краул нашёл менее половины адресов — сначала проверьте его полноту" if (s["coverage"] or 100) < 50 else "")
    return Result(FAIL if share > 0.05 else WARN,
                  f"{len(bad)} из {total} страниц индекса — сироты ({share:.0%}): нет в краулe {s['by_category']['not_in_crawl']}, "
                  f"без ссылок {s['by_category']['no_inlinks']}, глубже {ctx.cs.max_depth} кликов {s['by_category']['deep']}{warn}{note}",
                  items, "crawl", stats)
'''

PLAN_LINE = '    "index_orphans": ("SEO", 4, "Внутренние ссылки на страницы-сироты из индекса или вывод ненужных из индекса"),\n'
PLAN_ANCHOR = '    "crawl_depth": '

SETTINGS_FIELDS = '''    dict(key="GSC_PAGES_DAYS", group="misc", kind="int", default="90", label="Сироты: глубина выгрузки страниц GSC, дней",
         help="За какой период брать страницы с показами в Google (Search Analytics)."),
    dict(key="ORPHAN_PROBE_LIMIT", group="misc", kind="int", default="300", label="Сироты: живая проверка, URL за сравнение",
         help="Сколько адресов «нет в краулe» запросить с сайта, чтобы отличить живых сирот от устаревших записей индекса. 0 — не проверять."),
'''


def backup(p: Path) -> None:
    dst = BACKUP / p.relative_to(ROOT)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)


def edit(rel: str, fn) -> str:
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    new = fn(s)
    if new is None:
        return f"= {rel}: уже внесено"
    backup(p)
    p.write_text(new, encoding="utf-8", newline="")
    return f"+ {rel}"


def patch_app(s: str):
    if "/orphans" in s:
        return None
    s = s.replace("from .. import evidence as ev\n", "from .. import evidence as ev\nfrom .. import indexed as ix\n", 1)
    s = s.replace("from .. import metrics as mx\n", "from .. import metrics as mx\nfrom .. import orphans as orph\n", 1)
    marker = "    # ================================================================== ИИ: что делает, статус, запуск"
    if marker not in s:
        raise SystemExit("app.py: не найдено место для вставки маршрутов (раздел «ИИ»)")
    return s.replace(marker, ROUTES.lstrip("\n") + "\n" + marker, 1)


def patch_base(s: str):
    if "'orphans'" in s:
        return None
    return s.replace("('crawl','/crawl','Краул'),", "('crawl','/crawl','Краул'),('orphans','/orphans','Сироты'),", 1)


def patch_webdb(s: str):
    if "orphans_mod" in s:
        return None
    s = s.replace("    from . import external as external_mod\n", "    from . import external as external_mod\n    from . import indexed as indexed_mod\n    from . import orphans as orphans_mod\n", 1)
    return s.replace("crawl_mod.SCHEMA, external_mod.SCHEMA,", "crawl_mod.SCHEMA, indexed_mod.SCHEMA, orphans_mod.SCHEMA, external_mod.SCHEMA,", 1)


def patch_settings(s: str):
    if "GSC_PAGES_DAYS" in s:
        return None
    anchor = '    dict(key="CRAWL_MAX_MB"'
    return s.replace(anchor, SETTINGS_FIELDS + anchor, 1)


def patch_xurls(s: str):
    if "index_orphans" in s:
        return None
    return s.rstrip("\n") + "\n" + XCHECK


def patch_plan(s: str):
    if "index_orphans" in s:
        return None
    i = s.index(PLAN_ANCHOR)
    return s[:i] + PLAN_LINE + s[i:]


def patch_readme(s: str):
    if "Сироты" in s:
        return None
    add = ("- **Сироты: индекс против краула.** Раздел «Сироты»: выгрузка страниц в поиске из Яндекс Вебмастера (API, до 50 000 URL) и страниц с показами из "
           "Google Search Console (API) или из файла CSV/XLSX; сравнение с краулом Screaming Frog: нет в краулe, нет входящих ссылок, глубже MAX_DEPTH, "
           "не 200, расхождение сигналов; живая проверка «сирот» (200 или уже 404/редирект), экспорт CSV/Excel. Консоль: `python find_orphans.py --help`. "
           "Проверка `index_orphans` попадает в отчёты.\n")
    marker = "- **Краулы Screaming Frog.**"
    i = s.find(marker)
    if i < 0:
        return s.rstrip("\n") + "\n" + add
    j = s.find("\n", i) + 1
    return s[:j] + add + s[j:]


def main() -> int:
    for rel, fn in (("techaudit/web/app.py", patch_app), ("techaudit/web/templates/base.html", patch_base),
                    ("techaudit/webdb.py", patch_webdb), ("techaudit/appsettings.py", patch_settings),
                    ("techaudit/checks/x_urls.py", patch_xurls), ("techaudit/checks/plan.py", patch_plan),
                    ("README_web.md", patch_readme)):
        print(edit(rel, fn))
    print("Готово. Копии прежних файлов: _backup_before_orphans/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
