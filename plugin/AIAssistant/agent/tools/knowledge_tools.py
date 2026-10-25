"""Инструменты базы знаний: правила печати, подбор материала и пресеты под задачу.

Детерминированный поиск по agent/knowledge/*.md|json, чтобы маленькой модели не
приходилось помнить (или выдумывать) правила принтера, свойства материалов и точные
ключи настроек. Слайсер не нужен — обработчики работают в потоке вызывающего.
"""

import io
import json
import os
import re
from collections import OrderedDict

from .registry import ToolError

GROUP = "knowledge"

KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge")
RULES_FILE = os.path.join(KNOWLEDGE_DIR, "print_rules_ru.md")
MATERIALS_FILE = os.path.join(KNOWLEDGE_DIR, "materials_h530.json")
PRESETS_FILE = os.path.join(KNOWLEDGE_DIR, "task_presets.json")

TOPICS = OrderedDict([
    ("modes", "выбор режима 3D / 5D Spiral / 5D"),
    ("setup_5d", "подготовка сцены и настроек в 5D: ось, центр, Base Diameter, оснастка"),
    ("orientation", "ориентация и положение в 3D"),
    ("supports", "поддержки"),
    ("adhesion", "адгезия к столу и оснастке"),
    ("materials", "правила выбора материала"),
    ("settings", "ключевые настройки и их точные ключи"),
    ("quality", "профили качества"),
    ("honesty", "невыполнимые требования и оговорки"),
    ("troubleshooting", "ошибки и восстановление"),
])

NEEDS = OrderedDict([
    ("uv", "стойкость к УФ/солнцу"),
    ("moisture", "гидрофобность, влажная среда"),
    ("stiffness", "высокая жёсткость (модуль)"),
    ("toughness", "ударная вязкость, не хрупкий"),
    ("flexibility", "эластичность, гибкость"),
    ("oil", "масла, смазки, СОЖ"),
    ("fuel", "бензин, топливо"),
    ("alkali", "щёлочи, моющие средства"),
    ("acid", "слабые кислоты, соли, реагенты"),
    ("hot_water_glycol", "горячая вода, гликоль, тосол"),
    ("wear", "износ, абразив"),
    ("low_friction", "трение скольжения, щадить ответную деталь"),
    ("grip", "сцепление с поверхностью (ролики, колёса)"),
    ("food_contact", "контакт с пищей"),
    ("dielectric", "диэлектрик, без проводящего наполнителя"),
    ("clean", "не пылит и не пачкает"),
    ("surface", "внешний вид поверхности"),
    ("printability", "простота печати"),
])

TASKS = ["functional_load", "flexible", "decorative", "precise_fit", "chemical", "heat_resistant",
         "rotation_5d", "prototype"]

MODES = ["classic", "cylindrical", "cylindrical_full"]
FIVE_D_INFILL = ("lines", "grid", "concentric")
QUALITY_ORDER = ["high", "normal", "fast", "draft", "verydraft"]   # от тонкого слоя к толстому
MAX_LIMIT = 10
ELASTOMER_NEEDS = {"flexibility", "grip"}
ABRASIVE_PENALTY = 0.5   # штраф к оценке абразивных (наполненных) материалов, см. _score

_cache = {}


# Загрузка данных

def _read_text(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _load_json(path):
    mtime = os.path.getmtime(path)
    cached = _cache.get(path)
    if cached is None or cached[0] != mtime:
        data = json.loads(_read_text(path), object_pairs_hook=OrderedDict)
        cached = (mtime, data)
        _cache[path] = cached
    return cached[1]


def _materials():
    return _load_json(MATERIALS_FILE)


def _presets():
    return _load_json(PRESETS_FILE)


def _rules_sections():
    """{тема: текст} по заголовкам вида '## <тема> — <название>' из print_rules_ru.md."""
    text = re.sub(r"<!--.*?-->", "", _read_text(RULES_FILE), flags=re.S)
    sections = OrderedDict()
    current = None
    lines = []
    for line in text.splitlines():
        match = re.match(r"^##\s+([a-z_0-9]+)\s", line)
        if match:
            if current:
                sections[current] = "\n".join(lines).strip()
            current = match.group(1)
            lines = [line]
        elif current:
            lines.append(line)
    if current:
        sections[current] = "\n".join(lines).strip()
    return sections


def find_material(material_id):
    wanted = (material_id or "").strip().lower()
    for item in _materials()["materials"]:
        if item["id"].lower() == wanted or item["name"].lower() == wanted:
            return item
    return None


# Обработчики

def get_print_guidelines(args, ctx=None):
    topic = args.get("topic")
    sections = _rules_sections()
    if topic not in sections:
        raise ToolError("Нет раздела '%s'. Доступные: %s" % (topic, ", ".join(sections)))
    return sections[topic]


def recommend_material(args, ctx=None):
    needs = _unique(args.get("needs") or [])
    service_temp = args.get("service_temp_c")
    min_tg = args.get("min_tg_c")
    max_density = args.get("max_density")
    # модель часто не указывает kind: эластомеры берём, только если просят гибкость или сцепление
    kind = args.get("kind") or ("any" if set(needs) & ELASTOMER_NEEDS else "rigid")
    hardened = args.get("hardened_nozzle", True)
    exclude = set(x.lower() for x in (args.get("exclude") or []))
    limit = int(args.get("limit") or 3)

    weights = _need_weights(needs)
    rejected = OrderedDict()
    candidates = []
    for item in _materials()["materials"]:
        reason = _reject_reason(item, kind, hardened, exclude, max_density, service_temp, min_tg)
        if reason:
            rejected[item["id"]] = reason
            continue
        candidates.append(item)

    relaxed = None
    if not candidates and (service_temp is not None or min_tg is not None):
        # По температуре не подошёл никто — всё равно ранжируем и честно об этом пишем.
        relaxed = "Ни один доступный материал не проходит по температуре; показаны лучшие по остальным критериям."
        candidates = [item for item in _materials()["materials"]
                      if not _reject_reason(item, kind, hardened, exclude, max_density, None, None)]
    if not candidates:
        raise ToolError("Нет материалов под эти ограничения (kind=%s, hardened_nozzle=%s, max_density=%s). "
                        "Ослабь ограничения или сообщи пользователю, что подходящего материала нет."
                        % (kind, hardened, max_density))

    scored = []
    for item in candidates:
        score, strong, weak = _score(item, needs, weights)
        scored.append((score, item["ratings"].get("printability", 0), item, strong, weak))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]["id"]))

    ranked = []
    for score, _printability, item, strong, weak in scored[:max(1, min(limit, MAX_LIMIT))]:
        ranked.append(_material_card(item, round(score, 2), strong, weak, service_temp, min_tg))

    result = OrderedDict()
    result["needs"] = [("%s (%s)" % (need, NEEDS[need])) for need in needs]
    result["ranked"] = ranked
    if relaxed:
        result["warning"] = relaxed
    result["excluded_count"] = len(rejected)
    if not hardened:
        result["note_nozzle"] = "Абразивные материалы (Fiberpart, Metalcast) исключены: нет закалённого сопла."
    result["disclaimer"] = ("Рейтинги и температуры — оценка команды по описаниям производителя, не паспорт. "
                            "Проверь id через list_materials; температуры уточнить по паспорту филамента.")
    return result


def get_print_preset(args, ctx=None):
    tasks = _unique(args.get("tasks") or [])
    if not tasks:
        raise ToolError("Укажи хотя бы одну задачу в tasks: %s" % ", ".join(TASKS))
    mode = args.get("mode", "classic")
    data = _presets()
    presets = data["presets"]

    quality = None
    settings = OrderedDict()
    speed_cap = None
    why = []
    notes = []
    for task in tasks:
        preset = presets[task]
        task_quality = (preset.get("quality_type_by_mode") or {}).get(mode, preset["quality_type"])
        if task_quality != preset["quality_type"] and preset.get("quality_note_by_mode", {}).get(mode):
            notes.append(preset["quality_note_by_mode"][mode])
        quality = _thinner_quality(quality, task_quality)
        for key, value in preset["settings"].items():
            settings[key] = _merge_value(key, settings.get(key), value)
        cap = preset.get("speed_cap")
        if cap is not None:
            speed_cap = cap if speed_cap is None else min(speed_cap, cap)
        why.append("%s: %s" % (preset["title"], preset["why"]))
        notes.extend(preset.get("notes") or [])

    material = None
    material_id = args.get("material_id")
    if material_id:
        material = find_material(material_id)
        if material is None:
            notes.append("Материал '%s' не найден в базе знаний; скорость не подобрана по материалу." % material_id)

    if material is not None:
        recommended = material["profile"].get("speed_print_recommended")
        speed = recommended if recommended else speed_cap
        if speed is not None and speed_cap is not None:
            speed = min(speed, speed_cap)
        if speed:
            settings["speed_print"] = speed
        if material["flexible"] and "flexible" not in tasks:
            notes.append("Материал эластичный: добавь задачу 'flexible' (медленная печать, без Z-hop).")
        if material["abrasive"]:
            notes.append("%s абразивен: нужно закалённое сопло (6AB040H), T сопла на 15-30 °C выше, чем для "
                         "латуни — уточнить по паспорту филамента." % material["name"])
    elif speed_cap is not None:
        settings["speed_print"] = speed_cap

    if mode in ("cylindrical", "cylindrical_full"):
        settings, removed = _adapt_for_5d(settings, data.get("five_d_rules", {}))
        if removed:
            notes.append("Для 5D убраны ключи %s: их задаёт профиль 5D." % ", ".join(removed))
        base = args.get("base_diameter")
        if base is not None:
            if base < 4:
                raise ToolError("base_diameter не может быть меньше 4 мм (минимум слайсера).")
            settings["cylindrical_mode_base_diameter"] = base
        else:
            notes.append("Задай cylindrical_mode_base_diameter: диаметр осевого отверстия (если >= 6 мм) или 6.")
    elif "rotation_5d" in tasks:
        notes.append("Пресет rotation_5d рассчитан на режим 5D, а выбран classic.")

    result = OrderedDict()
    result["mode"] = mode
    result["quality_type"] = quality
    result["layer_height"] = data["quality_types"].get(quality, {}).get("layer_height")
    result["settings"] = settings
    result["how_to_apply"] = ("1) set_print_mode('%s'); 2) set_material; 3) select_profile('%s'); "
                              "4) set_settings(changes=settings) одним вызовом." % (mode, quality))
    if material is not None:
        result["material"] = OrderedDict([("id", material["id"]), ("name", material["name"]),
                                          ("profile_from_manufacturer", material["profile"])])
    result["why"] = why
    result["notes"] = _unique(notes)
    result["label"] = "Стартовые значения: оценка команды, требуется пробная печать."
    return result


# Вспомогательные функции

def _unique(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _need_weights(needs):
    """Чем раньше требование в списке, тем больше вес: 1.5 у первого, до 1.0 у последнего."""
    count = len(needs)
    if count <= 1:
        return dict((need, 1.0) for need in needs)
    return dict((need, 1.0 + 0.5 * (count - 1 - index) / float(count - 1)) for index, need in enumerate(needs))


def _reject_reason(item, kind, hardened, exclude, max_density, service_temp, min_tg):
    if item.get("support_only"):
        return "только для поддержек"
    if item["id"].lower() in exclude:
        return "исключён пользователем"
    if kind == "rigid" and (item["flexible"] or item.get("elastomer")):
        return "эластомер"
    if kind == "flexible" and item["ratings"].get("flexibility", 0) < 2:
        return "недостаточно эластичный"
    if not hardened and item["abrasive"]:
        return "абразивный, нужно закалённое сопло"
    if max_density is not None and item["density"] > max_density:
        return "плотность %s > %s" % (item["density"], max_density)
    if service_temp is not None and item["service_temp_c_est"] < service_temp:
        return "рабочая T ~%s < %s °C" % (item["service_temp_c_est"], service_temp)
    if min_tg is not None and item["tg_c_est"] < min_tg:
        return "Tg ~%s < %s °C" % (item["tg_c_est"], min_tg)
    return None


def _score(item, needs, weights):
    ratings = item["ratings"]
    score = 0.0
    strong = []
    weak = []
    for need in needs:
        value = ratings.get(need, 0)
        score += weights[need] * value
        if value >= 3:
            strong.append(NEEDS[need])
        elif value <= 1:
            weak.append(NEEDS[need])
            score -= 1.0
    if item["brand"] == "Stereotech":
        score += 0.5
    if item["abrasive"]:
        # Замена сопла на закалённое, выше температура, износ — при почти равных оценках берём ненаполненный материал.
        score -= ABRASIVE_PENALTY
    return score, strong, weak


def _material_card(item, score, strong, weak, service_temp, min_tg):
    card = OrderedDict()
    card["id"] = item["id"]
    card["name"] = item["name"]
    card["polymer"] = item["polymer"] + (" + " + item["filler"] if item.get("filler") else "")
    card["score"] = score
    card["strong"] = strong
    card["weak_or_unmet"] = weak
    card["density"] = item["density"]
    card["service_temp_c_est"] = item["service_temp_c_est"]
    if min_tg is not None:
        card["tg_c_est"] = item["tg_c_est"]
    card["needs_hardened_nozzle"] = item["abrasive"]
    card["extruders"] = item["extruders"]
    card["profile_from_manufacturer"] = item["profile"]
    card["strengths"] = item["strengths"]
    card["limitations"] = item["limitations"]
    return card


def _thinner_quality(current, candidate):
    if current is None:
        return candidate
    if candidate not in QUALITY_ORDER or current not in QUALITY_ORDER:
        return current
    return candidate if QUALITY_ORDER.index(candidate) < QUALITY_ORDER.index(current) else current


_MAX_KEYS = ("wall_line_count", "top_bottom_thickness", "infill_sparse_density", "brim_width")
_MIN_KEYS = ("speed_wall_0",)


def _merge_value(key, old, new):
    if old is None:
        return new
    if isinstance(new, bool):
        return bool(old) or new
    if key in _MAX_KEYS:
        return max(old, new)
    if key in _MIN_KEYS:
        return min(old, new)
    if key == "adhesion_type":
        order = ["none", "skirt", "brim", "raft"]
        return old if order.index(old) >= order.index(new) else new
    return old   # для перечислений вроде infill_pattern побеждает первая задача


def _adapt_for_5d(settings, rules):
    removed = []
    adapted = OrderedDict()
    drop = set(rules.get("do_not_set") or [])
    for key, value in settings.items():
        if key in drop:
            removed.append(key)
            continue
        if key == "infill_pattern" and value not in FIVE_D_INFILL:
            value = "grid"
        if key == "support_tree_enable":
            removed.append(key)
            continue
        adapted[key] = value
    return adapted, removed


# Регистрация

def _object(properties, required=None):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def register(registry):
    registry.tool(
        "get_print_guidelines",
        "Правила печати H530 по теме (режимы, 5D-сцена, ориентация, поддержки, адгезия, материалы, ключи "
        "настроек, качество, честность, ошибки).",
        _object({"topic": {"type": "string", "enum": list(TOPICS)}}, ["topic"]),
        main_thread=False, group=GROUP,
    )(get_print_guidelines)

    registry.tool(
        "recommend_material",
        "Подбор материала из доступных для H530 по условиям (по таблице): рейтинг, сильные стороны, "
        "невыполненные требования, плотность, температуры. id проверь в list_materials.",
        _object({
            "needs": {"type": "array", "items": {"type": "string", "enum": list(NEEDS)}, "minItems": 1,
                      "description": "по убыванию важности"},
            "service_temp_c": {"type": "number", "description": "макс. рабочая T, °C"},
            "min_tg_c": {"type": "number", "description": "мин. Tg, только если Tg названа в ТЗ; Вика/HDT сюда не передавай"},
            "max_density": {"type": "number", "description": "г/см³"},
            "kind": {"type": "string", "enum": ["any", "rigid", "flexible"]},
            "hardened_nozzle": {"type": "boolean", "default": True, "description": "false — без абразивных"},
            "exclude": {"type": "array", "items": {"type": "string"}, "description": "id материалов"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 3},
        }, ["needs"]),
        main_thread=False, group=GROUP,
    )(recommend_material)

    registry.tool(
        "get_print_preset",
        "Стартовые настройки (точные ключи) для типовых задач (можно несколько; rotation_5d — тело вращения "
        "в 5D). Возвращает quality_type для select_profile и settings для одного set_settings.",
        _object({
            "tasks": {"type": "array", "items": {"type": "string", "enum": TASKS}, "minItems": 1},
            "mode": {"type": "string", "enum": MODES, "default": "classic"},
            "material_id": {"type": "string", "description": "id материала (скорость по материалу)"},
            "base_diameter": {"type": "number", "description": "Base Diameter 5D, мм (>= 4)"},
        }, ["tasks"]),
        main_thread=False, group=GROUP,
    )(get_print_preset)
