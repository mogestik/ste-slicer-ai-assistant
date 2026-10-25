"""Обработчики инструментов от начала до конца на моке слайсера с имитацией главного потока Qt.

Реестр выполняет инструменты с main_thread=True через run_in_main_thread, как в STE Slicer,
а мок проверяет правила потоков при каждом обращении к адаптеру.

Запуск из корня репозитория: python -m unittest tests.test_tools_mock
"""

import json
import os
import queue
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import zipfile

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_DIR, "plugin", "AIAssistant")
DEVTOOLS_DIR = os.path.join(REPO_DIR, "devtools")
for path in (PLUGIN_DIR, DEVTOOLS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent import main_thread  # noqa: E402
from agent.slicer import set_adapter  # noqa: E402
from agent.tools import build_registry  # noqa: E402
from agent.tools import settings_tools, slicing_tools  # noqa: E402
from agent.tools.registry import ToolContext  # noqa: E402
import mock_slicer  # noqa: E402


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


class FakeQtApp(object):
    def __init__(self):
        self.thread = threading.current_thread()
        self.queue = queue.Queue()

    def getMainThread(self):
        return self.thread

    def callLater(self, func, *args, **kwargs):
        self.queue.put((func, args, kwargs))


class MockStandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiassistant_test_")
        self.app = FakeQtApp()
        main_thread.set_app_provider(lambda: self.app)
        self.registry = build_registry()
        self.mock = mock_slicer.install(self.registry, slice_step=0.01)
        self.events = []
        self.ctx = ToolContext(session_id="test", emit=lambda kind, **data: self.events.append((kind, data)),
                               services={"results_dir": os.path.join(self.tmp, "results")})
        self.box = os.path.join(self.tmp, "box.stl")
        write_box_stl(self.box, 40, 20, 10)
        self.tall = os.path.join(self.tmp, "tall.stl")
        write_box_stl(self.tall, 10, 10, 260)

    def tearDown(self):
        main_thread.set_app_provider(None)
        set_adapter(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Вспомогательное
    def call(self, name, args=None, expect_ok=True):
        """Выполняет инструмент в рабочем потоке, пока этот («главный») поток крутит callLater."""
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
        if expect_ok is not None:
            self.assertEqual(result.ok, expect_ok, "%s -> %s" % (name, result.text))
        return json.loads(result.text) if result.ok else result.text

    # Поддержки другим материалом
    def test_support_material_on_second_extruder(self):
        self.call("set_print_mode", {"mode": "classic"})
        self.call("set_material", {"material_id": "enduse_petg"})
        done = self.call("set_support_material", {"material_id": "proto_pva"})
        self.assertEqual(done["support_extruder"], 1)
        self.assertEqual(done["support_material"]["material_id"], "proto_pva")
        self.assertIn("support_extruder_nr", done["settings"])
        self.assertTrue(any("PVA" in note for note in done["notes"]))
        off = self.call("set_support_material", {"disable": True})
        self.assertEqual(off["support_extruder"], 0)

    def test_support_material_refused_in_5d_and_for_sealant(self):
        self.call("set_print_mode", {"mode": "cylindrical_full"})
        self.call("set_support_material", {"material_id": "proto_pva"}, expect_ok=False)
        self.call("set_print_mode", {"mode": "classic"})
        self.call("set_support_material", {"material_id": "sealant_tpu"}, expect_ok=False)

    # Партии
    def test_duplicate_object_places_copies_and_reports_overflow(self):
        self.call("set_print_mode", {"mode": "classic"})
        self.call("load_model", {"path": self.box})
        done = self.call("duplicate_object", {"copies": 3})
        self.assertEqual(done["total"], 4)
        self.assertEqual(done["inside_build_area"], 4)
        many = self.call("duplicate_object", {"object_id": "1", "copies": 50})
        self.assertEqual(many["total"], 54)
        self.assertLess(many["inside_build_area"], 54)
        self.assertTrue(many["outside_build_area_ids"])

    def test_duplicate_object_refused_in_5d(self):
        self.call("set_print_mode", {"mode": "cylindrical"})
        self.call("load_model", {"path": self.box})
        self.call("duplicate_object", {"copies": 2}, expect_ok=False)

    # Настройки
    def test_initial_config_is_5d_and_mode_switch_reports_changes(self):
        config = self.call("get_current_config")
        self.assertEqual(config["print_mode"], "cylindrical_full")
        self.assertEqual(config["definition_id"], "hte530v5.2.4")
        report = self.call("set_print_mode", {"mode": "classic"})
        self.assertEqual(report["backend_changed"], "CylindricalBackend -> CuraEngineBackend")
        self.assertEqual(report["build_volume_mm"]["height_z"], 300.0)
        self.assertIn("adhesion_type", report["changed_settings"])
        self.assertEqual(self.mock.user["global"]["printing_mode"], "classic")
        # полный сброс оставляет явно заданный режим печати
        self.call("set_settings", {"changes": {"layer_height": 0.2}})
        reset = self.call("reset_settings")
        self.assertEqual(reset["kept"], {"printing_mode": "classic"})
        self.assertEqual(self.call("get_current_config")["print_mode"], "classic")

    def test_set_settings_validation(self):
        result = self.call("set_settings", {"changes": {
            "layer_height": "0,2", "fill_density": "40%", "support_enable": "да",
            "material_print_temperature": 500, "no_such_setting_xyz": 1}})
        applied = result["applied"]
        self.assertEqual(applied["layer_height"]["value"], 0.2)
        self.assertEqual(applied["infill_sparse_density"]["value"], 40.0)
        self.assertEqual(applied["infill_sparse_density"]["stack"], "extruder_0")
        self.assertIs(applied["support_enable"]["value"], True)
        self.assertIn("material_print_temperature", result["errors"])
        self.assertIn("no_such_setting_xyz", result["errors"])
        error = self.call("set_settings", {"changes": {"layer_height": -1}}, expect_ok=False)
        self.assertIn("Ни одна настройка не применена", error)
        settings = self.call("get_settings", {"keys": ["infill_sparse_density", "layer_height"]})["settings"]
        self.assertEqual(settings["layer_height"]["source"], "пользовательское изменение")

    def test_profiles_and_materials(self):
        materials = self.call("list_materials", {"query": "PETG"})
        self.assertEqual({m["id"] for m in materials["materials"]}, {"enduse_petg", "generic_petg_175"})
        self.assertEqual(self.call("list_materials")["count"], 34)
        result = self.call("set_material", {"material_id": "Enduse PETG"})
        self.assertEqual(result["material_id"], "enduse_petg")
        self.assertEqual(result["material_print_temperature"], 235.0)
        self.call("set_material", {"material_id": "PEEK"}, expect_ok=False)
        self.call("select_profile", {"name": "fast"})
        self.assertEqual(self.call("get_settings", {"keys": ["layer_height"]})["settings"]["layer_height"]["value"],
                         0.15)
        self.call("save_profile", expect_ok=False)          # встроенный профиль перезаписать нельзя
        self.call("set_settings", {"changes": {"wall_line_count": 4}})
        created = self.call("create_profile", {"name": "Шестерня PETG"})
        self.assertEqual(created["settings_moved_to_profile"], 1)
        profiles = self.call("list_profiles")
        self.assertTrue(profiles["custom_profiles"][0]["active"])
        self.call("set_settings", {"changes": {"wall_line_count": 5}})
        self.assertEqual(self.call("save_profile")["saved_changes"], 1)

    # Сцена
    def test_load_rotate_center(self):
        loaded = self.call("load_model", {"path": self.box})["loaded"][0]
        self.assertEqual(loaded["size_mm"], [40.0, 20.0, 10.0])
        self.assertTrue(loaded["centered_on_5d_axis"])      # единственный объект в 5D центрируется сам
        self.assertAlmostEqual(loaded["volume_cm3"], 8.0, places=3)
        # Y сцены вертикален: 90 градусов вокруг него меняют местами горизонтальные размеры
        rotated = self.call("rotate_object", {"object_id": "1", "axis": "y", "angle_deg": 90})
        self.assertEqual(rotated["size_mm"], [20.0, 40.0, 10.0])
        # X принтера: 90 градусов ставят брусок стоймя (Z становится 20)
        rotated = self.call("rotate_object", {"object_id": "1", "axis": "x", "angle_deg": 90, "frame": "printer",
                                              "reset_rotation": True})
        self.assertEqual(rotated["size_mm"], [40.0, 10.0, 20.0])
        self.assertEqual(rotated["bottom_z_mm"], 0.0)
        flat = self.call("rotate_object", {"object_id": "1", "lay_flat": True})
        self.assertEqual(flat["size_mm"][2], 10.0)
        steps = self.call("rotate_object", {"object_id": 1, "steps": [{"axis": "x", "angle_deg": -90}],
                                            "reset_rotation": True})
        self.assertEqual(steps["size_mm"], [40.0, 10.0, 20.0])
        moved = self.call("move_object", {"object_id": "box.stl", "x": 30, "y": -10})
        self.assertEqual(moved["center_mm"][:2], [30.0, -10.0])
        self.assertTrue(any("5D" in w for w in moved["warnings"]))
        scene = self.call("move_object", {"object_id": "1", "x": 5, "z": 7, "frame": "scene"})
        self.assertEqual(scene["center_mm"][:2], [5.0, -7.0])
        centered = self.call("center_for_5d")
        self.assertTrue(centered["centered_on_5d_axis"])
        scaled = self.call("scale_object", {"object_id": "1", "factor": 2})
        self.assertEqual(scaled["size_mm"], [80.0, 20.0, 40.0])
        self.assertEqual(scaled["bottom_z_mm"], 0.0)
        self.assertTrue(scaled["centered_on_5d_axis"])
        # масштаб по осям — по собственным осям модели (файла): сбрасываем поворот, и тогда z — это высота
        self.call("rotate_object", {"object_id": "1", "reset_rotation": True})
        scaled = self.call("scale_object", {"object_id": "1", "z": 3})
        self.assertEqual(scaled["scale"], [2.0, 2.0, 3.0])
        self.assertEqual(scaled["size_mm"], [80.0, 40.0, 30.0])

    def test_objects_outside_and_delete(self):
        self.call("load_model", {"path": self.tall})
        objects = self.call("list_objects")["objects"]
        self.assertTrue(objects[0]["outside_build_area"])    # 260 mm > 231.1 mm in 5D
        error = self.call("start_slicing", expect_ok=False)
        self.assertIn("вне рабочей области", error)
        report = self.call("set_print_mode", {"mode": "classic"})
        self.assertEqual(report["build_volume_mm"]["height_z"], 300.0)
        self.assertFalse(self.call("list_objects")["objects"][0]["outside_build_area"])
        self.call("load_model", {"path": self.box})
        self.assertEqual(self.call("list_objects")["count"], 2)
        self.assertEqual(self.call("delete_objects", {"object_ids": ["1"]})["remaining"], 1)
        self.assertEqual(self.call("delete_objects", {"all": True})["remaining"], 0)
        self.assertIn("нет моделей", self.call("start_slicing", expect_ok=False))
        self.call("load_model", {"path": os.path.join(self.tmp, "missing.stl")}, expect_ok=False)

    # Нарезка
    def test_slice_export_save(self):
        self.call("set_print_mode", {"mode": "classic"})
        self.call("load_model", {"path": self.box})
        self.call("export_gcode", expect_ok=False)            # ещё ничего не нарезано
        result = self.call("start_slicing")
        self.assertGreater(result["print_time_s"], 0)
        self.assertEqual(result["material"][0]["extruder"], 0)
        kinds = [kind for kind, _data in self.events]
        self.assertIn("slicing_progress", kinds)
        card = [data for kind, data in self.events if kind == "slicing_result"][0]
        self.assertEqual(card["mode"], "classic")
        self.assertEqual(self.call("get_slicing_status")["state"], "done")
        gcode = self.call("export_gcode", {"path": os.path.join(self.tmp, "out", "result")})
        self.assertTrue(gcode["path"].endswith("result.gcode"))
        self.assertEqual(gcode["printing_mode"], "classic")
        self.assertTrue(gcode["looks_valid"])
        project = self.call("save_project")
        self.assertTrue(project["looks_valid"])
        with zipfile.ZipFile(project["path"]) as archive:
            names = archive.namelist()
        self.assertIn("3D/3dmodel.model", names)
        # любая правка сбрасывает G-code, как в настоящем слайсере
        self.call("set_settings", {"changes": {"infill_sparse_density": 30}})
        self.assertEqual(self.call("get_slicing_status")["state"], "not_started")
        self.call("export_gcode", expect_ok=False)

    def test_project_readable_by_ste3mf(self):
        try:
            import ste3mf
        except Exception as exc:  # pragma: no cover - нужен Python 3.8+
            self.skipTest("devtools/ste3mf.py недоступен: %s" % exc)
        self.call("set_print_mode", {"mode": "cylindrical"})
        self.call("load_model", {"path": self.box})
        self.call("rotate_object", {"object_id": "1", "axis": "x", "angle_deg": 90, "frame": "printer"})
        self.call("set_material", {"material_id": "enduse_pa6"})
        self.call("set_settings", {"changes": {"infill_sparse_density": 60}})
        path = self.call("save_project", {"path": os.path.join(self.tmp, "p.3mf")})["path"]
        project = ste3mf.Project(path)
        self.assertEqual(project.machine_definition(), "hte530v5.2.4")
        self.assertEqual(len(project.objects), 1)

    def test_5d_slice_and_atss_stall(self):
        self.call("load_model", {"path": self.box})
        result = self.call("start_slicing", {"timeout_s": 30})
        self.assertEqual(result["mode"], "cylindrical_full")
        self.assertTrue(any("ATSS" in w for w in result["warnings"]))
        self.mock.atss_license = False
        saved = slicing_tools.STALL_LIMIT_S, slicing_tools.HINT_AFTER_S
        slicing_tools.STALL_LIMIT_S, slicing_tools.HINT_AFTER_S = 1.0, 0.5
        try:
            error = self.call("start_slicing", {"timeout_s": 30}, expect_ok=False)
        finally:
            slicing_tools.STALL_LIMIT_S, slicing_tools.HINT_AFTER_S = saved
        self.assertIn("ATSS", error)
        self.assertEqual(self.call("get_slicing_status")["state"], "not_started")   # остановлено после зависания

    def test_slicing_without_wait(self):
        self.call("set_print_mode", {"mode": "classic"})
        self.call("load_model", {"path": self.box})
        started = self.call("start_slicing", {"wait": False})
        self.assertTrue(started["started"])
        for _ in range(200):
            if self.mock.monitor_snapshot()["state"] == "done":
                break
            threading.Event().wait(0.02)
        self.assertEqual(self.call("get_slicing_result")["mode"], "classic")


class PureHelpersTest(unittest.TestCase):
    def test_describe_mode_change(self):
        before = {"print_mode": "cylindrical_full", "backend": "CylindricalBackend",
                  "build_volume_mm": {"height_z": 231.1}, "settings": {
                      "adhesion_type": {"value": "raft", "enabled": True},
                      "cylindrical_mode_base_diameter": {"value": 6, "enabled": True}}}
        after = {"print_mode": "classic", "backend": "CuraEngineBackend", "build_volume_mm": {"height_z": 300},
                 "settings": {"adhesion_type": {"value": "brim", "enabled": True},
                              "cylindrical_mode_base_diameter": {"value": 6, "enabled": False}},
                 "objects_outside": []}
        result = settings_tools.describe_mode_change(before, after)
        self.assertEqual(result["backend_changed"], "CylindricalBackend -> CuraEngineBackend")
        self.assertEqual(result["changed_settings"], {"adhesion_type": "raft -> brim"})
        self.assertEqual(result["now_inactive_settings"], ["cylindrical_mode_base_diameter"])

    def test_resolve_output_path(self):
        tmp = tempfile.mkdtemp()
        try:
            ctx = ToolContext(services={"results_dir": tmp})
            path = slicing_tools.resolve_output_path(None, ".gcode", ctx, None, now=0)
            self.assertTrue(path.startswith(tmp) and path.endswith(".gcode"))
            self.assertEqual(slicing_tools.resolve_output_path("a/b", ".3mf", ctx),
                             os.path.join(tmp, "a", "b.3mf"))
            directory = os.path.join(tmp, "dir")
            os.makedirs(directory)
            self.assertTrue(slicing_tools.resolve_output_path(directory, ".3mf", ctx).startswith(directory + os.sep))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
