"""Тесты второй очереди: ключи, краул Screaming Frog, блоки и период на дашборде, аккордеон проверок, debug-доказательства, выгрузки, ИИ.

    python -m unittest tests.test_web2
"""
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from techaudit import crawl, evidence, external, keys
from techaudit import metrics as mx
from techaudit.storage import Store
from techaudit.web import jobs as jb

SECRET = "SECRET-TOKEN-VALUE"
SF_HEADER = ("Address,Content Type,Status Code,Indexability,Indexability Status,Title 1,Title 1 Length,Meta Description 1,"
             "Meta Description 1 Length,H1-1,H1-2,Meta Robots 1,X-Robots-Tag 1,Canonical Link Element 1,Size (bytes),Word Count,"
             "Crawl Depth,Inlinks,Unique Inlinks,Outlinks,Response Time,Redirect URL,Redirect Type,Last Modified,Crawl Timestamp")


def sf_csv(extra: str = "") -> bytes:
    rows = [
        'https://5ka.example/,text/html; charset=utf-8,200,Indexable,,Главная,7,Описание,8,Главная,,,,https://5ka.example/,"12 345",500,0,"1,965",1500,40,"0,25",,,,2026-10-20 10:00:00',
        'https://5ka.example/cat/,text/html,200,Indexable,,Каталог,7,,0,Каталог,,,,https://5ka.example/cat/,9000,300,1,12,10,30,"0,31",,,,2026-10-20 10:01:00',
        'https://5ka.example/gone/,text/html,404,Non-Indexable,Client Error,,0,,0,,,,,,512,0,2,3,3,0,"0,10",,,,2026-10-20 10:02:00',
        'https://5ka.example/old/,text/html,301,Non-Indexable,Redirected,,0,,0,,,,,,0,0,1,2,2,0,"0,05",https://5ka.example/cat/,HTTP Redirect,,2026-10-20 10:03:00',
        'https://5ka.example/lonely/,text/html,200,Indexable,,Сирота,6,,0,Сирота,,,,https://5ka.example/gone/,8000,200,2,0,0,5,"0,40",,,,2026-10-20 10:04:00',
        'https://5ka.example/api,text/html,500,Non-Indexable,Server Error,,0,,0,,,,,,0,0,1,1,1,0,"1,00",,,,2026-10-20 10:05:00',
        'https://other.example/x,text/html,200,Indexable,,Чужой,5,,0,Чужой,,,,,100,10,1,1,1,1,"0,10",,,,2026-10-20 10:06:00',
    ]
    return (SF_HEADER + "\n" + "\n".join(rows) + extra + "\n").encode("utf-8-sig")


def make_app(tmp: Path):
    os.environ.update({
        "TECHAUDIT_ENV": os.devnull, "NETWORKS": "5ka", "NET_5KA_URL": "https://5ka.example", "NET_5KA_TOKEN": SECRET,
        "DATA_DIR": str(tmp / "data"), "REGISTRY_DIR": str(tmp / "registry"),
    })
    from techaudit.web.app import create_app
    return create_app(None, start_jobs=False)


class KeysTest(unittest.TestCase):
    def test_lists_and_dedup(self):
        env = {"PAGESPEED_API_KEYS": "a, b;c\nb", "PAGESPEED_API_KEY": "a", "PAGESPEED_API_KEY_2": "d"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            self.assertEqual(keys.pagespeed_keys(), ["a", "b", "c", "d"])
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_gsc_properties_default(self):
        p = keys.gsc_properties("5ka", "https://www.5ka.ru", {})
        self.assertEqual(p, ["sc-domain:5ka.ru", "https://www.5ka.ru/"])
        self.assertEqual(keys.gsc_properties("5ka", "https://x", {"NET_5KA_GSC_PROPERTIES": "sc-domain:a.ru,https://a.ru/"}),
                         ["sc-domain:a.ru", "https://a.ru/"])

    def test_pool_rotation_and_kill(self):
        pool = external.Pool(["k1", "k2", "k3"])
        got = [pool.take()[1] for _ in range(4)]
        self.assertEqual(got, ["k1", "k2", "k3", "k1"])
        pool.kill(0, "квота")
        pool.kill(1, "квота")
        self.assertEqual({pool.take()[1] for _ in range(3)}, {"k3"})
        pool.kill(2, "квота")
        self.assertIsNone(pool.take())
        self.assertIn("исчерпано", pool.summary("ключи"))
        self.assertNotIn("k1", pool.summary("ключи"))  # значения ключей в сводку не попадают

    def test_external_off_by_default(self):
        old = os.environ.pop("EXTERNAL_CHECKS", None)
        try:
            self.assertFalse(external.enabled())
            os.environ["EXTERNAL_CHECKS"] = "1"
            self.assertTrue(external.enabled())
        finally:
            os.environ.pop("EXTERNAL_CHECKS", None)
            if old is not None:
                os.environ["EXTERNAL_CHECKS"] = old


class BlocksFreshProcessTest(unittest.TestCase):
    def test_check_ids_matched_without_preloaded_registry(self):
        """Регрессия: в «чистом» процессе пункты чек-листа должны получать check_id (иначе всё выглядит «ручным»)."""
        import subprocess
        import sys
        code = ("import sys; sys.path.insert(0, %r); from techaudit import blocks; "
                "i = blocks.check_index(); print(len(i), i.get('redirect_slash', {}).get('block'))" % str(Path(__file__).resolve().parent.parent))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "TECHAUDIT_ENV": os.devnull}).stdout.split()
        self.assertGreater(int(out[0]), 40)
        self.assertEqual(out[1], "Редиректы")


class JobParamsTest(unittest.TestCase):
    def test_mobile_and_baseline_chain(self):
        p = jb.clean_params({"networks": ["5ka"], "checks": True, "mobile": True, "live": True})
        cmds = jb.build_commands(p, "RUN1", "RUN1-mobile", "PREV")
        self.assertEqual(len(cmds), 3)
        self.assertIn("googlebot-mobile", cmds[1])
        self.assertEqual(cmds[2][cmds[2].index("--mobile-run") + 1], "RUN1-mobile")
        self.assertEqual(cmds[2][cmds[2].index("--baseline") + 1], "PREV")
        self.assertIn("--live", cmds[2])
        self.assertTrue(jb.DEFAULT_PARAMS["live"])

    def test_flags_reach_commands(self):
        p = jb.clean_params({"networks": ["5ka"], "external": "on", "debug": True, "ai": "1", "checks": True})
        cmds = jb.build_commands(p, "RUN1")
        self.assertIn("--debug", cmds[0])
        self.assertTrue({"--external", "--debug", "--ai"} <= set(cmds[-1]))
        a = jb.clean_params({"kind": "ai", "run_id": "2026;rm -rf", "networks": ["5ka"]})
        self.assertEqual(a["run_id"], "2026rm-rf")  # опасные символы вычищены
        c = jb.build_commands(a)
        self.assertEqual(len(c), 1)
        self.assertIn("--ai-only", c[0])


@unittest.skipIf(TestClient is None, "нет fastapi/httpx")
class Web2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.app = make_app(cls.tmp)
        cls.c = TestClient(cls.app, follow_redirects=False)
        cls._seed()

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        cls._tmp.cleanup()

    @classmethod
    def db(cls):
        from techaudit.webdb import connect
        return connect(cls.app.state.db_path)

    @classmethod
    def _seed(cls):
        store = Store(cls.app.state.settings.data_dir)
        now = datetime.now().astimezone()
        cls.runs = []
        for i, fail in enumerate([2, 1]):
            rid = f"2026102{i}-000000-prod-t"
            cls.runs.append(rid)
            opts = {"limit": 2, "variants": ["plain", "bot"], "probes": ["redirects"], "probe_limit": 2, "debug": True,
                    "ua_strings": {"bot": "Googlebot/2.1 (+http://www.google.com/bot.html)", "plain": "Mozilla/5.0 Desktop"},
                    "networks": {"5ka": {
                        "bot": {"user_agent": "googlebot", "token": "X-SP-Token: SEC…(18 симв.)", "warnings": []},
                        "plain": {"user_agent": "desktop", "token": "ВЫКЛЮЧЕН", "warnings": []}}}}
            store.db.execute("INSERT INTO runs(run_id, started_at, env, options) VALUES (?,?,?,?)",
                             (rid, (now - timedelta(days=2 - i)).isoformat(timespec="seconds"), "prod", json.dumps(opts)))
            store.add_page({"run_id": rid, "network": "5ka", "page_type": "home", "variant": "bot", "ua_name": "googlebot", "mode": "raw",
                            "source_url": "/", "url": "https://5ka.example/", "status": 200, "ttfb_ms": 200, "bytes": 2048,
                            "has_title": 1, "has_h1": 1, "fetched_at": "2026-01-01T00:00:00+00:00"})
            store.add_probe({"run_id": rid, "network": "5ka", "kind": "slash", "url": "https://5ka.example/cat", "source_url": "/cat/",
                             "status": 301, "final_url": "https://5ka.example/cat/", "verdict": "pass",
                             "fetched_at": "2026-01-01T00:00:00+00:00"})
            rows = [("redirect_slash", "pass" if i else "fail", None), ("ttfb", "pass", None),
                    ("links_404", "fail", {"total": 10, "bad": fail}), ("viewport", "skipped", None)]
            for cid, st, stats in rows:
                store.db.execute(
                    "INSERT INTO check_results(run_id,network,check_id,title,status,summary,items,source,created_at,stats) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, "5ka", cid, f"Проверка {cid}", st, "итог", json.dumps(
                        [{"url": "https://5ka.example/", "ok": st == "pass", "msg": "сообщение", "kind": "home"}]),
                     "html", "2026-01-01T00:00:00+00:00", json.dumps(stats) if stats else None))
        store.db.execute("INSERT INTO ai_notes(run_id,network,kind,data,model,created_at) VALUES (?,?,?,?,?,?)",
                         (cls.runs[-1], "5ka", "analysis", json.dumps({"summary": "Сводка ИИ", "root_causes": [], "tasks": [],
                                                                      "comments": {"links_404": "Комментарий ИИ"}}), "m1", "2026-01-01T00:00:00+00:00"))
        store.db.commit()
        store.close()

    # ---------------------------------------------------------------- краул

    def test_01_crawl_upload_and_metrics(self):
        r = self.c.post("/crawl/upload", data={"network": "5ka", "label": "до релиза"},
                        files={"file": ("sf.csv", io.BytesIO(sf_csv()), "text/csv")})
        self.assertEqual(r.status_code, 303, r.text)
        self.assertIn("/crawl/", r.headers["location"])
        db = self.db()
        imp = crawl.imports(db, "5ka")[0]
        self.assertEqual(imp["rows"], 6)  # чужой хост отброшен
        m = crawl.metrics(db, imp["id"])
        self.assertEqual(m["crawl_4xx"], 1)
        self.assertEqual(m["crawl_5xx"], 1)
        self.assertEqual(m["crawl_orphans"], 1)
        self.assertEqual(m["crawl_links_to_404"], 3)
        row = db.execute("SELECT size, response_ms FROM crawl_pages WHERE import_id=? AND address='https://5ka.example/'",
                         (imp["id"],)).fetchone()
        self.assertEqual((row["size"], row["response_ms"]), (12345, 250.0))  # «12 345» и «0,25 с» → мс
        db.close()
        for path in ("/crawl", "/crawl?net=5ka", f"/crawl/{imp['id']}", f"/crawl/{imp['id']}?flt=orphans",
                     f"/crawl/{imp['id']}?flt=4xx&q=gone", f"/crawl/{imp['id']}/export.csv?flt=all"):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        bad = self.c.post("/crawl/upload", data={"network": "5ka"}, files={"file": ("x.csv", io.BytesIO(b""), "text/csv")})
        self.assertIn("err=", bad.headers["location"])
        bad = self.c.post("/crawl/upload", data={"network": "5ka"},
                          files={"file": ("x.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")})
        self.assertIn("err=", bad.headers["location"])

    def test_02_crawl_diff_and_delete(self):
        extra = '\nhttps://5ka.example/new/,text/html,200,Indexable,,Новая,5,,0,Новая,,,,,100,10,1,1,1,1,"0,10",,,,2026-10-21 10:00:00'
        r = self.c.post("/crawl/upload", data={"network": "5ka"}, files={"file": ("sf2.csv", io.BytesIO(sf_csv(extra)), "text/csv")})
        new_id = int(r.headers["location"].split("/crawl/")[1].split("?")[0])
        db = self.db()
        old_id = [i["id"] for i in crawl.imports(db, "5ka") if i["id"] != new_id][-1]
        d = crawl.diff(db, old_id, new_id)
        self.assertEqual(d["new_total"], 1)
        self.assertEqual(d["gone_total"], 0)
        db.close()
        self.assertEqual(self.c.get(f"/crawl/{new_id}?vs={old_id}").status_code, 200)
        self.assertEqual(self.c.post(f"/crawl/{new_id}/delete").status_code, 303)
        db = self.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM crawl_pages WHERE import_id=?", (new_id,)).fetchone()[0], 0)
        db.close()

    def test_03_checks_use_crawl(self):
        from techaudit.checks.runner import run_checks
        from techaudit.config import load_settings
        store = Store(self.app.state.settings.data_dir)
        settings = load_settings(None)
        # запуск новее краула → берётся краул
        store.db.execute("INSERT INTO runs(run_id, started_at, env, options) VALUES (?,?,?,?)",
                         ("run-after-crawl", datetime.now().astimezone().isoformat(timespec="seconds"), "prod", "{}"))
        store.db.commit()
        res = run_checks(store, settings, "run-after-crawl", "5ka")
        store.close()
        for cid in ("links_404", "links_redirects", "orphans", "status_no5xx", "canonical_target_ok"):
            self.assertEqual(res[cid].source, "crawl", cid)
        self.assertEqual(res["links_404"].status, "fail")
        self.assertEqual(res["links_404"].stats["bad"], 1)
        self.assertEqual(res["orphans"].status, "fail")
        self.assertEqual(res["status_no5xx"].status, "fail")
        self.assertEqual(res["canonical_target_ok"].status, "fail")  # canonical лежит на 404
        self.assertIn("Screaming Frog", res["links_404"].summary)

    # ---------------------------------------------------------------- дашборд

    def test_04_board_blocks_points_period(self):
        db = self.db()
        data = mx.board(db, "5ka")
        kinds = [p["kind"] for p in data["points"]]
        self.assertIn("crawl", kinds)
        self.assertIn("run", kinds)
        names = [g["name"] for g in data["tree"]]
        self.assertIn("Редиректы", names)
        self.assertIn("Итоговый краул", names)
        self.assertLess(names.index("Итоги"), names.index("Редиректы"))
        # балл блока и значение проверки из stats (1 плохая из 10 → 90%)
        self.assertIn("block:Редиректы", data["series"])
        last_run_idx = max(i for i, p in enumerate(data["points"]) if p["id"] == self.runs[-1])
        self.assertEqual(data["series"]["check:links_404"][last_run_idx], 90.0)
        self.assertEqual(data["series"]["block:Редиректы"][last_run_idx], 100.0)
        # краул-метрики лежат только на точке краула
        ci = kinds.index("crawl")
        self.assertIsNotNone(data["series"]["crawl_orphans"][ci])
        # без сети краулов нет
        self.assertNotIn("crawl", [p["kind"] for p in mx.board(db, None)["points"]])
        # период: далёкое будущее — пусто
        self.assertEqual(mx.board(db, "5ka", "2999-01-01", None)["points"], [])
        csv_text = mx.board_csv(data)
        self.assertIn("Блок;Метрика;Единица", csv_text)
        db.close()
        r = self.c.get("/api/board?net=5ka&p=0")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["points"])
        for path in ("/", "/?net=all", "/?net=5ka&p=7", "/?net=5ka&from=2020-01-01&to=2999-01-01"):
            self.assertEqual(self.c.get(path).status_code, 200, path)

    # ---------------------------------------------------------------- проверки, debug, выгрузки, ИИ

    def test_05_accordion_detail_evidence(self):
        view = mx.checks_view(self.db(), "5ka", self.runs[-1])
        red = next(b for b in view["blocks"] if b["name"] == "Редиректы")
        slash = next(i for i in red["rows"] if i["check_id"] == "redirect_slash")
        self.assertFalse(slash["manual"])
        self.assertIn(slash["status"], ("pass", "fail"))
        self.assertNotIn("Прочее", [b["name"] for b in view["blocks"]])
        r = self.c.get(f"/checks?net=5ka&run={self.runs[-1]}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("chk-acc", r.text)
        self.assertIn("Редиректы", r.text)  # блоки как в чек-листе
        self.assertIn("Сводка ИИ", r.text)
        d = self.c.get(f"/checks/detail?net=5ka&run={self.runs[-1]}&check=links_404")
        self.assertEqual(d.status_code, 200)
        self.assertIn("Комментарий ИИ", d.text)
        self.assertIn("Условия запуска", d.text)
        self.assertIn("Googlebot/2.1", d.text)  # строка UA из режима debug
        self.assertIn("не отправлялся", d.text)  # у plain токена нет
        ev = self.c.get("/checks/evidence", params={"net": "5ka", "run": self.runs[-1], "url": "https://5ka.example/"})
        self.assertEqual(ev.status_code, 200)
        self.assertIn("curl", ev.text)
        self.assertIn('$SP_TOKEN', ev.text)
        self.assertIn("X-SP-Token", ev.text)
        pe = self.c.get("/checks/evidence", params={"net": "5ka", "run": self.runs[-1], "url": "https://5ka.example/cat/"})
        self.assertIn("проба", pe.text)
        none = self.c.get("/checks/evidence", params={"net": "5ka", "run": self.runs[-1], "url": "https://5ka.example/zzz/"})
        self.assertIn("нет сохранённых запросов", none.text)
        e = evidence.for_url(self.db(), self.app.state.settings, self.runs[-1], "5ka", "https://5ka.example/")
        self.assertTrue(e["found"])
        self.assertTrue(e["requests"][0]["cond"]["token_on"])

    def test_06_exports(self):
        rid = self.runs[-1]
        r = self.c.get(f"/export/report.html?run={rid}&net=5ka")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("Отчёт технического аудита", r.text)
        self.assertIn("Сводка ИИ", r.text)
        self.assertEqual(self.c.get(f"/export/report.html?run={rid}&inline=1").status_code, 200)
        r = self.c.get(f"/export/checks.csv?run={rid}&net=5ka")
        self.assertIn("Статус", r.text)
        self.assertIn("не пройдено", self.c.get(f"/export/checks.csv?run={rid}&urls=1").text)
        r = self.c.get(f"/export/metrics.csv?net=5ka&p=0")
        self.assertIn("Блок;Метрика", r.text)
        x = self.c.get(f"/export/checklist.xlsx?run={rid}&net=5ka")
        self.assertIn(x.status_code, (200, 404))
        if x.status_code == 200:
            self.assertTrue(x.content.startswith(b"PK"))
        self.assertEqual(self.c.get("/export/report.html?run=nope").status_code, 404)

    def test_07_ai_page_and_job(self):
        r = self.c.get(f"/ai?net=5ka&run={self.runs[-1]}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Сводка ИИ", r.text)
        self.assertIn("не меняет", r.text.lower() + " не меняет")
        r = self.c.post("/ai/start", data={"run": self.runs[-1], "net": "5ka"})
        self.assertEqual(r.status_code, 303)
        db = self.db()
        job = db.execute("SELECT * FROM web_jobs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(job["params"])["kind"], "ai")
        db.close()

    def test_08_no_secrets_anywhere(self):
        paths = ["/", "/checks?net=5ka", f"/checks/detail?net=5ka&run={self.runs[-1]}&check=links_404&all=1", "/settings", "/ai?net=5ka",
                 "/runs", "/crawl", "/api/board?net=5ka", f"/runs/{self.runs[-1]}",
                 f"/checks/evidence?net=5ka&run={self.runs[-1]}&url=https://5ka.example/"]
        for p in paths:
            self.assertNotIn(SECRET, self.c.get(p).text, p)
        self.assertIn("Ключи PageSpeed", self.c.get("/settings").text)

    def test_09_run_form_has_new_options(self):
        t = self.c.get("/runs").text
        for name in ('name="external"', 'name="debug"', 'name="ai"'):
            self.assertIn(name, t)


if __name__ == "__main__":
    unittest.main()
