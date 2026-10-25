"""Клиент Chat Completions (OpenAI-совместимый) на urllib, без внешних зависимостей.

Что умеет: вызов инструментов; потоковый ответ SSE (`delta.content`,
`delta.reasoning_content`, `delta.tool_calls` по частям); переход на обычный ответ, если
поток не поддерживается; повторы с паузой при 429/5xx/сбоях сети; повтор без
необязательных параметров, если шлюз их не принимает (`parallel_tool_calls`,
`stream_options`, `tool_choice`); отмену без ожидания сервера; распознавание оборванного
потока и понятные сообщения об ошибках на русском.
"""

import http.client
import json
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import __version__, log

RETRYABLE_STATUSES = (408, 409, 425, 429, 500, 502, 503, 504)
USER_AGENT = "STE-Slicer-AIAssistant/%s" % __version__
MAX_TIMEOUT_RETRIES = 1          # запрос с таймаутом уже потратил request_timeout секунд
CANCEL_POLL_S = 0.1

# Необязательные поля запроса, на которые шлюз может ответить HTTP 400/422. Ищем их в тексте
# ошибки в этом порядке; "stream" (потоковый режим целиком) проверяем последним.
OPTIONAL_PARAMS = ("stream_options", "parallel_tool_calls", "tool_choice")

# адрес (base_url + model) -> параметры, которые он отклонил. Общее для всех клиентов:
# агент создаёт новый клиент на каждое сообщение.
_UNSUPPORTED = {}
_UNSUPPORTED_LOCK = threading.Lock()


class LLMError(Exception):
    """Ошибка обмена с LLM; `message` можно сразу показать пользователю.

    `partial` = True, если до ошибки часть ответа уже ушла в интерфейс (тогда клиент сам
    не повторяет запрос).
    """

    def __init__(self, message, kind="unknown", status=None, retryable=False, details=None, retry_after=None):
        Exception.__init__(self, message)
        self.message = message
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.details = details
        self.retry_after = retry_after
        self.partial = False


class LLMCancelled(LLMError):
    def __init__(self):
        LLMError.__init__(self, "Запрос остановлен пользователем", kind="cancelled")


class LLMClient(object):
    def __init__(self, base_url, api_key, model, timeout=180, verify_ssl=True, max_retries=3):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.max_retries = max_retries
        self.backoff_base = 1.5
        self.backoff_max = 20.0
        self._response = None
        self._response_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings, **overrides):
        values = settings.as_dict()
        values.update(overrides)
        return cls(values["base_url"], values["api_key"], values["model"],
                   timeout=values["request_timeout"], verify_ssl=values["verify_ssl"],
                   max_retries=values.get("max_retries", 3))

    def list_models(self):
        """Список id моделей, которые отдаёт сервер."""
        self._check_config(need_model=False)
        response = self._open(self._request("GET", "/models"))
        try:
            data = _read_json(response)
        finally:
            response.close()
        return sorted(item.get("id") for item in data.get("data", []) if item.get("id"))

    def chat(self, messages, tools=None, temperature=None, max_tokens=None, stream=True,
             on_event=None, cancel_event=None, tool_choice=None, parallel_tool_calls=None):
        """Один запрос Chat Completions, возвращает сообщение ассистента.

        В `on_event(kind, text)` приходят фрагменты потока, kind — "content" или "reasoning".
        Результат — словарь с ключами role, content, tool_calls (список, может быть пустым),
        reasoning_content, finish_reason, usage.
        """
        self._check_config(need_model=True)
        body = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
            if parallel_tool_calls is not None:
                body["parallel_tool_calls"] = bool(parallel_tool_calls)
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens:
            body["max_tokens"] = max_tokens
        for param in self._unsupported():
            if param == "stream":
                stream = False
            body.pop(param, None)

        attempt = 0
        timeouts = 0
        fallback_used = False
        while True:
            _raise_if_cancelled(cancel_event)
            emitted = []

            def tracking_event(kind, text):
                if cancel_event is not None and cancel_event.is_set():
                    return          # отменённый запрос может ещё дорабатывать в своём потоке
                emitted.append(kind)
                if on_event is not None:
                    on_event(kind, text)

            request_body = dict(body)
            try:
                if stream:
                    return self._cancellable(lambda: self._chat_stream(request_body, tracking_event), cancel_event)
                return self._cancellable(lambda: self._chat_plain(request_body, tracking_event), cancel_event)
            except LLMCancelled:
                raise
            except LLMError as exc:
                if exc.status in (400, 422) and not emitted:
                    dropped = self._drop_rejected_param(exc, body, stream, fallback_used)
                    if dropped:
                        if dropped == "stream":
                            stream = False
                        elif dropped == "*":
                            fallback_used = True
                        continue
                if emitted:
                    exc.partial = True
                # Если часть ответа уже показана, сами не повторяем — решает агент.
                if not exc.retryable or emitted or attempt >= self.max_retries:
                    raise
                if exc.kind == "timeout":
                    timeouts += 1
                    if timeouts > MAX_TIMEOUT_RETRIES:
                        raise
                delay = exc.retry_after or self.backoff_delay(attempt)
                attempt += 1
                log.warning("LLM request failed (%s), retry %d/%d in %.1f s", exc.message, attempt,
                            self.max_retries, delay)
                if cancel_event is not None:
                    if cancel_event.wait(delay):
                        raise LLMCancelled()
                else:
                    time.sleep(delay)

    def backoff_delay(self, attempt):
        """Экспоненциальная пауза с небольшим разбросом: 1.5, 3, 6, 12 … с (с потолком)."""
        delay = min(self.backoff_base * (2 ** attempt), self.backoff_max)
        return delay * (0.85 + 0.3 * random.random())

    def abort(self):
        """Закрывает читаемый ответ (при отмене; можно вызывать из любого потока)."""
        with self._response_lock:
            response, self._response = self._response, None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    # Необязательные параметры запроса

    def _endpoint_key(self):
        return "%s|%s" % (self.base_url, self.model)

    def _unsupported(self):
        with _UNSUPPORTED_LOCK:
            return set(_UNSUPPORTED.get(self._endpoint_key(), ()))

    def _remember_unsupported(self, param):
        with _UNSUPPORTED_LOCK:
            _UNSUPPORTED.setdefault(self._endpoint_key(), set()).add(param)

    def _drop_rejected_param(self, exc, body, stream, fallback_used):
        """Убирает необязательное поле, которое отклонил шлюз; возвращает его имя, "*" или None."""
        text = ("%s %s" % (exc.details or "", exc.message or "")).lower()
        if stream and "stream_options" in text:      # его добавляет _chat_stream, в `body` его нет
            log.warning("Endpoint rejected 'stream_options', retrying without it")
            self._remember_unsupported("stream_options")
            return "stream_options"
        for param in OPTIONAL_PARAMS:
            if param in body and param in text:
                log.warning("Endpoint rejected '%s', retrying without it", param)
                body.pop(param, None)
                self._remember_unsupported(param)
                return param
        if stream and "stream" in text:
            log.warning("Endpoint rejected streaming, falling back to non-stream mode")
            self._remember_unsupported("stream")
            return "stream"
        if not fallback_used and any(param in body for param in OPTIONAL_PARAMS[:2]):
            # Непонятная 400: один раз пробуем обычный запрос OpenAI без лишних полей.
            log.warning("Endpoint rejected the request (%s); retrying without optional parameters", exc.status)
            for param in OPTIONAL_PARAMS[:2]:
                body.pop(param, None)
            return "*"
        return None

    # Отправка запросов

    def _cancellable(self, func, cancel_event):
        """Выполняет блокирующий запрос во вспомогательном потоке, чтобы «Стоп» срабатывал сразу."""
        if cancel_event is None:
            return func()
        box = {}
        done = threading.Event()

        def runner():
            try:
                box["result"] = func()
            except BaseException as exc:  # пробросим в вызывающем потоке
                box["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=runner, name="AIAssistant-llm")
        worker.daemon = True
        worker.start()
        while not done.wait(CANCEL_POLL_S):
            if cancel_event.is_set():
                self.abort()
                raise LLMCancelled()
        if cancel_event.is_set():
            raise LLMCancelled()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _check_config(self, need_model):
        if not self.base_url:
            raise LLMError("Не задан URL LLM-сервера. Откройте настройки ассистента.", kind="config")
        if not self.api_key:
            raise LLMError("Не задан API-ключ. Откройте настройки ассистента и введите ключ.", kind="config")
        if need_model and not self.model:
            raise LLMError("Не задана модель. Укажите её в настройках ассистента.", kind="config")

    def _request(self, method, path, body=None, stream=False):
        data = None
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "User-Agent": USER_AGENT,
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        return urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)

    def _open(self, request):
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout,
                                              context=_ssl_context(self.verify_ssl))
        except urllib.error.HTTPError as exc:
            raise _http_error(exc)
        except urllib.error.URLError as exc:
            raise _network_error(exc.reason)
        except (socket.timeout, ssl.SSLError, OSError, http.client.HTTPException) as exc:
            raise _network_error(exc)
        with self._response_lock:
            self._response = response
        return response

    def _release(self, response):
        with self._response_lock:
            if self._response is response:
                self._response = None
        try:
            response.close()
        except Exception:
            pass

    def _chat_plain(self, body, on_event):
        body["stream"] = False
        response = self._open(self._request("POST", "/chat/completions", body))
        try:
            data = _read_json(response)
        except (socket.timeout, ssl.SSLError, OSError, http.client.HTTPException) as exc:
            raise _network_error(exc)
        finally:
            self._release(response)
        return _message_from_completion(data, on_event)

    def _chat_stream(self, body, on_event):
        body["stream"] = True
        if "stream_options" not in self._unsupported():
            body["stream_options"] = {"include_usage": True}
        response = self._open(self._request("POST", "/chat/completions", body, stream=True))
        try:
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type:
                # Некоторые прокси игнорируют "stream" и отдают обычный ответ.
                return _message_from_completion(_read_json(response), on_event)
            return self._read_sse(response, on_event)
        except (socket.timeout, ssl.SSLError, OSError, http.client.HTTPException, ValueError) as exc:
            # ValueError: чтение ответа, который закрыл abort()
            raise _network_error(exc, during_stream=True)
        finally:
            self._release(response)

    def _read_sse(self, response, on_event):
        assembler = _StreamAssembler(on_event)
        finished = False
        while True:
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                finished = True
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                log.warning("Skipping malformed SSE chunk: %s", payload[:200])
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise _api_error_from_payload(chunk, status=None)
            assembler.feed(chunk)
        if not finished and not assembler.finish_reason:
            raise LLMError("Поток ответа LLM оборвался (соединение закрыто до конца ответа).",
                           kind="stream_interrupted", retryable=True)
        return assembler.result()


class _StreamAssembler(object):
    """Собирает фрагменты потока в полное сообщение ассистента.

    Части вызовов инструментов сопоставляем по `index`. Бывают серверы, которые не шлют
    index или ставят 0 всем параллельным вызовам (так делал Qwen за LiteLLM) — тогда новый
    вызов узнаём по новому `id` или имени функции.
    """

    def __init__(self, on_event):
        self._on_event = on_event
        self._content = []
        self._reasoning = []
        self._calls = []          # собранные вызовы в порядке прихода
        self._by_index = {}       # index от сервера -> позиция в self._calls
        self.finish_reason = None
        self._usage = None

    def feed(self, chunk):
        if chunk.get("usage"):
            self._usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning and isinstance(reasoning, str):
                self._reasoning.append(reasoning)
                self._on_event("reasoning", reasoning)
            content = delta.get("content")
            if content and isinstance(content, str):
                self._content.append(content)
                self._on_event("content", content)
            for fragment in delta.get("tool_calls") or []:
                if isinstance(fragment, dict):
                    self._feed_tool_call(fragment)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def _new_call(self, index):
        entry = {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
        self._calls.append(entry)
        if index is not None:
            self._by_index[index] = len(self._calls) - 1
        return entry

    def _feed_tool_call(self, fragment):
        index = fragment.get("index")
        function = fragment.get("function") or {}
        new_id = fragment.get("id")
        name = function.get("name")
        entry = None
        if index is not None and index in self._by_index:
            entry = self._calls[self._by_index[index]]
        elif index is None and self._calls:
            entry = self._calls[-1]
        if entry is not None:
            starts_new = ((new_id and entry["id"] and new_id != entry["id"]) or
                          (name and entry["function"]["name"] and entry["function"]["arguments"]
                           and not new_id and index is None))
            if starts_new:
                entry = None
        if entry is None:
            entry = self._new_call(index)
        if new_id and not entry["id"]:
            entry["id"] = new_id
        if name and not entry["function"]["name"]:
            entry["function"]["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if arguments:
            entry["function"]["arguments"] += str(arguments)

    def result(self):
        calls = [_normalise_tool_call(call) for call in self._calls if call["function"]["name"] or
                 call["function"]["arguments"]]
        content, think = _split_think("".join(self._content))
        reasoning = "".join(self._reasoning) + think
        return {
            "role": "assistant",
            "content": content,
            "reasoning_content": reasoning,
            "tool_calls": calls,
            "finish_reason": self.finish_reason or ("tool_calls" if calls else "stop"),
            "usage": self._usage,
        }


def _message_from_completion(data, on_event):
    if not isinstance(data, dict):
        raise LLMError("LLM вернула неожиданный ответ", kind="bad_response", details=str(data)[:500])
    if data.get("error"):
        raise _api_error_from_payload(data, status=None)
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LLMError("LLM вернула пустой ответ (нет choices)", kind="bad_response", details=json.dumps(data)[:500])
    message = choices[0].get("message") or {}
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    content, think = _split_think(message.get("content") or "")
    reasoning = (reasoning if isinstance(reasoning, str) else "") + think
    if reasoning:
        on_event("reasoning", reasoning)
    if content:
        on_event("content", content)
    calls = [_normalise_tool_call(call) for call in message.get("tool_calls") or [] if isinstance(call, dict)]
    return {
        "role": "assistant",
        "content": content,
        "reasoning_content": reasoning,
        "tool_calls": calls,
        "finish_reason": choices[0].get("finish_reason") or ("tool_calls" if calls else "stop"),
        "usage": data.get("usage"),
    }


_THINK_RE = re.compile(r"^\s*<think>(.*?)(?:</think>|$)", re.DOTALL)


def _split_think(content):
    """Выносит из ответа блок <think>…</think> в начале (так отвечают некоторые сборки Qwen)."""
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    match = _THINK_RE.match(content)
    if not match:
        return content, ""
    return content[match.end():].lstrip(), match.group(1).strip()


def _normalise_tool_call(call):
    function = call.get("function") or {}
    arguments = function.get("arguments")
    if isinstance(arguments, (dict, list)):
        arguments = json.dumps(arguments, ensure_ascii=False)
    elif arguments is not None and not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {
        "id": str(call.get("id") or "") or "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {"name": str(function.get("name") or "").strip(), "arguments": arguments or ""},
    }


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise LLMCancelled()


def _read_json(response):
    raw = response.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        raise LLMError("Сервер LLM вернул не JSON: %s" % raw[:300], kind="bad_response", details=raw[:2000])


_SSL_CONTEXTS = {}


def _ssl_context(verify):
    """SSL-контекст с кэшем. Сертификаты — системные плюс certifi, если он есть в сборке."""
    if verify in _SSL_CONTEXTS:
        return _SSL_CONTEXTS[verify]
    context = ssl.create_default_context()
    if verify:
        try:
            import certifi
            cafile = certifi.where()
            if os.path.isfile(cafile):
                context.load_verify_locations(cafile)
        except Exception:  # certifi нет или он не читается — хватит системного хранилища
            pass
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    _SSL_CONTEXTS[verify] = context
    return context


def _http_error(exc):
    status = exc.code
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    detail = raw
    try:
        detail = _extract_error_message(json.loads(raw)) or raw
    except ValueError:
        pass
    detail = (detail or exc.reason or "").strip()
    retry_after = None
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header:
        try:
            retry_after = min(float(header), 60.0)
        except ValueError:
            retry_after = None

    if status in (401, 403):
        message = "Неверный API-ключ или нет доступа к модели (HTTP %d)." % status
        kind = "auth"
    elif status == 404:
        message = "Адрес не найден (HTTP 404): проверьте URL сервера и название модели."
        kind = "not_found"
    elif status == 429:
        message = "Превышен лимит запросов к LLM (HTTP 429). Повторите чуть позже."
        kind = "rate_limit"
    elif status >= 500:
        message = "Ошибка на стороне LLM-сервера (HTTP %d)." % status
        kind = "server"
    else:
        message = "LLM-сервер отклонил запрос (HTTP %d)." % status
        kind = "bad_request"
    if detail:
        message += " " + detail[:400]
    return LLMError(message, kind=kind, status=status, retryable=status in RETRYABLE_STATUSES,
                    details=raw[:4000], retry_after=retry_after)


def _api_error_from_payload(payload, status):
    detail = _extract_error_message(payload) or json.dumps(payload, ensure_ascii=False)[:400]
    return LLMError("Ошибка LLM: %s" % detail, kind="api_error", status=status, details=detail)


def _extract_error_message(payload):
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message") or error.get("code")
    if isinstance(error, str):
        return error
    return payload.get("message") or payload.get("detail")


def _network_error(reason, during_stream=False):
    if isinstance(reason, LLMError):
        return reason
    text = str(reason) or type(reason).__name__
    if isinstance(reason, (ssl.CertificateError, ssl.SSLError)) or "CERTIFICATE" in text.upper():
        return LLMError("Ошибка SSL при подключении к LLM: %s. Проверьте системное время и сертификаты; "
                        "в крайнем случае отключите проверку SSL в настройках." % text,
                        kind="ssl", retryable=False, details=text)
    if isinstance(reason, socket.timeout) or "timed out" in text:
        return LLMError("LLM не ответила вовремя (таймаут). Попробуйте ещё раз или увеличьте таймаут в настройках.",
                        kind="timeout", retryable=True, details=text)
    if isinstance(reason, socket.gaierror):
        return LLMError("Не удалось найти сервер LLM (DNS): проверьте URL и подключение к интернету.",
                        kind="network", retryable=True, details=text)
    if isinstance(reason, ConnectionRefusedError):
        return LLMError("Сервер LLM отказал в подключении: проверьте URL и порт.", kind="network",
                        retryable=True, details=text)
    if during_stream:
        return LLMError("Поток ответа LLM оборвался: %s" % text, kind="stream_interrupted", retryable=True,
                        details=text)
    return LLMError("Нет связи с LLM-сервером: %s" % text, kind="network", retryable=True, details=text)
