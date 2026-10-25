"""Локальный HTTP-сервер: JSON API для панели чата в слайсере и MCP для внешних агентов.

Маршруты
    GET  /                      информация о сервисе (сам интерфейс — QML-панель в слайсере)
    GET  /api/health            состояние сервера
    GET  /api/settings          настройки (ключ API замаскирован)
    POST /api/settings          частичное обновление настроек
    POST /api/settings/test     проверка адреса/ключа/модели (можно до сохранения)
    GET  /api/models            список моделей у провайдера
    GET  /api/tools             каталог инструментов
    GET  /api/sessions          список диалогов
    POST /api/sessions          новый диалог
    GET  /api/session?session=  история диалога и номер последнего события
    POST /api/message           {session, text, attachments:[id вложений]}
    POST /api/stop              {session}
    POST /api/reset             {session}
    POST /api/upload?session=&name=   тело запроса — сам файл
    GET  /api/attachment?id=    загруженное фото (для превью)
    GET  /api/events?session=&since=&timeout=   long polling
    POST /mcp                   MCP JSON-RPC 2.0 (ответы application/json)

Безопасность: слушаем только 127.0.0.1, отклоняем чужие заголовки Host (DNS rebinding) и
запросы с чужих Origin. POST в API обязан нести заголовок X-AI-Assistant: чужая страница
не может его отправить без CORS preflight, а preflight этот сервер не одобряет.
"""

import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlsplit

from . import __version__, log
from .attachments import AttachmentError, public_record
from .chat_agent import AgentBusyError
from .config import SettingsError, mask_secret
from .llm_client import LLMClient, LLMError

LOCAL_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]", "::1")
CSRF_HEADER = "X-AI-Assistant"
MAX_JSON_BODY = 1024 * 1024
LONG_POLL_MAX = 30.0


class HttpError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # В Windows SO_REUSEADDR позволяет двум процессам занять один порт. Отключаем, чтобы
    # заметить занятый порт и перейти на свободный.
    allow_reuse_address = False


class AssistantServer(object):
    """Держит поток HTTP-сервера и объекты, нужные обработчикам."""

    def __init__(self, sessions, settings, mcp, uploads, host_name="STE Slicer"):
        self.sessions = sessions
        self.settings = settings
        self.mcp = mcp
        self.uploads = uploads
        self.host_name = host_name
        self.started = None
        self._httpd = None
        self._thread = None

    @property
    def port(self):
        return self._httpd.server_port if self._httpd else None

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.port if self._httpd else None

    @property
    def running(self):
        return self._httpd is not None

    def start(self, preferred_port=0):
        if self._httpd is not None:
            return self.port
        httpd = None
        if preferred_port:
            try:
                httpd = _ThreadingHTTPServer(("127.0.0.1", preferred_port), _Handler)
            except OSError as exc:
                log.warning("Port %d is busy (%s), using a free port", preferred_port, exc)
        if httpd is None:
            httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.assistant = self
        self._httpd = httpd
        self.started = time.time()
        self._thread = threading.Thread(target=httpd.serve_forever, name="AIAssistant-http",
                                        kwargs={"poll_interval": 0.5})
        self._thread.daemon = True
        self._thread.start()
        log.info("AI Assistant server listening on %s", self.url)
        return self.port

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            log.exception("Error while stopping the HTTP server")
        log.info("AI Assistant server stopped")


class _Handler(BaseHTTPRequestHandler):
    server_version = "AIAssistant/" + __version__
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.assistant

    def log_message(self, fmt, *args):  # стандартный вывод каждого запроса в stderr не нужен
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        # CORS не поддерживаем: запросы с чужих сайтов нам не нужны.
        self._send_json(405, {"error": "Method not allowed"}, extra_headers={"Allow": "GET, POST"})

    def _dispatch(self, method):
        url = urlsplit(self.path)
        path = url.path
        self.query = {key: values[-1] for key, values in parse_qs(url.query).items()}
        self._body = None
        try:
            self._check_host()
            if method != "GET":
                self._check_origin()
            if path == "/mcp":
                return self._handle_mcp(method)
            if path.startswith("/api/"):
                route = _ROUTES.get((method, path))
                if route is None:
                    raise HttpError(404, "Unknown API route: %s %s" % (method, path))
                if method == "POST" and not self.headers.get(CSRF_HEADER):
                    raise HttpError(403, "Missing %s header" % CSRF_HEADER)
                if method == "POST" and path != "/api/upload":
                    # Тело читаем всегда: непрочитанные байты испортят следующий запрос
                    # в том же keep-alive соединении.
                    self._body = self._read_body()
                result = route(self)
                if result is not None:
                    self._send_json(200, result)
                return
            if method != "GET":
                raise HttpError(405, "Method not allowed")
            if path != "/":
                raise HttpError(404, "Not found")
            self._send_json(200, {"service": "AI Assistant for STE Slicer", "version": __version__,
                                  "ui": "panel inside STE Slicer", "api": "/api/health", "mcp": "/mcp"})
        except HttpError as exc:
            self.close_connection = True  # тело запроса могло остаться непрочитанным
            self._send_json(exc.status, {"error": exc.message})
        except (ConnectionError, OSError) as exc:
            log.debug("Client connection error on %s: %s", path, exc)
        except Exception as exc:
            log.exception("Unhandled error on %s %s", method, path)
            try:
                self._send_json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
            except OSError:
                pass

    def _check_host(self):
        host = (self.headers.get("Host") or "").strip().lower()
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if hostname not in LOCAL_HOSTNAMES:
            raise HttpError(403, "Forbidden host")

    def _check_origin(self):
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return  # не браузер: MCP-клиенты, curl
        allowed = ("http://127.0.0.1:%d" % self.app.port, "http://localhost:%d" % self.app.port)
        if origin.rstrip("/") not in allowed:
            raise HttpError(403, "Cross-origin requests are not allowed")

    def _read_body(self, limit=MAX_JSON_BODY):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise HttpError(413, "Request body too large")
        return self.rfile.read(length) if length else b""

    def _read_json(self):
        raw = self._body if self._body is not None else self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise HttpError(400, "Invalid JSON: %s" % exc)

    def _send_bytes(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", extra_headers)

    def _session(self, payload=None, create=True):
        session_id = (payload or {}).get("session") or self.query.get("session")
        if not session_id:
            raise HttpError(400, "Parameter 'session' is required")
        session = self.app.sessions.get(session_id, create=create)
        if session is None:
            raise HttpError(404, "Unknown session")
        return session

    # MCP

    def _handle_mcp(self, method):
        if method == "DELETE":
            self._send_bytes(204, b"", "text/plain")
            return
        if method != "POST":
            # Поток SSE от сервера мы не реализуем.
            raise HttpError(405, "Use POST for MCP JSON-RPC requests")
        raw = self._read_body(limit=8 * MAX_JSON_BODY)
        response = self.app.mcp.handle_text(raw.decode("utf-8", "replace"))
        if response is None:
            self._send_bytes(202, b"", "text/plain")  # в запросе были только уведомления
            return
        headers = {}
        if isinstance(response, dict) and "protocolVersion" in (response.get("result") or {}):
            headers["Mcp-Session-Id"] = self.app.mcp.new_session_id()
        self._send_json(200, response, extra_headers=headers)

    # API: информация и настройки

    def api_health(self):
        return {"ok": True, "version": __version__, "host": self.app.host_name, "port": self.app.port,
                "uptime_s": int(time.time() - (self.app.started or time.time()))}

    def api_get_settings(self):
        return {"settings": self.app.settings.public_dict()}

    def api_post_settings(self):
        try:
            updated = self.app.settings.update(self._read_json())
        except SettingsError as exc:
            raise HttpError(400, str(exc))
        return {"ok": True, "updated": list(updated), "settings": self.app.settings.public_dict()}

    def _client_from_payload(self, payload):
        settings = self.app.settings
        overrides = {}
        for key in ("base_url", "api_key", "model"):
            value = payload.get(key)
            if value is None or (key == "api_key" and value == mask_secret(settings.get("api_key"))):
                continue
            overrides[key] = value.strip().rstrip("/") if key == "base_url" else value.strip()
        return LLMClient.from_settings(settings, **overrides)

    def api_test_settings(self):
        client = self._client_from_payload(self._read_json())
        client.max_retries = 0
        started = time.time()
        result = {"ok": False, "model": client.model}
        try:
            models = client.list_models()
            result["models_count"] = len(models)
            result["model_listed"] = client.model in models
        except LLMError as exc:
            if exc.kind in ("auth", "config", "ssl", "network", "timeout"):
                result["message"] = exc.message
                return result
            result["models_count"] = None  # /models у OpenAI-совместимых серверов необязателен
        try:
            reply = client.chat([{"role": "user", "content": "Ответь одним словом: готово"}], stream=False)
        except LLMError as exc:
            result["message"] = exc.message
            return result
        result["ok"] = True
        result["latency_ms"] = int((time.time() - started) * 1000)
        result["reply"] = (reply.get("content") or "")[:200]
        result["message"] = "Подключение работает: модель %s ответила за %.1f с" % (
            client.model, result["latency_ms"] / 1000.0)
        return result

    def api_models(self):
        try:
            return {"models": LLMClient.from_settings(self.app.settings).list_models()}
        except LLMError as exc:
            raise HttpError(502, exc.message)

    def api_tools(self):
        registry = self.app.sessions.registry
        return {"tools": [dict(tool.to_mcp(), group=tool.group,
                               main_thread=tool.main_thread) for tool in registry.tools()]}

    # API: чат

    def api_list_sessions(self):
        return {"sessions": self.app.sessions.list_sessions()}

    def api_new_session(self):
        session = self.app.sessions.create_session()
        return {"session": session.summary()}

    def api_session(self):
        session = self._session()
        items, last_seq = session.events.snapshot()
        return {"session": session.summary(), "items": items, "last_seq": last_seq, "usage": session.usage}

    def api_message(self):
        payload = self._read_json()
        session = self._session(payload)
        try:
            turn_id = self.app.sessions.send_message(session.id, payload.get("text") or "",
                                                     payload.get("attachments") or [])
        except AgentBusyError as exc:
            raise HttpError(409, str(exc))
        except ValueError as exc:
            raise HttpError(400, str(exc))
        return {"ok": True, "turn_id": turn_id}

    def api_stop(self):
        session = self._session(self._read_json(), create=False)
        return {"ok": True, "stopping": self.app.sessions.stop(session.id)}

    def api_reset(self):
        session = self._session(self._read_json(), create=False)
        try:
            self.app.sessions.reset(session.id)
        except AgentBusyError as exc:
            raise HttpError(409, str(exc))
        return {"ok": True}

    def api_events(self):
        session = self._session()
        try:
            since = int(self.query.get("since", 0))
            timeout = min(float(self.query.get("timeout", 25)), LONG_POLL_MAX)
        except ValueError:
            raise HttpError(400, "Invalid 'since' or 'timeout'")
        events = session.events.since(since, timeout=timeout)
        # По `head` панель замечает перезапуск сервера: head меньше её последнего seq.
        return {"events": events, "last_seq": events[-1]["seq"] if events else since,
                "head": session.events.last_seq, "running": session.running}

    # API: файлы

    def api_upload(self):
        session = self._session()
        name = self.query.get("name")
        if not name:
            raise HttpError(400, "Parameter 'name' is required")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            record = self.app.uploads.save_stream(session.id, name, self.rfile, length)
        except AttachmentError as exc:
            self.close_connection = True  # непрочитанное тело делает соединение непригодным
            raise HttpError(400, str(exc))
        return {"attachment": public_record(record)}

    def api_attachment(self):
        record = self.app.uploads.get(self.query.get("id", ""))
        if record is None or record["kind"] != "image" or not os.path.isfile(record["path"]):
            raise HttpError(404, "Attachment not found")
        content_type = mimetypes.guess_type(record["path"])[0] or "application/octet-stream"
        with open(record["path"], "rb") as handle:
            self._send_bytes(200, handle.read(), content_type)


_ROUTES = {
    ("GET", "/api/health"): _Handler.api_health,
    ("GET", "/api/settings"): _Handler.api_get_settings,
    ("POST", "/api/settings"): _Handler.api_post_settings,
    ("POST", "/api/settings/test"): _Handler.api_test_settings,
    ("GET", "/api/models"): _Handler.api_models,
    ("GET", "/api/tools"): _Handler.api_tools,
    ("GET", "/api/sessions"): _Handler.api_list_sessions,
    ("POST", "/api/sessions"): _Handler.api_new_session,
    ("GET", "/api/session"): _Handler.api_session,
    ("POST", "/api/message"): _Handler.api_message,
    ("POST", "/api/stop"): _Handler.api_stop,
    ("POST", "/api/reset"): _Handler.api_reset,
    ("GET", "/api/events"): _Handler.api_events,
    ("POST", "/api/upload"): _Handler.api_upload,
    ("GET", "/api/attachment"): _Handler.api_attachment,
}
