"""Общий реестр инструментов для MCP-сервера и агента.

Инструмент описывается один раз (имя, описание, JSON-схема, обработчик) и дальше
доступен и как MCP-инструмент (tools/list, tools/call), и как элемент `tools` для
Chat Completions.

Обработчик: `handler(args, ctx)`, где `args` — проверенный словарь аргументов, `ctx` —
ToolContext. Возвращает любое значение, которое можно сериализовать в JSON (или строку).
Ожидаемые ошибки, понятные пользователю, — через ToolError.
"""

import ast
import difflib
import json
import re
import threading
import time
from collections import OrderedDict

from .. import log
from ..main_thread import run_in_main_thread

MAX_RESULT_CHARS = 16000   # ~5 тыс. токенов: больше — и маленькая модель тонет в контексте
MAIN_THREAD_TIMEOUT = 120.0


class ToolError(Exception):
    """Ожидаемая ошибка инструмента; текст видят и модель, и пользователь."""


class ToolValidationError(ToolError):
    pass


class ToolContext(object):
    """Всё, что доступно обработчику кроме аргументов."""

    def __init__(self, session_id=None, emit=None, cancel_event=None, services=None):
        self.session_id = session_id
        self._emit = emit
        self.cancel_event = cancel_event or threading.Event()
        # Общие объекты от хоста: настройки, папка загрузок, сведения о приложении...
        self.services = services or {}

    def emit(self, event_type, **data):
        """Отправляет событие интерфейса (например, slicing_progress) в диалог, откуда пришёл вызов."""
        if self._emit is not None:
            self._emit(event_type, **data)

    @property
    def cancelled(self):
        return self.cancel_event.is_set()


class ToolResult(object):
    def __init__(self, ok, data, text):
        self.ok = ok
        self.data = data
        self.text = text

    def to_mcp(self):
        return {"content": [{"type": "text", "text": self.text}], "isError": not self.ok}


class Tool(object):
    def __init__(self, name, description, input_schema, handler, main_thread=True,
                 group="general", title=None):
        self.name = name
        self.title = title or name
        self.description = description.strip()
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self.handler = handler
        # True — обработчик целиком выполняется в главном потоке Qt (короткие операции со
        # сценой и настройками). False — в потоке вызывающего; к слайсеру он обращается сам
        # через run_in_main_thread (долгие инструменты: нарезка, загрузка, запись файлов).
        self.main_thread = main_thread
        self.group = group

    def to_mcp(self):
        return OrderedDict([
            ("name", self.name),
            ("title", self.title),
            ("description", self.description),
            ("inputSchema", self.input_schema),
        ])

    def to_openai(self, compact=False):
        """Элемент `tools` для OpenAI. compact=True убирает поля схемы, которые только тратят
        токены (`additionalProperties`, диапазоны, `default: false`); validate их всё равно проверит."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _compact_schema(self.input_schema) if compact else self.input_schema,
            },
        }


class ToolRegistry(object):
    def __init__(self):
        self._tools = OrderedDict()

    def add(self, tool, replace=False):
        if tool.name in self._tools and not replace:
            raise ValueError("Tool already registered: %s" % tool.name)
        self._tools[tool.name] = tool
        return tool

    def tool(self, name, description, input_schema=None, main_thread=True, group="general", title=None):
        """`add` в виде декоратора."""
        def decorator(handler):
            self.add(Tool(name, description, input_schema, handler, main_thread=main_thread,
                          group=group, title=title))
            return handler
        return decorator

    def override(self, name, handler, main_thread=None):
        """Заменить обработчик существующего инструмента (имя и схема остаются)."""
        tool = self.get(name)
        tool.handler = handler
        if main_thread is not None:
            tool.main_thread = main_thread
        return tool

    def get(self, name):
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(name)
        return tool

    def has(self, name):
        return name in self._tools

    def tools(self):
        return list(self._tools.values())

    def to_mcp_list(self):
        return [tool.to_mcp() for tool in self._tools.values()]

    def to_openai_tools(self, names=None, compact=False):
        """Инструменты в формате OpenAI: все или только `names` (неизвестные имена пропускаем)."""
        if names is None:
            tools = self._tools.values()
        else:
            tools = [self._tools[name] for name in names if name in self._tools]
        return [tool.to_openai(compact=compact) for tool in tools]

    def resolve_name(self, name):
        """Настоящее имя инструмента по тому, как его назвала модель (`functions.load_model`, `Load_Model`…), или None."""
        name = (name or "").strip()
        if name in self._tools:
            return name
        candidate = re.split(r"[./:]", name)[-1].strip().lower()
        candidate = re.sub(r"[\s-]+", "_", candidate)
        return candidate if candidate in self._tools else None

    def unknown_tool_message(self, name):
        close = difflib.get_close_matches((name or "").lower(), list(self._tools), n=3, cutoff=0.6)
        text = "Неизвестный инструмент «%s»." % (name or "")
        if close:
            text += " Возможно, имелся в виду: %s." % ", ".join(close)
        return text + " Вызывай только инструменты из списка tools (точное имя)."

    def call(self, name, arguments, ctx=None):
        """Проверяет аргументы и выполняет инструмент; при ошибках инструмента не бросает исключений."""
        ctx = ctx or ToolContext()
        resolved = self.resolve_name(name)
        if resolved is None:
            return ToolResult(False, None, self.unknown_tool_message(name))
        tool = self._tools[resolved]
        name = resolved
        try:
            arguments = coerce_arguments(parse_arguments(arguments), tool.input_schema)
        except ToolValidationError as exc:
            return ToolResult(False, None, "Ошибка: %s" % exc)
        started = time.time()
        try:
            args = validate(arguments, tool.input_schema)
            if tool.main_thread:
                data = run_in_main_thread(tool.handler, args, ctx, timeout=MAIN_THREAD_TIMEOUT)
            else:
                data = tool.handler(args, ctx)
            result = ToolResult(True, data, format_result(data))
        except ToolError as exc:
            result = ToolResult(False, None, "Ошибка: %s" % exc)
        except Exception as exc:
            log.exception("Tool %s failed", name)
            result = ToolResult(False, None, "Внутренняя ошибка инструмента %s: %s: %s"
                                % (name, type(exc).__name__, exc))
        log.debug("Tool %s finished in %.0f ms (ok=%s)", name, (time.time() - started) * 1000, result.ok)
        return result


# Терпимый разбор аргументов: модели часто присылают их криво

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*(.*?)\s*```$", re.DOTALL)
_LEADING_ZEROS_RE = re.compile(r"(?<![\w.\"])0+(\d)")


def parse_arguments(raw):
    """Аргументы вызова в том виде, как их прислала модель -> значение Python.

    Принимает словарь, пустую строку или null (без аргументов), строку JSON, JSON в блоке
    ``` или с текстом после него, JSON, закодированный дважды, и литералы Python
    ({'a': True}). Если разобрать не вышло — ToolValidationError с подсказкой для модели.
    """
    if raw is None:
        return {}
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text or text in ("null", "None"):
        return {}
    value, error = _loads(text)
    if error is not None:
        fenced = _FENCE_RE.match(text)
        candidates = [fenced.group(1)] if fenced else []
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            candidates.append(text[start:end + 1])
        # числа с ведущими нулями ("x": 00.021) — не JSON, а модели их иногда пишут
        candidates += [_LEADING_ZEROS_RE.sub(r"\1", candidate) for candidate in candidates + [text]]
        for candidate in candidates:
            value, candidate_error = _loads(candidate)
            if candidate_error is None:
                error = None
                break
            try:
                value = ast.literal_eval(candidate)
                error = None
                break
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                continue
    if error is not None:
        raise ToolValidationError(
            "аргументы не являются корректным JSON (%s). Передай JSON-объект без пояснений и markdown, "
            "например {\"key\": 1}. Получено: %s" % (error, _clip(text)))
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        nested, nested_error = _loads(value.strip())      # JSON, закодированный дважды
        if nested_error is None:
            value = nested
    return value


def coerce_arguments(value, schema):
    """Аргументы должны быть объектом; одиночное значение подставляем в единственный параметр."""
    if isinstance(value, dict):
        return value
    properties = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or []
    single = required[0] if len(required) == 1 else (list(properties)[0] if len(properties) == 1 else None)
    if single is not None and value is not None and not isinstance(value, (dict, list)):
        return {single: value}
    if single is not None and isinstance(value, list) and (properties.get(single) or {}).get("type") == "array":
        return {single: value}
    expected = ", ".join(properties) or "без параметров — передай {}"
    raise ToolValidationError("аргументы должны быть JSON-объектом с именованными параметрами (%s), получено: %s"
                              % (expected, _clip(json.dumps(value, ensure_ascii=False, default=str))))


def _loads(text):
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, str(exc)


def _clip(text, limit=200):
    return text if len(text) <= limit else text[:limit] + "…"


# Эти поля validate проверит сам и объяснит в ошибке — тратить на них токены каждый шаг незачем.
_LLM_DROPPED_FIELDS = ("minimum", "maximum", "minItems", "maxItems")


def _compact_schema(schema):
    if isinstance(schema, dict):
        return dict((key, _compact_schema(value)) for key, value in schema.items()
                    if key not in _LLM_DROPPED_FIELDS and
                    not (key == "additionalProperties" and value is False) and
                    not (key == "default" and value is False))
    if isinstance(schema, list):
        return [_compact_schema(item) for item in schema]
    return schema


def format_result(data):
    """Результат обработчика для модели: компактный JSON, слишком длинный обрезаем."""
    if isinstance(data, str):
        text = data
    else:
        text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "… [обрезано, всего %d символов]" % len(text)
    return text


# Минимальная проверка по JSON Schema

_TYPE_NAMES = {
    "string": "строка", "number": "число", "integer": "целое число", "boolean": "логическое значение",
    "object": "объект", "array": "массив", "null": "null",
}


def validate(value, schema, path="arguments"):
    """Проверяет `value` по подмножеству JSON Schema и возвращает приведённое значение.

    Поддерживаются: type (один или список), properties, required, additionalProperties
    (bool), enum, items, minimum, maximum, minItems, maxItems, default. Числа и логические
    значения в строках приводим ("0.2" -> 0.2): модели часто берут числа в кавычки.
    """
    if not schema:
        return value
    if value is None and "default" in schema:
        return schema["default"]

    types = schema.get("type")
    if types is not None:
        type_list = types if isinstance(types, list) else [types]
        value = _coerce_to_types(value, type_list, path)

    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError("%s: недопустимое значение %r, ожидается одно из: %s"
                                  % (path, value, ", ".join(repr(v) for v in schema["enum"])))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolValidationError("%s: значение %s меньше минимума %s" % (path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolValidationError("%s: значение %s больше максимума %s" % (path, value, schema["maximum"]))

    if isinstance(value, dict):
        value = _validate_object(value, schema, path)
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ToolValidationError("%s: нужно не меньше %d элементов" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolValidationError("%s: допускается не больше %d элементов" % (path, schema["maxItems"]))
        item_schema = schema.get("items")
        if item_schema:
            value = [validate(item, item_schema, "%s[%d]" % (path, index)) for index, item in enumerate(value)]
    return value


def _validate_object(value, schema, path):
    properties = schema.get("properties", {})
    missing = [key for key in schema.get("required", []) if key not in value or value[key] is None]
    if missing:
        raise ToolValidationError("%s: не указаны обязательные параметры: %s" % (path, ", ".join(missing)))
    if schema.get("additionalProperties") is False:
        unknown = [key for key in value if key not in properties]
        if unknown:
            raise ToolValidationError("%s: неизвестные параметры: %s (допустимые: %s)"
                                      % (path, ", ".join(unknown), ", ".join(properties) or "нет"))
    result = OrderedDict()
    for key, item in value.items():
        if item is None and key in properties and "null" not in _types_of(properties[key]):
            continue  # null модели шлют вместо пропущенного необязательного параметра
        if key in properties:
            result[key] = validate(item, properties[key], "%s.%s" % (path, key))
        else:
            result[key] = item
    for key, prop_schema in properties.items():
        if key not in result and "default" in prop_schema:
            result[key] = prop_schema["default"]
    return result


def _types_of(schema):
    types = schema.get("type")
    return types if isinstance(types, list) else [types]


def _coerce_to_types(value, type_list, path):
    for type_name in type_list:
        if _matches(value, type_name):
            return value
    # Мягко приводим значения, присланные строкой.
    if isinstance(value, str):
        text = value.strip()
        for type_name in type_list:
            try:
                if type_name == "integer" and _is_int_text(text):
                    return int(text)
                if type_name == "number":
                    return float(text.replace(",", "."))
                if type_name == "boolean" and text.lower() in ("true", "false"):
                    return text.lower() == "true"
            except ValueError:
                continue
    if "integer" in type_list and isinstance(value, float) and value.is_integer():
        return int(value)
    expected = " или ".join(_TYPE_NAMES.get(t, t) for t in type_list)
    raise ToolValidationError("%s: ожидается %s, получено %r" % (path, expected, value))


def _is_int_text(text):
    return text.lstrip("+-").isdigit()


def _matches(value, type_name):
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "null":
        return value is None
    return True
