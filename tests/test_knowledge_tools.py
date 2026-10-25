"""Инструменты базы знаний (recommend_material, get_print_preset) и их согласие с devtools/targets.

Требования из ТЗ, которые должна вычитать LLM (devtools/pipeline_dryrun.TZ_EXTRACT), подаются в
детерминированные инструменты; их ответы по материалу и качеству должны попасть в `expect` целей.
Вся цепочка на моке слайсера: python devtools/pipeline_dryrun.py

Запуск из корня репозитория: python -m unittest tests.test_knowledge_tools
"""

import json
import os
import sys
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_DIR, "plugin", "AIAssistant")
DEVTOOLS_DIR = os.path.join(REPO_DIR, "devtools")
TARGETS_DIR = os.path.join(DEVTOOLS_DIR, "targets")
for path in (PLUGIN_DIR, DEVTOOLS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent.tools import knowledge_tools as kt  # noqa: E402
from agent.tools.registry import ToolError  # noqa: E402
import pipeline_dryrun  # noqa: E402


def load_targets():
    targets = {}
    for name in sorted(os.listdir(TARGETS_DIR)):
        if name.endswith(".json"):
            with open(os.path.join(TARGETS_DIR, name), encoding="utf-8") as handle:
                targets[name[:2]] = json.load(handle)
    return targets


def check(target, check_id):
    for item in target["checks"]:
        if item["id"] == check_id:
            return item
    return None


def mode_of(target):
    return check(target, "printing_mode")["expect"]["eq"]


class TargetsConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.targets = load_targets()

    def test_seven_targets_with_tz_extract(self):
        self.assertEqual(sorted(self.targets), ["01", "02", "03", "04", "05", "06", "07"])
        self.assertEqual(sorted(pipeline_dryrun.TZ_EXTRACT), sorted(self.targets))

    @unittest.skipUnless(os.path.isdir(os.path.join(REPO_DIR, "models", "1. Шестерня - 3D")),
                         "нет моделей кейса в models/ (см. models/README.md)")
    def test_tz_documents_exist(self):
        for prefix, extract in pipeline_dryrun.TZ_EXTRACT.items():
            self.assertTrue(os.path.isfile(os.path.join(pipeline_dryrun.REPO_DIR, extract["tz"])), extract["tz"])

    def test_every_check_has_source_and_confidence(self):
        for prefix, target in self.targets.items():
            for item in target["checks"]:
                self.assertTrue(item.get("source"), "%s %s: no source" % (prefix, item["id"]))
                self.assertIn(item.get("confidence"), ("high", "medium", "low"), "%s %s" % (prefix, item["id"]))

    def test_materials_and_qualities_exist(self):
        ids = set(m["id"] for m in kt._materials()["materials"])
        qualities = set(kt._presets()["quality_types"])
        for prefix, target in self.targets.items():
            material = check(target, "material")
            for key in ("expect", "acceptable"):
                for material_id in (material.get(key) or {}).get("one_of", []):
                    self.assertIn(material_id, ids, "%s: %s" % (prefix, material_id))
            quality = check(target, "quality")
            for key in ("expect", "acceptable"):
                for quality_type in (quality.get(key) or {}).get("one_of", []):
                    self.assertIn(quality_type, qualities, "%s: %s" % (prefix, quality_type))


class ToolsAgreeWithTargetsTest(unittest.TestCase):
    """Обычный путь из system_ru.md: одни только инструменты должны попасть в `expect` целей."""

    def setUp(self):
        self.targets = load_targets()

    def test_recommend_material_top1_in_expect(self):
        for prefix, extract in pipeline_dryrun.TZ_EXTRACT.items():
            ranked = kt.recommend_material(dict(extract["material"]))["ranked"]
            expected = check(self.targets[prefix], "material")["expect"]["one_of"]
            self.assertIn(ranked[0]["id"], expected, "%s: %s" % (prefix, [r["id"] for r in ranked]))

    def test_preset_quality_and_layer_in_expect(self):
        for prefix, extract in pipeline_dryrun.TZ_EXTRACT.items():
            target = self.targets[prefix]
            preset = kt.get_print_preset({"tasks": extract["tasks"], "mode": mode_of(target)})
            self.assertIn(preset["quality_type"], check(target, "quality")["expect"]["one_of"], prefix)
            layer = check(target, "layer_height")["expect"]
            self.assertTrue(layer["min"] - 1e-9 <= preset["layer_height"] <= layer["max"] + 1e-9, prefix)

    def test_preset_plus_step6_matches_setting_checks(self):
        """Настройки пресета + шаг 6 промпта (числа из ручного разбора моделей) против проверок настроек."""
        geometry = {"01": (3.8, 29.3, False), "02": (46.7, 101.5, None), "03": (71.42, 690.7, True),
                    "04": (725.7, 474.8, True), "05": (152.6, 766.5, None), "06": (53.18, 692.2, True),
                    "07": (15.23, 49.1, None)}
        for prefix, extract in pipeline_dryrun.TZ_EXTRACT.items():
            target = self.targets[prefix]
            mode = mode_of(target)
            settings = dict(kt.get_print_preset({"tasks": extract["tasks"], "mode": mode})["settings"])
            volume, surface, needs_support = geometry[prefix]
            analysis = {"geometry": {"volume_cm3": volume, "surface_cm2": surface}}
            if needs_support is not None:
                analysis["best_orientation_3d"] = {"needs_support": needs_support, "description": "test"}
            pipeline_dryrun.llm_step6(settings, extract["tasks"], mode, analysis)
            for key in ("wall_line_count", "infill_sparse_density", "support_enable", "adhesion_type"):
                item = check(target, key)
                if item is None or key not in settings:
                    continue
                expect = item["expect"]
                value = settings[key]
                if "eq" in expect:
                    self.assertEqual(value, expect["eq"], "%s %s" % (prefix, key))
                if "one_of" in expect:
                    self.assertIn(value, expect["one_of"], "%s %s" % (prefix, key))
                if "min" in expect:
                    self.assertGreaterEqual(value, expect["min"], "%s %s" % (prefix, key))
                if "max" in expect:
                    self.assertLessEqual(value, expect["max"], "%s %s" % (prefix, key))


class RecommendMaterialTest(unittest.TestCase):
    def test_abrasive_penalty_prefers_unfilled_at_near_tie(self):
        ranked = kt.recommend_material({"needs": ["toughness", "uv", "surface"], "service_temp_c": 60,
                                        "kind": "rigid"})["ranked"]
        self.assertEqual(ranked[0]["id"], "enduse_petg")
        self.assertEqual(ranked[1]["id"], "fiberpart_abspa_g8")

    def test_no_hardened_nozzle_excludes_abrasive(self):
        ranked = kt.recommend_material({"needs": ["moisture", "stiffness", "uv"], "hardened_nozzle": False,
                                        "limit": 5})["ranked"]
        self.assertTrue(ranked)
        self.assertFalse(any(r["needs_hardened_nozzle"] for r in ranked))

    def test_temperature_filter_and_relaxed_warning(self):
        result = kt.recommend_material({"needs": ["stiffness"], "service_temp_c": 500})
        self.assertIn("warning", result)
        result = kt.recommend_material({"needs": ["hot_water_glycol"], "service_temp_c": 85, "max_density": 1.2})
        self.assertTrue(all(r["service_temp_c_est"] >= 85 and r["density"] <= 1.2 for r in result["ranked"]))

    def test_flexible_kind(self):
        ranked = kt.recommend_material({"needs": ["flexibility", "oil", "fuel"], "service_temp_c": 75,
                                        "kind": "flexible"})["ranked"]
        self.assertEqual(ranked[0]["id"], "sealant_tpv80")


class PrintPresetTest(unittest.TestCase):
    def test_precise_fit_quality_depends_on_mode(self):
        classic = kt.get_print_preset({"tasks": ["precise_fit"]})
        self.assertEqual(classic["quality_type"], "normal")
        spiral = kt.get_print_preset({"tasks": ["rotation_5d", "precise_fit"], "mode": "cylindrical",
                                      "base_diameter": 16})
        self.assertEqual(spiral["quality_type"], "fast")
        self.assertTrue(any("5D Spiral" in note for note in spiral["notes"]))
        full = kt.get_print_preset({"tasks": ["rotation_5d", "precise_fit"], "mode": "cylindrical_full",
                                    "base_diameter": 6})
        self.assertEqual(full["quality_type"], "normal")

    def test_5d_drops_plate_keys_and_maps_infill(self):
        preset = kt.get_print_preset({"tasks": ["functional_load", "decorative"], "mode": "cylindrical_full",
                                      "base_diameter": 6})
        settings = preset["settings"]
        for key in ("adhesion_type", "brim_width", "support_tree_enable"):
            self.assertNotIn(key, settings)
        self.assertIn(settings["infill_pattern"], kt.FIVE_D_INFILL)
        self.assertEqual(settings["cylindrical_mode_base_diameter"], 6)
        with self.assertRaises(ToolError):
            kt.get_print_preset({"tasks": ["rotation_5d"], "mode": "cylindrical", "base_diameter": 3})

    def test_merge_rule(self):
        preset = kt.get_print_preset({"tasks": ["prototype", "functional_load", "heat_resistant"],
                                      "material_id": "enduse_pa6"})
        settings = preset["settings"]
        self.assertEqual(preset["quality_type"], "fast")   # самый тонкий слой из draft/fast/fast
        self.assertEqual(settings["wall_line_count"], 4)
        self.assertEqual(settings["brim_width"], 8)
        self.assertEqual(settings["adhesion_type"], "brim")
        self.assertEqual(settings["speed_print"], 45)   # min(material 50, caps 60/50/45)

    def test_quality_labels_match_slicer_profiles(self):
        # resources/quality/*.inst.cfg STE Slicer: quality_type -> название в интерфейсе, высота слоя
        expected = {"high": ("Extra Fine", 0.06), "normal": ("Fine", 0.1), "fast": ("Normal", 0.15),
                    "draft": ("Draft", 0.2), "verydraft": ("Extra Fast", 0.3)}
        for quality_type, data in kt._presets()["quality_types"].items():
            self.assertEqual((data["label"], data["layer_height"]), expected[quality_type])


class Step6Test(unittest.TestCase):
    def test_thin_wall_and_small_part(self):
        settings = {"wall_line_count": 3, "infill_sparse_density": 30}
        pipeline_dryrun.llm_step6(settings, ["flexible"], "classic",
                                  {"geometry": {"volume_cm3": 71.42, "surface_cm2": 690.7}})
        self.assertEqual(settings["wall_line_count"], 5)     # 2V/S = 2.07 mm -> 5 lines of 0.4
        self.assertEqual(settings["infill_sparse_density"], 100)
        settings = {"wall_line_count": 4, "infill_sparse_density": 40}
        pipeline_dryrun.llm_step6(settings, ["functional_load"], "cylindrical_full",
                                  {"geometry": {"volume_cm3": 15.2, "surface_cm2": 49.1}})
        self.assertEqual(settings, {"wall_line_count": 4, "infill_sparse_density": 100})


if __name__ == "__main__":
    unittest.main()
