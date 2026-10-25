"""Инструменты сцены: загрузка моделей, список объектов, перемещение, поворот, масштаб.

Положения и размеры по умолчанию — в координатах ПРИНТЕРА, мм, как в интерфейсе слайсера:
X вправо, Y назад, Z вверх, начало — центр стола на его поверхности. Сама сцена слайсера
Y-up (X вправо, Y вверх, Z к передней стороне; см. agent/slicer/coords.py). rotate_object
по умолчанию работает в осях СЦЕНЫ, потому что модуль геометрии (analyze_model,
suggest_orientation) выдаёт rotation_steps и кватернионы в сцене; у move/scale/rotate
систему координат можно сменить параметром frame. В 5D ось вращения — вертикаль
X=0, Y=0 у принтера, то есть X=0, Z=0 в сцене.
"""

import os

from ..slicer import coords, get_adapter
from ..slicer.base import tool_errors
from .registry import ToolError

GROUP = "scene"
LOAD_TIMEOUT = 180.0
MODEL_EXTENSIONS = (".stl", ".obj", ".3mf", ".amf", ".ply", ".x3d", ".gltf", ".glb", ".dae")

COORDINATES_NOTE = ("Координаты принтера, мм: X вправо, Y назад, Z вверх; 0,0 — центр стола; z — высота низа "
                    "объекта над столом. В 5D ось вращения — вертикаль X=0, Y=0.")

# Обе системы координат один раз объяснены в системном промпте (prompts/system_ru.md).
_FRAME_PRINTER = {"type": "string", "enum": ["printer", "scene"], "default": "printer"}

_OBJECT_ID = {
    "type": ["string", "integer"],
    "description": "id из list_objects (или уникальное имя)",
}


def _object(properties, required=None):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def register(registry):
    registry.tool(
        "load_model",
        "Загрузить модель (STL/OBJ/3MF) по абсолютному пути (вложения — путь из сообщения или "
        "list_attachments). Кладёт на стол; возвращает id, габариты, объём. В 5D одиночная модель ставится на ось.",
        _object({
            "path": {"type": "string"},
            "clear_scene": {"type": "boolean", "default": False, "description": "удалить остальные объекты"},
            "center_for_5d": {"type": "boolean", "description": "центр на ось 5D; по умолчанию — в режимах 5D"},
        }, ["path"]), main_thread=False, group=GROUP,
    )(_load_model)

    registry.tool(
        "list_objects",
        "Объекты на столе: id, имя, центр, размеры, низ над столом, поворот, масштаб, выход за область, на "
        "оси 5D ли (координаты принтера).",
        _object({}), main_thread=True, group=GROUP,
    )(_list_objects)

    registry.tool(
        "get_object_dimensions",
        "Габариты объекта: размеры, min/max, объём, контакт со столом, радиус от оси 5D, треугольники.",
        _object({"object_id": _OBJECT_ID}, ["object_id"]), main_thread=True, group=GROUP,
    )(_get_object_dimensions)

    registry.tool(
        "move_object",
        "Переместить объект, мм. frame=printer: x, y — центр габаритов, z — низ над столом (не задавай — "
        "ляжет на стол). frame=scene: x, z — центр по горизонтали (bbox_center_scene), y — низ. "
        "relative=true — смещение.",
        _object({
            "object_id": _OBJECT_ID,
            "x": {"type": "number"},
            "y": {"type": "number"},
            "z": {"type": "number"},
            "relative": {"type": "boolean", "default": False},
            "frame": _FRAME_PRINTER,
        }, ["object_id"]), main_thread=True, group=GROUP,
    )(_move_object)

    registry.tool(
        "rotate_object",
        "Повернуть объект: steps (rotation_steps из analyze_model/suggest_orientation), axis+angle_deg или "
        "quaternion_xyzw; оси сцены по умолчанию (frame=scene), угол против часовой с конца оси. "
        "reset_rotation=true — сначала исходная ориентация файла (обязательно для steps из анализа). После "
        "поворота объект на столе, центр сохраняется. lay_flat=true — наибольшей гранью на стол.",
        _object({
            "object_id": _OBJECT_ID,
            "axis": {"type": "string", "enum": ["x", "y", "z"]},
            "angle_deg": {"type": "number", "minimum": -360, "maximum": 360},
            "steps": {"type": "array", "maxItems": 10,
                      "items": {"type": "object", "properties": {
                          "axis": {"type": "string", "enum": ["x", "y", "z"]},
                          "angle_deg": {"type": "number", "minimum": -360, "maximum": 360}},
                          "required": ["axis", "angle_deg"]}},
            "quaternion_xyzw": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            "frame": {"type": "string", "enum": ["scene", "printer"], "default": "scene"},
            "reset_rotation": {"type": "boolean", "default": False},
            "center_for_5d": {"type": "boolean", "default": False, "description": "затем центр на ось 5D"},
            "lay_flat": {"type": "boolean", "default": False},
        }, ["object_id"]), main_thread=True, group=GROUP,
    )(_rotate_object)

    registry.tool(
        "scale_object",
        "Масштаб относительно исходного файла (1.0 — исходный): factor — равномерно, или x/y/z по осям файла "
        "(неуказанные не меняются). Объект остаётся на столе.",
        _object({
            "object_id": _OBJECT_ID,
            "factor": {"type": "number", "minimum": 0.01, "maximum": 100},
            "x": {"type": "number", "minimum": 0.01, "maximum": 100},
            "y": {"type": "number", "minimum": 0.01, "maximum": 100},
            "z": {"type": "number", "minimum": 0.01, "maximum": 100},
            "frame": _FRAME_PRINTER,
        }, ["object_id"]), main_thread=True, group=GROUP,
    )(_scale_object)

    registry.tool(
        "center_for_5d",
        "Центр габаритов на ось 5D (0, 0), низ на стол; ориентацию задать до. Несимметричные детали — "
        "move_object(frame=scene) по bbox_center_scene. Без object_id — единственный объект.",
        _object({"object_id": _OBJECT_ID}), main_thread=True, group=GROUP,
    )(_center_for_5d)

    registry.tool(
        "arrange_all",
        "Авторасстановка всех объектов на столе (3D).",
        _object({}), main_thread=False, group=GROUP,
    )(_arrange_all)

    registry.tool(
        "duplicate_object",
        "Партия: добавить copies копий объекта и расставить на столе (3D). Отвечает, сколько поместилось "
        "(inside_build_area) и какие копии вне стола — их удали и скажи, сколько столов нужно.",
        _object({
            "object_id": _OBJECT_ID,
            "copies": {"type": "integer", "minimum": 1, "maximum": 50, "description": "сколько копий добавить"},
        }, ["copies"]), main_thread=False, group=GROUP,
    )(_duplicate_object)

    registry.tool(
        "delete_objects",
        "Удалить объекты: object_ids или all=true.",
        _object({
            "object_ids": {"type": "array", "items": _OBJECT_ID},
            "all": {"type": "boolean", "default": False},
        }), main_thread=True, group=GROUP,
    )(_delete_objects)


@tool_errors
def _load_model(args, ctx):
    path = os.path.expanduser((args.get("path") or "").strip().strip('"'))
    if not path:
        raise ToolError("Не указан путь к файлу модели")
    if not os.path.isabs(path):
        raise ToolError("Нужен абсолютный путь к файлу (см. list_attachments): %s" % path)
    if not os.path.isfile(path):
        raise ToolError("Файл не найден: %s" % path)
    if not path.lower().endswith(MODEL_EXTENSIONS):
        raise ToolError("Формат %s не похож на 3D-модель (ожидается STL, OBJ или 3MF)" % os.path.splitext(path)[1])
    result = get_adapter().load_model(path, clear_scene=bool(args.get("clear_scene")), timeout=LOAD_TIMEOUT,
                                      cancel_event=ctx.cancel_event, center=args.get("center_for_5d"))
    result["coordinates"] = COORDINATES_NOTE
    return result


@tool_errors
def _list_objects(args, ctx):
    adapter = get_adapter()
    objects = adapter.list_objects()
    return {"objects": objects, "count": len(objects), "print_mode": adapter.print_mode(),
            "coordinates": COORDINATES_NOTE}


@tool_errors
def _get_object_dimensions(args, ctx):
    return get_adapter().object_info(args["object_id"], detailed=True)


@tool_errors
def _move_object(args, ctx):
    x, y, z = args.get("x"), args.get("y"), args.get("z")
    relative = bool(args.get("relative"))
    if args.get("frame") == "scene":
        # сцена (x, y вверх, z вперёд) -> принтер (x, y назад = -z, z вверх = y)
        x, y, z = x, (None if z is None else -z), y
    if x is None and y is None and z is None and relative:
        raise ToolError("Укажи смещение x, y и/или z")
    return get_adapter().move_object(args["object_id"], x=x, y=y, z=z, relative=relative)


@tool_errors
def _rotate_object(args, ctx):
    adapter = get_adapter()
    ref = args["object_id"]
    if args.get("lay_flat"):
        return adapter.lay_flat(ref)
    frame = args.get("frame") or "scene"
    try:
        if args.get("quaternion_xyzw"):
            matrix = coords.quaternion_to_matrix(args["quaternion_xyzw"])
            if frame == "printer":
                matrix = coords.printer_matrix_to_scene(matrix)
        else:
            steps = [(step["axis"], step["angle_deg"]) for step in args.get("steps") or []]
            if args.get("axis") and args.get("angle_deg") is not None:
                steps.append((args["axis"], args["angle_deg"]))
            if not steps and not args.get("reset_rotation"):
                raise ToolError("Укажи axis и angle_deg, steps или quaternion_xyzw (либо lay_flat=true)")
            matrix = coords.rotation_from_steps(steps, frame)
    except ValueError as exc:
        raise ToolError("Некорректный поворот: %s" % exc)
    center = (0.0, 0.0) if args.get("center_for_5d") else None
    return adapter.apply_rotation_matrix(ref, matrix, frame="scene", drop_to_bed=True, center_xy=center,
                                         reset=bool(args.get("reset_rotation")))


@tool_errors
def _scale_object(args, ctx):
    adapter = get_adapter()
    factor = args.get("factor")
    per_axis = [args.get(axis) for axis in ("x", "y", "z")]
    if args.get("frame") == "scene":
        per_axis = [per_axis[0], per_axis[2], per_axis[1]]   # сцена (x, y вверх, z) -> принтер (x, y, z вверх)
    if factor is None and all(value is None for value in per_axis):
        raise ToolError("Укажи factor или хотя бы одну из осей x, y, z")
    if factor is not None:
        factors = [float(factor)] * 3
    else:
        current = adapter.object_info(args["object_id"], detailed=False).get("scale") or [1.0, 1.0, 1.0]
        factors = [float(value) if value is not None else float(current[i]) for i, value in enumerate(per_axis)]
    return adapter.scale_object(args["object_id"], tuple(factors))


@tool_errors
def _center_for_5d(args, ctx):
    return get_adapter().center_for_5d(args.get("object_id"))


@tool_errors
def _duplicate_object(args, ctx):
    return get_adapter().duplicate_object(args.get("object_id"), args["copies"])


@tool_errors
def _arrange_all(args, ctx):
    return get_adapter().arrange_all()


@tool_errors
def _delete_objects(args, ctx):
    ids = args.get("object_ids") or []
    if not ids and not args.get("all"):
        raise ToolError("Укажи object_ids или all=true")
    return get_adapter().delete_objects(ids, delete_all=bool(args.get("all")))
