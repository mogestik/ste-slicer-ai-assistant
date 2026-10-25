"""Приведение и проверка значений настроек по описанию настройки.

Описание (`info`) адаптер читает из живого стека контейнеров: {"type", "options":
[[ключ, подпись], ...], "minimum_value", "maximum_value", "minimum_value_warning",
"maximum_value_warning"}, пределы уже посчитаны в числа (или None). Чистый Python,
без импортов слайсера.
"""

import ast
import json
import re

_TRUE = {"true", "1", "yes", "on", "да", "вкл", "включить", "включено", "y", "истина"}
_FALSE = {"false", "0", "no", "off", "нет", "выкл", "выключить", "выключено", "n", "ложь", "none", ""}
_NUMBER_RE = re.compile(r"^[-+]?(\d+([.,]\d*)?|[.,]\d+)([eE][-+]?\d+)?")
NUMERIC_TYPES = ("float", "int")


class ValueProblem(ValueError):
    """Значение не подходит для этой настройки (текст по-русски)."""


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower().replace("ё", "е")
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    raise ValueProblem("ожидается логическое значение true/false, получено %r" % (value,))


def parse_number(value):
    """Число из int/float или из текста вроде '0,2', '40%', '0.2 мм'."""
    if isinstance(value, bool):
        raise ValueProblem("ожидается число, получено логическое значение %r" % (value,))
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace("−", "-")
        match = _NUMBER_RE.match(text)
        if match:
            number_text = match.group(0).replace(",", ".")
            rest = text[match.end():].strip().lower()
            if rest in ("", "%", "мм", "mm", "°c", "°", "c", "с", "s", "мм/с", "mm/s", "г", "g", "deg", "град"):
                number = float(number_text)
                return int(number) if number.is_integer() and "." not in number_text and "e" not in number_text.lower() else number
    raise ValueProblem("ожидается число, получено %r" % (value,))


def option_pairs(options):
    """Варианты перечисления (словарь ключ->подпись или список [ключ, подпись, ...]) -> [(ключ, подпись)]."""
    if not options:
        return []
    if isinstance(options, dict):
        return [(str(k), str(v)) for k, v in options.items()]
    pairs = []
    for item in options:
        if isinstance(item, (list, tuple)):
            pairs.append((str(item[0]), str(item[1]) if len(item) > 1 else str(item[0])))
        else:
            pairs.append((str(item), str(item)))
    return pairs


def coerce_value(value, info):
    """Возвращает (значение нужного типа, [предупреждения]) или бросает ValueProblem."""
    setting_type = info.get("type") or "str"
    warnings = []
    if value is None:
        raise ValueProblem("не указано значение")
    if setting_type == "bool":
        return parse_bool(value), warnings
    if setting_type == "enum":
        return _coerce_enum(value, info), warnings
    if setting_type in NUMERIC_TYPES:
        number = parse_number(value)
        if setting_type == "int":
            if isinstance(number, float):
                if not number.is_integer():
                    rounded = int(round(number))
                    warnings.append("значение %s округлено до целого %d" % (_fmt(number), rounded))
                    number = rounded
                else:
                    number = int(number)
        else:
            number = float(number)
        warnings.extend(check_limits(number, info))
        return number, warnings
    if setting_type in ("extruder", "optional_extruder"):
        number = parse_number(value)
        if not float(number).is_integer():
            raise ValueProblem("номер экструдера должен быть целым числом")
        number = int(number)
        count = info.get("extruder_count")
        if number < -1 or (number == -1 and setting_type != "optional_extruder"):
            raise ValueProblem("недопустимый номер экструдера %d" % number)
        if count is not None and number >= int(count):
            raise ValueProblem("у принтера только %d экструдер(а), допустимо 0..%d" % (count, int(count) - 1))
        return str(number), warnings
    if setting_type in ("[int]", "[float]"):
        return _coerce_list(value, setting_type), warnings
    if setting_type in ("polygon", "polygons"):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueProblem("ожидается список точек [[x, y], ...]")
        if not isinstance(value, list):
            raise ValueProblem("ожидается список точек [[x, y], ...]")
        return json.dumps(value), warnings
    if isinstance(value, (dict, list)):
        raise ValueProblem("ожидается строка")
    return value if isinstance(value, str) else str(value), warnings


def _coerce_enum(value, info):
    pairs = option_pairs(info.get("options"))
    text = ("true" if value else "false") if isinstance(value, bool) else str(value).strip()
    for key, _label in pairs:
        if key == text:
            return key
    folded = text.lower().replace("ё", "е")
    for key, label in pairs:
        if key.lower() == folded or label.lower().replace("ё", "е") == folded:
            return key
    for key, label in pairs:
        if key.lower().replace("_", " ") == folded.replace("_", " "):
            return key
    listing = ", ".join("%s (%s)" % (key, label) if label != key else key for key, label in pairs)
    raise ValueProblem("недопустимое значение %r, допустимые: %s" % (value, listing))


def _coerce_list(value, setting_type):
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "[]"
        if not text.startswith("["):
            text = "[" + text + "]"
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            raise ValueProblem("ожидается список чисел, например [45, 135]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueProblem("ожидается список чисел, например [45, 135]")
    items = []
    for item in value:
        number = parse_number(item)
        items.append(int(round(number)) if setting_type == "[int]" else float(number))
    return "[%s]" % ", ".join(_fmt(item) for item in items)


def check_limits(number, info):
    """Жёсткие пределы — ValueProblem, мягкие — возвращаем как предупреждения."""
    minimum = _num(info.get("minimum_value"))
    maximum = _num(info.get("maximum_value"))
    if minimum is not None and number < minimum:
        raise ValueProblem("значение %s меньше допустимого минимума %s%s"
                           % (_fmt(number), _fmt(minimum), _unit(info)))
    if maximum is not None and number > maximum:
        raise ValueProblem("значение %s больше допустимого максимума %s%s"
                           % (_fmt(number), _fmt(maximum), _unit(info)))
    warnings = []
    min_warn = _num(info.get("minimum_value_warning"))
    max_warn = _num(info.get("maximum_value_warning"))
    if min_warn is not None and number < min_warn:
        warnings.append("значение %s ниже рекомендуемого минимума %s%s"
                        % (_fmt(number), _fmt(min_warn), _unit(info)))
    if max_warn is not None and number > max_warn:
        warnings.append("значение %s выше рекомендуемого максимума %s%s"
                        % (_fmt(number), _fmt(max_warn), _unit(info)))
    return warnings


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unit(info):
    unit = info.get("unit")
    return " " + unit if unit else ""


def _fmt(number):
    if isinstance(number, float):
        if number.is_integer():
            return str(int(number))
        return ("%.4f" % number).rstrip("0").rstrip(".")
    return str(number)


def json_safe(value):
    """Делает значение пригодным для JSON (множества, функции, объекты вроде Duration)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return dict((str(k), json_safe(v)) for k, v in value.items())
    return str(value)


# Материалы
_SERIES = ("proto", "enduse", "fiberpart", "sealant", "metalcast", "hightemp")


def polymer_of(name, material_type=None, brand=None):
    """Тип полимера материала: 'Enduse PETG' -> 'PETG', Generic 'PLA' -> 'PLA'."""
    name = (name or "").strip()
    if brand and brand.lower() != "stereotech" and material_type:
        return material_type
    words = name.split()
    if words and words[0].lower() in _SERIES and len(words) > 1:
        return words[1]
    if material_type and material_type.lower() not in _SERIES:
        return material_type
    return words[0] if words else (material_type or "")
