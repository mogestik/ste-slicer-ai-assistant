"""Логика слоя адаптера без Qt: координаты, математика сеток, приведение значений, помощники.

Запуск из корня репозитория: python -m unittest tests.test_tools_slicer_math
"""

import math
import os
import sys
import unittest

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "AIAssistant")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from agent.slicer import coords  # noqa: E402
from agent.slicer.base import ObjectIds, SlicerError, resolve_object  # noqa: E402
from agent.slicer.qt_adapter import format_duration, pick_material  # noqa: E402
from agent.slicer.values import ValueProblem, coerce_value, parse_number, polymer_of  # noqa: E402


def box(sx, sy, sz, origin=(0.0, 0.0, 0.0)):
    """Замкнутый брусок вдоль осей как (вершины, грани), нормали наружу."""
    x0, y0, z0 = origin
    v = [(x0, y0, z0), (x0 + sx, y0, z0), (x0 + sx, y0 + sy, z0), (x0, y0 + sy, z0),
         (x0, y0, z0 + sz), (x0 + sx, y0, z0 + sz), (x0 + sx, y0 + sy, z0 + sz), (x0, y0 + sy, z0 + sz)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v, f


def close(a, b, tol=1e-6):
    return all(abs(x - y) < tol for x, y in zip(a, b))


class FramesTest(unittest.TestCase):
    def test_scene_printer_roundtrip(self):
        p = (1.0, 2.0, 3.0)
        s = coords.printer_to_scene(p)
        self.assertEqual(s, (1.0, 3.0, -2.0))
        self.assertEqual(coords.scene_to_printer(s), p)

    def test_printer_axes(self):
        # Z принтера (вверх) — это Y сцены, Y принтера (назад) — это -Z сцены
        self.assertEqual(coords.PRINTER_AXES_IN_SCENE["z"], (0.0, 1.0, 0.0))
        self.assertEqual(coords.PRINTER_AXES_IN_SCENE["y"], (0.0, 0.0, -1.0))

    def test_axis_angle_right_handed(self):
        m = coords.axis_angle_matrix((0, 0, 1), 90)
        self.assertTrue(close(coords.apply_matrix(m, (1, 0, 0)), (0, 1, 0)))

    def test_printer_matrix_to_scene_is_consistent(self):
        m_printer = coords.axis_angle_matrix((0, 0, 1), 30)          # вокруг Z принтера (вертикаль)
        m_scene = coords.printer_matrix_to_scene(m_printer)
        expected = coords.axis_angle_matrix((0, 1, 0), 30)          # вокруг Y сцены (вертикаль)
        for i in range(3):
            self.assertTrue(close(m_scene[i], expected[i]))
        # Один и тот же физический поворот одной и той же точки в обеих системах.
        point = (3.0, 4.0, 5.0)
        lhs = coords.scene_to_printer(coords.apply_matrix(m_scene, coords.printer_to_scene(point)))
        self.assertTrue(close(lhs, coords.apply_matrix(m_printer, point)))

    def test_steps_scene_vs_printer(self):
        scene = coords.rotation_from_steps([("y", 45), ("x", 90)], "scene")
        printer = coords.rotation_from_steps([("z", 45), ("x", 90)], "printer")
        for i in range(3):
            self.assertTrue(close(scene[i], printer[i]))
        with self.assertRaises(ValueError):
            coords.rotation_from_steps([("w", 10)])

    def test_quaternion_roundtrip(self):
        for axis, angle in (((1, 0, 0), 90), ((0, 1, 1), 200), ((1, 2, 3), 33), ((0, 0, 1), 180)):
            m = coords.axis_angle_matrix(axis, angle)
            q = coords.matrix_to_quaternion(m)
            back = coords.quaternion_to_matrix(q)
            for i in range(3):
                self.assertTrue(close(m[i], back[i], 1e-9))
            unit, deg = coords.rotation_axis_angle(m)
            self.assertAlmostEqual(deg, angle if angle <= 180 else 360 - angle, places=6)

    def test_rotation_between(self):
        for a, b in (((0, 0, 1), (0, 0, -1)), ((1, 0, 0), (0, 1, 0)), ((1, 1, 0), (1, 1, 0)), ((0, 1, 0), (0, -1, 0))):
            m = coords.rotation_between(a, b)
            n = math.sqrt(sum(c * c for c in b))
            self.assertTrue(close(coords.apply_matrix(m, coords._normalize(a)), [c / n for c in b], 1e-9))
            coords.check_rotation_matrix(m)

    def test_check_rotation_matrix(self):
        with self.assertRaises(ValueError):
            coords.check_rotation_matrix([[1, 0, 0], [0, 1, 0], [0, 0, -1]])   # mirror
        with self.assertRaises(ValueError):
            coords.check_rotation_matrix([[2, 0, 0], [0, 1, 0], [0, 0, 1]])


class MeshMathTest(unittest.TestCase):
    def _both_paths(self, func):
        func()
        saved = coords.numpy
        coords.numpy = None
        try:
            func()
        finally:
            coords.numpy = saved

    def test_volume_area_bbox(self):
        def check():
            v, f = box(10, 20, 30, origin=(-5, -10, 0))
            self.assertAlmostEqual(coords.mesh_volume(v, f), 6000.0, places=6)
            self.assertAlmostEqual(coords.bottom_contact_area(v, f, up_axis=2), 200.0, places=6)
            lo, hi = coords.bounding_box(v)
            self.assertEqual(tuple(lo), (-5.0, -10.0, 0.0))
            self.assertEqual(tuple(hi), (5.0, 10.0, 30.0))
        self._both_paths(check)

    def test_unindexed_triangles(self):
        def check():
            v, f = box(2, 2, 2)
            flat = [v[i] for tri in f for i in tri]
            self.assertAlmostEqual(coords.mesh_volume(flat, None), 8.0, places=6)
        self._both_paths(check)

    def test_largest_flat_face_prefers_supporting_plane(self):
        def check():
            v, f = box(40, 30, 5)      # пластина лежит на самой большой грани: -Z
            normal, area = coords.largest_flat_face(v, f)
            self.assertTrue(close(normal, (0, 0, -1)) or close(normal, (0, 0, 1)))
            self.assertAlmostEqual(area, 1200.0, places=3)
        self._both_paths(check)


class ValuesTest(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(parse_number("40%"), 40)
        self.assertEqual(parse_number("0,2"), 0.2)
        self.assertEqual(parse_number("0.2 мм"), 0.2)
        with self.assertRaises(ValueProblem):
            parse_number("много")
        with self.assertRaises(ValueProblem):
            parse_number(True)

    def test_coerce_types(self):
        self.assertEqual(coerce_value("да", {"type": "bool"})[0], True)
        self.assertEqual(coerce_value("off", {"type": "bool"})[0], False)
        value, warnings = coerce_value(3.4, {"type": "int"})
        self.assertEqual(value, 3)
        self.assertTrue(warnings)
        self.assertIsInstance(coerce_value("2", {"type": "float"})[0], float)
        options = [["classic", "3D"], ["cylindrical", "5D Spiral"], ["cylindrical_full", "5D"]]
        self.assertEqual(coerce_value("5D", {"type": "enum", "options": options})[0], "cylindrical_full")
        self.assertEqual(coerce_value("Classic", {"type": "enum", "options": options})[0], "classic")
        with self.assertRaises(ValueProblem):
            coerce_value("4D", {"type": "enum", "options": options})
        self.assertEqual(coerce_value("45, 135", {"type": "[int]"})[0], "[45, 135]")
        self.assertEqual(coerce_value(1, {"type": "extruder", "extruder_count": 2})[0], "1")
        with self.assertRaises(ValueProblem):
            coerce_value(2, {"type": "extruder", "extruder_count": 2})

    def test_limits(self):
        info = {"type": "float", "unit": "°C", "minimum_value": -273.15, "maximum_value": 365,
                "maximum_value_warning": 350, "minimum_value_warning": 150}
        with self.assertRaises(ValueProblem) as ctx:
            coerce_value(500, info)
        self.assertIn("365", str(ctx.exception))
        value, warnings = coerce_value(355, info)
        self.assertEqual(value, 355.0)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(coerce_value(240, info)[1], [])

    def test_polymer(self):
        self.assertEqual(polymer_of("Enduse PETG", "Enduse", "Stereotech"), "PETG")
        self.assertEqual(polymer_of("Fiberpart ABS G4", "Fiberpart", "Stereotech"), "ABS")
        self.assertEqual(polymer_of("PLA", "PLA", "Generic"), "PLA")


class HelpersTest(unittest.TestCase):
    MATERIALS = [
        {"id": "enduse_petg", "name": "Enduse PETG", "type": "PETG"},
        {"id": "generic_petg_175", "name": "PETG", "type": "PETG"},
        {"id": "enduse_pa6", "name": "Enduse PA6", "type": "PA6"},
    ]

    def test_pick_material(self):
        self.assertEqual(pick_material(self.MATERIALS, "enduse_petg"), "enduse_petg")
        self.assertEqual(pick_material(self.MATERIALS, "Enduse PETG"), "enduse_petg")
        self.assertEqual(pick_material(self.MATERIALS, "petg"), "generic_petg_175")   # точное имя generic-материала
        self.assertEqual(pick_material(self.MATERIALS, "pa6"), "enduse_pa6")
        with self.assertRaises(SlicerError):
            pick_material(self.MATERIALS, "PEEK")

    def test_format_duration(self):
        self.assertEqual(format_duration(3725), "1 ч 02 мин")
        self.assertEqual(format_duration(125), "2 мин")
        self.assertEqual(format_duration(5), "5 с")

    def test_resolve_object(self):
        objects = [{"id": "1", "name": "gear.stl"}, {"id": "2", "name": "wheel.stl"},
                   {"id": "3", "name": "wheel.stl"}]
        self.assertEqual(resolve_object(objects, 2)["id"], "2")
        self.assertEqual(resolve_object(objects, "GEAR.STL")["id"], "1")
        with self.assertRaises(SlicerError):
            resolve_object(objects, "wheel.stl")       # ambiguous
        with self.assertRaises(SlicerError):
            resolve_object(objects, "nothing")
        with self.assertRaises(SlicerError):
            resolve_object(objects, None)
        self.assertEqual(resolve_object(objects[:1], None)["id"], "1")
        with self.assertRaises(SlicerError):
            resolve_object([], "1")

    def test_object_ids(self):
        class Node(object):
            pass
        ids = ObjectIds()
        a, b = Node(), Node()
        self.assertEqual(ids.short_id(a), "1")
        self.assertEqual(ids.short_id(b), "2")
        self.assertEqual(ids.short_id(a), "1")
        self.assertIs(ids.node("2"), b)
        del b
        ids.forget_dead()
        self.assertIsNone(ids.node("2"))


if __name__ == "__main__":
    unittest.main()
