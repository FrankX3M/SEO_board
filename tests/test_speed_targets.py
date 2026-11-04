import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from techaudit import speed_targets as st
from techaudit.checks.core import CheckSettings
from techaudit.db_registry import SCHEMA as REGISTRY_SCHEMA


class SpeedTargetsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript(REGISTRY_SCHEMA + st.SCHEMA)
        self.addCleanup(self.db.close)
        for site in ('a', 'b'):
            for kind, path, noindex, status in [
                ('about', '/about/', 0, 'active'), ('home', '/', 0, 'active'),
                ('cat_l1', '/cat/1', 0, 'active'), ('cat_l1', '/cat/2', 0, 'active'),
                ('cat_l2', '/cat/3', 0, 'active'), ('product', '/p/1', 0, 'active'),
                ('product', '/p/2', 0, 'active'), ('product', '/p/noindex', 1, 'active'),
                ('product', '/p/gone', 0, 'gone')]:
                self.db.execute('INSERT INTO ref_urls(site,page_type,url,path_qs,noindex,status,created_at,updated_at) '
                                'VALUES (?,?,?,?,?,?,?,?)', (site, kind, path, path, noindex, status, '', ''))

    def selected(self, site='a', base='https://shop'):
        return st.select(self.db, site, base, CheckSettings())

    def test_default_includes_every_traffic_url_without_three_url_limit(self):
        rows = self.selected()
        self.assertEqual(len(rows), 6)
        self.assertEqual({r.url for r in rows}, {'https://shop/', 'https://shop/cat/1',
                         'https://shop/cat/2', 'https://shop/cat/3', 'https://shop/p/1', 'https://shop/p/2'})

    def test_custom_types_are_isolated_per_network_and_expand_with_registry(self):
        st.save(self.db, 'a', 'https://shop', 'custom', ['about'], '')
        self.assertEqual([r.url for r in self.selected()], ['https://shop/about/'])
        self.assertEqual(len(self.selected('b')), 6)
        self.db.execute("INSERT INTO ref_urls(site,page_type,url,path_qs,created_at,updated_at) VALUES ('a','about','/new','/new','','')")
        self.assertEqual(len(self.selected()), 2)

    def test_explicit_urls_deduplicate_and_follow_network_address(self):
        st.save(self.db, 'a', 'https://shop', 'custom', ['product'], '/p/1\nhttps://shop/p/1\n/special?x=1')
        self.assertEqual(len(self.selected()), 3)
        self.assertIn('https://newshop/special?x=1', [r.url for r in self.selected(base='https://newshop')])

    def test_empty_custom_does_not_fallback_to_automatic(self):
        st.save(self.db, 'a', 'https://shop', 'custom', [], '')
        self.assertEqual(self.selected(), [])

    def test_invalid_url_or_mode_does_not_overwrite_config(self):
        for mode, urls in [('wrong', ''), ('custom', 'https://other/'), ('custom', '//other/'),
                           ('custom', 'javascript:alert(1)'), ('custom', '/p/#anchor')]:
            with self.assertRaises(ValueError):
                st.save(self.db, 'a', 'https://shop', mode, ['home'], urls)
        self.assertEqual(st.config(self.db, 'a')['mode'], 'traffic')

    def test_legacy_database_has_traffic_defaults(self):
        self.db.execute('DROP TABLE speed_target_settings')
        self.assertEqual(len(self.selected()), 6)

    def test_role_overrides_are_respected(self):
        rows = st.select(self.db, 'a', 'https://shop', CheckSettings(role_overrides={'about': 'listing'}))
        self.assertEqual(len(rows), 7)

    def test_speed_selection_does_not_depend_on_collected_html(self):
        from techaudit.checks.c_external import _psi_pages
        ctx = SimpleNamespace(db=self.db, network='a', base_url='https://shop', cs=CheckSettings(),
                              settings=SimpleNamespace(registry_dir=None))
        self.assertEqual(len(_psi_pages(ctx)), 6)

    def test_exhausted_api_marks_all_requested_devices_and_urls(self):
        from techaudit.checks import c_external as ce
        from techaudit.external import ExternalError
        pages = self.selected()
        with patch.object(ce, '_off', return_value=None), patch.object(ce, '_psi_pages', return_value=pages), \
             patch.object(ce, '_psi_result', side_effect=ExternalError('все ключи PageSpeed исчерпаны')):
            result = ce.cwv(None)
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(len(result.stats['speed_measurements']), 12)
        self.assertTrue(all(row.get('error') for row in result.stats['speed_measurements']))
