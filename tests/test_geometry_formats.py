"""Чтение OBJ / 3MF и анализ объектов на сцене (agent/geometry, tools/geometry_tools.py).

* STL-модели переводим в OBJ и 3MF (во временной папке), и анализ должен совпасть с
  анализом STL с учётом того, как слайсер загружает каждый формат (3MF: поворот узла
  Flip * R_item, единицы, преобразования элементов build);
* объекты сцены (мок слайсера): загрузка -> произвольный поворот -> масштаб ->
  analyze_model(object_id) -> rotate_object(steps, reset_rotation=true) должны дать ту же
  позу, что и анализ файла, применённый к свежему объекту.

Запуск из корня репозитория: python -m unittest tests.test_geometry_formats
"""

import glob
import json
import math
import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_DIR, "plugin", "AIAssistant")
DEVTOOLS_DIR = os.path.join(REPO_DIR, "devtools")
for _path in (PLUGIN_DIR, DEVTOOLS_DIR, os.path.dirname(os.path.abspath(__file__))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from agent import geometry, main_thread  # noqa: E402
from agent.geometry import mesh, threemf, vecmath  # noqa: E402
from agent.slicer import set_adapter  # noqa: E402
from agent.tools import build_registry  # noqa: E402
from agent.tools.registry import ToolContext  # noqa: E402
import mock_slicer  # noqa: E402
from test_geometry import box, revolve, write_binary_stl  # noqa: E402

MODELS_DIR = os.path.join(REPO_DIR, "models")
# Модели кейса для проверки форматов (префикс папки): колесо (5D Spiral), крыльчатка (3D), ось Krones (5D).
CASE_PREFIXES = ("2.", "6.", "7.")
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


# Преобразование форматов
def stl_triangles(path):
    m = mesh.read_stl(path)
    c = m.coords
    return [((c[o], c[o + 1], c[o + 2]), (c[o + 3], c[o + 4], c[o + 5]), (c[o + 6], c[o + 7], c[o + 8]))
            for o in range(0, len(c), 9)]


def indexed(triangles, transform=None):
    """(вершины, грани) с общими вершинами; `transform(v)` преобразует каждую вершину."""
    index = {}
    vertices = []
    faces = []
    for triangle in triangles:
        face = []
        for v in triangle:
            key = tuple(v)
            if key not in index:
                index[key] = len(vertices)
                vertices.append(transform(v) if transform else v)
            face.append(index[key])
        faces.append(face)
    return vertices, faces


def write_obj(path, triangles):
    """OBJ с общими вершинами; грани по очереди в формах `v`, `v//vn` и с отрицательными индексами."""
    vertices, faces = indexed(triangles)
    with open(path, "w") as handle:
        handle.write("# test\no part\n")
        for v in vertices:
            handle.write("v %r %r %r\n" % tuple(float(x) for x in v))
        handle.write("vn 0 0 1\n")
        n = len(vertices)
        for k, (a, b, c) in enumerate(faces):
            if k % 3 == 0:
                handle.write("f %d %d %d\n" % (a + 1, b + 1, c + 1))
            elif k % 3 == 1:
                handle.write("f %d//1 %d//1 %d//1\n" % (a + 1, b + 1, c + 1))
            else:
                handle.write("f %d %d %d\n" % (a - n, b - n, c - n))


def write_3mf(path, objects, unit="millimeter"):
    """3MF, по элементу build на каждую запись `objects`: (треугольники, строка преобразования или "", имя, map)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<model unit="%s" xml:lang="en-US" xmlns="%s">'
             '<resources>' % (unit, NS)]
    items = []
    for number, (triangles, transform, name, vertex_map) in enumerate(objects, 1):
        vertices, faces = indexed(triangles, vertex_map)
        parts.append('<object id="%d" type="model" name="%s"><mesh><vertices>' % (number, name))
        parts.extend('<vertex x="%r" y="%r" z="%r"/>' % tuple(float(x) for x in v) for v in vertices)
        parts.append('</vertices><triangles>')
        parts.extend('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(f) for f in faces)
        parts.append('</triangles></mesh></object>')
        items.append('<item objectid="%d"%s/>' % (number, ' transform="%s"' % transform if transform else ""))
    parts.append('</resources><build>%s</build></model>' % "".join(items))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("3D/3dmodel.model", "".join(parts))


def transform_string(rotation, translation=(0.0, 0.0, 0.0)):
    """Преобразование 3MF для x' = R x + t (R по строкам) -> запись строками-векторами m00 m01 m02 ... m32."""
    m = [rotation[col][row] for row in range(3) for col in range(3)]
    return " ".join(repr(float(v)) for v in m + list(translation))


def steps_matrix(steps):
    return vecmath.steps_to_matrix([(s["axis"], s["angle_deg"]) for s in steps])


def comparable(report):
    """Отчёт без сведений об источнике и времени (JSON, округлённые числа)."""
    data = dict(report)
    data.pop("file", None)
    data.pop("analysis_time_s", None)
    return json.loads(json.dumps(data))


def case_models():
    files = {}
    for path in glob.glob(os.path.join(MODELS_DIR, "*", "*")):
        prefix = os.path.basename(os.path.dirname(path))[:2]
        if path.lower().endswith(".stl") and prefix in CASE_PREFIXES:
            files[prefix] = path
    return files


BUSHING = [(5, 0), (12, 0), (12, 4), (20, 4), (20, 10), (12, 10), (12, 30), (5, 30)]   # 5D Spiral, буртик не по центру
SHAFT = [(0, 0), (8, 0), (8, 6), (15, 6), (15, 12), (8, 12), (8, 40), (0, 40)]           # 5D, буртик не по центру


def l_bracket():
    """Несимметричная 3D-деталь: пластина с ребром (два перекрывающихся бруска)."""
    rib = [tuple(vecmath.add(v, (0.0, 0.0, 4.0)) for v in t) for t in box(6, 25, 30)]
    return box(40, 25, 4) + rib


# Форматы
class FormatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="geometry_formats_")
        cls.sources = {}
        for name, triangles in (("bushing", revolve(BUSHING, segments=64)), ("bracket", l_bracket())):
            path = os.path.join(cls.tmp, name + ".stl")
            write_binary_stl(path, triangles)
            cls.sources[name] = path
        for prefix, path in sorted(case_models().items()):
            cls.sources["case" + prefix] = path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def convert(self, name, kind, **kwargs):
        triangles = stl_triangles(self.sources[name])
        path = os.path.join(self.tmp, "%s_%s.%s" % (name, kwargs.get("tag", "plain"), kind))
        if kind == "obj":
            write_obj(path, triangles)
        else:
            write_3mf(path, [(triangles, kwargs.get("transform", ""), name, kwargs.get("vertex_map"))],
                      unit=kwargs.get("unit", "millimeter"))
        return path

    def test_obj_equals_stl(self):
        for name in sorted(self.sources):
            stl = geometry.analyze_file(self.sources[name], 45.0)
            obj = geometry.analyze_file(self.convert(name, "obj"), 45.0)
            self.assertEqual(comparable(obj), comparable(stl), name)

    def test_3mf_equals_stl_with_load_flip(self):
        flip = threemf.FLIP
        for name in sorted(self.sources):
            stl = geometry.analyze_file(self.sources[name], 45.0)
            report = geometry.analyze_file(self.convert(name, "3mf"), 45.0)
            self.assertEqual(report["file"]["format"], "3mf")
            self.assertEqual(report["geometry"], stl["geometry"], name)
            self.assertEqual(report["axis"], stl["axis"], name)
            self.assertEqual(report["mode"], stl["mode"], name)
            pairs = [(stl["orientation_3d"][0], report["orientation_3d"][0])]
            if "placement_5d" in stl:
                pairs.append((stl["placement_5d"], report["placement_5d"]))
            for a, b in pairs:
                self.assertEqual(a["up_file"], b["up_file"], name)
                self.assertEqual(a["transform"]["bbox_center_scene"], b["transform"]["bbox_center_scene"], name)
                # В узле 3MF уже есть Flip; reset_rotation его сбрасывает -> steps(3MF) = steps(STL) * Flip.
                expected = vecmath.mat_mul(steps_matrix(a["transform"]["rotation_steps"]), flip)
                self.assert_matrix(steps_matrix(b["transform"]["rotation_steps"]), expected, name)

    def test_3mf_units_and_item_rotation(self):
        """Вершины в дюймах и заранее повёрнуты; элемент build поворачивает их обратно — анализ как у STL."""
        r_item = vecmath.steps_to_matrix([("z", 90.0), ("x", 30.0)])
        inverse = vecmath.transpose(r_item)
        k = 25.4
        for name in ("bushing", "bracket"):
            stl = geometry.analyze_file(self.sources[name], 45.0)
            path = self.convert(name, "3mf", tag="inch", unit="inch", transform=transform_string(r_item),
                                vertex_map=lambda v: vecmath.scale(vecmath.mat_vec(inverse, v), 1.0 / k))
            report = geometry.analyze_file(path, 45.0)
            self.assertEqual(report["file"]["unit"], "inch")
            self.assertEqual(report["mode"]["printing_mode"], stl["mode"]["printing_mode"], name)
            for key in ("size_file_xyz_mm", "center_of_mass_file"):
                for a, b in zip(report["geometry"][key], stl["geometry"][key]):
                    self.assertAlmostEqual(a, b, delta=0.02, msg=name)
            self.assertAlmostEqual(report["geometry"]["volume_cm3"], stl["geometry"]["volume_cm3"], delta=0.02)
            a, b = stl["orientation_3d"][0], report["orientation_3d"][0]
            self.assertEqual(a["up_file"], b["up_file"], name)
            expected = vecmath.mat_mul(steps_matrix(a["transform"]["rotation_steps"]),
                                       vecmath.mat_mul(threemf.FLIP, r_item))
            self.assert_matrix(steps_matrix(b["transform"]["rotation_steps"]), expected, name)

    def test_3mf_parts_and_components(self):
        small = box(10, 10, 10)
        big = revolve(SHAFT, segments=48)
        path = os.path.join(self.tmp, "two.3mf")
        write_3mf(path, [(small, "1 0 0 0 1 0 0 0 1 100 0 0", "cube", None), (big, "", "shaft", None)])
        report = geometry.analyze_file(path, 45.0)
        self.assertEqual(report["file"]["part"], 2)
        self.assertEqual([p["name"] for p in report["file"]["parts"]], ["cube", "shaft"])
        self.assertEqual(report["mode"]["printing_mode"], "cylindrical_full")
        cube = geometry.analyze_file(path, 45.0, part=1)
        self.assertEqual(cube["geometry"]["size_file_xyz_mm"], [10.0, 10.0, 10.0])
        self.assertEqual(cube["geometry"]["bbox_min_file"], [100.0, 0.0, 0.0])
        with self.assertRaises(mesh.MeshError):
            geometry.analyze_file(path, 45.0, part=3)
        # Объект из двух компонентов (один сдвинут) — это одна деталь.
        vertices, faces = indexed(small)
        xml = ['<model unit="millimeter" xmlns="%s"><resources><object id="1" type="model"><mesh><vertices>' % NS]
        xml.extend('<vertex x="%r" y="%r" z="%r"/>' % tuple(float(x) for x in v) for v in vertices)
        xml.append('</vertices><triangles>')
        xml.extend('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(f) for f in faces)
        xml.append('</triangles></mesh></object><object id="2" type="model"><components>'
                   '<component objectid="1"/><component objectid="1" transform="1 0 0 0 1 0 0 0 2 0 0 10"/>'
                   '</components></object></resources><build><item objectid="2"/></build></model>')
        comp = os.path.join(self.tmp, "components.3mf")
        with zipfile.ZipFile(comp, "w") as archive:
            archive.writestr("3D/3dmodel.model", "".join(xml))
        report = geometry.analyze_file(comp, 45.0)
        self.assertEqual(report["geometry"]["size_file_xyz_mm"], [10.0, 10.0, 30.0])
        self.assertAlmostEqual(report["geometry"]["volume_cm3"], 3.0, places=2)

    def test_obj_polygons(self):
        path = os.path.join(self.tmp, "quads.obj")
        with open(path, "w") as handle:
            handle.write("v 0 0 0\nv 10 0 0\nv 10 20 0\nv 0 20 0\nv 0 0 30\nv 10 0 30\nv 10 20 30\nv 0 20 30\n")
            handle.write("f 1 4 3 2\nf 5 6 7 8\nf 1/1 2/2 6/6 5/5\nf 2 3 7 6\nf 3 4 8 7\nf 4 1 5 8\n")
        report = geometry.analyze_file(path, 45.0)
        self.assertEqual(report["geometry"]["triangles"], 12)
        self.assertAlmostEqual(report["geometry"]["volume_cm3"], 6.0, places=3)
        self.assertTrue(report["geometry"]["closed_surface"])

    def test_bad_files(self):
        path = os.path.join(self.tmp, "bad.3mf")
        with open(path, "wb") as handle:
            handle.write(b"not a zip")
        with self.assertRaises(mesh.MeshError):
            geometry.analyze_file(path)
        with self.assertRaises(mesh.MeshError):
            mesh.read_mesh(os.path.join(self.tmp, "model.ply"))

    def assert_matrix(self, actual, expected, msg=None):
        for row_a, row_e in zip(actual, expected):
            for a, e in zip(row_a, row_e):
                self.assertAlmostEqual(a, e, delta=2e-3, msg=msg)


# Объекты сцены на моке слайсера

class FakeQtApp(object):
    def __init__(self):
        self.thread = threading.current_thread()
        self.queue = queue.Queue()

    def getMainThread(self):
        return self.thread

    def callLater(self, func, *args, **kwargs):
        self.queue.put((func, args, kwargs))


P_FROM_SCENE_FLIP = [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]]


class SceneObjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="geometry_scene_")
        self.app = FakeQtApp()
        main_thread.set_app_provider(lambda: self.app)
        self.registry = build_registry()
        self.mock = mock_slicer.install(self.registry, slice_step=0.01)
        self.ctx = ToolContext(session_id="test", services={})
        self.call("set_print_mode", {"mode": "classic"})

    def tearDown(self):
        main_thread.set_app_provider(None)
        set_adapter(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, name, args=None):
        box_ = {}

        def worker():
            box_["result"] = self.registry.call(name, args or {}, self.ctx)
            self.app.queue.put(None)

        thread = threading.Thread(target=worker)
        thread.start()
        while True:
            item = self.app.queue.get(timeout=60)
            if item is None:
                break
            func, a, k = item
            func(*a, **k)
        thread.join()
        result = box_["result"]
        self.assertTrue(result.ok, "%s -> %s" % (name, result.text))
        return json.loads(result.text)

    def load(self, path):
        return str(self.call("load_model", {"path": path})["loaded"][0]["id"])

    def place(self, object_id, transform, factor=1.0):
        self.call("rotate_object", {"object_id": object_id, "steps": transform["rotation_steps"],
                                    "reset_rotation": True})
        centre = transform["bbox_center_scene"]
        self.call("move_object", {"object_id": object_id, "frame": "scene", "x": centre[0] * factor,
                                  "z": centre[2] * factor})

    def world(self, index):
        return [tuple(float(c) for c in v) for v in self.mock.objects[index].world()]

    def assert_same_pose(self, a, b, tolerance=0.1):
        """Те же вершины с точностью до сдвига по столу (эталонный объект стоит в другом месте).

        Одну и ту же сетку сравниваем по порядку вершин, STL с индексным 3MF — как множества вершин.
        """
        for points in (a, b):
            self.assertAlmostEqual(min(v[2] for v in points), 0.0, delta=0.01)   # на столе
        centres = [[(min(v[i] for v in points) + max(v[i] for v in points)) / 2.0 for i in range(2)]
                   for points in (a, b)]
        dx, dy = centres[0][0] - centres[1][0], centres[0][1] - centres[1][1]
        b = [(v[0] + dx, v[1] + dy, v[2]) for v in b]
        if len(a) == len(b):
            worst = max(max(abs(p - q) for p, q in zip(u, v)) for u, v in zip(a, b))
        else:
            a, b = set(a), set(b)
            worst = max(max(min(max(abs(p - q) for p, q in zip(u, v)) for v in other) for u in one)
                        for one, other in ((a, b), (b, a)))
        self.assertLess(worst, tolerance)

    @staticmethod
    def transform_of(data, mode):
        placement = data["placement"] if "placement" in data else data["candidates"][0]
        return placement["transform"]

    def check_round_trip(self, path, mode, factor, arbitrary, emulate=None):
        """Свежий объект, поставленный по анализу файла, == повёрнутый и отмасштабированный объект, поставленный по своему анализу."""
        reference = self.load(path)
        if emulate:
            emulate(0)
        if factor != 1.0:
            self.call("scale_object", {"object_id": reference, "factor": factor})
        by_file = self.call("suggest_orientation", {"path": path, "mode": mode})
        self.place(reference, self.transform_of(by_file, mode), factor)

        moved = self.load(path)
        if emulate:
            emulate(1)
        self.call("rotate_object", {"object_id": moved, "steps": arbitrary})
        if factor != 1.0:
            self.call("scale_object", {"object_id": moved, "factor": factor})
        by_object = self.call("suggest_orientation", {"object_id": moved, "mode": mode})
        self.assertEqual(by_object["source"], self.mock.objects[1].name)
        self.place(moved, self.transform_of(by_object, mode))
        self.assert_same_pose(self.world(0), self.world(1))
        return by_file, by_object

    def test_scene_object_stl_3d_and_5d(self):
        arbitrary = [{"axis": "x", "angle_deg": 37}, {"axis": "y", "angle_deg": -110}, {"axis": "z", "angle_deg": 15}]
        bracket = os.path.join(self.tmp, "bracket.stl")
        write_binary_stl(bracket, l_bracket())
        self.check_round_trip(bracket, "classic", 1.5, arbitrary)
        self.mock.objects[:] = []
        shaft = os.path.join(self.tmp, "shaft.stl")
        write_binary_stl(shaft, revolve(SHAFT, segments=48))
        self.call("set_print_mode", {"mode": "cylindrical_full"})
        by_file, by_object = self.check_round_trip(shaft, "cylindrical_full", 2.0, arbitrary)
        self.assertEqual(by_object["placement"]["up_file"], by_file["placement"]["up_file"])

    def test_scene_object_scale_is_seen(self):
        bracket = os.path.join(self.tmp, "bracket.stl")
        write_binary_stl(bracket, l_bracket())
        object_id = self.load(bracket)
        self.call("rotate_object", {"object_id": object_id, "axis": "x", "angle_deg": 90})
        self.call("scale_object", {"object_id": object_id, "x": 2.0, "y": 1.0, "z": 0.5})
        data = self.call("analyze_model", {"object_id": object_id})
        self.assertEqual(data["source"]["scale"], [2.0, 1.0, 0.5])
        for a, b in zip(data["geometry"]["size_file_xyz_mm"], [80.0, 25.0, 17.0]):
            self.assertAlmostEqual(a, b, delta=0.05)
        # Кэш: повторный анализ того же объекта (другим инструментом) ответ не меняет.
        again = self.call("recommend_print_mode", {"object_id": object_id})
        self.assertEqual(again["printing_mode"], data["recommended_mode"]["printing_mode"])

    def test_3mf_like_the_slicer_loads_it(self):
        """Мок загружает 3MF как ThreeMFReader: исходная сетка, поворот узла Flip * R_item, масштаб единиц."""
        stl = os.path.join(self.tmp, "bracket.stl")
        write_binary_stl(stl, l_bracket())
        path = os.path.join(self.tmp, "bracket.3mf")
        k = 25.4
        write_3mf(path, [(l_bracket(), "", "bracket", lambda v: vecmath.scale(v, 1.0 / k))], unit="inch")
        # Эталон — STL, поставленный по своему анализу; 3MF, поставленный по анализу 3MF, должен с ним совпасть.
        reference = self.load(stl)
        by_stl = self.call("suggest_orientation", {"path": stl, "mode": "classic"})
        self.place(reference, self.transform_of(by_stl, "classic"))
        loaded = self.load(path)
        obj = self.mock.objects[1]
        self.assertEqual(obj.scale, [k, k, k])
        self.assert_matrix(obj.rotation, P_FROM_SCENE_FLIP)   # Flip вокруг X в системе принтера мока
        by_3mf = self.call("suggest_orientation", {"path": path, "mode": "classic"})
        self.place(loaded, self.transform_of(by_3mf, "classic"))
        self.assert_same_pose(self.world(0), self.world(1))
        # Тот же объект 3MF, проанализированный на сцене после произвольного поворота.
        self.call("rotate_object", {"object_id": loaded, "steps": [{"axis": "y", "angle_deg": 63}]})
        by_object = self.call("suggest_orientation", {"object_id": loaded, "mode": "classic"})
        self.place(loaded, self.transform_of(by_object, "classic"))
        self.assert_same_pose(self.world(0), self.world(1))

    def test_3mf_items_are_separate_objects(self):
        """Каждый элемент build — объект; преобразование элемента и единицы дают позу из файла."""
        r_item = vecmath.steps_to_matrix([("z", 90.0), ("x", 30.0)])
        inverse = vecmath.transpose(r_item)
        k = 25.4
        stl = os.path.join(self.tmp, "bracket.stl")
        write_binary_stl(stl, l_bracket())
        path = os.path.join(self.tmp, "two.3mf")
        write_3mf(path, [(box(10, 10, 10), "1 0 0 0 1 0 0 0 1 2 0 0", "cube", None),
                         (l_bracket(), transform_string(r_item, (4.0, 1.0, 0.0)), "bracket",
                          lambda v: vecmath.scale(vecmath.mat_vec(inverse, v), 1.0 / k))], unit="inch")
        self.call("set_print_mode", {"mode": "classic"})
        loaded = self.call("load_model", {"path": path})["loaded"]
        self.assertEqual(len(loaded), 2)
        self.assertEqual([o["size_mm"] for o in loaded][0], [254.0, 254.0, 254.0])
        for a, b in zip(loaded[1]["size_mm"], [40.0, 25.0, 34.0]):
            self.assertAlmostEqual(a, b, delta=0.01)
        # Элемент остаётся на месте из файла (без расстановки): k * (raw + t_item + c) - стол / 2, где c —
        # центр исходной сетки (ThreeMFReader._read); куб 0..10 дюймов, t_item = (2, 0, 0).
        expected = [k * 12.0 - mock_slicer.WIDTH / 2.0, k * 10.0 - mock_slicer.DEPTH / 2.0]
        for a, b in zip(loaded[0]["center_mm"][:2], expected):
            self.assertAlmostEqual(a, b, delta=0.01)
        self.load(stl)
        self.assert_same_pose(self.world(2), self.world(1))     # поворот элемента отменяет предварительный поворот вершин
        # Анализ элемента на сцене видит ту же деталь, что и STL.
        by_object = self.call("analyze_model", {"object_id": loaded[1]["id"]})
        by_stl = self.call("analyze_model", {"path": stl})
        self.assertEqual(by_object["recommended_mode"]["printing_mode"], by_stl["recommended_mode"]["printing_mode"])
        # (its own frame is the raw, pre-rotated mesh: sizes along other axes, the same volume)
        self.assertAlmostEqual(by_object["geometry"]["volume_cm3"], by_stl["geometry"]["volume_cm3"], delta=0.02)

    def assert_matrix(self, actual, expected):
        for row_a, row_e in zip(actual, expected):
            for a, e in zip(row_a, row_e):
                self.assertAlmostEqual(a, e, places=6)


class SceneObjectWorldMeshTest(SceneObjectTest):
    """Тот же анализ сцены на адаптере без get_object_local_mesh (мировая сетка, поворот снят)."""

    def setUp(self):
        SceneObjectTest.setUp(self)

        def not_implemented(ref):
            raise NotImplementedError

        self.mock.get_object_local_mesh = not_implemented


class LocalMeshUsedTest(SceneObjectTest):
    """Если get_object_local_mesh есть, анализ сцены берёт сетку из него."""

    def setUp(self):
        SceneObjectTest.setUp(self)
        self.local_calls = []
        original = self.mock.get_object_local_mesh

        def counted(ref):
            self.local_calls.append(ref)
            return original(ref)

        self.mock.get_object_local_mesh = counted
        self.mock.get_object_mesh = None    # не должен понадобиться

    def tearDown(self):
        self.assertTrue(self.local_calls)
        SceneObjectTest.tearDown(self)


if __name__ == "__main__":
    unittest.main()
