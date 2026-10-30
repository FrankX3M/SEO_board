"""Regression cases from the manual review of the 5ka audit."""
import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from techaudit.ai import AIClient, AIError, SCHEMA, parse_json_object
from techaudit.probes import ProbeContext, ProbeSpec, evaluate_redirect
from techaudit.rawfetch import RawResult
from techaudit.checks.c_http import _redirect_check
from techaudit.checks.c_seo import canonical_pagination
from techaudit.checks.guidance import normalize_rows, META_BLOCK
from techaudit.checks.x_server import bot_head_parity
from techaudit.checks.core import CheckSettings, PageDoc


class ManualReviewTest(unittest.TestCase):
    def test_permanent_redirect_codes_and_temporary_redirect_failure(self):
        for code in (301, 308, 302):
            r = RawResult('https://s.test/a', 'https://s.test/a/', 200,
                          [{'url': 'https://s.test/a', 'status': code}, {'url': 'https://s.test/a/', 'status': 200}])
            reasons = evaluate_redirect(r, {'target': r.final_url, 'status': 200, 'hops': 2})
            self.assertEqual(bool(reasons), code == 302)

    def test_collect_normalizes_fixture_before_testing_http_and_slash(self):
        target = 'https://s.test/a'
        baseline = RawResult(target, target + '/', 200, [{'url': target, 'status': 308}, {'url': target + '/', 'status': 200}])
        async def run():
            ctx = ProbeContext.__new__(ProbeContext)
            seen = []
            async def fetch(url):
                seen.append(url)
                return baseline if url == target else RawResult(url, target + '/', 200, [{'url': url, 'status': 301}, {'url': target + '/', 'status': 200}])
            ctx.fetch, ctx.record = fetch, Mock()
            exp = {'target': target, 'status': 200, 'hops': 2}
            await ctx.run_spec(ProbeSpec('http', 'listing', target, 'http://s.test/a', exp))
            self.assertEqual(seen, [target, 'http://s.test/a/'])
            self.assertEqual(ctx.record.call_args.kwargs['expected']['target'], target + '/')
            self.assertEqual(ctx.record.call_args.args[3], [])
        asyncio.run(run())

    def test_historical_slash_fixture_is_warning_with_chain(self):
        url = 'https://s.test/a/'
        row = {'url': url, 'kind': 'slash', 'status': 200, 'final_url': url,
               'expected_d': {'target': url[:-1]}, 'chain': json.dumps([{'url': url, 'status': 200}]), 'reason': 'no_redirect_200_duplicate'}
        ctx = SimpleNamespace(probes=lambda *k: [row], pages=lambda v: [SimpleNamespace(url=url[:-1], final_url=url)])
        result = _redirect_check(ctx, 'slash')
        self.assertEqual(result.status, 'warn')
        self.assertIn('200 https://s.test/a/', result.items[0].msg)
        self.assertIn('повторный сбор', result.items[0].msg)

    def test_pagination_canonical_to_first_is_approved(self):
        row = {'url': 'https://s.test/a/?page=2', 'final_url': 'https://s.test/a/?page=2', 'status': 200,
               'meta_d': {'canonical': 'https://s.test/a/', 'clean_canonical': 'https://s.test/a/'}}
        self.assertEqual(canonical_pagination(SimpleNamespace(live=lambda k: [row])).status, 'pass')

    def test_bot_render_only_compares_equivalent_rendered_pages(self):
        def page(variant, title):
            return PageDoc('5ka', 'listing', variant, '/a', 'https://s.test/a', 'https://s.test/a', 200, {}, 1,
                           f'<html><head><title>{title}</title></head><body><h1>{title}</h1></body></html>'.encode(), 'https://s.test')
        docs = {'plain': [page('plain', 'Shell')], 'bot': [page('bot', 'Category')], 'browser': [page('browser', 'Category')]}
        ctx = SimpleNamespace(network='5ka', pages=lambda v: docs.get(v, []))
        with patch.dict('os.environ', {'BOT_RENDER_ONLY': '1'}):
            self.assertEqual(bot_head_parity(ctx).status, 'pass')

    def test_grouping_retains_each_check(self):
        rows = [{'block': b, 'check_id': cid, 'title': cid} for b, cid in [('Микроразметка', 'schema_product'), ('Open Graph', 'og_url'), ('Мета-теги и заголовки', 'title_quality')]]
        self.assertEqual({r['block'] for r in normalize_rows(rows)}, {META_BLOCK})
        self.assertEqual(len(rows), 3)

    def test_json_retry_does_not_cache_invalid_output(self):
        db = sqlite3.connect(':memory:')
        db.executescript(SCHEMA)
        client = AIClient.__new__(AIClient)
        client.db, client.model = db, 'test'
        client.chat = Mock(side_effect=['{"summary": "broken"', '{"summary":"ok"}'])
        self.assertEqual(client.chat_json('system', 'data'), {'summary': 'ok'})
        self.assertEqual(client.chat.call_count, 2)
        db.close()

    def test_json_rejects_partial_arrays_and_multiple_objects(self):
        for value in ('{"a":', '[]', '{} {}'):
            with self.assertRaises(AIError):
                parse_json_object(value)
        self.assertEqual(parse_json_object('```json\n{"a":1}\n```'), {'a': 1})


if __name__ == '__main__':
    unittest.main()
