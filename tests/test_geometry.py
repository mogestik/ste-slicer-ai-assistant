"""Тесты agent/geometry и agent/tools/geometry_tools.py. Запуск из корня: python -m unittest

Синтетические тела (куб, цилиндр, труба, втулка, вал, наклонный цилиндр) проверяют
математику; 7 моделей кейса (models/*) сверяются с нашим ручным разбором моделей.
Если папки models нет, эти тесты пропускаются.
"""

import glob
import json
import math
import os
import struct
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "plugin", "AIAssistant")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from agent import geometry  # noqa: E402
from agent.geometry import mesh, vecmath  # noqa: E402
from agent.tools import geometry_tools  # noqa: E402
from agent.tools.registry import ToolContext, ToolRegistry  # noqa: E402

MODELS_DIR = os.path.join(ROOT, "models")


# Синтетические сетки

def revolve(profile, segments=96, axis=(0.0, 0.0, 1.0), origin=(0.0, 0.0, 0.0)):
    """Треугольники тела, полученного вращением замкнутого профиля (r, h) вокруг `axis` через `origin`."""
    e1, e2 = vecmath.perpendicular_basis(axis)

    def point(r, h, k):
        angle = 2.0 * math.pi * k / segments
        radial = vecmath.add(vecmath.scale(e1, r * math.cos(angle)), vecmath.scale(e2, r * math.sin(angle)))
        return vecmath.add(origin, vecmath.add(radial, vecmath.scale(axis, h)))

    triangles = []
    for index in range(len(profile)):
        (r0, h0), (r1, h1) = profile[index], profile[(index + 1) % len(profile)]
        for k in range(segments):
            a, b = point(r0, h0, k), point(r0, h0, k + 1)
            c, d = point(r1, h1, k + 1), point(r1, h1, k)
            if r0 > 0:
                triangles.append((a, b, c))
            if r1 > 0:
                triangles.append((a, c, d))
    return _oriented(triangles)


def box(sx, sy, sz):
    v = [(x * sx, y * sy, z * sz) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    triangles = []
    for a, b, c, d in faces:
        triangles.append((v[a], v[b], v[c]))
        triangles.append((v[a], v[c], v[d]))
    return _oriented(triangles)


def _oriented(triangles):
    volume = sum(vecmath.dot(a, vecmath.cross(b, c)) for a, b, c in triangles)
    if volume < 0:
        triangles = [(a, c, b) for a, b, c in triangles]
    return triangles


def flat(triangles):
    return [value for triangle in triangles for vertex in triangle for value in vertex]


def write_binary_stl(path, triangles):
    with open(path, "wb") as handle:
        handle.write(b"\0" * 80)
        handle.write(struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            handle.write(struct.pack("<12fH", 0, 0, 0, *(list(a) + list(b) + list(c) + [0])))


def write_ascii_stl(path, triangles):
    with open(path, "w") as handle:
        handle.write("solid test\n")
        for triangle in triangles:
            handle.write("facet normal 0 0 0\nouter loop\n")
            for vertex in triangle:
                handle.write("vertex %.6f %.6f %.6f\n" % vertex)
            handle.write("endloop\nendfacet\n")
        handle.write("endsolid test\n")


def analyse(triangles, support_angle=45.0):
    return geometry.analyze_mesh(mesh.mesh_from_triangles(flat(triangles), "file"), support_angle)


def placed_scene_points(points_file, mesh_obj, transform):
    """Применяет преобразование из отчёта так, как это сделал бы слайсер: узел — сетка, центрированная по габаритам."""
    centre = vecmath.file_to_scene(vecmath.scale(vecmath.add(mesh_obj.bbox_min, mesh_obj.bbox_max), 0.5))
    q = tuple(transform["quaternion_xyzw"])
    position = transform["node_position"]
    result = []
    for p in points_file:
        local = vecmath.sub(vecmath.file_to_scene(p), centre)
        result.append(vecmath.add(position, vecmath.quat_rotate(q, local)))
    return result


# Тесты на синтетике

class SyntheticTest(unittest.TestCase):
    def test_cube_properties(self):
        report = analyse(box(20, 30, 40))
        geo = report["geometry"]
        self.assertAlmostEqual(geo["volume_cm3"], 24.0, places=2)
        self.assertAlmostEqual(geo["surface_cm2"], 52.0, places=1)
        self.assertEqual(geo["center_of_mass_file"], [10.0, 15.0, 20.0])
        self.assertTrue(geo["closed_surface"])
        self.assertFalse(report["axis"]["is_revolution"])
        self.assertEqual(report["mode"]["printing_mode"], "classic")
        best = report["orientation_3d"][0]
        self.assertEqual(best["overhang_mm2"], 0)
        self.assertGreater(best["contact_mm2"], 599)

    def test_cylinder_axis_volume(self):
        report = analyse(revolve([(0, 0), (10, 0), (10, 30), (0, 30)], segments=128))
        expected = math.pi * 100 * 30 / 1000.0 * (128 / (2 * math.pi)) * math.sin(2 * math.pi / 128)
        self.assertAlmostEqual(report["geometry"]["volume_cm3"], round(expected, 2), places=2)
        axis = report["axis"]
        self.assertTrue(axis["is_revolution"])
        self.assertEqual(axis["axis_file"], "Z")
        self.assertAlmostEqual(axis["axis_point_file"][0], 0.0, places=1)
        self.assertEqual(axis["bore"]["axis_material_share"], 1.0)
        # Стоит на торце без свесов -> 3D.
        self.assertEqual(report["mode"]["printing_mode"], "classic")
        self.assertEqual(report["orientation_3d"][0]["overhang_mm2"], 0)

    def test_tube_bore(self):
        report = analyse(revolve([(5, 0), (15, 0), (15, 40), (5, 40)]))
        bore = report["axis"]["bore"]
        self.assertTrue(bore["through_bore"])
        self.assertAlmostEqual(bore["bore_min_diameter_mm"], 10.0, delta=0.05)
        self.assertGreaterEqual(bore["bore_roundness"], 0.99)
        self.assertEqual(bore["bore_support_share"], 1.0)

    def test_bushing_with_collar_is_5d_spiral(self):
        profile = [(5, 0), (12, 0), (12, 12), (20, 12), (20, 18), (12, 18), (12, 30), (5, 30)]
        report = analyse(revolve(profile))
        mode = report["mode"]
        self.assertEqual(mode["printing_mode"], "cylindrical")
        self.assertAlmostEqual(mode["settings"]["cylindrical_mode_base_diameter"], 10.0, delta=0.05)

    def test_shaft_with_collar_is_5d_full(self):
        profile = [(0, 0), (8, 0), (8, 12), (15, 12), (15, 18), (8, 18), (8, 30), (0, 30)]
        report = analyse(revolve(profile))
        self.assertEqual(report["mode"]["printing_mode"], "cylindrical_full")
        self.assertEqual(report["mode"]["settings"]["cylindrical_mode_base_diameter"], 6.0)

    def test_thin_ring_is_3d(self):
        report = analyse(revolve([(40, 0), (45, 0), (45, 5), (40, 5)]))
        self.assertEqual(report["mode"]["printing_mode"], "classic")

    def test_tilted_shaft_transform(self):
        axis = vecmath.normalize((1.0, 2.0, 3.0))
        origin = (40.0, -25.0, 7.0)
        profile = [(0, 0), (8, 0), (8, 12), (15, 12), (15, 18), (8, 18), (8, 30), (0, 30)]
        triangles = revolve(profile, axis=axis, origin=origin)
        mesh_obj = mesh.mesh_from_triangles(flat(triangles), "file")
        report = geometry.analyze_mesh(mesh_obj, 45.0)
        found = report["axis"]["axis_direction_file"]
        self.assertGreater(abs(vecmath.dot(found, axis)), 0.999)
        transform = report["placement_5d"]["transform"]
        bottom, top = placed_scene_points([origin, vecmath.add(origin, vecmath.scale(axis, 30.0))],
                                          mesh_obj, transform)
        # Ось вертикально, через X=Z=0 сцены, деталь стоит на столе.
        for point in (bottom, top):
            self.assertAlmostEqual(point[0], 0.0, delta=0.05)
            self.assertAlmostEqual(point[2], 0.0, delta=0.05)
        self.assertAlmostEqual(min(bottom[1], top[1]), 0.0, delta=0.05)
        self.assertAlmostEqual(abs(top[1] - bottom[1]), 30.0, delta=0.05)

    def test_rotation_steps_match_quaternion(self):
        for up in ((0, 0, 1), (0, 0, -1), (0, 1, 0), (0, -1, 0), (1, 0, 0), (-1, 0, 0), (0.3, -0.5, 0.8)):
            rotation = geometry.orientation.scene_rotation(up)
            q = (0.0, 0.0, 0.0, 1.0)
            for name, angle in rotation["steps"]:
                axis = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[name]
                q = vecmath.quat_mul(vecmath.quat_from_axis_angle(axis, math.radians(angle)), q)
            mapped = vecmath.quat_rotate(q, vecmath.file_to_scene(vecmath.normalize(up)))
            self.assertAlmostEqual(mapped[1], 1.0, places=6)
            same = vecmath.quat_rotate(rotation["quaternion"], (0.3, 0.2, 0.1))
            self.assertAlmostEqual(vecmath.norm(vecmath.sub(same, vecmath.quat_rotate(q, (0.3, 0.2, 0.1)))), 0.0,
                                   places=6)

    def test_stl_readers(self):
        triangles = revolve([(5, 0), (15, 0), (15, 40), (5, 40)], segments=32)
        directory = tempfile.mkdtemp()
        for writer, name in ((write_binary_stl, "tube_bin.stl"), (write_ascii_stl, "tube_ascii.stl")):
            path = os.path.join(directory, name)
            writer(path, triangles)
            loaded = mesh.read_stl(path).compute()
            self.assertEqual(loaded.triangle_count, len(triangles))
            self.assertAlmostEqual(loaded.volume / 1000.0,
                                   mesh.mesh_from_triangles(flat(triangles)).compute().volume / 1000.0, places=3)
            os.remove(path)
        os.rmdir(directory)

    def test_scene_frame_input(self):
        triangles = box(10, 20, 30)
        scene = [value for t in triangles for v in t for value in vecmath.file_to_scene(v)]
        report = geometry.analyze_triangles(scene, frame="scene")
        self.assertEqual(report["geometry"]["size_file_xyz_mm"], [10.0, 20.0, 30.0])


# Инструменты
class _Uploads(object):
    def __init__(self, records):
        self.records = records

    def get(self, upload_id):
        return next((r for r in self.records if r["id"] == upload_id), None)

    def list_for_session(self, session_id):
        return [r for r in self.records if r["session"] == session_id]


class ToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.path = os.path.join(cls.directory, "bushing.stl")
        write_binary_stl(cls.path, revolve([(5, 0), (12, 0), (12, 12), (20, 12), (20, 18), (12, 18), (12, 30),
                                            (5, 30)]))
        cls.registry = ToolRegistry()
        geometry_tools.register(cls.registry)
        uploads = _Uploads([{"id": "abc", "session": "s1", "kind": "model", "path": cls.path, "name": "bushing.stl"}])
        cls.ctx = ToolContext(session_id="s1", services={"uploads": uploads})

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.path)
        os.rmdir(cls.directory)

    def call(self, name, args):
        result = self.registry.call(name, args, self.ctx)
        return result.ok, (json.loads(result.text) if result.ok else result.text)

    def test_registered(self):
        names = [tool.name for tool in self.registry.tools()]
        self.assertEqual(names, ["analyze_model", "recommend_print_mode", "suggest_orientation"])
        self.assertTrue(all(not tool.main_thread and tool.group == "geometry" for tool in self.registry.tools()))

    def test_analyze_by_path_and_attachment(self):
        ok, data = self.call("analyze_model", {"path": self.path})
        self.assertTrue(ok, data)
        self.assertEqual(data["recommended_mode"]["printing_mode"], "cylindrical")
        self.assertIn("5D Spiral", data["summary"])
        ok, data2 = self.call("analyze_model", {"attachment_id": "abc"})
        self.assertTrue(ok)
        ok, data3 = self.call("recommend_print_mode", {})
        self.assertTrue(ok)
        self.assertEqual(data3["printing_mode"], "cylindrical")
        self.assertIn("transform", data3["placement"])

    def test_suggest_orientation(self):
        ok, data = self.call("suggest_orientation", {"path": self.path, "mode": "classic", "count": 2})
        self.assertTrue(ok)
        self.assertEqual(len(data["candidates"]), 2)
        self.assertLessEqual(data["candidates"][0]["overhang_mm2"], data["candidates"][1]["overhang_mm2"])
        ok, data = self.call("suggest_orientation", {"path": self.path})
        self.assertTrue(ok)
        self.assertEqual(data["mode"], "cylindrical")
        self.assertIn("placement", data)

    def test_errors(self):
        ok, text = self.call("analyze_model", {"path": "/no/such/file.stl"})
        self.assertFalse(ok)
        self.assertIn("не найден", text)
        ok, text = self.call("analyze_model", {"attachment_id": "zzz"})
        self.assertFalse(ok)
        ok, text = self.call("suggest_orientation", {"path": self.path, "mode": "bad"})
        self.assertFalse(ok)


# 7 моделей кейса

# префикс папки -> ожидание (режим, ось, лучший «верх» в 3D, «верх» в 5D) — по нашему ручному разбору моделей
EXPECTED = {
    "1.": ("classic", "Y", [0, 1, 0], None),
    "2.": ("cylindrical", "Y", [0, 1, 0], [0, 1, 0]),
    "3.": ("classic", "X", [1, 0, 0], None),
    "4.": ("classic", None, [0, 0, 1], None),
    "5.": ("cylindrical_full", "Y", [0, -1, 0], [0, 1, 0]),
    "6.": ("classic", "Y", [0, -1, 0], None),
    "7.": ("cylindrical_full", "Z", [0, 0, -1], [0, 0, 1]),
}


def model_files():
    files = {}
    for path in glob.glob(os.path.join(MODELS_DIR, "*", "*")):
        if path.lower().endswith(".stl"):
            files[os.path.basename(os.path.dirname(path))[:2]] = path
    return files


@unittest.skipUnless(len(model_files()) == 7, "нет моделей кейса в models/ (см. models/README.md)")
class CaseModelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = {}
        cls.times = {}
        for prefix, path in sorted(model_files().items()):
            started = time.time()
            cls.reports[prefix] = geometry.analyze_file(path, 45.0)
            cls.times[prefix] = time.time() - started

    def test_modes_axes_orientations(self):
        for prefix, (mode, axis, up_3d, up_5d) in EXPECTED.items():
            report = self.reports[prefix]
            self.assertEqual(report["mode"]["printing_mode"], mode, prefix)
            if axis is None:
                self.assertFalse(report["axis"]["is_revolution"], prefix)
            else:
                self.assertEqual(report["axis"]["axis_file"], axis, prefix)
            self.assertEqual(report["orientation_3d"][0]["up_file"], up_3d, prefix)
            if up_5d is not None:
                self.assertEqual(report["placement_5d"]["up_file"], up_5d, prefix)

    def test_numbers_match_manual_analysis(self):
        r = self.reports
        self.assertAlmostEqual(r["1."]["geometry"]["volume_cm3"], 3.80, delta=0.01)
        self.assertAlmostEqual(r["4."]["geometry"]["volume_cm3"], 725.7, delta=0.1)
        self.assertAlmostEqual(r["2."]["mode"]["settings"]["cylindrical_mode_base_diameter"], 16.0, delta=0.05)
        self.assertAlmostEqual(r["1."]["axis"]["bore"]["bore_min_diameter_mm"], 4.3, delta=0.05)
        # Ось вала (а не центр габаритов: кулачок с одной стороны).
        point = r["7."]["axis"]["axis_point_file"]
        self.assertAlmostEqual(point[0], 30.9, delta=0.1)
        self.assertAlmostEqual(point[1], 24.8, delta=0.1)
        # Позы 3D (ручной разбор, свесы при 45°): шестерня 0 / 730, колесо 570 / 1800, вал 100 / 794.
        self.assertEqual(r["1."]["orientation_3d"][0]["overhang_mm2"], 0)
        self.assertAlmostEqual(r["2."]["orientation_3d"][0]["overhang_mm2"], 570, delta=20)
        self.assertAlmostEqual(r["2."]["orientation_3d"][0]["contact_mm2"], 1800, delta=20)
        self.assertAlmostEqual(r["7."]["orientation_3d"][0]["overhang_mm2"], 100, delta=10)
        self.assertAlmostEqual(r["7."]["orientation_3d"][0]["contact_mm2"], 794, delta=10)

    def test_5d_placement_puts_axis_on_centre(self):
        for prefix in ("2.", "5.", "7."):
            report = self.reports[prefix]
            path = model_files()[prefix]
            loaded = mesh.read_stl(path).compute()
            point = report["axis"]["axis_point_file"]
            direction = report["axis"]["axis_direction_file"]
            far = vecmath.add(point, vecmath.scale(direction, report["axis"]["length_mm"]))
            for placed in placed_scene_points([point, far], loaded, report["placement_5d"]["transform"]):
                self.assertAlmostEqual(placed[0], 0.0, delta=0.05, msg=prefix)
                self.assertAlmostEqual(placed[2], 0.0, delta=0.05, msg=prefix)

    def test_speed(self):
        for prefix, seconds in self.times.items():
            self.assertLess(seconds, 30.0, prefix)


if __name__ == "__main__":
    unittest.main()
