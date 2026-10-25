"""Минимальный MCP-сервер: диспетчер JSON-RPC 2.0 поверх реестра инструментов.

Реализовано подмножество Model Context Protocol, которого хватает клиентам инструментов:
initialize, notifications/initialized, ping, tools/list, tools/call. Модуль не знает про
транспорт: `handle(payload)` принимает разобранный JSON (один запрос или пачку) и
возвращает ответ, либо None, если пришли только уведомления. HTTP-часть — в
http_server.py (POST /mcp, ответы application/json).
"""

import json
import uuid

from . import __version__, log
from .tools.registry import ToolContext

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

SERVER_INSTRUCTIONS = (
    "Инструменты управляют STE Slicer: настройки и профили, режим печати 3D/5D, модели на столе, "
    "слайсинг и экспорт G-code/3MF."
)


class JsonRpcError(Exception):
    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.data = data


class McpServer(object):
    def __init__(self, registry, server_name="ste-slicer-ai-assistant", services=None):
        self._registry = registry
        self._server_name = server_name
        self._services = services or {}
        self._methods = {
            "initialize": self._initialize,
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }

    def handle(self, payload):
        if isinstance(payload, list):
            if not payload:
                return _error_response(None, INVALID_REQUEST, "Empty batch")
            responses = [self._handle_one(item) for item in payload]
            responses = [item for item in responses if item is not None]
            return responses or None
        return self._handle_one(payload)

    def handle_text(self, text):
        """Разбирает тело запроса и обрабатывает его (для транспорта)."""
        try:
            payload = json.loads(text)
        except ValueError as exc:
            return _error_response(None, PARSE_ERROR, "Parse error: %s" % exc)
        return self.handle(payload)

    def new_session_id(self):
        return uuid.uuid4().hex

    def _handle_one(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            request_id = message.get("id") if isinstance(message, dict) else None
            if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
                return None  # это ответ клиента, отвечать на него не нужно
            return _error_response(request_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

        method = message["method"]
        is_notification = "id" not in message
        request_id = message.get("id")
        params = message.get("params") or {}

        if is_notification:
            # На notifications/initialized, notifications/cancelled и т. п. не отвечаем.
            log.debug("MCP notification: %s", method)
            return None

        handler = self._methods.get(method)
        if handler is None:
            return _error_response(request_id, METHOD_NOT_FOUND, "Method not found: %s" % method)
        if not isinstance(params, dict):
            return _error_response(request_id, INVALID_PARAMS, "params must be an object")
        try:
            return {"jsonrpc": "2.0", "id": request_id, "result": handler(params)}
        except JsonRpcError as exc:
            return _error_response(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            log.exception("MCP method %s failed", method)
            return _error_response(request_id, INTERNAL_ERROR, "%s: %s" % (type(exc).__name__, exc))

    # Методы протокола

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        client = params.get("clientInfo") or {}
        log.info("MCP initialize from %s %s (protocol %s)", client.get("name"), client.get("version"), version)
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self._server_name, "title": "STE Slicer AI Assistant",
                           "version": __version__},
            "instructions": SERVER_INSTRUCTIONS,
        }

    def _ping(self, params):
        return {}

    def _tools_list(self, params):
        # Инструментов немного — отдаём одной страницей, без курсора.
        return {"tools": self._registry.to_mcp_list()}

    def _tools_call(self, params):
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires 'name'")
        if not self._registry.has(name):
            raise JsonRpcError(INVALID_PARAMS, "Unknown tool: %s" % name)
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "'arguments' must be an object")
        ctx = ToolContext(session_id="mcp", services=self._services)
        return self._registry.call(name, arguments, ctx).to_mcp()


def _error_response(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
