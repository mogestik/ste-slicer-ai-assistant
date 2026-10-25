"""Каталог инструментов плагина.

`build_registry()` собирает все группы инструментов в один ToolRegistry. Его отдаёт MCP
(tools/list всегда возвращает ВСЕ инструменты) и его же получает LLM.

Экономия токенов: агент не шлёт модели все схемы на каждом шаге. Редкие инструменты
(EXTRA_TOOLS) перечислены только по имени в описании служебного инструмента
`enable_tools`. Полную схему агент добавляет, когда модель её попросит, вызовет такой
инструмент напрямую или когда о нём говорит сообщение пользователя (EXTRA_KEYWORDS).
Всё остальное — основной набор, он отправляется всегда.
"""

import re
from collections import OrderedDict

from .registry import (Tool, ToolContext, ToolError, ToolRegistry,  # noqa: F401
                       ToolResult, ToolValidationError, validate)
from . import (app_tools, geometry_tools, knowledge_tools, scene_tools, settings_tools, slicing_tools,
               strength_tools, verify_tools)

TOOL_GROUPS = (
    ("app", "Служебные", app_tools),
    ("settings", "Настройки и профили", settings_tools),
    ("scene", "Модели и сцена", scene_tools),
    ("slicing", "Слайсинг и экспорт", slicing_tools),
    ("geometry", "Анализ геометрии", geometry_tools),
    ("strength", "Прочность 3D/5D", strength_tools),
    ("knowledge", "Знания и подбор", knowledge_tools),
    ("verify", "Самопроверка результата", verify_tools),
)

# имя -> краткое описание для каталога enable_tools.
EXTRA_TOOLS = OrderedDict([
    ("get_settings", "значения и диапазоны настроек"),
    ("list_attachments", "вложения диалога"),
    ("get_slicing_status", "статус слайсинга"),
    ("select_machine", "выбор принтера"),
    ("list_profiles", "список профилей"),
    ("create_profile", "профиль из текущих настроек"),
    ("save_profile", "сохранить в профиль"),
    ("reset_settings", "сброс настроек"),
    ("get_object_dimensions", "подробные габариты"),
    ("scale_object", "масштаб"),
    ("center_for_5d", "центр на ось 5D"),
    ("arrange_all", "расстановка"),
    ("duplicate_object", "копии объекта (партия)"),
    ("set_support_material", "поддержки другим материалом (2-й экструдер)"),
    ("delete_objects", "удаление объектов"),
    ("cancel_slicing", "стоп слайсинга"),
    ("recommend_print_mode", "режим по геометрии"),
    ("get_app_info", "диагностика"),
    ("ping", "проверка связи"),
])

# Регулярки по сообщению пользователя (в нижнем регистре): что подключить заранее.
EXTRA_KEYWORDS = (
    (r"профил", ("list_profiles", "create_profile", "save_profile")),
    (r"принтер|машин", ("select_machine",)),
    (r"текущ|какое значение|какая .*(настройк|температур|высот)|провер", ("get_settings",)),
    (r"сброс|по умолчанию|верни настройк", ("reset_settings",)),
    (r"масштаб|увелич|уменьш|в \d+([.,]\d+)? раз|scale", ("scale_object",)),
    (r"удали|убери|очисти|delete", ("delete_objects",)),
    (r"расстав|разлож|arrange", ("arrange_all",)),
    (r"поддерж\w* (из |друг|втор|раствор)|раствор\w* поддерж|pva|hips|втор\w* экструдер", ("set_support_material",)),
    (r"партия|партии|\d+ (шт|копи|фигур|дет)|копи[июя]|размнож", ("duplicate_object", "arrange_all", "delete_objects")),
    (r"габарит|размер|объ[её]м|площад", ("get_object_dimensions",)),
    (r"останови сла|отмени сла|прерви", ("cancel_slicing", "get_slicing_status")),
    (r"вложен|прикреп|файл", ("list_attachments",)),
    (r"верси|диагност|не работает|ошибк[аи] плагин|ping|связ", ("get_app_info", "ping")),
)

ENABLE_TOOLS_NAME = "enable_tools"


def build_registry():
    registry = ToolRegistry()
    for _group_id, _title, module in TOOL_GROUPS:
        module.register(registry)
    return registry


def core_tool_names(registry):
    return [tool.name for tool in registry.tools() if tool.name not in EXTRA_TOOLS]


def extra_tool_names(registry):
    return [name for name in EXTRA_TOOLS if registry.has(name)]


def tools_for_text(text):
    """Редкие инструменты, которые стоит подключить по словам из сообщения."""
    lowered = (text or "").lower()
    names = []
    for pattern, tool_names in EXTRA_KEYWORDS:
        if re.search(pattern, lowered):
            names.extend(name for name in tool_names if name not in names)
    return names


def enable_tools_schema(registry, enabled=()):
    """Описание служебного инструмента enable_tools для OpenAI (в MCP его нет)."""
    available = [name for name in extra_tool_names(registry) if name not in enabled]
    return {
        "type": "function",
        "function": {
            "name": ENABLE_TOOLS_NAME,
            "description": "Подключить редкие инструменты (доступны со следующего шага): " +
                           "; ".join("%s — %s" % (name, EXTRA_TOOLS[name]) for name in available) + ".",
            "parameters": {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                           "required": ["names"]},
        },
    }
