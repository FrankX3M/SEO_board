"""Флажки noindex / nofollow в справочнике: БД, CSV, веб-форма и влияние на проверки.

    python -m pytest tests/test_noindex_flags.py
"""
import os
import re
import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from techaudit import db_registry as reg
from techaudit.checks.core import FAIL, PASS, CheckSettings
from techaudit.checks.runner import load_all, run_checks
from techaudit.config import load_settings
from techaudit.storage import Store, now_iso

PAGE = """<html><head><title>Карточка клуба</title><meta name="robots" content="noindex,nofollow">
<link rel="canonical" href="https://shop.test/card/"></head><body><p>Текст без заголовка первого уровня.</p></body></html>"""


class FlagsInChecksTest(unittest.TestCase):
    """Страница с флажком noindex: директива — норма, но отсутствие H1 всё равно считается ошибкой."""

    @classmethod
    def setUpClass(cls):
        load_all()
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name) / "data"
        env = Path(cls.tmp.name) / ".env"
        env.write_text("NETWORKS=shop\nNET_SHOP_URL=https://shop.test\nNET_SHOP_TOKEN=t\n", encoding="utf-8")
        os.environ["DATA_DIR"] = str(data)
        cls.settings = load_settings(str(env))
        cls.store = Store(data)
        cls.store.start_run("r1", "prod", {})
        cls.store.db.executescript(reg.SCHEMA)
        reg.migrate(cls.store.db)
        rel = cls.store.save_html("r1", "shop", "card", "https://shop.test/card/", "bot", PAGE.encode("utf-8"))
        cls.store.add_page(dict(run_id="r1", network="shop", page_type="card", variant="bot", ua_name="x", mode="raw",
                                source_url="/card/", url="https://shop.test/card/", status=200,
                                final_url="https://shop.test/card/", chain="[]", headers="{}", ttfb_ms=10, total_ms=10,
                                bytes=len(PAGE), sha256="x", file_path=rel, has_title=1, has_h1=0, suspect="", error=None,
                                fetched_at=now_iso()))
        cls.store.db.commit()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DATA_DIR", None)
        cls.store.close()
        cls.tmp.cleanup()

    def run_all(self):
        return run_checks(self.store, self.settings, "r1", "shop")

    def set_flags(self, noindex, nofollow):
        db = self.store.db
        db.execute("DELETE FROM ref_urls")
        db.execute("INSERT INTO ref_sites(site,name_ru,host) VALUES ('shop','shop','') ON CONFLICT DO NOTHING")
        db.execute("INSERT INTO ref_urls(site,page_type,url,path_qs,note,status,noindex,nofollow,created_at,updated_at)"
                   " VALUES ('shop','card','/card/','/card/','','active',?,?,'t','t')", (noindex, nofollow))
        db.commit()

    def test_without_flags_noindex_is_error(self):
        self.set_flags(0, 0)
        res = self.run_all()
        self.assertEqual(res["noindex_accidental"].status, FAIL)
        self.assertEqual(res["nofollow_accidental"].status, FAIL)

    def test_flags_make_directives_normal_but_h1_still_counted(self):
        self.set_flags(1, 1)
        res = self.run_all()
        self.assertNotEqual(res["noindex_accidental"].status, FAIL)
        self.assertNotEqual(res["nofollow_accidental"].status, FAIL)
        self.assertEqual(res["noindex_where_needed"].status, PASS)  # директива действительно стоит
        self.assertEqual(res["h1_single"].status, FAIL)  # H1 нет — ошибка, хотя страница noindex
        self.assertIn("/card/", " ".join(i.url for i in res["h1_single"].items))


@unittest.skipIf(TestClient is None, "нет fastapi/httpx")
class FlagsWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_web import make_app
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

    def test_roundtrip_add_edit_bulk_csv(self):
        db = self.db()
        uid = reg.add_url(db, "5ka", "home", "/flag-a/", noindex=1)
        self.assertEqual(reg.get_url(db, uid)["noindex"], 1)
        self.assertEqual(reg.get_url(db, uid)["nofollow"], 0)
        # форма правки показывает галочки; сетка колонок не меняется (9 ячеек + colspan)
        html = self.c.get(f"/registry/{uid}/edit").text
        self.assertRegex(html, r'name="noindex" value="1" checked')
        self.assertNotRegex(html, r'name="nofollow" value="1" checked')
        # сохранение: noindex снят, nofollow включён
        r = self.c.post(f"/registry/{uid}/save", data={"site": "5ka", "page_type": "home", "url": "/flag-a/", "note": "",
                                                        "status": "active", "nofollow": "1"})
        self.assertEqual(r.status_code, 200)
        row = reg.get_url(db, uid)
        self.assertEqual((row["noindex"], row["nofollow"]), (0, 1))
        self.assertIn("nofollow", r.text)
        # массовое включение / снятие
        r = self.c.post("/registry/bulk", data={"ids": [str(uid)], "action": "noindex_on", "back": "/registry"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(reg.get_url(db, uid)["noindex"], 1)
        # фильтр
        rows, total = reg.list_urls(db, robots="noindex")
        self.assertIn(uid, [x["id"] for x in rows])
        page = self.c.get("/registry?robots=noindex").text
        self.assertIn("/flag-a/", page)
        # экспорт → импорт (upsert) сохраняет флажки
        text = reg.export_csv(db, "5ka")
        self.assertIn(";noindex;nofollow", text.splitlines()[0])
        reg.set_flag(db, [uid], "noindex", 0)
        reg.set_flag(db, [uid], "nofollow", 0)
        rep = reg.import_urls(db, text, mode="upsert")
        self.assertFalse(rep["errors"], rep["errors"])
        row = reg.get_url(db, uid)
        self.assertEqual((row["noindex"], row["nofollow"]), (1, 1))
        # сборщик получает флажки
        entries = {e.path_qs: e for e in reg.load_entries(db, "5ka")}
        self.assertTrue(entries["/flag-a/"].noindex and entries["/flag-a/"].nofollow)
        db.close()

    def test_import_without_flag_columns_keeps_flags(self):
        db = self.db()
        uid = reg.add_url(db, "5ka", "home", "/flag-b/", noindex=1)
        reg.import_urls(db, "site;type;url;note\n5ka;home;/flag-b/;заметка\n", mode="upsert")
        self.assertEqual(reg.get_url(db, uid)["noindex"], 1)
        db.close()

    def test_registry_page_layout(self):
        html = self.c.get("/registry").text
        self.assertIn('class="reg"', html)
        self.assertEqual(len(re.findall(r"<col ", html)), 9)
        self.assertIn("Индексация", html)


if __name__ == "__main__":
    unittest.main()
