"""Цикл агента: диалоги, запросы к LLM и выполнение инструментов.

На каждый диалог — один ChatSession с историей в формате OpenAI и очередью событий для
панели. Сообщение пользователя обрабатывается как «ход» в отдельном потоке: зовём LLM с
каталогом инструментов, выполняем запрошенные вызовы (по очереди и в порядке модели, чтобы
цепочка «загрузить -> повернуть -> нарезать» не теряла смысл), возвращаем результаты и
повторяем, пока модель не ответит текстом без вызовов, не кончится лимит шагов, не
обнаружится зацикливание или пользователь не нажмёт «Стоп».

Защита от слабостей маленьких моделей (вроде Qwen flash): битые, обёрнутые в ``` или пустые
аргументы чиним или отвечаем понятной ошибкой; у каждого tool_call уникальный id и ровно
один ответ `tool`; одинаковые повторные вызовы отклоняет LoopGuard; при лимите шагов или
цикле просим итоговый текст без инструментов. Фото отправляем один раз, дальше в истории
вместо него пометка; результаты прошлых ходов сокращаем.
"""

import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict

from . import __version__, log
from .attachments import build_user_content, public_record
from .events import EventQueue
from .llm_client import LLMCancelled, LLMClient, LLMError
from .tools import (ENABLE_TOOLS_NAME, EXTRA_TOOLS, core_tool_names, enable_tools_schema, extra_tool_names,
                    tools_for_text)
from .tools.registry import ToolContext, ToolValidationError, parse_arguments

PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
DEFAULT_PROMPT_FILE = os.path.join(PROMPT_DIR, "system_ru.md")
MAX_SESSIONS = 50
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)  # заметки для разработчиков, модели их не шлём

MAX_EMPTY_RETRIES = 1          # ответ без текста и без вызовов переспрашиваем один раз
MAX_PARTIAL_RETRIES = 1        # поток, оборванный посреди ответа, перезапрашиваем один раз
OLD_TOOL_RESULT_CHARS = 1500   # результаты инструментов прошлых ходов в запросах сокращаем
OLD_ASSISTANT_CHARS = 6000

EMPTY_ANSWER_NOTE = ("Твой предыдущий ответ был пустым. Продолжай: вызови нужный инструмент или ответь "
                     "пользователю текстом по-русски.")
LIMIT_NOTE = ("Лимит шагов на этот запрос исчерпан, инструменты вызывать нельзя. Кратко ответь пользователю: "
              "что сделано, что не успел и что делать дальше (можно написать «продолжай»).")
LOOP_NOTE = ("Ты повторяешь одни и те же вызовы. Инструменты больше не вызывай: объясни пользователю, что "
             "получилось, что не вышло и что сделать вручную.")
IMAGE_SEEN_NOTE = "[изображение было показано модели на первом шаге и в истории не хранится]"


class AgentBusyError(Exception):
    """В этом диалоге уже идёт ход."""


class ChatSession(object):
    def __init__(self, session_id=None):
        self.id = session_id or uuid.uuid4().hex[:16]
        self.title = "Новый диалог"
        self.created = time.time()
        self.updated = self.created
        self.history = []                 # сообщения OpenAI без системного промпта
        self.events = EventQueue()
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.cancel_event = threading.Event()
        self.current_turn = None
        self.turn_start = 0               # индекс в истории сообщения пользователя текущего хода
        self.enabled_tools = set()        # дополнительные инструменты, подключённые в этом диалоге
        self.seen_call_ids = set()
        self._lock = threading.Lock()

    @property
    def running(self):
        return self.current_turn is not None

    def emit(self, event_type, **data):
        self.updated = time.time()
        return self.events.publish(event_type, **data)

    def summary(self):
        return {"id": self.id, "title": self.title, "created": self.created, "updated": self.updated,
                "running": self.running, "messages": len(self.history)}


class SessionManager(object):
    def __init__(self, settings, registry, uploads, prompt_file=DEFAULT_PROMPT_FILE, services=None,
                 client_factory=None):
        self.settings = settings
        self.registry = registry
        self.uploads = uploads
        self.prompt_file = prompt_file
        self.services = services or {}
        self.services.setdefault("uploads", uploads)
        self.services.setdefault("settings", settings)
        self._client_factory = client_factory or (lambda: LLMClient.from_settings(settings))
        self._sessions = OrderedDict()
        self._lock = threading.Lock()

    def create_session(self):
        session = ChatSession()
        with self._lock:
            self._sessions[session.id] = session
            while len(self._sessions) > MAX_SESSIONS:
                oldest_id = next(iter(self._sessions))
                if self._sessions[oldest_id].running:
                    break
                del self._sessions[oldest_id]
        return session

    def get(self, session_id, create=False):
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None and create:
            session = ChatSession(session_id)
            with self._lock:
                self._sessions[session.id] = session
        return session

    def list_sessions(self):
        with self._lock:
            sessions = list(self._sessions.values())
        return [s.summary() for s in sorted(sessions, key=lambda s: s.updated, reverse=True)]

    def reset(self, session_id):
        session = self.get(session_id)
        if session is None:
            return
        if session.running:
            raise AgentBusyError("Сначала остановите текущий ответ")
        session.history = []
        session.turn_start = 0
        session.enabled_tools = set()
        session.seen_call_ids = set()
        session.title = "Новый диалог"
        session.emit("session_reset")

    # Ходы агента

    def send_message(self, session_id, text, attachment_ids=None):
        session = self.get(session_id, create=True)
        records = []
        for upload_id in attachment_ids or []:
            record = self.uploads.get(upload_id)
            if record is None:
                raise ValueError("Вложение не найдено: %s" % upload_id)
            records.append(record)
        if not (text or "").strip() and not records:
            raise ValueError("Пустое сообщение")

        with session._lock:
            if session.running:
                raise AgentBusyError("Ассистент ещё отвечает. Дождитесь ответа или нажмите «Стоп».")
            turn_id = uuid.uuid4().hex[:12]
            session.current_turn = turn_id
            session.cancel_event = threading.Event()

        if not session.history:
            session.title = _make_title(text, records)
        session.emit("user_message", message_id="u-" + turn_id, text=text or "",
                     attachments=[public_record(r) for r in records])
        worker = threading.Thread(target=self._run_turn, name="AIAssistant-turn-" + turn_id,
                                  args=(session, turn_id, text, records))
        worker.daemon = True
        worker.start()
        return turn_id

    def stop(self, session_id):
        session = self.get(session_id)
        if session is not None and session.running:
            session.cancel_event.set()
            return True
        return False

    def _run_turn(self, session, turn_id, text, records):
        reason = "completed"
        session.emit("turn_started", turn_id=turn_id)
        try:
            content = build_user_content(text, records, self.settings.get("image_max_side"))
            session.turn_start = len(session.history)
            session.history.append({"role": "user", "content": content})
            session.enabled_tools.update(tools_for_text(text))
            reason = self._agent_loop(session, turn_id)
        except LLMCancelled:
            reason = "cancelled"
        except LLMError as exc:
            log.warning("LLM error in session %s: %s", session.id, exc.message)
            session.emit("error", message=exc.message, kind=exc.kind)
            reason = "error"
        except Exception as exc:
            log.exception("Agent turn failed")
            session.emit("error", message="Внутренняя ошибка ассистента: %s: %s" % (type(exc).__name__, exc),
                         kind="internal")
            reason = "error"
        finally:
            with session._lock:
                session.current_turn = None
            session.emit("done", turn_id=turn_id, reason=reason)

    def _agent_loop(self, session, turn_id):
        client = self._client_factory()
        max_iterations = self.settings.get("max_iterations")
        guard = LoopGuard()
        empty_retries = 0
        nudge = None

        for iteration in range(max_iterations):
            if session.cancel_event.is_set():
                return "cancelled"
            reply = self._request(session, client, "%s-%d" % (turn_id, iteration), extra_note=nudge)
            nudge = None
            # Фото модель уже видела — на каждом шаге их заново не отправляем.
            _strip_images(session.history)

            prepared = self._prepare_calls(session, reply["tool_calls"])
            content = reply.get("content") or ""
            if not prepared:
                if not content.strip():
                    if empty_retries < MAX_EMPTY_RETRIES:
                        empty_retries += 1
                        nudge = EMPTY_ANSWER_NOTE
                        continue
                    session.emit("error", kind="empty_answer",
                                 message="Модель не дала ответа (только рассуждения). Повторите запрос или "
                                         "переформулируйте его.")
                    return "empty"
                session.history.append({"role": "assistant", "content": content})
                if reply.get("finish_reason") == "length":
                    session.emit("error", kind="length",
                                 message="Ответ обрезан: модель упёрлась в лимит токенов (max_tokens).")
                return "completed"

            session.history.append({"role": "assistant", "content": content,
                                    "tool_calls": [call for call, _raw in prepared]})
            self._execute_tool_calls(session, prepared, guard)
            if session.cancel_event.is_set():
                return "cancelled"
            if guard.looping:
                session.emit("error", kind="loop",
                             message="Ассистент начал повторять одни и те же действия — цикл остановлен.")
                self._final_answer(session, client, turn_id, LOOP_NOTE)
                return "loop"

        session.emit("error", kind="max_iterations",
                     message="Достигнут лимит шагов агента (%d). Напишите «продолжай», чтобы продолжить."
                     % max_iterations)
        self._final_answer(session, client, turn_id, LIMIT_NOTE)
        return "max_iterations"

    # Запрос к LLM

    def _request(self, session, client, step_id, extra_note=None, final=False):
        """Один вызов Chat Completions; поток, оборванный посреди ответа, повторяем один раз."""
        settings = self.settings
        tools = self.tools_for_session(session)
        messages = self.build_messages(session)
        if extra_note:
            messages.append({"role": "system", "content": extra_note})
        for attempt in range(1 + MAX_PARTIAL_RETRIES):
            message_id = "a-%s" % step_id + ("-r%d" % attempt if attempt else "")

            def on_event(kind, fragment, message_id=message_id):
                event_type = "assistant_delta" if kind == "content" else "reasoning_delta"
                session.emit(event_type, message_id=message_id, text=fragment)

            try:
                reply = client.chat(messages, tools=tools, temperature=settings.get("temperature"),
                                    max_tokens=settings.get("max_tokens") or None, stream=settings.get("stream"),
                                    on_event=on_event, cancel_event=session.cancel_event,
                                    tool_choice="none" if final else None,
                                    parallel_tool_calls=settings.get("parallel_tool_calls"))
            except LLMCancelled:
                raise
            except LLMError as exc:
                if exc.partial and exc.retryable and attempt < MAX_PARTIAL_RETRIES:
                    session.emit("error", kind="retry", message="Ответ модели оборвался (%s). Запрашиваю заново…"
                                 % exc.message)
                    continue
                raise
            self._account_usage(session, reply.get("usage"))
            return reply

    def _final_answer(self, session, client, turn_id, note):
        """Просит итог текстом без инструментов (лимит шагов или цикл)."""
        try:
            reply = self._request(session, client, "%s-final" % turn_id, extra_note=note, final=True)
        except LLMCancelled:
            raise
        except LLMError as exc:
            log.warning("Final summary request failed: %s", exc.message)
            return
        content = (reply.get("content") or "").strip()
        if content:   # вызовы инструментов тут игнорируем: ход закончен
            session.history.append({"role": "assistant", "content": content})

    # Инструменты

    def tools_for_session(self, session):
        """Инструменты в формате OpenAI для следующего запроса (см. tools/__init__.py)."""
        registry = self.registry
        if self.settings.get("llm_tools") == "all":
            return registry.to_openai_tools(compact=True)
        extras = [name for name in extra_tool_names(registry) if name in session.enabled_tools]
        tools = registry.to_openai_tools(core_tool_names(registry) + extras, compact=True)
        if len(extras) < len(extra_tool_names(registry)):
            tools.append(enable_tools_schema(registry, extras))
        return tools

    def _prepare_calls(self, session, tool_calls):
        """[(вызов для истории, исходные аргументы)]: уникальные id и валидный JSON аргументов
        для истории (API отклоняет повтор истории, где у вызова битые аргументы)."""
        prepared = []
        for call in tool_calls or []:
            call_id = call.get("id") or ""
            if not call_id or call_id in session.seen_call_ids:
                call_id = "call_" + uuid.uuid4().hex[:24]
            session.seen_call_ids.add(call_id)
            function = call.get("function") or {}
            raw = function.get("arguments")
            try:
                parsed = parse_arguments(raw)
                arguments = json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else "{}"
            except ToolValidationError:
                arguments = "{}"
            prepared.append(({"id": call_id, "type": "function",
                              "function": {"name": function.get("name") or "", "arguments": arguments}}, raw))
        return prepared

    def _execute_tool_calls(self, session, prepared, guard):
        skip_reason = None
        for position, (call, raw) in enumerate(prepared):
            name = call["function"]["name"]
            shown = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
            session.emit("tool_call_started", call_id=call["id"], name=name, arguments=shown)
            started = time.time()
            if session.cancel_event.is_set():
                ok, text = False, "Отменено пользователем"
            elif skip_reason:
                ok, text = False, skip_reason
            elif name == ENABLE_TOOLS_NAME:
                ok, text = self._enable_tools(session, raw)
            else:
                resolved = self.registry.resolve_name(name) or name
                key = _call_key(resolved, raw)
                refusal = guard.check(resolved, key)
                if refusal:
                    ok, text = False, refusal
                else:
                    ctx = ToolContext(session_id=session.id, emit=session.emit,
                                      cancel_event=session.cancel_event, services=self.services)
                    result = self.registry.call(name, raw, ctx)
                    ok, text = result.ok, result.text
                    guard.record(resolved, key, ok, text)
                    if resolved in EXTRA_TOOLS:
                        session.enabled_tools.add(resolved)
            duration_ms = int((time.time() - started) * 1000)
            session.emit("tool_call_result", call_id=call["id"], name=name, ok=ok, result=text,
                         duration_ms=duration_ms)
            # На каждый tool_call_id нужен ответ, даже если вызов отменён или пропущен.
            session.history.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            if not ok and not skip_reason and position < len(prepared) - 1:
                skip_reason = ("Не выполнено: предыдущий вызов %s в этом же ответе завершился ошибкой. "
                               "Исправь его, затем вызывай зависимые инструменты по одному." % (name or "?"))
        guard.end_step()

    def _enable_tools(self, session, raw):
        try:
            args = parse_arguments(raw)
        except ToolValidationError as exc:
            return False, "Ошибка: %s" % exc
        names = args.get("names") if isinstance(args, dict) else args
        if isinstance(names, str):
            names = [part.strip() for part in names.split(",")]
        if not isinstance(names, list) or not names:
            return False, "Ошибка: передай names — список имён инструментов из описания enable_tools"
        extras = extra_tool_names(self.registry)
        enabled, already, unknown = [], [], []
        for item in names:
            resolved = self.registry.resolve_name(str(item))
            if resolved in extras:
                session.enabled_tools.add(resolved)
                enabled.append(resolved)
            elif resolved is not None:
                already.append(resolved)
            else:
                unknown.append(str(item))
        parts = []
        if enabled:
            parts.append("Подключены: %s — вызывай их на следующем шаге." % ", ".join(enabled))
        if already:
            parts.append("Уже доступны: %s." % ", ".join(already))
        if unknown:
            parts.append("Нет таких инструментов: %s. Доступные для подключения: %s."
                         % (", ".join(unknown), ", ".join(extras)))
        return bool(enabled or already), " ".join(parts)

    def _account_usage(self, session, usage):
        if not usage or not isinstance(usage, dict):
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                session.usage[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        session.emit("usage", **session.usage)

    # Сообщения и промпт

    def build_messages(self, session):
        """Системный промпт + история, исправленная под требования API, со сжатыми старыми ходами."""
        turn_start = session.turn_start or 0
        history = [(_shorten(entry) if index < turn_start else entry)
                   for index, entry in enumerate(session.history)]
        return [{"role": "system", "content": self.system_prompt()}] + _repair_history(history)

    def system_prompt(self):
        """Системный промпт из файла-шаблона. Читаем на каждом ходу, чтобы править его без перезапуска."""
        try:
            with open(self.prompt_file, "r", encoding="utf-8") as handle:
                template = handle.read()
        except OSError:
            log.exception("Cannot read system prompt %s", self.prompt_file)
            template = "Ты — ассистент слайсера STE Slicer. Отвечай по-русски."
        template = _COMMENT_RE.sub("", template).strip()
        values = {
            "date": time.strftime("%Y-%m-%d"),
            "plugin_version": __version__,
            "host": self.services.get("host_name", "STE Slicer"),
            "tools": ", ".join(tool.name for tool in self.registry.tools()),
            "extra_tools": ", ".join(extra_tool_names(self.registry)),
        }
        for key, value in values.items():
            template = template.replace("{{%s}}" % key, str(value))
        return template


def _make_title(text, records):
    title = " ".join((text or "").split())
    if not title and records:
        title = records[0]["name"]
    return (title[:57] + "…") if len(title) > 60 else (title or "Новый диалог")


class LoopGuard(object):
    """Отклоняет одинаковые повторные вызовы в пределах хода и замечает зацикливание.

    * вызов, совпадающий с только что упавшим (без успешных вызовов после него), отклоняем;
    * 3-й одинаковый вызов подряд (для get_slicing_status — 7-й) или 5-й одинаковый за ход
      отклоняем и возвращаем прошлый результат;
    * 3 отказа за ход или 2 шага подряд только из отклонённых вызовов — это цикл (агент
      тогда просит итоговый ответ).
    """

    MAX_CONSECUTIVE = 2
    MAX_TOTAL = 4
    MAX_REFUSALS = 3
    POLLING = {"get_slicing_status": 6}

    def __init__(self):
        self.total = {}
        self.failed = {}
        self.last_text = {}
        self.last_key = None
        self.consecutive = 0
        self.refusals = 0
        self.refused_steps = 0
        self.looping = False
        self._step_calls = 0
        self._step_refusals = 0

    def check(self, name, key):
        """Текст отказа для этого вызова или None, если его можно выполнять."""
        refusal = None
        consecutive = self.consecutive + 1 if key == self.last_key else 1
        if key in self.failed:
            refusal = ("Ошибка: этот вызов с теми же аргументами только что завершился ошибкой (%s). Не повторяй "
                       "его: исправь аргументы, выбери другой инструмент или объясни пользователю, что не вышло."
                       % self.failed[key])
        elif consecutive > self.POLLING.get(name, self.MAX_CONSECUTIVE) or \
                (name not in self.POLLING and self.total.get(key, 0) >= self.MAX_TOTAL):
            refusal = ("Ошибка: %s уже вызван %d раз с теми же аргументами, результат не изменится: %s. Не "
                       "повторяй вызов — переходи к следующему шагу или ответь пользователю."
                       % (name, self.total.get(key, 0), self.last_text.get(key, "")))
        self._step_calls += 1
        if refusal:
            self.refusals += 1
            self._step_refusals += 1
            if self.refusals >= self.MAX_REFUSALS:
                self.looping = True
        return refusal

    def record(self, name, key, ok, text):
        self.consecutive = self.consecutive + 1 if key == self.last_key else 1
        self.last_key = key
        self.total[key] = self.total.get(key, 0) + 1
        self.last_text[key] = _clip(text, 300)
        if ok:
            self.failed = {}
        else:
            self.failed[key] = _clip(text, 300)

    def end_step(self):
        if self._step_calls and self._step_refusals == self._step_calls:
            self.refused_steps += 1
            if self.refused_steps >= 2:
                self.looping = True
        else:
            self.refused_steps = 0
        self._step_calls = self._step_refusals = 0


def _call_key(name, raw):
    try:
        value = parse_arguments(raw)
    except ToolValidationError:
        return "%s:raw:%s" % (name, raw)
    return "%s:%s" % (name, json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


# Работа с историей

def _strip_images(history):
    """Заменяет фото в сообщениях пользователя текстовой пометкой (на месте)."""
    for index, entry in enumerate(history):
        if entry.get("role") != "user" or not isinstance(entry.get("content"), list):
            continue
        texts = [part.get("text", "") for part in entry["content"] if part.get("type") == "text"]
        history[index] = dict(entry, content=("\n".join(t for t in texts if t) + "\n" + IMAGE_SEEN_NOTE).strip())


def _shorten(entry):
    """Копия записи прошлого хода с обрезанным длинным текстом."""
    role = entry.get("role")
    content = entry.get("content")
    if role == "tool" and isinstance(content, str) and len(content) > OLD_TOOL_RESULT_CHARS:
        return dict(entry, content=content[:OLD_TOOL_RESULT_CHARS] + "… [сокращено: результат прошлого запроса]")
    if role == "assistant" and isinstance(content, str) and len(content) > OLD_ASSISTANT_CHARS:
        return dict(entry, content=content[:OLD_ASSISTANT_CHARS] + "… [сокращено]")
    return entry


def _repair_history(history):
    """История, которую примет API: за каждым tool_call ассистента — ровно одно сообщение
    tool с его id; ответы tool без вызова выбрасываем."""
    result = []
    pending = []

    def close_pending():
        for call_id in pending:
            result.append({"role": "tool", "tool_call_id": call_id, "content": "Нет результата (выполнение прервано)."})
        del pending[:]

    for entry in history:
        role = entry.get("role")
        if role == "tool":
            if entry.get("tool_call_id") in pending:
                pending.remove(entry["tool_call_id"])
                result.append(entry)
            continue
        close_pending()
        result.append(entry)
        if role == "assistant" and entry.get("tool_calls"):
            pending.extend(call["id"] for call in entry["tool_calls"])
    close_pending()
    return result
