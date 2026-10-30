import csv
import io
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from techaudit import crawl
from techaudit.metric_exports import crawl_selection, download_url
from techaudit.webdb import connect
from tests.test_web2 import make_app


class MetricExportsTest(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        self.addCleanup(self.restore_env)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = make_app(Path(self.tmp.name))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.db = connect(self.app.state.db_path)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO crawl_imports(id,network,taken_at,uploaded_at,rows) VALUES(1,'5ka','2026-10-20','2026-10-20',610)")
        self.db.execute("INSERT INTO crawl_imports(id,network,taken_at,uploaded_at,rows) VALUES(2,'other','2026-10-21','2026-10-21',1)")
        self.db.executemany("INSERT INTO crawl_pages(import_id,address,content_type,status,indexability,title,h1,canonical,inlinks,depth) VALUES(1,?,'text/html',404,'Non-Indexable','','','',2,1)",
                            [(f'https://5ka.example/missing/{i}',) for i in range(605)])
        for url, title in [('https://5ka.example/a', ' SAME  title '), ('https://5ka.example/b', 'same title'), ('https://5ka.example/c', 'Unique')]:
            self.db.execute("INSERT INTO crawl_pages(import_id,address,content_type,status,indexability,title,h1,meta_desc,canonical,inlinks,depth) VALUES(1,?,'text/html',200,'Indexable',?,'H1','Description',?,1,1)", (url,title,url))
        self.db.execute("INSERT INTO crawl_pages(import_id,address,content_type,status,indexability,title,h1,canonical) VALUES(1,'https://5ka.example/page2','text/html',200,'Non-Indexable','Pagination','H1','https://5ka.example/c')")
        self.db.execute("INSERT INTO crawl_pages(import_id,address,content_type,status) VALUES(1,'https://5ka.example/no-answer','text/html',0)")

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.env)

    def get_csv(self, metric, point='crawl:1', net='5ka'):
        response = self.client.get('/export/metric-urls.csv', params={'metric': metric, 'point': point, 'net': net})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('attachment;', response.headers['content-disposition'])
        return list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff')), delimiter=';'))

    def test_exports_all_matching_pages_without_sample_cap(self):
        rows = self.get_csv('crawl_4xx')
        self.assertEqual(len(rows), 605)
        self.assertTrue(all(r['HTTP-статус'] == '404' for r in rows))
        self.assertEqual({r['ID краула'] for r in rows}, {'1'})

    def test_link_counts_have_destination_rows_and_inlinks(self):
        rows = self.get_csv('crawl_links_to_404')
        self.assertEqual(len(rows), 605)
        self.assertEqual(sum(int(r['Inlinks']) for r in rows), crawl.metrics(self.db, 1)['crawl_links_to_404'])

    def test_duplicate_title_normalization_matches_metric(self):
        rows = self.get_csv('crawl_title_dup')
        self.assertEqual({r['URL'] for r in rows}, {'https://5ka.example/a','https://5ka.example/b'})
        self.assertEqual(len(rows), crawl.metrics(self.db, 1)['crawl_title_dup'])

    def test_share_exports_failed_side_and_preserves_zero(self):
        self.assertEqual(len(self.get_csv('crawl_2xx_share')), 606)
        rows = self.get_csv('crawl_no_response')
        self.assertEqual(rows[0]['HTTP-статус'], '0')

    def test_combined_export_has_reasons_and_accepts_pagination(self):
        rows = self.get_csv('crawl_problems')
        self.assertEqual(len(rows), 608)
        self.assertNotIn('https://5ka.example/page2', {r['URL'] for r in rows})
        self.assertTrue(all(r['Причина / критерий выборки'] for r in rows))

    def test_wrong_network_unknown_metric_and_missing_import(self):
        for metric, point, network, code in [('crawl_4xx','crawl:2','5ka',404), ('bad','crawl:1','5ka',400),
                                             ('crawl_4xx','crawl:999','5ka',404), ('crawl_4xx','crawl:abc','5ka',400)]:
            r = self.client.get('/export/metric-urls.csv', params={'metric':metric,'point':point,'net':network})
            self.assertEqual(r.status_code, code)

    def test_empty_selection_returns_headers(self):
        self.assertEqual(self.get_csv('crawl_5xx'), [])

    def test_download_links_pin_selected_data_point(self):
        url = download_url('crawl_4xx','crawl:1','5ka')
        self.assertIn('point=crawl%3A1', url)
        self.assertIn('run=old-run', download_url('check:alt','run:old-run','5ka'))
        self.assertIsNone(download_url('crawl_4xx','run:old-run','5ka'))
        self.assertIsNone(download_url('ttfb_avg','run:old-run','5ka'))

    def test_dashboard_and_block_have_downloads_for_selected_period(self):
        self.db.execute("INSERT INTO crawl_imports(id,network,taken_at,uploaded_at,rows) VALUES(3,'5ka','2026-10-25','2026-10-25',0)")
        params = {'net':'5ka', 'from':'2026-10-20', 'to':'2026-10-20'}
        board = self.client.get('/api/board', params=params).json()
        card = board['cards']['crawl_4xx']
        self.assertEqual(card['point'], 'crawl:1')
        self.assertIn('point=crawl%3A1', card['download_url'])
        block = self.client.get('/block', params={**params, 'name':'Итоговый краул'})
        self.assertEqual(block.status_code, 200)
        self.assertIn('metric=crawl_4xx', block.text)
        overview = self.client.get('/', params=params)
        self.assertIn('metric=crawl_problems', overview.text)
        explore = self.client.get('/explore', params=params)
        self.assertEqual(explore.status_code, 200)
        self.assertIn('download_url', explore.text)
        self.assertIn('Выбранная точка', explore.text)


if __name__ == '__main__':
    unittest.main()
