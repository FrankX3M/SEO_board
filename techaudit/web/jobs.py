"""Фоновые запуски (сбор + проверки) и расписание для веб-интерфейса.

Запуск = подпроцессы тех же скриптов, что и в консоли (collect_html.py, run_checks.py), поэтому
токены, UA и пороги берутся из настроек (БД веб-интерфейса, запасной вариант — .env). В параметрах заданий и в БД секретов нет.
Одновременно выполняется одно задание, остальные ждут в очереди.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from ..config import ROOT
from ..storage import now_iso
from ..webdb import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, params TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',     -- queued | running | done | failed | cancelled
  trigger TEXT NOT NULL DEFAULT 'manual',    -- manual | schedule
  schedule_id INTEGER, run_id TEXT, exit_code INTEGER,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, log_path TEXT
);
CREATE TABLE IF NOT EXISTS web_schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  mode TEXT NOT NULL DEFAULT 'daily',        -- daily | interval
  at_time TEXT, weekdays TEXT DEFAULT '1,2,3,4,5,6,7', every_hours REAL,
  params TEXT NOT NULL, last_run_at TEXT, next_run_at TEXT
);
"""

DEFAULT_PARAMS = {
    "networks": [],            # пусто = все сети из настроек
    "limit": None,             # не более N URL на тип (None = весь справочник)
    "variants": "plain,bot,browser",
    "probes": "all",           # all | none | redirects,not_found,...
    "no_diff": False,
    "ua": "",                  # профиль UA для bot/browser ('' = «UA для бота» из настроек)
    "token": "env",            # env (как в .env) | on | off
    "checks": True,            # после сбора запустить проверки
    "live": True,              # живая выборка с прода: ссылки, битые картинки, canonical, sitemap
    "mobile": True,            # второй сбор с мобильным UA для сверки мобильной и десктопной версий
    "baseline": "",            # эталон для сверки микроразметки ('' = предыдущий запуск)
    "external": False,         # PageSpeed + Search Console (тратят квоту ключей)
    "debug": False,            # сохранить условия запуска (UA, заголовок токена, пороги) для ручной перепроверки
    "ai": False,               # после проверок — этап ИИ (сводка, первопричины, черновики комментариев)
    "kind": "collect",         # collect — сбор + проверки; ai — только этап ИИ по готовому запуску
    "run_id": "",
}


def clean_params(raw: dict) -> dict:
    p = dict(DEFAULT_PARAMS)
    p["networks"] = [n for n in (raw.get("networks") or []) if n]
    lim = raw.get("limit")
    p["limit"] = int(lim) if str(lim or "").strip().isdigit() and int(lim) > 0 else None
    for k in ("variants", "probes", "ua"):
        v = (raw.get(k) or DEFAULT_PARAMS[k]).strip() if k != "ua" else (raw.get(k) or "").strip()
        p[k] = "".join(ch for ch in v if ch.isalnum() or ch in ",-_:") or DEFAULT_PARAMS[k]
    for k in ("no_diff", "checks", "live", "mobile", "external", "debug", "ai"):
        v = raw.get(k, DEFAULT_PARAMS[k])
        p[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "on", "true", "yes", "да")
    if raw.get("kind") == "ai":
        rid = "".join(ch for ch in str(raw.get("run_id") or "") if ch.isalnum() or ch in "-_.")
        p["kind"], p["run_id"] = "ai", rid
    p["baseline"] = "".join(ch for ch in str(raw.get("baseline") or "") if ch.isalnum() or ch in "-_.")
    p["token"] = raw.get("token") if raw.get("token") in ("env", "on", "off") else "env"
    return p


def build_commands(params: dict, run_id: str | None = None, mobile_id: str | None = None,
                   baseline: str | None = None) -> list[list[str]]:
    py = sys.executable
    if params.get("kind") == "ai":
        cmd = [py, "-u", str(ROOT / "run_checks.py"), "--run", params["run_id"], "--ai-only"]
        if params["networks"]:
            cmd += ["--network", ",".join(params["networks"])]
        return [cmd]
    cmd = [py, "-u", str(ROOT / "collect_html.py"), "--tag", "web", "--variants", params["variants"],
           "--probes", params["probes"]]
    if params["networks"]:
        cmd += ["--network", ",".join(params["networks"])]
    if params["limit"]:
        cmd += ["--limit", str(params["limit"])]
    if params["no_diff"]:
        cmd += ["--no-diff"]
    if params.get("debug"):
        cmd += ["--debug"]
    if params["ua"]:
        cmd += ["--ua", params["ua"]]
    if params["token"] == "on":
        cmd += ["--token"]
    elif params["token"] == "off":
        cmd += ["--no-token"]
    cmds = [cmd]
    if params["checks"] and params.get("mobile"):
        m = [py, "-u", str(ROOT / "collect_html.py"), "--tag", "mobile", "--variants", "bot", "--probes", "none", "--no-diff",
             "--ua", "googlebot-mobile"]
        if params["networks"]:
            m += ["--network", ",".join(params["networks"])]
        if params["limit"]:
            m += ["--limit", str(params["limit"])]
        if params["token"] == "on":
            m += ["--token"]
        elif params["token"] == "off":
            m += ["--no-token"]
        cmds.append(m)
    if params["checks"]:
        c2 = [py, "-u", str(ROOT / "run_checks.py")]
        if run_id:
            c2 += ["--run", run_id]
        if params["networks"]:
            c2 += ["--network", ",".join(params["networks"])]
        if mobile_id:
            c2 += ["--mobile-run", mobile_id]
        base = params.get("baseline") or baseline
        if base:
            c2 += ["--baseline", base]
        if params["live"]:
            c2 += ["--live"]
        for flag, key in (("--external", "external"), ("--debug", "debug"), ("--ai", "ai")):
            if params.get(key):
                c2 += [flag]
        if params["token"] == "off":
            c2 += ["--no-token"]
        cmds.append(c2)
    return cmds


# ------------------------------------------------------------------ расписание

def parse_hhmm(s: str) -> tuple[int, int]:
    try:
        h, m = s.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        return h, m
    except Exception as e:  # noqa: BLE001
        raise ValueError("время в формате ЧЧ:ММ, например 07:30") from e


def parse_weekdays(s: str) -> list[int]:
    days = sorted({int(x) for x in (s or "").replace(" ", "").split(",") if x.isdigit() and 1 <= int(x) <= 7})
    return days or [1, 2, 3, 4, 5, 6, 7]


def compute_next(mode: str, at_time: str | None, weekdays: str | None, every_hours: float | None,
                 now: datetime | None = None) -> datetime:
    """Следующий запуск по локальному времени машины (дни недели: 1 = понедельник)."""
    now = now or datetime.now()
    if mode == "interval":
        h = float(every_hours or 0)
        if h <= 0:
            raise ValueError("интервал должен быть больше нуля часов")
        return now + timedelta(hours=h)
    hh, mm = parse_hhmm(at_time or "")
    days = parse_weekdays(weekdays or "")
    for add in range(0, 8):
        cand = (now + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand > now and cand.isoweekday() in days:
            return cand
    raise ValueError("не удалось вычислить следующий запуск")


class JobManager:
    def __init__(self, db_path: Path, logs_dir: Path):
        self.db_path, self.logs_dir = Path(db_path), Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._current: int | None = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._cancel_flag: int | None = None

    # ---- жизненный цикл
    def start(self) -> None:
        db = connect(self.db_path)
        try:
            db.execute("UPDATE web_jobs SET status='failed', finished_at=? WHERE status='running'", (now_iso(),))
        finally:
            db.close()
        for target in (self._worker, self._scheduler):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self.cancel_current()

    # ---- очередь
    def enqueue(self, params: dict, *, title: str = "", trigger: str = "manual", schedule_id: int | None = None) -> int:
        p = clean_params(params)
        nets = ", ".join(p["networks"]) or "все сети"
        if p["kind"] == "ai":
            title = title or f"ИИ-анализ запуска {p['run_id']}: {nets}"
        title = title or f"Сбор + проверки: {nets}" + (f", до {p['limit']} URL на тип" if p["limit"] else "")
        db = connect(self.db_path)
        try:
            cur = db.execute("INSERT INTO web_jobs(title, params, status, trigger, schedule_id, created_at) "
                             "VALUES (?,?,?,?,?,?)",
                             (title, json.dumps(p, ensure_ascii=False), "queued", trigger, schedule_id, now_iso()))
            return cur.lastrowid
        finally:
            db.close()

    def cancel(self, job_id: int) -> bool:
        db = connect(self.db_path)
        try:
            row = db.execute("SELECT status FROM web_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return False
            if row["status"] == "queued":
                db.execute("UPDATE web_jobs SET status='cancelled', finished_at=? WHERE id=?", (now_iso(), job_id))
                return True
        finally:
            db.close()
        if self._current == job_id:
            self._cancel_flag = job_id
            self.cancel_current()
            return True
        return False

    def cancel_current(self) -> None:
        with self._lock:
            p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    # ---- исполнитель
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                db = connect(self.db_path)
                try:
                    row = db.execute("SELECT * FROM web_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                finally:
                    db.close()
                if row:
                    self._run_job(dict(row))
                    continue
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            self._stop.wait(2)

    def _run_job(self, job: dict) -> None:
        jid = job["id"]
        params = json.loads(job["params"])
        log_path = self.logs_dir / f"job-{jid}.log"
        started = now_iso()
        db = connect(self.db_path)
        try:
            db.execute("UPDATE web_jobs SET status='running', started_at=?, log_path=? WHERE id=?",
                       (started, log_path.name, jid))
        finally:
            db.close()
        self._current = jid
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        code, run_id, cancelled = 0, None, False
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                mobile_id = None
                for i in range(len(build_commands(params))):
                    cmds = build_commands(params, run_id, mobile_id, self._prev_run(run_id) if run_id else None)
                    cmd = cmds[i]
                    is_mobile = "mobile" in cmd and "--tag" in cmd and cmd[cmd.index("--tag") + 1] == "mobile"
                    log.write(f"\n$ {' '.join(os.path.basename(c) if c == sys.executable else c for c in cmd)}\n")
                    log.flush()
                    t0 = now_iso()
                    with self._lock:
                        self._proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
                    code = self._proc.wait()
                    with self._lock:
                        self._proc = None
                    if params.get("kind") == "ai":
                        run_id = params["run_id"]
                    elif i == 0:
                        run_id = self._find_run(started)
                        if run_id:
                            self._set_run(jid, run_id)
                    elif is_mobile and code == 0:
                        mobile_id = self._find_run(t0)
                    if code != 0:
                        cancelled = self._stop.is_set() or self._cancel_flag == jid
                        if is_mobile and not cancelled:
                            log.write("Мобильный сбор не удался — проверки продолжатся без сверки версий.\n")
                            code = 0
                            continue
                        break
        except Exception as e:  # noqa: BLE001
            code = 1
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\nОшибка запуска: {type(e).__name__}: {e}\n")
        finally:
            self._current = None
            db = connect(self.db_path)
            try:
                status = "cancelled" if cancelled else ("done" if code == 0 else "failed")
                db.execute("UPDATE web_jobs SET status=?, exit_code=?, finished_at=?, run_id=COALESCE(?, run_id) WHERE id=?",
                           (status, code, now_iso(), run_id, jid))
            finally:
                db.close()

    def _find_run(self, since_iso: str) -> str | None:
        db = connect(self.db_path)
        try:
            r = db.execute("SELECT run_id FROM runs WHERE started_at>=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                           (since_iso,)).fetchone()
            return r["run_id"] if r else None
        finally:
            db.close()

    def _prev_run(self, run_id: str) -> str | None:
        """Предыдущий обычный запуск (не мобильный) — эталон для сверки состава микроразметки."""
        db = connect(self.db_path)
        try:
            r = db.execute("SELECT run_id FROM runs WHERE started_at < (SELECT started_at FROM runs WHERE run_id=?) "
                           "AND run_id NOT LIKE '%-mobile' ORDER BY started_at DESC LIMIT 1", (run_id,)).fetchone()
            return r["run_id"] if r else None
        finally:
            db.close()

    def _set_run(self, jid: int, run_id: str) -> None:
        db = connect(self.db_path)
        try:
            db.execute("UPDATE web_jobs SET run_id=? WHERE id=?", (run_id, jid))
        finally:
            db.close()

    # ---- расписание
    def _scheduler(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            self._stop.wait(20)

    def tick(self, now: datetime | None = None) -> list[int]:
        """Ставит в очередь задания по сработавшим расписаниям. Возвращает id созданных заданий."""
        now = now or datetime.now()
        made = []
        db = connect(self.db_path)
        try:
            for s in db.execute("SELECT * FROM web_schedules WHERE enabled=1").fetchall():
                nxt = datetime.fromisoformat(s["next_run_at"]) if s["next_run_at"] else None
                if nxt is None or nxt > now:
                    continue
                busy = db.execute("SELECT 1 FROM web_jobs WHERE schedule_id=? AND status IN ('queued','running')",
                                  (s["id"],)).fetchone()
                new_next = compute_next(s["mode"], s["at_time"], s["weekdays"], s["every_hours"], now)
                db.execute("UPDATE web_schedules SET next_run_at=?, last_run_at=? WHERE id=?",
                           (new_next.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), s["id"]))
                if busy:
                    continue  # предыдущий запуск по этому расписанию ещё идёт — пропускаем, не копим очередь
                made.append(self.enqueue(json.loads(s["params"]), title=f"По расписанию: {s['name']}",
                                         trigger="schedule", schedule_id=s["id"]))
        finally:
            db.close()
        return made
