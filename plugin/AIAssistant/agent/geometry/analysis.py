"""Анализ модели для агента: сводка геометрии, ось вращения, режим печати, позы.

`analyze_mesh(mesh)` возвращает словарь, готовый для JSON (числа округлены, пояснения
по-русски). `analyze_file(path)` добавляет кэш в памяти: одну и ту же модель обычно
анализируют несколько инструментов подряд.

Как выбираем режим (правила проверены на 7 моделях кейса, см. tests/test_geometry.py):

1. не тело вращения -> classic (3D);
2. плоский диск или кольцо (L/D < 0.2) -> classic: лежит плашмя, 5D ничего не даёт;
3. в лучшей 3D-позе свесов (почти) нет -> classic;
4. нет ступицы у оси (тонкая оболочка, кольцо) -> classic: на оправке не удержится, а
   сердечнику 5D не на что опереться;
5. круглое сквозное отверстие держит деталь на большей части длины -> cylindrical
   (5D Spiral), Base Diameter = диаметр отверстия;
6. иначе (сплошной вал или ступица на оси, ступенчатое отверстие или со шпонкой) ->
   cylindrical_full (5D): сердечник плоскими слоями, остальное цилиндрическими.
"""

import os
import threading
import time
from collections import OrderedDict

from . import mesh as mesh_module
from . import orientation, symmetry, vecmath

DEFAULT_SUPPORT_ANGLE = 50.0      # support_angle по умолчанию в fdmprinter
MATERIAL_DENSITY = 1.2            # г/см³, грубая оценка для PLA/PETG/ABS
MIN_BASE_DIAMETER = 4.0           # минимум cylindrical_mode_base_diameter
DEFAULT_BASE_DIAMETER = 6.0       # штатный пруток в цанге ER11
# Пороги для выбора режима.
REVOLUTION_ROUND_FRACTION = 0.4   # доля длины с круглыми сечениями
FLAT_DISC_LD = 0.2
NO_OVERHANG_SHARE = 0.01
HUB_RADIUS_SHARE = 0.3            # ступица: внутренняя стенка ближе 30% внешнего радиуса
HUB_LENGTH_SHARE = 0.4
MANDREL_SUPPORT_SHARE = 0.6
ROUND_BORE = 0.95
# Рабочие объёмы, мм: область 3D и эллипс/высота 5D.
VOLUME_3D = (320.0, 300.0, 300.0)
VOLUME_5D_RADIUS = 150.0
VOLUME_5D_HEIGHT = 231.1

MODE_LABELS = {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}

_cache = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 6


def analyze_file(path, support_angle=DEFAULT_SUPPORT_ANGLE, part=None):
    """Анализ файла STL / OBJ / 3MF (кэш по пути, размеру, времени изменения, углу поддержек и части 3MF)."""
    path = os.path.abspath(path)
    stat = os.stat(path)
    key = (path, stat.st_size, stat.st_mtime, round(float(support_angle), 2), part)

    def compute():
        started = time.time()
        mesh = mesh_module.read_mesh(path, part)
        report = analyze_mesh(mesh, support_angle, started=started)
        info = OrderedDict([("name", os.path.basename(path)), ("size_mb", round(stat.st_size / 1048576.0, 2))])
        info.update(mesh.source)
        report["file"] = info
        return report

    return _cached(key, compute)


def analyze_triangles(coords, frame="scene", support_angle=DEFAULT_SUPPORT_ANGLE, name="", cache_key=None):
    """Анализ «голых» треугольников: плоский список координат вершин граней (9 чисел на треугольник).

    Для объектов, которые уже на сцене (tools/geometry_tools.py): передаётся сетка узла в
    координатах сцены БЕЗ поворота (с масштабом), чтобы преобразования считались от
    сброшенной позы, как ждёт rotate_object(reset_rotation=true). `cache_key` (хешируемый)
    включает кэш отчётов.
    """
    def compute():
        report = analyze_mesh(mesh_module.mesh_from_triangles(coords, frame, name), support_angle)
        report["file"] = OrderedDict([("name", name)])
        return report

    if cache_key is None:
        return compute()
    return _cached(("triangles", cache_key, round(float(support_angle), 2)), compute)


def _cached(key, compute):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    report = compute()
    with _cache_lock:
        _cache[key] = report
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return report


def analyze_mesh(mesh, support_angle=DEFAULT_SUPPORT_ANGLE, started=None):
    started = started or time.time()
    mesh.compute()
    fit, all_fits = symmetry.find_axis(mesh)
    revolution = fit.round_fraction >= REVOLUTION_ROUND_FRACTION
    bore = symmetry.analyse_bore(mesh, fit) if revolution else None
    poses = orientation.rank_poses(mesh, support_angle, fit.direction if revolution else None)
    best_3d = poses[0]

    report = OrderedDict()
    report["geometry"] = _geometry(mesh)
    report["axis"] = _axis_summary(mesh, fit, revolution, bore)
    mode = _recommend_mode(mesh, fit, revolution, bore, best_3d)
    report["mode"] = mode
    report["orientation_3d"] = [_pose_summary(mesh, pose) for pose in poses[:8]]
    if revolution:
        report["placement_5d"] = _placement_5d(mesh, fit, bore, support_angle, mode)
    report["support_angle_deg"] = support_angle
    report["frames"] = ("Файл: координаты STL (Z вверх). Сцена Cura: Y вверх, X вправо, Z к зрителю; "
                        "STL-вершина (x, y, z) загружается как (x, z, -y). Повороты rotation_steps — вокруг "
                        "осей СЦЕНЫ по порядку, градусы, положительный угол против часовой стрелки, если смотреть "
                        "с конца оси. node_position — абсолютная позиция ноды (её начало = центр bbox исходного "
                        "меша) для SetTransformOperation вместе с quaternion_xyzw; bbox_center_scene — куда "
                        "встанет центр bbox (модель стоит на столе, низ на Y=0). Принтер: X=X сцены, Y=-Z сцены, "
                        "Z=Y сцены.")
    report["analysis_time_s"] = round(time.time() - started, 2)
    return report


# Разделы отчёта

def _geometry(mesh):
    size = vecmath.sub(mesh.bbox_max, mesh.bbox_min)
    return OrderedDict([
        ("triangles", mesh.triangle_count),
        ("size_file_xyz_mm", _r(size, 2)),
        ("bbox_min_file", _r(mesh.bbox_min, 2)),
        ("bbox_max_file", _r(mesh.bbox_max, 2)),
        ("volume_cm3", round(mesh.volume / 1000.0, 2)),
        ("surface_cm2", round(mesh.area / 100.0, 1)),
        ("mass_g_at_1_2", round(mesh.volume / 1000.0 * MATERIAL_DENSITY, 1)),
        ("center_of_mass_file", _r(mesh.center_of_mass, 2)),
        ("closed_surface", mesh.closure_error < 1e-3 and not mesh.degenerate > 0.01 * mesh.triangle_count),
        ("inverted_normals", mesh.inverted),
    ])


def _axis_summary(mesh, fit, revolution, bore):
    length = fit.h_range[1] - fit.h_range[0]
    diameter = 2.0 * fit.r_max
    result = OrderedDict()
    result["is_revolution"] = revolution
    if fit.round_fraction >= 0.9 and fit.score < 0.02:
        verdict = "тело вращения"
    elif revolution:
        verdict = "частично тело вращения (круглые сечения на %d%% длины)" % round(100 * fit.round_fraction)
    else:
        verdict = "не тело вращения"
    result["verdict"] = verdict
    result["asymmetry"] = round(fit.score, 3)
    result["round_length_share"] = round(fit.round_fraction, 2)
    if not revolution:
        result["closest_axis_file"] = _axis_name(fit.direction)
        return result
    result["axis_file"] = _axis_name(fit.direction)
    result["axis_direction_file"] = _r(fit.direction, 3)
    result["axis_point_file"] = _r(fit.point, 2)
    result["length_mm"] = round(length, 2)
    result["max_diameter_mm"] = round(diameter, 2)
    result["length_to_diameter"] = round(length / diameter, 2) if diameter else None
    if bore is not None:
        b = OrderedDict()
        b["axis_material_share"] = round(bore.axis_material_fraction, 2)
        b["through_bore"] = bore.through_bore
        if bore.through_bore:
            b["bore_min_diameter_mm"] = round(bore.bore_diameter, 2)
            b["bore_wall_diameter_mm"] = _round_or_none(bore.bore_circle_diameter, 2)
            b["bore_roundness"] = _round_or_none(bore.roundness, 2)
            b["bore_support_share"] = round(bore.support_fraction, 2)
        b["hub_length_share"] = round(_hub_share(fit, bore), 2)
        inner = _hub_inner_diameters(fit) if bore.through_bore else None
        if inner:
            b["hub_inner_diameter_range_mm"] = [round(inner[0], 1), round(inner[1], 1)]
        if bore.through_bore:
            text = "сквозное осевое отверстие Ø%.1f мм" % bore.bore_diameter
            if bore.roundness is not None and bore.roundness < ROUND_BORE:
                text += " (некруглое: лыски/шпонка/ступени, стенка до Ø%.1f)" % bore.bore_circle_diameter
            text += ", стенка отверстия на %d%% длины" % round(100 * bore.support_fraction)
        elif bore.axis_material_fraction > 0.95:
            text = "по оси сплошной материал (вал/ступица без отверстия)"
        else:
            text = "по оси материал на %d%% длины (глухое отверстие или полость)" % round(
                100 * bore.axis_material_fraction)
        b["text"] = text
        result["bore"] = b
    return result


def _hub_inner_diameters(fit):
    """(мин, макс) диаметра внутренней стенки на участке ступицы."""
    limit = HUB_RADIUS_SHARE * fit.r_max
    values = []
    for row in fit.rmin_grid:
        finite = [value for value in row if value != float("inf")]
        if finite and min(finite) <= limit:
            values.append(2.0 * min(finite))
    return (min(values), max(values)) if values else None


def _hub_share(fit, bore):
    """Доля длины вдоль оси, где материал лежит на оси или рядом с ней.

    Облако точек покрывает только поверхность, поэтому материал прямо на оси (сплошной
    вал) определяем лучами.
    """
    limit = HUB_RADIUS_SHARE * fit.r_max
    bands = hub = 0
    for row in fit.rmin_grid:
        finite = [value for value in row if value != float("inf")]
        if not finite:
            continue
        bands += 1
        if min(finite) <= limit:
            hub += 1
    share = hub / float(bands) if bands else 0.0
    return max(share, bore.axis_material_fraction)


def _recommend_mode(mesh, fit, revolution, bore, best_3d):
    length = fit.h_range[1] - fit.h_range[0]
    diameter = 2.0 * fit.r_max
    ld = length / diameter if diameter else 0.0
    overhang_share = best_3d.overhang / mesh.area if mesh.area else 0.0
    reasons = []
    settings = OrderedDict()
    alternatives = []

    def result(mode, confidence):
        out = OrderedDict()
        out["printing_mode"] = mode
        out["label"] = MODE_LABELS[mode]
        out["confidence"] = round(confidence, 2)
        out["reasons"] = reasons
        if settings:
            out["settings"] = settings
        if alternatives:
            out["alternatives"] = alternatives
        out["fits_build_volume"] = _fits(mesh, fit, best_3d, mode)
        return out

    if not revolution:
        reasons.append("Не тело вращения: круглые сечения только на %d%% длины лучшей оси (%s), "
                       "асимметрия %.2f." % (round(100 * fit.round_fraction), _axis_name(fit.direction), fit.score))
        reasons.append("5D-слои цилиндрические вокруг оси — для такой формы выигрыша нет.")
        return result("classic", 0.9 if fit.round_fraction < 0.2 else 0.7)

    reasons.append("Тело вращения вокруг оси %s (круглые сечения на %d%% длины), L/D = %.2f."
                   % (_axis_name(fit.direction), round(100 * fit.round_fraction), ld))
    if ld < FLAT_DISC_LD:
        reasons.append("Плоский диск/кольцо (L/D < %.1f): в 3D печатается плашмя, 5D не даёт выигрыша."
                       % FLAT_DISC_LD)
        alternatives.append("5D возможен, но слои поперёк тонкого диска — без пользы.")
        return result("classic", 0.8)
    if overhang_share < NO_OVERHANG_SHARE:
        reasons.append("В 3D есть поза почти без свесов (%.0f мм², %.1f%% поверхности): поддержки не нужны, "
                       "5D не требуется." % (best_3d.overhang, 100 * overhang_share))
        if bore is not None and bore.through_bore and bore.bore_diameter < DEFAULT_BASE_DIAMETER:
            reasons.append("Осевое отверстие Ø%.1f мм меньше штатной оправки %.0f мм."
                           % (bore.bore_diameter, DEFAULT_BASE_DIAMETER))
        return result("classic", 0.75)
    hub = _hub_share(fit, bore)
    if hub < HUB_LENGTH_SHARE:
        reasons.append("Ступицы у оси нет (материал близко к оси только на %d%% длины): тонкостенная "
                       "оболочка/кольцо, оправка её не держит, сердечнику не за что зацепиться."
                       % round(100 * hub))
        alternatives.append("5D Spiral возможен только на специальной оснастке по внутренней форме.")
        return result("classic", 0.7)
    if bore.through_bore and bore.bore_diameter >= MIN_BASE_DIAMETER \
            and (bore.roundness or 0) >= ROUND_BORE and bore.support_fraction >= MANDREL_SUPPORT_SHARE:
        base = round(bore.bore_diameter, 1)
        reasons.append("Сквозное круглое осевое отверстие Ø%.1f мм по %d%% длины: деталь надевается на "
                       "оправку, отверстие формируется оправкой, слои идут по окружности."
                       % (bore.bore_diameter, round(100 * bore.support_fraction)))
        settings["cylindrical_mode_base_diameter"] = base
        if base > 8.0:
            reasons.append("Оправка Ø%.1f больше прутка цанги ER11 (до 8 мм) — нужна оснастка этого диаметра."
                           % base)
        alternatives.append("5D (cylindrical_full) — если оснастки Ø%.1f нет." % base)
        confidence = 0.85 if bore.support_fraction >= 0.9 and bore.roundness >= 0.98 else 0.7
        return result("cylindrical", confidence)

    if bore.through_bore:
        if (bore.roundness or 0) < ROUND_BORE:
            reasons.append("Осевое отверстие Ø%.1f мм некруглое (лыски/шпонка/ступени): на гладкую оправку "
                           "деталь не встанет." % bore.bore_diameter)
        else:
            reasons.append("Осевое отверстие Ø%.1f мм держит деталь только на %d%% длины (ступица/буртик), "
                           "оправкой его не сформировать." % (bore.bore_diameter, round(100 * bore.support_fraction)))
        reasons.append("Есть ступица у оси: сердечник печатается плоскими слоями, остальное — цилиндрическими.")
        settings["cylindrical_mode_base_diameter"] = DEFAULT_BASE_DIAMETER
        if bore.bore_diameter >= DEFAULT_BASE_DIAMETER:
            reasons.append("Сердечник Ø%.0f меньше отверстия Ø%.1f и модель не пересечёт: слайсер напечатает "
                           "его как опору; Base Diameter больше отверстия срежет его стенку."
                           % (DEFAULT_BASE_DIAMETER, bore.bore_diameter))
        confidence = 0.7
    else:
        reasons.append("По оси сплошной материал (вал/ступица, %d%% длины): сердечник Ø%.0f мм печатается "
                       "плоскими слоями, остальное — цилиндрическими (5D)."
                       % (round(100 * bore.axis_material_fraction), DEFAULT_BASE_DIAMETER))
        settings["cylindrical_mode_base_diameter"] = DEFAULT_BASE_DIAMETER
        confidence = 0.8 if fit.round_fraction >= 0.8 else 0.7
    if fit.round_fraction < 0.8:
        reasons.append("Часть детали не осесимметрична (%d%% длины, например кулачок/лопасти) — "
                       "проверить превью слоёв." % round(100 * (1 - fit.round_fraction)))
    reasons.append("В 3D лучшая поза даёт свесы %.0f мм² (%.0f%% поверхности)."
                   % (best_3d.overhang, 100 * overhang_share))
    alternatives.append("3D: %s, свесы %.0f мм²." % (best_3d.source, best_3d.overhang))
    return result("cylindrical_full", confidence)


def _fits(mesh, fit, best_3d, mode):
    if mode == "classic":
        size = sorted(best_3d.footprint, reverse=True)
        return size[0] <= VOLUME_3D[0] and size[1] <= VOLUME_3D[1] and best_3d.height <= VOLUME_3D[2]
    length = fit.h_range[1] - fit.h_range[0]
    return fit.r_max <= VOLUME_5D_RADIUS and length <= VOLUME_5D_HEIGHT


def _pose_summary(mesh, pose):
    out = OrderedDict()
    out["up_file"] = _r(pose.up, 3)
    out["description"] = pose.source
    out["overhang_mm2"] = round(pose.overhang)
    out["overhang_share"] = round(pose.overhang / mesh.area, 3) if mesh.area else 0
    out["contact_mm2"] = round(pose.contact)
    out["height_mm"] = round(pose.height, 1)
    out["footprint_mm"] = _r(pose.footprint, 1)
    out["needs_support"] = pose.overhang > max(0.01 * mesh.area, 20.0)
    out["score"] = round(pose.score)
    out["transform"] = orientation.placement(mesh, pose)
    return out


def _placement_5d(mesh, fit, bore, support_angle, mode):
    """Ставит ось вертикально через X=Z=0 сцены и выбирает, каким концом вниз."""
    options = []
    for sign in (1.0, -1.0):
        up = vecmath.scale(fit.direction, sign)
        pose = orientation.evaluate_pose(mesh, up, "ось вверх" if sign > 0 else "ось вверх другим концом",
                                         support_angle)
        # Если на нижнем конце есть материал на оси, сердечник 5D начнётся прямо с основания.
        if bore is None:
            core_bottom = False
        else:
            core_bottom = bore.material_at_start if sign > 0 else bore.material_at_end
        rotation = vecmath.angle_deg(pose.up, (0.0, 0.0, 1.0))
        dominant = max(range(3), key=lambda i: abs(pose.up[i]))
        positive = pose.up[dominant] > 0
        options.append((not core_bottom if mode == "cylindrical_full" else False,
                        round(rotation, 1), not positive, pose.overhang, pose, core_bottom))
    options.sort(key=lambda item: item[:4])
    chosen = options[0]
    other = options[1]
    pose = chosen[4]
    out = OrderedDict()
    out["up_file"] = _r(pose.up, 3)
    out["description"] = "%s файла вверх, ось вращения вертикально через центр стола (X=0, Z=0 сцены)" % _axis_name(
        pose.up, signed=True)
    out["core_on_axis_at_bottom"] = chosen[5]
    out["height_mm"] = round(pose.height, 1)
    out["max_radius_mm"] = round(fit.r_max, 1)
    out["transform"] = orientation.placement(mesh, pose, axis_point=fit.point)
    out["why_this_end"] = ("правило: в 5D сердечник должен начинаться на основании; затем минимальный поворот "
                           "от исходной позы файла; затем положительное направление оси вверх")
    out["other_end_down"] = OrderedDict([
        ("up_file", _r(other[4].up, 3)),
        ("transform", orientation.placement(mesh, other[4], axis_point=fit.point)),
    ])
    return out


# Мелкие помощники

def _axis_name(direction, signed=False):
    """'Y' (или '+Y'/'-Y' со знаком) для направления вдоль оси, иначе сам вектор."""
    names = ("X", "Y", "Z")
    index = max(range(3), key=lambda i: abs(direction[i]))
    if abs(direction[index]) > 0.995:
        if not signed:
            return names[index]
        return ("+" if direction[index] > 0 else "-") + names[index]
    if not signed and direction[index] < 0:
        direction = vecmath.scale(direction, -1.0)
    return "(%.2f, %.2f, %.2f)" % tuple(direction)


def _r(values, digits):
    return [round(float(value), digits) + 0.0 for value in values]


def _round_or_none(value, digits):
    return None if value is None else round(value, digits)
