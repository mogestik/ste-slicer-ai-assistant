"""Выбор 3D или 5D по прочности: compare_print_strategies.

Анизотропия слоёв FDM (Цай–Хилл, см. geometry/strength.py) под нагрузками из ТЗ для
лучших поз 3D и для цилиндрических слоёв 5D у тела вращения. Результат — оценка, а не
измерение. Работает в рабочем потоке и берёт кэшированный анализ геометрических инструментов.
"""

from collections import OrderedDict

from ..geometry import strength
from . import geometry_tools
from .geometry_tools import _SOURCE, _object
from .knowledge_tools import find_material
from .registry import ToolError

GROUP = "strength"
MIN_SPIRAL_BORE_MM = 4.0
POSES_SHOWN = 2

_MODE_LABELS = {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}

_LOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(strength.LOAD_TYPES),
                 "description": "shaft_bending изгиб вала; torsion кручение; axial_tension вдоль оси; "
                                "centrifugal обороты; hoop_pressure давление/натяг; radial_load нагрузка на обод; "
                                "gear_tooth изгиб зуба; custom — свой direction"},
        "weight": {"type": "number", "exclusiveMinimum": 0, "default": 1, "description": "Важность нагрузки"},
        "direction": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                      "description": "Направление напряжения [x, y, z] в осях файла (custom; для тела без оси "
                                     "и axial_tension)"},
    },
    "required": ["type"],
    "additionalProperties": False,
}


def register(registry):
    registry.tool(
        "compare_print_strategies",
        "Выбор 3D или 5D по прочности под нагрузкой из ТЗ (изгиб, кручение, обороты, зуб...): индекс прочности "
        "с учётом анизотропии слоёв для лучших поз 3D и для 5D Spiral/5D, рекомендация и причина. Вызывай после "
        "analyze_model, ДО set_print_mode, только если в ТЗ есть нагрузка. Оценка, не измерение.",
        _object(dict(_SOURCE, **{
            "loads": {"type": "array", "items": _LOAD_SCHEMA, "minItems": 1,
                      "description": "Нагрузки детали, переведённые из ТЗ"},
            "material_id": {"type": "string", "description": "id материала (класс анизотропии); по умолчанию PETG-подобный"},
            "rpm": {"type": "number", "minimum": 0, "description": "Обороты, об/мин (только для текста)"},
        }), required=["loads"]),
        main_thread=False, group=GROUP,
    )(_compare_print_strategies)


def _compare_print_strategies(args, ctx):
    report = geometry_tools._report(args, ctx)
    record = None
    if args.get("material_id"):
        record = find_material(args["material_id"])
        if record is None:
            raise ToolError("Материал %s не найден, см. list_materials" % args["material_id"])
    klass = strength.material_class(record)
    axis_info = report["axis"]
    revolution = bool(axis_info.get("is_revolution"))
    axis = axis_info.get("axis_direction_file") if revolution else None
    five_d = revolution and "placement_5d" in report
    try:
        data = strength.compare(axis, report["orientation_3d"], args["loads"], klass, five_d)
    except strength.StrengthError as exc:
        raise ToolError(str(exc))

    poses = sorted(data["poses_3d"], key=lambda entry: -round(entry["index"], 2))[:POSES_SHOWN]
    shell = data.get("shell")
    mode = report["mode"]
    bore = axis_info.get("bore") or {}
    spiral_ok = bool(five_d and bore.get("through_bore") and bore.get("bore_min_diameter_mm", 0) >= MIN_SPIRAL_BORE_MM)
    choice = strength.recommend(poses[0], shell, mode["printing_mode"], spiral_ok)

    result = OrderedDict()
    result["source"] = report["file"]["name"]
    kz, ks, label = strength.MATERIAL_CLASSES[klass][0], strength.MATERIAL_CLASSES[klass][1], \
        strength.MATERIAL_CLASSES[klass][2]
    result["basis"] = ("[оценка] Анизотропия FDM по Цаю–Хиллу, класс материала: %s (k_z=%.2f, k_s=%.2f); типичные "
                       "значения, не измерения. 1.0 = прочность вдоль слоя." % (label, kz, ks))
    result["loads"] = [_load_text(load) for load in data["loads"]]
    if data["skipped"]:
        result["skipped_loads"] = data["skipped"]
    variants = [_row_3d(entry) for entry in poses]
    standing = _standing_pose(data["poses_3d"], poses, axis)
    if standing is not None:
        row = _row_3d(standing)
        row["variant"] += " (для сравнения: ось детали вертикально)"
        variants.append(row)
    if spiral_ok:
        variants.append(_row_5d("cylindrical", shell, "5D Spiral на оправке Ø%.1f мм" % bore["bore_min_diameter_mm"]))
    if five_d:
        variants.append(_row_5d("cylindrical_full", shell, "5D (сердечник Ø6 + цилиндрические слои)"))
    else:
        variants.append(OrderedDict([("variant", "5D"), ("note", "неприменимо: нет оси вращения")]))
    result["variants"] = variants
    if shell is not None:
        result["core_note"] = ("Сердечник Ø6 мм в 5D печатается плоскими слоями (индекс %.2f), но изгиб и кручение "
                               "максимальны на внешних волокнах — оценка по оболочке." % data["core"]["index"])
    result["recommended"] = _recommended(choice, poses[0], mode)
    result["why"] = _why(choice, poses[0], shell, data["loads"], args.get("rpm"))
    return result


# -- вывод ----------------------------------------------------------------------------

def _load_text(load):
    return "%s (%s), вес %g" % (load["type"], load["label"], load["weight"])


def _standing_pose(all_poses, shown, axis):
    """Лучшая поза с вертикальной осью вращения, если её нет среди показанных."""
    if axis is None:
        return None
    standing = [entry for entry in all_poses if entry not in shown
                and abs(sum(a * b for a, b in zip(axis, entry["pose"]["up_file"]))) > 0.95]
    return max(standing, key=lambda entry: entry["index"]) if standing else None


def _row_3d(entry):
    pose = entry["pose"]
    row = OrderedDict()
    row["variant"] = "3D: " + pose["description"]
    row["index"] = round(entry["index"], 2)
    row["worst_load"] = "%s %.2f" % (entry["worst_load"], entry["worst_value"])
    row["supports"] = "да" if pose["needs_support"] else "нет"
    row["overhang_share"] = pose["overhang_share"]
    row["up_file"] = pose["up_file"]
    row["per_load"] = _rounded(entry["per_load"])
    return row


def _row_5d(mode, shell, title):
    row = OrderedDict()
    row["variant"] = title
    row["printing_mode"] = mode
    row["index"] = round(shell["index"], 2)
    row["worst_load"] = "%s %.2f" % (shell["worst_load"], shell["worst_value"])
    row["supports"] = "нет"
    row["overhang_share"] = 0
    row["per_load"] = _rounded(shell["per_load"])
    return row


def _rounded(per_load):
    return OrderedDict((key, round(value, 2)) for key, value in per_load.items())


def _recommended(choice, best_3d, mode):
    out = OrderedDict()
    out["printing_mode"] = choice["choice"]
    out["label"] = _MODE_LABELS[choice["choice"]]
    out["decided_by"] = {"strength": "прочность", "geometry": "геометрия (поддержки, свесы)",
                         "no_5d": "нет оси вращения"}[choice["basis"]]
    out["geometry_mode"] = mode["printing_mode"]
    if choice["choice"] == "classic":
        out["pose"] = best_3d["pose"]["description"]
        out["up_file"] = best_3d["pose"]["up_file"]
    return out


def _why(choice, best_3d, shell, loads, rpm):
    names = ", ".join(load["label"] for load in loads)
    i3 = best_3d["index"]
    pose = best_3d["pose"]["description"]
    if shell is None:
        text = ("Нагрузки: %s. Тела вращения нет, 5D неприменимо; лучшая поза 3D (%s) — индекс %.2f, "
                "слабее всего: %s %.2f." % (names, pose, i3, best_3d["worst_load"], best_3d["worst_value"]))
    else:
        i5 = shell["index"]
        label = _MODE_LABELS[choice["choice"]]
        if choice["basis"] == "strength":
            if choice["choice"] == "classic":
                text = ("Нагрузки: %s. Индекс 3D (%s) %.2f против %.2f у 5D — 3D прочнее: слои 5D ложатся "
                        "поперёк слабого направления (%s %.2f)." % (names, pose, i3, i5, shell["worst_load"],
                                                                   shell["worst_value"]))
            else:
                text = ("Нагрузки: %s. Индекс 5D %.2f против %.2f у 3D (%s): в 3D слабее всего %s (%.2f) — "
                        "напряжение идёт поперёк слоёв, в 5D слои цилиндрические и повторяют линии нагрузки."
                        % (names, i5, i3, pose, best_3d["worst_load"], best_3d["worst_value"]))
        else:
            text = ("Нагрузки: %s. Прочность близка (3D %.2f, 5D %.2f), решает геометрия: %s." % (
                names, i3, i5, label))
        if choice["compromise"]:
            if choice["choice"] == "classic":
                text += " Компромисс: 5D прочнее на %.2f, но 3D выбран по геометрии." % abs(i5 - i3)
            else:
                weak = shell["worst_load"]
                text += (" Компромисс: %s — без поддержек, ценой прочности (%s: %.2f против %.2f у 3D)." % (
                    label, weak, shell["worst_value"], best_3d["per_load"][weak]))
    if rpm:
        text += " Обороты %g об/мин: центробежная нагрузка учтена качественно, расчёт на разрыв не делался." % rpm
    return text + " [оценка]"
