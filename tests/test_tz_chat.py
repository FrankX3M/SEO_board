"""Чат под ТЗ на странице блока: разбор ответа ИИ, сборка сообщений, эндпоинты (ИИ подменён).

    python -m unittest tests.test_tz_chat
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from techaudit import tz_chat as tc
from tests import test_web2 as tw


class ParseTest(unittest.TestCase):
    def test_plain_answer(self):
        self.assertEqual(tc.parse_reply("Потому что H1 нет."), ("Потому что H1 нет.", None))

    def test_tz_block(self):
        say, tz = tc.parse_reply("Сократил.\n<TZ>\nНазвание\nX\n</TZ>")
        self.assertEqual(say, "Сократил.")
        self.assertEqual(tz, "Название\nX")

    def test_tz_only(self):
        say, tz = tc.parse_reply("<tz>Текст</tz>")
        self.assertEqual((say, tz), ("ТЗ обновлено.", "Текст"))

    def test_truncated_tz_not_applied(self):
        say, tz = tc.parse_reply("Правлю.\n<TZ>Название\nобрыв")
        self.assertIsNone(tz)
        self.assertIn("оборвался", say)

    def test_empty_tz_ignored(self):
        self.assertIsNone(tc.parse_reply("ок <TZ> </TZ>")[1])


class MessagesTest(unittest.TestCase):
    def test_roles_alternate_and_tz_at_end(self):
        hist = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1", "tz": True},
                {"role": "user", "content": "q2"}, {"role": "user", "content": "q3"}]
        m = tc.build_messages({"block": "B"}, hist, "МОЁ ТЗ", "новый вопрос")
        roles = [x["role"] for x in m]
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)
        self.assertEqual(roles[0], "user")
        self.assertEqual(roles[-1], "user")
        self.assertIn("ТЕКУЩЕЕ ТЗ:\nМОЁ ТЗ", m[-1]["content"])
        self.assertIn("новый вопрос", m[-1]["content"])
        self.assertIn('"block": "B"', m[0]["content"])
        self.assertIn("обновлён", json.dumps(m, ensure_ascii=False))

    def test_history_limit_and_empty_tz(self):
        hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(100)]
        m = tc.build_messages({}, hist, "", "x")
        self.assertLessEqual(len(m), 2 + tc.MAX_HISTORY + 1)
        self.assertIn("(пока пусто)", m[-1]["content"])

    def test_clean_history(self):
        self.assertEqual(tc.clean_history([{"role": "system", "content": "x"}, {"role": "user", "content": "ok"}, "junk"]),
                         [{"role": "user", "content": "ok", "tz": False, "err": False}])


@unittest.skipIf(TestClient is None, "нет fastapi/httpx")
class ChatEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.app = tw.make_app(cls.tmp)
        cls.c = TestClient(cls.app, follow_redirects=False)
        tw.Web2Test._seed.__func__(cls)
        cls.app.state.settings.integrations["ai"] = "test-key"
        from techaudit import blocks
        cls.block = blocks.check_index()["links_404"]["block"]
        cls.form = {"name": cls.block, "net": "5ka", "p": "90", "from": "", "to": "", "run": ""}

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        cls._tmp.cleanup()

    def _post(self, reply, msg="сократи", tz="СТАРОЕ ТЗ", path="/block/chat"):
        with mock.patch("techaudit.ai.AIClient.chat_messages", return_value=reply) as m:
            r = self.c.post(path, data={**self.form, "msg": msg, "tz_current": tz})
        return r, m

    def test_1_question_keeps_tz(self):
        r, m = self._post("Потому что на странице нет описания.", msg="почему это важно?")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("почему это важно?", r.text)
        self.assertIn("нет описания", r.text)
        self.assertNotIn("hx-swap-oob", r.text)
        sent = m.call_args.args[1]
        self.assertIn("ТЕКУЩЕЕ ТЗ:\nСТАРОЕ ТЗ", sent[-1]["content"])  # уходит текст из поля, а не из БД
        self.assertIn("Нет внутренних ссылок на 404", sent[0]["content"])  # данные блока
        self.assertNotIn("SECRET", json.dumps(sent))

    def test_2_tz_update_swaps_editor_and_saves(self):
        r, _ = self._post("Сократил.\n<TZ>\nНовое ТЗ\n1. пункт\n</TZ>", msg="сократи")
        self.assertIn('hx-swap-oob="innerHTML:#tz-box"', r.text)
        self.assertIn("Новое ТЗ", r.text)
        self.assertIn("Текст ТЗ выше обновлён", r.text)
        page = self.c.get(f"/block?name={quote(self.block)}&net=5ka&p=90")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Новое ТЗ", page.text)         # ТЗ сохранилось
        self.assertIn("сократи", page.text)           # история сохранилась
        self.assertIn('id="chat-form"', page.text)

    def test_3_history_goes_to_model(self):
        _, m = self._post("ок", msg="второй вопрос", tz="Новое ТЗ")
        sent = json.dumps(m.call_args.args[1], ensure_ascii=False)
        self.assertIn("сократи", sent)
        self.assertIn("второй вопрос", sent)

    def test_4_ai_error_keeps_user_message_not_saved(self):
        from techaudit.ai import AIError
        with mock.patch("techaudit.ai.AIClient.chat_messages", side_effect=AIError("HTTP 500")):
            r = self.c.post("/block/chat", data={**self.form, "msg": "вопрос при сбое", "tz_current": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("вопрос при сбое", r.text)
        self.assertIn("HTTP 500", r.text)
        self.assertNotIn("вопрос при сбое", self.c.get(f"/block?name={quote(self.block)}&net=5ka&p=90").text)

    def test_5_no_key(self):
        self.app.state.settings.integrations["ai"] = ""
        try:
            r = self.c.post("/block/chat", data={**self.form, "msg": "привет", "tz_current": ""})
            self.assertIn("ИИ не подключён", r.text)
        finally:
            self.app.state.settings.integrations["ai"] = "test-key"

    def test_6_empty_message_and_clear(self):
        r = self.c.post("/block/chat", data={**self.form, "msg": "  ", "tz_current": ""})
        self.assertEqual(r.status_code, 200)
        r = self.c.post("/block/chat/clear", data=self.form)
        self.assertIn("Пока пусто", r.text)
        page = self.c.get(f"/block?name={quote(self.block)}&net=5ka&p=90")
        self.assertNotIn("второй вопрос", page.text)
        self.assertIn("Новое ТЗ", page.text)  # ТЗ чистка диалога не трогает


if __name__ == "__main__":
    unittest.main()
