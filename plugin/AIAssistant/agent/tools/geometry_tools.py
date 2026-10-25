"""Инструменты геометрии: анализ модели, рекомендация режима печати и ориентация.

Источник — файл (STL / OBJ / 3MF по пути или attachment_id) или объект, который уже на
сцене (object_id). Инструменты работают в рабочем потоке: для объекта сцены в главном
потоке Qt выполняется только получение сетки, сам анализ интерфейс не блокирует. Отчёты
кэширует agent.geometry.analysis, так что несколько инструментов подряд — это один анализ.

Преобразования (rotation_steps вокруг осей СЦЕНЫ + bbox_center_scene) задаются от позы
объекта со сброшенным поворотом, то есть применяются так: rotate_object(steps,
reset_rotation=true), затем move_object(frame=scene, x, z):

* файл — поза сразу после load_model (для 3MF шаги ещё и отменяют поворот, который
  читатель 3MF слайсера записывает в узел, см. geometry/threemf.py);
* объект сцены — сетка берётся с учётом масштаба и без текущего поворота
  (adapter.get_object_local_mesh; если адаптер так не умеет — мировая сетка с отменённым
  поворотом), поэтому шаги подходят именно этому объекту, даже уже повёрнутому.
"""

import os
from array import array
from collections import OrderedDict

from .. import geometry
from ..geometry import vecmath
from ..geometry.mesh import MODEL_EXTENSIONS, MeshError
from ..main_thread import run_in_main_thread
from .registry import ToolError

GROUP = "geometry"
FETCH_TIMEOUT = 120.0

_MODES = ["auto", "classic", "cylindrical", "cylindrical_full"]


def _object(properties, required=None):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_SOURCE = {
    "path": {"type": "string", "description": "Путь к STL/OBJ/3MF"},
    "attachment_id": {"type": "string", "description": "id вложения (по умолчанию последняя модель)"},
    "object_id": {"type": ["string", "integer"], "description": "Объект сцены (id из list_objects)"},
    "part": {"type": "integer", "minimum": 1, "description": "Объект 3MF (по умолчанию крупнейший)"},
    "support_angle": {"type": "number", "minimum": 0, "maximum": 90, "default": geometry.DEFAULT_SUPPORT_ANGLE,
                      "description": "Угол свеса, °"},
}


def register(registry):
    registry.tool(
        "analyze_model",
        "Анализ геометрии модели (файл или объект сцены): габариты, объём, тело вращения (ось, отверстие), "
        "режим печати 3D/5D Spiral/5D с причинами, лучшая поза 3D и постановка 5D. Вызывай первым для новой "
        "модели.",
        _object(dict(_SOURCE, **{
            "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary",
                       "description": "full — все позы и полные трансформации"},
        })),
        main_thread=False, group=GROUP,
    )(_analyze_model)

    registry.tool(
        "recommend_print_mode",
        "Режим печати по геометрии: classic (3D), cylindrical (5D Spiral на оправке по осевому отверстию) или "
        "cylindrical_full (5D: сердечник + цилиндрические слои); уверенность, причины, Base Diameter, постановка.",
        _object(dict(_SOURCE)),
        main_thread=False, group=GROUP,
    )(_recommend_print_mode)

    registry.tool(
        "suggest_orientation",
        "Ориентация для режима: 3D — позы по площади свесов с учётом контакта со столом; 5D — ось вертикально "
        "через центр стола и какой конец вниз.",
        _object(dict(_SOURCE, **{
            "mode": {"type": "string", "enum": _MODES, "default": "auto", "description": "auto — рекомендуемый"},
            "count": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3, "description": "Число поз 3D"},
        })),
        main_thread=False, group=GROUP,
    )(_suggest_orientation)


# Обработчики

def _analyze_model(args, ctx):
    report = _report(args, ctx)
    full = args.get("detail") == "full"
    result = OrderedDict()
    result["source"] = report["file"]
    result["summary"] = _summary_text(report)
    geometry = OrderedDict(report["geometry"])  # отчёт из кэша — копируем, прежде чем дописывать поля
    if geometry.get("surface_cm2"):
        # средняя толщина стенки оболочки: 2 * V / S, мм
        geometry["mean_wall_mm"] = round(20.0 * geometry["volume_cm3"] / geometry["surface_cm2"], 2)
    result["geometry"] = geometry
    result["axis"] = report["axis"]
    result["recommended_mode"] = report["mode"]
    if full:
        result["orientation_3d"] = report["orientation_3d"]
    else:
        result["best_orientation_3d"] = _compact_pose(report["orientation_3d"][0])
    if "placement_5d" in report:
        placement = report["placement_5d"]
        result["placement_5d"] = placement if full else _compact_5d(placement)
    _add_note(result, report)
    result["analysis_time_s"] = report["analysis_time_s"]
    return result


def _recommend_print_mode(args, ctx):
    report = _report(args, ctx)
    mode = report["mode"]
    result = OrderedDict()
    result["source"] = report["file"]["name"]
    result.update(mode)
    result["geometry"] = OrderedDict([
        ("size_file_xyz_mm", report["geometry"]["size_file_xyz_mm"]),
        ("volume_cm3", report["geometry"]["volume_cm3"]),
        ("axis", _axis_text(report["axis"])),
    ])
    if mode["printing_mode"] == "classic" or "placement_5d" not in report:
        result["placement"] = _compact_pose(report["orientation_3d"][0])
    else:
        result["placement"] = _compact_5d(report["placement_5d"])
    _add_note(result, report)
    return result


def _suggest_orientation(args, ctx):
    report = _report(args, ctx)
    mode = args.get("mode") or "auto"
    if mode == "auto":
        mode = report["mode"]["printing_mode"]
    result = OrderedDict()
    result["source"] = report["file"]["name"]
    result["mode"] = mode
    if mode == "classic":
        poses = report["orientation_3d"][:args.get("count", 3)]
        result["candidates"] = [_compact_pose(pose) for pose in poses]
    else:
        if "placement_5d" not in report:
            raise ToolError("Модель не является телом вращения (асимметрия %.2f): постановка для 5D не "
                            "определена. Рекомендуемый режим: %s." % (report["axis"]["asymmetry"],
                                                                     report["mode"]["label"]))
        result["placement"] = _compact_5d(report["placement_5d"])
        result["placement"]["why_this_end"] = report["placement_5d"]["why_this_end"]
        result["axis"] = _axis_text(report["axis"])
    _add_note(result, report)
    return result


# Откуда берём сетку

def _report(args, ctx):
    support_angle = args.get("support_angle", geometry.DEFAULT_SUPPORT_ANGLE)
    if args.get("object_id") is not None and not args.get("path") and not args.get("attachment_id"):
        return _scene_report(args["object_id"], support_angle)
    path = _resolve_path(args, ctx)
    name = os.path.basename(path)
    if not path.lower().endswith(MODEL_EXTENSIONS):
        raise ToolError("Анализ файла поддерживает STL, OBJ и 3MF, а не %s: загрузи модель (load_model) и "
                        "передай object_id" % name)
    try:
        return geometry.analyze_file(path, support_angle, part=args.get("part"))
    except MeshError as exc:
        raise ToolError("Не удалось прочитать %s: %s" % (name, exc))
    except (IOError, OSError) as exc:
        raise ToolError("Не удалось открыть файл %s: %s" % (path, exc))


def _resolve_path(args, ctx):
    path = args.get("path")
    if path:
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            raise ToolError("Файл не найден: %s" % path)
        return path
    uploads = ctx.services.get("uploads")
    attachment_id = args.get("attachment_id")
    if attachment_id:
        record = uploads.get(attachment_id) if uploads is not None else None
        if record is None:
            raise ToolError("Вложение %s не найдено, см. list_attachments" % attachment_id)
        return record["path"]
    if uploads is not None:
        models = [item for item in uploads.list_for_session(ctx.session_id) if item["kind"] == "model"]
        if models:
            return models[-1]["path"]
    raise ToolError("Укажи path, attachment_id или object_id")


def _fetch_scene_object(ref):
    """Главный поток: (вершины, грани, сведения об объекте, вершины_локальные) для объекта сцены."""
    from ..slicer import get_adapter
    adapter = get_adapter()
    info = adapter.object_info(ref, detailed=False)
    local = getattr(adapter, "get_object_local_mesh", None)
    if local is not None:
        try:
            vertices, faces = local(ref)
            return vertices, faces, info, True
        except NotImplementedError:
            pass      # адаптер так не умеет — возьмём мировую сетку и снимем поворот (scene_local_triangles)
    vertices, faces = adapter.get_object_mesh(ref, frame="scene")
    return vertices, faces, info, False


def _scene_report(ref, support_angle):
    from ..slicer import SlicerError
    try:
        vertices, faces, info, is_local = run_in_main_thread(_fetch_scene_object, ref, timeout=FETCH_TIMEOUT)
    except SlicerError as exc:
        raise ToolError(str(exc))
    if not vertices or not faces:
        raise ToolError("У объекта %s нет сетки для анализа" % ref)
    coords = scene_local_triangles(vertices, faces, None if is_local else info.get("rotation"))
    name = info.get("name") or str(ref)
    key = _mesh_key(coords)
    try:
        report = geometry.analyze_triangles(coords, "scene", support_angle, name=name, cache_key=key)
    except MeshError as exc:
        raise ToolError("Не удалось проанализировать объект %s: %s" % (ref, exc))
    report = OrderedDict(report)
    report["file"] = OrderedDict([("object_id", info.get("id", ref)), ("name", name),
                                  ("scale", info.get("scale"))])
    return report


def scene_local_triangles(vertices, faces, rotation=None):
    """Плоский список координат треугольников сетки сцены без поворота узла (оси сцены, Y вверх).

    `rotation` — поворот объекта в виде, как его отдаёт list_objects ({"angle_deg", "axis"},
    ось в координатах принтера); вершины сначала центрируются по габаритам.
    """
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    centre = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, (min(zs) + max(zs)) / 2.0)
    del xs, ys, zs
    inverse = vecmath.IDENTITY
    if rotation and rotation.get("axis") and rotation.get("angle_deg"):
        px, py, pz = rotation["axis"]
        inverse = vecmath.transpose(vecmath.axis_angle_matrix((px, pz, -py), float(rotation["angle_deg"])))
    (a, b, c), (d, e, f), (g, h, i) = inverse
    cx, cy, cz = centre
    local = []
    for v in vertices:
        x = v[0] - cx
        y = v[1] - cy
        z = v[2] - cz
        local.append((a * x + b * y + c * z, d * x + e * y + f * z, g * x + h * y + i * z))
    coords = array("f")
    for face in faces:
        for index in face[:3]:
            coords.extend(local[index])
    return coords


def _mesh_key(coords):
    """Ключ кэша для локальной сетки: не зависит от положения, поворота и id объекта."""
    n = len(coords)
    if not n:
        return (0,)
    bounds = tuple(round(f(coords[k::3]), 1) for k in range(3) for f in (min, max))
    return (n, bounds, round(sum(coords[::97]), 1))


# Компактный вывод для модели

def _add_note(result, report):
    if report["file"].get("format") == "3mf":
        result["note"] = ("3MF: шаги учитывают поворот, который слайсер задаёт объекту при загрузке, — "
                          "только с reset_rotation=true.")


def _compact_pose(pose):
    out = OrderedDict()
    for key in ("description", "up_file", "overhang_mm2", "contact_mm2", "height_mm", "footprint_mm",
                "needs_support"):
        out[key] = pose[key]
    if "transform" in pose:
        out["transform"] = _compact_transform(pose["transform"])
    return out


def _compact_5d(placement):
    out = OrderedDict()
    out["description"] = placement["description"]
    out["up_file"] = placement["up_file"]
    out["height_mm"] = placement["height_mm"]
    out["max_radius_mm"] = placement["max_radius_mm"]
    out["transform"] = _compact_transform(placement["transform"])
    out["other_end_down_steps"] = placement["other_end_down"]["transform"]["rotation_steps"]
    return out


def _compact_transform(transform):
    out = OrderedDict()
    for key in ("rotation_steps", "bbox_center_scene"):
        out[key] = transform[key]
    return out


def _axis_text(axis):
    if not axis.get("is_revolution"):
        return "%s (асимметрия %.2f)" % (axis["verdict"], axis["asymmetry"])
    text = "%s, ось %s через %s, L=%.1f мм, Ø%.1f мм" % (
        axis["verdict"], axis["axis_file"], axis["axis_point_file"], axis["length_mm"], axis["max_diameter_mm"])
    bore = axis.get("bore")
    if bore:
        text += "; " + bore["text"]
    return text


def _summary_text(report):
    geo = report["geometry"]
    mode = report["mode"]
    best = report["orientation_3d"][0]
    size = geo["size_file_xyz_mm"]
    parts = ["%s: %.1f x %.1f x %.1f мм, %.1f см³ (~%.0f г)." % (
        report["file"]["name"], size[0], size[1], size[2], geo["volume_cm3"], geo["mass_g_at_1_2"])]
    parts.append(_axis_text(report["axis"]) + ".")
    parts.append("Режим: %s (%s), уверенность %.0f%%." % (mode["label"], mode["printing_mode"],
                                                          100 * mode["confidence"]))
    if mode["printing_mode"] == "classic" or "placement_5d" not in report:
        parts.append("3D: %s, свесы %d мм², контакт %d мм², высота %.1f мм." % (
            best["description"], best["overhang_mm2"], best["contact_mm2"], best["height_mm"]))
    else:
        parts.append("5D: %s." % report["placement_5d"]["description"])
    if not geo["closed_surface"]:
        parts.append("Внимание: поверхность, похоже, не замкнута.")
    return " ".join(parts)
