"""Справочник настроек печати STE Slicer: проверка ключей, синонимы и приведение значений.

Файл данных settings_catalog.json собирается из определений слайсера скриптом
devtools/build_settings_catalog.py (H530 V5.2.4: fdmprinter -> stereotech -> принтер,
плюс определения экструдеров и русские переводы).

Модели любят угадывать ключи ("fill_density" вместо "infill_sparse_density") и присылать
значения текстом ("40%"). Обработчики инструментов через этот модуль приводят такой ввод
к точным ключам и типизированным значениям:

    catalog = get_catalog()
    key, hint = catalog.resolve("fill_density")      # -> ("infill_sparse_density", None)
    value, warning = catalog.coerce(key, "40%")      # -> (40.0, None)

Только стандартная библиотека, Python 3.5+, без импортов слайсера.
"""

import ast
import difflib
import io
import json
import math
import os
import re
import threading
from collections import OrderedDict

CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_catalog.json")


class SettingValueError(ValueError):
    """Ключ или значение настройки принять нельзя. str(error) — сообщение для пользователя и модели."""

    def __init__(self, message, key=None):
        ValueError.__init__(self, message)
        self.key = key


# Синонимы и частые ошибки моделей: термины Cura/PrusaSlicer/Orca/Simplify3D и русские
# названия из интерфейса -> точный ключ. Ключи словаря нормализуются через _norm().
SYNONYMS = {
    # infill
    "infill_sparse_density": [
        "fill_density", "infill", "infill_density", "infill_percent", "infill_percentage",
        "sparse_infill_density", "infill_amount", "density", "fill", "заполнение",
        "плотность заполнения", "процент заполнения", "плотность", "инфилл"],
    "infill_pattern": [
        "fill_pattern", "infill_type", "sparse_infill_pattern", "infill_style", "узор заполнения",
        "шаблон заполнения", "рисунок заполнения", "тип заполнения", "паттерн заполнения"],
    "infill_before_walls": ["заполнение перед стенками"],
    # temperatures
    "material_print_temperature": [
        "temperature", "temp", "nozzle_temp", "nozzle_temperature", "hotend_temp", "hotend_temperature",
        "extruder_temp", "extruder_temperature", "print_temperature", "printing_temperature",
        "print_temp", "температура", "температура сопла", "температура печати",
        "температура экструдера", "температура хотэнда"],
    "material_print_temperature_layer_0": [
        "first_layer_temperature", "initial_layer_temperature", "first_layer_temp",
        "температура первого слоя"],
    "material_bed_temperature": [
        "bed_temp", "bed_temperature", "hot_bed_temperature", "build_plate_temperature",
        "plate_temperature", "heatbed_temperature", "bed", "температура стола",
        "температура платформы", "температура подогреваемого стола"],
    "material_bed_temperature_layer_0": ["first_layer_bed_temperature", "initial_layer_bed_temperature"],
    "material_standby_temperature": ["standby_temperature", "standby_temp", "температура ожидания"],
    "material_flow": ["flow", "flow_rate", "extrusion_multiplier", "поток", "коэффициент потока"],
    "material_diameter": ["filament_diameter", "диаметр прутка", "диаметр филамента"],
    # supports
    "support_enable": [
        "supports", "support", "support_material", "enable_support", "enable_supports", "generate_support",
        "support_enabled", "поддержки", "поддержка", "включить поддержки", "генерация поддержек"],
    "support_type": [
        "support_placement", "support_on_build_plate_only", "support_location", "тип поддержек",
        "размещение поддержек", "расположение поддержек"],
    "support_angle": [
        "support_threshold", "support_material_threshold", "overhang_angle", "support_overhang_angle",
        "support_threshold_angle", "угол поддержек", "угол нависания", "угол поддержки"],
    "support_infill_rate": ["support_density", "support_infill_density", "плотность поддержек"],
    "support_pattern": ["support_material_pattern", "support_style", "шаблон поддержек", "узор поддержек"],
    "support_tree_enable": ["tree_support", "tree_supports", "древовидные поддержки", "поддержки деревом"],
    "support_extruder_nr": ["support_extruder", "support_material_extruder", "support_filament",
                            "экструдер поддержек"],
    "support_interface_enable": ["support_interface", "интерфейс поддержек"],
    "support_enable_cylindrical": ["support_5d", "5d_support", "поддержки 5d"],
    # стенки и оболочка
    "wall_line_count": [
        "perimeters", "perimeter", "walls", "wall_count", "wall_loops", "shells", "outlines",
        "perimeter_count", "number_of_walls", "wall_lines", "стенки", "количество стенок", "периметры",
        "число стенок", "число периметров", "количество периметров", "количество линий стенки"],
    "wall_thickness": ["shell_thickness", "wall_width", "толщина стенки", "толщина стенок",
                       "толщина оболочки"],
    "top_layers": ["top_solid_layers", "top_shell_layers", "top_layer_count", "слои крышки",
                   "верхние слои", "количество верхних слоев"],
    "bottom_layers": ["bottom_solid_layers", "bottom_shell_layers", "bottom_layer_count", "слои дна",
                      "нижние слои", "количество нижних слоев"],
    "top_bottom_thickness": ["solid_thickness", "толщина крышки и дна", "толщина верха и низа"],
    "top_bottom_pattern": ["top_fill_pattern", "solid_infill_pattern", "top_surface_pattern"],
    # speed
    "speed_print": ["print_speed", "speed", "printing_speed", "default_speed", "скорость",
                    "скорость печати"],
    "speed_travel": ["travel_speed", "move_speed", "скорость перемещения", "скорость холостых перемещений"],
    "speed_layer_0": ["first_layer_speed", "initial_layer_speed", "скорость первого слоя"],
    "speed_wall_0": ["outer_wall_speed", "perimeter_speed", "external_perimeter_speed",
                     "скорость внешней стенки"],
    "speed_wall_x": ["inner_wall_speed", "скорость внутренних стенок"],
    "speed_infill": ["infill_speed", "sparse_infill_speed", "скорость заполнения"],
    "speed_topbottom": ["top_bottom_speed", "solid_infill_speed", "скорость крышки и дна"],
    # слои и разрешение
    "layer_height": ["layer", "layer_thickness", "layer_size", "layer_h", "layerheight",
                     "высота слоя", "толщина слоя", "слой"],
    "layer_height_0": ["first_layer_height", "initial_layer_height", "высота первого слоя"],
    "line_width": ["extrusion_width", "line_thickness", "ширина линии", "ширина экструзии"],
    "machine_nozzle_size": ["nozzle_diameter", "nozzle", "nozzle_size", "диаметр сопла"],
    # cooling
    "cool_fan_speed": ["fan", "fan_speed", "cooling", "cooling_fan_speed", "part_cooling", "обдув",
                       "вентилятор", "скорость вентилятора", "скорость обдува"],
    "cool_fan_enabled": ["enable_cooling", "fan_enabled", "cooling_enabled", "enable_fan",
                         "включить обдув", "охлаждение"],
    "cool_min_layer_time": ["min_layer_time", "minimal_layer_time", "slowdown_below_layer_time",
                            "минимальное время слоя"],
    # retraction
    "retraction_amount": ["retraction", "retract", "retract_length", "retraction_length",
                          "retraction_distance", "откат", "ретракт", "длина ретракта", "длина отката"],
    "retraction_speed": ["retract_speed", "retraction_feedrate", "скорость ретракта", "скорость отката"],
    "retraction_enable": ["enable_retraction", "retraction_enabled", "use_retraction", "включить ретракт",
                          "включить откат"],
    "retraction_hop_enabled": ["z_hop", "zhop", "retract_lift", "lift_z", "z_lift", "подъем z",
                               "поднятие по z"],
    # adhesion
    "adhesion_type": ["adhesion", "bed_adhesion", "platform_adhesion", "build_plate_adhesion",
                      "plate_adhesion", "тип прилипания", "адгезия", "прилипание", "прилипание к столу",
                      "тип адгезии"],
    "brim_width": ["brim_size", "brim_distance", "ширина каймы", "ширина брима"],
    "skirt_line_count": ["skirts", "skirt_loops", "skirt_count", "skirt_lines", "количество линий юбки"],
    "cylindrical_raft_type": ["raft_type_5d", "cylindrical_raft", "5d_raft", "5d подложка",
                              "тип 5d подложки", "цилиндрическая подложка"],
    # misc
    "z_seam_type": ["seam", "seam_position", "z_seam", "seam_type", "шов", "положение шва"],
    "ironing_enabled": ["ironing", "enable_ironing", "разглаживание", "глажка", "утюжка"],
    "magic_spiralize": ["vase", "vase_mode", "spiral_vase", "spiralize", "spiral_mode", "режим вазы", "ваза"],
    "xy_offset": ["horizontal_expansion", "xy_compensation", "xy_size_compensation",
                  "горизонтальное расширение", "компенсация xy"],
    "print_sequence": ["sequence", "one_at_a_time", "complete_objects", "последовательность печати"],
    # 5D
    "printing_mode": ["print_mode", "mode", "printing_mode_type", "режим", "режим печати", "mode_5d"],
    "cylindrical_mode_base_diameter": ["base_diameter", "core_diameter", "диаметр основания",
                                       "диаметр сердечника"],
    "non_printing_base_diameter": ["mandrel_diameter", "непечатаемый диаметр", "диаметр оснастки"],
    "cylindrical_layer_height": ["5d_layer_height", "cylindrical_layer", "высота слоя 5d"],
}

# Дополнительные написания значений перечислений (ключ варианта и подписи на английском/русском тоже работают).
ENUM_ALIASES = {
    "printing_mode": {
        "3d": "classic", "3д": "classic", "classic 3d": "classic", "классический": "classic",
        "классика": "classic", "обычный": "classic", "плоский": "classic", "flat": "classic",
        "5d": "cylindrical_full", "5д": "cylindrical_full", "5d full": "cylindrical_full",
        "5d spiral full": "cylindrical_full", "full": "cylindrical_full", "полный 5d": "cylindrical_full",
        "spiral": "cylindrical", "5d spiral": "cylindrical", "5d спираль": "cylindrical",
        "спираль": "cylindrical", "спиральный": "cylindrical",
    },
    "adhesion_type": {
        "без": "none", "нет": "none", "off": "none", "no": "none", "false": "none", "отсутствует": "none",
        "каемка": "brim", "брим": "brim", "рафт": "raft", "плот": "raft", "скирт": "skirt",
    },
    "support_type": {
        "везде": "everywhere", "all": "everywhere", "anywhere": "everywhere",
        "от стола": "buildplate", "только от стола": "buildplate", "build plate": "buildplate",
        "build plate only": "buildplate", "buildplate only": "buildplate", "touching build plate": "buildplate",
        "on build plate only": "buildplate", "стол": "buildplate", "touching_buildplate": "buildplate",
    },
    "infill_pattern": {
        "гироид": "gyroid", "гироидный": "gyroid", "rectilinear": "lines", "linear": "lines",
        "line": "lines", "линейный": "lines", "сеточный": "grid", "rectangular": "grid",
        "концентрический": "concentric", "треугольный": "triangles", "triangle": "triangles",
        "кубический": "cubic", "куб": "cubic", "octet": "tetrahedral", "октет": "tetrahedral",
        "зигзаг": "zigzag", "zig zag": "zigzag", "крест": "cross", "honeycomb": "trihexagon",
    },
    "support_pattern": {"зигзаг": "zigzag", "zig zag": "zigzag", "линии": "lines", "сетка": "grid"},
    "top_bottom_pattern": {"зигзаг": "zigzag", "zig zag": "zigzag", "rectilinear": "lines"},
    "z_seam_type": {"aligned": "back", "user": "back", "rear": "back", "сзади": "back",
                    "nearest": "shortest", "sharpest corner": "sharpest_corner"},
}

# «Ключи», которые на самом деле значения другой настройки или вовсе не настройки.
NOT_A_SETTING = {
    "quality": "Профиль качества выбирается инструментом select_profile (quality_type), а не настройкой; "
               "толщина слоя — layer_height.",
    "profile": "Профиль выбирается инструментом select_profile, а не настройкой.",
    "качество": "Профиль качества выбирается инструментом select_profile; высота слоя — layer_height.",
    "профиль": "Профиль выбирается инструментом select_profile, а не настройкой.",
    "material": "Материал меняется инструментом set_material (см. list_materials), а не настройкой.",
    "материал": "Материал меняется инструментом set_material (см. list_materials), а не настройкой.",
    "filament": "Материал меняется инструментом set_material (см. list_materials), а не настройкой.",
}

# Перечисления, варианты которых модель может прислать как ключ ("brim": true).
_VALUE_HINT_SETTINGS = ("adhesion_type", "printing_mode", "infill_pattern", "support_type",
                        "cylindrical_raft_type", "z_seam_type")

_TRUE_WORDS = ("true", "1", "yes", "y", "on", "enable", "enabled", "да", "вкл", "включить", "включено",
               "включен", "истина", "+")
_FALSE_WORDS = ("false", "0", "no", "n", "off", "disable", "disabled", "нет", "выкл", "выключить",
                "выключено", "выключен", "ложь", "-", "none")

_UNIT_SUFFIX_RE = re.compile(
    r"\s*(%|mm/s²|mm/s\^?2|mm/s|мм/с|mm³|mm\^?3|mm|мм|°c|°с|°|c|с|deg|градус(ов|а)?|s|sec|сек|"
    r"секунд[аы]?|ms|мс|μm|um|мкм)\s*$", re.IGNORECASE)

_NUMERIC_TYPES = ("float", "int", "extruder", "optional_extruder")


def _norm(text):
    """Нижний регистр, ё->е, единые разделители: 'Fill-Density ' -> 'fill_density'."""
    text = str(text).strip().lower().replace("ё", "е")
    text = re.sub(r"[\s\-./]+", "_", text)
    return text.strip("_")


def _norm_label(text):
    return " ".join(str(text).strip().lower().replace("ё", "е").replace("_", " ").split())


def _stem(token):
    if len(token) >= 7:
        return token[:-2]
    if len(token) >= 5:
        return token[:-1]
    return token


def _format_number(value):
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return str(value)


class SettingsCatalog(object):
    def __init__(self, path=CATALOG_FILE):
        with io.open(path, encoding="utf-8") as handle:
            data = json.load(handle, object_pairs_hook=OrderedDict)  # в 3.5 иначе потеряем порядок определений
        self.machine = data.get("machine")
        self.machine_name = data.get("machine_name")
        self.categories = data.get("categories") or {}
        self._settings = data.get("settings") or {}
        self._important = list(data.get("important") or [])
        for key, entry in self._settings.items():
            entry["key"] = key
        self._build_indexes()

    # Индексы
    def _build_indexes(self):
        self._synonyms = {}      # нормализованный синоним -> точный ключ
        for key, aliases in SYNONYMS.items():
            if key not in self._settings:
                continue
            for alias in aliases:
                self._synonyms.setdefault(_norm(alias), key)
        self._labels = {}        # нормализованная подпись (en/ru) -> ключ (побеждает первая: родители раньше детей)
        for key, entry in self._settings.items():
            for label in (entry.get("label"), entry.get("label_ru")):
                if label:
                    self._labels.setdefault(_norm(label), key)
        self._value_hints = {}   # нормализованное имя варианта -> [(настройка, вариант)]
        for key in _VALUE_HINT_SETTINGS:
            entry = self._settings.get(key)
            if not entry:
                continue
            names = {}
            for option in entry.get("options") or []:
                for name in option:
                    if name:
                        names[_norm(name)] = option[0]
            for alias, option_key in ENUM_ALIASES.get(key, {}).items():
                names.setdefault(_norm(alias), option_key)
            for name, option_key in names.items():
                if name in ("none", "нет", "no", "off", "false", "без"):
                    continue
                self._value_hints.setdefault(name, []).append((key, option_key))
        # строки для нечёткого поиска -> ключ
        self._fuzzy = {}
        for key, entry in self._settings.items():
            self._fuzzy.setdefault(key, key)
            for label in (entry.get("label"), entry.get("label_ru")):
                if label:
                    self._fuzzy.setdefault(_norm(label), key)
        for alias, key in self._synonyms.items():
            self._fuzzy.setdefault(alias, key)
        self._search_text = {}
        reverse = {}
        for alias, key in self._synonyms.items():
            reverse.setdefault(key, []).append(alias.replace("_", " "))
        for key, entry in self._settings.items():
            parts = [key.replace("_", " "), entry.get("label") or "", entry.get("label_ru") or ""]
            parts.extend(reverse.get(key, []))
            self._search_text[key] = (
                _norm_label(" ".join(parts)),
                _norm_label(" ".join([entry.get("desc") or "", entry.get("desc_ru") or ""])),
                reverse.get(key, []),
            )

    # Поиск
    def __contains__(self, key):
        return key in self._settings

    def __len__(self):
        return len(self._settings)

    def keys(self):
        return list(self._settings.keys())

    def get(self, key):
        """Полная запись справочника по точному ключу или None."""
        return self._settings.get(key)

    def important_keys(self):
        """Ключи, которые агент меняет чаще всего (для системного промпта и описаний инструментов)."""
        return list(self._important)

    def label(self, key, russian=True):
        entry = self._settings.get(key) or {}
        if russian and entry.get("label_ru"):
            return entry["label_ru"]
        return entry.get("label") or key

    def describe(self, key):
        """Описание в одну строку: 'infill_sparse_density («Плотность заполнения», float, %)'."""
        entry = self._settings.get(key)
        if not entry:
            return key
        details = [entry.get("type")]
        if entry.get("unit"):
            details.append(entry["unit"])
        if entry.get("type") == "enum":
            details.append("варианты: " + ", ".join(option[0] for option in entry.get("options") or []))
        return "%s («%s», %s)" % (key, self.label(key), ", ".join(d for d in details if d))

    def resolve(self, key):
        """Сопоставляет возможно неверный ключ с точным.

        Возвращает (точный_ключ, None) или (None, подсказка) с 1–5 кандидатами.
        """
        if key is None or not str(key).strip():
            return None, "Пустой ключ настройки. Используй search_settings, чтобы найти нужный ключ."
        raw = str(key).strip()
        if raw in self._settings:
            return raw, None
        normalized = _norm(raw)
        if normalized in self._settings:
            return normalized, None
        if normalized in self._synonyms:
            return self._synonyms[normalized], None
        if normalized in self._labels:
            return self._labels[normalized], None
        if normalized in NOT_A_SETTING:
            return None, "«%s» — не настройка. %s" % (raw, NOT_A_SETTING[normalized])
        hints = self._value_hints.get(normalized)
        if hints:
            parts = ["%s=\"%s\" (%s)" % (setting, option, self.label(setting)) for setting, option in hints[:3]]
            return None, ("«%s» — это значение, а не ключ настройки. Используй %s." % (raw, " или ".join(parts)))
        candidates = self._candidates(raw, normalized)
        if candidates:
            return None, ("Неизвестная настройка «%s». Возможно, имелось в виду: %s. Используй точный ключ "
                          "(или search_settings)." % (raw, "; ".join(self.describe(k) for k in candidates)))
        return None, ("Неизвестная настройка «%s». Найди правильный ключ через search_settings." % raw)

    def _candidates(self, raw, normalized, limit=5):
        result = []

        def add(key):
            if key and key not in result:
                result.append(key)

        matches = difflib.get_close_matches(normalized, list(self._fuzzy.keys()), n=12, cutoff=0.6)
        for match in matches:
            add(self._fuzzy[match])
        if len(result) < limit:
            for item in self.search(raw.replace("_", " "), limit=limit):
                add(item["key"])
        # среди похожих кандидатов сначала важные настройки, в остальном порядок difflib
        order = dict((k, index) for index, k in enumerate(result[:8]))
        ranked = sorted(order, key=lambda k: (0 if self._settings[k].get("important") else 1, order[k]))
        return ranked[:limit]

    # Значения
    def coerce(self, key, value, context=None):
        """Приводит значение от модели к типу настройки и проверяет пределы.

        Возвращает (значение, предупреждение_или_None); при ошибке — SettingValueError с
        объяснением. `context` (необязательно) — словарь или функция ключ -> текущее значение,
        по нему считаются пределы, заданные формулами; формулы, которые не посчитать, пропускаем.
        """
        entry = self._settings.get(key)
        if entry is None:
            canonical, hint = self.resolve(key)
            if canonical is None:
                raise SettingValueError(hint, key)
            key, entry = canonical, self._settings[canonical]
        setting_type = entry.get("type")
        if value is None:
            raise SettingValueError("Для %s не указано значение." % self.describe(key), key)
        warnings = []
        if setting_type == "bool":
            result = self._to_bool(key, value)
        elif setting_type == "enum":
            result = self._to_enum(key, entry, value)
        elif setting_type in _NUMERIC_TYPES:
            result = self._to_number(key, entry, value, warnings)
            self._check_limits(key, entry, result, warnings, context)
        elif setting_type in ("[int]", "[float]"):
            result = self._to_list(key, setting_type, value)
        elif setting_type in ("polygon", "polygons"):
            result = self._to_polygon(key, value)
        else:  # str и всё неизвестное
            if isinstance(value, (dict, list)):
                raise SettingValueError("%s ожидает строку." % self.describe(key), key)
            result = value if isinstance(value, str) else str(value)
        return result, ("; ".join(warnings) if warnings else None)

    def _to_bool(self, key, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if value in (0, 1):
                return bool(value)
        elif isinstance(value, str):
            word = value.strip().lower().replace("ё", "е")
            if word in _TRUE_WORDS:
                return True
            if word in _FALSE_WORDS or word == "":
                return False
        raise SettingValueError("%s — логическая настройка: нужно true или false, получено %r."
                                % (self.describe(key), value), key)

    def _to_enum(self, key, entry, value):
        options = entry.get("options") or []
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        wanted = _norm_label(text)
        for option in options:
            if option[0] == text:
                return option[0]
        for option in options:
            if any(name and _norm_label(name) == wanted for name in option):
                return option[0]
        aliases = ENUM_ALIASES.get(key, {})
        for alias, option_key in aliases.items():
            if _norm_label(alias) == wanted:
                return option_key
        names = {}
        for option in options:
            for name in option:
                if name:
                    names[_norm_label(name)] = option[0]
        close = difflib.get_close_matches(wanted, list(names.keys()), n=3, cutoff=0.75)
        found = []
        for name in close:
            if names[name] not in found:
                found.append(names[name])
        if len(found) == 1:
            return found[0]
        listing = ", ".join("%s (%s)" % (o[0], o[2] or o[1]) for o in options)
        raise SettingValueError("Недопустимое значение %r для %s. Допустимые значения: %s."
                                % (value, self.describe(key).split(" (")[0], listing), key)

    def _to_number(self, key, entry, value, warnings):
        setting_type = entry.get("type")
        original = value
        had_percent = False
        if isinstance(value, bool):
            raise SettingValueError("%s — число, а получено логическое значение %r." % (self.describe(key), value),
                                    key)
        if isinstance(value, str):
            text = value.strip().replace("−", "-").replace(" ", "").replace(" ", "")
            had_percent = "%" in text
            while True:
                stripped = _UNIT_SUFFIX_RE.sub("", text)
                if stripped == text:
                    break
                text = stripped
            text = text.replace(",", ".")
            if setting_type in ("extruder", "optional_extruder"):
                lowered = text.lower()
                words = {"main": "0", "основной": "0", "левый": "0", "first": "0", "первый": "0",
                         "second": "1", "второй": "1", "правый": "1", "вспомогательный": "1"}
                text = words.get(lowered, text)
            try:
                value = float(text)
            except ValueError:
                raise SettingValueError("%s ожидает число, получено %r." % (self.describe(key), original), key)
        elif not isinstance(value, (int, float)):
            raise SettingValueError("%s ожидает число, получено %r." % (self.describe(key), original), key)
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise SettingValueError("%s: недопустимое число %r." % (self.describe(key), original), key)
        if setting_type == "float":
            if entry.get("unit") == "%" and 0 < value < 1 and not had_percent and isinstance(original, float):
                warnings.append("%s задаётся в процентах (0-100): %s означает %s%%, а не %s%%. "
                                "Проверь, не имелось ли в виду %s."
                                % (key, _format_number(value), _format_number(value),
                                   _format_number(round(value * 100, 4)), _format_number(round(value * 100, 4))))
            return value
        if value != int(value):
            raise SettingValueError("%s — целое число, получено %r." % (self.describe(key), original), key)
        return int(value)

    def _limit(self, raw, context):
        if raw is None:
            return None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        if context is None or not isinstance(raw, str):
            return None
        result = _eval_formula(raw, context)
        if isinstance(result, (int, float)) and not isinstance(result, bool):
            return float(result)
        return None

    def _check_limits(self, key, entry, value, warnings, context):
        unit = entry.get("unit") or ""
        suffix = (" " + unit) if unit else ""
        minimum = self._limit(entry.get("min"), context)
        maximum = self._limit(entry.get("max"), context)
        if entry.get("type") == "extruder":
            minimum = 0 if minimum is None else minimum
        if minimum is not None and value < minimum:
            raise SettingValueError("%s: значение %s меньше минимально допустимого %s%s."
                                    % (self.describe(key), _format_number(value), _format_number(minimum), suffix),
                                    key)
        if maximum is not None and value > maximum:
            raise SettingValueError("%s: значение %s больше максимально допустимого %s%s."
                                    % (self.describe(key), _format_number(value), _format_number(maximum), suffix),
                                    key)
        min_warn = self._limit(entry.get("min_warn"), context)
        max_warn = self._limit(entry.get("max_warn"), context)
        if min_warn is not None and value < min_warn:
            warnings.append("%s = %s%s ниже рекомендуемого минимума %s%s (слайсер покажет предупреждение)."
                            % (key, _format_number(value), suffix, _format_number(min_warn), suffix))
        elif max_warn is not None and value > max_warn:
            warnings.append("%s = %s%s выше рекомендуемого максимума %s%s (слайсер покажет предупреждение)."
                            % (key, _format_number(value), suffix, _format_number(max_warn), suffix))

    def _to_list(self, key, setting_type, value):
        items = value
        if isinstance(value, str):
            text = value.strip()
            if not text.startswith("["):
                text = "[" + text + "]"
            try:
                items = json.loads(text.replace(";", ","))
            except ValueError:
                raise SettingValueError("%s ожидает список чисел, например [0, 90]; получено %r."
                                        % (self.describe(key), value), key)
        if not isinstance(items, (list, tuple)):
            items = [items]
        result = []
        for item in items:
            try:
                number = float(item)
            except (TypeError, ValueError):
                raise SettingValueError("%s ожидает список чисел, получено %r." % (self.describe(key), value), key)
            if setting_type == "[int]":
                if number != int(number):
                    raise SettingValueError("%s ожидает список целых чисел, получено %r."
                                            % (self.describe(key), value), key)
                result.append(str(int(number)))
            else:
                result.append(_format_number(number))
        # Cura хранит настройки-списки их строковым представлением
        return "[" + ", ".join(result) + "]" if result else "[ ]"

    def _to_polygon(self, key, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise SettingValueError("%s ожидает список точек [[x, y], ...]." % self.describe(key), key)
        if not isinstance(value, list):
            raise SettingValueError("%s ожидает список точек [[x, y], ...]." % self.describe(key), key)
        return value

    # Поиск по тексту
    def search(self, query, limit=10, category=None):
        """Ищет настройки по ключу, подписи на английском/русском, синонимам или описанию.

        Порядок: точное совпадение -> начало -> подстрока -> описание; внутри уровня важные
        настройки идут первыми. Возвращает короткие словари.
        """
        query = (query or "").strip()
        if not query:
            return []
        q_key = _norm(query)
        q_text = _norm_label(query)
        tokens = [t for t in re.split(r"[^\w%°]+", q_text) if t]
        stems = [_stem(t) for t in tokens]
        # "кайма", "gyroid", "5D": имена вариантов перечислений указывают на саму настройку
        value_hint_keys = set(setting for setting, _ in self._value_hints.get(q_key, []))
        scored = []
        for key, entry in self._settings.items():
            if category and entry.get("category") != category:
                continue
            title_text, desc_text, aliases = self._search_text[key]
            labels = [_norm_label(entry.get("label") or ""), _norm_label(entry.get("label_ru") or "")]
            score = 0
            if key == q_key:
                score = 100
            elif self._synonyms.get(q_key) == key or q_text in labels:
                score = 90
            elif key in value_hint_keys:
                score = 80
            elif key.startswith(q_key) or any(label.startswith(q_text) for label in labels if label):
                score = 70
            elif any(alias.startswith(q_text) for alias in aliases):
                score = 65
            elif q_key in key or any(q_text in label for label in labels if label):
                score = 55
            elif stems and all(stem in title_text for stem in stems):
                score = 45
            elif stems and all(stem in title_text or stem in desc_text for stem in stems):
                score = 25
            elif stems and len(stems) > 1:
                hits = sum(1 for stem in stems if stem in title_text or stem in desc_text)
                if hits:
                    score = 5 + 10 * hits // len(stems)
            if not score:
                continue
            if entry.get("important"):
                score += 8
            if not entry.get("parent"):
                score += 1
            if entry.get("category") == "machine_settings" or entry.get("category") == "blackmagic":
                score -= 2
            if "alpha" in (entry.get("label") or "").lower():
                score -= 2
            scored.append((-score, len(key), key))
        scored.sort()
        return [self.brief(key) for _, _, key in scored[:max(1, int(limit))]]

    def brief(self, key):
        entry = self._settings[key]
        item = {
            "key": key,
            "label": entry.get("label"),
            "label_ru": entry.get("label_ru") or entry.get("label"),
            "type": entry.get("type"),
            "unit": entry.get("unit") or "",
            "default": entry.get("default"),
            "category": entry.get("category"),
            "important": bool(entry.get("important")),
        }
        if "default_3d" in entry:
            item["default_3d"] = entry["default_3d"]
        if entry.get("type") == "enum":
            item["options"] = [option[0] for option in entry.get("options") or []]
        if entry.get("mode_5d"):
            item["mode_5d"] = True
        return item


# Вычисление формул (пределы, заданные выражениями Cura)

_SAFE_FUNCTIONS = {
    "math": math, "min": min, "max": max, "round": round, "sum": sum, "any": any, "all": all,
    "len": len, "int": int, "float": float, "abs": abs, "bool": bool, "str": str,
}


def _eval_formula(expr, context):
    """Вычисляет выражение настройки Cura по `context` (словарь или функция). None, если не вышло."""
    def lookup(name):
        value = context(name) if callable(context) else context.get(name)
        if value is None:
            raise KeyError(name)
        return value

    try:
        tree = ast.parse(expr.strip(), mode="eval")
        namespace = dict(_SAFE_FUNCTIONS)
        namespace.update({
            "resolveOrValue": lookup,
            "extruderValue": lambda extruder, name: lookup(name),
            "extruderValues": lambda name: [lookup(name)],
            "defaultExtruderPosition": lambda: "0",
        })
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                return None
            if isinstance(node, ast.Name) and node.id not in namespace and node.id not in ("True", "False", "None"):
                namespace[node.id] = lookup(node.id)
        namespace["__builtins__"] = {}
        return eval(compile(tree, "<setting>", "eval"), namespace)
    except Exception:  # неизвестные имена, ошибки типов — этот предел просто не проверяем
        return None


_catalog = None
_catalog_lock = threading.Lock()


def get_catalog():
    """Единственный SettingsCatalog, загружается при первом обращении."""
    global _catalog
    if _catalog is None:
        with _catalog_lock:
            if _catalog is None:
                _catalog = SettingsCatalog()
    return _catalog
