"""Сравнение 3D / 5D по прочности (geometry/strength.py + compare_print_strategies).

Синтетические тела: сплошной цилиндр, труба, пластина. Запуск из корня: python -m unittest tests.test_strength
"""

import json
import math
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "plugin", "AIAssistant")
for path in (PLUGIN_DIR, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent import geometry  # noqa: E402
from agent.geometry import strength  # noqa: E402
from agent.tools import build_registry, core_tool_names  # noqa: E402
from agent.tools.registry import ToolContext  # noqa: E402
from tests.test_geometry import box, flat, revolve, write_binary_stl  # noqa: E402


def compare(triangles, loads, klass="standard"):
    report = geometry.analyze_triangles(flat(triangles), "file")
    axis = report["axis"]
    axis_dir = axis["axis_direction_file"] if axis["is_revolution"] else None
    data = strength.compare(axis_dir, report["orientation_3d"], loads, klass, axis["is_revolution"])
    return report, axis_dir, data


def pose_along(data, axis, vertical):
    """Первая поза с вертикальной / горизонтальной осью."""
    for entry in data["poses_3d"]:
        along = abs(sum(a * b for a, b in zip(axis, entry["pose"]["up_file"])))
        if vertical and along > 0.95 or not vertical and along < 0.05:
            return entry
    raise AssertionError("no such pose")


class FormulaTest(unittest.TestCase):
    def test_layer_strength_limits(self):
        self.assertAlmostEqual(strength.layer_strength(0.0), 1.0)
        self.assertAlmostEqual(strength.layer_strength(1.0), 0.55)
        mid = strength.layer_strength(math.sqrt(0.5))
        self.assertTrue(0.55 < mid < 1.0)

    def test_material_class(self):
        self.assertEqual(strength.material_class(None), "standard")
        self.assertEqual(strength.material_class({"filler": "стекловолокно", "flexible": False}), "fiber")
        self.assertEqual(strength.material_class({"filler": None, "flexible": True}), "tpu")
        self.assertEqual(strength.material_class({"filler": None, "elastomer": True}), "tpu")

    def test_bad_loads(self):
        with self.assertRaises(strength.StrengthError):
            strength.normalize_loads([{"type": "bogus"}], True)
        with self.assertRaises(strength.StrengthError):
            strength.normalize_loads([{"type": "custom"}], True)
        with self.assertRaises(strength.StrengthError):
            strength.normalize_loads([{"type": "torsion"}], False)


class CylinderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shaft = revolve([(0, 0), (10, 0), (10, 120), (0, 120)], segments=96)
        cls.tube = revolve([(8, 0), (18, 0), (18, 40), (8, 40)], segments=96)

    def test_shaft_bending(self):
        _, axis, data = compare(self.shaft, [{"type": "shaft_bending"}])
        self.assertAlmostEqual(pose_along(data, axis, True)["index"], 0.55, places=2)
        self.assertAlmostEqual(data["shell"]["index"], 1.0, places=2)
        self.assertAlmostEqual(pose_along(data, axis, False)["index"], 1.0, places=2)

    def test_torsion_standing_weaker_than_5d(self):
        _, axis, data = compare(self.shaft, [{"type": "torsion"}])
        self.assertLess(pose_along(data, axis, True)["index"], data["shell"]["index"] - 0.1)

    def test_gear_tooth_flat_beats_5d(self):
        _, axis, data = compare(self.tube, [{"type": "gear_tooth"}])
        self.assertGreater(pose_along(data, axis, True)["index"], data["shell"]["index"])

    def test_centrifugal_flat_not_worse_than_5d(self):
        _, axis, data = compare(self.tube, [{"type": "centrifugal"}])
        self.assertGreaterEqual(pose_along(data, axis, True)["index"], data["shell"]["index"])

    def test_material_class_changes_index(self):
        _, axis, standard = compare(self.shaft, [{"type": "shaft_bending"}])
        _, _, fiber = compare(self.shaft, [{"type": "shaft_bending"}], "fiber")
        self.assertLess(pose_along(fiber, axis, True)["index"], pose_along(standard, axis, True)["index"])

    def test_weights_and_worst_load(self):
        _, axis, data = compare(self.shaft, [{"type": "shaft_bending", "weight": 3}, {"type": "torsion"}])
        standing = pose_along(data, axis, True)
        self.assertEqual(standing["worst_load"], "shaft_bending")
        expected = (3 * standing["per_load"]["shaft_bending"] + standing["per_load"]["torsion"]) / 4.0
        self.assertAlmostEqual(standing["index"], expected)


class PlateTest(unittest.TestCase):
    def test_no_5d_for_plate(self):
        _, axis, data = compare(box(60, 40, 5), [{"type": "custom", "direction": [1, 0, 0]},
                                                 {"type": "axial_tension", "direction": [0, 1, 0]}])
        self.assertIsNone(axis)
        self.assertNotIn("shell", data)
        flat_pose = max(data["poses_3d"], key=lambda entry: entry["index"])
        self.assertAlmostEqual(flat_pose["index"], 1.0, places=2)

    def test_axis_loads_skipped_without_axis(self):
        _, _, data = compare(box(60, 40, 5), [{"type": "torsion"}, {"type": "custom", "direction": [0, 0, 1]}])
        self.assertEqual(len(data["skipped"]), 1)


class RecommendTest(unittest.TestCase):
    def test_strength_wins_above_015(self):
        out = strength.recommend({"index": 0.55}, {"index": 0.9}, "classic", True)
        self.assertEqual((out["choice"], out["basis"]), ("cylindrical", "strength"))
        out = strength.recommend({"index": 1.0}, {"index": 0.8}, "cylindrical", True)
        self.assertEqual((out["choice"], out["basis"]), ("classic", "strength"))

    def test_geometry_decides_when_close(self):
        out = strength.recommend({"index": 0.9}, {"index": 0.85}, "cylindrical_full")
        self.assertEqual((out["choice"], out["basis"], out["compromise"]), ("cylindrical_full", "geometry", False))

    def test_compromise_band(self):
        out = strength.recommend({"index": 1.0}, {"index": 0.88}, "cylindrical_full")
        self.assertEqual(out["choice"], "cylindrical_full")
        self.assertTrue(out["compromise"])

    def test_no_5d(self):
        self.assertEqual(strength.recommend({"index": 0.9}, None, "classic")["choice"], "classic")


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiassistant_strength_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.registry = build_registry()
        self.ctx = ToolContext(session_id="test", emit=lambda *a, **k: None, services={})

    def call(self, args, ok=True):
        result = self.registry.call("compare_print_strategies", args, self.ctx)
        self.assertEqual(result.ok, ok, result.text)
        return json.loads(result.text) if result.ok else result.text

    def test_registered_as_core_tool(self):
        self.assertIn("compare_print_strategies", core_tool_names(self.registry))

    def test_shaft_report(self):
        path = os.path.join(self.tmp, "shaft.stl")
        write_binary_stl(path, revolve([(0, 0), (10, 0), (10, 120), (0, 120)], segments=96))
        data = self.call({"path": path, "loads": [{"type": "shaft_bending"}, {"type": "torsion"}],
                          "material_id": "enduse_pa6"})
        self.assertIn("[оценка]", data["why"])
        self.assertTrue(any(row.get("printing_mode") == "cylindrical_full" for row in data["variants"]))
        # лежащий цилиндр без свесов с высоким индексом: геометрия может оставить 3D
        self.assertIn(data["recommended"]["printing_mode"], ("classic", "cylindrical_full"))
        standing = [row for row in data["variants"] if "ось детали вертикально" in row["variant"]]
        self.assertTrue(standing and standing[0]["index"] < 0.8)

    def test_plate_has_no_5d_row(self):
        path = os.path.join(self.tmp, "plate.stl")
        write_binary_stl(path, box(60, 40, 5))
        data = self.call({"path": path, "loads": [{"type": "custom", "direction": [1, 0, 0]}]})
        self.assertEqual(data["recommended"]["printing_mode"], "classic")
        self.assertIn("неприменимо", data["variants"][-1]["note"])

    def test_errors(self):
        path = os.path.join(self.tmp, "plate2.stl")
        write_binary_stl(path, box(60, 40, 5))
        self.call({"path": path, "loads": [{"type": "torsion"}]}, ok=False)
        self.call({"path": path, "loads": [{"type": "custom", "direction": [1, 0, 0]}],
                   "material_id": "no_such"}, ok=False)


if __name__ == "__main__":
    unittest.main()
