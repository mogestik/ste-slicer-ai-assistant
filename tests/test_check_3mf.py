"""Тесты devtools/ste3mf.py, inspect_3mf.py, check_3mf.py и check_gcode.py.

Запуск из корня репозитория: python -m unittest
Фикстуры: tests/fixtures/*.3mf|*.gcode, пересобрать: python tests/fixtures/make_fixtures.py
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SOLUTION = os.path.dirname(HERE)
DEVTOOLS = os.path.join(SOLUTION, "devtools")
sys.path.insert(0, DEVTOOLS)

import ste3mf  # noqa: E402
import inspect_3mf  # noqa: E402
import check_3mf  # noqa: E402
import check_gcode  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
GEAR = os.path.join(FIX, "gear_3d.3mf")
WHEEL = os.path.join(FIX, "wheel_5d.3mf")
TARGETS = os.path.join(DEVTOOLS, "targets")
GLOBAL_USER = "SteSlicer/Stereotech Hybrid 530 V5.2.4_user.inst.cfg"


def rewrite(src, dst, edit):
    """Копирует 3MF, применяя edit(имя, байты) -> байты|None (None — выбросить файл)."""
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for info in zi.infolist():
            data = edit(info.filename, zi.read(info.filename))
            if data is not None:
                zo.writestr(info.filename, data)
    return dst


def load_target(name):
    with open(os.path.join(TARGETS, name), encoding="utf-8") as f:
        return json.load(f)


def statuses(rows):
    return {r["id"]: r["status"] for r in rows}


class ArchiveStructureTest(unittest.TestCase):
    def test_layout_like_threemfworkspacewriter(self):
        with zipfile.ZipFile(GEAR) as z:
            names = z.namelist()
        for required in ("3D/3dmodel.model", "[Content_Types].xml", "_rels/.rels",
                         "SteSlicer/preferences.cfg", "SteSlicer/version.ini",
                         "SteSlicer/Stereotech Hybrid 530 V5.2.4.global.cfg", "SteSlicer/hte530v5.2.4.def.json",
                         "SteSlicer/fiberpart_pc_g20.xml.fdm_material", "SteSlicer/fast.inst.cfg"):
            self.assertIn(required, names)
        self.assertEqual(sum(n.endswith(".extruder.cfg") for n in names), 2)
        self.assertFalse(any("empty_" in n for n in names), "empty containers are not written")


class InspectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = ste3mf.load_project(GEAR)
        cls.s = cls.p.summary()
        cls.w = ste3mf.load_project(WHEEL)

    def test_machine_mode_quality_material(self):
        s = self.s
        self.assertEqual(s["warnings"], [])
        self.assertEqual(s["machine"]["definition"], "hte530v5.2.4")
        self.assertEqual(s["printing_mode"]["value"], "classic")
        self.assertEqual(s["printing_mode"]["source"], "global.user")
        self.assertEqual(s["quality"]["quality_type"], "fast")
        self.assertEqual(s["extruders"]["0"]["material"]["base_file"], "fiberpart_pc_g20")
        self.assertEqual(s["extruders"]["0"]["material"]["GUID"], "21c7f710-c23e-4799-a8f7-595f92ebec62")

    def test_stack_priority(self):
        r = self.p.resolver
        # качество 'fast' -> 0.15, для заполнения user экструдера важнее качества
        self.assertAlmostEqual(r.resolve("layer_height")["value"], 0.15)
        self.assertEqual(r.resolve("layer_height")["source"], "global.quality")
        self.assertEqual(r.resolve("infill_sparse_density", "0")["value"], 100)
        self.assertEqual(r.resolve("infill_sparse_density", "0")["source"], "extruder0.user")
        # контейнер материала важнее определения (ретракт из XML Fiberpart PC G20)
        self.assertAlmostEqual(r.resolve("retraction_amount", "0")["value"], 6.4)
        self.assertEqual(r.resolve("retraction_amount", "0")["source"], "extruder0.material")
        # формулы: значения принтера для 3D и resolve по экструдерам
        self.assertEqual(r.resolve("machine_height")["value"], 300)
        self.assertIs(r.resolve("machine_center_is_zero")["value"], False)
        self.assertAlmostEqual(r.resolve("material_bed_temperature")["value"], 115)  # максимум по экструдерам
        self.assertEqual(r.resolve("top_layers", "0")["value"], 0)  # заполнение 100% -> 0 верхних слоёв

    def test_defaults_without_user_changes(self):
        def edit(name, data):
            if name == GLOBAL_USER:
                return data.split(b"[values]")[0] + b"[values]\n\n"
            return data
        with tempfile.TemporaryDirectory() as td:
            p = ste3mf.load_project(rewrite(GEAR, os.path.join(td, "x.3mf"), edit))
            r = p.resolver
            self.assertEqual(r.resolve("printing_mode")["value"], "cylindrical_full")  # умолчание fdmprinter
            self.assertEqual(r.resolve("adhesion_type")["value"], "raft")
            self.assertEqual(r.resolve("cylindrical_raft_type")["value"], "solid")
            self.assertAlmostEqual(r.resolve("machine_height")["value"], 231.1)

    def test_5d_values(self):
        r = self.w.resolver
        self.assertEqual(r.resolve("printing_mode")["value"], "cylindrical")
        self.assertEqual(r.resolve("cylindrical_mode_base_diameter")["value"], 16)
        self.assertEqual(r.resolve("machine_shape")["value"], "elliptic")
        self.assertEqual(self.w.objects[0].settings, {"infill_sparse_density": "60"})

    def test_object_geometry(self):
        o = self.s["objects"][0]
        self.assertEqual(o["triangles"], 128)
        self.assertEqual(o["source_axis_pointing_up"]["nearest"], "+Y")
        self.assertEqual(o["world_bbox_3mf"]["size"], [33.1, 33.1, 10.1])
        self.assertEqual(o["position"]["center_rel_plate_center_xy"], [0.0, 0.0])
        self.assertEqual(o["position"]["bottom_z"], 0.0)
        self.assertEqual(o["rotation_vs_source_stl"]["euler_xyz_deg"], [90.0, -0.0, 0.0])
        # сырая матрица 3MF — преобразование сцены с переставленными Y/Z: +90 вокруг X у принтера = 180 вокруг X в 3MF
        self.assertAlmostEqual(o["transform_decomposed"]["axis_angle"]["angle_deg"], 180.0)

    def test_transform_string_convention(self):
        lin, t = ste3mf.parse_3mf_transform("1 0 0 0 0 1 0 -1 0 10 20 30")
        # Cura пишет по столбцам (m00 m10 m20 m01 ...) — это порядок строк-векторов 3MF
        self.assertEqual(lin, [[1, 0, 0], [0, 0, -1], [0, 1, 0]])
        self.assertEqual(t, [10, 20, 30])

    def test_cli_human_and_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            inspect_3mf.main([GEAR])
        out = buf.getvalue()
        self.assertIn("printing_mode = classic (3D)", out)
        self.assertIn("fiberpart_pc_g20", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            inspect_3mf.main([WHEEL, "--json"])
        d = json.loads(buf.getvalue())
        self.assertEqual(d["printing_mode"]["value"], "cylindrical")

    def test_fallback_definitions_when_def_json_missing(self):
        res = ste3mf.find_resources_dir(SOLUTION)
        if res is None:
            self.skipTest("нет файлов слайсера (resources)")
        with tempfile.TemporaryDirectory() as td:
            path = rewrite(GEAR, os.path.join(td, "nodef.3mf"),
                           lambda n, d: None if n.endswith(".def.json") else d)
            p = inspect_3mf.load(path)
            self.assertTrue(any("definitions are not in the archive" in w for w in p.warnings))
            self.assertEqual(p.resolver.resolve("machine_height")["value"], 300)


class CheckTargetTest(unittest.TestCase):
    def test_all_targets_are_valid(self):
        files = sorted(f for f in os.listdir(TARGETS) if f.endswith(".json"))
        self.assertEqual(len(files), 7)
        for f in files:
            t = load_target(f)
            self.assertIn("Гипотеза", t["status"])
            ids = [c["id"] for c in t["checks"]]
            self.assertEqual(len(ids), len(set(ids)), f)
            for c in t["checks"]:
                self.assertIn(c["confidence"], ("high", "medium", "low"), (f, c["id"]))
                self.assertTrue(c["source"], (f, c["id"]))
            kinds = {c["kind"] for c in t["checks"]}
            self.assertTrue({"machine", "setting", "material", "up_axis"} <= kinds, f)
            mode = [c for c in t["checks"] if c["id"] == "printing_mode"][0]["expect"]["eq"]
            if mode != "classic":
                self.assertIn("axis_on_rotation_axis", kinds, f)

    def test_gear_fixture_matches_gear_target(self):
        rows = check_3mf.check_target(ste3mf.load_project(GEAR), load_target("01_gear.json"))
        st = statuses(rows)
        self.assertTrue(all(v == check_3mf.PASS for v in st.values()), st)
        self.assertEqual(check_3mf.total_score(rows), 100.0)

    def test_wheel_fixture_matches_wheel_target(self):
        rows = check_3mf.check_target(ste3mf.load_project(WHEEL), load_target("02_conveyor_wheel.json"))
        st = statuses(rows)
        # Фикстура напечатана из fiberpart_tpu_c5, а цель считает его только допустимым (углеродный
        # наполнитель против «не пачкать и не пылить» из ТЗ), поэтому у материала «~».
        self.assertEqual(st.pop("material"), check_3mf.PARTIAL)
        self.assertTrue(all(v == check_3mf.PASS for v in st.values()), st)

    def test_wrong_mode_and_orientation_detected(self):
        rows = check_3mf.check_target(ste3mf.load_project(GEAR), load_target("02_conveyor_wheel.json"))
        st = statuses(rows)
        self.assertEqual(st["printing_mode"], check_3mf.FAIL)
        self.assertEqual(st["material"], check_3mf.FAIL)
        self.assertEqual(st["height_z"], check_3mf.FAIL)
        self.assertLess(check_3mf.total_score(rows), 75)

    def test_acceptable_alternative_gives_partial(self):
        def edit(name, data):
            if name == GLOBAL_USER:
                return data.replace(b"printing_mode = cylindrical", b"printing_mode = cylindrical_full")
            return data
        with tempfile.TemporaryDirectory() as td:
            p = ste3mf.load_project(rewrite(WHEEL, os.path.join(td, "full.3mf"), edit))
            st = statuses(check_3mf.check_target(p, load_target("02_conveyor_wheel.json")))
            self.assertEqual(st["printing_mode"], check_3mf.PARTIAL)

    def test_off_axis_5d_fails(self):
        def edit(name, data):
            if name == "3D/3dmodel.model":
                txt = data.decode("utf-8")
                m = re.search(r'transform="([^"]+)"', txt)
                vals = m.group(1).split()
                vals[9] = repr(float(vals[9]) + 20.0)  # move 20 mm in X
                return txt.replace(m.group(1), " ".join(vals)).encode("utf-8")
            return data
        with tempfile.TemporaryDirectory() as td:
            p = ste3mf.load_project(rewrite(WHEEL, os.path.join(td, "off.3mf"), edit))
            st = statuses(check_3mf.check_target(p, load_target("02_conveyor_wheel.json")))
            self.assertEqual(st["axis_on_rotation_axis"], check_3mf.FAIL)
            self.assertEqual(st["orientation"], check_3mf.PASS)

    def test_cli_min_score_exit_code(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_ok = check_3mf.main([GEAR, "--target", os.path.join(TARGETS, "01_gear.json"), "--min-score", "90"])
            rc_bad = check_3mf.main([GEAR, "--target", os.path.join(TARGETS, "05_wilo_impeller.json"),
                                     "--min-score", "90"])
        self.assertEqual(rc_ok, 0)
        self.assertEqual(rc_bad, 1)
        self.assertIn("score", buf.getvalue())


class CheckReferenceTest(unittest.TestCase):
    def test_same_file_is_100(self):
        p = ste3mf.load_project(GEAR)
        rows = check_3mf.compare_reference(p, ste3mf.load_project(GEAR))
        self.assertEqual(check_3mf.total_score(rows), 100.0, [r for r in rows if r["status"] != "✓"])

    def test_different_projects(self):
        rows = check_3mf.compare_reference(ste3mf.load_project(GEAR), ste3mf.load_project(WHEEL))
        st = statuses(rows)
        self.assertEqual(st["machine"], check_3mf.PASS)
        self.assertEqual(st["printing_mode"], check_3mf.FAIL)
        self.assertEqual(st["material_E0"], check_3mf.FAIL)
        self.assertEqual(st["layer_height"], check_3mf.FAIL)
        self.assertEqual(st["obj1.orientation"], check_3mf.PASS)  # в обоих +Y STL смотрит вверх
        self.assertEqual(st["obj1.per_object_settings"], check_3mf.FAIL)


class CheckGcodeTest(unittest.TestCase):
    @staticmethod
    def read(name):
        with open(os.path.join(FIX, name), encoding="utf-8") as f:
            return f.read()

    def test_3d_gcode_with_project(self):
        rows, _ = check_gcode.check(self.read("gear_3d.gcode"), ste3mf.load_project(GEAR))
        st = statuses(rows)
        self.assertTrue(all(v == check_gcode.PASS for v in st.values()), st)

    def test_5d_gcode_with_project(self):
        rows, _ = check_gcode.check(self.read("wheel_5d.gcode"), ste3mf.load_project(WHEEL))
        st = statuses(rows)
        self.assertEqual(st["rotary_axes"], check_gcode.PASS)
        self.assertEqual(st["coordinate_system"], check_gcode.PASS)
        self.assertFalse(check_gcode.errors(rows), rows)

    def test_detects_problems(self):
        bad = self.read("gear_3d.gcode").replace("G1 X192.0 Y164.7 E2.4", "G1 X1e9 Ynan E2.4") \
            .replace("M104 S290", "M104 S250").replace("M109 S290", "M109 S250")
        rows, _ = check_gcode.check(bad, ste3mf.load_project(GEAR))
        st = statuses(rows)
        self.assertEqual(st["no_nan_inf"], check_gcode.FAIL)
        self.assertEqual(st["no_huge_coords"], check_gcode.FAIL)
        self.assertEqual(st["hotend_temp_vs_3mf"], check_gcode.FAIL)

    def test_mode_mismatch_and_empty(self):
        rows, _ = check_gcode.check(self.read("wheel_5d.gcode"), ste3mf.load_project(GEAR))
        self.assertEqual(statuses(rows)["mode_vs_3mf"], check_gcode.FAIL)
        rows, _ = check_gcode.check("   \n", None)
        self.assertEqual(rows[0]["status"], check_gcode.FAIL)

    def test_settings_block_roundtrip(self):
        st = check_gcode.parse(self.read("gear_3d.gcode"))
        sb = check_gcode.parse_settings_block(st)
        self.assertEqual(sb["global"]["printing_mode"], "classic")
        self.assertEqual(sb["extruders"][0]["material_print_temperature"], "290")


if __name__ == "__main__":
    unittest.main()
