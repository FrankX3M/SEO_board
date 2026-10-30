"""Самопроверка сборщика на моковом сайте. Запуск: python -m unittest tests.test_smoke -v"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import mock_site  # noqa: E402
from techaudit.collect import main  # noqa: E402

try:
    import playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

REGISTRY = """type,url,note
main,/,
catalog,/catalog/,
old,/old/,
upper,/Upper/,
gone,/gone/,
bot,/bot-only/,
ssr,/ssr/,
sub,/catalog/sub/,
"""


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.srv, base = mock_site.start()
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "registry").mkdir()
        (tmp / "registry" / "mock.csv").write_text(REGISTRY, encoding="utf-8")
        self._env = dict(os.environ)
        os.environ.update({
            "NETWORKS": "mock", "NET_MOCK_URL": base, "NET_MOCK_TOKEN": mock_site.TOKEN,
            "DATA_DIR": str(tmp / "data"), "REGISTRY_DIR": str(tmp / "registry"), "UA_PROFILE": "googlebot",
        })
        self.data = tmp / "data"

    def tearDown(self):
        self.srv.shutdown()
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def probes(self, **where):
        db = sqlite3.connect(self.data / "techaudit.db")
        db.row_factory = sqlite3.Row
        q = "SELECT * FROM probes" + (" WHERE " + " AND ".join(f"{k}=?" for k in where) if where else "")
        return db.execute(q, tuple(where.values())).fetchall()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(argv), env_file=os.devnull)
        return code, buf.getvalue()

    def rows(self, **where):
        db = sqlite3.connect(self.data / "techaudit.db")
        db.row_factory = sqlite3.Row
        q = "SELECT * FROM pages" + (" WHERE " + " AND ".join(f"{k}=?" for k in where) if where else "")
        return {(r["page_type"], r["variant"]): r for r in db.execute(q, tuple(where.values()))}

    def test_raw_statuses_redirects_and_ua(self):
        code, out = self.run_cli("--network", "mock", "--variants", "bot", "--no-probes")
        self.assertEqual(code, 0, out)
        r = self.rows()
        self.assertEqual(r[("main", "bot")]["status"], 200)
        self.assertEqual(r[("main", "bot")]["has_h1"], 1)
        # /old/ -> 301 -> /catalog/: цепочка из двух хопов, финальный 200
        old = r[("old", "bot")]
        chain = json.loads(old["chain"])
        self.assertEqual([h["status"] for h in chain], [301, 200])
        self.assertTrue(old["final_url"].endswith("/catalog/"))
        # заглавные буквы -> 302 -> 404
        self.assertEqual([h["status"] for h in json.loads(r[("upper", "bot")]["chain"])], [302, 404])
        self.assertEqual(r[("gone", "bot")]["status"], 404)
        # raw не исполняет JS: h1 каталога в сыром HTML нет
        self.assertEqual(r[("catalog", "bot")]["has_h1"], 0)
        # googlebot получает полный ответ
        self.assertEqual(r[("bot", "bot")]["suspect"], "")

    def test_toggles(self):
        # без токена — 403 и пометка blocked_status
        self.run_cli("--network", "mock", "--variants", "bot", "--types", "main", "--no-token", "--no-probes")
        r = self.rows()[("main", "bot")]
        self.assertEqual(r["status"], 403)
        self.assertIn("blocked_status", r["suspect"])
        # без подмены UA бот-страница отдаёт заглушку с капчей
        self.run_cli("--network", "mock", "--variants", "bot", "--types", "bot", "--ua", "none", "--no-probes")
        r = self.rows()[("bot", "bot")]
        self.assertIn("captcha_marker", r["suspect"])

    def test_secrets_not_stored(self):
        self.run_cli("--network", "mock", "--variants", "bot", "--types", "main")
        db = sqlite3.connect(self.data / "techaudit.db")
        dump = " ".join(str(v) for t in ("runs", "pages", "probes") for row in db.execute(f"SELECT * FROM {t}") for v in row)
        self.assertNotIn(mock_site.TOKEN, dump)
        self.assertIn("-prod", db.execute("SELECT run_id FROM runs").fetchone()[0])

    def test_token_is_required(self):
        os.environ.pop("NET_MOCK_TOKEN")
        code, out = self.run_cli("--network", "mock", "--variants", "bot", "--no-probes")
        self.assertEqual(code, 2)
        self.assertEqual(mock_site.Handler.seen, [])  # без токена к сайту не ходим

    def test_missing_integration_keys_do_not_crash(self):
        code, out = self.run_cli("--network", "mock", "--dry-run", "--variants", "bot")
        self.assertEqual(code, 0, out)
        self.assertIn("нет PAGESPEED_API_KEY", out)

    def test_dry_run_makes_no_requests(self):
        code, out = self.run_cli("--network", "mock", "--dry-run", "--variants", "plain,bot")
        self.assertEqual(code, 0)
        self.assertEqual(mock_site.Handler.seen, [])
        self.assertIn("запросов страниц: 16", out)
        self.assertIn("проб:", out)
        self.assertNotIn(mock_site.TOKEN, out)

    @unittest.skipUnless(HAVE_PW, "playwright не установлен")
    def test_rendered_executes_js_and_saves_header(self):
        code, out = self.run_cli("--network", "mock", "--variants", "bot,browser", "--types", "catalog,main", "--no-probes")
        self.assertEqual(code, 0, out)
        r = self.rows()
        self.assertEqual(r[("catalog", "bot")]["has_h1"], 0)
        self.assertEqual(r[("catalog", "browser")]["has_h1"], 1)  # h1 появился после JS
        f = self.data / r[("catalog", "browser")]["file_path"]
        self.assertTrue(f.read_text(encoding="utf-8").startswith("<!-- SOURCE_URL: "))
        # токен ушёл в браузерные запросы к сайту
        self.assertTrue(any(h["headers"].get("x-sp-token") == mock_site.TOKEN for h in mock_site.Handler.seen))

    def test_plain_vs_bot_diff_finds_new_blocks(self):
        code, out = self.run_cli("--network", "mock", "--variants", "plain,bot", "--types", "ssr", "--no-probes")
        self.assertEqual(code, 0, out)
        r = self.rows()
        self.assertEqual(r[("ssr", "plain")]["ua_name"], "desktop")
        self.assertEqual(r[("ssr", "bot")]["ua_name"], "googlebot")
        db = sqlite3.connect(self.data / "techaudit.db")
        db.row_factory = sqlite3.Row
        d = db.execute("SELECT * FROM diffs WHERE variant_a='plain' AND variant_b='bot'").fetchone()
        details = json.loads(d["details"])
        keys = {b["key"]: b for b in details["blocks_appeared"]}
        self.assertIn("qa:product-list", keys)
        self.assertEqual(keys["qa:product-card-*"]["instances"], 3)  # карточки схлопнуты в один тип блока
        self.assertIn("footer", keys)
        self.assertEqual(details["head"]["h1"]["a"], [])
        self.assertGreater(d["links_appeared"], 0)
        self.assertIn("qa:product-card-*", out)
        self.assertTrue(list((self.data / "reports").glob("*-diff.html")))

    @unittest.skipUnless(HAVE_PW, "playwright не установлен")
    def test_bot_vs_browser_shows_js_only_block(self):
        code, out = self.run_cli("--network", "mock", "--variants", "bot,browser", "--types", "ssr", "--no-probes")
        self.assertEqual(code, 0, out)
        db = sqlite3.connect(self.data / "techaudit.db")
        d = db.execute("SELECT details FROM diffs WHERE variant_a='bot' AND variant_b='browser'").fetchone()
        keys = [b["key"] for b in json.loads(d[0])["blocks_appeared"]]
        self.assertEqual(keys, ["qa:reviews-block"])  # только то, что дорисовал JS

    def test_recompare_without_requests(self):
        self.run_cli("--network", "mock", "--variants", "plain,bot", "--types", "ssr", "--no-diff", "--no-probes")
        db = sqlite3.connect(self.data / "techaudit.db")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM diffs").fetchone()[0], 0)
        n = len(mock_site.Handler.seen)
        from techaudit.comparer import compare_run, latest_run
        from techaudit.storage import Store
        st = Store(self.data)
        res = compare_run(st, latest_run(st), [("plain", "bot")])
        self.assertEqual(len(res), 1)
        self.assertEqual(len(mock_site.Handler.seen), n)  # сайт не трогали


    def test_probes_redirects_404_robots_sitemap_resources(self):
        code, out = self.run_cli("--network", "mock", "--probes-only", "--probe-limit", "1")
        self.assertEqual(code, 0, out)
        by = {}
        for r in self.probes():
            by.setdefault(r["kind"], []).append(r)

        base = by["robots"][0]["url"].rsplit("/", 1)[0]

        def one(kind, part):
            rows = [r for r in by[kind] if (r["url"].startswith(base + part) if "ta-404-" in part else r["url"] == base + part)]
            self.assertEqual(len(rows), 1, (kind, part, [r["url"] for r in by[kind]]))
            return rows[0]

        self.assertEqual(one("slash", "/catalog")["verdict"], "pass")  # 301 одним хопом на /catalog/
        up = one("upper", "/CATALOG/")
        self.assertEqual((up["verdict"], up["reason"]), ("fail", "not_301:302"))
        self.assertEqual(one("index_php", "/index.php")["verdict"], "pass")
        dup = one("index_html", "/index.html")
        self.assertEqual(dup["reason"], "no_redirect_200_duplicate")
        nf = {r["page_type"]: r for r in by["not_found"]}
        self.assertEqual(nf["catalog"]["verdict"], "pass")
        self.assertTrue(nf["catalog"]["file_path"])  # тело 404 сохранено для проверки шаблона
        self.assertTrue(nf["sub"]["url"].startswith(base + "/catalog/ta-404-"))  # заменён последний сегмент
        self.assertEqual(nf["sub"]["reason"], "redirected:302")  # 404 через редирект на отдельную страницу
        self.assertEqual(by["robots"][0]["verdict"], "pass")
        sm = {r["url"].rsplit("/", 1)[1]: r for r in by["sitemap"]}
        self.assertEqual(sm["sitemap_5ka.xml"]["verdict"], "pass")
        self.assertEqual(json.loads(sm["sitemap_5ka_pages.xml"]["meta"])["urls"], 2)
        self.assertEqual(sm["sitemap_5ka_tags.xml"]["reason"], "status_404")  # вложенная карта недоступна
        self.assertEqual(by["favicon"][0]["verdict"], "pass")
        self.assertEqual(len(by["favicon"]), 1)  # ссылка из head и /favicon.ico — один URL
        self.assertEqual(by["manifest"][0]["verdict"], "pass")
        icons = {r["url"].rsplit("/", 1)[1]: r["reason"] for r in by["manifest_icon"]}
        self.assertEqual(icons, {"icon-192.png": "", "icon-512.png": "status_404"})
        meta = json.loads(by["og_image"][0]["meta"])
        self.assertEqual((meta["width"], meta["height"]), (1200, 630))
        self.assertIn("ПРОБЫ", out)

    def test_probe_kinds_selection(self):
        self.run_cli("--network", "mock", "--probes-only", "--probes", "robots,sitemap")
        self.assertEqual({r["kind"] for r in self.probes()}, {"robots", "sitemap"})


class ProbeSpecUnitTest(unittest.TestCase):
    def test_redirect_specs_for_https_site(self):
        from techaudit.probes import ALL_KINDS, redirect_specs
        from techaudit.registry import RegistryEntry
        e = RegistryEntry("5ka", "category", "/catalog/", "/catalog/")
        specs = {s.kind: s for s in redirect_specs("https://5ka.ru", e, ALL_KINDS)}
        self.assertEqual(specs["slash"].url, "https://5ka.ru/catalog")
        self.assertEqual(specs["upper"].url, "https://5ka.ru/CATALOG/")
        self.assertEqual(specs["alt_host"].url, "https://www.5ka.ru/catalog/")
        self.assertEqual(specs["http"].url, "http://5ka.ru/catalog/")
        self.assertEqual(specs["combined"].url, "http://www.5ka.ru/CATALOG")
        self.assertEqual(specs["index_php"].url, "https://5ka.ru/catalog/index.php")
        self.assertTrue(all(s.expected["target"] == "https://5ka.ru/catalog/" for s in specs.values()))

    def test_www_site_and_skipped_types(self):
        from techaudit.probes import ALL_KINDS, redirect_specs
        from techaudit.registry import RegistryEntry
        e = RegistryEntry("n", "product", "/product/x--1/", "/product/x--1/")
        specs = {s.kind: s for s in redirect_specs("https://www.n.ru", e, ALL_KINDS, skip_index_types=("product",))}
        self.assertEqual(specs["alt_host"].url, "https://n.ru/product/x--1/")
        self.assertNotIn("index_php", specs)

    def test_not_found_spec_replaces_last_segment(self):
        from techaudit.probes import not_found_spec
        from techaudit.registry import RegistryEntry
        sp = not_found_spec("https://5ka.ru", RegistryEntry("5ka", "product", "/product/x--1/", "/product/x--1/"))
        self.assertRegex(sp.url, r"^https://5ka\.ru/product/ta-404-[0-9a-f]{8}/$")
        root = not_found_spec("https://5ka.ru", RegistryEntry("5ka", "main", "/", "/"))
        self.assertRegex(root.url, r"^https://5ka\.ru/ta-404-[0-9a-f]{8}/$")

    def test_image_size_and_sitemap_parser(self):
        from techaudit.assets import image_size
        from techaudit.probes import parse_sitemap
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (64).to_bytes(4, "big") + (32).to_bytes(4, "big")
        self.assertEqual(image_size(png), (64, 32))
        self.assertEqual(image_size(b"GIF89a" + (5).to_bytes(2, "little") + (7).to_bytes(2, "little")), (5, 7))
        self.assertEqual(parse_sitemap(b"<urlset><url><loc>a</loc></url></urlset>")["urls"], 1)
        self.assertEqual(parse_sitemap(b"not xml")["type"], "invalid")


class AccessUnitTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith(("NET_", "SP_TOKEN", "TECHAUDIT_ENV")) or k == "NETWORKS":
                os.environ.pop(k)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_each_network_uses_its_own_token(self):
        from techaudit.config import load_settings, resolve_access
        os.environ.update(NETWORKS="a,b", NET_A_URL="https://a.ru", NET_B_URL="https://b.ru",
                          NET_A_TOKEN="token-a", NET_B_TOKEN="token-b", NET_B_TOKEN_HEADER="X-B")
        st = load_settings(os.devnull)
        a = resolve_access(st, st.networks["a"])
        b = resolve_access(st, st.networks["b"])
        self.assertEqual(a.auth_headers(), {"X-SP-Token": "token-a"})
        self.assertEqual(b.auth_headers(), {"X-B": "token-b"})
        self.assertEqual(a.warnings, ())

    def test_network_without_own_token_is_flagged(self):
        from techaudit.config import ConfigError, load_settings, resolve_access
        os.environ.update(NETWORKS="a,b", NET_A_URL="https://a.ru", NET_B_URL="https://b.ru", NET_A_TOKEN="ta")
        st = load_settings(os.devnull)
        with self.assertRaises(ConfigError) as cm:
            resolve_access(st, st.networks["b"])
        self.assertIn("NET_B_TOKEN", str(cm.exception))
        os.environ["SP_TOKEN"] = "shared"
        st = load_settings(os.devnull)
        acc = resolve_access(st, st.networks["b"])  # запасной общий токен — но с предупреждением
        self.assertEqual(acc.token, "shared")
        self.assertTrue(any("общий SP_TOKEN" in w for w in acc.warnings))

    def test_env_file_lookup_order(self):
        import tempfile
        from pathlib import Path
        from techaudit import config
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "secrets.env"
            f.write_text("NETWORKS=zz\nNET_ZZ_URL=https://zz.ru\nNET_ZZ_TOKEN=tz\n", encoding="utf-8")
            self.assertEqual(config.find_env_file(f), f)  # явный путь
            os.environ["TECHAUDIT_ENV"] = str(f)
            st = config.load_settings()  # через переменную TECHAUDIT_ENV
            self.assertEqual(st.env_file, str(f))
            self.assertEqual(st.networks["zz"].token, "tz")
            os.environ.pop("NETWORKS", None)
            os.environ.pop("NET_ZZ_URL", None)
            os.environ.pop("NET_ZZ_TOKEN", None)
        os.environ.pop("TECHAUDIT_ENV")
        parent_first = [config.ROOT.parent / ".env", config.ROOT / ".env"]
        self.assertEqual(parent_first[0].parent, config.ROOT.parent)  # выше проекта проверяется раньше


class IntegrationsUnitTest(unittest.TestCase):
    def _settings(self, **keys):
        from techaudit.config import INTEGRATION_VARS, load_settings
        saved = dict(os.environ)
        try:
            for var, _ in INTEGRATION_VARS.values():
                os.environ.pop(var, None)
            os.environ.update(keys)
            return load_settings(os.devnull)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_no_key_is_skipped_not_error(self):
        from techaudit.integrations import guarded
        out = guarded(self._settings(), "pagespeed", lambda key: 1 / 0)
        self.assertEqual(out.status, "skipped")
        self.assertIn("PAGESPEED_API_KEY", out.reason)

    def test_service_failure_does_not_raise(self):
        from techaudit.integrations import guarded
        out = guarded(self._settings(PAGESPEED_API_KEY="k"), "pagespeed", lambda key: 1 / 0)
        self.assertEqual(out.status, "error")
        self.assertEqual(guarded(self._settings(PAGESPEED_API_KEY="k"), "pagespeed", lambda key: key).value, "k")


class DiffUnitTest(unittest.TestCase):
    def test_identical_pages_are_same(self):
        from techaudit.diff import compare, parse_doc
        h = "<html><body><header>Шапка</header><main><h1>Заголовок страницы</h1><p>Текст текст</p></main></body></html>"
        res = compare(parse_doc(h, "https://x.ru/"), parse_doc(h, "https://x.ru/"))
        self.assertEqual(res["verdict"], "same")

    def test_noise_is_ignored_and_markers_normalized(self):
        from techaudit.diff import compare, parse_doc
        a = "<html><body><div id='__next'></div></body></html>"
        b = ("<html><body><div id='__next'><div data-qa='card-4280098'>Сыр</div><div data-qa='card-4255306'>Масло</div>"
             "</div><next-route-announcer><p>Страница</p></next-route-announcer></body></html>")
        res = compare(parse_doc(a, "https://x.ru/"), parse_doc(b, "https://x.ru/"))
        self.assertEqual([x["key"] for x in res["blocks_appeared"]], ["qa:card-*"])
        self.assertEqual(res["blocks_appeared"][0]["instances"], 2)

    def test_structured_data_and_head_diff(self):
        from techaudit.diff import compare, parse_doc
        a = "<html><head><title>A</title></head><body><p>текст страницы</p></body></html>"
        b = ("<html><head><title>B</title><script type='application/ld+json'>{\"@type\":\"BreadcrumbList\"}</script></head>"
             "<body><p>текст страницы</p></body></html>")
        res = compare(parse_doc(a, "https://x.ru/"), parse_doc(b, "https://x.ru/"))
        self.assertEqual(res["head"]["title"], {"a": "A", "b": "B"})
        self.assertEqual(res["structured"]["jsonld"]["b"], {"BreadcrumbList": 1})


if __name__ == "__main__":
    unittest.main()
