"""Дашборд v3: обзор по блокам, страница блока, выгрузки URL, переименование «Проба» → «Тест адреса».

    python -m unittest tests.test_dashboard_v3
"""
import json
import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from techaudit import explain
from techaudit import metrics as mx
from tests.test_web2 import make_app


class LabelsTest(unittest.TestCase):
    def test_probe_renamed(self):
        self.assertIn("Тест адреса", mx.label("probe:slash"))
        self.assertNotIn("Проб", mx.label("probe:slash"))
        self.assertEqual(mx.CATALOG["probes_total"][3], "Тесты адресов")

    def test_help_exists_for_catalog(self):
        for mid in mx.CATALOG:
            self.assertTrue(explain.metric_help(mid)["what"], mid)

    def test_chart_pairs_incompatible_units(self):
        ms = [{"id": "a", "label": "A", "unit": "%"}, {"id": "b", "label": "B", "unit": "шт"}, {"id": "c", "label": "C", "unit": "мс"}]
        ch = mx.metric_charts(ms, {"a": [1, 2], "b": [3, 4], "c": [5, 6]})
        self.assertEqual(len(ch), 2)  # % + шт на одном графике, мс отдельно
        self.assertEqual({d["axis"] for d in ch[0]["datasets"]}, {"y", "y1"})


@unittest.skipIf(TestClient is None, "нет fastapi")
class PagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = TestClient(make_app(Path(self.tmp.name)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_pages_open_on_empty_db(self):
        for u in ("/", "/explore", "/block?name=%D0%A0%D0%B5%D0%B4%D0%B8%D1%80%D0%B5%D0%BA%D1%82%D1%8B&net=5ka", "/api/overview?net=5ka"):
            r = self.c.get(u)
            self.assertIn(r.status_code, (200, 404), u)
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_exports_need_run(self):
        for u in ("/export/urls.csv", "/export/tests.csv", "/export/pages.csv"):
            self.assertEqual(self.c.get(u).status_code, 404, u)


if __name__ == "__main__":
    unittest.main()
