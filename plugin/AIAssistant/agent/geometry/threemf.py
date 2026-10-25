"""Чтение 3MF (основная спецификация: 3D/3dmodel.model) по образцу ThreeMFReader слайсера.

Как STE Slicer (ThreeMFReader из Cura 3.6 + Savitar) загружает 3MF:

* каждый <build><item> становится отдельным узлом сцены; компоненты объекта — дочерними
  узлами со своими преобразованиями;
* данные сетки остаются в исходных координатах объекта (система 3MF, Z вверх);
* преобразование узла — Flip * Unit * Item, где Flip переводит (x, y, z) в сцену
  (x, z, -y), а Unit переводит `unit` в мм. На экране деталь стоит как в файле (как STL),
  но ПОВОРОТ узла равен R_load = Flip * R_item (R_item — поворотная часть преобразования
  элемента).

reset_rotation в адаптере ставит узлу заданную матрицу поворота, то есть отбрасывает
R_load. Анализ работает с элементом в том виде, как он выглядит после загрузки (система
файла, мм, как у STL), и запоминает R_load в `Mesh.load_rotation`; преобразования
начинаются с шагов R_load, и rotate_object(steps, reset_rotation=true) даёт ровно ту
позу, которую мы анализировали.
"""

import os
import zipfile
import xml.etree.ElementTree as ET
from array import array

from . import vecmath
from .mesh import Mesh, MeshError

MODEL_PATH = "3D/3dmodel.model"
UNITS_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "meter": 1000.0, "inch": 25.4,
            "foot": 304.8}
# Flip из ThreeMFReader: 3MF (x, y, z) -> сцена (x, z, -y), это поворот на -90 градусов вокруг X.
FLIP = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))
IDENTITY_TRANSFORM = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
MAX_DEPTH = 16


class _Object(object):
    def __init__(self, object_id, name):
        self.id = object_id
        self.name = name
        self.vertices = array("d")
        self.triangles = array("l")
        self.components = []   # (object id, transform)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_transform(text):
    """Преобразование 3MF "m00 m01 m02 m10 ... m32" (строки-векторы: p' = p * M) — 12 чисел."""
    if not text:
        return IDENTITY_TRANSFORM
    values = text.split()
    if len(values) != 12:
        raise MeshError("некорректный transform в 3MF: %s" % text)
    try:
        return tuple(float(value) for value in values)
    except ValueError:
        raise MeshError("некорректный transform в 3MF: %s" % text)


def compose(outer, inner):
    """Преобразование «сначала `inner`, потом `outer`» (оба в виде строк-векторов, как в 3MF)."""
    a = outer
    b = inner
    out = []
    for row in range(4):
        r = (b[3 * row], b[3 * row + 1], b[3 * row + 2])
        for col in range(3):
            value = r[0] * a[col] + r[1] * a[3 + col] + r[2] * a[6 + col]
            if row == 3:
                value += a[9 + col]
            out.append(value)
    return tuple(out)


def linear_part(transform):
    """Матрица 3x3 (по строкам, для векторов-столбцов) из преобразования 3MF."""
    m = transform
    return ((m[0], m[3], m[6]), (m[1], m[4], m[7]), (m[2], m[5], m[8]))


def _parse(data):
    """(единицы, {id: _Object}, [(id объекта, преобразование)]) из документа 3dmodel.model."""
    unit = "millimeter"
    objects = {}
    items = []
    current = None
    container = None   # <vertices> / <triangles>: разобранные дочерние элементы сразу выбрасываем
    try:
        for event, element in ET.iterparse(data, events=("start", "end")):
            tag = _local(element.tag)
            if event == "start":
                if tag == "model":
                    unit = element.get("unit") or "millimeter"
                elif tag == "object":
                    current = _Object(element.get("id"), element.get("name") or "")
                elif tag in ("vertices", "triangles"):
                    container = element
                continue
            if tag == "vertex" and current is not None:
                current.vertices.append(float(element.get("x")))
                current.vertices.append(float(element.get("y")))
                current.vertices.append(float(element.get("z")))
            elif tag == "triangle" and current is not None:
                current.triangles.append(int(element.get("v1")))
                current.triangles.append(int(element.get("v2")))
                current.triangles.append(int(element.get("v3")))
            elif tag == "component" and current is not None:
                current.components.append((element.get("objectid"), parse_transform(element.get("transform"))))
            elif tag == "object":
                if current is not None:
                    objects[current.id] = current
                current = None
            elif tag == "item":
                items.append((element.get("objectid"), parse_transform(element.get("transform"))))
            if tag in ("vertex", "triangle"):
                if container is not None and len(container) >= 4096:
                    del container[:]
            elif tag in ("vertices", "triangles", "component", "item", "object"):
                element.clear()
                container = None
    except ET.ParseError as exc:
        raise MeshError("некорректный XML 3MF: %s" % exc)
    except (TypeError, ValueError):
        raise MeshError("некорректная вершина/треугольник в 3MF")
    return unit, objects, items


def _emit(objects, object_id, transform, out, depth=0):
    """Добавляет треугольники объекта (вместе с компонентами), преобразованные `transform`."""
    obj = objects.get(object_id)
    if obj is None:
        raise MeshError("3MF ссылается на несуществующий объект %s" % object_id)
    if depth > MAX_DEPTH:
        raise MeshError("слишком глубокая вложенность компонентов 3MF")
    m = transform
    v = obj.vertices
    count = len(v) // 3
    for index in obj.triangles:
        if not 0 <= index < count:
            raise MeshError("индекс вершины вне диапазона в 3MF")
        o = 3 * index
        x, y, z = v[o], v[o + 1], v[o + 2]
        out.append(x * m[0] + y * m[3] + z * m[6] + m[9])
        out.append(x * m[1] + y * m[4] + z * m[7] + m[10])
        out.append(x * m[2] + y * m[5] + z * m[8] + m[11])
    for child_id, child_transform in obj.components:
        _emit(objects, child_id, compose(m, child_transform), out, depth + 1)


def read_3mf(path, part=None):
    """Сетка одного элемента build из 3MF (система файла, мм) с `load_rotation` и сведениями в `source`.

    `part` — номер элемента с 1; None — элемент с самыми большими габаритами. Если
    элементов несколько, `mesh.source["parts"]` перечисляет их все (имя, треугольники, размер).
    """
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                handle = archive.open(MODEL_PATH)
            except KeyError:
                raise MeshError("в 3MF нет %s (слайсер такой файл не откроет)" % MODEL_PATH)
            with handle:
                unit, objects, items = _parse(handle)
    except zipfile.BadZipfile:
        raise MeshError("файл не является 3MF (zip-архивом)")
    if not items:
        raise MeshError("в 3MF нет объектов для печати (<build><item>)")
    k = UNITS_MM.get(unit, 1.0)
    unit_scale = (k, 0.0, 0.0, 0.0, k, 0.0, 0.0, 0.0, k, 0.0, 0.0, 0.0)
    parts = []
    for number, (object_id, transform) in enumerate(items, 1):
        coords = array("f")
        _emit(objects, object_id, compose(unit_scale, transform), coords)
        if not coords:
            continue
        lo = [min(coords[i::3]) for i in range(3)]
        hi = [max(coords[i::3]) for i in range(3)]
        size = [hi[i] - lo[i] for i in range(3)]
        name = objects[object_id].name or "Object %d" % number
        parts.append((number, name, coords, size, transform))
    if not parts:
        raise MeshError("в 3MF нет треугольников")
    if part is None:
        chosen = max(parts, key=lambda p: (p[3][0] + 1e-6) * (p[3][1] + 1e-6) * (p[3][2] + 1e-6))
    else:
        chosen = next((p for p in parts if p[0] == part), None)
        if chosen is None:
            raise MeshError("в 3MF нет объекта part=%s (есть: %s)" % (part, ", ".join(str(p[0]) for p in parts)))
    number, name, coords, size, transform = chosen
    mesh = Mesh(coords, os.path.basename(path))
    item_rotation = vecmath.orthonormalize(linear_part(transform))
    mesh.load_rotation = vecmath.mat_mul(FLIP, item_rotation)
    mesh.source = {"format": "3mf", "unit": unit, "part": number, "part_name": name}
    if unit not in UNITS_MM:
        mesh.source["unit_note"] = "неизвестная единица %s, принято мм (как в слайсере)" % unit
    if len(parts) > 1:
        mesh.source["parts"] = [{"part": p[0], "name": p[1], "triangles": len(p[2]) // 9,
                                 "size_mm": [round(value, 1) for value in p[3]]} for p in parts]
    return mesh
