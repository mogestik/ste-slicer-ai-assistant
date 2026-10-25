"""Быстрая проверка MCP (JSON-RPC 2.0 через HTTP POST /mcp).

    python devtools/mcp_smoke_test.py [--url http://127.0.0.1:8765/mcp]

Проверяет initialize, notifications/initialized, ping, tools/list, tools/call (успех,
ошибка проверки аргументов, неизвестный инструмент), неизвестный метод и ошибку разбора.
Код выхода 0, если всё прошло.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


class Checker(object):
    def __init__(self, url):
        self.url = url
        self.failures = 0
        self._next_id = 0

    def post(self, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.url, data=data, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return response.status, (json.loads(body) if body else None), response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return exc.code, (json.loads(body) if body else None), exc.headers

    def rpc(self, method, params=None):
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        return self.post(payload)

    def check(self, title, condition, detail=""):
        print("[%s] %s%s" % ("OK" if condition else "FAIL", title, (" — " + detail) if detail and not condition else ""))
        if not condition:
            self.failures += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()
    c = Checker(args.url)

    status, body, headers = c.rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "mcp-smoke-test", "version": "1.0"}})
    result = (body or {}).get("result", {})
    c.check("initialize", status == 200 and result.get("protocolVersion") == "2025-06-18"
            and "tools" in result.get("capabilities", {}) and result.get("serverInfo", {}).get("name"),
            json.dumps(body, ensure_ascii=False))
    c.check("Mcp-Session-Id header", bool(headers.get("Mcp-Session-Id")))

    status, body, _ = c.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    c.check("notifications/initialized -> 202 without body", status == 202 and body is None, str(status))

    status, body, _ = c.rpc("ping")
    c.check("ping", status == 200 and body.get("result") == {}, json.dumps(body))

    status, body, _ = c.rpc("tools/list")
    tools = (body or {}).get("result", {}).get("tools", [])
    names = [tool["name"] for tool in tools]
    c.check("tools/list returns tools", len(tools) >= 20, "%d tools" % len(tools))
    c.check("every tool has name/description/inputSchema",
            all(t.get("name") and t.get("description") and t.get("inputSchema", {}).get("type") == "object"
                for t in tools))
    for required in ("load_model", "set_settings", "start_slicing", "export_gcode", "save_project", "set_print_mode"):
        c.check("tool %s listed" % required, required in names)
    print("     tools: " + ", ".join(names))

    status, body, _ = c.rpc("tools/call", {"name": "ping", "arguments": {}})
    result = (body or {}).get("result", {})
    c.check("tools/call ping", status == 200 and result.get("isError") is False
            and "pong" in result.get("content", [{}])[0].get("text", ""), json.dumps(body, ensure_ascii=False))

    status, body, _ = c.rpc("tools/call", {"name": "get_app_info", "arguments": {}})
    result = (body or {}).get("result", {})
    c.check("tools/call get_app_info (main thread)", result.get("isError") is False,
            json.dumps(body, ensure_ascii=False))

    status, body, _ = c.rpc("tools/call", {"name": "set_print_mode", "arguments": {"mode": "4D"}})
    result = (body or {}).get("result", {})
    c.check("tools/call enum validation -> isError", result.get("isError") is True,
            json.dumps(body, ensure_ascii=False))
    print("     " + result.get("content", [{}])[0].get("text", ""))

    status, body, _ = c.rpc("tools/call", {"name": "get_settings", "arguments": {}})
    result = (body or {}).get("result", {})
    c.check("tools/call missing required -> isError", result.get("isError") is True)

    status, body, _ = c.rpc("tools/call", {"name": "no_such_tool", "arguments": {}})
    c.check("unknown tool -> JSON-RPC error -32602", (body or {}).get("error", {}).get("code") == -32602)

    status, body, _ = c.rpc("resources/list")
    c.check("unknown method -> -32601", (body or {}).get("error", {}).get("code") == -32601)

    status, body, _ = c.post(None, raw=b"{not json")
    c.check("parse error -> -32700", (body or {}).get("error", {}).get("code") == -32700)

    status, body, _ = c.post([{"jsonrpc": "2.0", "id": 100, "method": "ping"},
                              {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    c.check("batch request", isinstance(body, list) and len(body) == 1 and body[0].get("id") == 100)

    print("\n%s: %d failure(s)" % ("PASSED" if not c.failures else "FAILED", c.failures))
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
