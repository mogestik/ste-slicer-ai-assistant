"""Служебные инструменты, не связанные с нарезкой."""

import os
import platform
import sys
import time

from .. import __version__

GROUP = "app"


def register(registry):
    registry.tool(
        "ping",
        "Проверка связи: 'pong' и время сервера.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        main_thread=False, group=GROUP,
    )(_ping)

    registry.tool(
        "get_app_info",
        "Диагностика: версии плагина, Python и слайсера, число инструментов.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        main_thread=True, group=GROUP,
    )(_make_app_info(registry))

    registry.tool(
        "list_attachments",
        "Файлы, прикреплённые в диалоге (модели, изображения, документы), с локальными путями.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["any", "model", "image", "document"], "default": "any"},
            },
            "additionalProperties": False,
        },
        main_thread=False, group=GROUP,
    )(_list_attachments)


def _ping(args, ctx):
    return {"result": "pong", "server_time": time.strftime("%Y-%m-%d %H:%M:%S")}


def _make_app_info(registry):
    def handler(args, ctx):
        info = {
            "plugin_version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "tools_count": len(registry.tools()),
            "slicer_running": False,
        }
        try:
            from UM.Application import Application
            app = Application.getInstance()
        except ImportError:
            app = None
        if app is not None:
            info["slicer_running"] = True
            info["application"] = app.getApplicationName()
            info["application_version"] = app.getVersion()
        else:
            info["host"] = ctx.services.get("host_name", "standalone")
        try:
            from ..slicer import get_adapter
            info["slicer_adapter"] = get_adapter().name
        except Exception as exc:  # SlicerUnavailable: вне слайсера и без мока
            info["slicer_adapter"] = None
            info["slicer_adapter_error"] = str(exc)
        return info
    return handler


def _list_attachments(args, ctx):
    uploads = ctx.services.get("uploads")
    if uploads is None:
        return {"files": []}
    kind = args.get("kind", "any")
    files = []
    for item in uploads.list_for_session(ctx.session_id):
        if kind != "any" and item["kind"] != kind:
            continue
        files.append({"name": item["name"], "kind": item["kind"], "path": item["path"],
                      "size_bytes": item["size"], "exists": os.path.isfile(item["path"])})
    return {"files": files}
