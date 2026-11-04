import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, select_autoescape

from techaudit.external import parse_psi, ExternalError
from techaudit.speed_report import report
from techaudit.checks import c_external


class SpeedReportTest(unittest.TestCase):
    def test_field_fallback_per_metric_and_cls_units(self):
        data = parse_psi({
            'loadingExperience': {'metrics': {'CUMULATIVE_LAYOUT_SHIFT_SCORE': {'percentile': 0}}},
            'originLoadingExperience': {'id': 'https://shop', 'metrics': {
                'LARGEST_CONTENTFUL_PAINT_MS': {'percentile': 2800},
                'CUMULATIVE_LAYOUT_SHIFT_SCORE': {'percentile': 15}}},
            'lighthouseResult': {'audits': {'total-blocking-time': {'numericValue': 400}}}})
        self.assertEqual(data['field']['cls']['p75'], 0)
        self.assertEqual(data['field']['cls']['source'], 'page')
        self.assertEqual(data['field']['lcp']['source'], 'origin')
        self.assertIsNone(data['field']['inp'])
        other = parse_psi({'originLoadingExperience': {'metrics': {
            'CUMULATIVE_LAYOUT_SHIFT_SCORE': {'percentile': 15}}}})
        self.assertEqual(other['field']['cls']['p75'], 0.15)

    def test_both_devices_and_api_failure_are_persisted(self):
        page = SimpleNamespace(url='https://shop/', page_type='home')
        def fetch(ctx, strategy, url):
            if strategy == 'desktop':
                raise ExternalError('timeout')
            return {'lcp_ms': 1000, 'cls': 0, 'tbt_ms': 0}, False
        with patch.object(c_external, '_off', return_value=None), \
             patch.object(c_external, '_psi_pages', return_value=[page]), \
             patch.object(c_external, '_psi_result', side_effect=fetch), \
             patch.object(c_external, '_psi', return_value=SimpleNamespace(pool=SimpleNamespace(summary=lambda _: ''))):
            result = c_external.cwv(None)
        rows = result.stats['speed_measurements']
        self.assertEqual([r['strategy'] for r in rows], ['mobile', 'desktop'])
        self.assertEqual(result.stats['bad'], 1)
        self.assertIn('timeout', rows[1]['error'])

    def test_history_scope_and_template_missing_values(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        db.executescript('CREATE TABLE runs(run_id TEXT, started_at TEXT); '
                         'CREATE TABLE check_results(id INTEGER, run_id TEXT, network TEXT, check_id TEXT, created_at TEXT, stats TEXT);')
        row = {'url': 'https://shop/<test>', 'page_type': 'home', 'strategy': 'mobile',
               'data': {'perf': 0, 'cls': 0}, 'cached': True}
        for i, network in enumerate(['a', 'a', 'b']):
            db.execute('INSERT INTO runs VALUES (?,?)', (str(i), str(i)))
            db.execute('INSERT INTO check_results VALUES (?,?,?,?,?,?)',
                       (i, str(i), network, 'cwv', str(i), json.dumps({'speed_measurements': [row]})))
        view = report(db, 'a', ['1', '2'], '1')
        self.assertEqual(len(view['history']), 1)
        self.assertEqual(len(view['latest']), 1)
        self.assertEqual(report(db, 'a', ['1'], 'old')['latest'], [])
        env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / 'techaudit/web/templates'),
                          autoescape=select_autoescape())
        html = env.get_template('_speed_report.html').render(v={'speed': view})
        self.assertIn('0 баллов', html)
        self.assertIn('0.000', html)
        self.assertIn('Нет полевых данных', html)
        self.assertIn('&lt;test&gt;', html)
        self.assertNotIn('None', html)
        self.assertEqual(html.count('id="speed-metric"'), 1)
        self.assertEqual(html.count('id="speed-chart"'), 1)
        db.close()
