"""Мелкие функции для 3D-векторов и поворотов на обычных кортежах (без numpy).

Системы координат пакета geometry:

* система файла — координаты как в STL (Z вверх, как у принтера);
* система сцены — сцена Uranium/Cura (Y вверх, X вправо, Z к зрителю).
  STLReader переводит вершину файла (x, y, z) в вершину сцены (x, z, -y).
"""

import math


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    return math.sqrt(dot(a, a))


def normalize(a):
    length = norm(a)
    if length <= 0.0:
        return (0.0, 0.0, 1.0)
    return (a[0] / length, a[1] / length, a[2] / length)


def angle_deg(a, b):
    """Угол между двумя единичными векторами, в градусах."""
    return math.degrees(math.acos(max(-1.0, min(1.0, dot(a, b)))))


def perpendicular_basis(axis):
    """Два единичных вектора, которые вместе с осью дают правую тройку (e1, e2, axis)."""
    axis = normalize(axis)
    helper = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
    e1 = normalize(cross(helper, axis))
    e2 = cross(axis, e1)
    return e1, e2


def file_to_scene(v):
    """Точка или направление файла (Z вверх) -> сцена Cura (Y вверх), так же как в STLReader."""
    return (v[0], v[2], -v[1])


def scene_to_file(v):
    return (v[0], -v[2], v[1])


# Кватернионы (x, y, z, w), правые, в том же порядке, что UM.Math.Quaternion

def quat_from_axis_angle(axis, angle_rad):
    axis = normalize(axis)
    s = math.sin(angle_rad / 2.0)
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(angle_rad / 2.0))


def quat_mul(a, b):
    """Произведение Гамильтона a*b (сначала b, потом a)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def quat_rotate(q, v):
    x, y, z, w = q
    # v' = v + 2w(q x v) + 2 q x (q x v)
    qv = (x, y, z)
    t = scale(cross(qv, v), 2.0)
    return add(add(v, scale(t, w)), cross(qv, t))


def quat_to_axis_angle(q):
    x, y, z, w = q
    if w < 0:
        x, y, z, w = -x, -y, -z, -w
    w = max(-1.0, min(1.0, w))
    angle = 2.0 * math.acos(w)
    s = math.sqrt(max(0.0, 1.0 - w * w))
    if s < 1e-9:
        return (0.0, 1.0, 0.0), 0.0
    return (x / s, y / s, z / s), angle


def quat_to_matrix(q):
    x, y, z, w = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def quat_between(a, b):
    """Кратчайший поворот, переводящий единичный вектор a в единичный вектор b."""
    a = normalize(a)
    b = normalize(b)
    d = dot(a, b)
    if d > 1.0 - 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    if d < -1.0 + 1e-12:
        # 180 градусов: подойдёт любая ось, перпендикулярная a; берём по возможности координатную.
        for candidate in ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)):
            if abs(dot(candidate, a)) < 1e-6:
                return quat_from_axis_angle(candidate, math.pi)
        return quat_from_axis_angle(perpendicular_basis(a)[0], math.pi)
    axis = cross(a, b)
    return quat_from_axis_angle(axis, math.acos(d))


# Собственные значения симметричной матрицы 3x3 (метод Якоби)

def symmetric_eigen(matrix):
    """Собственные значения и единичные собственные векторы симметричной матрицы 3x3.

    Возвращает список (значение, вектор) по возрастанию значения.
    """
    a = [list(row) for row in matrix]
    v = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for _sweep in range(50):
        off = abs(a[0][1]) + abs(a[0][2]) + abs(a[1][2])
        if off < 1e-12 * (abs(a[0][0]) + abs(a[1][1]) + abs(a[2][2]) + 1e-30):
            break
        for p, q in ((0, 1), (0, 2), (1, 2)):
            if abs(a[p][q]) < 1e-300:
                continue
            theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
            t = (1.0 if theta >= 0 else -1.0) / (abs(theta) + math.sqrt(theta * theta + 1.0))
            c = 1.0 / math.sqrt(t * t + 1.0)
            s = t * c
            for k in range(3):
                akp, akq = a[k][p], a[k][q]
                a[k][p] = c * akp - s * akq
                a[k][q] = s * akp + c * akq
            for k in range(3):
                apk, aqk = a[p][k], a[q][k]
                a[p][k] = c * apk - s * aqk
                a[q][k] = s * apk + c * aqk
            for k in range(3):
                vkp, vkq = v[k][p], v[k][q]
                v[k][p] = c * vkp - s * vkq
                v[k][q] = s * vkp + c * vkq
    pairs = [(a[i][i], normalize((v[0][i], v[1][i], v[2][i]))) for i in range(3)]
    pairs.sort(key=lambda item: item[0])
    return pairs


def solve3(m, b):
    """Решает линейную систему 3x3 (метод Крамера); None, если матрица вырождена."""
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    if abs(det) < 1e-18:
        return None
    result = []
    for col in range(3):
        mc = [list(row) for row in m]
        for row in range(3):
            mc[row][col] = b[row]
        result.append((mc[0][0] * (mc[1][1] * mc[2][2] - mc[1][2] * mc[2][1])
                       - mc[0][1] * (mc[1][0] * mc[2][2] - mc[1][2] * mc[2][0])
                       + mc[0][2] * (mc[1][0] * mc[2][1] - mc[1][1] * mc[2][0])) / det)
    return result


# Матрицы 3x3 (кортежи строк)
IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def mat_vec(m, v):
    return (dot(m[0], v), dot(m[1], v), dot(m[2], v))


def transpose(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def axis_rotation(axis_name, angle_deg):
    """Матрица поворота вокруг координатной оси ('x', 'y' или 'z'), угол в градусах."""
    c = math.cos(math.radians(angle_deg))
    s = math.sin(math.radians(angle_deg))
    if axis_name == "x":
        return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
    if axis_name == "y":
        return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def axis_angle_matrix(axis, angle_deg):
    x, y, z = normalize(axis)
    c = math.cos(math.radians(angle_deg))
    s = math.sin(math.radians(angle_deg))
    t = 1.0 - c
    return ((t * x * x + c, t * x * y - s * z, t * x * z + s * y),
            (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
            (t * x * z - s * y, t * y * z + s * x, t * z * z + c))


def orthonormalize(m):
    """Поворот, близкий к линейному отображению 3x3 (Грам-Шмидт по столбцам, отражение отбрасываем)."""
    cols = transpose(m)
    c0 = normalize(cols[0])
    c1 = normalize(sub(cols[1], scale(c0, dot(cols[1], c0))))
    c2 = cross(c0, c1)
    return transpose((c0, c1, c2))


def matrix_to_steps(m, tolerance=0.01):
    """Повороты вокруг неподвижных осей: сначала x, потом y, потом z (m = Rz * Ry * Rx): [(ось, градусы), ...]."""
    sy = max(-1.0, min(1.0, -m[2][0]))
    b = math.asin(sy)
    if abs(sy) < 1.0 - 1e-9:
        a = math.atan2(m[2][1], m[2][2])
        c = math.atan2(m[1][0], m[0][0])
    else:  # складывание рамок (gimbal lock): определена только сумма a + c (или разность a - c)
        a = math.atan2(-m[1][2], m[1][1])
        c = 0.0
    steps = []
    for name, angle in (("x", a), ("y", b), ("z", c)):
        degrees = math.degrees(angle)
        if abs(degrees) > tolerance:
            steps.append((name, degrees))
    return steps


def steps_to_matrix(steps):
    total = IDENTITY
    for name, angle in steps:
        total = mat_mul(axis_rotation(name, angle), total)
    return total


def matrix_to_quat(m):
    """(x, y, z, w) по матрице поворота."""
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        s = 2.0 * math.sqrt(trace + 1.0)
        return ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, 0.25 * s)
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = 2.0 * math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2])
        return (0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s)
    if m[1][1] > m[2][2]:
        s = 2.0 * math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2])
        return ((m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s)
    s = 2.0 * math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1])
    return ((m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s, (m[1][0] - m[0][1]) / s)
