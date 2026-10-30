"""Тесты веб-интерфейса: справочник в БД, CSV-импорт, метрики в динамике, расписание.

    python -m unittest tests.test_web
Сайт не трогается; нужны fastapi и httpx (pip install -r requirements.txt).
"""
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

from techaudit import db_registry as reg
from techaudit import metrics as mx
from techaudit.storage import Store
from techaudit.web import jobs as jb


def make_app(tmp: Path):
    os.environ.update({
        "TECHAUDIT_ENV": os.devnull, "NETWORKS": "5ka", "NET_5KA_URL": "https://5ka.example",
        "NET_5KA_TOKEN": "SECRET-TOKEN-VALUE", "DATA_DIR": str(tmp / "data"), "REGISTRY_DIR": str(tmp / "registry"),
    })
    from techaudit.web.app import create_app
    return create_app(None, start_jobs=False)


@unittest.skipIf(TestClient is None, "нет fastapi/httpx")
class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.app = make_app(cls.tmp)
        cls.c = TestClient(cls.app, follow_redirects=False)
        cls.db_path = cls.app.state.db_path

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        cls._tmp.cleanup()

    def db(self):
        from techaudit.webdb import connect
        return connect(self.db_path)

    # ---------------------------------------------------------------- справочник

    def test_01_seeded_and_pages_render(self):
        db = self.db()
        self.assertGreater(reg.counts(db)["urls"], 50)
        db.close()
        for path in ("/", "/checks", "/registry", "/reference?tab=sites", "/reference?tab=types", "/reference?tab=owners",
                     "/reference?tab=log", "/import", "/import?entity=types", "/runs", "/schedules", "/settings"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_02_crud_url(self):
        r = self.c.post("/registry/add", data={"site": "5ka", "page_type": "home", "url": "/test-crud/", "note": "t"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("msg=", r.headers["location"])
        db = self.db()
        rows, _ = reg.list_urls(db, q="test-crud")
        self.assertEqual(len(rows), 1)
        rid = rows[0]["id"]
        # дубль
        r = self.c.post("/registry/add", data={"site": "5ka", "page_type": "home", "url": "/test-crud/"})
        self.assertIn("err=", r.headers["location"])
        # неверный хост
        r = self.c.post("/registry/add", data={"site": "5ka", "page_type": "home", "url": "https://evil.example/x"})
        self.assertIn("err=", r.headers["location"])
        # правка
        r = self.c.post(f"/registry/{rid}/save", data={"site": "5ka", "page_type": "home", "url": "/test-crud-2/",
                                                        "note": "n", "status": "gone"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("/test-crud-2/", r.text)
        r = self.c.post(f"/registry/{rid}/save", data={"site": "5ka", "page_type": "home", "url": "bad url", "status": "active"})
        self.assertIn("err-inline", r.text)
        # удалённые не попадают к сборщику
        entries = reg.load_entries(db, "5ka")
        self.assertNotIn("/test-crud-2/", [e.path_qs for e in entries])
        # удаление
        self.assertEqual(self.c.delete(f"/registry/{rid}").status_code, 200)
        self.assertIsNone(reg.get_url(db, rid))
        db.close()

    def test_03_bulk_actions(self):
        db = self.db()
        ids = [reg.add_url(db, "5ka", "home", f"/bulk-{i}/") for i in range(3)]
        r = self.c.post("/registry/bulk", data={"ids": ids, "action": "gone"})
        self.assertIn("msg=", r.headers["location"])
        self.assertTrue(all(reg.get_url(db, i)["status"] == "gone" for i in ids))
        self.c.post("/registry/bulk", data={"ids": ids, "action": "delete"})
        self.assertTrue(all(reg.get_url(db, i) is None for i in ids))
        r = self.c.post("/registry/bulk", data={"action": "delete"})
        self.assertIn("err=", r.headers["location"])
        db.close()

    def test_04_csv_import_preview_apply(self):
        text = ("site;type;url;note;status\n"
                "5ka;home;/imp-a/;A;active\n"
                "5ka;home;/imp-a/;дубль;active\n"
                "5ka;newtype_x;/imp-b/;B;\n"
                "5ka;home;;пустой;\n"
                "nosuch;home;/x/;;\n"
                "5ka;home;/imp-c/;C;gone\n")
        r = self.c.post("/import/preview", data={"entity": "urls", "mode": "add", "create_types": "1", "text": text})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Предпросмотр", r.text)
        db = self.db()
        self.assertEqual(reg.list_urls(db, q="imp-")[1], 0, "предпросмотр ничего не пишет")
        rep = reg.import_urls(db, text, mode="add", create_types=True, dry_run=True)
        self.assertEqual((rep["added"], rep["skipped"], len(rep["errors"])), (3, 1, 2))
        self.assertEqual(sorted(e["line"] for e in rep["errors"]), [5, 6])
        self.assertEqual(rep["new_types"], ["newtype_x"])
        r = self.c.post("/import/apply", data={"entity": "urls", "mode": "add", "create_types": "1", "text": text})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(reg.list_urls(db, q="imp-")[1], 3)
        # повтор: ничего нового
        rep = reg.import_urls(db, text, mode="add", create_types=True)
        self.assertEqual(rep["added"], 0)
        # upsert обновляет комментарий
        rep = reg.import_urls(db, "site;type;url;note\n5ka;home;/imp-a/;новый\n", mode="upsert")
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(reg.list_urls(db, q="imp-a")[0][0]["note"], "новый")
        db.close()

    def test_05_import_formats_and_replace(self):
        db = self.db()
        # старый формат registry (запятые, без site) + выбор сети
        rep = reg.import_urls(db, "# коммент\ntype,url,note\nhome,/old-fmt/,x\n", default_site="5ka")
        self.assertEqual(rep["added"], 1)
        # cp1251 и таб
        raw = "site\ttype\turl\tnote\n5ka\thome\t/tab/\tПривет\n".encode("cp1251")
        from techaudit.web.app import decode_upload
        self.assertEqual(reg.import_urls(db, decode_upload(raw))["added"], 1)
        # replace: пробный прогон считает удаляемое и не пишет
        before = reg.list_urls(db, site="5ka")[1]
        rep = reg.import_urls(db, "site;type;url\n5ka;home;/only/\n", mode="replace", dry_run=True)
        self.assertEqual(rep["deleted"], before)
        self.assertEqual(reg.list_urls(db, site="5ka")[1], before)
        # с ошибками в файле replace ничего не удаляет
        rep = reg.import_urls(db, "site;type;url\n5ka;home;/only/\n5ka;home;bad url\n", mode="replace")
        self.assertEqual(rep["deleted"], 0)
        # экспорт → импорт в режиме replace без изменений
        csv_text = reg.export_csv(db, "5ka")
        rep = reg.import_urls(db, csv_text, mode="replace", dry_run=True)
        self.assertEqual((rep["added"], rep["deleted"], len(rep["errors"])), (0, 0, 0))
        with self.assertRaises(ValueError):
            reg.import_urls(db, "foo;bar\n1;2\n")
        db.close()

    def test_06_mirror_csv_and_collector_source(self):
        db = self.db()
        reg.add_url(db, "5ka", "home", "/mirror-check/")
        reg.sync_csv_files(db, self.tmp / "registry", ["5ka"])
        body = (self.tmp / "registry" / "5ka.csv").read_text(encoding="utf-8")
        self.assertIn("/mirror-check/", body)
        self.assertIn("type,url,note", body)
        from techaudit.config import load_settings
        entries = reg.entries_for(load_settings(None), "5ka")
        self.assertIn("/mirror-check/", [e.path_qs for e in entries])
        # сеть без строк в БД → читается старый CSV
        (self.tmp / "registry" / "zz.csv").write_text("type,url\nhome,/z/\n", encoding="utf-8")
        self.assertEqual([e.path_qs for e in reg.entries_for(load_settings(None), "zz")], ["/z/"])
        db.close()

    def test_07_small_entities(self):
        db = self.db()
        reg.entity_add(db, "types", {"slug": "tt_new", "name_ru": "Новый"})
        with self.assertRaises(ValueError):
            reg.entity_add(db, "types", {"slug": "tt_new"})
        reg.entity_add(db, "owners", {"page_type": "tt_new", "email": "a@b.ru", "area": "seo"})
        reg.add_url(db, "5ka", "tt_new", "/tt/")
        with self.assertRaises(ValueError):
            reg.entity_delete(db, "types", "tt_new")  # используется
        reg.entity_update(db, "types", "tt_new", {"slug": "tt_renamed", "name_ru": "Переименован"})
        self.assertEqual(reg.list_urls(db, q="/tt/")[0][0]["page_type"], "tt_renamed")
        rep = reg.import_entity(db, "types", "slug;name_ru\ntt_renamed;Ещё раз\nbad slug!;x\n")
        self.assertEqual((rep["updated"], len(rep["errors"])), (1, 1))
        db.close()

    # ---------------------------------------------------------------- метрики

    def _fake_runs(self):
        store = Store(self.app.state.settings.data_dir)
        t0 = datetime.now().astimezone() - timedelta(days=3)
        for i, (okn, fail, ttfb) in enumerate([(8, 2, 300), (9, 1, 250), (10, 0, 200)]):
            rid = f"2026010{i + 1}-000000-prod-t"
            store.db.execute("INSERT INTO runs(run_id, started_at, env, options) VALUES (?,?,?,?)",
                             (rid, (t0 + timedelta(days=i)).isoformat(timespec="seconds"), "prod",
                              json.dumps({"limit": 2, "variants": ["plain", "bot"], "networks": {"5ka": {}}})))
            for k in range(10):
                store.add_page({"run_id": rid, "network": "5ka", "page_type": "home", "variant": "bot", "mode": "raw",
                                "source_url": "/hist/", "url": "https://5ka.example/hist/",
                                "status": 200 if k < okn else 500, "ttfb_ms": ttfb, "bytes": 2048,
                                "has_title": 1, "has_h1": 1, "fetched_at": "2026-01-01T00:00:00+00:00"})
            store.add_probe({"run_id": rid, "network": "5ka", "kind": "slash", "url": "u", "verdict": "pass" if i else "fail",
                             "fetched_at": "2026-01-01T00:00:00+00:00"})
            for cid, st in (("c_a", "pass" if i else "fail"), ("c_b", "fail" if i == 2 else "pass"), ("c_c", "skipped")):
                store.db.execute("INSERT INTO check_results(run_id,network,check_id,title,status,summary,items,source,created_at)"
                                 " VALUES (?,?,?,?,?,?,?,?,?)",
                                 (rid, "5ka", cid, f"Проверка {cid}", st, "итог", json.dumps([{"url": "/u", "ok": st == "pass", "msg": "m"}]),
                                  "html", "2026-01-01T00:00:00+00:00"))
        store.db.commit()
        store.close()

    def test_08_metrics_series_and_changes(self):
        self._fake_runs()
        db = self.db()
        d = mx.dashboard(db, "5ka", 10)
        ids = [r["run_id"] for r in d["runs"] if r["run_id"].endswith("-t")]
        self.assertEqual(len(ids), 3)
        s = d["series"]
        tail = lambda m: s[m][-3:]  # noqa: E731
        self.assertEqual(tail("pages_ok_share"), [80.0, 90.0, 100.0])
        self.assertEqual(tail("ttfb_avg"), [300.0, 250.0, 200.0])
        self.assertEqual(tail("probe:slash"), [0.0, 100.0, 100.0])
        self.assertEqual(tail("checks_fail"), [1, 0, 1])
        self.assertEqual(tail("checks_score"), [50.0, 100.0, 50.0])
        card = {c["id"]: c for c in d["cards"]}["ttfb_avg"]
        self.assertEqual(card["trend"], "good")  # TTFB упал — это хорошо
        m = mx.checks_matrix(db, "5ka", 5)
        kinds = {c["check_id"]: c["kind"] for c in m["changes"]}
        self.assertEqual(kinds.get("c_b"), "regress")  # pass → fail
        det = mx.check_detail(db, ids[-1], "5ka", "c_b")
        self.assertEqual(det["status"], "fail")
        hist = mx.url_history(db, "5ka", "/hist/")
        self.assertEqual(len(hist["runs"]), 3)
        db.close()
        for path in ("/", "/?net=5ka", f"/runs/{ids[-1]}", "/history?net=5ka&url=/hist/",
                     "/api/dashboard?net=5ka", "/api/series?metrics=ttfb_avg&net=5ka", "/checks?net=5ka",
                     f"/checks/detail?net=5ka&run={ids[-1]}&check=c_b"):
            self.assertEqual(self.c.get(path).status_code, 200, path)

    # ---------------------------------------------------------------- расписание, задания, защита

    def test_09_schedule_math(self):
        now = datetime(2026, 10, 22, 8, 0)  # четверг
        self.assertEqual(jb.compute_next("daily", "07:00", "1,2,3,4,5,6,7", None, now), datetime(2026, 10, 23, 7, 0))
        self.assertEqual(jb.compute_next("daily", "09:30", "4", None, now), datetime(2026, 10, 22, 9, 30))
        self.assertEqual(jb.compute_next("daily", "09:30", "1", None, now), datetime(2026, 10, 26, 9, 30))
        self.assertEqual(jb.compute_next("interval", None, None, 6, now), datetime(2026, 10, 22, 14, 0))
        for bad in (("daily", "25:00", "1", None), ("interval", None, None, 0)):
            with self.assertRaises(ValueError):
                jb.compute_next(*bad, now)

    def test_10_scheduler_tick_and_job_queue(self):
        r = self.c.post("/schedules/add", data={"name": "t", "mode": "interval", "every_hours": "1", "networks": ["5ka"],
                                                "limit": "2", "checks": "1"})
        self.assertIn("msg=", r.headers["location"])
        db = self.db()
        sid = db.execute("SELECT id FROM web_schedules WHERE name='t'").fetchone()["id"]
        db.execute("UPDATE web_schedules SET next_run_at=? WHERE id=?",
                   ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), sid))
        mgr = self.app.state.jobs
        made = mgr.tick()
        self.assertEqual(len(made), 1)
        self.assertEqual(mgr.tick(), [], "повторно не ставится: следующий запуск уже в будущем")
        job = db.execute("SELECT * FROM web_jobs WHERE id=?", (made[0],)).fetchone()
        self.assertEqual((job["status"], job["trigger"], job["schedule_id"]), ("queued", "schedule", sid))
        params = json.loads(job["params"])
        self.assertEqual((params["networks"], params["limit"]), (["5ka"], 2))
        # занятое расписание не копит очередь
        db.execute("UPDATE web_schedules SET next_run_at=? WHERE id=?",
                   ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), sid))
        self.assertEqual(mgr.tick(), [])
        self.assertEqual(self.c.get(f"/jobs/{made[0]}/log").status_code, 200)
        self.assertTrue(mgr.cancel(made[0]))
        self.assertEqual(self.c.get(f"/jobs/{made[0]}/log").status_code, 286)
        self.assertEqual(self.c.post(f"/schedules/{sid}/toggle").status_code, 303)
        self.assertEqual(self.c.post(f"/schedules/{sid}/delete").status_code, 303)
        db.close()

    def test_11_commands_have_no_secrets(self):
        p = jb.clean_params({"networks": ["5ka"], "limit": "5", "token": "off", "ua": "yandexbot;rm -rf", "live": "1"})
        self.assertEqual(p["ua"], "yandexbotrm-rf")  # из UA вычищено всё, кроме безопасных символов
        cmds = jb.build_commands(p, "RUN1")
        flat = " ".join(" ".join(c) for c in cmds)
        self.assertNotIn("SECRET", flat)
        self.assertIn("--no-token", flat)
        self.assertIn("--run RUN1", flat)
        self.assertIn("--limit 5", flat)

    def test_12_security(self):
        self.assertEqual(self.c.get("/", headers={"host": "evil.example"}).status_code, 400)
        r = self.c.post("/registry/add", headers={"origin": "https://evil.example"},
                        data={"site": "5ka", "page_type": "home", "url": "/csrf/"})
        self.assertEqual(r.status_code, 403)
        for path in ("/settings", "/runs", "/"):
            self.assertNotIn("SECRET-TOKEN-VALUE", self.c.get(path).text)
        self.assertIn("задан", self.c.get("/settings").text)
        # XSS: комментарий экранируется
        db = self.db()
        reg.add_url(db, "5ka", "home", "/xss/", note="<script>alert(1)</script>")
        html = self.c.get("/registry?q=xss").text
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        db.close()


if __name__ == "__main__":
    unittest.main()
