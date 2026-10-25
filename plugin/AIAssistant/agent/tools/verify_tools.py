"""Самопроверка: перечитать сохранённый проект 3MF (и G-code) и сравнить с тем, что агент заявил.

Только чтение файлов, без обращений к слайсеру, поэтому обработчик работает в потоке
вызывающего. Без `path` проверяется самый свежий .3mf в папке результатов плагина (туда
по умолчанию пишет save_project); G-code подбирается так же, если он записан в пределах
30 минут от проекта.
"""

import os

from .. import log
from .. import verify as verify_mod
from .registry import ToolError

GROUP = "verify"


def register(registry):
    registry.tool(
        "verify_project",
        "Проверить сохранённый 3MF (и G-code): режим, материал, профиль, настройки, положение модели; "
        "сверить с expected. Без path — последний сохранённый. Вызывай после save_project/export_gcode.",
        {"type": "object", "additionalProperties": False, "properties": {
            "path": {"type": "string"},
            "expected": {"type": "object", "description": "printing_mode, material_id, quality_type, "
                                                          "settings{key:value}"},
            "gcode_path": {"type": "string"},
        }}, main_thread=False, group=GROUP,
    )(_verify_project)


def _catalog_settings():
    try:
        from ..knowledge.settings_catalog import get_catalog
        catalog = get_catalog()
        return dict((key, catalog.get(key)) for key in catalog.keys())
    except Exception:
        log.exception("Settings catalog is unavailable, verifying without it")
        return None


def _results_dir(ctx):
    return ctx.services.get("results_dir") or os.path.join(os.path.expanduser("~"), "AIAssistant")


def _verify_project(args, ctx):
    results_dir = _results_dir(ctx)
    path = (args.get("path") or "").strip().strip('"')
    auto = not path
    if auto:
        path = verify_mod.newest_file(results_dir, ".3mf")
        if path is None:
            raise ToolError("Нет сохранённых проектов в %s — сначала save_project или укажи path." % results_dir)
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise ToolError("Файл не найден: %s" % path)
    gcode_path = (args.get("gcode_path") or "").strip().strip('"')
    if gcode_path:
        gcode_path = os.path.abspath(os.path.expanduser(gcode_path))
    elif auto:
        gcode_path = verify_mod.companion_gcode(path, results_dir)
    try:
        return verify_mod.verify(path, args.get("expected") or None, gcode_path or None, _catalog_settings())
    except verify_mod.ProjectError as exc:
        raise ToolError(str(exc))
