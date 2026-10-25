"""Треугольная сетка и её чтение (STL бинарный и ASCII, OBJ; 3MF — в threemf.py).

Координаты храним плоским `array('f')`, по 9 чисел на треугольник, в системе файла
(Z вверх). Читатели STL и OBJ в слайсере кладут вершину файла (x, y, z) в сцену как
(x, z, -y) без поворота узла, поэтому для них система файла и есть поза после
load_model. `Mesh.load_rotation` (только у 3MF) — поворот, который слайсер записывает в
узел при загрузке (см. threemf.py). Один проход по треугольникам (`Mesh.compute`)
собирает всё, что нужно анализу: площадь, объём, центр масс, вторые моменты, гистограмму
нормалей по площади и дополнительные точки поверхности вдоль длинных рёбер.
"""

import math
import os
import re
import struct
import sys
from array import array

from . import vecmath

# Нормали граней раскладываем по ячейкам шириной 1/NORMAL_BINS по компонентам единичного
# вектора (около 1 градуса) — для порогов свесов этого хватает.
NORMAL_BINS = 60
# Шаг выборки точек поверхности = диагональ габаритов / SAMPLE_DIVISIONS.
SAMPLE_DIVISIONS = 150.0


class MeshError(Exception):
    pass


class Mesh(object):
    def __init__(self, coords, name=""):
        if len(coords) % 9:
            raise MeshError("число координат не кратно 9")
        self.coords = coords
        self.name = name
        self.triangle_count = len(coords) // 9
        self.computed = False
        # Поворот сцены, который читатель записал в узел (None — единичный). Поза со сброшенным
        # поворотом узла — это load_rotation^-1, применённый к позе файла (бывает только у 3MF).
        self.load_rotation = None
        self.source = {}

    # Один проход по всем треугольникам

    def compute(self):
        if self.computed:
            return self
        c = self.coords
        if not self.triangle_count:
            raise MeshError("в файле нет треугольников")
        xs, ys, zs = c[0::3], c[1::3], c[2::3]
        self.bbox_min = (min(xs), min(ys), min(zs))
        self.bbox_max = (max(xs), max(ys), max(zs))
        del xs, ys, zs
        extents = vecmath.sub(self.bbox_max, self.bbox_min)
        self.diagonal = vecmath.norm(extents) or 1.0
        step = self.diagonal / SAMPLE_DIVISIONS
        self.sample_step = step
        long_edge2 = (2.0 * step) ** 2

        q = float(NORMAL_BINS)
        bins = {}            # ключ -> [площадь, nx*площадь, ny*площадь, nz*площадь, [индексы треугольников]]
        extra = array("f")   # дополнительные точки поверхности (плоско xyz) вдоль длинных рёбер
        area_sum = 0.0
        det_sum = 0.0
        cx = cy = cz = 0.0
        sxx = syy = szz = sxy = sxz = syz = 0.0
        nsx = nsy = nsz = 0.0
        degenerate = 0
        index = -1
        for x0, y0, z0, x1, y1, z1, x2, y2, z2 in zip(*[iter(c)] * 9):
            index += 1
            ax = x1 - x0
            ay = y1 - y0
            az = z1 - z0
            bx = x2 - x0
            by = y2 - y0
            bz = z2 - z0
            nx = ay * bz - az * by
            ny = az * bx - ax * bz
            nz = ax * by - ay * bx
            length2 = nx * nx + ny * ny + nz * nz
            if length2 <= 0.0:
                degenerate += 1
                continue
            length = math.sqrt(length2)
            area_sum += length
            nsx += nx
            nsy += ny
            nsz += nz
            # Ориентированный объём тетраэдра (начало, v0, v1, v2), умноженный на 6.
            det = x0 * (y1 * z2 - z1 * y2) + y0 * (z1 * x2 - x1 * z2) + z0 * (x1 * y2 - y1 * x2)
            det_sum += det
            sx = x0 + x1 + x2
            sy = y0 + y1 + y2
            sz = z0 + z1 + z2
            cx += det * sx
            cy += det * sy
            cz += det * sz
            sxx += det * (x0 * x0 + x1 * x1 + x2 * x2 + sx * sx)
            syy += det * (y0 * y0 + y1 * y1 + y2 * y2 + sy * sy)
            szz += det * (z0 * z0 + z1 * z1 + z2 * z2 + sz * sz)
            sxy += det * (x0 * y0 + x1 * y1 + x2 * y2 + sx * sy)
            sxz += det * (x0 * z0 + x1 * z1 + x2 * z2 + sx * sz)
            syz += det * (y0 * z0 + y1 * z1 + y2 * z2 + sy * sz)
            inv = 1.0 / length
            key = (int(round(nx * inv * q)), int(round(ny * inv * q)), int(round(nz * inv * q)))
            entry = bins.get(key)
            if entry is None:
                entry = bins[key] = [0.0, 0.0, 0.0, 0.0, []]
            entry[0] += length
            entry[1] += nx
            entry[2] += ny
            entry[3] += nz
            entry[4].append(index)
            # Длинные рёбра (грубая триангуляция из CAD): добавляем точки вдоль них, чтобы
            # радиальные профили видели всю поверхность, а не только углы.
            if ax * ax + ay * ay + az * az > long_edge2 or bx * bx + by * by + bz * bz > long_edge2 \
                    or (x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2 > long_edge2:
                for px, py, pz, qx, qy, qz in ((x0, y0, z0, x1, y1, z1), (x1, y1, z1, x2, y2, z2),
                                               (x2, y2, z2, x0, y0, z0)):
                    edge = math.sqrt((qx - px) ** 2 + (qy - py) ** 2 + (qz - pz) ** 2)
                    count = int(edge / step)
                    for k in range(1, count):
                        t = k / float(count)
                        extra.append(px + (qx - px) * t)
                        extra.append(py + (qy - py) * t)
                        extra.append(pz + (qz - pz) * t)

        self.area = area_sum / 2.0
        volume = det_sum / 6.0
        self.inverted = volume < 0
        self.volume = abs(volume)
        self.degenerate = degenerate
        # |сумма векторов площадей| / площадь ≈ 0 для любой замкнутой поверхности.
        self.closure_error = (math.sqrt(nsx * nsx + nsy * nsy + nsz * nsz) / area_sum) if area_sum else 1.0
        if abs(det_sum) > 1e-12:
            self.center_of_mass = (cx / (4.0 * det_sum), cy / (4.0 * det_sum), cz / (4.0 * det_sum))
            k = 1.0 / 120.0  # (det/6) / 20
            vol = det_sum / 6.0
            com = self.center_of_mass
            mxx = sxx * k - vol * com[0] * com[0]
            myy = syy * k - vol * com[1] * com[1]
            mzz = szz * k - vol * com[2] * com[2]
            mxy = sxy * k - vol * com[0] * com[1]
            mxz = sxz * k - vol * com[0] * com[2]
            myz = syz * k - vol * com[1] * com[2]
            if vol < 0:
                mxx, myy, mzz, mxy, mxz, myz = -mxx, -myy, -mzz, -mxy, -mxz, -myz
            self.inertia = ((myy + mzz, -mxy, -mxz), (-mxy, mxx + mzz, -myz), (-mxz, -myz, mxx + myy))
        else:
            self.center_of_mass = vecmath.scale(vecmath.add(self.bbox_min, self.bbox_max), 0.5)
            self.inertia = None

        self.normal_bins = []
        for entry in bins.values():
            n = vecmath.normalize((entry[1], entry[2], entry[3]))
            self.normal_bins.append((n, entry[0] / 2.0, entry[4]))

        # Уникальные вершины (для крайних высот) и облако точек.
        unique = set(zip(c[0::3], c[1::3], c[2::3]))
        self.vertex_count = len(unique)
        ux = array("d")
        uy = array("d")
        uz = array("d")
        for x, y, z in unique:
            ux.append(x)
            uy.append(y)
            uz.append(z)
        del unique
        self.vertices = (ux, uy, uz)
        self.samples = (ux + array("d", extra[0::3]), uy + array("d", extra[1::3]),
                        uz + array("d", extra[2::3]))
        self.computed = True
        return self

    # Помощники для анализа

    def triangle(self, index):
        o = index * 9
        c = self.coords
        return ((c[o], c[o + 1], c[o + 2]), (c[o + 3], c[o + 4], c[o + 5]), (c[o + 6], c[o + 7], c[o + 8]))

    def vertex_range(self, direction):
        """(мин, макс) проекций вершин на `direction`."""
        dx, dy, dz = direction
        ux, uy, uz = self.vertices
        values = [x * dx + y * dy + z * dz for x, y, z in zip(ux, uy, uz)]
        return min(values), max(values)


# Чтение файлов
MODEL_EXTENSIONS = (".stl", ".obj", ".3mf")


def read_mesh(path, part=None):
    """Читает STL, OBJ или 3MF (для 3MF `part` — номер элемента build с 1, None — самый большой)."""
    lower = path.lower()
    if lower.endswith(".stl"):
        return read_stl(path)
    if lower.endswith(".obj"):
        return read_obj(path)
    if lower.endswith(".3mf"):
        from . import threemf
        return threemf.read_3mf(path, part)
    raise MeshError("формат не поддерживается (нужен STL, OBJ или 3MF): %s" % os.path.basename(path))


def read_stl(path):
    """Читает бинарный или ASCII STL в Mesh (система файла, единицы как в файле)."""
    with open(path, "rb") as handle:
        data = handle.read()
    name = os.path.basename(path)
    if _is_binary_stl(data):
        return Mesh(_parse_binary(data), name)
    return Mesh(_parse_ascii(data), name)


def read_obj(path):
    """Wavefront OBJ: все записи `v` / `f` в одну сетку (как OBJReader в Uranium, единицы как в файле).

    Многоугольники бьём веером на треугольники; элементы `f` бывают `v`, `v/vt`, `v//vn`
    или `v/vt/vn`, отрицательные индексы считаются с конца.
    """
    xs = array("d")
    coords = array("f")
    with open(path, "rb") as handle:
        for line in handle:
            if line[:2] == b"v ":
                parts = line.split()
                try:
                    xs.append(float(parts[1]))
                    xs.append(float(parts[2]))
                    xs.append(float(parts[3]))
                except (IndexError, ValueError):
                    raise MeshError("некорректная строка v в OBJ")
            elif line[:2] == b"f ":
                count = len(xs) // 3
                indices = []
                for item in line.split()[1:]:
                    try:
                        index = int(item.split(b"/")[0])
                    except ValueError:
                        raise MeshError("некорректная строка f в OBJ")
                    index = index - 1 if index > 0 else count + index
                    if not 0 <= index < count:
                        raise MeshError("индекс вершины вне диапазона в OBJ")
                    indices.append(3 * index)
                for k in range(1, len(indices) - 1):
                    for o in (indices[0], indices[k], indices[k + 1]):
                        coords.append(xs[o])
                        coords.append(xs[o + 1])
                        coords.append(xs[o + 2])
    return Mesh(coords, os.path.basename(path))


def mesh_from_triangles(coords, frame="file", name=""):
    """Сетка из плоской последовательности координат вершин треугольников (по 9 на треугольник).

    `frame="scene"` — координаты сцены Cura (Y вверх); их переводим в систему файла (Z вверх),
    с которой работает анализ. Через эту функцию анализируют объект, который уже на сцене:
    передают вершины узла, например `node.getMeshDataTransformedVertices()`, развёрнутые по
    граням (если сетка индексная, сначала раскрыть буфер индексов).
    """
    values = array("f")
    if frame == "scene":
        # сцена (x, y, z) -> файл (x, -z, y)
        it = iter(coords)
        for x, y, z in zip(it, it, it):
            values.append(x)
            values.append(-z)
            values.append(y)
    elif frame == "file":
        values.extend(float(value) for value in coords)
    else:
        raise MeshError("неизвестная система координат: %s" % frame)
    return Mesh(values, name)


def _is_binary_stl(data):
    if len(data) < 84:
        return False
    count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + 50 * count
    if count and expected == len(data):
        return True
    head = data[:1024].lstrip().lower()
    if head.startswith(b"solid") and b"facet" in data[:4096].lower():
        return False
    return bool(count) and expected <= len(data)


def _parse_binary(data):
    count = struct.unpack_from("<I", data, 80)[0]
    # Из каждой 50-байтной записи выбрасываем нормаль (12 байт) и атрибут (2 байта) —
    # остаются вершины подряд, одним массивом float32.
    end = 84 + 50 * count
    chunk = b"".join([data[offset + 12:offset + 48] for offset in range(84, end, 50)])
    coords = array("f")
    coords.frombytes(chunk)
    if sys.byteorder != "little":
        coords.byteswap()
    return coords


_VERTEX_RE = re.compile(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)")


def _parse_ascii(data):
    coords = array("f")
    for match in _VERTEX_RE.finditer(data):
        try:
            coords.append(float(match.group(1)))
            coords.append(float(match.group(2)))
            coords.append(float(match.group(3)))
        except ValueError:
            raise MeshError("некорректная строка vertex в ASCII STL")
    if len(coords) % 9:
        raise MeshError("число вершин в ASCII STL не кратно 3")
    return coords
