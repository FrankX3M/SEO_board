import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from fastapi.testclient import TestClient
from tests.test_web import make_app
from techaudit import site_contacts, sitemapcheck, appsettings
from techaudit.webdb import connect, tx


class SiteSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = make_app(Path(cls.tmp.name))
        cls.client = TestClient(cls.app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def test_contact_lifecycle_and_site_isolation(self):
        c = self.client
        self.assertEqual(c.get('/sites/5ka').status_code, 200)
        self.assertEqual(c.get('/sites/chizhik').status_code, 200)
        response = c.post('/sites/5ka/contacts', data={'name':'Тест', 'email':'person@example.org', 'area':'SEO'})
        self.assertEqual(response.status_code, 303)
        db = connect(self.app.state.db_path)
        try:
            person = db.execute("SELECT id FROM site_contacts WHERE site='5ka' AND email='person@example.org'").fetchone()[0]
            other = db.execute("SELECT id FROM site_contacts WHERE site='chizhik' LIMIT 1").fetchone()[0]
            before = len(site_contacts.contacts(db, '5ka'))
            site_contacts.migrate_site(db, '5ka')
            self.assertEqual(len(site_contacts.contacts(db, '5ka')), before)
            response = c.post('/sites/5ka/types/home/contacts', data={'contact_ids':str(person)})
            self.assertNotIn('err=', response.headers['location'])
            self.assertEqual(site_contacts.assignments(db,'5ka')['home'], [person])
            response = c.post('/sites/5ka/types/home/contacts', data={'contact_ids':str(other)})
            self.assertIn('err=', response.headers['location'])
            self.assertEqual(site_contacts.assignments(db,'5ka')['home'], [person])
            response = c.post(f'/sites/5ka/contacts/{person}', data={'name':'Новое имя','email':'new@example.org','area':'Контент'})
            self.assertNotIn('err=', response.headers['location'])
            html = c.get('/reference?tab=types&net=5ka').text
            self.assertIn('Новое имя', html)
            self.assertNotIn('tab=owners', html)
            self.assertIn(f'value="{person}" selected', html)
            other_before = site_contacts.assignments(db,'chizhik')
            c.post(f'/sites/5ka/contacts/{person}/delete')
            self.assertNotIn(person, site_contacts.assignments(db,'5ka').get('home', []))
            self.assertEqual(site_contacts.assignments(db,'chizhik'), other_before)
            site_contacts.migrate_site(db, '5ka')
            self.assertFalse(db.execute('SELECT 1 FROM site_contacts WHERE id=?',(person,)).fetchone())
        finally:
            db.close()

    def test_registry_only_site_settings(self):
        name = 'registry_only_settings'
        db = connect(self.app.state.db_path)
        try:
            with tx(db):
                db.execute('INSERT INTO ref_sites(site, name_ru, host) VALUES (?,?,?)',
                           (name, name, 'example.org'))
            self.assertIn(f'id="net-{name}"', self.client.get('/settings').text)
            card = self.client.get('/sites/' + name)
            self.assertEqual(card.status_code, 200)
            self.assertIn('value="https://example.org"', card.text)
            response = self.client.post('/settings/network', data={
                'original': name, 'url': 'https://example.org', 'site_card': '1'})
            self.assertEqual(response.status_code, 303)
            self.assertNotIn('err=', response.headers['location'])
            self.assertEqual(appsettings.get_network(db, name)['url'], 'https://example.org')
            response = self.client.post(f'/settings/network/{name}/delete')
            self.assertEqual(response.status_code, 303)
            html = self.client.get('/settings').text
            self.assertNotIn(f'id="net-{name}"', html)
            self.assertNotIn(f'>{name}</option>', html)
            self.assertIn(name, appsettings.removed_networks(db))
            self.assertIsNotNone(db.execute('SELECT 1 FROM ref_sites WHERE site=?', (name,)).fetchone())
            response = self.client.post('/settings/network', data={
                'name': name, 'url': 'https://example.org'})
            self.assertNotIn('err=', response.headers['location'])
            self.assertIn(f'id="net-{name}"', self.client.get('/settings').text)
            self.assertNotIn(name, appsettings.removed_networks(db))
        finally:
            db.close()

    def test_settings_and_token_form(self):
        html = self.client.get('/sites/5ka').text
        self.assertIn('data-token-row', html)
        self.assertEqual(html.count('name="token"'), 1)
        self.assertEqual(html.count('name="token_header"'), 1)
        self.assertIn('data-add-sitemap', html)
        self.assertNotIn('SECRET-TOKEN-VALUE', html)
        top = html.split('aria-label="Основные разделы"')[1].split('</nav>')[0]
        self.assertNotIn('Страницы', top)
        self.assertIn('Страницы и источники', self.client.get('/settings').text)

    def test_sitemap_results_and_invalid_urls(self):
        c = self.client
        for value in ('javascript:alert(1)', 'https://a b/s.xml', 'http://[bad', 'https://a:xyz/s.xml', 'https://u:p@a/s.xml'):
            self.assertIsNone(appsettings.resolve_sitemap_url('https://example.org', value))
            self.assertEqual(c.post('/settings/network/5ka/check-sitemap-row', data={'url':value}).status_code, 400)
        cases = [(200, b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>', True, True),
                 (200, b'<html/>', True, False), (404, b'not found', False, None),
                 (200, b'<broken', True, False)]
        for status, body, available, valid in cases:
            with patch('techaudit.sitemapcheck.fetch_raw', new=AsyncMock(return_value=SimpleNamespace(status=status,body=body,error=None))):
                response = c.post('/settings/network/5ka/check-sitemap-row', data={'url':'/sitemap.xml'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['available'],available)
            self.assertEqual(response.json()['valid'],valid)
            self.assertIn('checked_at',response.json())
            self.assertNotIn('SECRET-TOKEN-VALUE',response.text)
