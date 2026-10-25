"""Инструменты настроек, профилей, материалов, принтера и режима печати 3D/5D.

Обработчики тонкие: со слайсером работает адаптер (agent/slicer). Ключи и значения
настроек сначала нормализует справочник (agent/knowledge/settings_catalog.py, он
необязателен), а окончательно их проверяет живой стек контейнеров слайсера — он и есть
источник истины.
"""

import time

from ..main_thread import run_in_main_thread
from ..slicer import get_adapter
from ..slicer.base import ATSS_NOTE, FIVE_D_MODES, PRINT_MODE_TITLES, TARGET_MACHINE, tool_errors
from ..slicer.values import ValueProblem, coerce_value, json_safe
from .registry import ToolError

GROUP = "settings"

PRINT_MODES = ["classic", "cylindrical", "cylindrical_full"]
MODE_SWITCH_DELAY = 0.6   # сигналы смены свойств и перестройка объёма печати идут через callLater и таймеры

# Настройки, которые показывает get_current_config (действующие значения).
CONFIG_KEYS = (
    "layer_height", "wall_line_count", "top_layers", "bottom_layers", "infill_sparse_density", "infill_pattern",
    "support_enable", "adhesion_type", "material_print_temperature", "material_bed_temperature", "speed_print",
    "cool_fan_speed", "retraction_amount", "cylindrical_mode_base_diameter", "cylindrical_layer_height",
    "cylindrical_raft_type",
)

_SCOPE_PROPS = {
    "scope": {
        "type": "string", "enum": ["global", "extruder"], "default": "global",
    },
    "extruder_index": {"type": "integer", "minimum": 0, "maximum": 7, "default": 0},
}

# Точные ключи перечислены один раз в системном промпте (prompts/system_ru.md), а не в каждом описании.
_COMMON_KEYS_HINT = "Ключи — точные ключи слайсера (не уверен — search_settings)."


def _object(properties, required=None):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _with_scope(properties):
    merged = dict(_SCOPE_PROPS)
    merged.update(properties)
    return merged


def register(registry):
    registry.tool(
        "get_current_config",
        "Текущая конфигурация: принтер, режим печати, профиль, материал и сопло экструдеров, изменённые и "
        "ключевые настройки, объём печати, ошибки.",
        _object({}), main_thread=True, group=GROUP,
    )(_get_current_config)

    registry.tool(
        "select_machine",
        "Выбрать (добавить) принтер по id определения. Новый принтер стартует в 5D — затем set_print_mode.",
        _object({
            "definition_id": {"type": "string", "default": TARGET_MACHINE},
        }), main_thread=True, group=GROUP,
    )(_select_machine)

    registry.tool(
        "get_settings",
        "Значения настроек: название, единицы, диапазон, источник значения, активность в текущем режиме. "
        + _COMMON_KEYS_HINT,
        _object(_with_scope({
            "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50},
        }), ["keys"]), main_thread=True, group=GROUP,
    )(_get_settings)

    registry.tool(
        "set_settings",
        "Изменить настройки (все нужные — одним вызовом). Значения в единицах слайсера (мм, °C, мм/с, % "
        "числом); возвращает применённые значения, предупреждения и ошибки с подсказками. Режим — "
        "set_print_mode, материал — set_material. " + _COMMON_KEYS_HINT,
        _object(_with_scope({
            "changes": {"type": "object", "description": "{ключ: значение}, напр. "
                        "{\"layer_height\": 0.2, \"infill_sparse_density\": 40}"},
        }), ["changes"]), main_thread=True, group=GROUP,
    )(_set_settings)

    registry.tool(
        "reset_settings",
        "Сбросить изменения настроек к профилю: keys или все (без keys; режим печати сохраняется).",
        _object(_with_scope({
            "keys": {"type": "array", "items": {"type": "string"}},
        })), main_thread=True, group=GROUP,
    )(_reset_settings)

    registry.tool(
        "search_settings",
        "Найти ключ настройки по слову (рус./англ.): ключ, название, тип, единицы, значение.",
        _object({
            "query": {"type": "string", "description": "напр. 'заполнение', 'retraction'"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15},
        }, ["query"]), main_thread=True, group=GROUP,
    )(_search_settings)

    registry.tool(
        "list_profiles",
        "Профили качества (quality_type, высота слоя) и пользовательские; активный отмечен. Названия "
        "обманчивы (normal = 0.1 мм, fast = 0.15 мм) — смотри высоту слоя.",
        _object({}), main_thread=True, group=GROUP,
    )(_list_profiles)

    registry.tool(
        "select_profile",
        "Выбрать профиль качества по quality_type или пользовательский (kind=custom) по имени. Изменения "
        "настроек сохраняются.",
        _object({
            "kind": {"type": "string", "enum": ["quality", "custom"], "default": "quality"},
            "name": {"type": "string", "description": "quality_type или имя профиля"},
        }, ["name"]), main_thread=True, group=GROUP,
    )(_select_profile)

    registry.tool(
        "create_profile",
        "Создать пользовательский профиль из текущих настроек (изменения переносятся в него) и активировать.",
        _object({
            "name": {"type": "string"},
        }, ["name"]), main_thread=True, group=GROUP,
    )(_create_profile)

    registry.tool(
        "save_profile",
        "Сохранить изменения в активный пользовательский профиль (встроенный нельзя — create_profile).",
        _object({}), main_thread=True, group=GROUP,
    )(_save_profile)

    registry.tool(
        "list_materials",
        "Материалы экструдера (Ø1.75, Stereotech и Generic): id, название, полимер, производитель, плотность; "
        "активный отмечен.",
        _object({
            "query": {"type": "string", "description": "фильтр, напр. 'PETG'"},
            "extruder_index": _SCOPE_PROPS["extruder_index"],
        }), main_thread=True, group=GROUP,
    )(_list_materials)

    registry.tool(
        "set_material",
        "Назначить материал по id из list_materials. Температуры, обдув и ретракт берутся из материала.",
        _object({
            "material_id": {"type": "string", "description": "напр. enduse_petg"},
            "extruder_index": _SCOPE_PROPS["extruder_index"],
        }, ["material_id"]), main_thread=True, group=GROUP,
    )(_set_material)

    registry.tool(
        "set_support_material",
        "Поддержки другим материалом со второго экструдера (только 3D и только по прямой просьбе пользователя): "
        "назначает материал экструдеру 1, support_extruder_nr=1, включает поддержки и черновую башню. "
        "Возвращает совместимость пары. disable=true — вернуть поддержки на основной экструдер.",
        _object({
            "material_id": {"type": "string", "description": "id материала поддержек, напр. proto_pva"},
            "disable": {"type": "boolean", "default": False},
        }), main_thread=True, group=GROUP,
    )(_set_support_material)

    registry.tool(
        "set_print_mode",
        "Режим печати: classic — 3D; cylindrical — 5D Spiral на оправке (внутри Base Diameter удаляется); "
        "cylindrical_full — 5D. Возвращает, что изменилось. 5D требует лицензию ATSS.",
        _object({
            "mode": {"type": "string", "enum": PRINT_MODES},
        }, ["mode"]), main_thread=False, group=GROUP,
    )(_set_print_mode)


# Справочник настроек (необязательная зависимость)

def _catalog():
    try:
        from ..knowledge.settings_catalog import get_catalog
        return get_catalog()
    except Exception:  # справочника нет или данные битые — проверяем только по стеку слайсера
        return None


def _catalog_error_class():
    try:
        from ..knowledge.settings_catalog import SettingValueError
        return SettingValueError
    except Exception:
        return ValueError


def _resolve_key(catalog, adapter, key, scope, extruder_index):
    """(точный ключ, info, подсказка); info = None, если такого ключа в стеке нет."""
    canonical, hint = key, None
    if catalog is not None:
        resolved, catalog_hint = catalog.resolve(key)
        if resolved is not None:
            canonical = resolved
        else:
            hint = catalog_hint
    info = adapter.setting_info(canonical, scope, extruder_index)
    if info is None and canonical != key:
        info = adapter.setting_info(key, scope, extruder_index)
        canonical = key if info is not None else canonical
    if info is None and hint is None:
        hint = "Неизвестная настройка «%s». Найди ключ через search_settings." % key
    return canonical, info, hint


def _label(catalog, key, info):
    if catalog is not None:
        try:
            label = catalog.label(key)
            if label and label != key:
                return label
        except Exception:
            pass
    return (info or {}).get("label") or key


def _present(catalog, key, info, brief=False):
    item = {"value": info.get("value"), "label": _label(catalog, key, info)}
    if info.get("unit"):
        item["unit"] = info["unit"]
    item["type"] = info.get("type")
    if brief:
        return item
    if info.get("type") == "enum":
        item["options"] = ["%s (%s)" % (k, l) if k != l else k for k, l in (info.get("options") or [])]
    for limit in ("minimum_value", "maximum_value", "minimum_value_warning", "maximum_value_warning"):
        if info.get(limit) is not None:
            item[limit] = info[limit]
    item["enabled"] = info.get("enabled")
    item["source"] = info.get("source")
    item["stack"] = info.get("stack")
    if info.get("validation") and info["validation"] != "valid":
        item["validation"] = info["validation"]
    if info.get("note"):
        item["note"] = info["note"]
    description = None
    if catalog is not None:
        entry = catalog.get(key) or {}
        description = entry.get("desc_ru") or entry.get("desc")
    description = description or info.get("description")
    if description:
        item["description"] = description[:240]
    return item


# Обработчики

@tool_errors
def _get_current_config(args, ctx):
    adapter = get_adapter()
    config = adapter.config()
    if config.get("machine"):
        key_settings = {}
        for key in CONFIG_KEYS:
            info = adapter.setting_info(key, "global", 0)
            if info is not None and info.get("enabled"):
                key_settings[key] = info.get("value")
        config["key_settings"] = key_settings
    return config


@tool_errors
def _select_machine(args, ctx):
    adapter = get_adapter()
    result = adapter.select_machine(args.get("definition_id") or TARGET_MACHINE)
    result["machines"] = adapter.list_machines()
    return result


@tool_errors
def _get_settings(args, ctx):
    adapter = get_adapter()
    catalog = _catalog()
    scope, index = args.get("scope", "global"), args.get("extruder_index", 0)
    values, unknown = {}, {}
    for key in args["keys"]:
        canonical, info, hint = _resolve_key(catalog, adapter, key, scope, index)
        if info is None:
            unknown[key] = hint
            continue
        item = _present(catalog, canonical, info)
        if canonical != key:
            item["resolved_from"] = key
        values[canonical] = item
    if not values and unknown:
        raise ToolError("Ни одна настройка не найдена: %s" % "; ".join("%s — %s" % kv for kv in unknown.items()))
    result = {"settings": values}
    if unknown:
        result["unknown"] = unknown
    return result


@tool_errors
def _set_settings(args, ctx):
    adapter = get_adapter()
    catalog = _catalog()
    catalog_error = _catalog_error_class()
    scope, index = args.get("scope", "global"), args.get("extruder_index", 0)
    changes = args["changes"]
    if not isinstance(changes, dict) or not changes:
        raise ToolError("changes должен быть непустым словарём {ключ: значение}")
    getter = adapter.setting_value_getter(scope, index)
    applied, errors, warnings = {}, {}, []
    for key, raw_value in changes.items():
        canonical, info, hint = _resolve_key(catalog, adapter, key, scope, index)
        if info is None:
            errors[key] = hint
            continue
        if canonical == "printing_mode":
            warnings.append("Режим печати лучше менять через set_print_mode (он возвращает, что изменилось).")
        value = raw_value
        notes = []
        catalog_problem = None
        if catalog is not None and catalog.get(canonical) is not None:
            try:
                value, catalog_warning = catalog.coerce(canonical, raw_value, context=getter)
                if catalog_warning:
                    notes.append(catalog_warning)
            except catalog_error as exc:
                catalog_problem = str(exc)
                value = raw_value
            except Exception:  # справочник не должен ломать инструмент
                value = raw_value
        try:
            typed, stack_warnings = coerce_value(value, info)
        except ValueProblem as exc:
            errors[canonical] = "%s: %s%s" % (_label(catalog, canonical, info), exc,
                                              (" (%s)" % catalog_problem) if catalog_problem else "")
            continue
        notes.extend(w for w in stack_warnings if w not in notes)
        if catalog_problem:
            notes.append("справочник: %s (слайсер значение принял)" % catalog_problem)
        if not info.get("enabled"):
            notes.append("настройка неактивна в текущем режиме/конфигурации (enabled=false) — значение "
                         "сохранено, но на печать не повлияет")
        after = adapter.set_setting(canonical, typed, scope, index)
        if str(after.get("value")) != str(json_safe(typed)):
            notes.append("итоговое значение %r отличается от заданного %r (переопределено формулой/стеком)"
                         % (after.get("value"), typed))
        if after.get("validation") == "error":
            notes.append("ОШИБКА: слайсер считает значение недопустимым — слайсинг будет невозможен, исправь")
        elif after.get("validation") == "warning":
            notes.append("слайсер помечает значение предупреждением (вне рекомендуемого диапазона)")
        entry = {"value": after.get("value"), "previous": info.get("value"), "label": _label(catalog, canonical, info),
                 "stack": after.get("stack")}
        if info.get("unit"):
            entry["unit"] = info["unit"]
        if canonical != key:
            entry["resolved_from"] = key
        if after.get("note"):
            notes.append(after["note"])
        if notes:
            entry["warnings"] = notes
        applied[canonical] = entry
    if not applied:
        raise ToolError("Ни одна настройка не применена: %s"
                        % "; ".join("%s — %s" % (k, v) for k, v in errors.items()))
    result = {"applied": applied}
    if errors:
        result["errors"] = errors
    if warnings:
        result["warnings"] = warnings
    return result


@tool_errors
def _reset_settings(args, ctx):
    adapter = get_adapter()
    catalog = _catalog()
    scope, index = args.get("scope", "global"), args.get("extruder_index", 0)
    keys = args.get("keys")
    if not keys:
        return adapter.reset_settings(None, scope, index)
    canonical_keys, unknown = [], {}
    for key in keys:
        canonical, info, hint = _resolve_key(catalog, adapter, key, scope, index)
        if info is None:
            unknown[key] = hint
        else:
            canonical_keys.append(canonical)
    result = adapter.reset_settings(canonical_keys, scope, index) if canonical_keys else {"reset": []}
    if unknown:
        result["unknown"] = unknown
    return result


@tool_errors
def _search_settings(args, ctx):
    adapter = get_adapter()
    catalog = _catalog()
    limit = args.get("limit", 15)
    query = args["query"]
    if catalog is None:
        return {"results": adapter.search_settings(query, limit), "source": "slicer"}
    results = []
    for item in catalog.search(query, limit=limit):
        info = adapter.setting_info(item["key"], "global", 0)
        if info is None:
            continue   # в определении этого принтера такой настройки нет
        entry = {"key": item["key"], "label": item.get("label_ru") or item.get("label"),
                 "type": info.get("type"), "unit": info.get("unit") or "", "value": info.get("value"),
                 "enabled": info.get("enabled")}
        if info.get("type") == "enum":
            entry["options"] = [k for k, _l in info.get("options") or []]
        results.append(entry)
    if not results:
        results = adapter.search_settings(query, limit)
    return {"results": results}


@tool_errors
def _list_profiles(args, ctx):
    return get_adapter().list_profiles()


@tool_errors
def _select_profile(args, ctx):
    adapter = get_adapter()
    if args.get("kind", "quality") == "custom":
        return adapter.select_custom_profile(args["name"])
    return adapter.select_quality(args["name"])


@tool_errors
def _create_profile(args, ctx):
    return get_adapter().create_profile(args["name"])


@tool_errors
def _save_profile(args, ctx):
    return get_adapter().save_profile()


@tool_errors
def _list_materials(args, ctx):
    materials = get_adapter().list_materials(args.get("extruder_index", 0))
    query = (args.get("query") or "").strip().lower()
    if query:
        materials = [m for m in materials if query in " ".join(
            str(m.get(k) or "") for k in ("id", "name", "type", "series", "brand")).lower()]
    active = [m["id"] for m in materials if m.get("active")]
    return {"materials": materials, "count": len(materials), "active": active[0] if active else None}


@tool_errors
def _set_material(args, ctx):
    return get_adapter().set_material(args["material_id"], args.get("extruder_index", 0))


SUPPORT_EXTRUDER = 1
SOLUBLE_SUPPORT = {"PVA": "растворяется в воде", "HIPS": "растворяется в D-лимонене"}


def _material_record(material_id):
    from .knowledge_tools import _materials
    for record in _materials().get("materials", []):
        if record.get("id") == material_id:
            return record
    return None


def support_pair_notes(main, support):
    """Заметки о совместимости пары «материал детали / материал поддержек» (записи из materials_h530.json)."""
    notes = []
    main_polymer = (main or {}).get("polymer") or ""
    support_polymer = (support or {}).get("polymer") or ""
    if support_polymer in SOLUBLE_SUPPORT:
        notes.append("%s %s — поддержки убираются без следов." % (support_polymer, SOLUBLE_SUPPORT[support_polymer]))
    else:
        notes.append("Отрывные поддержки: другой материал слабо липнет к детали, отделяются легче, чем из того же "
                     "материала.")
    if support_polymer == main_polymer and main_polymer:
        notes.append("Тот же полимер (%s): поддержки сварятся с деталью — смысла в отдельном материале нет, "
                     "если это не растворимый пластик." % main_polymer)
    if support_polymer == "PVA" and main_polymer not in ("PLA", "PETG", "TPU", ""):
        notes.append("PVA печатается при ~200 °C и деградирует выше ~220 °C, а %s требует горячего сопла/стола: "
                     "пара рискованная, лучше HIPS для ABS или отрывные поддержки." % main_polymer)
    if support_polymer == "HIPS" and main_polymer not in ("ABS", "HIPS"):
        notes.append("HIPS — классическая пара для ABS; с %s адгезия и усадка могут не совпасть." % main_polymer)
    main_profile, support_profile = (main or {}).get("profile") or {}, (support or {}).get("profile") or {}
    bed_main, bed_support = main_profile.get("material_bed_temperature"), support_profile.get("material_bed_temperature")
    if bed_main is not None and bed_support is not None and abs(bed_main - bed_support) > 25:
        notes.append("Температуры стола сильно различаются (%s и %s °C): стол один — берётся значение основного "
                     "экструдера, проверьте адгезию поддержек." % (bed_main, bed_support))
    notes.append("Включена черновая башня (prime_tower_enable): она прочищает сопло при смене материала.")
    notes.append("Второй экструдер — bowden со смещением X 25 мм: заправьте в него материал поддержек и "
                 "откалибруйте смещение сопел.")
    return notes


@tool_errors
def _set_support_material(args, ctx):
    adapter = get_adapter()
    mode = adapter.print_mode()
    if args.get("disable"):
        result = _set_settings({"changes": {"support_extruder_nr": 0, "prime_tower_enable": False}}, ctx)
        return {"support_extruder": 0, "settings": result.get("applied"), "errors": result.get("errors")}
    material_id = (args.get("material_id") or "").strip()
    if not material_id:
        raise ToolError("Укажите material_id материала поддержек (или disable=true).")
    if mode in FIVE_D_MODES:
        raise ToolError("Поддержки другим материалом доступны только в 3D (classic): в 5D второй экструдер для "
                        "поддержек не используется. Предложите 3D или поддержки основным материалом.")
    record = _material_record(material_id)
    if record is not None and "только основной" in (record.get("extruders") or ""):
        raise ToolError("%s можно печатать только основным экструдером (0) — для поддержек он не подходит."
                        % record.get("name", material_id))
    getter = adapter.setting_value_getter("global", 0)
    enabled = getter("extruders_enabled_count")
    if enabled is not None and int(enabled) < 2:
        raise ToolError("Второй экструдер выключен (extruders_enabled_count=%s): включите его в слайсере "
                        "(Extruder 2 → Enable) и повторите." % enabled)
    material = adapter.set_material(material_id, SUPPORT_EXTRUDER)
    applied = _set_settings({"changes": {"support_enable": True, "support_extruder_nr": SUPPORT_EXTRUDER,
                                         "prime_tower_enable": True}}, ctx)
    active = [m["id"] for m in adapter.list_materials(0) if m.get("active")]
    main_id = active[0] if active else None
    main_record = _material_record(main_id) if main_id else None
    return {
        "support_extruder": SUPPORT_EXTRUDER, "support_material": material,
        "main_material_id": main_id, "settings": applied.get("applied"), "errors": applied.get("errors") or None,
        "warnings": applied.get("warnings") or None, "notes": support_pair_notes(main_record, record),
    }


@tool_errors
def _set_print_mode(args, ctx):
    adapter = get_adapter()
    mode = args["mode"]
    before = run_in_main_thread(adapter.set_print_mode, mode)
    time.sleep(MODE_SWITCH_DELAY)
    after = run_in_main_thread(adapter.print_mode_report)
    return describe_mode_change(before, after)


def describe_mode_change(before, after):
    """Разница двух состояний режима печати (adapter.set_print_mode / print_mode_report)."""
    mode = after.get("print_mode")
    result = {"print_mode": mode, "title": PRINT_MODE_TITLES.get(mode, mode),
              "previous_mode": before.get("print_mode"), "backend": after.get("backend")}
    if before.get("backend") != after.get("backend"):
        result["backend_changed"] = "%s -> %s" % (before.get("backend"), after.get("backend"))
    if before.get("build_volume_mm") != after.get("build_volume_mm"):
        result["build_volume_mm"] = after.get("build_volume_mm")
        result["previous_build_volume_mm"] = before.get("build_volume_mm")
    changed, became_visible, became_hidden = {}, [], []
    old_settings = before.get("settings") or {}
    for key, state in (after.get("settings") or {}).items():
        old = old_settings.get(key) or {}
        if old.get("value") != state.get("value"):
            changed[key] = "%s -> %s" % (old.get("value"), state.get("value"))
        if state.get("enabled") and not old.get("enabled", False):
            became_visible.append(key)
        if old.get("enabled") and not state.get("enabled"):
            became_hidden.append(key)
    if changed:
        result["changed_settings"] = changed
    if became_visible:
        result["now_active_settings"] = became_visible
    if became_hidden:
        result["now_inactive_settings"] = became_hidden
    result["active_5d_settings"] = dict((k, v.get("value")) for k, v in (after.get("settings") or {}).items()
                                        if v.get("enabled") and (k.startswith("cylindrical") or
                                                                 k.startswith("non_printing")))
    notes = []
    if mode in FIVE_D_MODES:
        notes.append(ATSS_NOTE)
        notes.append("Модель должна стоять осью вращения вертикально на оси стола: после load_model примени "
                     "suggest_orientation (rotate_object steps + move_object frame=scene).")
        if mode == "cylindrical":
            notes.append("5D Spiral: всё внутри cylindrical_mode_base_diameter удаляется (это оснастка/пруток).")
    else:
        notes.append("3D: печать на столе, подогрев стола активен, высота до 300 мм.")
    if after.get("objects_outside"):
        notes.append("Объекты вне рабочей области после смены режима: %s" % ", ".join(after["objects_outside"]))
    result["notes"] = notes
    return result
