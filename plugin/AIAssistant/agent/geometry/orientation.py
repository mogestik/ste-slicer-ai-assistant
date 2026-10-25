"""Ориентации для печати: свесы, контакт со столом и преобразование сцены.

Ориентацию задаёт `up` — единичное направление в системе файла (Z вверх), которое станет
вертикалью принтера. Метрики:

* свесы — площадь граней, нормаль которых отклоняется от направления строго вниз меньше
  чем на (90 - support_angle) градусов (критерий поддержек CuraEngine), за вычетом граней,
  лежащих на столе;
* контакт — площадь граней на столе (нормаль вниз, все вершины на нижней высоте);
* пятно и высота габаритов в этой позе.

`placement()` превращает ориентацию в преобразование для сцены Cura (Y вверх): поворот
(кватернион и простые шаги вокруг осей сцены) и положение объекта на столе (центр
габаритов или ось вращения в X=Z=0).
"""

import math

from . import vecmath

# Контакт со столом: грани, смотрящие вниз (с точностью до 20 градусов), у которых все
# вершины не выше CONTACT_GAP мм от нижней точки (примерно половина первого слоя).
CONTACT_NORMAL_COS = math.cos(math.radians(20.0))
CONTACT_GAP = 0.1
SCENE_UP = (0.0, 1.0, 0.0)
_AXIS_DIRECTIONS = (
    ((0.0, 0.0, 1.0), "+Z файла вверх (как в файле)"),
    ((0.0, 0.0, -1.0), "-Z файла вверх (перевернуть)"),
    ((0.0, 1.0, 0.0), "+Y файла вверх"),
    ((0.0, -1.0, 0.0), "-Y файла вверх"),
    ((1.0, 0.0, 0.0), "+X файла вверх"),
    ((-1.0, 0.0, 0.0), "-X файла вверх"),
)


class Pose(object):
    def __init__(self, up, source):
        self.up = vecmath.normalize(up)
        self.source = source
        self.overhang = 0.0
        self.contact = 0.0
        self.height = 0.0
        self.footprint = (0.0, 0.0)
        self.score = 0.0
        self.rotation = None
        self.ranges = None   # диапазоны (x, y, z) вершин относительно центра габаритов, оси сцены


def evaluate_pose(mesh, up, source, support_angle):
    pose = Pose(up, source)
    pose.rotation = scene_rotation(pose.up)
    dirs = scene_axes_in_file(pose.rotation)
    pose.ranges = _vertex_ranges(mesh, dirs)
    (x0, x1), (h0, h1), (z0, z1) = pose.ranges
    pose.height = h1 - h0
    pose.footprint = (x1 - x0, z1 - z0)
    up = pose.up
    threshold = -math.sin(math.radians(support_angle))
    overhang = 0.0
    contact = 0.0
    tolerance = CONTACT_GAP + mesh.diagonal * 1e-6
    centre_h = vecmath.dot(_bbox_centre(mesh), up)
    floor = h0 + centre_h + tolerance
    for normal, area, triangles in mesh.normal_bins:
        cosine = vecmath.dot(normal, up)
        if cosine >= threshold:
            continue
        overhang += area
        if cosine > -CONTACT_NORMAL_COS:
            continue
        for index in triangles:
            a, b, c = mesh.triangle(index)
            if vecmath.dot(a, up) <= floor and vecmath.dot(b, up) <= floor and vecmath.dot(c, up) <= floor:
                contact += 0.5 * vecmath.norm(vecmath.cross(vecmath.sub(b, a), vecmath.sub(c, a)))
    pose.contact = contact
    pose.overhang = max(0.0, overhang - contact)
    # Ранжируем по площади свесов, плюс штраф позе, которая едва касается стола (ребром или
    # точкой — без каймы или поддержек не устоит), плюс небольшое предпочтение низким позам.
    footprint_area = max(pose.footprint[0] * pose.footprint[1], 1e-9)
    stable_contact = max(0.03 * footprint_area, 20.0)
    penalty = 0.0
    if contact < stable_contact:
        penalty = 0.3 * mesh.area * (1.0 - contact / stable_contact)
    pose.score = pose.overhang + penalty + 0.002 * mesh.area * pose.height / mesh.diagonal
    return pose


def candidate_ups(mesh, axis=None, flat_share=0.02, max_flat=8):
    """Какие направления «вверх» перебираем: 6 граней габаритов, оба конца оси, крупные плоские грани вниз."""
    result = list(_AXIS_DIRECTIONS)
    if axis is not None:
        result.append((axis, "ось вращения вверх"))
        result.append((vecmath.scale(axis, -1.0), "ось вращения вверх (другим концом)"))
    flats = sorted((item for item in mesh.normal_bins if item[1] >= flat_share * mesh.area),
                   key=lambda item: -item[1])
    for normal, area, _triangles in flats[:max_flat]:
        result.append((vecmath.scale(normal, -1.0), "плоская грань %.0f мм² на стол" % area))
    unique = []
    for up, source in result:
        up = vecmath.normalize(up)
        if all(vecmath.dot(up, other) < math.cos(math.radians(3.0)) for other, _s in unique):
            unique.append((up, source))
    return unique


def rank_poses(mesh, support_angle, axis=None):
    poses = [evaluate_pose(mesh, up, source, support_angle) for up, source in candidate_ups(mesh, axis)]
    poses.sort(key=lambda pose: pose.score)
    return poses


# Преобразование сцены

def scene_rotation(up_file):
    """Поворот сцены, после которого `up_file` смотрит вертикально, в виде Rz(b) * Rx(a).

    Возвращает {"quaternion": (x, y, z, w), "steps": [("x", a), ("z", b)]}: углы в градусах
    вокруг осей сцены, применяются по порядку.
    """
    sx, sy, sz = vecmath.file_to_scene(vecmath.normalize(up_file))
    angle_x = math.atan2(-sz, sy)
    y_after = math.sqrt(sy * sy + sz * sz)
    angle_z = math.atan2(sx, y_after)
    if abs(angle_x) < 1e-9 and y_after < 1e-9:
        angle_x = 0.0
    qx = vecmath.quat_from_axis_angle((1.0, 0.0, 0.0), angle_x)
    qz = vecmath.quat_from_axis_angle((0.0, 0.0, 1.0), angle_z)
    q = vecmath.quat_mul(qz, qx)
    steps = []
    for axis, angle in (("x", angle_x), ("z", angle_z)):
        degrees = math.degrees(angle)
        if abs(degrees) > 0.05:
            steps.append((axis, degrees))
    return {"quaternion": q, "steps": steps}


def scene_axes_in_file(rotation):
    """Направления в системе файла, которые после поворота лягут вдоль X, Y (вверх) и Z сцены."""
    q = rotation["quaternion"]
    inverse = (-q[0], -q[1], -q[2], q[3])
    return [vecmath.normalize(vecmath.scene_to_file(vecmath.quat_rotate(inverse, axis)))
            for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]


def placement(mesh, pose, axis_point=None):
    """Куда встаёт объект в сцене для данной позы.

    Без `axis_point` центр габаритов ставится в X=Z=0 (3D). С ним (точка оси вращения в
    системе файла) через X=Z=0 проходит ось (5D). Объект всегда стоит на столе (низ в Y=0
    сцены). У только что загруженного STL начало узла — центр габаритов, поэтому
    `node_position` — абсолютное положение для SetTransformOperation (у 3MF начало узла не в
    центре габаритов, там None). При `mesh.load_rotation` (3MF) поворот абсолютный от
    сброшенной позы.
    """
    dirs = scene_axes_in_file(pose.rotation)
    (x0, x1), (h0, h1), (z0, z1) = pose.ranges
    if axis_point is None:
        offset_x = (x0 + x1) / 2.0
        offset_z = (z0 + z1) / 2.0
    else:
        relative = vecmath.sub(axis_point, _bbox_centre(mesh))
        offset_x = vecmath.dot(relative, dirs[0])
        offset_z = vecmath.dot(relative, dirs[2])
    node_position = (-offset_x, -h0, -offset_z)
    bbox_centre = (node_position[0] + (x0 + x1) / 2.0, (h1 - h0) / 2.0, node_position[2] + (z0 + z1) / 2.0)
    q = pose.rotation["quaternion"]
    steps = pose.rotation["steps"]
    load = getattr(mesh, "load_rotation", None)
    if load is not None:
        # В узле уже есть поворот `load` (3MF); reset_rotation его отбрасывает, поэтому
        # абсолютный поворот от сброшенной позы — pose * load.
        total = vecmath.mat_mul(vecmath.quat_to_matrix(q), load)
        steps = vecmath.matrix_to_steps(total)
        q = vecmath.matrix_to_quat(vecmath.steps_to_matrix(steps))
    axis, angle = vecmath.quat_to_axis_angle(q)
    return {
        "rotation_steps": [{"axis": name, "angle_deg": round(value, 2)} for name, value in steps],
        "quaternion_xyzw": [round(value, 6) + 0.0 for value in q],
        "axis_angle": {"axis": [round(value, 6) + 0.0 for value in axis], "angle_deg": round(math.degrees(angle), 3)},
        "node_position": None if load is not None else [round(value, 3) + 0.0 for value in node_position],
        "bbox_center_scene": [round(value, 3) + 0.0 for value in bbox_centre],
        "bbox_size_scene": [round(x1 - x0, 2), round(h1 - h0, 2), round(z1 - z0, 2)],
    }


def _bbox_centre(mesh):
    return vecmath.scale(vecmath.add(mesh.bbox_min, mesh.bbox_max), 0.5)


def _vertex_ranges(mesh, dirs):
    """Диапазоны проекций (v - центр габаритов) на три направления."""
    centre = _bbox_centre(mesh)
    axis_aligned = all(max(abs(value) for value in d) > 1.0 - 1e-9 for d in dirs)
    if axis_aligned:
        ranges = []
        for d in dirs:
            k = max(range(3), key=lambda i: abs(d[i]))
            lo = (mesh.bbox_min[k] - centre[k]) * d[k]
            hi = (mesh.bbox_max[k] - centre[k]) * d[k]
            ranges.append((min(lo, hi), max(lo, hi)))
        return ranges
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = dirs
    ux, uy, uz = mesh.vertices
    inf = float("inf")
    amin = bmin = cmin = inf
    amax = bmax = cmax = -inf
    for x, y, z in zip(ux, uy, uz):
        a = x * ax + y * ay + z * az
        b = x * bx + y * by + z * bz
        c = x * cx + y * cy + z * cz
        if a < amin:
            amin = a
        if a > amax:
            amax = a
        if b < bmin:
            bmin = b
        if b > bmax:
            bmax = b
        if c < cmin:
            cmin = c
        if c > cmax:
            cmax = c
    shift = [vecmath.dot(centre, d) for d in dirs]
    return [(amin - shift[0], amax - shift[0]), (bmin - shift[1], bmax - shift[1]),
            (cmin - shift[2], cmax - shift[2])]
