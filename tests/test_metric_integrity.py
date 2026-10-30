"""Regression tests for report counts; no network or production database writes."""
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from techaudit import crawl
from techaudit.checks import c_crawl, c_external, report
from techaudit.checks.core import PASS, WARN, FAIL, Item
from techaudit.checks.extras import graded
from techaudit.checks import x_meta


class MetricIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        crawl.ensure_schema(self.db)
        self.db.execute("INSERT INTO crawl_imports(id,network,taken_at,uploaded_at,rows) VALUES(1,'shop','2026-10-22T14:00:00+03:00','2026-10-22T11:00:00+00:00',5)")
        self.imp = dict(self.db.execute("SELECT * FROM crawl_imports").fetchone())
        self.ctx = SimpleNamespace(db=self.db, network='shop', _crawl_imp=self.imp)

    def tearDown(self):
        self.db.close()

    def add(self, url, status=200, **kw):
        row = dict(import_id=1, address=url, status=status, **kw)
        self.db.execute('INSERT INTO crawl_pages('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')', list(row.values()))

    def test_zero_is_not_5xx(self):
        self.add('https://shop/blocked', 0)
        self.add('https://shop/unknown', None)
        self.add('https://shop/error', 503)
        self.add('https://shop/nonhttp', 600)
        result = c_crawl.no_5xx(self.ctx, self.imp)
        self.assertEqual(result.stats['bad'], 1)
        self.assertEqual(c_crawl.no_response(self.ctx).stats['bad'], 2)

    def test_redirect_without_content_type_is_counted(self):
        self.add('https://shop/document.pdf', 308, inlinks=12)
        result = c_crawl.links_redirects(self.ctx, self.imp)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.stats['bad'], 1)
        self.assertEqual(result.stats['inlinks'], 12)

    def test_title_normalization_and_2xx(self):
        self.add('https://shop/a', title=' Milk  product ', content_type='text/html', indexability='Indexable')
        self.add('https://shop/b', title='milk product', content_type='text/html', indexability='Indexable')
        self.add('https://shop/c', 204, content_type='text/html')
        m = crawl.metrics(self.db, 1)
        self.assertEqual(m['crawl_title_dup'], 2)
        self.assertEqual(m['crawl_2xx_share'], 100)

    def test_crawl_time_uses_utc_and_never_future(self):
        self.assertIsNone(crawl.latest_for(self.db, 'shop', '2026-10-22T10:59:59+00:00'))
        self.assertEqual(crawl.latest_for(self.db, 'shop', '2026-10-22T11:00:00+00:00')['id'], 1)

    def test_examples_do_not_replace_full_counts(self):
        html = report._items_html([dict(url='x', ok=False, msg='bad')], {'total': 8830, 'bad': 7983})
        self.assertIn('7983', html)
        self.assertIn('примеров: 1', html)

    def test_warning_is_separate_from_error(self):
        result = graded([(Item('a', False), 'fail'), (Item('b', False), 'warn')])
        self.assertEqual((result.stats['failed'], result.stats['warned']), (1, 1))

    def test_unknown_canonical_target_is_not_pass(self):
        self.add('https://shop/a', content_type='text/html', canonical='https://shop/missing')
        result = c_crawl.canonical_target_ok(self.ctx, self.imp)
        self.assertEqual(result.status, WARN)
        self.assertEqual(result.stats['unknown'], 1)

    def test_noindex_outside_registry_is_visible(self):
        self.add('https://shop/product', content_type='text/html', meta_robots='noindex, nofollow')
        result = c_crawl.robots_directive(self.ctx, self.imp, 'noindex')
        self.assertEqual(result.status, WARN)
        self.assertEqual(result.stats['bad'], 1)

    def test_synthetic_200_without_error_text_is_only_candidate(self):
        ctx = SimpleNamespace(primary=lambda: [], probes=lambda *k: [dict(status=200, url='https://shop/missing')],
                              probe_body=lambda row: b'<html><title>Welcome</title></html>')
        self.assertEqual(x_meta.soft_404(ctx).status, WARN)
        ctx.probe_body = lambda row: b'<html><title>404 not found</title></html>'
        self.assertEqual(x_meta.soft_404(ctx).status, FAIL)

    def test_redirect_code_policy_keeps_target_validation(self):
        from techaudit.probes import evaluate_redirect
        res = SimpleNamespace(error=None, chain=[dict(status=308), dict(status=200)],
                              final_url='https://shop/a', status=200)
        exp = dict(first_status=301, allowed_first_status=[301, 308], hops=2, target='https://shop/a', status=200)
        self.assertEqual(evaluate_redirect(res, exp), [])
        exp['target'] = 'https://shop/b'
        self.assertIn('wrong_target', evaluate_redirect(res, exp))

    def test_missing_lighthouse_metrics_cannot_pass(self):
        page = SimpleNamespace(url='https://shop/a', page_type='product')
        def run(ctx, pages, fn, what):
            ok, msg = fn(page)
            return [Item(page.url, ok, msg)], [], None
        pool = SimpleNamespace(summary=lambda x: '')
        with patch.object(c_external, '_off', return_value=None), \
             patch.object(c_external, '_psi_pages', return_value=[page]), \
             patch.object(c_external, '_psi_result', return_value=({'cls': 0}, False)), \
             patch.object(c_external, '_run_items', side_effect=run), \
             patch.object(c_external, '_psi', return_value=SimpleNamespace(pool=pool)):
            result = c_external.cwv(self.ctx)
        self.assertEqual(result.status, WARN)
        self.assertIn('неполный замер', result.items[0].msg)


if __name__ == '__main__':
    unittest.main()
