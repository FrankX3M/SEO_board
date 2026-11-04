"""Navigation integration tests: URLs, site context and separated workflows."""
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from tests.test_web import make_app


class NavigationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.client = TestClient(make_app(Path(cls.tmp.name)))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def test_workflows(self):
        for path in ('/', '/checks', '/crawl', '/orphans', '/orphans?view=sources',
                     '/registry', '/runs', '/runs?view=new', '/runs?view=queue',
                     '/schedules', '/ai', '/settings', '/sites/5ka', '/reference?tab=types'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn('aria-label="Основные разделы"', response.text)
        history = self.client.get('/runs').text
        self.assertNotIn('action="/runs/start"', history)
        self.assertIn('action="/runs/start"', self.client.get('/runs?view=new').text)
        self.assertIn('Очередь и задания', self.client.get('/runs?view=queue').text)
        sources = self.client.get('/orphans?view=sources&net=5ka').text
        analysis = self.client.get('/orphans?net=5ka').text
        self.assertIn('action="/orphans/upload"', sources)
        self.assertNotIn('action="/orphans/upload"', analysis)
        self.assertNotIn('Результаты сравнений', sources)
        self.assertIn('Результаты сравнений', analysis)

    def test_site_context_and_explicit_override(self):
        self.client.get('/registry?site=5ka')
        page = self.client.get('/runs?view=new').text
        self.assertIn('name="networks" value="5ka" checked', page)
        self.assertIn('/checks?net=5ka', page)
        page = self.client.get('/runs?view=new&net=all').text
        self.assertNotIn('name="networks" value="5ka" checked', page)


if __name__ == '__main__':
    unittest.main()
