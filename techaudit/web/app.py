"""Веб-интерфейс техаудита (FastAPI + Jinja + HTMX + Chart.js).

Запуск: python web.py  → http://127.0.0.1:8765
Слушает только локальный адрес, без входа. Все настройки (сети, токены, UA, карты сайта, ключи, пороги)
задаются на странице «Доступ и настройки» и хранятся в БД; консольные скрипты читают те же значения.
Секреты в интерфейсе не показываются.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import appsettings as aps
from .. import blocks as bl
from .. import crawl as cr
from .. import db_registry as reg
from .. import evidence as ev
from .. import indexed as ix
from .. import keys as keymod
from .. import metrics as mx
from .. import orphans as orph
from .. import sitemapcheck as smc
from .. import tz_chat as tzc
from ..config import BUILTIN_UA, ConfigError, load_settings
from ..webdb import connect, init_all, tx
from . import jobs as jb

HERE = Path(__file__).parent
MAX_UPLOAD = 2 * 1024 * 1024
MAX_ROWS = 20000
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


# ------------------------------------------------------------------ вспомогательное

def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return str(iso)


def fmt_num(v, unit: str = "") -> str:
    if v is None:
        return "—"
    s = f"{v:,.1f}".replace(",", " ").replace(".0", "") if isinstance(v, float) else f"{v:,}".replace(",", " ")
    return f"{s} {unit}".strip() if unit else s


def redirect(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = {}
    if msg:
        q["msg"] = msg
    if err:
        q["err"] = err
    sep = "&" if "?" in path else "?"
    return RedirectResponse(path + (sep + urlencode(q) if q else ""), status_code=303)


def decode_upload(data: bytes) -> str:
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"файл больше {MAX_UPLOAD // 1024 // 1024} МБ")
    for enc in ("utf-8-sig", "cp1251"):  # CSV из русского Excel часто в cp1251
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("не удалось определить кодировку (нужен UTF-8 или Windows-1251)")
    if text.count("\n") > MAX_ROWS:
        raise ValueError(f"в файле больше {MAX_ROWS} строк — разбейте на части")
    return text


def safe_back(back: str | None, default: str) -> str:
    return back if back and back.startswith("/") and not back.startswith("//") else default


def params_from_form(form) -> dict:
    return {
        "networks": form.getlist("networks"), "limit": form.get("limit"), "variants": form.get("variants"),
        "probes": form.get("probes"), "no_diff": form.get("no_diff") == "1", "ua": form.get("ua"),
        "token": form.get("token"), "checks": form.get("checks") == "1", "live": form.get("live") == "1",
        "mobile": form.get("mobile") == "1", "baseline": form.get("baseline") or "",
        "external": form.get("external") == "1", "debug": form.get("debug") == "1", "ai": form.get("ai") == "1",
    }


# ------------------------------------------------------------------ приложение

def create_app(env_file: str | None = None, *, start_jobs: bool = True, extra_hosts: tuple[str, ...] = ()) -> FastAPI:
    try:
        settings = load_settings(env_file)
    except ConfigError as e:
        raise SystemExit(f"Ошибка настроек: {e}") from e
    db_path = init_all(settings.data_dir)
    if not settings.from_db:  # БД могла быть создана только что — подхватить из неё настройки
        settings.__dict__.update(load_settings(env_file).__dict__)
    manager = jb.JobManager(db_path, settings.data_dir / "logs")
    allowed = ALLOWED_HOSTS | set(extra_hosts)

    @asynccontextmanager
    async def lifespan(_app):  # noqa: ANN001
        if start_jobs:
            manager.start()
        yield
        if start_jobs:
            manager.stop()

    app = FastAPI(title="Техаудит", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings, app.state.db_path, app.state.jobs = settings, db_path, manager

    # первичная загрузка справочника из файлов, если БД пока пустая
    boot = connect(db_path)
    try:
        if boot.execute("SELECT COUNT(*) FROM ref_urls").fetchone()[0] == 0:
            from ..config import ROOT
            rep = reg.seed_from_files(boot, ROOT / "справочники", settings.registry_dir)
            print(f"Справочник загружен из файлов в БД: {rep}")
    finally:
        boot.close()

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["dt"] = fmt_dt
    templates.env.filters["num"] = fmt_num
    from ..checks.core import STATUS_LABELS as CHECK_LABELS
    templates.env.globals.update(STATUS_LABELS=reg.STATUS_LABELS, STATUS_RU=CHECK_LABELS, mlabel=mx.label)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    reports_dir = settings.data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/reports", StaticFiles(directory=str(reports_dir)), name="reports")

    # ---- защита от обращений с чужих сайтов (DNS rebinding / CSRF для локального сервера)
    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN202
        h = request.headers.get("host") or ""
        host = h[1:h.index("]")] if h.startswith("[") and "]" in h else (h.rsplit(":", 1)[0] if ":" in h else h)
        if host and host not in allowed:
            return PlainTextResponse("Недопустимый Host", status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != "null":
                oh = re.sub(r"^https?://", "", origin).rsplit(":", 1)[0].strip("[]")
                if oh not in allowed:
                    return PlainTextResponse("Недопустимый Origin", status_code=403)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):  # noqa: ANN202
        """Вместо пустого «Internal Server Error» показываем причину и пишем traceback в консоль web.py."""
        import html as _html
        tb = traceback.format_exc()
        print(f"\n[ОШИБКА] {request.method} {request.url.path}\n{tb}", flush=True)
        body = (f"<!doctype html><meta charset='utf-8'><title>Ошибка</title><body style='font-family:system-ui;max-width:900px;margin:30px auto'>"
                f"<h2>Внутренняя ошибка</h2><p><b>{_html.escape(request.method)} {_html.escape(request.url.path)}</b>: "
                f"{_html.escape(type(exc).__name__)}: {_html.escape(str(exc))}</p>"
                f"<pre style='white-space:pre-wrap;background:#f4f4f4;padding:10px;font-size:12px'>{_html.escape(tb[-3000:])}</pre>"
                f"<p><a href='javascript:history.back()'>← назад</a></p>")
        return HTMLResponse(body, status_code=500)

    def get_db(request: Request):
        db = connect(request.app.state.db_path)
        try:
            yield db
        finally:
            db.close()

    def networks_list(db) -> list[str]:
        names = list(settings.networks)
        for r in db.execute("SELECT DISTINCT network FROM pages UNION SELECT DISTINCT network FROM check_results"):
            if r[0] not in names:
                names.append(r[0])
        return names

    def render(request: Request, name: str, nav: str, **ctx):
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("err", request.query_params.get("err"))
        return templates.TemplateResponse(request, name, {"nav": nav, **ctx})

    def mirror(db) -> None:
        try:
            reg.sync_csv_files(db, settings.registry_dir, list(settings.networks))
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def site_base(db, site: str) -> str:
        cfg = settings.networks.get(site)
        if cfg and cfg.url:
            return cfg.url.rstrip("/")
        r = db.execute("SELECT host FROM ref_sites WHERE site=?", (site,)).fetchone()
        return ("https://" + r["host"]) if r and r["host"] else ""

    # ================================================================== общие помощники для новых разделов

    def results_db(db, run_id: str, net: str = "") -> dict:
        out: dict = {}
        q = "SELECT * FROM check_results WHERE run_id=?" + (" AND network=?" if net else "")
        for r in db.execute(q, (run_id, net) if net else (run_id,)):
            out.setdefault(r["network"], {})[r["check_id"]] = {
                "status": r["status"], "summary": r["summary"], "items": json.loads(r["items"] or "[]"), "source": r["source"]}
        return out

    def notes_db(db, run_id: str) -> dict:
        out: dict = {}
        try:
            for r in db.execute("SELECT * FROM ai_notes WHERE run_id=?", (run_id,)):
                d = out.setdefault(r["network"], {"model": r["model"], "at": r["created_at"]})
                d[r["kind"]] = json.loads(r["data"])
        except sqlite3.OperationalError:
            pass
        return out

    def latest_checked_run(db, net: str = "") -> str | None:
        q = ("SELECT cr.run_id FROM check_results cr JOIN runs r ON r.run_id=cr.run_id" + (" WHERE cr.network=?" if net else "") +
             " GROUP BY cr.run_id ORDER BY r.started_at DESC LIMIT 1")
        r = db.execute(q, (net,) if net else ()).fetchone()
        return r[0] if r else None

    def period_of(p: str, d_from: str, d_to: str) -> tuple[str | None, str | None]:
        if d_from or d_to:
            return d_from or None, d_to or None
        if p.isdigit() and int(p) > 0:
            from datetime import date, timedelta
            return (date.today() - timedelta(days=int(p))).isoformat(), None
        return None, None

    def default_net(db, net: str) -> str:
        if net == "all":
            return ""
        nets = networks_list(db)
        return net or (nets[0] if nets else "")

    # ================================================================== дашборд

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, net: str = "", p: str = "30", date_from: str = Query("", alias="from"),
              date_to: str = Query("", alias="to"), db=Depends(get_db)):
        """Обзор: плитки блоков, светофор, что изменилось. Клик по плитке — страница блока (/block)."""
        net = default_net(db, net)
        f, t = period_of(p, date_from, date_to)
        data = mx.overview(db, net or None, f, t)
        last_job = db.execute("SELECT * FROM web_jobs ORDER BY id DESC LIMIT 1").fetchone()
        return render(request, "index.html", "dashboard", net=net, p=p, date_from=date_from, date_to=date_to,
                      networks=networks_list(db), data=data, counts=reg.counts(db),
                      last_job=dict(last_job) if last_job else None)

    @app.get("/api/overview")
    def api_overview(net: str = "", p: str = "30", date_from: str = Query("", alias="from"),
                     date_to: str = Query("", alias="to"), db=Depends(get_db)):
        f, t = period_of(p, date_from, date_to)
        return JSONResponse(mx.overview(db, (None if net in ("", "all") else net), f, t))

    @app.get("/explore", response_class=HTMLResponse)
    def explore(request: Request, net: str = "", p: str = "30", date_from: str = Query("", alias="from"),
                date_to: str = Query("", alias="to"), db=Depends(get_db)):
        """Свободный режим: дерево метрик слева, графики справа (прежний дашборд)."""
        net = default_net(db, net)
        f, t = period_of(p, date_from, date_to)
        data = mx.board(db, net or None, f, t)
        last_job = db.execute("SELECT * FROM web_jobs ORDER BY id DESC LIMIT 1").fetchone()
        return render(request, "explore.html", "dashboard", net=net, p=p, date_from=date_from, date_to=date_to,
                      networks=networks_list(db), data=data, counts=reg.counts(db),
                      last_job=dict(last_job) if last_job else None)

    # ---- страница блока: детальный дашборд
    def block_ctx(db, name: str, net: str, p: str, date_from: str, date_to: str) -> dict:
        nets = networks_list(db)
        net = net if net in nets else (nets[0] if nets else "")
        f, t = period_of(p, date_from, date_to)
        v = mx.block_view(db, net, name, f, t)
        if not v:
            raise HTTPException(404, f"Блок «{name}» не найден: нет данных за выбранный период")
        notes = (notes_db(db, v["run_id"]).get(net) or {}) if v["run_id"] else {}
        return {"net": net, "networks": nets, "p": p, "date_from": date_from, "date_to": date_to, "v": v,
                "ai": notes.get("block:" + name), "ai_model": notes.get("model"),
                "tz": (notes.get("tz:" + name) or {}).get("text") if isinstance(notes.get("tz:" + name), dict) else None,
                "chat": tzc.clean_history((notes.get("chat:" + name) or {}).get("messages")) if isinstance(notes.get("chat:" + name), dict) else []}

    @app.get("/block", response_class=HTMLResponse)
    def block_page(request: Request, name: str, net: str = "", p: str = "30", date_from: str = Query("", alias="from"),
                   date_to: str = Query("", alias="to"), db=Depends(get_db)):
        return render(request, "block.html", "dashboard", **block_ctx(db, name, net, p, date_from, date_to))

    @app.post("/block/ai", response_class=HTMLResponse)
    async def block_ai(request: Request, db=Depends(get_db)):
        """ИИ-расшифровка блока по кнопке: простыми словами, что значат цифры и с чего начать. Статусы не меняет."""
        form = await request.form()
        name, net, run = form.get("name") or "", form.get("net") or "", form.get("run") or ""
        ctx = block_ctx(db, name, net, form.get("p") or "30", form.get("from") or "", form.get("to") or "")
        v = ctx["v"]
        out = await run_in_threadpool(block_ai_work, db_path=request.app.state.db_path, v=v, net=ctx["net"], run=run or v["run_id"])
        ctx["ai"], ctx["ai_error"] = out.get("data"), out.get("error")
        ctx["ai_model"] = out.get("model")
        return templates.TemplateResponse(request, "_block_ai.html", ctx)

    def block_ai_work(*, db_path, v: dict, net: str, run: str) -> dict:
        from .. import integrations
        from ..ai import AIClient, AIError
        from ..checks.ai_stage import SYSTEM_RULES
        from ..storage import Store, now_iso
        con = connect(db_path)
        try:
            items_by = results_db(con, run, net).get(net) or {}
        finally:
            con.close()
        payload = {
            "block": v["name"], "meaning": v["help"], "score": (v["score"] or {}).get("value"), "score_delta": (v["score"] or {}).get("delta"),
            "checks": [{"id": r["check_id"], "title": r["title"], "priority": r["priority"], "status": r["status"], "summary": r["summary"],
                        "criterion": (r.get("criterion") or "")[:300], "trend_percent": r.get("spark"),
                        "examples": [{"url": i["url"], "msg": str(i["msg"])[:160]} for i in
                                     [i for i in (items_by.get(r["check_id"]) or {}).get("items", []) if not i.get("ok")][:2]]}
                       for r in v["rows"] if r["status"] in ("fail", "warn")][:15],
            "metrics": [{"title": m["label"], "value": m["value"], "unit": m["unit"], "delta": m["delta"]} for m in v["metrics"]][:12],
            "address_tests": [{"name": t["name"], "passed_percent": t["share"], "failed": t["failed"]} for t in v["tests"]],
        }
        prompt = ("Ниже данные одного блока техаудита сайта. Объясни результат человеку, который не является техническим специалистом. "
                  "Верни JSON: {\"summary\": \"2-4 предложения: что происходит в блоке, в чём главный риск для поискового трафика\", "
                  "\"explain\": [{\"title\": \"название проверки или метрики из данных\", \"meaning\": \"что это значит простыми словами\", "
                  "\"action\": \"что конкретно сделать\"}], \"first_step\": \"с чего начать\"}. "
                  "В explain — не более 5 самых важных пунктов. Используй только данные ниже.\n\nДАННЫЕ:\n" + json.dumps(payload, ensure_ascii=False))
        holder: dict = {}

        def work(key: str):
            store = Store(settings.data_dir)
            holder["store"] = store
            client = AIClient(store, key, verify=settings.verify_tls)
            holder["client"] = client
            try:
                return client.chat_json(SYSTEM_RULES, prompt, version="block-v1", max_tokens=2500)
            finally:
                client.close()

        out = integrations.guarded(settings, "ai", work)
        try:
            if out.status != "ok":
                msg = out.reason or "ИИ недоступен"
                if out.status == "skipped":
                    msg = "ИИ не подключён: " + msg
                return {"error": msg}
            st = holder["store"]
            st.db.execute("DELETE FROM ai_notes WHERE run_id=? AND network=? AND kind=?", (run, net, "block:" + v["name"]))
            st.db.execute("INSERT INTO ai_notes(run_id,network,kind,data,model,created_at) VALUES (?,?,?,?,?,?)",
                          (run, net, "block:" + v["name"], json.dumps(out.value, ensure_ascii=False), holder["client"].model, now_iso()))
            st.db.commit()
            return {"data": out.value, "model": holder["client"].model}
        finally:
            if holder.get("store"):
                holder["store"].close()

    @app.post("/block/tz", response_class=HTMLResponse)
    async def block_tz(request: Request, db=Depends(get_db)):
        """ТЗ в разработку по непройденным проверкам блока (ИИ, текст для Kaiten / Яндекс Трекера). Статусы не меняет."""
        form = await request.form()
        name, net, run = form.get("name") or "", form.get("net") or "", form.get("run") or ""
        ctx = block_ctx(db, name, net, form.get("p") or "30", form.get("from") or "", form.get("to") or "")
        v = ctx["v"]
        out = await run_in_threadpool(block_tz_work, db_path=request.app.state.db_path, v=v, net=ctx["net"], run=run or v["run_id"])
        ctx["tz"], ctx["tz_error"], ctx["tz_model"] = out.get("text"), out.get("error"), out.get("model")
        return templates.TemplateResponse(request, "_block_tz.html", ctx)

    def tz_payload(v: dict, net: str, items_by: dict) -> dict:
        """Данные непройденных проверок блока для ТЗ и для чата под ТЗ."""
        rows = [r for r in v["rows"] if r["status"] in ("fail", "warn")][:12]
        return {
            "site_network": net, "block": v["name"], "block_meaning": v["help"],
            "checks": [{"title": r["title"], "priority": r["priority"], "status": r["status"], "summary": r["summary"],
                        "criterion": (r.get("criterion") or "")[:400],
                        "failed_url_examples": [{"url": i["url"], "problem": str(i["msg"])[:200]} for i in
                                                [i for i in (items_by.get(r["check_id"]) or {}).get("items", []) if not i.get("ok")][:5]]}
                       for r in rows],
        }

    def block_tz_work(*, db_path, v: dict, net: str, run: str) -> dict:
        from .. import integrations
        from ..ai import AIClient
        from ..storage import Store, now_iso
        from ..tz_prompt import TZ_SYSTEM, TZ_USER
        con = connect(db_path)
        try:
            items_by = results_db(con, run, net).get(net) or {}
        finally:
            con.close()
        payload = tz_payload(v, net, items_by)
        if not payload["checks"]:
            return {"error": "В блоке нет непройденных проверок — ТЗ писать не по чему."}
        user = TZ_USER + json.dumps(payload, ensure_ascii=False)
        holder: dict = {}

        def work(key: str):
            store = Store(settings.data_dir)
            holder["store"] = store
            client = AIClient(store, key, verify=settings.verify_tls)
            holder["client"] = client
            try:
                return client.chat(TZ_SYSTEM, user, version="tz-v1", max_tokens=3500)
            finally:
                client.close()

        out = integrations.guarded(settings, "ai", work)
        try:
            if out.status != "ok":
                msg = out.reason or "ИИ недоступен"
                if out.status == "skipped":
                    msg = "ИИ не подключён: " + msg
                return {"error": msg}
            st = holder["store"]
            kind = "tz:" + v["name"]
            st.db.execute("DELETE FROM ai_notes WHERE run_id=? AND network=? AND kind=?", (run, net, kind))
            st.db.execute("INSERT INTO ai_notes(run_id,network,kind,data,model,created_at) VALUES (?,?,?,?,?,?)",
                          (run, net, kind, json.dumps({"text": out.value}, ensure_ascii=False), holder["client"].model, now_iso()))
            st.db.commit()
            return {"text": out.value, "model": holder["client"].model}
        finally:
            if holder.get("store"):
                holder["store"].close()

    @app.post("/block/chat", response_class=HTMLResponse)
    async def block_chat(request: Request, db=Depends(get_db)):
        """Чат под ТЗ: вопрос к ИИ или правка текста ТЗ. Если ИИ вернул новый ТЗ — подменяем поле, статусы проверок не трогаем."""
        form = await request.form()
        name, net, run = form.get("name") or "", form.get("net") or "", form.get("run") or ""
        msg = (form.get("msg") or "").strip()[:tzc.MAX_USER_CHARS]
        tz_now = form.get("tz_current") or ""
        ctx = block_ctx(db, name, net, form.get("p") or "30", form.get("from") or "", form.get("to") or "")
        v = ctx["v"]
        run = run or v["run_id"]
        if not msg:
            return templates.TemplateResponse(request, "_block_chat.html", ctx)
        out = await run_in_threadpool(block_chat_work, db_path=request.app.state.db_path, v=v, net=ctx["net"], run=run,
                                      msg=msg, tz_now=tz_now, history=ctx["chat"])
        ctx["chat"] = out["chat"]
        ctx["chat_error"] = out.get("error")
        if out.get("tz"):
            ctx["tz"], ctx["tz_model"], ctx["tz_updated"] = out["tz"], out.get("model"), True
        return templates.TemplateResponse(request, "_block_chat.html", ctx)

    @app.post("/block/chat/clear", response_class=HTMLResponse)
    async def block_chat_clear(request: Request, db=Depends(get_db)):
        form = await request.form()
        name, net = form.get("name") or "", form.get("net") or ""
        ctx = block_ctx(db, name, net, form.get("p") or "30", form.get("from") or "", form.get("to") or "")
        run = form.get("run") or ctx["v"]["run_id"]
        if run:
            con = connect(request.app.state.db_path)
            try:
                with tx(con):
                    con.execute("DELETE FROM ai_notes WHERE run_id=? AND network=? AND kind=?", (run, ctx["net"], "chat:" + ctx["v"]["name"]))
            finally:
                con.close()
        ctx["chat"] = []
        return templates.TemplateResponse(request, "_block_chat.html", ctx)

    def block_chat_work(*, db_path, v: dict, net: str, run: str, msg: str, tz_now: str, history: list) -> dict:
        from .. import integrations
        from ..ai import AIClient
        from ..storage import Store, now_iso
        from ..tz_prompt import TZ_CHAT_RULES, TZ_SYSTEM
        con = connect(db_path)
        try:
            items_by = results_db(con, run, net).get(net) or {}
        finally:
            con.close()
        payload = tz_payload(v, net, items_by)
        messages = tzc.build_messages(payload, history, tz_now, msg)
        holder: dict = {}

        def work(key: str):
            store = Store(settings.data_dir)
            holder["store"] = store
            client = AIClient(store, key, verify=settings.verify_tls)
            holder["client"] = client
            try:
                return client.chat_messages(TZ_SYSTEM + TZ_CHAT_RULES, messages, max_tokens=4000)
            finally:
                client.close()

        out = integrations.guarded(settings, "ai", work)
        try:
            if out.status != "ok":
                reason = out.reason or "ИИ недоступен"
                if out.status == "skipped":
                    reason = "ИИ не подключён: " + reason
                # реплику пользователя не теряем, чтобы можно было повторить; ответ ИИ не сохраняется
                return {"chat": history + [{"role": "user", "content": msg, "tz": False, "err": False}], "error": reason}
            say, new_tz = tzc.parse_reply(out.value)
            model = holder["client"].model
            chat = tzc.clean_history(history + [{"role": "user", "content": msg},
                                                {"role": "assistant", "content": say, "tz": bool(new_tz)}])
            st = holder["store"]
            st.db.execute("DELETE FROM ai_notes WHERE run_id=? AND network=? AND kind=?", (run, net, "chat:" + v["name"]))
            st.db.execute("INSERT INTO ai_notes(run_id,network,kind,data,model,created_at) VALUES (?,?,?,?,?,?)",
                          (run, net, "chat:" + v["name"], json.dumps({"messages": chat}, ensure_ascii=False), model, now_iso()))
            if new_tz:
                st.db.execute("DELETE FROM ai_notes WHERE run_id=? AND network=? AND kind=?", (run, net, "tz:" + v["name"]))
                st.db.execute("INSERT INTO ai_notes(run_id,network,kind,data,model,created_at) VALUES (?,?,?,?,?,?)",
                              (run, net, "tz:" + v["name"], json.dumps({"text": new_tz}, ensure_ascii=False), model, now_iso()))
            st.db.commit()
            return {"chat": chat, "tz": new_tz, "model": model}
        finally:
            if holder.get("store"):
                holder["store"].close()

    @app.get("/api/board")
    def api_board(net: str = "", p: str = "30", date_from: str = Query("", alias="from"),
                  date_to: str = Query("", alias="to"), db=Depends(get_db)):
        f, t = period_of(p, date_from, date_to)
        return JSONResponse(mx.board(db, (None if net in ("", "all") else net), f, t))

    @app.get("/api/dashboard")
    def api_dashboard(net: str = "", n: int = 30, db=Depends(get_db)):
        return JSONResponse(mx.dashboard(db, net or None, max(5, min(n, 200))))

    @app.get("/api/series")
    def api_series(metrics: str, net: str = "", n: int = 30, db=Depends(get_db)):
        d = mx.dashboard(db, net or None, max(5, min(n, 200)))
        want = [m for m in metrics.split(",") if m]
        return {"runs": d["runs"], "series": {m: d["series"].get(m) for m in want}}

    # ================================================================== проверки (аккордеон по блокам)

    @app.get("/checks", response_class=HTMLResponse)
    def checks(request: Request, net: str = "", run: str = "", s: str = "", db=Depends(get_db)):
        nets = networks_list(db)
        net = net or (nets[0] if nets else "")
        view = mx.checks_view(db, net, run or None) if net else {"run_id": None, "runs": [], "blocks": [], "changes": [], "totals": {}}
        notes = notes_db(db, view["run_id"]) if view["run_id"] else {}
        return render(request, "checks.html", "checks", net=net, networks=nets, v=view, s=s, notes=(notes.get(net) or {}),
                      files=run_files(view["run_id"]) if view["run_id"] else {})

    def detail_ctx(db, net: str, run: str, check: str, show_all: bool) -> dict:
        if check == "crawl_overview":
            show_all = True
        d = mx.check_detail(db, run, net, check)
        if not d:
            raise HTTPException(404, "нет данных")
        info = bl.check_index().get(check) or {}
        r = db.execute("SELECT items FROM check_results WHERE run_id=? AND network=? AND check_id=?", (run, net, check)).fetchone()
        items = json.loads(r["items"] or "[]") if r else []
        shown = items if show_all else [i for i in items if not i.get("ok")]
        notes = notes_db(db, run).get(net) or {}
        ai_comment = ((notes.get("analysis") or {}).get("comments") or {}).get(check)
        ai_desc = (notes.get("descriptions") or {}).get("pages") if check == "text_descriptions" else None
        return {"d": d, "history": mx.check_history(db, net, check), "info": info, "items": shown[:400], "all_items": len(items),
                "show_all": show_all, "cond": ev.run_conditions(db, settings, run, net), "ai_comment": ai_comment,
                "ai_desc": ai_desc, "ai_model": notes.get("model")}

    @app.get("/checks/detail", response_class=HTMLResponse)
    def check_detail(request: Request, net: str, run: str, check: str, all: int = 0, db=Depends(get_db)):
        return templates.TemplateResponse(request, "_check_detail.html", detail_ctx(db, net, run, check, bool(all)))

    @app.get("/checks/evidence", response_class=HTMLResponse)
    def check_evidence(request: Request, net: str, run: str, url: str, db=Depends(get_db)):
        return templates.TemplateResponse(request, "_evidence.html", {"e": ev.for_url(db, settings, run, net, url)})

    # ================================================================== выгрузка отчётов

    def pick_run(db, run: str, net: str) -> str:
        rid = run or latest_checked_run(db, net)
        if not rid:
            raise HTTPException(404, "Нет запусков с проверками: сначала запустите сбор с проверками")
        return rid

    @app.get("/export/report.html")
    def export_report(run: str = "", net: str = "", inline: int = 0, db=Depends(get_db)):
        from ..checks import report
        rid = pick_run(db, run, net)
        results = results_db(db, rid, net)
        if not results:
            raise HTTPException(404, "Для этого запуска проверок нет")
        html = report.build_html(rid, bl.checklist_rows(), results,
                                 {"note": "выборка страниц по справочнику" + (", краул Screaming Frog учтён" if any(
                                     (x.get("source") == "crawl") for r in results.values() for x in r.values()) else "")},
                                 notes_db(db, rid))
        name = f"techaudit-{rid}{'-' + net if net else ''}.html"
        headers = {} if inline else {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}
        return HTMLResponse(html, headers=headers)

    @app.get("/export/checklist.xlsx")
    def export_xlsx(run: str = "", net: str = "", db=Depends(get_db)):
        from ..checks import checklist
        from ..config import ROOT
        src = checklist.find_checklist(ROOT)
        if not src:
            raise HTTPException(404, "Не найден xlsx чек-листа (CHECKLIST_XLSX в .env)")
        rid = pick_run(db, run, net)
        results = results_db(db, rid, net)
        dst = reports_dir / f"{rid}{'-' + net if net else ''}-checklist.xlsx"
        checklist.write_xlsx(src, dst, results, rid, notes_db(db, rid))
        return FileResponse(dst, filename=dst.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/export/checks.csv")
    def export_checks_csv(run: str = "", net: str = "", urls: int = 0, db=Depends(get_db)):
        import csv
        import io
        rid = pick_run(db, run, net)
        idx = bl.check_index()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        if urls:
            w.writerow(["Сеть", "Блок", "Проверка", "Статус", "URL", "Результат по URL", "Тип страницы"])
        else:
            w.writerow(["Сеть", "Блок", "Проверка", "Приоритет", "Статус", "Результат", "Источник"])
        labels = {"pass": "пройдено", "warn": "требует внимания", "fail": "не пройдено", "skipped": "пропущено", "na": "нет данных"}
        for n, res in results_db(db, rid, net).items():
            for cid, x in res.items():
                i = idx.get(cid) or {}
                if urls:
                    for it in x["items"]:
                        if not it.get("ok"):
                            w.writerow([n, i.get("block", ""), i.get("title", cid), "не пройдено", it["url"], it["msg"], it.get("kind", "")])
                else:
                    w.writerow([n, i.get("block", ""), i.get("title", cid), i.get("priority", ""), labels.get(x["status"], x["status"]),
                                x["summary"], x["source"]])
        name = f"checks-{'urls-' if urls else ''}{rid}.csv"
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    def csv_response(rows: list, header: list, name: str) -> Response:
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(header)
        w.writerows(rows)
        return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/export/urls.csv")
    def export_urls(run: str = "", net: str = "", check: str = "", block: str = "", only: str = "fail", statuses: str = "", db=Depends(get_db)):
        """Список URL по проверкам: only=fail (не прошли) | pass (прошли) | all. Можно сузить до одной проверки или блока."""
        rid = pick_run(db, run, net)
        idx = bl.check_index()
        out = []
        for n, res in results_db(db, rid, net).items():
            for cid, x in res.items():
                if statuses and x["status"] not in statuses.split(","):
                    continue
                info = idx.get(cid) or {}
                if check and cid != check:
                    continue
                if block and (info.get("block") or "Прочее") != block:
                    continue
                for it in x["items"]:
                    if only == "fail" and it.get("ok"):
                        continue
                    if only == "pass" and not it.get("ok"):
                        continue
                    out.append([n, info.get("block", ""), info.get("title", cid), "прошёл" if it.get("ok") else "не прошёл",
                                it.get("url", ""), it.get("msg", ""), it.get("kind", "")])
        tag = {"fail": "problem", "pass": "ok", "all": "all"}.get(only, only)
        return csv_response(out, ["Сеть", "Блок", "Проверка", "Результат", "URL", "Комментарий", "Тип страницы"],
                            f"urls-{check or block or 'all'}-{tag}-{rid}.csv")

    @app.get("/export/tests.csv")
    def export_tests(run: str = "", net: str = "", kind: str = "", block: str = "", verdict: str = "fail", db=Depends(get_db)):
        """Тесты адресов (искажённые URL, robots, sitemap, иконки): verdict=fail | pass | all; kind — один вид, block — блок."""
        from .. import explain
        rid = pick_run(db, run, net)
        kinds = [kind] if kind else [k for k, b in bl._PROBE_BLOCK.items() if not block or b == block]
        q = f"SELECT * FROM probes WHERE run_id=?{' AND network=?' if net else ''} AND kind IN ({','.join('?' * len(kinds))})"
        if verdict == "fail":
            q += " AND (verdict IS NULL OR verdict!='pass')"
        elif verdict == "pass":
            q += " AND verdict='pass'"
        out = []
        for r in db.execute(q + " ORDER BY network, kind, id", [rid, *([net] if net else []), *kinds]):
            out.append([r["network"], explain.probe_name(r["kind"]), r["page_type"] or "", r["source_url"] or "", r["url"], r["expected"] or "",
                        r["status"] if r["status"] is not None else "", r["final_url"] or "", "прошёл" if r["verdict"] == "pass" else "не прошёл",
                        r["reason"] or ""])
        return csv_response(out, ["Сеть", "Вид теста", "Тип страницы", "Исходный URL", "Тестовый URL", "Ожидалось", "Статус ответа",
                                  "Конечный URL", "Результат", "Причина"], f"address-tests-{kind or block or 'all'}-{verdict}-{rid}.csv")

    @app.get("/export/pages.csv")
    def export_pages(run: str = "", net: str = "", flt: str = "err", db=Depends(get_db)):
        """Страницы запуска: flt=err — только с ошибкой/4xx/5xx/подозрительным ответом, all — все."""
        rid = pick_run(db, run, net)
        q = f"SELECT * FROM pages WHERE run_id=?{' AND network=?' if net else ''}"
        if flt == "err":
            q += " AND (status>=400 OR (error IS NOT NULL AND error!='') OR (suspect IS NOT NULL AND suspect!=''))"
        out = [[r["network"], r["page_type"], r["variant"] or "", r["source_url"], r["status"] if r["status"] is not None else "",
                r["final_url"] or "", r["error"] or "", r["suspect"] or "", round(r["ttfb_ms"]) if r["ttfb_ms"] is not None else ""]
               for r in db.execute(q + " ORDER BY network, page_type, id", [rid, *([net] if net else [])])]
        return csv_response(out, ["Сеть", "Тип страницы", "Вариант", "URL", "Статус", "Конечный URL", "Ошибка", "Подозрение", "TTFB, мс"],
                            f"pages-{flt}-{rid}.csv")

    @app.get("/export/metric-urls.csv")
    def export_metric_urls(metric: str, point: str, net: str = "", db=Depends(get_db)):
        from ..metric_exports import CRAWL_SELECTIONS, crawl_selection
        network = "" if net in ("", "all") else net
        if point.startswith("crawl:") and metric in CRAWL_SELECTIONS:
            try:
                imp_id = int(point[6:])
            except ValueError:
                raise HTTPException(400, "некорректная точка краула")
            imp = db.execute("SELECT * FROM crawl_imports WHERE id=?", (imp_id,)).fetchone()
            if not imp or (network and imp["network"] != network):
                raise HTTPException(404, "краул выбранной сети не найден")
            title = cr.CATALOG[metric][0] if metric in cr.CATALOG else "Все проблемные URL краула"
            selection = {"crawl_2xx_share": "HTML-URL без ответа 2xx", "crawl_indexable_share": "HTML с ответом 200, не отмеченный Indexable",
                         "crawl_resp_p95_ms": "Все URL замеров, по которым рассчитан p95 (без порога ошибки)",
                         "crawl_resp_avg_ms": "Все URL замеров среднего времени ответа (без порога ошибки)"}.get(metric, title)
            rows = ([imp["network"], imp_id, imp["taken_at"], title, r["selection_reasons"] or selection, r["address"], r["status"], r["indexability"],
                     r["indexability_status"], r["title"], r["meta_desc"], r["h1"], r["h1_2"], r["canonical"],
                     r["inlinks"], r["depth"], r["response_ms"], r["redirect_url"]]
                    for r in crawl_selection(db, imp_id, metric))
            return csv_response(rows, ["Сеть", "ID краула", "Дата краула", "Показатель", "Причина / критерий выборки", "URL", "HTTP-статус", "Индексируемость",
                                      "Причина индексируемости", "Title", "Description", "H1", "H1 (2)", "Canonical", "Inlinks", "Глубина", "Время ответа, мс", "Редирект на"],
                                f"{metric}-crawl-{imp_id}.csv")
        page_filters = {
            "pages_http_err": "status BETWEEN 400 AND 599",
            "pages_net_err": "COALESCE(error,'')!=''",
            "pages_suspect": "COALESCE(suspect,'')!=''",
            "pages_ok_share": "status IS NULL OR status NOT BETWEEN 200 AND 299 OR COALESCE(error,'')!=''",
        }
        if point.startswith("run:") and metric in page_filters:
            rid = point[4:]
            if not db.execute("SELECT 1 FROM runs WHERE run_id=?", (rid,)).fetchone():
                raise HTTPException(404, "запуск не найден")
            q = "SELECT * FROM pages WHERE run_id=?" + (" AND network=?" if network else "")
            rows = ([r["network"], rid, metric, r["url"], r["variant"], r["status"], r["error"], r["suspect"]]
                    for r in db.execute(q + f" AND ({page_filters[metric]}) ORDER BY network,url,variant", [rid] + ([network] if network else [])))
            return csv_response(rows, ["Сеть", "Запуск", "Показатель", "URL", "Вариант", "HTTP-статус", "Ошибка", "Подозрение"], f"{metric}-{rid}.csv")
        raise HTTPException(400, "для этой метрики нет выборки URL или выбран неверный тип точки")

    @app.get("/export/metrics.csv")
    def export_metrics(net: str = "", p: str = "0", date_from: str = Query("", alias="from"),
                       date_to: str = Query("", alias="to"), db=Depends(get_db)):
        f, t = period_of(p, date_from, date_to)
        data = mx.board(db, (None if net in ("", "all") else net), f, t)
        name = f"metrics-{net or 'all'}-{datetime.now():%Y%m%d}.csv"
        return Response("﻿" + mx.board_csv(data), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    # ================================================================== краулы Screaming Frog

    def crawl_max_mb() -> int:
        return int(os.environ.get("CRAWL_MAX_MB") or 1500)  # меняется в «Доступ и настройки» без перезапуска

    def net_host(net: str) -> str | None:
        cfg = settings.networks.get(net)
        return urlsplit(cfg.url).hostname if cfg and cfg.url else None

    @app.get("/crawl", response_class=HTMLResponse)
    def crawl_list(request: Request, net: str = "", db=Depends(get_db)):
        cr.ensure_schema(db)
        imps = cr.imports(db, net or None)
        for i in imps:
            i["m"] = cr.metrics(db, i["id"])
        return render(request, "crawl.html", "crawl", net=net, networks=networks_list(db), imps=imps, cat=cr.CATALOG,
                      max_mb=crawl_max_mb(), no_url=[n for n in settings.networks if not net_host(n)])

    @app.post("/crawl/upload")
    async def crawl_upload(request: Request):
        try:
            if int(request.headers.get("content-length") or 0) > crawl_max_mb() * 1024 * 1024:
                return redirect("/crawl", err=f"Файл больше {crawl_max_mb()} МБ (лимит меняется в «Доступ и настройки → Прочее»)")
        except ValueError:
            pass
        form = await request.form()
        net = (form.get("network") or "").strip()
        up = form.get("file")
        if not net:
            return redirect("/crawl", err="Выберите сеть")
        if up is None or not getattr(up, "filename", ""):
            return redirect(f"/crawl?net={quote(net)}", err="Выберите CSV-файл из Screaming Frog")
        label, fname, host = (form.get("label") or "").strip()[:120], up.filename, net_host(net)

        def work():
            db = connect(db_path)
            try:
                return cr.import_csv(db, net, up.file, label=label, source_file=fname, host=host)
            finally:
                db.close()
        try:
            res = await run_in_threadpool(work)
        except (cr.CrawlError, ValueError, UnicodeError) as e:
            return redirect(f"/crawl?net={quote(net)}", err=f"Краул не загружен: {e}")
        msg = f"Загружено {res['rows']} URL ({res['html_rows']} HTML), краул от {fmt_dt(res['taken_at'])}"
        if res.get("skipped_external"):
            msg += f"; чужих адресов пропущено: {res['skipped_external']}"
        if res.get("missing_columns"):
            msg += "; нет колонок: " + ", ".join(res["missing_columns"][:6])
        return redirect(f"/crawl/{res['import_id']}", msg=msg)

    @app.get("/crawl/{imp_id:int}", response_class=HTMLResponse)
    def crawl_detail(request: Request, imp_id: int, flt: str = "all", q: str = "", page: int = 1, vs: int = 0, db=Depends(get_db)):
        cr.ensure_schema(db)
        imp = db.execute("SELECT * FROM crawl_imports WHERE id=?", (imp_id,)).fetchone()
        if not imp:
            raise HTTPException(404, "выгрузка не найдена")
        imp = dict(imp)
        per, page = 100, max(1, page)
        flt = flt if flt in cr.FILTERS else "all"
        rows, total = cr.rows(db, imp_id, flt, q.strip() or None, per, (page - 1) * per)
        others = [i for i in cr.imports(db, imp["network"]) if i["id"] != imp_id]
        d = cr.diff(db, vs, imp_id) if vs and any(o["id"] == vs for o in others) else None
        counts = {k: db.execute(f"SELECT COUNT(*) FROM crawl_pages WHERE import_id=? AND {v[0]}", (imp_id,)).fetchone()[0]
                  for k, v in cr.FILTERS.items()}
        return render(request, "crawl_detail.html", "crawl", imp=imp, m=cr.metrics(db, imp_id), cat=cr.CATALOG,
                      dist=cr.status_distribution(db, imp_id), rows=rows, total=total, page=page, pages=max(1, -(-total // per)),
                      flt=flt, q=q, filters=cr.FILTERS, counts=counts, others=others, vs=vs, diff=d)

    @app.post("/crawl/{imp_id:int}/delete")
    def crawl_delete(imp_id: int, db=Depends(get_db)):
        cr.ensure_schema(db)
        r = db.execute("SELECT network FROM crawl_imports WHERE id=?", (imp_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        cr.delete_import(db, imp_id)
        return redirect(f"/crawl?net={quote(r['network'])}", msg="Выгрузка удалена")

    @app.get("/crawl/{imp_id:int}/export.csv")
    def crawl_export(imp_id: int, flt: str = "all", db=Depends(get_db)):
        import csv
        import io
        flt = flt if flt in cr.FILTERS else "all"
        rows, _ = cr.rows(db, imp_id, flt, None, 200000, 0)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Address", "Status", "Indexability", "Title", "H1", "Canonical", "Inlinks", "Depth", "Response ms", "Redirect URL"])
        for r in rows:
            w.writerow([r["address"], r["status"], r["indexability"], r["title"], r["h1"], r["canonical"], r["inlinks"],
                        r["depth"], r["response_ms"], r["redirect_url"]])
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=crawl-{imp_id}-{flt}.csv"})

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


    # ================================================================== ИИ: что делает, статус, запуск

    @app.get("/ai", response_class=HTMLResponse)
    def ai_page(request: Request, net: str = "", run: str = "", db=Depends(get_db)):
        nets = networks_list(db)
        net = net or (nets[0] if nets else "")
        runs_ = mx.run_ids_with_checks(db, net) if net else []
        run = run if any(r["run_id"] == run for r in runs_) else (runs_[0]["run_id"] if runs_ else "")
        notes = (notes_db(db, run).get(net) or {}) if run else {}
        e = os.environ
        cfg = {"key": bool(settings.integrations.get("ai")), "model": e.get("AI_MODEL") or "x5-airun-medium",
               "host": urlsplit(e.get("AI_API_URL") or "https://api-copilot.x5.ru/v1").hostname,
               "max_requests": e.get("AI_MAX_REQUESTS") or "20", "concurrency": e.get("AI_CONCURRENCY") or "2"}
        try:
            cache = db.execute("SELECT COUNT(*) FROM ai_cache").fetchone()[0]
        except sqlite3.OperationalError:
            cache = 0
        last_ai = None
        if run:
            for j in db.execute("SELECT id, status, params, finished_at FROM web_jobs WHERE params LIKE '%\"kind\": \"ai\"%' "
                                "ORDER BY id DESC LIMIT 20"):
                p_ = json.loads(j["params"])
                if p_.get("run_id") == run and (not p_.get("networks") or net in p_["networks"]):
                    reason = ""
                    lg = settings.data_dir / "logs" / f"job-{j['id']}.log"
                    if lg.exists():
                        for line in lg.read_text(encoding="utf-8", errors="replace").splitlines():
                            if "ИИ" in line and ("error" in line or "skipped" in line):
                                reason = line.strip()
                    hint = ""
                    if "getaddrinfo" in reason or "ConnectError" in reason or "timed out" in reason.lower():
                        hint = ("Сервис ИИ недоступен из сети этого компьютера (не находится адрес или нет соединения). "
                                "Проверьте корпоративный VPN/прокси и значение AI_API_URL в .env.")
                    last_ai = {"id": j["id"], "status": j["status"], "reason": reason, "hint": hint, "at": j["finished_at"]}
                    break
        return render(request, "ai.html", "ai", last_ai=last_ai, net=net, networks=nets, runs=runs_, run=run, notes=notes, cfg=cfg, cache=cache,
                      titles={c: i["title"] for c, i in bl.check_index().items()})

    @app.post("/ai/start")
    async def ai_start(request: Request):
        form = await request.form()
        run, net = form.get("run") or "", form.get("net") or ""
        if not run:
            return redirect("/ai", err="Нет запуска с проверками")
        try:
            jid = manager.enqueue({"kind": "ai", "run_id": run, "networks": [net] if net else []})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return redirect(f"/ai?net={quote(net)}&run={quote(run)}", err=f"Не удалось поставить в очередь: {type(e).__name__}: {e}")
        return redirect(f"/jobs/{jid}", msg="ИИ-анализ поставлен в очередь")

    # ================================================================== справочник URL

    @app.get("/registry", response_class=HTMLResponse)
    def registry(request: Request, site: str = "", type: str = "", status: str = "", q: str = "", robots: str = "",
                 page: int = 1, db=Depends(get_db)):
        per = 100
        page = max(1, page)
        rows, total = reg.list_urls(db, site=site or None, page_type=type or None, status=status or None,
                                    q=q.strip() or None, robots=robots or None, limit=per,
                                    offset=(page - 1) * per)
        bases = {}
        for r in rows:
            bases.setdefault(r["site"], site_base(db, r["site"]))
        return render(request, "registry.html", "registry", rows=rows, total=total, page=page, pages=max(1, -(-total // per)),
                      f={"site": site, "type": type, "status": status, "q": q, "robots": robots}, bases=bases,
                      sites=reg.entity_rows(db, "sites"), types=reg.entity_rows(db, "types"), counts=reg.counts(db),
                      back=str(request.url.path) + ("?" + request.url.query if request.url.query else ""))

    def row_ctx(db, row_id: int) -> dict:
        row = reg.get_url(db, row_id)
        if not row:
            raise HTTPException(404, "запись не найдена")
        return {"r": row, "bases": {row["site"]: site_base(db, row["site"])}}

    @app.get("/registry/{row_id:int}/row", response_class=HTMLResponse)
    def url_row(request: Request, row_id: int, db=Depends(get_db)):
        return templates.TemplateResponse(request, "_url_row.html", row_ctx(db, row_id))

    @app.get("/registry/{row_id:int}/edit", response_class=HTMLResponse)
    def url_edit(request: Request, row_id: int, db=Depends(get_db)):
        return templates.TemplateResponse(request, "_url_edit.html", {
            **row_ctx(db, row_id), "sites": reg.entity_rows(db, "sites"), "types": reg.entity_rows(db, "types"), "error": ""})

    @app.post("/registry/{row_id:int}/save", response_class=HTMLResponse)
    async def url_save(request: Request, row_id: int, db=Depends(get_db)):
        form = await request.form()
        data = {k: (form.get(k) or "") for k in ("site", "page_type", "url", "note", "status")}
        data["noindex"] = 1 if form.get("noindex") else 0
        data["nofollow"] = 1 if form.get("nofollow") else 0
        try:
            reg.update_url(db, row_id, **data)
        except ValueError as e:
            ctx = row_ctx(db, row_id)
            ctx["r"] = {**ctx["r"], **data}
            return templates.TemplateResponse(request, "_url_edit.html", {
                **ctx, "sites": reg.entity_rows(db, "sites"), "types": reg.entity_rows(db, "types"), "error": str(e)})
        mirror(db)
        return templates.TemplateResponse(request, "_url_row.html", row_ctx(db, row_id))

    @app.delete("/registry/{row_id:int}")
    def url_delete(row_id: int, db=Depends(get_db)):
        reg.delete_urls(db, [row_id])
        mirror(db)
        return Response("")

    @app.post("/registry/add")
    async def url_add(request: Request, db=Depends(get_db)):
        form = await request.form()
        back = safe_back(form.get("back"), "/registry")
        try:
            reg.add_url(db, form.get("site", ""), form.get("page_type", ""), form.get("url", ""),
                        form.get("note", ""), form.get("status", "active"),
                        1 if form.get("noindex") else 0, 1 if form.get("nofollow") else 0)
        except ValueError as e:
            return redirect(back, err=f"Не добавлено: {e}")
        mirror(db)
        return redirect(back, msg="URL добавлен")

    @app.post("/registry/bulk")
    async def url_bulk(request: Request, db=Depends(get_db)):
        form = await request.form()
        back = safe_back(form.get("back"), "/registry")
        ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
        action = form.get("action")
        if not ids:
            return redirect(back, err="Ничего не выбрано")
        if action == "delete":
            n = reg.delete_urls(db, ids)
            msg = f"Удалено: {n}"
        elif action in ("active", "gone"):
            n = reg.set_status(db, ids, action)
            msg = f"Статус «{reg.STATUS_LABELS[action]}» у {n} URL"
        elif action in ("noindex_on", "noindex_off", "nofollow_on", "nofollow_off"):
            field, _, st = action.partition("_")
            n = reg.set_flag(db, ids, field, 1 if st == "on" else 0)
            msg = f"{field}: {'включён' if st == 'on' else 'снят'} у {n} URL"
        else:
            return redirect(back, err="Неизвестное действие")
        mirror(db)
        return redirect(back, msg=msg)

    @app.get("/registry/export.csv")
    def url_export(site: str = "", db=Depends(get_db)):
        text = "﻿" + reg.export_csv(db, site or None)
        name = f"registry-{site or 'all'}-{datetime.now():%Y%m%d}.csv"
        return Response(text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/registry/template.csv")
    def url_template():
        return Response("﻿" + reg.CSV_TEMPLATE, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=registry-template.csv"})

    # ---- массовый импорт CSV (URL и малые справочники): предпросмотр → применение

    @app.get("/import", response_class=HTMLResponse)
    def import_form(request: Request, entity: str = "urls", db=Depends(get_db)):
        return render(request, "import.html", "registry", entity=entity if entity in ("urls", *reg.ENTITIES) else "urls",
                      sites=reg.entity_rows(db, "sites"), entities=reg.ENTITIES)

    async def read_import(form) -> tuple[str, dict]:
        entity = form.get("entity") or "urls"
        if entity != "urls" and entity not in reg.ENTITIES:
            raise ValueError("неизвестный справочник")
        opts = {"entity": entity, "mode": form.get("mode") or "add", "default_site": form.get("default_site") or "",
                "create_types": form.get("create_types") == "1"}
        up = form.get("file")
        if up is not None and hasattr(up, "read") and getattr(up, "filename", ""):
            text = decode_upload(await up.read())
        else:
            text = (form.get("text") or "").strip("\r\n")
            if not text.strip():
                raise ValueError("выберите CSV-файл или вставьте текст")
            text = decode_upload(text.encode("utf-8"))
        return text, opts

    def run_import(db, text: str, o: dict, dry: bool) -> dict:
        if o["entity"] == "urls":
            return reg.import_urls(db, text, mode=o["mode"], default_site=o["default_site"] or None,
                                   create_types=o["create_types"], dry_run=dry)
        return reg.import_entity(db, o["entity"], text, dry_run=dry)

    @app.post("/import/preview", response_class=HTMLResponse)
    async def import_preview(request: Request, db=Depends(get_db)):
        form = await request.form()
        try:
            text, o = await read_import(form)
            rep = run_import(db, text, o, True)
        except ValueError as e:
            return redirect(f"/import?entity={form.get('entity') or 'urls'}", err=str(e))
        return render(request, "import_preview.html", "registry", rep=rep, o=o, text=text,
                      title=("URL справочника" if o["entity"] == "urls" else reg.ENTITIES[o["entity"]]["title"]))

    @app.post("/import/apply")
    async def import_apply(request: Request, db=Depends(get_db)):
        form = await request.form()
        try:
            text, o = await read_import(form)
            rep = run_import(db, text, o, False)
        except ValueError as e:
            return redirect("/import", err=str(e))
        mirror(db)
        msg = f"Импорт выполнен: добавлено {rep['added']}, обновлено {rep['updated']}"
        if rep.get("deleted"):
            msg += f", удалено {rep['deleted']}"
        if rep["errors"]:
            msg += f"; строк с ошибками пропущено: {len(rep['errors'])}"
        return redirect("/registry" if o["entity"] == "urls" else f"/reference?tab={o['entity']}", msg=msg)

    # ================================================================== сети / типы / ответственные

    @app.get("/reference", response_class=HTMLResponse)
    def reference(request: Request, tab: str = "sites", q: str = "", db=Depends(get_db)):
        if tab not in (*reg.ENTITIES, "log"):
            tab = "sites"
        ctx = {"tab": tab, "q": q, "entities": reg.ENTITIES, "counts": reg.counts(db)}
        if tab == "log":
            ctx["changes"] = reg.recent_changes(db, 200)
        else:
            ctx["rows"] = reg.entity_rows(db, tab, q.strip() or None)
            ctx["e"] = reg.ENTITIES[tab]
            ctx["types"] = reg.entity_rows(db, "types")
            usage = {}
            if tab == "sites":
                usage = {r[0]: r[1] for r in db.execute("SELECT site, COUNT(*) FROM ref_urls GROUP BY site")}
            elif tab == "types":
                usage = {r[0]: r[1] for r in db.execute("SELECT page_type, COUNT(*) FROM ref_urls GROUP BY page_type")}
            ctx["usage"] = usage
            ctx["show_usage"] = tab in ("sites", "types")
            # сети, в которых встречается тип страницы (название сети + число URL)
            nets: dict[str, list[str]] = {}
            if tab in ("types", "owners"):
                for pt, nm, cnt in db.execute(
                        "SELECT u.page_type, COALESCE(NULLIF(s.name_ru,''), u.site), COUNT(*) "
                        "FROM ref_urls u LEFT JOIN ref_sites s ON s.site=u.site "
                        "GROUP BY u.page_type, u.site ORDER BY u.page_type, 3 DESC"):
                    nets.setdefault(pt, []).append(f"{nm} ({cnt})")
            ctx["nets"] = {k: ", ".join(v) for k, v in nets.items()}
            ctx["show_nets"] = tab in ("types", "owners")
        return render(request, "reference.html", "reference", **ctx)

    def ent_row_id(name: str, row: dict) -> str:
        return str(row["id"]) if name == "owners" else str(row[reg.ENTITIES[name]["key"][0]])

    @app.post("/reference/{name}/add")
    async def ref_add(request: Request, name: str, db=Depends(get_db)):
        if name not in reg.ENTITIES:
            raise HTTPException(404)
        form = await request.form()
        try:
            reg.entity_add(db, name, dict(form))
        except ValueError as e:
            return redirect(f"/reference?tab={name}", err=f"Не добавлено: {e}")
        mirror(db)
        return redirect(f"/reference?tab={name}", msg="Добавлено")

    @app.post("/reference/{name}/{row_id}/save")
    async def ref_save(request: Request, name: str, row_id: str, db=Depends(get_db)):
        if name not in reg.ENTITIES:
            raise HTTPException(404)
        form = await request.form()
        try:
            reg.entity_update(db, name, row_id, dict(form))
        except ValueError as e:
            return redirect(f"/reference?tab={name}", err=f"Не сохранено: {e}")
        mirror(db)
        return redirect(f"/reference?tab={name}", msg="Сохранено")

    @app.post("/reference/{name}/{row_id}/delete")
    async def ref_delete(name: str, row_id: str, db=Depends(get_db)):
        if name not in reg.ENTITIES:
            raise HTTPException(404)
        try:
            reg.entity_delete(db, name, row_id)
        except ValueError as e:
            return redirect(f"/reference?tab={name}", err=f"Не удалено: {e}")
        mirror(db)
        return redirect(f"/reference?tab={name}", msg="Удалено")

    # ================================================================== запуски

    def run_files(run_id: str) -> dict:
        out = {}
        for key, suffix in (("checks", "-checks.html"), ("xlsx", "-checklist.xlsx"), ("diff", "-diff.html")):
            if (reports_dir / f"{run_id}{suffix}").exists():
                out[key] = f"/reports/{quote(run_id)}{suffix}"
        return out

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, db=Depends(get_db)):
        runs_ = list(reversed(mx.list_runs(db, 40)))
        for r in runs_:
            r["files"] = run_files(r["run_id"])
        jobs_ = [dict(r) for r in db.execute("SELECT * FROM web_jobs ORDER BY id DESC LIMIT 15")]
        return render(request, "runs.html", "runs", runs=runs_, jobs=jobs_, networks=list(settings.networks),
                      defaults=jb.DEFAULT_PARAMS, ua_profiles=sorted(settings.ua_profiles),
                      ua_default=settings.ua_default)

    @app.post("/runs/start")
    async def runs_start(request: Request):
        form = await request.form()
        jid = manager.enqueue(params_from_form(form))
        return redirect(f"/jobs/{jid}", msg="Запуск поставлен в очередь")

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str, db=Depends(get_db)):
        run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "запуск не найден")
        nets = [r[0] for r in db.execute("SELECT DISTINCT network FROM pages WHERE run_id=? UNION "
                                         "SELECT DISTINCT network FROM check_results WHERE run_id=?", (run_id, run_id))]
        per_net = {n: mx.collect_metrics(db, [run_id], n)[run_id] for n in nets}
        types = {n: mx.by_page_type(db, run_id, n) for n in nets}
        return render(request, "run.html", "runs", run=dict(run), nets=nets, per_net=per_net, types=types,
                      files=run_files(run_id), catalog=mx.CATALOG,
                      options=json.dumps(json.loads(run["options"] or "{}"), ensure_ascii=False, indent=1)
                      if run["options"] else "")

    @app.get("/jobs/{job_id:int}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int, db=Depends(get_db)):
        job = db.execute("SELECT * FROM web_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "задание не найдено")
        j = dict(job)
        j["p"] = json.loads(j["params"])
        j["files"] = run_files(j["run_id"]) if j["run_id"] else {}
        return render(request, "job.html", "runs", job=j)

    @app.get("/jobs/{job_id:int}/log", response_class=HTMLResponse)
    def job_log(job_id: int, db=Depends(get_db)):
        job = db.execute("SELECT status, log_path FROM web_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404)
        text = ""
        if job["log_path"]:
            p = settings.data_dir / "logs" / Path(job["log_path"]).name
            if p.exists():
                raw = p.read_text(encoding="utf-8", errors="replace")
                text = raw[-60000:]
        from html import escape
        body = escape(text) or ("В очереди…" if job["status"] == "queued" else "Лог пока пуст")
        status = 286 if job["status"] in ("done", "failed", "cancelled") else 200  # 286 — htmx прекращает опрос
        return HTMLResponse(body, status_code=status, headers={"X-Job-Status": job["status"]})

    @app.get("/jobs/{job_id:int}/status", response_class=HTMLResponse)
    def job_status(request: Request, job_id: int, db=Depends(get_db)):
        job = db.execute("SELECT * FROM web_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404)
        j = dict(job)
        j["files"] = run_files(j["run_id"]) if j["run_id"] else {}
        status = 286 if j["status"] in ("done", "failed", "cancelled") else 200
        return templates.TemplateResponse(request, "_job_status.html", {"job": j}, status_code=status)

    @app.post("/jobs/{job_id:int}/cancel")
    def job_cancel(job_id: int):
        ok = manager.cancel(job_id)
        return redirect(f"/jobs/{job_id}", msg="Остановка запрошена" if ok else None, err=None if ok else "Нечего останавливать")

    # ================================================================== расписание

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules(request: Request, db=Depends(get_db)):
        rows = []
        for s in db.execute("SELECT * FROM web_schedules ORDER BY id"):
            d = dict(s)
            d["p"] = json.loads(d["params"])
            rows.append(d)
        return render(request, "schedules.html", "schedules", rows=rows, networks=list(settings.networks),
                      defaults=jb.DEFAULT_PARAMS, ua_profiles=sorted(settings.ua_profiles), ua_default=settings.ua_default)

    @app.post("/schedules/add")
    async def schedule_add(request: Request, db=Depends(get_db)):
        form = await request.form()
        name = (form.get("name") or "").strip() or "Регулярный запуск"
        mode = form.get("mode") if form.get("mode") in ("daily", "interval") else "daily"
        days = ",".join(form.getlist("weekdays")) or "1,2,3,4,5,6,7"
        try:
            hours = float((form.get("every_hours") or "0").replace(",", ".")) if mode == "interval" else None
            nxt = jb.compute_next(mode, form.get("at_time"), days, hours)
        except ValueError as e:
            return redirect("/schedules", err=f"Расписание не создано: {e}")
        params = jb.clean_params(params_from_form(form))
        db.execute("INSERT INTO web_schedules(name, enabled, mode, at_time, weekdays, every_hours, params, next_run_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (name, 1, mode, form.get("at_time") if mode == "daily" else None, days, hours,
                    json.dumps(params, ensure_ascii=False), nxt.isoformat(timespec="seconds")))
        return redirect("/schedules", msg=f"Расписание создано, ближайший запуск {nxt:%d.%m.%Y %H:%M}")

    @app.post("/schedules/{sid:int}/toggle")
    def schedule_toggle(sid: int, db=Depends(get_db)):
        s = db.execute("SELECT * FROM web_schedules WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404)
        enabled = 0 if s["enabled"] else 1
        nxt = jb.compute_next(s["mode"], s["at_time"], s["weekdays"], s["every_hours"]) if enabled else None
        db.execute("UPDATE web_schedules SET enabled=?, next_run_at=? WHERE id=?",
                   (enabled, nxt.isoformat(timespec="seconds") if nxt else None, sid))
        return redirect("/schedules", msg="Включено" if enabled else "Выключено")

    @app.post("/schedules/{sid:int}/delete")
    def schedule_delete(sid: int, db=Depends(get_db)):
        db.execute("DELETE FROM web_schedules WHERE id=?", (sid,))
        return redirect("/schedules", msg="Расписание удалено")

    @app.post("/schedules/{sid:int}/run")
    def schedule_run(sid: int, db=Depends(get_db)):
        s = db.execute("SELECT * FROM web_schedules WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404)
        jid = manager.enqueue(json.loads(s["params"]), title=f"Вручную по расписанию: {s['name']}", schedule_id=None)
        return redirect(f"/jobs/{jid}", msg="Запуск поставлен в очередь")

    # ================================================================== история URL, настройки

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, net: str, url: str, db=Depends(get_db)):
        return render(request, "history.html", "registry", h=mx.url_history(db, net, url))

    # ================================================================== «Доступ и настройки» (всё хранится в БД)

    def reload_settings() -> None:
        """Перечитывает настройки на месте: объект settings общий для всех маршрутов."""
        settings.__dict__.update(load_settings(env_file).__dict__)

    def ensure_site(db, name: str, url: str) -> None:
        """Новая сеть сразу появляется в справочнике страниц; адрес сети задаёт хост для URL."""
        host = urlsplit(url).hostname or ""
        if db.execute("SELECT 1 FROM ref_sites WHERE site=?", (name,)).fetchone():
            if host:
                db.execute("UPDATE ref_sites SET host=? WHERE site=?", (host, name))
        else:
            db.execute("INSERT INTO ref_sites(site, name_ru, host) VALUES (?,?,?)", (name, name, host))

    async def form_dict(request: Request) -> dict:
        return {k: v for k, v in (await request.form()).items() if isinstance(v, str)}

    def back(anchor: str = "", msg: str | None = None, err: str | None = None) -> RedirectResponse:
        r = redirect("/settings", msg=msg, err=err)
        if anchor:
            r.headers["location"] += "#" + anchor
        return r

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, db=Depends(get_db)):
        stored = aps.get_settings(db)
        in_db = {r["name"] for r in aps.list_networks(db)}
        nets = []
        for name, cfg in settings.networks.items():
            k = aps.net_key(name)
            nets.append({
                "name": name, "url": cfg.url or "", "has_token": bool(cfg.token),
                "fallback": bool(not cfg.token and settings.default_token),
                "header": cfg.token_header or "", "sitemaps": "\n".join(cfg.sitemaps),
                "sitemaps_robots": cfg.sitemaps_robots, "sitemaps_fallback": cfg.sitemaps_fallback,
                "gsc_files": ", ".join(aps.parse_list(os.environ.get(f"NET_{k}_GSC_FILES"))),
                "gsc_properties": ", ".join(aps.parse_list(os.environ.get(f"NET_{k}_GSC_PROPERTIES"))),
                "in_db": name in in_db, "info": keymod.describe(name, cfg.url),
            })
        groups = []
        for gid, title, note in aps.GROUPS:
            fields = []
            for f in aps.FIELDS:
                if f["group"] != gid:
                    continue
                secret = f["kind"] in ("secret", "secret_area")
                eff = os.environ.get(f["key"], "")
                fields.append({**f, "secret": secret, "stored": f["key"] in stored, "is_set": bool(eff.strip()),
                               "value": "" if secret else stored.get(f["key"], ""),
                               "shown": "" if secret else eff, "from_env": bool(eff.strip()) and f["key"] not in stored})
            groups.append({"id": gid, "title": title, "note": note, "fields": fields})
        custom_ua = sorted(k for k in settings.ua_profiles if k not in BUILTIN_UA)
        return render(request, "settings.html", "settings", nets=nets, s=settings, groups=groups, db_path=str(db_path),
                      counts=reg.counts(db), ua_profiles=settings.ua_profiles, builtin_ua=BUILTIN_UA, custom_ua=custom_ua,
                      env_cand=aps.env_candidates(), no_db_nets=bool(settings.networks) and not in_db,
                      ua_names=sorted(settings.ua_profiles), ps_keys=len(keymod.pagespeed_keys()))

    @app.post("/settings/save/{group}")
    async def settings_save(request: Request, group: str, db=Depends(get_db)):
        if group not in {g[0] for g in aps.GROUPS}:
            raise HTTPException(404)
        try:
            with tx(db):
                changed = aps.save_group(db, group, await form_dict(request))
        except ValueError as e:
            return back(group, err=str(e))
        reload_settings()
        return back(group, msg=("Сохранено: " + ", ".join(changed)) if changed else "Изменений нет")

    @app.post("/settings/network")
    async def settings_network(request: Request, db=Depends(get_db)):
        f = await form_dict(request)
        orig = (f.get("original") or "").strip() or None
        try:
            with tx(db):
                if not aps.list_networks(db):  # сети ещё читались из .env — переносим, иначе они пропадут из списка
                    aps.import_from_env(db, with_settings=False)
                name = aps.save_network(db, f, original=orig)
                ensure_site(db, name, aps.norm_url(f.get("url", "")))
        except ValueError as e:
            return back("networks", err=str(e))
        reload_settings()
        mirror(db)
        return back("net-" + name, msg=f"Сеть «{name}» {'сохранена' if orig else 'добавлена'}")

    @app.post("/settings/network/{name}/delete")
    def settings_network_delete(name: str, db=Depends(get_db)):
        aps.delete_network(db, name)
        reload_settings()
        return back("networks", msg=f"Сеть «{name}» убрана из настроек. Справочник страниц и история проверок сохранены.")

    @app.post("/settings/network/{name}/check-sitemaps", response_class=HTMLResponse)
    def settings_check_sitemaps(request: Request, name: str):
        cfg = settings.networks.get(name)
        if not cfg:
            raise HTTPException(404)
        try:
            res, err = smc.check_network(settings, cfg), None
        except ConfigError as e:
            res, err = None, str(e)
        return render(request, "sitemap_check.html", "settings", net_name=name, res=res, cfg_err=err)

    @app.post("/settings/ua")
    async def settings_ua_add(request: Request, db=Depends(get_db)):
        f = await form_dict(request)
        try:
            name = aps.add_ua(db, f.get("name", ""), f.get("value", ""), set(BUILTIN_UA))
        except ValueError as e:
            return back("ua", err=str(e))
        reload_settings()
        return back("ua", msg=f"Профиль UA «{name}» сохранён")

    @app.post("/settings/ua/{name}/delete")
    def settings_ua_delete(name: str, db=Depends(get_db)):
        if name in BUILTIN_UA:
            return back("ua", err="Встроенный профиль удалить нельзя")
        aps.delete_ua(db, name)
        reload_settings()
        return back("ua", msg=f"Профиль «{name}» удалён")

    @app.post("/settings/import-env")
    def settings_import_env(db=Depends(get_db)):
        with tx(db):
            rep = aps.import_from_env(db)
            for n in aps.list_networks(db):
                if n["url"]:
                    ensure_site(db, n["name"], n["url"])
        reload_settings()
        mirror(db)
        return back("import", msg=f"Перенесено из .env: сетей — {rep['networks']}, настроек — {rep['settings']}")

    return app
