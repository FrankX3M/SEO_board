import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

from tests.test_settings_ui import EnvCase, TestClient
from techaudit import appsettings as aps
from techaudit.config import load_settings, resolve_access
from techaudit.httpheaders import parse_headers
from techaudit.rawfetch import request_headers
from techaudit.render import BrowserSession


class SiteHeadersTest(EnvCase):
    def save(self, rows):
        db = self.db()
        aps.save_network(db, dict(name="shop", url="https://shop.example", token="token",
                                  headers=json.dumps(rows)))
        return db

    def test_headers_override_defaults_and_stay_on_site(self):
        self.save([dict(name="Cookie", value="version=new"),
                   dict(name="accept-language", value="en"),
                   dict(name="user-agent", value="Custom/1"),
                   dict(name="X-Off", value="no", enabled=False)])
        st = load_settings()
        access = resolve_access(st, st.networks["shop"])
        h = request_headers(access, "https://shop.example/a")
        self.assertEqual(h["Cookie"], "version=new")
        self.assertEqual(h["accept-language"], "en")
        self.assertNotIn("Accept-Language", h)
        self.assertEqual(h["user-agent"], "Custom/1")
        self.assertNotIn("X-Off", h)
        for url in ("https://external.example/", "https://shop.example.evil/"):
            self.assertNotIn("Cookie", request_headers(access, url))
            self.assertNotIn("X-SP-Token", request_headers(access, url))
        self.assertNotIn("version=new", str(access.describe()))

    def test_blank_keeps_secret_disable_and_delete(self):
        db = self.save([dict(name="Cookie", value="version=new")])
        form = dict(url="https://shop.example", headers=json.dumps([
            dict(name="Cookie", value="", keep=True, enabled=False)]))
        aps.save_network(db, form, original="shop")
        row = json.loads(aps.get_network(db, "shop")["headers"])[0]
        self.assertEqual(row["value"], "version=new")
        self.assertFalse(row["enabled"])
        form["headers"] = "[]"
        aps.save_network(db, form, original="shop")
        self.assertEqual(aps.get_network(db, "shop")["headers"], "[]")

    def test_invalid_headers(self):
        for rows in ([dict(name="Bad\nName", value="x")],
                     [dict(name="X-Test", value="ok\r\nInjected: yes")],
                     [dict(name="Cookie", value="a=1"), dict(name="cookie", value="b=2")]):
            with self.assertRaises(ValueError):
                parse_headers(rows)

    def test_migrate_old_database(self):
        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE TABLE app_networks(name TEXT PRIMARY KEY)")
            aps.ensure_schema(db)
            aps.ensure_schema(db)
            self.assertIn("headers", {r[1] for r in db.execute("PRAGMA table_info(app_networks)")})
        finally:
            db.close()

    def test_browser_cookie_and_headers(self):
        self.save([dict(name="Cookie", value="version=new; region=77"),
                   dict(name="Referer", value="https://shop.example/start")])
        st = load_settings()
        access = resolve_access(st, st.networks["shop"])

        async def run():
            ctx = AsyncMock()
            session = BrowserSession()
            session.browser = AsyncMock()
            session.browser.new_context.return_value = ctx
            await session.new_renderer(access, mobile=False, block_assets=False, verify_tls=True)
            cookies = ctx.add_cookies.call_args.args[0]
            self.assertEqual(cookies[0]["name"], "version")
            self.assertEqual(cookies[0]["domain"], ".shop.example")
            handler = ctx.route.call_args.args[1]
            from types import SimpleNamespace
            route = AsyncMock()
            await handler(route, SimpleNamespace(url="https://shop.example/a", headers={}, resource_type="document"))
            self.assertEqual(route.continue_.call_args.kwargs["headers"]["Referer"], "https://shop.example/start")
            route.reset_mock()
            await handler(route, SimpleNamespace(url="https://other.example/", headers={}, resource_type="document"))
            self.assertEqual(route.continue_.call_args.kwargs, {})
        asyncio.run(run())

    def test_site_card_and_secret_masking(self):
        self.save([dict(name="Cookie", value="PRIVATE-VERSION-COOKIE")])
        from techaudit.web.app import create_app
        app = create_app(None, start_jobs=False)
        with TestClient(app, follow_redirects=False) as client:
            listing = client.get("/reference?tab=sites")
            self.assertIn('href="/sites/shop"', listing.text)
            card = client.get("/sites/shop")
            self.assertEqual(card.status_code, 200)
            self.assertIn('HTTP-', card.text)
            self.assertIn('site-types', card.text)
            self.assertIn('site-jobs', card.text)
            self.assertNotIn('PRIVATE-VERSION-COOKIE', card.text)
            response = client.post("/settings/network", data=dict(original="shop", site_card="1",
                url="https://shop.example", headers=json.dumps([dict(name="Cookie", keep=True, value="")])) )
            self.assertEqual(response.headers['location'].split('?')[0], '/sites/shop')
            self.assertEqual(app.state.settings.networks['shop'].headers[0]['value'], 'PRIVATE-VERSION-COOKIE')
            self.assertNotIn('PRIVATE-VERSION-COOKIE', client.get('/reference?tab=log').text)
            self.assertEqual(client.get('/sites/missing').status_code, 404)

    def test_browser_editor(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright не установлен')
        from techaudit.web.app import create_app
        self.save([dict(name="Cookie", value="version=new")])
        app = create_app(None, start_jobs=False)
        with TestClient(app, follow_redirects=False) as client, sync_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():
                self.skipTest('Chromium не установлен')
            class Handler(BaseHTTPRequestHandler):
                def handle_request(self):
                    body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                    response = client.request(self.command, self.path, content=body,
                        headers={k: v for k, v in self.headers.items() if k.lower() not in {'host', 'content-length'}})
                    self.send_response(response.status_code)
                    for key, value in response.headers.items():
                        if key.lower() not in {'content-length', 'content-encoding'}:
                            self.send_header(key, value)
                    self.send_header('Content-Length', str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)

                do_GET = do_POST = handle_request

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                page.goto(f"http://127.0.0.1:{server.server_port}/reference?tab=sites")
                card_link = page.locator('a[href="/sites/shop"]')
                self.assertTrue(card_link.is_visible())
                card_link.click()
                page.wait_for_url('**/sites/shop')
                editor = page.locator('.header-editor')
                editor.locator('[data-add-header]').click()
                row = editor.locator('[data-header-row]').last
                row.locator('[data-name]').fill('Accept-Language')
                row.locator('[data-value]').fill('en-US')
                page.get_by_role('button', name='Сохранить сеть', exact=True).click()
                page.wait_for_load_state('networkidle')
                headers = app.state.settings.networks['shop'].headers
                self.assertEqual(headers[1]['value'], 'en-US')
                self.assertEqual(headers[0]['value'], 'version=new')
                self.assertEqual(page.locator('[data-header-row]').count(), 2, page.url + '\n' + page.content()[:2500])
                self.assertEqual(page.locator('[data-value]').first.input_value(), '')
                page.locator('[data-header-row]').last.locator('[data-remove-header]').click()
                page.get_by_role('button', name='Сохранить сеть', exact=True).click()
                page.wait_for_load_state('networkidle')
                self.assertEqual(len(app.state.settings.networks['shop'].headers), 1)
                self.assertEqual(errors, [])
            finally:
                browser.close()
                server.shutdown()
                server.server_close()
                thread.join()
