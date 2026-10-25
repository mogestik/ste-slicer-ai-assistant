"""Системы координат и математика сеток (чистый Python, numpy — если есть).

Систем две:

* сцена (Uranium) — с ней работают UM.Scene и UM.Operations: X вправо, Y ВВЕРХ, Z к
  зрителю (к передней стороне принтера), начало в центре стола на его поверхности.
  Габариты: width = X, height = Y, depth = Z.
* принтер — её видят LLM, интерфейс слайсера (инструмент перемещения) и G-code:
  X вправо, Y назад, Z вверх, начало в центре стола.

    принтер (x, y, z) = сцена (x, -z, y)        сцена (x, y, z) = принтер (x, z, -y)

В режимах 5D ось вращения принтера — вертикаль через центр стола: у принтера X = 0, Y = 0
(в сцене X = 0, Z = 0).
"""

import math

try:
    import numpy
except ImportError:  # стенд может работать без numpy
    numpy = None


def scene_to_printer(point):
    x, y, z = point
    return (x, -z, y)


def printer_to_scene(point):
    x, y, z = point
    return (x, z, -y)


# Имя оси принтера -> единичный вектор в системе сцены.
PRINTER_AXES_IN_SCENE = {"x": (1.0, 0.0, 0.0), "y": (0.0, 0.0, -1.0), "z": (0.0, 1.0, 0.0)}


def round_list(values, digits=2):
    return [round(float(v), digits) + 0.0 for v in values]


# Повороты
def axis_angle_matrix(axis, angle_deg):
    """Матрица поворота 3x3 (список строк) для правого поворота вокруг `axis` (единичный вектор)."""
    x, y, z = _normalize(axis)
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    t = 1.0 - c
    return [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ]


def printer_matrix_to_scene(matrix):
    """Переводит матрицу поворота из системы принтера в систему сцены (M_s = P^T M_p P)."""
    # P переводит сцену в принтер: p = P s, строки (1,0,0), (0,0,-1), (0,1,0).
    p = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]
    pt = transpose(p)
    return matmul(pt, matmul(matrix, p))


def transpose(m):
    return [[m[j][i] for j in range(3)] for i in range(3)]


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def apply_matrix(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def check_rotation_matrix(m, tolerance=1e-3):
    """ValueError, если `m` — не настоящая матрица поворота 3x3."""
    if len(m) != 3 or any(len(row) != 3 for row in m):
        raise ValueError("матрица поворота должна быть 3x3")
    product = matmul(m, transpose(m))
    for i in range(3):
        for j in range(3):
            if abs(product[i][j] - (1.0 if i == j else 0.0)) > tolerance:
                raise ValueError("матрица не ортонормирована")
    if determinant(m) < 0:
        raise ValueError("матрица содержит отражение (определитель < 0)")


def determinant(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def matrix_to_quaternion(m):
    """Единичный кватернион (x, y, z, w) по матрице поворота (метод Шеппарда)."""
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2][1] - m[1][2]) / s
        y = (m[0][2] - m[2][0]) / s
        z = (m[1][0] - m[0][1]) / s
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        w = (m[2][1] - m[1][2]) / s
        x = 0.25 * s
        y = (m[0][1] + m[1][0]) / s
        z = (m[0][2] + m[2][0]) / s
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        w = (m[0][2] - m[2][0]) / s
        x = (m[0][1] + m[1][0]) / s
        y = 0.25 * s
        z = (m[1][2] + m[2][1]) / s
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        w = (m[1][0] - m[0][1]) / s
        x = (m[0][2] + m[2][0]) / s
        y = (m[1][2] + m[2][1]) / s
        z = 0.25 * s
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    return (x / norm, y / norm, z / norm, w / norm)


def quaternion_to_matrix(q):
    """Матрица поворота по кватерниону (x, y, z, w); кватернион сначала нормируется."""
    x, y, z, w = [float(c) for c in q]
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-12:
        raise ValueError("нулевой кватернион")
    x, y, z, w = x / n, y / n, z / n, w / n
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


SCENE_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


def rotation_from_steps(steps, frame="scene"):
    """Матрица в системе сцены для последовательных поворотов вокруг неподвижных мировых осей [(ось, угол_в_градусах), ...]."""
    axes = SCENE_AXES if frame == "scene" else PRINTER_AXES_IN_SCENE
    total = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for axis, angle in steps:
        if axis not in axes:
            raise ValueError("ось должна быть x, y или z, получено %r" % (axis,))
        total = matmul(axis_angle_matrix(axes[axis], float(angle)), total)
    return total


def rotation_axis_angle(m):
    """(единичный вектор оси или None, угол в градусах) по матрице поворота."""
    cos_angle = max(-1.0, min(1.0, (m[0][0] + m[1][1] + m[2][2] - 1.0) / 2.0))
    angle = math.degrees(math.acos(cos_angle))
    if angle < 1e-6:
        return None, 0.0
    axis = (m[2][1] - m[1][2], m[0][2] - m[2][0], m[1][0] - m[0][1])
    length = math.sqrt(sum(c * c for c in axis))
    if length < 1e-9:  # 180 градусов: ось берём по диагонали
        x = math.sqrt(max(0.0, (m[0][0] + 1) / 2.0))
        y = math.sqrt(max(0.0, (m[1][1] + 1) / 2.0))
        z = math.sqrt(max(0.0, (m[2][2] + 1) / 2.0))
        if x > 1e-6:
            y = math.copysign(y, m[0][1])
            z = math.copysign(z, m[0][2])
        elif y > 1e-6:
            z = math.copysign(z, m[1][2])
        return _normalize((x, y, z)), angle
    return tuple(c / length for c in axis), angle


def rotation_between(v_from, v_to):
    """Матрица поворота, переводящая направление `v_from` в `v_to`."""
    a = _normalize(v_from)
    b = _normalize(v_to)
    cross = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    dot = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))
    sin_angle = math.sqrt(sum(c * c for c in cross))
    if sin_angle < 1e-9:
        if dot > 0:
            return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        # Противоположные направления: 180 градусов вокруг любой оси, перпендикулярной `a`.
        helper = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 0.0, 1.0)
        perp = (a[1] * helper[2] - a[2] * helper[1], a[2] * helper[0] - a[0] * helper[2],
                a[0] * helper[1] - a[1] * helper[0])
        return axis_angle_matrix(perp, 180.0)
    angle = math.degrees(math.atan2(sin_angle, dot))
    return axis_angle_matrix(cross, angle)


def _normalize(v):
    length = math.sqrt(sum(float(c) * float(c) for c in v))
    if length < 1e-12:
        raise ValueError("нулевой вектор оси")
    return tuple(float(c) / length for c in v)


# Сетки
def _as_triangles(vertices, faces):
    """Список кортежей (v0, v1, v2) (вариант на чистом Python)."""
    verts = [tuple(float(c) for c in v) for v in _tolist(vertices)]
    if faces is None:
        return [(verts[i], verts[i + 1], verts[i + 2]) for i in range(0, len(verts) - 2, 3)]
    return [(verts[int(a)], verts[int(b)], verts[int(c)]) for a, b, c in _tolist(faces)]


def _tolist(array):
    return array.tolist() if hasattr(array, "tolist") else list(array)


def _np_triangles(vertices, faces):
    verts = numpy.asarray(vertices, dtype=numpy.float64).reshape(-1, 3)
    if faces is None:
        count = len(verts) // 3 * 3
        return verts[:count].reshape(-1, 3, 3)
    idx = numpy.asarray(faces, dtype=numpy.int64).reshape(-1, 3)
    return verts[idx]


def bounding_box(vertices):
    """(min_xyz, max_xyz) для списка вершин."""
    if numpy is not None:
        verts = numpy.asarray(vertices, dtype=numpy.float64).reshape(-1, 3)
        if not len(verts):
            return None
        return tuple(verts.min(axis=0).tolist()), tuple(verts.max(axis=0).tolist())
    verts = _tolist(vertices)
    if not verts:
        return None
    mins = [min(float(v[i]) for v in verts) for i in range(3)]
    maxs = [max(float(v[i]) for v in verts) for i in range(3)]
    return tuple(mins), tuple(maxs)


def mesh_volume(vertices, faces=None):
    """Объём (мм³) замкнутой треугольной сетки, по модулю."""
    if numpy is not None:
        tri = _np_triangles(vertices, faces)
        if not len(tri):
            return 0.0
        return float(abs(numpy.einsum("ij,ij->i", tri[:, 0], numpy.cross(tri[:, 1], tri[:, 2])).sum()) / 6.0)
    total = 0.0
    for v0, v1, v2 in _as_triangles(vertices, faces):
        total += (v0[0] * (v1[1] * v2[2] - v1[2] * v2[1])
                  - v0[1] * (v1[0] * v2[2] - v1[2] * v2[0])
                  + v0[2] * (v1[0] * v2[1] - v1[1] * v2[0]))
    return abs(total) / 6.0


def bottom_contact_area(vertices, faces=None, up_axis=1, tolerance=0.05):
    """Площадь (мм²) обращённых вниз треугольников, лежащих в самой нижней плоскости по `up_axis`.

    Для сцены up_axis=1 (Y), для принтера up_axis=2 (Z).
    """
    if numpy is not None:
        tri = _np_triangles(vertices, faces)
        if not len(tri):
            return 0.0
        low = tri[:, :, up_axis].min()
        on_plane = numpy.all(tri[:, :, up_axis] <= low + tolerance, axis=1)
        normals = numpy.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        areas = numpy.linalg.norm(normals, axis=1) / 2.0
        return float(areas[on_plane].sum())
    triangles = _as_triangles(vertices, faces)
    if not triangles:
        return 0.0
    low = min(min(v[up_axis] for v in t) for t in triangles)
    area = 0.0
    for t in triangles:
        if all(v[up_axis] <= low + tolerance for v in t):
            area += _triangle_area(t)
    return area


def _triangle_area(t):
    v0, v1, v2 = t
    a = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
    b = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
    c = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    return math.sqrt(c[0] * c[0] + c[1] * c[1] + c[2] * c[2]) / 2.0


def largest_flat_face(vertices, faces=None, angle_tolerance_deg=1.0, offset_tolerance=0.05, max_candidates=50):
    """Внешняя нормаль и площадь самой большой плоской грани, на которую объект можно поставить.

    Треугольники группируются по (нормали, смещению плоскости); группы перебираются от
    большей площади к меньшей, побеждает первая опорная плоскость (за неё не выходит ни
    одна вершина). Возвращает (нормаль, площадь) или (None, 0.0). Работает в той системе
    координат, в которой даны вершины.
    """
    triangles = _as_triangles(vertices, faces) if numpy is None else None
    groups = {}
    step = math.radians(angle_tolerance_deg)
    if numpy is not None:
        tri = _np_triangles(vertices, faces)
        if not len(tri):
            return None, 0.0
        normals = numpy.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        lengths = numpy.linalg.norm(normals, axis=1)
        valid = lengths > 1e-12
        unit = numpy.zeros_like(normals)
        unit[valid] = normals[valid] / lengths[valid][:, None]
        offsets = numpy.einsum("ij,ij->i", unit, tri[:, 0])
        areas = lengths / 2.0
        keys_n = numpy.round(unit / step).astype(numpy.int64)
        keys_d = numpy.round(offsets / offset_tolerance).astype(numpy.int64)
        for i in numpy.nonzero(valid)[0].tolist():
            key = (int(keys_n[i, 0]), int(keys_n[i, 1]), int(keys_n[i, 2]), int(keys_d[i]))
            entry = groups.get(key)
            if entry is None:
                groups[key] = [float(areas[i]), unit[i] * areas[i], float(offsets[i])]
            else:
                entry[0] += float(areas[i])
                entry[1] = entry[1] + unit[i] * areas[i]
        all_verts = tri.reshape(-1, 3)
    else:
        for t in triangles:
            v0, v1, v2 = t
            a = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
            b = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
            n = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
            length = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
            if length < 1e-12:
                continue
            u = (n[0] / length, n[1] / length, n[2] / length)
            d = u[0] * v0[0] + u[1] * v0[1] + u[2] * v0[2]
            key = (int(round(u[0] / step)), int(round(u[1] / step)), int(round(u[2] / step)),
                   int(round(d / offset_tolerance)))
            area = length / 2.0
            entry = groups.get(key)
            if entry is None:
                groups[key] = [area, [u[0] * area, u[1] * area, u[2] * area], d]
            else:
                entry[0] += area
                entry[1] = [entry[1][k] + u[k] * area for k in range(3)]
        all_verts = [v for t in triangles for v in t]
    if not groups:
        return None, 0.0
    ranked = sorted(groups.values(), key=lambda g: -g[0])[:max_candidates]
    for area, weighted, offset in ranked:
        normal = _normalize(_tolist(weighted))
        if _is_supporting_plane(all_verts, normal, offset, offset_tolerance * 4):
            return normal, area
    area, weighted, _offset = ranked[0]
    return _normalize(_tolist(weighted)), area


def _is_supporting_plane(verts, normal, offset, tolerance):
    if numpy is not None and hasattr(verts, "shape"):
        return float(numpy.dot(verts, numpy.asarray(normal)).max()) <= offset + tolerance
    best = max(v[0] * normal[0] + v[1] * normal[1] + v[2] * normal[2] for v in verts)
    return best <= offset + tolerance
