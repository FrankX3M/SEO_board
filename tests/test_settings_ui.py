"""Настройки через веб-интерфейс: хранение в БД, подстановка в окружение, карты сайта, маскирование секретов.

    python -m unittest tests.test_settings_ui
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from techaudit import appsettings as aps
from techaudit.collect import main
from techaudit.config import load_settings, resolve_access
from tests import mock_site

SECRET = "ULTRA-SECRET-TOKEN-123"


class EnvCase(unittest.TestCase):
    """Каждый тест — чистое окружение и своя папка данных."""

    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for k in [k for k in os.environ if k.startswith(("NET_", "UA_")) or k in ("NETWORKS", "SP_TOKEN", "TTFB_MAX_MS")]:
            del os.environ[k]
        os.environ.update({"TECHAUDIT_ENV": os.devnull, "DATA_DIR": str(self.root / "data"),
                           "REGISTRY_DIR": str(self.root / "registry")})
        aps._applied.clear()

    def tearDown(self):
        aps._restore_env()
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def db(self):
        from techaudit.webdb import connect, init_all
        return connect(init_all(self.root / "data"))


class AppSettingsTest(EnvCase):
    def test_db_values_override_env_and_are_rolled_back(self):
        os.environ["TTFB_MAX_MS"] = "111"
        db = self.db()
        aps.save_group(db, "thresholds", {"TTFB_MAX_MS": "999"})
        load_settings()
        self.assertEqual(os.environ["TTFB_MAX_MS"], "999")
        db.execute("DELETE FROM app_settings")
        load_settings()
        self.assertEqual(os.environ["TTFB_MAX_MS"], "111")  # вернулось значение из окружения

    def test_networks_from_db_with_sitemaps_and_token(self):
        db = self.db()
        aps.save_network(db, {"name": "shop", "url": "shop.example.ru/", "token": SECRET, "sitemaps": "/a.xml\nhttps://cdn.example.ru/b.xml, /a.xml",
                              "sitemaps_robots": "1"})
        st = load_settings()
        n = st.networks["shop"]
        self.assertEqual(n.url, "https://shop.example.ru")
        self.assertEqual(n.token, SECRET)
        self.assertEqual(n.sitemap_urls(), ["https://shop.example.ru/a.xml", "https://cdn.example.ru/b.xml"])
        self.assertTrue(n.sitemaps_robots)
        self.assertFalse(n.sitemaps_fallback)  # флажок не передан = выключен
        acc = resolve_access(st, n)
        self.assertEqual(acc.sitemaps, ("https://shop.example.ru/a.xml", "https://cdn.example.ru/b.xml"))
        self.assertNotIn(SECRET, str(acc.describe()))

    def test_env_networks_are_used_until_db_has_some_and_can_be_imported(self):
        os.environ.update({"NETWORKS": "old", "NET_OLD_URL": "https://old.example.ru", "NET_OLD_TOKEN": "t-old",
                           "NET_OLD_SITEMAPS": "/m.xml", "TTFB_MAX_MS": "700"})
        st = load_settings()
        self.assertEqual(list(st.networks), ["old"])
        self.assertEqual(st.networks["old"].sitemaps, ("/m.xml",))
        db = self.db()
        rep = aps.import_from_env(db)
        self.assertEqual(rep["networks"], 1)
        row = aps.get_network(db, "old")
        self.assertEqual((row["token"], row["sitemaps"]), ("t-old", "/m.xml"))
        self.assertEqual(aps.get_settings(db)["TTFB_MAX_MS"], "700")
        aps.save_network(db, {"name": "new", "url": "https://new.example.ru"})
        self.assertEqual(list(load_settings().networks), ["old", "new"])

    def test_secret_semantics(self):
        db = self.db()
        aps.save_group(db, "access", {"SP_TOKEN": SECRET})
        aps.save_group(db, "access", {"SP_TOKEN": ""})  # пусто = не менять
        self.assertEqual(aps.get_settings(db)["SP_TOKEN"], SECRET)
        aps.save_group(db, "access", {"clear__SP_TOKEN": "1"})
        self.assertNotIn("SP_TOKEN", aps.get_settings(db))

    def test_validation(self):
        db = self.db()
        with self.assertRaises(ValueError):
            aps.save_group(db, "requests", {"CONCURRENCY": "много"})
        with self.assertRaises(ValueError):
            aps.save_network(db, {"name": "bad name", "url": "https://x.ru"})
        with self.assertRaises(ValueError):
            aps.save_network(db, {"name": "x", "url": "https://x.ru", "sitemaps": "javascript:alert(1)"})
        aps.save_network(db, {"name": "x", "url": "https://x.ru"})
        with self.assertRaises(ValueError):
            aps.save_network(db, {"name": "x", "url": "https://x.ru"})  # дубль

    def test_custom_ua(self):
        db = self.db()
        aps.add_ua(db, "my_bot", "Mozilla/5.0 (compatible; MyBot/1.0)", {"googlebot"})
        self.assertIn("my-bot", load_settings().ua_profiles)
        with self.assertRaises(ValueError):
            aps.add_ua(db, "googlebot", "Mozilla/5.0 something", {"googlebot"})
        aps.delete_ua(db, "my-bot")
        self.assertNotIn("my-bot", load_settings().ua_profiles)

    def test_edit_keeps_token_when_field_empty(self):
        db = self.db()
        aps.save_network(db, {"name": "s", "url": "https://s.ru", "token": SECRET})
        aps.save_network(db, {"name": "s", "url": "https://s.ru", "token": "", "sitemaps": "/x.xml"}, original="s")
        self.assertEqual(aps.get_network(db, "s")["token"], SECRET)
        aps.save_network(db, {"name": "s", "url": "https://s.ru", "clear_token": "1"}, original="s")
        self.assertEqual(aps.get_network(db, "s")["token"], "")


class SitemapDiscoveryTest(EnvCase):
    """Карты, заданные в настройках сети, собираются пробами даже если их нет в robots.txt."""

    def setUp(self):
        super().setUp()
        self.srv, self.base = mock_site.start()
        (self.root / "registry").mkdir()
        (self.root / "registry" / "mock.csv").write_text("type,url,note\nhome,/,\n", encoding="utf-8")
        mock_site.Handler.seen.clear()

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def run_probes(self):
        with redirect_stdout(io.StringIO()):
            code = main(["--network", "mock", "--variants", "plain", "--probes", "discovery"], env_file=os.devnull)
        self.assertEqual(code, 0)
        db = sqlite3.connect(self.root / "data" / "techaudit.db")
        db.row_factory = sqlite3.Row
        return {r["url"].replace(self.base, ""): r for r in db.execute("SELECT * FROM probes WHERE kind='sitemap'")}

    def save(self, **kw):
        db = self.db()
        aps.save_network(db, {"name": "mock", "url": self.base, "token": mock_site.TOKEN, **kw})
        db.close()

    def test_manual_sitemap_added_to_robots_ones(self):
        self.save(sitemaps="/custom/map.xml", sitemaps_robots="1")
        got = self.run_probes()
        self.assertIn("/custom/map.xml", got)
        self.assertEqual(got["/custom/map.xml"]["verdict"], "pass")
        self.assertIn("настройки сети", got["/custom/map.xml"]["meta"])
        self.assertIn("/sitemap_5ka.xml", got)  # из robots.txt тоже

    def test_robots_discovery_can_be_switched_off(self):
        self.save(sitemaps="/custom/map.xml")  # sitemaps_robots не передан = выкл
        got = self.run_probes()
        self.assertEqual(list(got), ["/custom/map.xml"])

    def test_fallback_only_when_nothing_found(self):
        self.save(sitemaps_fallback="1")
        got = self.run_probes()
        self.assertEqual(list(got), ["/sitemap.xml"])

    def test_check_network_helper(self):
        from techaudit import sitemapcheck
        self.save(sitemaps="/custom/map.xml\n/nope.xml", sitemaps_robots="1")
        st = load_settings()
        res = sitemapcheck.check_network(st, st.networks["mock"])
        by_url = {r["url"].replace(self.base, ""): r for r in res["rows"]}
        self.assertEqual(by_url["/custom/map.xml"]["type"], "urlset")
        self.assertEqual(by_url["/custom/map.xml"]["urls"], 1)
        self.assertTrue(by_url["/nope.xml"]["problem"])
        self.assertFalse(res["ok"])
        self.assertIn("/sitemap_5ka.xml", by_url)
        self.assertTrue(any(r["depth"] == 1 for r in res["rows"]))  # вложенные карты индекса


@unittest.skipIf(TestClient is None, "нет fastapi/httpx")
class SettingsPageTest(EnvCase):
    def setUp(self):
        super().setUp()
        from techaudit.web.app import create_app
        self.app = create_app(None, start_jobs=False)
        self.c = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        self.c.close()
        super().tearDown()

    def test_full_cycle_without_env(self):
        self.assertEqual(self.app.state.settings.networks, {})
        r = self.c.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Добавить сеть", r.text)
        r = self.c.post("/settings/network", data={"name": "shop", "url": "https://shop.example.ru", "token": SECRET,
                                                   "sitemaps": "/sitemap_index.xml\n/extra/map.xml", "sitemaps_robots": "1"})
        self.assertEqual(r.status_code, 303, r.text)
        st = self.app.state.settings
        self.assertEqual(st.networks["shop"].sitemaps, ("/sitemap_index.xml", "/extra/map.xml"))  # без перезапуска
        page = self.c.get("/settings").text
        self.assertIn("/extra/map.xml", page)
        self.assertNotIn(SECRET, page)  # секрет назад не отдаётся
        self.assertIn("shop", self.c.get("/runs").text)  # сеть доступна в формах запуска
        db = self.db()
        self.assertEqual(db.execute("SELECT host FROM ref_sites WHERE site='shop'").fetchone()[0], "shop.example.ru")
        # общие настройки
        r = self.c.post("/settings/save/requests", data={"CONCURRENCY": "7", "REQUEST_TIMEOUT": "12,5"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual((st.concurrency, st.timeout), (7, 12.5))
        r = self.c.post("/settings/save/requests", data={"CONCURRENCY": "abc"})
        self.assertIn("err=", r.headers["location"])
        self.assertEqual(st.concurrency, 7)
        # токен и UA
        self.c.post("/settings/save/access", data={"SP_TOKEN": "COMMON-" + SECRET, "UA_PROFILE": "yandexbot"})
        self.assertEqual(st.ua_default, "yandexbot")
        self.assertNotIn("COMMON-" + SECRET, self.c.get("/settings").text)
        self.c.post("/settings/ua", data={"name": "mybot", "value": "Mozilla/5.0 (compatible; MyBot/1.0)"})
        self.assertIn("mybot", st.ua_profiles)
        self.c.post("/settings/ua/mybot/delete")
        self.assertNotIn("mybot", st.ua_profiles)
        # удаление сети
        self.c.post("/settings/network/shop/delete")
        self.assertEqual(st.networks, {})

    def test_adding_network_keeps_networks_from_env(self):
        os.environ.update({"NETWORKS": "envnet", "NET_ENVNET_URL": "https://env.example.ru", "NET_ENVNET_TOKEN": "tt"})
        self.app.state.settings.__dict__.update(load_settings().__dict__)
        self.assertIn("перенести", self.c.get("/settings").text.lower())
        self.c.post("/settings/network", data={"name": "second", "url": "https://second.example.ru"})
        self.assertEqual(list(self.app.state.settings.networks), ["envnet", "second"])
        self.assertEqual(self.app.state.settings.networks["envnet"].token, "tt")

    def test_check_sitemaps_page(self):
        srv, base = mock_site.start()
        try:
            self.c.post("/settings/network", data={"name": "mock", "url": base, "token": mock_site.TOKEN,
                                                   "sitemaps": "/custom/map.xml", "sitemaps_robots": "1"})
            r = self.c.post("/settings/network/mock/check-sitemaps")
            self.assertEqual(r.status_code, 200, r.text[:500])
            self.assertIn("/custom/map.xml", r.text)
            self.assertNotIn(mock_site.TOKEN, r.text)
            # без токена — понятное сообщение, а не 500
            self.c.post("/settings/network", data={"original": "mock", "name": "mock", "url": base, "clear_token": "1"})
            r = self.c.post("/settings/network/mock/check-sitemaps")
            self.assertEqual(r.status_code, 200)
            self.assertIn("не задан токен", r.text)
        finally:
            srv.shutdown()

    def test_import_env_button(self):
        os.environ.update({"NETWORKS": "a", "NET_A_URL": "https://a.example.ru", "NET_A_TOKEN": "x", "TTFB_MAX_MS": "800"})
        r = self.c.post("/settings/import-env")
        self.assertEqual(r.status_code, 303)
        db = self.db()
        self.assertEqual(aps.get_network(db, "a")["url"], "https://a.example.ru")
        self.assertEqual(aps.get_settings(db)["TTFB_MAX_MS"], "800")

    def db(self):
        from techaudit.webdb import connect
        return connect(self.app.state.db_path)


if __name__ == "__main__":
    unittest.main()
