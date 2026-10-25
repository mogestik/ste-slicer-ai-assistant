"""Сравнение стратегий печати по прочности: анизотропия слоёв FDM-детали под нагрузкой.

FDM-деталь анизотропна. Вдоль слоя прочность X = 1 (нормируем), поперёк слоёв
(по нормали n к плоскости слоя) — Z = k_z, межслойный сдвиг — S = k_s. Для одноосного
напряжения вдоль единичного направления d под углом theta к плоскости слоя
(sin theta = |d . n|) прочность по Цаю–Хиллу относительно X:

    1 / s(theta)^2 = cos^4/X^2 + (1/S^2 - 1/X^2) cos^2 sin^2 + sin^4/Z^2

Коэффициенты ниже — типичные данные анизотропии FDM, НЕ измерения: всё, что
считается здесь, — оценка.

Нормаль слоя по стратегиям (оси файла: a — ось вращения, r(phi) / t(phi) —
радиальный и окружной единичные векторы в точке окружности с углом phi):

* 3D: n = u, вектор «вверх» позы (постоянный);
* 5D Spiral и оболочка 5D: слои — цилиндры вокруг оси, n = r(phi);
* сердечник 5D (пруток Ø6): плоские слои, n = a.

Нагрузка — набор главных направлений напряжения с весами; её индекс — среднее по
`POINTS` точкам окружности (для нагрузок из WORST_QUARTER_LOADS — по худшей четверти
точек). Чистый Python 3.5, без обращений к слайсеру.
"""

import math
from collections import OrderedDict

from . import vecmath

POINTS = 36
INV_SQRT2 = math.sqrt(0.5)

# класс -> (k_z, k_s, название); первый класс — по умолчанию
MATERIAL_CLASSES = OrderedDict([
    ("standard", (0.55, 0.60, "PLA/PETG/ABS/ASA/PC")),
    ("fiber", (0.45, 0.50, "наполненный волокном (CF/GF)")),
    ("tpu", (0.80, 0.80, "TPU / эластомер")),
])
DEFAULT_CLASS = "standard"

# вид -> (название, [(a, r, t, вес)] главные направления в осях детали)
LOAD_TYPES = OrderedDict([
    ("shaft_bending", ("изгиб вала", [(1.0, 0.0, 0.0, 1.0)])),
    ("torsion", ("кручение", [(INV_SQRT2, 0.0, INV_SQRT2, 0.5), (-INV_SQRT2, 0.0, INV_SQRT2, 0.5)])),
    ("axial_tension", ("растяжение/сжатие вдоль оси", [(1.0, 0.0, 0.0, 1.0)])),
    ("centrifugal", ("центробежная нагрузка", [(0.0, 0.0, 1.0, 0.7), (0.0, 1.0, 0.0, 0.3)])),
    ("hoop_pressure", ("внутреннее давление / натяг", [(0.0, 0.0, 1.0, 0.7), (1.0, 0.0, 0.0, 0.3)])),
    ("radial_load", ("радиальная нагрузка на обод/ступицу", [(0.0, 1.0, 0.0, 1.0)])),
    ("gear_tooth", ("изгиб зуба", [(0.0, 1.0, 0.0, 0.7), (0.0, 0.0, 1.0, 0.3)])),
    ("custom", ("своё направление", None)),
])
WORST_QUARTER_LOADS = ("gear_tooth", "radial_load")
# нагрузки, которым не нужна ось вращения (направление задано вектором)
AXISLESS_LOADS = ("custom", "axial_tension")


class StrengthError(ValueError):
    pass


def material_class(record):
    """Ключ класса по записи из materials_h530.json (None -> по умолчанию)."""
    if not record:
        return DEFAULT_CLASS
    if record.get("flexible") or record.get("elastomer"):
        return "tpu"
    if record.get("filler"):
        return "fiber"
    return DEFAULT_CLASS


def layer_strength(sin_theta, klass=DEFAULT_CLASS):
    """Прочность по Цаю–Хиллу (относительно X = 1) при напряжении под углом asin(sin_theta) к плоскости слоя."""
    kz, ks, _label = MATERIAL_CLASSES[klass]
    s2 = min(1.0, sin_theta * sin_theta)
    c2 = 1.0 - s2
    inverse = c2 * c2 + (1.0 / (ks * ks) - 1.0) * c2 * s2 + s2 * s2 / (kz * kz)
    return min(1.0, 1.0 / math.sqrt(inverse))


def normalize_loads(loads, has_axis):
    """-> (применимые нагрузки, пропущенные). У нагрузки: type, label, weight, directions, worst_quarter.

    `directions` — список (вес, в_осях_детали, вектор): вектор — тройка (a, r, t) в осях
    детали либо единичный вектор в осях файла (custom / axial_tension без оси).
    """
    usable = []
    skipped = []
    for item in loads or []:
        kind = item.get("type")
        if kind not in LOAD_TYPES:
            raise StrengthError("Неизвестная нагрузка '%s'. Допустимые: %s" % (kind, ", ".join(LOAD_TYPES)))
        weight = item.get("weight")
        weight = 1.0 if weight is None else float(weight)
        if weight <= 0:
            raise StrengthError("Вес нагрузки %s должен быть > 0" % kind)
        direction = item.get("direction")
        vector = None
        if direction is not None:
            if len(direction) != 3 or vecmath.norm(direction) < 1e-9:
                raise StrengthError("direction нагрузки %s: нужен ненулевой вектор [x, y, z] в осях файла" % kind)
            vector = vecmath.normalize(tuple(float(v) for v in direction))
        label = LOAD_TYPES[kind][0]
        if kind == "custom" or (kind == "axial_tension" and not has_axis):
            if vector is None:
                raise StrengthError("Для нагрузки %s без оси вращения нужен direction (оси файла)" % kind)
            directions = [(1.0, False, vector)]
        elif not has_axis:
            skipped.append("%s: нужна ось вращения, у модели её нет" % kind)
            continue
        else:
            directions = [(w, True, (ca, cr, ct)) for ca, cr, ct, w in LOAD_TYPES[kind][1]]
        usable.append({"type": kind, "label": label, "weight": weight, "directions": directions,
                       "worst_quarter": kind in WORST_QUARTER_LOADS})
    if not usable:
        raise StrengthError("Нет применимых нагрузок. " + "; ".join(skipped))
    return usable, skipped


def _frame(axis, k):
    """Единичные векторы (r, t) в точке окружности k."""
    e1, e2 = vecmath.perpendicular_basis(axis)
    phi = 2.0 * math.pi * k / POINTS
    c, s = math.cos(phi), math.sin(phi)
    r = vecmath.add(vecmath.scale(e1, c), vecmath.scale(e2, s))
    t = vecmath.add(vecmath.scale(e1, -s), vecmath.scale(e2, c))
    return r, t


def _point_strength(load, axis, r, t, normal, klass):
    total = 0.0
    weights = 0.0
    for weight, in_axis_frame, spec in load["directions"]:
        if in_axis_frame:
            a, cr, ct = spec
            vector = vecmath.add(vecmath.add(vecmath.scale(axis, a), vecmath.scale(r, cr)), vecmath.scale(t, ct))
        else:
            vector = spec
        total += weight * layer_strength(abs(vecmath.dot(vector, normal)), klass)
        weights += weight
    return total / weights


def load_index(load, axis, normal_of, klass):
    """Индекс одной нагрузки. `normal_of(r)` -> нормаль слоя в точке (r — радиальный вектор или None)."""
    values = []
    for k in range(POINTS if axis is not None else 1):
        r, t = _frame(axis, k) if axis is not None else (None, None)
        values.append(_point_strength(load, axis, r, t, normal_of(r), klass))
    if load["worst_quarter"] and len(values) > 1:
        values.sort()
        values = values[:int(math.ceil(len(values) / 4.0))]
    return sum(values) / len(values)


def evaluate(axis, normal_of, loads, klass):
    """-> OrderedDict(index, per_load, worst_load, worst_value) для одной стратегии."""
    per_load = OrderedDict()
    for load in loads:
        per_load[load["type"]] = load_index(load, axis, normal_of, klass)
    weights = sum(load["weight"] for load in loads)
    total = sum(load["weight"] * per_load[load["type"]] for load in loads) / weights
    worst = min(loads, key=lambda load: per_load[load["type"]])
    out = OrderedDict()
    out["index"] = total
    out["per_load"] = per_load
    out["worst_load"] = worst["type"]
    out["worst_value"] = per_load[worst["type"]]
    return out


def compare(axis, poses, loads, klass=DEFAULT_CLASS, five_d=False):
    """Прочность каждой позы 3D и (для тела вращения) оболочки и сердечника 5D.

    axis: единичное направление оси в осях файла или None; poses: словари с up_file;
    loads: исходные словари нагрузок (type, weight, direction).
    -> dict(loads, skipped, poses_3d [..], shell, core)
    """
    has_axis = axis is not None
    if klass not in MATERIAL_CLASSES:
        raise StrengthError("Неизвестный класс материала %s" % klass)
    usable, skipped = normalize_loads(loads, has_axis)
    if has_axis:
        axis = vecmath.normalize(tuple(axis))
    result = OrderedDict()
    result["loads"] = usable
    result["skipped"] = skipped
    result["poses_3d"] = []
    for pose in poses:
        up = vecmath.normalize(tuple(pose["up_file"]))
        entry = evaluate(axis, lambda _r, up=up: up, usable, klass)
        entry["pose"] = pose
        result["poses_3d"].append(entry)
    if has_axis and five_d:
        result["shell"] = evaluate(axis, lambda r: r, usable, klass)
        result["core"] = evaluate(axis, lambda _r: axis, usable, klass)
    return result


def recommend(best_3d, shell, geometry_mode, spiral_ok=False):
    """Выбор стратегии по индексам и геометрической рекомендации режима.

    best_3d / shell: результаты evaluate() (shell None -> 5D нет); geometry_mode:
    printing_mode из analyze_model. -> OrderedDict(choice, basis, difference, compromise)
    """
    out = OrderedDict()
    if shell is None:
        out["choice"] = "classic"
        out["basis"] = "no_5d"
        out["difference"] = None
        out["compromise"] = False
        return out
    diff = shell["index"] - best_3d["index"]      # > 0: 5D прочнее
    geometric = geometry_mode if geometry_mode != "classic" else "classic"
    five_d_mode = geometric if geometric != "classic" else ("cylindrical" if spiral_ok else "cylindrical_full")
    out["difference"] = diff
    if abs(diff) >= 0.15:
        out["basis"] = "strength"
        out["choice"] = five_d_mode if diff > 0 else "classic"
    else:
        out["basis"] = "geometry"
        out["choice"] = geometric
    stronger = "classic" if diff < 0 else "5d"
    chosen = "classic" if out["choice"] == "classic" else "5d"
    out["compromise"] = abs(diff) >= 0.08 and chosen != stronger
    return out
