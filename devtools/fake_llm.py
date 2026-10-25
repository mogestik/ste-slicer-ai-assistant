"""Поддельный OpenAI-совместимый сервер LLM, чтобы проверять цикл агента без сети.

    python devtools/fake_llm.py --scenario demo --port 8799
    # потом в настройках плагина: base_url http://127.0.0.1:8799/v1, ключ и модель любые

Отвечает на POST /v1/chat/completions (обычный JSON и поток SSE) и GET /v1/models.
Ответы идут по сценарию: список шагов, по одному на запрос, которые повторяют то, что
реально делает маленькая модель (Qwen flash):

* несколько tool_calls в одном ответе; вызовы без id, без index или все с index 0
  (так склеивают параллельные вызовы некоторые шлюзы);
* `arguments` битым JSON, JSON в блоке ```, пустой строкой, числом в строке; неизвестные
  имена инструментов; один и тот же вызов снова и снова;
* `reasoning_content` без `content`; очень длинные ответы;
* сбои транспорта: поток SSE обрывается посередине, HTTP 429/500/400, ответ медленнее
  таймаута клиента.

Ещё сервер проверяет каждый запрос строго, как настоящий OpenAI (у каждого tool_call
ассистента валидный JSON аргументов и ровно один ответ `tool` с его id, id уникальны, в
сообщениях нет лишних полей), на нарушения отвечает HTTP 400 и складывает их в
`server.violations`.

Python 3.5+, только stdlib.
"""

import argparse
import json
import socketserver
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

ALLOWED_MESSAGE_KEYS = {
    "system": {"role", "content", "name"},
    "user": {"role", "content", "name"},
    "assistant": {"role", "content", "tool_calls", "name", "reasoning_content"},
    "tool": {"role", "content", "tool_call_id", "name"},
}


# Шаги сценария

def call(name, arguments=None, call_id="auto"):
    """Вызов инструмента в ответе по сценарию. `arguments` — словарь (уйдёт как JSON) или сырая строка."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    if call_id == "auto":
        call_id = "call_" + uuid.uuid4().hex[:8]
    return {"id": call_id, "name": name, "arguments": arguments}


def reply(content="", tool_calls=None, reasoning="", finish_reason=None, **stream_options):
    """Обычный ответ. stream_options: chunk_size, omit_index, same_index, cut_after_chars,
    no_finish (поток кончается без finish_reason и [DONE])."""
    step = {"kind": "reply", "content": content, "tool_calls": tool_calls or [], "reasoning": reasoning,
            "finish_reason": finish_reason}
    step.update(stream_options)
    return step


def http_error(status, message="error", retry_after=None):
    return {"kind": "http_error", "status": status, "message": message, "retry_after": retry_after}


def slow(seconds, then=None):
    """Пауза перед ответом (для проверки таймаута клиента и отмены)."""
    return {"kind": "slow", "seconds": seconds, "then": then or reply("Готово.")}


def cut(content="", reasoning="", tool_calls=None, cut_after_chars=None):
    """Потоковый ответ, у которого соединение рвётся посередине (без finish_reason и [DONE])."""
    return reply(content, tool_calls=tool_calls, reasoning=reasoning, no_finish=True, cut_after_chars=cut_after_chars)


# Готовые сценарии для ручных прогонов со стендом

def scenarios():
    return {
        "demo": [
            reply(reasoning="Сначала посмотрю конфигурацию.", tool_calls=[
                call("get_current_config"), call("list_materials", '```json\n{"query": "PETG"}\n```')]),
            reply(tool_calls=[call("set_print_mode", '{"mode": "classic"')]),          # битый JSON
            reply(tool_calls=[call("set_print_mode", {"mode": "classic"}, call_id=None)]),
            reply(tool_calls=[call("make_coffee", "")]),                                 # неизвестный инструмент
            reply("**Готово.** Режим 3D выбран, материал можно подобрать на следующем шаге."),
        ],
        "loop": [reply(tool_calls=[call("list_objects")]) for _ in range(30)],
        "reasoning_only": [reply(reasoning="Думаю…"), reply(reasoning="Всё ещё думаю…")],
        "errors": [http_error(429, "Rate limit", retry_after=1), http_error(500, "Upstream error"),
                   reply("Ответ после ретраев.")],
        "cut": [cut("Начало ответа, которое обор", cut_after_chars=10), reply("Полный ответ после обрыва.")],
        "long": [reply("Очень длинный ответ. " * 5000)],
    }


# Сервер
class FakeLLMServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, steps=None, port=0, default=None, reject_params=(), require_key=None, strict=True):
        HTTPServer.__init__(self, ("127.0.0.1", port), _Handler)
        self.steps = list(steps or [])
        self.default = default if default is not None else reply("Готово.")
        self.reject_params = set(reject_params)   # 400, если в запросе есть одно из этих полей
        self.require_key = require_key
        self.strict = strict
        self.requests = []      # разобранные тела запросов по порядку
        self.violations = []    # нарушения строгой проверки (должно остаться пусто)
        self.lock = threading.Lock()
        self._thread = None

    # control ---------------------------------------------------------------
    @property
    def url(self):
        return "http://127.0.0.1:%d/v1" % self.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, name="fake-llm")
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()

    def push(self, *steps):
        with self.lock:
            self.steps.extend(steps)

    def next_step(self, body):
        with self.lock:
            self.requests.append(body)
            step = self.steps.pop(0) if self.steps else self.default
        if callable(step):
            step = step(body)
        return step

    @property
    def chat_requests(self):
        return [r for r in self.requests if isinstance(r, dict) and "messages" in r]

    # Строгая проверка запросов
    def validate(self, body):
        problems = []
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return ["messages must be a non-empty list"]
        pending = []
        seen_ids = set()
        for index, message in enumerate(messages):
            role = message.get("role")
            if role not in ALLOWED_MESSAGE_KEYS:
                problems.append("messages[%d]: bad role %r" % (index, role))
                continue
            extra = set(message) - ALLOWED_MESSAGE_KEYS[role]
            if extra:
                problems.append("messages[%d]: unknown fields %s" % (index, sorted(extra)))
            if role == "tool":
                if message.get("tool_call_id") not in pending:
                    problems.append("messages[%d]: tool message for unknown/answered id %r"
                                    % (index, message.get("tool_call_id")))
                else:
                    pending.remove(message["tool_call_id"])
                if not isinstance(message.get("content"), str):
                    problems.append("messages[%d]: tool content must be a string" % index)
                continue
            if pending:
                problems.append("messages[%d]: tool_calls %s were not answered" % (index, pending))
                pending = []
            if role == "assistant":
                for tool_call in message.get("tool_calls") or []:
                    call_id = tool_call.get("id")
                    if not call_id or call_id in seen_ids:
                        problems.append("messages[%d]: missing or duplicate tool_call id %r" % (index, call_id))
                    seen_ids.add(call_id)
                    pending.append(call_id)
                    arguments = (tool_call.get("function") or {}).get("arguments")
                    try:
                        if not isinstance(json.loads(arguments), dict):
                            raise ValueError("not an object")
                    except (TypeError, ValueError) as exc:
                        problems.append("messages[%d]: invalid tool arguments %r (%s)" % (index, arguments, exc))
                if not message.get("tool_calls") and not (message.get("content") or "").strip():
                    problems.append("messages[%d]: empty assistant message" % index)
            if role == "user" and isinstance(message.get("content"), list):
                for part in message["content"]:
                    if part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if not url.startswith("data:image/") or ";base64," not in url:
                            problems.append("messages[%d]: bad image_url" % index)
        if pending:
            problems.append("last tool_calls %s were not answered" % pending)
        if body.get("tools"):
            names = [t.get("function", {}).get("name") for t in body["tools"]]
            if len(names) != len(set(names)):
                problems.append("duplicate tool names")
        return problems


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeLLM/1.0"

    def log_message(self, fmt, *args):  # чтобы не мусорить в выводе тестов
        pass

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "fake-qwen", "object": "model"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        server = self.server
        if server.require_key and self.headers.get("Authorization") != "Bearer " + server.require_key:
            self._json(401, {"error": {"message": "Invalid API key"}})
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._json(400, {"error": {"message": "invalid JSON body"}})
            return
        rejected = [param for param in server.reject_params if param in body]
        if rejected:
            with server.lock:
                server.requests.append(body)
            self._json(400, {"error": {"message": "Unsupported parameter: '%s'" % rejected[0]}})
            return
        if server.strict:
            problems = server.validate(body)
            if problems:
                with server.lock:
                    server.requests.append(body)
                    server.violations.extend(problems)
                self._json(400, {"error": {"message": "; ".join(problems)}})
                return
        step = server.next_step(body)
        self._answer(step, body)

    # Ответы
    def _answer(self, step, body):
        kind = step.get("kind")
        if kind == "slow":
            time.sleep(step["seconds"])
            try:
                self._answer(step["then"], body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if kind == "http_error":
            headers = {"Retry-After": str(step["retry_after"])} if step.get("retry_after") is not None else None
            self._json(step["status"], {"error": {"message": step["message"], "code": step["status"]}}, headers)
            return
        if body.get("stream"):
            self._stream(step, body)
        else:
            self._json(200, _completion(step, body))

    def _json(self, status, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, step, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        model = body.get("model")
        chunk_size = step.get("chunk_size") or 7
        budget = [step.get("cut_after_chars")]

        def send(delta, finish=None):
            chunk = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)).encode("utf-8"))
            self.wfile.flush()

        def send_text(field, text):
            for start in range(0, len(text), chunk_size):
                piece = text[start:start + chunk_size]
                if budget[0] is not None:
                    if budget[0] <= 0:
                        return False
                    piece = piece[:budget[0]]
                    budget[0] -= len(piece)
                send({field: piece})
            return True

        try:
            send({"role": "assistant", "content": ""})
            if step.get("reasoning") and not send_text("reasoning_content", step["reasoning"]):
                return
            if step.get("content") and not send_text("content", step["content"]):
                return
            for position, tool_call in enumerate(step.get("tool_calls") or []):
                index = 0 if step.get("same_index") else position
                head = {"type": "function", "function": {"name": tool_call["name"], "arguments": ""}}
                if not step.get("omit_index"):
                    head["index"] = index
                if tool_call.get("id"):
                    head["id"] = tool_call["id"]
                send({"tool_calls": [head]})
                arguments = tool_call["arguments"]
                for start in range(0, len(arguments), chunk_size):
                    fragment = {"function": {"arguments": arguments[start:start + chunk_size]}}
                    if not step.get("omit_index"):
                        fragment["index"] = index
                    send({"tool_calls": [fragment]})
            if step.get("no_finish"):
                return       # соединение закрывается без finish_reason и [DONE]
            finish = step.get("finish_reason") or ("tool_calls" if step.get("tool_calls") else "stop")
            send({}, finish)
            usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
            self.wfile.write(("data: %s\n\n" % json.dumps({"choices": [], "usage": usage})).encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _completion(step, body):
    message = {"role": "assistant", "content": step.get("content") or ""}
    if step.get("reasoning"):
        message["reasoning_content"] = step["reasoning"]
    if step.get("tool_calls"):
        message["tool_calls"] = []
        for tool_call in step["tool_calls"]:
            entry = {"type": "function", "function": {"name": tool_call["name"], "arguments": tool_call["arguments"]}}
            if tool_call.get("id"):
                entry["id"] = tool_call["id"]
            message["tool_calls"].append(entry)
    finish = step.get("finish_reason") or ("tool_calls" if step.get("tool_calls") else "stop")
    return {"id": "chatcmpl-fake", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--scenario", default="demo", choices=sorted(scenarios()))
    parser.add_argument("--repeat", action="store_true", help="начинать сценарий заново, когда он кончится")
    args = parser.parse_args()
    steps = scenarios()[args.scenario]
    server = FakeLLMServer(steps, port=args.port)
    if args.repeat:
        server.default = lambda body: (server.push(*scenarios()[args.scenario]) or reply("(сценарий начат заново)"))
    print("Fake LLM: %s (scenario %s, %d steps)" % (server.url, args.scenario, len(steps)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
