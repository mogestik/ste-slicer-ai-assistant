"""Очередь событий диалога для панели чата (long polling) и сжатая история переписки.

Типы событий:
    user_message       {message_id, text, attachments}
    turn_started       {turn_id}
    reasoning_delta    {message_id, text}
    assistant_delta    {message_id, text}
    tool_call_started  {call_id, name, arguments}
    tool_call_result   {call_id, name, ok, result, duration_ms}
    slicing_progress   {progress (0..1), state, message}
    slicing_result     {print_time_s, print_time_text, material: [...], ...}
    usage              {prompt_tokens, completion_tokens, total_tokens}
    error              {message, kind}
    done               {turn_id, reason}
    session_reset      {}

У каждого события растущий номер `seq`; клиент запрашивает `since=<последний seq>`.
История (transcript) — свёртка тех же событий: по ней панель восстанавливает диалог
после переоткрытия или перезапуска.
"""

import copy
import threading
import time
from collections import deque

MAX_EVENTS = 20000
MAX_TOOL_RESULT_IN_TRANSCRIPT = 20000


class Transcript(object):
    """Сжатая история диалога, собранная из событий (то, что рисует панель)."""

    def __init__(self):
        self.items = []
        self._by_message = {}
        self._by_call = {}

    def clear(self):
        self.items = []
        self._by_message = {}
        self._by_call = {}

    def apply(self, event):
        kind = event["type"]
        if kind == "user_message":
            self.items.append({"kind": "user", "id": event["message_id"], "text": event.get("text", ""),
                               "attachments": event.get("attachments", [])})
        elif kind in ("assistant_delta", "reasoning_delta"):
            item = self._assistant_item(event["message_id"])
            field = "content" if kind == "assistant_delta" else "reasoning"
            item[field] += event.get("text", "")
        elif kind == "tool_call_started":
            item = {"kind": "tool", "id": event["call_id"], "name": event.get("name"),
                    "arguments": event.get("arguments"), "status": "running", "result": None}
            self._by_call[event["call_id"]] = item
            self.items.append(item)
        elif kind == "tool_call_result":
            item = self._by_call.get(event["call_id"])
            if item is not None:
                item["status"] = "ok" if event.get("ok") else "error"
                item["result"] = (event.get("result") or "")[:MAX_TOOL_RESULT_IN_TRANSCRIPT]
                item["duration_ms"] = event.get("duration_ms")
        elif kind == "slicing_result":
            self.items.append({"kind": "slicing_result", "data": dict(event)})
        elif kind == "error":
            self.items.append({"kind": "error", "message": event.get("message"), "error_kind": event.get("kind")})
        elif kind == "done" and event.get("reason") == "cancelled":
            self.items.append({"kind": "notice", "text": "Остановлено пользователем."})
        elif kind == "session_reset":
            self.clear()

    def _assistant_item(self, message_id):
        item = self._by_message.get(message_id)
        if item is None:
            item = {"kind": "assistant", "id": message_id, "content": "", "reasoning": ""}
            self._by_message[message_id] = item
            self.items.append(item)
        return item


class EventQueue(object):
    """Потокобезопасный журнал событий: только дописываем, читаем с ожиданием."""

    def __init__(self, max_events=MAX_EVENTS):
        self._events = deque(maxlen=max_events)
        self._seq = 0
        self._cond = threading.Condition()
        self.transcript = Transcript()

    @property
    def last_seq(self):
        with self._cond:
            return self._seq

    def publish(self, event_type, **data):
        with self._cond:
            self._seq += 1
            event = dict(data)
            event["type"] = event_type
            event["seq"] = self._seq
            event["ts"] = time.time()
            self._events.append(event)
            self.transcript.apply(event)
            self._cond.notify_all()
            return event

    def since(self, seq, timeout=0.0):
        """События с номером больше `seq`; новых ждём не дольше `timeout` секунд."""
        deadline = time.time() + max(0.0, timeout)
        with self._cond:
            while self._seq <= seq:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return [event for event in self._events if event["seq"] > seq]

    def snapshot(self):
        """(история, последний seq) — снимок под одной блокировкой."""
        with self._cond:
            return copy.deepcopy(self.transcript.items), self._seq
