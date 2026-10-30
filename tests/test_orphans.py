"""Тесты модуля «Сироты»: нормализация адресов, чтение файлов, API Вебмастера (на заглушке), сравнение с краулом, интерфейс.

    python -m pytest tests/test_orphans.py
Сайт и внешние сервисы не трогаются.
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx
from urllib.parse import quote

from techaudit import crawl as cr
from techaudit import indexed as ix
from techaudit import orphans as orph

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

HOST = "5ka.example"


def mem_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    orph.ensure_schema(db)
    return db


CRAWL_CSV = (
    "Address,Content Type,Status Code,Indexability,Indexability Status,Crawl Depth,Inlinks,Unique Inlinks,Redirect URL\n"
    f"https://{HOST}/,text/html,200,Indexable,,0,0,0,\n"
    f"https://{HOST}/catalog/,text/html,200,Indexable,,1,50,40,\n"
    f"https://{HOST}/catalog/milk/,text/html,200,Indexable,,2,10,8,\n"
    f"https://{HOST}/deep/page/,text/html,200,Indexable,,7,1,1,\n"
    f"https://{HOST}/sitemap-only/,text/html,200,Indexable,,,0,0,\n"
    f"https://{HOST}/old/,text/html,301,Non-Indexable,Redirected,1,3,3,https://{HOST}/catalog/\n"
    f"https://{HOST}/hidden/,text/html,200,Non-Indexable,Noindex,2,4,4,\n"
    f"https://{HOST}/Только-в-краулe/,text/html,200,Indexable,,1,2,2,\n"
)


def load_crawl(db) -> int:
    return cr.import_csv(db, "5ka", io.BytesIO(CRAWL_CSV.encode("utf-8")), host=HOST)["import_id"]


def load_index(db, source, urls, **kw) -> int:
    return ix.save_pages(db, "5ka", source, [{"url": u} for u in urls], host=HOST, origin="file", **kw)["import_id"]


class NormalizeTest(unittest.TestCase):
    def test_equivalent_forms(self):
        base = ix.normalize("https://5ka.example/catalog/milk/")
        for u in ("http://www.5ka.example/catalog/milk", "https://5KA.example:443/catalog/milk/#top", "https://5ka.example/catalog//milk/",
                  "https://5ka.example/catalog/milk/?utm_source=x&yclid=1"):
            self.assertEqual(ix.normalize(u), base, u)

    def test_cyrillic_and_query(self):
        self.assertEqual(ix.normalize("https://5ka.example/%D0%BC%D0%BE%D0%BB%D0%BE%D0%BA%D0%BE/"), ix.normalize("https://5ka.example/молоко"))
        self.assertEqual(ix.normalize("https://5ka.example/s?b=2&a=1"), ix.normalize("https://5ka.example/s?a=1&b=2"))
        self.assertNotEqual(ix.normalize("https://5ka.example/s?a=1"), ix.normalize("https://5ka.example/s"))

    def test_case_of_path_kept(self):
        self.assertNotEqual(ix.normalize("https://5ka.example/A"), ix.normalize("https://5ka.example/a"))

    def test_section(self):
        self.assertEqual(ix.section_of(ix.normalize("https://5ka.example/catalog/milk/")), "/catalog")
        self.assertEqual(ix.section_of(ix.normalize("https://5ka.example/")), "/")


class FilesTest(unittest.TestCase):
    def read(self, text, name="x.csv"):
        return list(ix.read_file(io.BytesIO(text.encode("utf-8-sig")), name))

    def test_semicolon_csv_with_russian_headers(self):
        r = self.read("Страница;Показы;Клики\nhttps://5ka.example/a/;10;2\nhttps://5ka.example/b/;5;0\n")
        self.assertEqual([x["url"] for x in r], ["https://5ka.example/a/", "https://5ka.example/b/"])
        self.assertEqual(r[0]["impressions"], "10")

    def test_comma_csv_gsc(self):
        r = self.read("URL,Last crawled\nhttps://5ka.example/a/,2026-10-01\n")
        self.assertEqual(r[0]["last_access"], "2026-10-01")

    def test_plain_list(self):
        r = self.read("https://5ka.example/a/\nhttps://5ka.example/b/\n", "list.txt")
        self.assertEqual(len(r), 2)

    def test_no_url_column(self):
        with self.assertRaises(ix.IndexedError):
            self.read("Имя;Значение\nа;б\n")

    def test_xlsx(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["URL", "Title"])
        ws.append(["https://5ka.example/a/", "Страница А"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        r = list(ix.read_file(buf, "ex.xlsx"))
        self.assertEqual((r[0]["url"], r[0]["title"]), ("https://5ka.example/a/", "Страница А"))

    def test_foreign_hosts_dropped_and_duplicates_collapsed(self):
        db = mem_db()
        res = ix.save_pages(db, "5ka", "ywm", [{"url": "https://5ka.example/a/"}, {"url": "http://www.5ka.example/a"},
                                                {"url": "https://other.example/a/"}], host=HOST, origin="file")
        self.assertEqual((res["rows"], res["skipped_external"]), (1, 1))

    def test_empty_import_rolls_back(self):
        db = mem_db()
        with self.assertRaises(ix.IndexedError):
            ix.save_pages(db, "5ka", "ywm", [{"url": "https://other.example/a/"}], host=HOST, origin="file")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM index_imports").fetchone()[0], 0)


class YwmTest(unittest.TestCase):
    def client(self, total=250, calls=None):
        def handler(req: httpx.Request) -> httpx.Response:
            if calls is not None:
                calls.append(str(req.url))
            assert req.headers["Authorization"] == "OAuth TOKEN"
            p = req.url.path
            if p == "/v4/user":
                return httpx.Response(200, json={"user_id": 42})
            if p == "/v4/user/42/hosts":
                return httpx.Response(200, json={"hosts": [
                    {"host_id": "http:5ka.example:80", "verified": True},
                    {"host_id": "https:www.5ka.example:443", "verified": True},
                    {"host_id": "https:other.example:443", "verified": True}]})
            if p.endswith("/search-urls/in-search/samples"):
                off, lim = int(req.url.params["offset"]), int(req.url.params["limit"])
                assert lim <= 100
                items = [{"url": f"https://5ka.example/p/{i}/", "title": f"T{i}", "last_access": "2026-10-01T00:00:00,000+0300"}
                         for i in range(off, min(off + lim, total))]
                return httpx.Response(200, json={"count": total, "samples": items})
            return httpx.Response(404, json={"error_code": "NOT_FOUND"})
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_paging_and_host_lookup(self):
        db = mem_db()
        calls = []
        res = ix.fetch_ywm(db, "5ka", f"https://{HOST}", "TOKEN", client=self.client(250, calls))
        self.assertEqual(res["rows"], 250)
        self.assertEqual(res["host_id"], "https:www.5ka.example:443")  # https предпочтительнее, www не мешает
        self.assertFalse(res["truncated"])
        self.assertTrue(any("offset=200" in c for c in calls))
        self.assertEqual(ix.latest(db, "5ka", "ywm")["rows"], 250)

    def test_unknown_host(self):
        with self.assertRaises(ix.IndexedError) as cm:
            ix.fetch_ywm(mem_db(), "x", "https://nothere.example", "TOKEN", client=self.client())
        self.assertIn("не найден", str(cm.exception))

    def test_no_token(self):
        with self.assertRaises(ix.IndexedError):
            ix.fetch_ywm(mem_db(), "5ka", f"https://{HOST}", "")

    def test_bad_token_message(self):
        c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error_code": "FORBIDDEN", "error_message": "no"})))
        with self.assertRaises(ix.IndexedError) as cm:
            ix.fetch_ywm(mem_db(), "5ka", f"https://{HOST}", "TOKEN", client=c)
        self.assertIn("отклонил токен", str(cm.exception))


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.db = mem_db()
        self.crawl_id = load_crawl(self.db)

    def cats(self, res):
        return {r["norm"].split("/", 1)[1] or "/": r["category"] for r in res["rows"]}

    def test_categories(self):
        yw = load_index(self.db, "ywm", [
            f"https://{HOST}/", f"https://www.{HOST}/catalog/milk", f"https://{HOST}/deep/page/", f"https://{HOST}/sitemap-only/",
            f"https://{HOST}/old/", f"https://{HOST}/hidden/", f"https://{HOST}/lost/page/", f"https://{HOST}/catalog/?utm_source=a"])
        res = orph.compare(self.db, self.crawl_id, {"ywm": yw}, max_depth=4)
        c = self.cats(res)
        self.assertEqual(c["/"], "ok")                    # главная: входящих 0, но глубина 0
        self.assertEqual(c["catalog/milk"], "ok")         # www и слеш не мешают
        self.assertEqual(c["deep/page"], "deep")
        self.assertEqual(c["sitemap-only"], "no_inlinks")
        self.assertEqual(c["old"], "bad_status")
        self.assertEqual(c["hidden"], "non_indexable")
        self.assertEqual(c["lost/page"], "not_in_crawl")
        self.assertEqual(c["catalog"], "ok")              # utm-метка отброшена
        s = res["summary"]
        self.assertEqual(s["index_total"], 8)
        self.assertEqual(s["orphans"], 3)
        self.assertEqual(s["in_crawl"], 7)

    def test_depth_threshold(self):
        yw = load_index(self.db, "ywm", [f"https://{HOST}/deep/page/"])
        self.assertEqual(self.cats(orph.compare(self.db, self.crawl_id, {"ywm": yw}, max_depth=8))["deep/page"], "ok")

    def test_two_sources_merge(self):
        yw = load_index(self.db, "ywm", [f"https://{HOST}/lost/", f"https://{HOST}/catalog/"])
        g = ix.save_pages(self.db, "5ka", "gsc", [{"url": f"https://{HOST}/lost", "impressions": 9, "clicks": 1},
                                                  {"url": f"https://{HOST}/g-only/", "impressions": 3}], host=HOST, origin="api")["import_id"]
        res = orph.compare(self.db, self.crawl_id, {"ywm": yw, "gsc": g})
        by = {r["norm"].split("/", 1)[1]: r for r in res["rows"]}
        self.assertEqual((by["lost"]["in_ywm"], by["lost"]["in_gsc"], by["lost"]["impressions"]), (1, 1, 9))
        self.assertEqual(res["summary"]["in_both"], 1)
        self.assertEqual(res["summary"]["index_total"], 3)
        self.assertEqual(by["g-only"]["category"], "not_in_crawl")

    def test_probe_splits_live_from_stale(self):
        yw = load_index(self.db, "ywm", [f"https://{HOST}/live/", f"https://{HOST}/gone/", f"https://{HOST}/moved/"])
        probes = {ix.normalize(f"https://{HOST}/live/"): (200, None), ix.normalize(f"https://{HOST}/gone/"): (404, None),
                  ix.normalize(f"https://{HOST}/moved/"): (301, "https://5ka.example/new/")}
        c = self.cats(orph.compare(self.db, self.crawl_id, {"ywm": yw}, probes=probes))
        self.assertEqual((c["live"], c["gone"], c["moved"]), ("not_in_crawl", "stale", "stale"))

    def test_cyrillic_match(self):
        yw = load_index(self.db, "ywm", [f"https://{HOST}" + quote("/Только-в-краулe/")])
        res = orph.compare(self.db, self.crawl_id, {"ywm": yw})
        self.assertEqual(res["rows"][0]["category"], "ok")

    def test_low_coverage_warning(self):
        yw = load_index(self.db, "ywm", [f"https://{HOST}/lost/{i}/" for i in range(5)] + [f"https://{HOST}/catalog/"])
        res = orph.compare(self.db, self.crawl_id, {"ywm": yw})
        self.assertTrue(any("неполн" in w or "Краул нашёл только" in w for w in res["summary"]["warnings"]))

    def test_store_and_query(self):
        yw = load_index(self.db, "ywm", [f"https://{HOST}/lost/", f"https://{HOST}/catalog/", f"https://{HOST}/deep/page/"])
        res = orph.run_compare(self.db, type("S", (), {"networks": {}})(), "5ka", save=True)
        rid = res["run_id"]
        rows, total = orph.rows(self.db, rid, "orphans")
        self.assertEqual(total, 2)
        self.assertEqual(orph.rows(self.db, rid, "not_in_crawl", "gsc")[1], 0)
        self.assertEqual(orph.rows(self.db, rid, "all", "ywm", "lost")[1], 1)
        self.assertEqual(orph.sections(self.db, rid)[0]["n"], 1)
        self.assertEqual(len(orph.export_row(rows[0])), len(orph.EXPORT_HEAD))
        orph.delete_run(self.db, rid)
        self.assertEqual(orph.get_run(self.db, rid), None)

    def test_no_index_imports(self):
        with self.assertRaises(ix.IndexedError):
            orph.run_compare(self.db, None, "5ka")

    def test_no_crawl(self):
        db = mem_db()
        load_index(db, "ywm", [f"https://{HOST}/a/"])
        with self.assertRaises(ix.IndexedError):
            orph.run_compare(db, None, "5ka")


@unittest.skipIf(TestClient is None, "нет fastapi/httpx")
class WebOrphansTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        os.environ.update({"TECHAUDIT_ENV": os.devnull, "NETWORKS": "5ka", "NET_5KA_URL": f"https://{HOST}", "NET_5KA_TOKEN": "SECRET-TOKEN-VALUE",
                           "DATA_DIR": str(tmp / "data"), "REGISTRY_DIR": str(tmp / "registry")})
        os.environ.pop("YANDEX_WEBMASTER_TOKEN", None)
        from techaudit.web.app import create_app
        cls.app = create_app(None, start_jobs=False)
        cls.c = TestClient(cls.app, follow_redirects=False)
        from techaudit.webdb import connect
        db = connect(cls.app.state.db_path)
        cls.crawl_id = load_crawl(db)
        db.close()

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        cls._tmp.cleanup()

    def test_flow(self):
        r = self.c.get("/orphans?net=5ka")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Страницы-сироты", r.text)
        self.assertIn("нет токена", r.text)  # токена Вебмастера нет — кнопка выключена, подсказка есть
        body = (f"URL;Заголовок\nhttps://{HOST}/lost/;Потерянная\nhttps://{HOST}/catalog/;Каталог\nhttps://{HOST}/deep/page/;Глубокая\n").encode("utf-8-sig")
        r = self.c.post("/orphans/upload", data={"network": "5ka", "source": "ywm"}, files={"file": ("ywm.csv", body, "text/csv")})
        self.assertEqual(r.status_code, 303, r.text)
        self.assertIn("msg=", r.headers["location"])
        r = self.c.post("/orphans/compare", data={"network": "5ka", "probe": "0"})
        self.assertEqual(r.status_code, 303, r.text)
        loc = r.headers["location"].split("?")[0]
        page = self.c.get(loc)
        self.assertEqual(page.status_code, 200)
        self.assertIn("lost", page.text)
        self.assertIn("Слишком глубоко", page.text)
        self.assertNotIn("SECRET-TOKEN-VALUE", page.text)
        csv_ = self.c.get(loc + "/export.csv?cat=all")
        self.assertEqual(csv_.status_code, 200)
        self.assertIn("lost", csv_.content.decode("utf-8-sig"))
        x = self.c.get(loc + "/export.xlsx")
        self.assertEqual(x.status_code, 200)
        self.assertTrue(x.content.startswith(b"PK"))
        self.assertEqual(self.c.get(loc + "?cat=deep&src=ywm&q=deep").status_code, 200)
        self.assertEqual(self.c.get("/orphans/99999").status_code, 404)

    def test_bad_upload(self):
        r = self.c.post("/orphans/upload", data={"network": "5ka", "source": "ywm"}, files={"file": ("x.csv", b"a;b\n1;2\n", "text/csv")})
        self.assertEqual(r.status_code, 303)
        self.assertIn("err=", r.headers["location"])

    def test_fetch_without_token_is_message_not_crash(self):
        r = self.c.post("/orphans/fetch/ywm", data={"network": "5ka"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("err=", r.headers["location"])


class CheckTest(unittest.TestCase):
    """Проверка index_orphans в отчётах: зарегистрирована, есть в плане работ, статусы по краулу и выгрузкам индекса."""

    def ctx(self, db, imp):
        from types import SimpleNamespace
        c = SimpleNamespace(db=db, network="5ka", cs=SimpleNamespace(max_depth=4))
        if imp is not None:
            c._crawl_imp = imp
        return c

    def run_check(self, ctx):
        from techaudit.checks.core import REGISTRY
        from techaudit.checks.runner import load_all
        load_all()
        return REGISTRY["index_orphans"].fn(ctx)

    def test_registered_and_planned(self):
        from techaudit.checks import plan
        from techaudit.checks.core import REGISTRY
        from techaudit.checks.extras import EXTRA
        from techaudit.checks.runner import load_all
        load_all()
        self.assertIn("index_orphans", REGISTRY)
        self.assertIn("index_orphans", EXTRA)
        self.assertIn("index_orphans", plan.PLAN)

    def test_skipped_without_crawl_or_index(self):
        from techaudit.checks.core import SKIPPED
        db = mem_db()
        cid = load_crawl(db)
        imp = dict(db.execute("SELECT * FROM crawl_imports WHERE id=?", (cid,)).fetchone())
        r = self.run_check(self.ctx(db, imp))
        self.assertEqual(r.status, SKIPPED)
        self.assertIn("выгрузок индекса", r.summary)

    def test_orphans_reported(self):
        from techaudit.checks.core import FAIL, PASS
        db = mem_db()
        cid = load_crawl(db)
        imp = dict(db.execute("SELECT * FROM crawl_imports WHERE id=?", (cid,)).fetchone())
        load_index(db, "ywm", [f"https://{HOST}/catalog/", f"https://{HOST}/catalog/milk/", f"https://{HOST}/lost/", f"https://{HOST}/deep/page/"])
        r = self.run_check(self.ctx(db, imp))
        self.assertEqual(r.status, FAIL)  # 2 из 4 — больше 5 %
        self.assertEqual({i.url for i in r.items}, {f"https://{HOST}/lost/", f"https://{HOST}/deep/page/"})
        self.assertEqual(r.stats, {"total": 4, "bad": 2})
        db.execute("DELETE FROM index_pages")
        db.execute("DELETE FROM index_imports")
        load_index(db, "ywm", [f"https://{HOST}/catalog/", f"https://{HOST}/catalog/milk/"])
        self.assertEqual(self.run_check(self.ctx(db, imp)).status, PASS)


if __name__ == "__main__":
    unittest.main()
