"""Тесты agent/verify (самопроверка сохранённых 3MF / G-code без слайсера) и инструмента verify_project.

Результаты сверяются с devtools/inspect_3mf.py (нужен Python 3.8+, иначе пропускаются).
Запуск из корня репозитория: python -m unittest tests.test_verify
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
PLUGIN_DIR = os.path.join(REPO_DIR, "plugin", "AIAssistant")
DEVTOOLS_DIR = os.path.join(REPO_DIR, "devtools")
for _path in (PLUGIN_DIR, DEVTOOLS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from agent import main_thread  # noqa: E402
from agent.slicer import set_adapter  # noqa: E402
from agent.tools import build_registry, verify_tools  # noqa: E402
from agent.tools.registry import ToolContext, ToolRegistry  # noqa: E402
from agent.verify import verify, verify_gcode  # noqa: E402
from agent.verify import gcode as vgcode  # noqa: E402

try:
    import inspect_3mf  # noqa: E402
except Exception:  # pragma: no cover - devtools требуют Python 3.8+
    inspect_3mf = None

FIX = os.path.join(HERE, "fixtures")
GEAR = os.path.join(FIX, "gear_3d.3mf")
WHEEL = os.path.join(FIX, "wheel_5d.3mf")
GEAR_G = os.path.join(FIX, "gear_3d.gcode")
WHEEL_G = os.path.join(FIX, "wheel_5d.gcode")


def catalog():
    return verify_tools._catalog_settings()


# В проектах мока нет ни *.def.json, ни XML материала: verify берёт справочник плагина, где умолчания,
# зависящие от материала, взяты от материала по умолчанию (generic PLA), а inspect_3mf берёт fdmprinter.
MATERIAL_DEFAULT_KEYS = ("material_print_temperature", "material_bed_temperature")


def assert_like_inspect(test, path, report, skip=()):
    """Ключевые поля отчёта verify == сводке devtools/inspect_3mf.py по тому же файлу."""
    if inspect_3mf is None:
        test.skipTest("devtools/inspect_3mf.py недоступен")
    keys = list(report["settings"])
    s = inspect_3mf.load(path).summary(keys=keys)
    test.assertEqual(report["printing_mode"], s["printing_mode"]["value"])
    test.assertEqual(report["quality"]["type"] or None, s["quality"]["quality_type"] or report["quality"]["type"])
    mat = s["extruders"]["0"]["material"]
    test.assertEqual(report["material"]["id"], mat["base_file"] or mat["id"])
    for key, value in report["settings"].items():
        if key in skip:
            continue
        row = s["effective"][key]
        ref = row.get("extruder0") or row["global"]
        if not ref.get("ok"):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            test.assertAlmostEqual(float(value), float(ref["value"]), places=4, msg=key)
        else:
            test.assertEqual(value, ref["value"], key)
    test.assertEqual(len(report["objects"]), len(s["objects"]))
    for mine, ref in zip(report["objects"], s["objects"]):
        for a, b in zip(mine["size_mm"], ref["world_bbox_3mf"]["size"]):
            test.assertAlmostEqual(a, b, places=1)
        for a, b in zip(mine["center_mm"], ref["position"]["center_rel_plate_center_xy"]):
            test.assertAlmostEqual(a, b, places=1)
        test.assertAlmostEqual(mine["bottom_z"], ref["position"]["bottom_z"], places=2)
        test.assertEqual(mine["up_axis_of_source"].split()[0], ref["source_axis_pointing_up"]["nearest"])
        for a, b in zip(mine["rotation_deg"], ref["rotation_vs_source_stl"]["euler_xyz_deg"]):
            test.assertAlmostEqual(abs(a), abs(b), places=0)


def write_box_stl(path, sx, sy, sz):
    """Бинарный STL бруска вдоль осей (система файла, Z вверх)."""
    v = [(0, 0, 0), (sx, 0, 0), (sx, sy, 0), (0, sy, 0), (0, 0, sz), (sx, 0, sz), (sx, sy, sz), (0, sy, sz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as handle:
        handle.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for a, b, c in faces:
            handle.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                handle.write(struct.pack("<3f", *v[i]))
            handle.write(b"\0\0")


def rewrite(src, dst, edit):
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for info in zi.infolist():
            data = edit(info.filename, zi.read(info.filename))
            if data is not None:
                zo.writestr(info.filename, data)


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiassistant_verify_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gear_matches_inspect(self):
        report = verify(GEAR)
        self.assertEqual(report["printing_mode"], "classic")
        self.assertEqual(report["material"]["id"], "fiberpart_pc_g20")
        self.assertEqual(report["quality"]["type"], "fast")
        self.assertEqual(report["settings"]["wall_line_count"], 4)
        self.assertEqual(report["settings"]["infill_sparse_density"], 100)
        self.assertEqual(report["settings"]["material_bed_temperature"], 115)   # глобальная формула 'resolve'
        self.assertEqual(report["objects"][0]["up_axis_of_source"], "+Y")
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["warnings"], [])
        assert_like_inspect(self, GEAR, report)

    def test_wheel_matches_inspect(self):
        report = verify(WHEEL)
        self.assertEqual(report["mode_title"], "5D Spiral")
        self.assertEqual(report["settings"]["cylindrical_mode_base_diameter"], 16)
        self.assertEqual(report["objects"][0]["center_mm"], [0.0, 0.0])
        self.assertEqual(report["objects"][0]["max_radius_mm"], 29.5)
        self.assertEqual(report["objects"][0]["per_object"], {"infill_sparse_density": "60"})
        self.assertTrue(report["ok"], report)
        assert_like_inspect(self, WHEEL, report)

    def test_expected(self):
        report = verify(GEAR, {"printing_mode": "classic", "material_id": "Fiberpart PC G20", "quality_type": "fast",
                               "settings": {"infill_sparse_density": "100", "support_enable": "false",
                                            "wall_line_count": 3, "no_such_key": 1},
                               "layer_height": 0.15})
        exp = report["expected"]
        self.assertEqual(set(exp["match"]), {"printing_mode", "material_id", "quality_type", "infill_sparse_density",
                                             "support_enable", "layer_height"})
        bad = dict((row["key"], row) for row in exp["mismatch"])
        self.assertEqual(set(bad), {"wall_line_count", "no_such_key"})
        self.assertEqual(bad["wall_line_count"]["actual"], 4)
        self.assertFalse(report["ok"])
        report = verify(WHEEL, {"printing_mode": "cylindrical_full", "settings": {"infill_sparse_density": 60}})
        self.assertEqual([row["key"] for row in report["expected"]["mismatch"]], ["printing_mode"])

    def test_per_object_override_is_reported(self):
        report = verify(WHEEL, {"settings": {"infill_sparse_density": 60}})
        self.assertEqual(report["expected"]["match"], ["infill_sparse_density"])
        path = os.path.join(self.tmp, "w.3mf")
        rewrite(WHEEL, path, lambda n, d: d.replace(b">60</metadata>", b">20</metadata>")
                if n == "3D/3dmodel.model" else d)
        row = verify(path, {"settings": {"infill_sparse_density": 60}})["expected"]["mismatch"][0]
        self.assertIn("переопределено", row["note"])

    def test_gcode_consistent(self):
        report = verify(GEAR, gcode_path=GEAR_G)
        g = report["gcode"]
        self.assertTrue(g["matches_3mf"], report)
        self.assertEqual((g["mode"], g["time_s"], g["filament_m"], g["hotend_c"], g["bed_c"]),
                         ("classic", 1534, 0.93, [290], [115]))
        self.assertTrue(report["ok"], report)
        report = verify(WHEEL, gcode_path=WHEEL_G)
        self.assertTrue(report["gcode"]["matches_3mf"], report)
        self.assertTrue(report["ok"], report)

    def test_gcode_from_other_project(self):
        report = verify(WHEEL, gcode_path=GEAR_G)
        self.assertFalse(report["gcode"]["matches_3mf"])
        text = " ".join(report["errors"])
        for part in ("режим classic", "температура сопла", "габариты сцены", "настройки:"):
            self.assertIn(part, text)

    def test_stale_gcode_settings(self):
        # пользователь поменял заполнение после выгрузки: блок ;SETTING_3 больше не совпадает с 3MF
        path = os.path.join(self.tmp, "g.3mf")
        rewrite(GEAR, path, lambda n, d: d.replace(b"infill_sparse_density = 100", b"infill_sparse_density = 40")
                if n.endswith("extruder_main #2_user.inst.cfg") else d)
        report = verify(path, gcode_path=GEAR_G)
        self.assertFalse(report["gcode"]["matches_3mf"])
        self.assertIn("E0 infill_sparse_density: G-code 100, 3MF 40", " ".join(report["errors"]))

    def test_check_gcode_parity(self):
        if inspect_3mf is None:
            self.skipTest("devtools недоступны")
        import check_gcode
        with open(GEAR_G, encoding="utf-8") as handle:
            ref = check_gcode.parse(handle.read())
        mine = vgcode.parse_file(GEAR_G)
        self.assertEqual(mine.layers, ref.layers)
        self.assertEqual(mine.extrusions, ref.extrusions)
        self.assertEqual(mine.ranges, ref.ranges)
        self.assertEqual(sorted(mine.coord_systems), sorted(set(ref.coord_systems)))
        self.assertEqual(vgcode.settings_block(mine), {k: v for k, v in check_gcode.parse_settings_block(ref).items()
                                                       if k in ("global", "extruders")})

    def test_verify_gcode_alone(self):
        report = verify_gcode(WHEEL_G)
        self.assertEqual(report["mode"], "cylindrical")
        self.assertNotIn("matches_3mf", report)
        self.assertTrue(report["ok"], report)

    def test_placement_problems(self):
        # поднимаем колесо на 5 мм и сдвигаем с оси вращения на 3 мм по X
        def edit(name, data):
            if name != "3D/3dmodel.model":
                return data
            text = data.decode("utf-8")
            start = text.index('transform="', text.index("<item")) + len('transform="')
            end = text.index('"', start)
            values = text[start:end].split()
            values[9] = "%.6f" % (float(values[9]) + 3.0)
            values[11] = "%.6f" % (float(values[11]) + 5.0)
            return (text[:start] + " ".join(values) + text[end:]).encode("utf-8")
        path = os.path.join(self.tmp, "off.3mf")
        rewrite(WHEEL, path, edit)
        report = verify(path)
        warnings = " ".join(report["warnings"])
        self.assertIn("не на столе", warnings)
        self.assertIn("смещён от оси вращения на 3.00 мм", warnings)
        self.assertEqual(report["objects"][0]["center_mm"], [3.0, 0.0])
        assert_like_inspect(self, path, report)

    def test_not_a_project(self):
        path = os.path.join(self.tmp, "mesh.3mf")
        rewrite(GEAR, path, lambda n, d: None if n.startswith("SteSlicer/") else d)
        report = verify(path)
        self.assertFalse(report["ok"])
        self.assertIn("не проект", report["errors"][0])

    def test_missing_definitions_fallback(self):
        path = os.path.join(self.tmp, "nodef.3mf")
        rewrite(GEAR, path, lambda n, d: None if n.endswith(".def.json") else d)
        report = verify(path, catalog_settings=catalog())
        self.assertIn("каталогу плагина", " ".join(report["warnings"]))
        self.assertEqual(report["settings"]["wall_line_count"], 4)          # явные значения точные
        self.assertEqual(report["settings"]["material_print_temperature"], 290)
        self.assertEqual(report["printing_mode"], "classic")
        report = verify(path)                                               # без справочника — только сырые значения
        self.assertEqual(report["settings"]["wall_line_count"], 4)
        self.assertIn("не вычислены", " ".join(report["warnings"]))


class FakeQtApp(object):
    def __init__(self):
        self.thread = threading.current_thread()
        self.queue = __import__("queue").Queue()

    def getMainThread(self):
        return self.thread

    def callLater(self, func, *args, **kwargs):
        self.queue.put((func, args, kwargs))


class MockToolTest(unittest.TestCase):
    """verify_project на проектах, записанных моком слайсера (save_project / export_gcode)."""

    def setUp(self):
        try:
            import mock_slicer
        except Exception as exc:  # pragma: no cover
            self.skipTest("мок слайсера недоступен: %s" % exc)
        self.tmp = tempfile.mkdtemp(prefix="aiassistant_verify_mock_")
        self.app = FakeQtApp()
        main_thread.set_app_provider(lambda: self.app)
        self.registry = build_registry()
        if not self.registry.has("verify_project"):
            verify_tools.register(self.registry)
        self.mock = mock_slicer.install(self.registry, slice_step=0.01)
        self.results = os.path.join(self.tmp, "results")
        self.ctx = ToolContext(session_id="test", emit=lambda kind, **data: None,
                               services={"results_dir": self.results})
        self.box = os.path.join(self.tmp, "box.stl")
        write_box_stl(self.box, 40, 20, 10)

    def tearDown(self):
        main_thread.set_app_provider(None)
        set_adapter(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, name, args=None, expect_ok=True):
        box = {}

        def worker():
            box["result"] = self.registry.call(name, args or {}, self.ctx)
            self.app.queue.put(None)

        thread = threading.Thread(target=worker)
        thread.start()
        while True:
            item = self.app.queue.get(timeout=30)
            if item is None:
                break
            func, a, k = item
            func(*a, **k)
        thread.join()
        result = box["result"]
        self.assertEqual(result.ok, expect_ok, "%s -> %s" % (name, result.text))
        return json.loads(result.text) if result.ok else result.text

    def test_no_saved_project(self):
        self.assertIn("save_project", self.call("verify_project", expect_ok=False))

    def test_classic_project_and_gcode(self):
        self.call("set_print_mode", {"mode": "classic"})
        self.call("load_model", {"path": self.box})
        self.call("set_material", {"material_id": "enduse_pa6"})
        self.call("set_settings", {"changes": {"infill_sparse_density": 35, "wall_line_count": 3}})
        self.call("start_slicing")
        gcode = self.call("export_gcode")["path"]
        project = self.call("save_project")["path"]
        report = self.call("verify_project", {"expected": {
            "printing_mode": "classic", "material_id": "enduse_pa6",
            "settings": {"infill_sparse_density": 35, "wall_line_count": 3}}})
        self.assertEqual(report["file"], project)
        self.assertEqual(report["gcode"]["file"], gcode)                # подобран автоматически
        self.assertTrue(report["gcode"]["matches_3mf"], report)
        self.assertEqual(report["expected"]["mismatch"], [])
        self.assertEqual(report["objects"][0]["size_mm"], [40.0, 20.0, 10.0])
        self.assertEqual(report["objects"][0]["bottom_z"], 0.0)
        self.assertIn("каталогу плагина", " ".join(report["warnings"]))  # мок не пишет *.def.json
        self.assertEqual(report["errors"], [])
        assert_like_inspect(self, project, report, MATERIAL_DEFAULT_KEYS)
        # правка после выгрузки: старый G-code не совпадает с пересохранённым проектом
        self.call("set_settings", {"changes": {"layer_height": 0.3}})
        self.call("save_project")
        report = self.call("verify_project", {"gcode_path": gcode})
        self.assertFalse(report["gcode"]["matches_3mf"])
        self.assertIn("высота слоя", " ".join(report["errors"]))

    def test_5d_off_axis(self):
        self.call("set_print_mode", {"mode": "cylindrical"})
        self.call("load_model", {"path": self.box})
        self.call("rotate_object", {"object_id": "1", "axis": "x", "angle_deg": 90, "frame": "printer"})
        path = self.call("save_project", {"path": os.path.join(self.tmp, "a.3mf")})["path"]
        report = self.call("verify_project", {"path": path, "expected": {"printing_mode": "cylindrical"}})
        self.assertEqual(report["mode_title"], "5D Spiral")
        self.assertEqual(report["objects"][0]["center_mm"], [0.0, 0.0])
        self.assertEqual(report["expected"]["match"], ["printing_mode"])
        assert_like_inspect(self, path, report, MATERIAL_DEFAULT_KEYS)
        self.call("move_object", {"object_id": "1", "x": 20, "y": 0})
        path = self.call("save_project", {"path": os.path.join(self.tmp, "b.3mf")})["path"]
        report = self.call("verify_project", {"path": path})
        self.assertIn("смещён от оси вращения", " ".join(report["warnings"]))
        assert_like_inspect(self, path, report, MATERIAL_DEFAULT_KEYS)


class RegistrationTest(unittest.TestCase):
    def test_tool_declaration(self):
        registry = ToolRegistry()
        verify_tools.register(registry)
        tool = registry.get("verify_project")
        self.assertEqual(tool.group, "verify")
        self.assertFalse(tool.main_thread)
        self.assertLessEqual(len(tool.description), 300)
        self.assertEqual(set(tool.input_schema["properties"]), {"path", "expected", "gcode_path"})

    def test_bad_file(self):
        registry = ToolRegistry()
        verify_tools.register(registry)
        tmp = tempfile.mkdtemp()
        try:
            bad = os.path.join(tmp, "bad.3mf")
            with open(bad, "w") as handle:
                handle.write("not a zip")
            result = registry.call("verify_project", {"path": bad}, ToolContext(services={"results_dir": tmp}))
            self.assertFalse(result.ok)
            self.assertIn("3MF", result.text)
            result = registry.call("verify_project", {"path": os.path.join(tmp, "none.3mf")}, ToolContext())
            self.assertIn("не найден", result.text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
