"""Устойчивость цикла агента: настоящие SessionManager / AssistantService + мок слайсера
против поддельной OpenAI-совместимой LLM (devtools/fake_llm.py).

Поток теста играет роль главного потока Qt (крутит очередь callLater), а ход агента идёт
в своём рабочем потоке — ровно как внутри STE Slicer.

Запуск из корня репозитория: python -m unittest tests.test_agent_loop
"""

import json
import os
import queue
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zlib

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_DIR, "plugin", "AIAssistant")
DEVTOOLS_DIR = os.path.join(REPO_DIR, "devtools")
for path in (PLUGIN_DIR, DEVTOOLS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent import main_thread  # noqa: E402
from agent.chat_agent import LoopGuard  # noqa: E402
from agent.llm_client import LLMClient, _UNSUPPORTED  # noqa: E402
from agent.service import AssistantService  # noqa: E402
from agent.slicer import set_adapter  # noqa: E402
from agent.tools import ENABLE_TOOLS_NAME, EXTRA_TOOLS, build_registry  # noqa: E402
import fake_llm  # noqa: E402
from fake_llm import call, cut, http_error, reply, slow  # noqa: E402
import mock_slicer  # noqa: E402


class FakeQtApp(object):
    def __init__(self):
        self.thread = threading.current_thread()
        self.queue = queue.Queue()

    def getMainThread(self):
        return self.thread

    def callLater(self, func, *args, **kwargs):
        self.queue.put((func, args, kwargs))


def write_png(path, width, height, rgba=False):
    """Крошечный корректный PNG (только stdlib)."""
    channels = 4 if rgba else 3
    row = b"\x00" + b"\x80" * (width * channels)
    raw = row * height

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6 if rgba else 2, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) +
                     chunk(b"IEND", b""))


class AgentLoopTest(unittest.TestCase):
    timeout = 1.0   # таймаут сокета клиента LLM в этих тестах

    def setUp(self):
        _UNSUPPORTED.clear()
        self.tmp = tempfile.mkdtemp(prefix="aiassistant_loop_")
        self.app = FakeQtApp()
        main_thread.set_app_provider(lambda: self.app)
        self.registry = build_registry()
        self.mock = mock_slicer.install(self.registry, slice_step=0.01)
        self.llm = fake_llm.FakeLLMServer().start()
        self.clients = []

        def client_factory():
            client = LLMClient(self.llm.url, "test-key", "fake-qwen", timeout=self.timeout, max_retries=3)
            client.backoff_base = 0.01
            self.clients.append(client)
            return client

        self.service = AssistantService(data_dir=self.tmp, registry=self.registry, client_factory=client_factory,
                                        host_name="test")
        self.service.settings.update({"base_url": self.llm.url, "api_key": "test-key", "model": "fake-qwen"})
        self.sessions = self.service.sessions

    def tearDown(self):
        for session in self.sessions.list_sessions():
            self.sessions.stop(session["id"])
        self.service.server.stop()
        self.llm.stop()
        main_thread.set_app_provider(None)
        set_adapter(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Вспомогательное
    def run_turn(self, text, steps=(), session=None, attachments=None, stop_after=None, wait=30.0):
        """Отправляет сообщение и крутит «главный поток», пока ход не кончится; возвращает (диалог, события)."""
        self.llm.push(*steps)
        session = session or self.sessions.create_session()
        first_seq = session.events.last_seq
        self.sessions.send_message(session.id, text, attachments)
        started = time.time()
        stopped = False
        while True:
            try:
                func, args, kwargs = self.app.queue.get(timeout=0.02)
                func(*args, **kwargs)
            except queue.Empty:
                pass
            if stop_after is not None and not stopped and time.time() - started > stop_after:
                self.sessions.stop(session.id)
                stopped = True
            if not session.running and any(e["type"] == "done" for e in session.events.since(first_seq)):
                break
            self.assertLess(time.time() - started, wait, "turn did not finish")
        return session, session.events.since(first_seq)

    def done_reason(self, events):
        return [e for e in events if e["type"] == "done"][-1]["reason"]

    def of_type(self, events, kind):
        return [e for e in events if e["type"] == kind]

    def assert_history_consistent(self, session):
        """На каждый tool_call ассистента ровно один ответ tool с тем же id, аргументы — валидный JSON."""
        pending = []
        ids = set()
        for entry in session.history:
            self.assertLessEqual(set(entry), {"role", "content", "tool_calls", "tool_call_id"})
            if entry["role"] == "tool":
                self.assertIn(entry["tool_call_id"], pending)
                pending.remove(entry["tool_call_id"])
                continue
            self.assertEqual(pending, [], "unanswered tool calls before %r" % entry)
            for tool_call in entry.get("tool_calls") or []:
                self.assertNotIn(tool_call["id"], ids)
                ids.add(tool_call["id"])
                self.assertIsInstance(json.loads(tool_call["function"]["arguments"]), dict)
                pending.append(tool_call["id"])
        self.assertEqual(pending, [])
        self.assertEqual(self.llm.violations, [])

    def tool_results(self, events):
        return dict((e["name"], e) for e in self.of_type(events, "tool_call_result"))

    # Тесты
    def test_parallel_tool_calls_events_and_request_defaults(self):
        session, events = self.run_turn("Настрой 3D печать", [
            reply(reasoning="Смотрю конфигурацию", tool_calls=[
                call("get_current_config"), call("list_materials", {"query": "PETG"}),
                call("set_print_mode", {"mode": "classic"})]),
            reply("Готово: режим 3D."),
        ])
        self.assertEqual(self.done_reason(events), "completed")
        results = self.of_type(events, "tool_call_result")
        self.assertEqual([r["name"] for r in results], ["get_current_config", "list_materials", "set_print_mode"])
        self.assertTrue(all(r["ok"] for r in results), [r["result"][:200] for r in results])
        self.assertEqual(self.mock.user["global"]["printing_mode"], "classic")
        types = [e["type"] for e in events]
        for kind in ("user_message", "turn_started", "reasoning_delta", "tool_call_started", "assistant_delta",
                     "usage", "done"):
            self.assertIn(kind, types)
        self.assertEqual(session.history[-1], {"role": "assistant", "content": "Готово: режим 3D."})
        self.assert_history_consistent(session)
        first = self.llm.chat_requests[0]
        self.assertIs(first["parallel_tool_calls"], False)
        self.assertEqual(first["temperature"], 0.2)
        self.assertTrue(first["stream"])
        names = [t["function"]["name"] for t in first["tools"]]
        self.assertIn(ENABLE_TOOLS_NAME, names)
        self.assertNotIn("scale_object", names)                 # редкие инструменты по умолчанию не отправляются
        self.assertIn("rotate_object", names)
        self.assertNotIn("additionalProperties", json.dumps(first["tools"]))
        # Во втором запросе ответы инструментов с теми же id.
        second = self.llm.chat_requests[1]["messages"]
        tool_ids = [m["tool_call_id"] for m in second if m["role"] == "tool"]
        self.assertEqual(tool_ids, [c["id"] for c in second[-4]["tool_calls"]])

    def test_broken_arguments_are_repaired_or_explained(self):
        session, events = self.run_turn("Проверь", [
            reply(tool_calls=[
                call("list_materials", '```json\n{"query": "PETG"}\n```'),     # блок ``` markdown
                call("get_current_config", ""),                                 # пустая строка
                call("get_print_guidelines", '"modes"'),                         # голое значение
                call("set_print_mode", '{"mode": "classic"'),                   # битый JSON
            ]),
            reply(tool_calls=[call("list_objects", "5")]),                       # число строкой
            reply(tool_calls=[call("search_settings", "{'query': 'заполнение'}")]),  # словарь в стиле Python
            reply("Итог."),
        ])
        self.assertEqual(self.done_reason(events), "completed")
        results = self.of_type(events, "tool_call_result")
        by_name = dict((r["name"], r) for r in results)
        self.assertTrue(by_name["list_materials"]["ok"], by_name["list_materials"]["result"])
        self.assertTrue(by_name["get_current_config"]["ok"])
        self.assertTrue(by_name["get_print_guidelines"]["ok"], by_name["get_print_guidelines"]["result"])
        self.assertFalse(by_name["set_print_mode"]["ok"])
        self.assertIn("корректным JSON", by_name["set_print_mode"]["result"])
        self.assertFalse(by_name["list_objects"]["ok"])
        self.assertIn("JSON-объектом", by_name["list_objects"]["result"])
        self.assertTrue(by_name["search_settings"]["ok"], by_name["search_settings"]["result"])
        self.assert_history_consistent(session)    # битые аргументы в API повторно не уходят

    def test_failed_call_skips_the_rest_of_the_batch(self):
        session, events = self.run_turn("Загрузи", [
            reply(tool_calls=[call("load_model", {"path": "/nope/missing.stl"}), call("list_objects"),
                              call("start_slicing")]),
            reply("Не получилось загрузить модель."),
        ])
        results = self.of_type(events, "tool_call_result")
        self.assertFalse(results[0]["ok"])
        self.assertIn("Не выполнено", results[1]["result"])
        self.assertIn("Не выполнено", results[2]["result"])
        self.assert_history_consistent(session)

    def test_unknown_and_prefixed_tool_names(self):
        session, events = self.run_turn("?", [
            reply(tool_calls=[call("functions.list_objects"), call("make_coffee", "{}")]),
            reply("Ок."),
        ])
        results = self.of_type(events, "tool_call_result")
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertIn("Неизвестный инструмент «make_coffee»", results[1]["result"])
        self.assertLess(len(results[1]["result"]), 300)
        self.assertEqual(self.done_reason(events), "completed")
        self.assert_history_consistent(session)

    def test_repeated_failing_call_is_refused_and_loop_stops(self):
        # Модель настаивает на одном и том же вызове; когда просим итог (tool_choice=none), сдаётся.
        self.llm.default = lambda body: (reply("Материал PEEK недоступен.") if body.get("tool_choice") == "none"
                                         else reply(tool_calls=[call("set_material", {"material_id": "PEEK"})]))
        session, events = self.run_turn("Поставь PEEK", [])
        self.assertEqual(self.done_reason(events), "loop")
        results = self.of_type(events, "tool_call_result")
        self.assertIn("только что завершился ошибкой", results[1]["result"])
        self.assertLessEqual(len(results), 5)
        final = self.llm.chat_requests[-1]
        self.assertEqual(final.get("tool_choice"), "none")
        self.assertIn("повторяешь", final["messages"][-1]["content"])
        self.assertEqual(session.history[-1]["content"], "Материал PEEK недоступен.")
        self.assertIn("loop", [e.get("kind") for e in self.of_type(events, "error")])
        self.assert_history_consistent(session)

    def test_repeated_successful_call_loop(self):
        steps = [reply(tool_calls=[call("list_objects")]) for _ in range(12)]
        session, events = self.run_turn("?", steps + [reply("Итог")])
        self.assertEqual(self.done_reason(events), "loop")
        oks = [r["ok"] for r in self.of_type(events, "tool_call_result")]
        self.assertEqual(oks[:2], [True, True])
        self.assertFalse(oks[2])
        self.assert_history_consistent(session)

    def test_iteration_limit_asks_for_final_answer(self):
        self.service.settings.update({"max_iterations": 3})
        topics = ["modes", "supports", "adhesion", "quality"]
        steps = [reply(tool_calls=[call("get_print_guidelines", {"topic": t})]) for t in topics]
        session, events = self.run_turn("?", steps[:3] + [reply("Сделано частично.")])
        self.assertEqual(self.done_reason(events), "max_iterations")
        self.assertIn("max_iterations", [e.get("kind") for e in self.of_type(events, "error")])
        self.assertEqual(len(self.llm.chat_requests), 4)
        self.assertEqual(self.llm.chat_requests[-1]["tool_choice"], "none")
        self.assertEqual(session.history[-1]["content"], "Сделано частично.")
        self.assert_history_consistent(session)

    def test_final_answer_ignores_tool_calls(self):
        self.service.settings.update({"max_iterations": 1})
        session, events = self.run_turn("?", [reply(tool_calls=[call("list_objects")]),
                                              reply(tool_calls=[call("list_objects")])])
        self.assertEqual(self.done_reason(events), "max_iterations")
        self.assertEqual(session.history[-1]["role"], "tool")
        self.assert_history_consistent(session)

    def test_reasoning_without_content_is_nudged_once(self):
        session, events = self.run_turn("Привет", [reply(reasoning="Думаю…"), reply("Здравствуйте!")])
        self.assertEqual(self.done_reason(events), "completed")
        nudge = self.llm.chat_requests[1]["messages"][-1]
        self.assertEqual(nudge["role"], "system")
        self.assertIn("пустым", nudge["content"])
        self.assertEqual(session.history[-1]["content"], "Здравствуйте!")
        self.assertEqual(len(session.history), 2)    # пустое сообщение ассистента в историю не попадает

        session, events = self.run_turn("Ещё", [reply(reasoning="…"), reply(reasoning="…")])
        self.assertEqual(self.done_reason(events), "empty")
        self.assertIn("empty_answer", [e.get("kind") for e in self.of_type(events, "error")])
        self.assert_history_consistent(session)

    def test_think_tags_are_moved_out_of_the_answer(self):
        session, _events = self.run_turn("?", [reply("<think>план</think>Ответ.")])
        self.assertEqual(session.history[-1]["content"], "Ответ.")

    def test_stream_cut_after_partial_output_is_retried(self):
        session, events = self.run_turn("?", [cut("Начало ответа, которое обрывается", cut_after_chars=10),
                                              reply("Полный ответ.")])
        self.assertEqual(self.done_reason(events), "completed")
        self.assertIn("retry", [e.get("kind") for e in self.of_type(events, "error")])
        message_ids = set(e["message_id"] for e in self.of_type(events, "assistant_delta"))
        self.assertEqual(len(message_ids), 2)          # повтор — отдельное сообщение в панели
        self.assertEqual(session.history[-1]["content"], "Полный ответ.")

    def test_stream_cut_inside_tool_call_and_before_output(self):
        session, events = self.run_turn("?", [
            cut(tool_calls=[call("set_print_mode", {"mode": "classic"})]),   # обрыв до конца, вывода ещё не было
            reply(tool_calls=[call("set_print_mode", {"mode": "classic"})]),
            reply("Ок."),
        ])
        self.assertEqual(self.done_reason(events), "completed")
        self.assertNotIn("retry", [e.get("kind") for e in self.of_type(events, "error")])  # клиент тихо повторил запрос
        self.assertEqual(len(self.of_type(events, "tool_call_result")), 1)
        self.assert_history_consistent(session)

    def test_http_429_and_500_are_retried(self):
        session, events = self.run_turn("?", [http_error(429, "Too many requests", retry_after=0),
                                              http_error(500, "boom"), http_error(503, "busy"),
                                              reply("Ответ после ретраев.")])
        self.assertEqual(self.done_reason(events), "completed")
        self.assertEqual(len(self.llm.chat_requests), 4)
        self.assertEqual(self.of_type(events, "error"), [])

    def test_retries_are_limited(self):
        session, events = self.run_turn("?", [http_error(500, "boom")] * 5)
        self.assertEqual(self.done_reason(events), "error")
        errors = self.of_type(events, "error")
        self.assertEqual(errors[-1]["kind"], "server")
        self.assertIn("HTTP 500", errors[-1]["message"])
        self.assertEqual(len(self.llm.chat_requests), 4)        # 1 + max_retries

    def test_auth_error_is_not_retried(self):
        self.llm.require_key = "other"
        session, events = self.run_turn("?", [])
        self.assertEqual(self.done_reason(events), "error")
        self.assertEqual(self.of_type(events, "error")[-1]["kind"], "auth")

    def test_rejected_optional_parameter_falls_back(self):
        self.llm.reject_params = {"parallel_tool_calls"}
        session, events = self.run_turn("?", [reply("Ок.")])
        self.assertEqual(self.done_reason(events), "completed")
        self.assertIn("parallel_tool_calls", self.llm.requests[0])
        self.assertNotIn("parallel_tool_calls", self.llm.requests[1])
        # запомнено для адреса: на следующем ходу параметр уже не шлём
        self.run_turn("ещё", [reply("Ок.")], session=session)
        self.assertNotIn("parallel_tool_calls", self.llm.requests[-1])

    def test_rejected_stream_options_falls_back(self):
        self.llm.reject_params = {"stream_options"}
        session, events = self.run_turn("?", [reply("Ок.")])
        self.assertEqual(self.done_reason(events), "completed")
        self.assertTrue(self.llm.requests[-1]["stream"])
        self.assertNotIn("stream_options", self.llm.requests[-1])

    def test_timeout_is_retried_once_then_reported(self):
        session, events = self.run_turn("?", [slow(2.5), slow(2.5), reply("поздно")], wait=20)
        self.assertEqual(self.done_reason(events), "error")
        self.assertEqual(self.of_type(events, "error")[-1]["kind"], "timeout")
        self.assertEqual(len(self.llm.chat_requests), 2)

    def test_cancel_while_waiting_for_the_llm(self):
        started = time.time()
        session, events = self.run_turn("?", [slow(5)], stop_after=0.3, wait=10)
        self.assertEqual(self.done_reason(events), "cancelled")
        self.assertLess(time.time() - started, 2.5)
        # диалог сразу принимает новое сообщение
        session, events = self.run_turn("снова", [reply("Ок.")], session=session)
        self.assertEqual(self.done_reason(events), "completed")
        self.assert_history_consistent(session)

    def test_cancel_during_tool_execution_answers_every_call(self):
        def slow_handler(args, ctx):
            ctx.cancel_event.wait(5)
            return {"ok": True}
        self.registry.override("get_app_info", slow_handler, main_thread=False)
        session, events = self.run_turn("?", [reply(tool_calls=[call("get_app_info"), call("list_objects")])],
                                        stop_after=0.3, wait=10)
        self.assertEqual(self.done_reason(events), "cancelled")
        results = self.of_type(events, "tool_call_result")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1]["result"], "Отменено пользователем")
        self.assert_history_consistent(session)

    def test_very_long_answer(self):
        long_text = "Очень длинный ответ. " * 10000
        session, events = self.run_turn("?", [reply(long_text, chunk_size=4000)])
        self.assertEqual(self.done_reason(events), "completed")
        self.assertEqual(session.history[-1]["content"], long_text)
        self.run_turn("ещё", [reply("Ок.")], session=session)
        previous = self.llm.chat_requests[-1]["messages"][2]
        self.assertEqual(previous["role"], "assistant")
        self.assertLess(len(previous["content"]), 7000)      # сокращается только в последующих запросах

    def test_length_finish_reason_is_reported(self):
        session, events = self.run_turn("?", [reply("Обрезанный отв", finish_reason="length")])
        self.assertIn("length", [e.get("kind") for e in self.of_type(events, "error")])

    def test_tool_calls_without_id_and_index(self):
        session, events = self.run_turn("?", [
            reply(tool_calls=[call("list_objects", call_id=None), call("get_current_config", call_id=None)],
                  omit_index=True),
            reply("Ок."),
        ])
        names = [r["name"] for r in self.of_type(events, "tool_call_result")]
        self.assertEqual(names, ["list_objects", "get_current_config"])
        self.assert_history_consistent(session)

    def test_parallel_calls_glued_on_index_zero(self):
        session, events = self.run_turn("?", [
            reply(tool_calls=[call("list_objects", {}), call("get_print_guidelines", {"topic": "modes"})],
                  same_index=True),
            reply("Ок."),
        ])
        results = self.of_type(events, "tool_call_result")
        self.assertEqual([r["name"] for r in results], ["list_objects", "get_print_guidelines"])
        self.assertTrue(all(r["ok"] for r in results), [r["result"][:200] for r in results])
        self.assert_history_consistent(session)

    def test_duplicate_tool_call_ids_are_made_unique(self):
        session, events = self.run_turn("?", [
            reply(tool_calls=[call("list_objects", call_id="call_1"),
                              call("get_print_guidelines", {"topic": "modes"}, call_id="call_1")]),
            reply(tool_calls=[call("get_print_guidelines", {"topic": "supports"}, call_id="call_1")]),
            reply("Ок."),
        ])
        self.assertEqual(self.done_reason(events), "completed")
        self.assert_history_consistent(session)

    def test_non_stream_mode(self):
        self.service.settings.update({"stream": False})
        session, events = self.run_turn("?", [
            reply(reasoning="r", tool_calls=[call("list_objects", call_id=None)]), reply("Ок.")])
        self.assertEqual(self.done_reason(events), "completed")
        self.assertFalse(self.llm.chat_requests[0]["stream"])
        self.assertIn("reasoning_delta", [e["type"] for e in events])
        self.assert_history_consistent(session)

    def test_enable_tools_and_direct_call_of_extra_tool(self):
        session, events = self.run_turn("?", [
            reply(tool_calls=[call(ENABLE_TOOLS_NAME, {"names": ["scale_object", "nope"]})]),
            reply(tool_calls=[call("delete_objects", {"all": True})]),      # редкий инструмент вызван напрямую
            reply("Ок."),
        ])
        results = self.of_type(events, "tool_call_result")
        self.assertTrue(results[0]["ok"])
        self.assertIn("scale_object", results[0]["result"])
        self.assertIn("nope", results[0]["result"])
        self.assertTrue(results[1]["ok"], results[1]["result"])
        names = [t["function"]["name"] for t in self.llm.chat_requests[1]["tools"]]
        self.assertIn("scale_object", names)
        names = [t["function"]["name"] for t in self.llm.chat_requests[2]["tools"]]
        self.assertIn("delete_objects", names)
        self.assert_history_consistent(session)

    def test_keywords_enable_extra_tools_and_all_mode(self):
        self.run_turn("Увеличь модель в 2 раза", [reply("Ок.")])
        names = [t["function"]["name"] for t in self.llm.chat_requests[-1]["tools"]]
        self.assertIn("scale_object", names)
        self.service.settings.update({"llm_tools": "all"})
        self.run_turn("?", [reply("Ок.")])
        names = [t["function"]["name"] for t in self.llm.chat_requests[-1]["tools"]]
        self.assertEqual(len(names), len(self.registry.tools()))
        self.assertNotIn(ENABLE_TOOLS_NAME, names)

    def test_mcp_lists_all_tools(self):
        response = self.service.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(names, [tool.name for tool in self.registry.tools()])
        for name in EXTRA_TOOLS:
            self.assertIn(name, names)
        self.assertNotIn(ENABLE_TOOLS_NAME, names)
        self.assertIn("additionalProperties", json.dumps(response["result"]["tools"]))

    def test_image_is_sent_once(self):
        png = os.path.join(self.tmp, "photo.png")
        write_png(png, 20, 10)
        with open(png, "rb") as handle:
            data = handle.read()
        session = self.sessions.create_session()
        import io
        record = self.service.uploads.save_stream(session.id, "photo.png", io.BytesIO(data), len(data))
        session, events = self.run_turn("Что на фото?", [reply(tool_calls=[call("list_objects")]),
                                                         reply("Шестерня.")],
                                        session=session, attachments=[record["id"]])
        first, second = self.llm.chat_requests[0], self.llm.chat_requests[1]
        parts = first["messages"][1]["content"]
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/"))
        self.assertIsInstance(second["messages"][1]["content"], str)
        self.assertIn("изображение было показано", second["messages"][1]["content"])
        self.assert_history_consistent(session)

    def test_broken_history_is_repaired_before_sending(self):
        session = self.sessions.create_session()
        session.history = [
            {"role": "user", "content": "старое"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x1", "type": "function", "function": {"name": "list_objects", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "zzz", "content": "сирота"},
        ]
        session, events = self.run_turn("?", [reply("Ок.")], session=session)
        self.assertEqual(self.done_reason(events), "completed")
        self.assertEqual(self.llm.violations, [])

    def test_spa_events_over_http(self):
        base = self.service.start(port=0)
        self.llm.push(reply(tool_calls=[call("list_objects")]), reply("Готово."))

        def post(path, payload):
            request = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(payload).encode("utf-8"),
                                             headers={"Content-Type": "application/json", "X-AI-Assistant": "1"},
                                             method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        session_id = post("/api/sessions", {})["session"]["id"]
        post("/api/message", {"session": session_id, "text": "Покажи объекты"})
        types = []
        since = 0
        deadline = time.time() + 20
        while "done" not in types and time.time() < deadline:
            try:
                func, args, kwargs = self.app.queue.get(timeout=0.02)
                func(*args, **kwargs)
            except queue.Empty:
                pass
            url = base.rstrip("/") + "/api/events?session=%s&since=%d&timeout=0" % (session_id, since)
            with urllib.request.urlopen(url, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            types.extend(e["type"] for e in payload["events"])
            since = payload["last_seq"]
        for kind in ("user_message", "turn_started", "tool_call_started", "tool_call_result", "assistant_delta",
                     "done"):
            self.assertIn(kind, types)


class LoopGuardTest(unittest.TestCase):
    def test_polling_tool_gets_more_room(self):
        guard = LoopGuard()
        for _ in range(6):
            self.assertIsNone(guard.check("get_slicing_status", "k"))
            guard.record("get_slicing_status", "k", True, "processing")
        self.assertIsNotNone(guard.check("get_slicing_status", "k"))

    def test_alternating_calls_are_capped(self):
        guard = LoopGuard()
        refused = 0
        for index in range(12):
            key = "a" if index % 2 else "b"
            if guard.check("list_objects", key):
                refused += 1
            else:
                guard.record("list_objects", key, True, "ok")
            guard.end_step()
        self.assertGreater(refused, 0)
        self.assertTrue(guard.looping)


if __name__ == "__main__":
    unittest.main()
