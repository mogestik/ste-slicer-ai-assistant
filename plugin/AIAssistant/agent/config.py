"""Настройки плагина: схема, хранилища и маскирование секретов.

В слайсере значения лежат в Preferences приложения в группе "ai_assistant/", то есть ключ
API хранится в конфиге слайсера, а не в исходниках. Стенд хранит их в JSON-файле.
"""

import json
import os
import threading
from collections import OrderedDict

from . import log
from .main_thread import run_in_main_thread

PREFERENCE_GROUP = "ai_assistant"

# ключ -> (значение по умолчанию, тип). По типу приводим значения из preferences.cfg
# (там всё хранится строками) и из формы настроек в панели.
SETTINGS_SCHEMA = OrderedDict([
    ("base_url", ("https://api.timeweb.ai/v1", str)),
    ("api_key", ("", str)),
    ("model", ("dashscope/qwen3.8-flash", str)),
    ("temperature", (0.2, float)),      # низкая — маленькая модель стабильнее вызывает инструменты
    ("max_tokens", (0, int)),            # 0 — max_tokens не передаём
    ("max_iterations", (30, int)),       # шагов агента на одно сообщение (обычный сценарий ~17 вызовов)
    ("request_timeout", (180, int)),     # секунды на одну операцию с сокетом
    ("stream", (True, bool)),
    ("parallel_tool_calls", (False, bool)),  # если шлюз не принимает параметр, отключаем автоматически
    ("llm_tools", ("dynamic", str)),      # dynamic — основные инструменты + каталог enable_tools; all — все сразу
    ("max_retries", (3, int)),           # повторы запроса к LLM при 429/5xx/сети, с растущей паузой
    ("verify_ssl", (True, bool)),
    ("image_max_side", (1280, int)),     # px, фото уменьшаем перед отправкой
    ("show_reasoning", (True, bool)),
    ("port", (8765, int)),               # желаемый порт; если занят, берём свободный
])

SECRET_KEYS = ("api_key",)

_CHOICES = {"llm_tools": ("dynamic", "all")}

_LIMITS = {
    "temperature": (0.0, 2.0),
    "max_tokens": (0, 200000),
    "max_iterations": (1, 100),
    "request_timeout": (10, 1800),
    "image_max_side": (256, 4096),
    "max_retries": (0, 10),
    "port": (0, 65535),
}


class SettingsError(ValueError):
    pass


def mask_secret(value):
    """Безопасный для показа вид секрета, например 'sk-…a1b2'."""
    if not value:
        return ""
    if len(value) <= 10:
        return "••••••"
    return value[:3] + "…" + value[-4:]


def coerce_value(key, value):
    """Приводит `value` к типу настройки `key` и проверяет допустимый диапазон."""
    if key not in SETTINGS_SCHEMA:
        raise SettingsError("Неизвестная настройка: %s" % key)
    default, value_type = SETTINGS_SCHEMA[key]
    if value is None:
        return default
    try:
        if value_type is bool:
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1", "yes", "on"):
                    result = True
                elif lowered in ("false", "0", "no", "off", ""):
                    result = False
                else:
                    raise ValueError(value)
            else:
                result = bool(value)
        elif value_type is int:
            result = int(float(value))
        elif value_type is float:
            result = float(value)
        else:
            result = str(value).strip()
    except (TypeError, ValueError):
        raise SettingsError("Некорректное значение для %s: %r" % (key, value))
    if key in _LIMITS:
        low, high = _LIMITS[key]
        if not (low <= result <= high):
            raise SettingsError("%s должно быть в диапазоне %s…%s" % (key, low, high))
    if key in _CHOICES and result not in _CHOICES[key]:
        raise SettingsError("%s: допустимые значения — %s" % (key, ", ".join(_CHOICES[key])))
    if key == "base_url" and result:
        if not (result.startswith("http://") or result.startswith("https://")):
            raise SettingsError("URL должен начинаться с http:// или https://")
        result = result.rstrip("/")
    return result


class _MemoryBackend(object):
    """Хранение в памяти (для тестов)."""

    def __init__(self):
        self._values = {}

    def load(self, key, default):
        return self._values.get(key, default)

    def save(self, values):
        self._values.update(values)


class JsonFileBackend(object):
    """Настройки в JSON-файле (стенд)."""

    def __init__(self, path):
        self._path = path
        self._values = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    self._values = json.load(handle)
            except (OSError, ValueError):
                log.exception("Cannot read settings file %s", path)

    def load(self, key, default):
        return self._values.get(key, default)

    def save(self, values):
        self._values.update(values)
        directory = os.path.dirname(self._path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(self._values, handle, ensure_ascii=False, indent=2)


class PreferencesBackend(object):
    """Настройки в Preferences приложения Uranium."""

    def __init__(self, application):
        self._app = application
        self._preferences = application.getPreferences()
        for key, (default, _type) in SETTINGS_SCHEMA.items():
            self._preferences.addPreference(self._pref_key(key), default)

    @staticmethod
    def _pref_key(key):
        return "%s/%s" % (PREFERENCE_GROUP, key)

    def load(self, key, default):
        value = self._preferences.getValue(self._pref_key(key))
        return default if value is None else value

    def save(self, values):
        def apply():
            for key, value in values.items():
                self._preferences.setValue(self._pref_key(key), value)
            # Сохраняем сразу, а не при выходе из приложения.
            if hasattr(self._app, "savePreferences"):
                self._app.savePreferences()
        run_in_main_thread(apply)


class SettingsStore(object):
    """Типизированный потокобезопасный доступ к настройкам плагина."""

    def __init__(self, backend=None):
        self._backend = backend or _MemoryBackend()
        self._lock = threading.Lock()

    def get(self, key):
        default, _type = SETTINGS_SCHEMA[key]
        raw = self._backend.load(key, default)
        try:
            return coerce_value(key, raw)
        except SettingsError:
            log.warning("Invalid stored value for %s, using default", key)
            return default

    def as_dict(self):
        return OrderedDict((key, self.get(key)) for key in SETTINGS_SCHEMA)

    def public_dict(self):
        """Настройки для панели: секреты замаскированы, отдельно сообщаем, задан ли ключ."""
        result = self.as_dict()
        for key in SECRET_KEYS:
            result[key + "_set"] = bool(result[key])
            result[key] = mask_secret(result[key])
        return result

    def resolve_update(self, payload):
        """Проверяет частичное обновление из панели; возвращает {ключ: типизированное значение}.

        Если вместо секрета пришла его маска, значит пользователь его не менял — пропускаем.
        """
        if not isinstance(payload, dict):
            raise SettingsError("Ожидался JSON-объект с настройками")
        updates = OrderedDict()
        for key, value in payload.items():
            if key.endswith("_set"):
                continue
            if key in SECRET_KEYS and value == mask_secret(self.get(key)):
                continue
            updates[key] = coerce_value(key, value)
        return updates

    def update(self, payload):
        updates = self.resolve_update(payload)
        if updates:
            with self._lock:
                self._backend.save(updates)
        return updates

