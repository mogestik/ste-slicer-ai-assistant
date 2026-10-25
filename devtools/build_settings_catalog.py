"""Собирает справочник настроек, по которому AI Assistant проверяет ключи и значения.

Читает определения STE Slicer (fdmprinter -> steprinter_v5 -> steprinter530_v5.2.4 ->
hte530v5.2.4, плюс fdmextruder -> stereotech_v5.2.4_extruder_main), материал по умолчанию
(generic_pla_175) и русские переводы, а пишет
plugin/AIAssistant/agent/knowledge/settings_catalog.json.

    python devtools/build_settings_catalog.py
    python devtools/build_settings_catalog.py --src "C:/Program Files/Stereotech STE Slicer 7.10" --out <файл.json>

--src — папка, в которой лежит resources/ (установленный слайсер или исходники STE-Slicer).
Значения по умолчанию считаются статически для H530 V5.2.4 (два одинаковых экструдера,
материал по умолчанию, качество "normal", которое пустое). Формулы считаются дважды: для
режима принтера по умолчанию (cylindrical_full, «5D») и для classic («3D»); `default_3d`
пишем, только если оно отличается.
"""

import argparse
import ast
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(REPO_DIR, "plugin", "AIAssistant", "agent", "knowledge", "settings_catalog.json")
# Где искать слайсер, если --src не указан: обычная установка и исходники рядом с репозиторием.
SRC_CANDIDATES = [
    r"C:\Program Files\Stereotech STE Slicer 7.10",
    os.path.join(os.path.dirname(REPO_DIR), "STE-Slicer-master"),
]

MACHINE_ID = "hte530v5.2.4"
EXTRUDER_ID = "stereotech_v5.2.4_extruder_main"
EXTRUDER_COUNT = 2
DEFAULT_MATERIAL = "generic_pla_175"
DEFAULT_MODE = "cylindrical_full"

DESC_LIMIT = 180
STR_DEFAULT_LIMIT = 200

# XmlMaterialProfile.__material_settings_setting_map
MATERIAL_SETTING_MAP = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
    "adhesion tendency": "material_adhesion_tendency",
    "surface energy": "material_surface_energy",
    "shrinkage percentage": "material_shrinkage_percentage",
}
# Настройки, действующее значение которых приходит из выбранного материала (напрямую или по формуле).
FROM_MATERIAL = set(MATERIAL_SETTING_MAP.values()) | {
    "material_print_temperature", "material_print_temperature_layer_0",
    "material_initial_print_temperature", "material_final_print_temperature",
    "material_bed_temperature", "material_bed_temperature_layer_0",
}

# Настройки, которые агент меняет чаще всего и которые сравниваем при проверках.
IMPORTANT = [
    "printing_mode", "layer_height", "layer_height_0", "line_width",
    "wall_thickness", "wall_line_count", "top_bottom_thickness", "top_thickness", "bottom_thickness",
    "top_layers", "bottom_layers", "top_bottom_pattern",
    "infill_sparse_density", "infill_pattern", "infill_before_walls",
    "material_print_temperature", "material_print_temperature_layer_0", "material_bed_temperature",
    "material_standby_temperature", "material_flow",
    "speed_print", "speed_wall_0", "speed_wall_x", "speed_infill", "speed_topbottom", "speed_layer_0",
    "speed_travel",
    "cool_fan_enabled", "cool_fan_speed", "cool_min_layer_time",
    "retraction_enable", "retraction_amount", "retraction_speed", "retraction_hop_enabled",
    "support_enable", "support_type", "support_angle", "support_pattern", "support_infill_rate",
    "support_extruder_nr", "support_interface_enable", "support_tree_enable",
    "support_enable_classic", "support_enable_cylindrical", "support_enable_top_support",
    "adhesion_type", "brim_width", "skirt_line_count", "cylindrical_raft_type",
    "z_seam_type", "ironing_enabled", "magic_spiralize", "print_sequence", "xy_offset",
    "cylindrical_mode_base_diameter", "non_printing_base_diameter", "cylindrical_mode_overlap",
    "cylindrical_layer_height", "cylindrical_layer_height_0",
]

_5D_KEY_RE = re.compile(r"(cylindrical|spherical|conical|descrete|discrete|_classic$|non_printing_base|"
                        r"segment_length|top_support|machine_five_axis|machine_hybrid|machine_[ac]_axis|"
                        r"basement|printing_mode)")


# Загрузка

def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=OrderedDict)


def definition_chain(defs_dir, def_id):
    chain = []
    while def_id:
        data = load_json(os.path.join(defs_dir, def_id + ".def.json"))
        chain.append((def_id, data))
        def_id = data.get("inherits")
    chain.reverse()  # сначала базовое
    return chain


def flatten(settings, category=None, parent=None, out=None):
    """Разворачивает вложенное дерево 'settings' в OrderedDict ключ -> свойства (+category, parent)."""
    if out is None:
        out = OrderedDict()
    for key, props in settings.items():
        entry = OrderedDict((k, v) for k, v in props.items() if k != "children")
        if props.get("type") == "category":
            entry["_category"] = key
        else:
            entry["_category"] = category
            entry["_parent"] = parent
        out[key] = entry
        children = props.get("children")
        if children:
            flatten(children, key if props.get("type") == "category" else category,
                    None if props.get("type") == "category" else key, out)
    return out


def merge_overrides(base, chain):
    for def_id, data in chain:
        for key, props in (data.get("overrides") or {}).items():
            if key in base:
                base[key].update(props)
                base[key]["_overridden_by"] = def_id


def parse_po(path):
    """Простейшее чтение gettext .po: возвращает {msgctxt: msgstr}."""
    result = {}
    if not os.path.exists(path):
        return result
    current = {}
    field = None

    def unquote(text):
        text = text.strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")

    def flush():
        if current.get("msgctxt") and current.get("msgstr"):
            result[current["msgctxt"]] = current["msgstr"]

    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                if line.startswith("#") and current.get("msgstr") is not None:
                    flush()
                    current = {}
                continue
            match = re.match(r'^(msgctxt|msgid|msgstr)\s+(".*")\s*$', line)
            if match:
                field = match.group(1)
                if field == "msgctxt" and "msgstr" in current:
                    flush()
                    current = {}
                current[field] = unquote(match.group(2))
            elif line.startswith('"') and field:
                current[field] = current.get(field, "") + unquote(line)
    flush()
    return result


def load_material(materials_dir, material_id):
    path = os.path.join(materials_dir, material_id + ".xml.fdm_material")
    values = {}
    if not os.path.exists(path):
        return values
    root = ET.parse(path).getroot()
    for node in root:
        if node.tag.endswith("settings"):
            for setting in node:
                if setting.tag.endswith("setting"):
                    target = MATERIAL_SETTING_MAP.get(setting.get("key"))
                    if target:
                        try:
                            values[target] = float(setting.text)
                        except (TypeError, ValueError):
                            pass
    return values


# Статическое вычисление формул

class EvalError(Exception):
    pass


class Evaluator(object):
    def __init__(self, settings, overrides):
        self.settings = settings
        self.overrides = overrides  # ключ -> зафиксированное значение (материал, printing_mode)
        self.cache = {}
        self.stack = []

    def _functions(self):
        return {
            "math": math, "min": min, "max": max, "round": round, "sum": sum, "any": any, "all": all,
            "len": len, "int": int, "float": float, "abs": abs, "str": str, "bool": bool, "map": map,
            "list": list,
            "resolveOrValue": self.value,
            "extruderValue": lambda extruder, key: self.value(key),
            "extruderValues": lambda key: [self.value(key)] * EXTRUDER_COUNT,
            "defaultExtruderPosition": lambda: "0",
            "valueFromContainer": lambda *args: None,
        }

    def names(self, expr):
        try:
            tree = ast.parse(expr.strip(), mode="eval")
        except SyntaxError:
            raise EvalError("syntax")
        return set(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))

    def eval_expr(self, expr):
        if not isinstance(expr, str):
            return expr
        namespace = self._functions()
        for name in self.names(expr):
            if name in namespace or name in ("True", "False", "None"):
                continue
            if name in self.settings:
                namespace[name] = self.value(name)
            else:
                raise EvalError("unknown name %s" % name)
        namespace["__builtins__"] = {}
        try:
            return eval(expr.strip(), namespace)
        except EvalError:
            raise
        except Exception as error:  # формула может упасть по многим причинам — считаем её невычислимой
            raise EvalError(str(error))

    def value(self, key):
        if key in self.overrides:
            return self.overrides[key]
        if key in self.cache:
            return self.cache[key]
        if key in self.stack:
            raise EvalError("cycle at %s" % key)
        props = self.settings.get(key)
        if props is None:
            raise EvalError("unknown setting %s" % key)
        self.stack.append(key)
        try:
            if "value" in props:
                result = cast(props.get("type"), self.eval_expr(props["value"]))
            else:
                result = props.get("default_value")
        finally:
            self.stack.pop()
        self.cache[key] = result
        return result

    def try_value(self, key):
        try:
            return True, self.value(key)
        except EvalError:
            return False, None

    def try_expr(self, expr):
        try:
            return True, self.eval_expr(expr)
        except EvalError:
            return False, None


def cast(setting_type, value):
    if value is None:
        return None
    try:
        if setting_type == "float":
            return float(value)
        if setting_type in ("int", "extruder", "optional_extruder"):
            return int(round(float(value)))
        if setting_type == "bool":
            return bool(value)
    except (TypeError, ValueError):
        return value
    return value


def compact_number(value):
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    if value != value or value in (float("inf"), float("-inf")):
        return None
    rounded = round(value, 6)
    if rounded == int(rounded) and abs(rounded) < 1e15:
        return int(rounded)
    return rounded


def compact_value(value, setting_type):
    if isinstance(value, float):
        value = compact_number(value)
        if setting_type == "float" and isinstance(value, int) and not isinstance(value, bool):
            return value  # 40 вместо 40.0 — файл меньше, при загрузке тип вернётся
        return value
    if isinstance(value, str) and len(value) > STR_DEFAULT_LIMIT:
        return value[:STR_DEFAULT_LIMIT] + "…"
    if isinstance(value, (list, tuple)):
        return [compact_value(item, None) for item in value]
    return value


def short_text(text, limit=DESC_LIMIT):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    if end > limit // 2:
        return cut[:end + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut) + "…"


def machine_only(evaluator, expr):
    """True, если формула зависит только от железа принтера/экструдера (для H530 это константа)."""
    try:
        names = evaluator.names(expr)
    except EvalError:
        return False
    for name in names:
        if name in ("math", "True", "False", "None", "min", "max", "round", "float", "int", "abs",
                    "extruderValue", "extruderValues", "resolveOrValue"):
            continue
        if not (name.startswith("machine_") or name == "material_diameter"):
            return False
    for literal in re.findall(r"['\"]([a-z0-9_]+)['\"]", expr):
        if literal in evaluator.settings and not (literal.startswith("machine_") or literal == "material_diameter"):
            return False
    return True



def build(src):
    res = os.path.join(src, "resources")
    defs_dir = os.path.join(res, "definitions")
    extruders_dir = os.path.join(res, "extruders")

    machine_chain = definition_chain(defs_dir, MACHINE_ID)
    if machine_chain[0][0] != "fdmprinter":
        raise SystemExit("unexpected base definition: %s" % machine_chain[0][0])
    settings = flatten(machine_chain[0][1]["settings"])

    extruder_chain = []
    def_id = EXTRUDER_ID
    while def_id:
        path = os.path.join(extruders_dir, def_id + ".def.json")
        if not os.path.exists(path):
            path = os.path.join(defs_dir, def_id + ".def.json")
        data = load_json(path)
        extruder_chain.append((def_id, data))
        def_id = data.get("inherits")
    extruder_chain.reverse()
    extruder_settings = flatten(extruder_chain[0][1]["settings"])  # fdmextruder
    extruder_only = set()
    for key, props in extruder_settings.items():
        if key not in settings:
            settings[key] = props
            extruder_only.add(key)
        elif props.get("type") != "category":
            settings[key].update(props)

    merge_overrides(settings, machine_chain[1:])
    merge_overrides(settings, extruder_chain[1:])

    material = load_material(os.path.join(res, "materials"), DEFAULT_MATERIAL)
    po = parse_po(os.path.join(res, "i18n", "ru_RU", "fdmprinter.def.json.po"))
    po.update(parse_po(os.path.join(res, "i18n", "ru_RU", "fdmextruder.def.json.po")))

    ev_5d = Evaluator(settings, dict(material, printing_mode=DEFAULT_MODE))
    ev_3d = Evaluator(settings, dict(material, printing_mode="classic"))

    categories = OrderedDict()
    catalog = OrderedDict()
    stats = {"settings": 0, "formula_default_failed": [], "missing_important": []}

    for key, props in settings.items():
        setting_type = props.get("type")
        if setting_type == "category":
            categories[key] = OrderedDict([("label", props.get("label")),
                                           ("label_ru", po.get(key + " label", ""))])
            continue
        entry = OrderedDict()
        entry["type"] = setting_type
        if props.get("unit"):
            entry["unit"] = props["unit"]
        entry["label"] = props.get("label")
        label_ru = po.get(key + " label")
        if label_ru and label_ru != props.get("label"):
            entry["label_ru"] = label_ru
        entry["category"] = props.get("_category")
        if props.get("_parent"):
            entry["parent"] = props["_parent"]

        ok_5d, value_5d = ev_5d.try_value(key)
        ok_3d, value_3d = ev_3d.try_value(key)
        if not ok_5d:
            value_5d = props.get("default_value")
            stats["formula_default_failed"].append(key)
        if not ok_3d:
            value_3d = value_5d
        entry["default"] = compact_value(value_5d, setting_type)
        if compact_value(value_3d, setting_type) != entry["default"]:
            entry["default_3d"] = compact_value(value_3d, setting_type)
        if "value" in props and isinstance(props["value"], str):
            entry["formula"] = props["value"].strip()
        if not ok_5d:
            entry["default_is_static"] = False

        for prop, short in (("minimum_value", "min"), ("maximum_value", "max"),
                            ("minimum_value_warning", "min_warn"), ("maximum_value_warning", "max_warn")):
            if prop not in props:
                continue
            raw = props[prop]
            if isinstance(raw, str):
                raw_s = raw.strip()
                try:
                    entry[short] = compact_number(float(raw_s))
                    continue
                except ValueError:
                    pass
                if machine_only(ev_5d, raw_s):
                    ok, number = ev_5d.try_expr(raw_s)
                    if ok and isinstance(number, (int, float)) and not isinstance(number, bool):
                        entry[short] = compact_number(float(number))
                        continue
                entry[short] = raw_s
            elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                entry[short] = compact_number(float(raw))

        if setting_type == "enum":
            options = []
            for option_key, option_label in (props.get("options") or {}).items():
                ru = po.get("%s option %s" % (key, option_key), "")
                options.append([option_key, option_label, ru if ru != option_label else ""])
            entry["options"] = options

        entry["per_extruder"] = bool(props.get("settable_per_extruder", True))
        entry["per_mesh"] = bool(props.get("settable_per_mesh", True))
        if key in extruder_only:
            entry["extruder_only"] = True

        enabled = props.get("enabled")
        if isinstance(enabled, str) and enabled.strip() not in ("True", "true"):
            entry["enabled"] = enabled.strip()
        elif enabled is False or enabled == "False":
            entry["enabled"] = "False"

        entry["desc"] = short_text(props.get("description"))
        desc_ru = po.get(key + " description")
        if desc_ru:
            entry["desc_ru"] = short_text(desc_ru)
        if key in IMPORTANT:
            entry["important"] = True
        if _5D_KEY_RE.search(key) or props.get("_category") == "printing_mode_category":
            entry["mode_5d"] = True
        if key in FROM_MATERIAL:
            entry["from_material"] = True
        catalog[key] = entry
        stats["settings"] += 1

    for key in IMPORTANT:
        if key not in catalog:
            stats["missing_important"].append(key)

    data = OrderedDict()
    data["format"] = 1
    data["machine"] = MACHINE_ID
    data["machine_name"] = machine_chain[-1][1].get("name")
    data["default_printing_mode"] = DEFAULT_MODE
    data["default_material"] = DEFAULT_MATERIAL
    data["note"] = ("default = effective value for H530 V5.2.4 in printing_mode %s, material %s, quality normal; "
                    "default_3d = value in classic mode when different; formula = Cura value expression; "
                    "min/max/min_warn/max_warn = number or unevaluated formula." % (DEFAULT_MODE, DEFAULT_MATERIAL))
    data["categories"] = categories
    data["important"] = [key for key in IMPORTANT if key in catalog]
    data["settings"] = catalog
    return data, stats


def dump(data, path):
    """Компактный JSON, по настройке на строку (и места мало, и diff читается)."""
    parts = ["{"]
    items = list(data.items())
    for index, (key, value) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        if key == "settings":
            parts.append('"settings":{')
            settings = list(value.items())
            for s_index, (s_key, s_value) in enumerate(settings):
                s_comma = "," if s_index < len(settings) - 1 else ""
                parts.append("%s:%s%s" % (json.dumps(s_key), json.dumps(s_value, ensure_ascii=False,
                                                                       separators=(",", ":")), s_comma))
            parts.append("}" + comma)
        else:
            parts.append("%s:%s%s" % (json.dumps(key), json.dumps(value, ensure_ascii=False,
                                                                 separators=(",", ":")), comma))
    parts.append("}")
    text = "\n".join(parts) + "\n"
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--src", help="папка слайсера с подпапкой resources (установленный STE Slicer или исходники)")
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()
    src = args.src
    if not src:
        src = next((path for path in SRC_CANDIDATES if os.path.isdir(path)), None)
    if not src or not os.path.isdir(os.path.join(src, "resources", "definitions")):
        raise SystemExit("Не найдена папка слайсера с resources/definitions — укажите --src")
    data, stats = build(src)
    size = dump(data, args.out)
    print("settings: %d, categories: %d, important: %d" % (stats["settings"], len(data["categories"]),
                                                         len(data["important"])))
    print("written %s (%.1f KB)" % (args.out, size / 1024.0))
    if stats["missing_important"]:
        print("WARNING: important keys missing: %s" % ", ".join(stats["missing_important"]))
    failed = stats["formula_default_failed"]
    print("defaults not computable statically (%d): %s" % (len(failed), ", ".join(failed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
