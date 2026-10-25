"""Тесты agent/knowledge/settings_catalog.py. Запуск из корня репозитория: python -m unittest"""

import os
import sys
import unittest

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "AIAssistant")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from agent.knowledge.settings_catalog import SettingValueError, get_catalog  # noqa: E402


class CatalogDataTest(unittest.TestCase):
    def setUp(self):
        self.catalog = get_catalog()

    def test_singleton_and_size(self):
        self.assertIs(self.catalog, get_catalog())
        self.assertGreater(len(self.catalog), 600)
        size = os.path.getsize(os.path.join(PLUGIN_DIR, "agent", "knowledge", "settings_catalog.json"))
        self.assertLess(size, 1500 * 1024)

    def test_entries(self):
        entry = self.catalog.get("infill_sparse_density")
        self.assertEqual(entry["type"], "float")
        self.assertEqual(entry["unit"], "%")
        self.assertEqual(entry["default"], 20)
        self.assertEqual(entry["label_ru"], "Плотность заполнения")
        self.assertTrue(entry["important"])
        self.assertIsNone(self.catalog.get("fill_density"))

    def test_h530_effective_defaults(self):
        get = self.catalog.get
        self.assertEqual(get("printing_mode")["default"], "cylindrical_full")
        self.assertEqual(get("adhesion_type")["default"], "raft")
        self.assertEqual(get("adhesion_type")["default_3d"], "brim")
        self.assertEqual(get("machine_height")["default"], 231.1)
        self.assertEqual(get("machine_height")["default_3d"], 300)
        self.assertEqual(get("machine_width")["default"], 351)
        self.assertEqual(get("retraction_amount")["default"], 1)
        self.assertEqual(get("material_print_temperature")["default"], 200)  # generic_pla_175
        self.assertEqual(get("infill_pattern")["default"], "grid")
        self.assertEqual(get("wall_line_count")["default"], 2)
        self.assertIn("formula", get("infill_pattern"))
        self.assertEqual(get("layer_height")["max_warn"], 0.32)  # 0.8 * machine_nozzle_size

    def test_flags(self):
        self.assertTrue(self.catalog.get("cylindrical_mode_base_diameter").get("mode_5d"))
        self.assertTrue(self.catalog.get("speed_infill_cylindrical").get("mode_5d"))
        self.assertFalse(self.catalog.get("infill_sparse_density").get("mode_5d", False))
        self.assertFalse(self.catalog.get("layer_height")["per_extruder"])
        self.assertTrue(self.catalog.get("material_print_temperature")["per_extruder"])
        keys = self.catalog.important_keys()
        for key in ("printing_mode", "layer_height", "infill_sparse_density", "support_enable", "adhesion_type"):
            self.assertIn(key, keys)
        for key in keys:
            self.assertIsNotNone(self.catalog.get(key), key)


class ResolveTest(unittest.TestCase):
    SYNONYMS = {
        "fill_density": "infill_sparse_density",
        "infill": "infill_sparse_density",
        "infill_density": "infill_sparse_density",
        "Infill Density": "infill_sparse_density",
        "sparse_infill_density": "infill_sparse_density",
        "temperature": "material_print_temperature",
        "nozzle_temp": "material_print_temperature",
        "bed_temp": "material_bed_temperature",
        "supports": "support_enable",
        "perimeters": "wall_line_count",
        "walls": "wall_line_count",
        "wall_loops": "wall_line_count",
        "print_speed": "speed_print",
        "layer": "layer_height",
        "first_layer_height": "layer_height_0",
        "fill_pattern": "infill_pattern",
        "support_on_build_plate_only": "support_type",
        "tree_support": "support_tree_enable",
        "retract_length": "retraction_amount",
        "z_hop": "retraction_hop_enabled",
        "fan_speed": "cool_fan_speed",
        "vase_mode": "magic_spiralize",
        "horizontal_expansion": "xy_offset",
        "заполнение": "infill_sparse_density",
        "Плотность заполнения": "infill_sparse_density",
        "высота слоя": "layer_height",
        "поддержки": "support_enable",
        "температура стола": "material_bed_temperature",
        "стенки": "wall_line_count",
        "ретракт": "retraction_amount",
        "режим печати": "printing_mode",
        "диаметр основания": "cylindrical_mode_base_diameter",
    }

    def setUp(self):
        self.catalog = get_catalog()

    def test_exact(self):
        self.assertEqual(self.catalog.resolve("layer_height"), ("layer_height", None))
        self.assertEqual(self.catalog.resolve(" Layer-Height "), ("layer_height", None))

    def test_synonyms(self):
        for alias, expected in self.SYNONYMS.items():
            key, hint = self.catalog.resolve(alias)
            self.assertEqual(key, expected, "%s -> %s (%s)" % (alias, key, hint))
            self.assertIsNone(hint)

    def test_value_given_as_key(self):
        for alias in ("brim", "кайма", "raft"):
            key, hint = self.catalog.resolve(alias)
            self.assertIsNone(key)
            self.assertIn("adhesion_type", hint)
        key, hint = self.catalog.resolve("brim")
        self.assertIn('adhesion_type="brim"', hint)
        key, hint = self.catalog.resolve("gyroid")
        self.assertIn('infill_pattern="gyroid"', hint)
        key, hint = self.catalog.resolve("5D")
        self.assertIn("printing_mode", hint)

    def test_not_a_setting(self):
        key, hint = self.catalog.resolve("material")
        self.assertIsNone(key)
        self.assertIn("set_material", hint)
        key, hint = self.catalog.resolve("quality")
        self.assertIn("select_profile", hint)

    def test_fuzzy(self):
        key, hint = self.catalog.resolve("layer_heigth")
        self.assertIsNone(key)
        self.assertIn("layer_height", hint)
        key, hint = self.catalog.resolve("fill_densty")
        self.assertIn("infill_sparse_density", hint)
        key, hint = self.catalog.resolve("infil_sparse_density")
        self.assertIn("infill_sparse_density", hint)

    def test_unknown(self):
        key, hint = self.catalog.resolve("qwertyuiop")
        self.assertIsNone(key)
        self.assertIn("search_settings", hint)
        self.assertIsNone(self.catalog.resolve("")[0])


class CoerceTest(unittest.TestCase):
    def setUp(self):
        self.catalog = get_catalog()

    def coerce(self, key, value):
        return self.catalog.coerce(key, value)

    def test_numbers(self):
        self.assertEqual(self.coerce("infill_sparse_density", "40%"), (40.0, None))
        self.assertEqual(self.coerce("infill_sparse_density", 40), (40.0, None))
        self.assertEqual(self.coerce("layer_height", "0,2"), (0.2, None))
        self.assertEqual(self.coerce("layer_height", "0.2 мм"), (0.2, None))
        self.assertEqual(self.coerce("material_print_temperature", "230°C"), (230.0, None))
        self.assertEqual(self.coerce("speed_print", "50 mm/s"), (50.0, None))
        value, warning = self.coerce("wall_line_count", "3")
        self.assertEqual(value, 3)
        self.assertIsInstance(value, int)
        self.assertEqual(self.coerce("wall_line_count", 3.0), (3, None))
        self.assertEqual(self.coerce("support_extruder_nr", "1"), (1, None))

    def test_synonym_key_accepted(self):
        self.assertEqual(self.coerce("fill_density", "40%"), (40.0, None))

    def test_number_errors(self):
        for key, value in (("wall_line_count", 2.5), ("layer_height", "abc"), ("layer_height", True),
                           ("layer_height", None), ("layer_height", [1])):
            with self.assertRaises(SettingValueError):
                self.coerce(key, value)

    def test_percent_fraction_warning(self):
        value, warning = self.coerce("infill_sparse_density", 0.4)
        self.assertEqual(value, 0.4)
        self.assertIn("40", warning)

    def test_bool(self):
        for raw in (True, "true", "True", "yes", "да", "Да", "вкл", "on", 1, "1"):
            self.assertEqual(self.coerce("support_enable", raw), (True, None), raw)
        for raw in (False, "false", "no", "нет", "выкл", "off", 0, "0"):
            self.assertEqual(self.coerce("support_enable", raw), (False, None), raw)
        with self.assertRaises(SettingValueError):
            self.coerce("support_enable", "maybe")
        with self.assertRaises(SettingValueError):
            self.coerce("support_enable", 5)

    def test_enum_adhesion(self):
        for raw, expected in (("brim", "brim"), ("Brim", "brim"), ("Кайма", "brim"), ("raft", "raft"),
                              ("Подложка", "raft"), ("юбка", "skirt"), ("none", "none"), ("Нет", "none"),
                              ("без", "none")):
            self.assertEqual(self.coerce("adhesion_type", raw), (expected, None), raw)
        with self.assertRaises(SettingValueError) as ctx:
            self.coerce("adhesion_type", "glue")
        self.assertIn("brim", str(ctx.exception))

    def test_enum_support_type(self):
        self.assertEqual(self.coerce("support_type", "everywhere")[0], "everywhere")
        self.assertEqual(self.coerce("support_type", "Везде")[0], "everywhere")
        self.assertEqual(self.coerce("support_type", "от стола")[0], "buildplate")
        self.assertEqual(self.coerce("support_type", "Touching Buildplate")[0], "buildplate")
        self.assertEqual(self.coerce("support_type", "build plate only")[0], "buildplate")

    def test_enum_infill_pattern(self):
        for raw, expected in (("gyroid", "gyroid"), ("Гироид", "gyroid"), ("Сетка", "grid"), ("lines", "lines"),
                              ("Octet", "tetrahedral"), ("Zig Zag", "zigzag"), ("rectilinear", "lines"),
                              ("Четверть куба", "quarter_cubic")):
            self.assertEqual(self.coerce("infill_pattern", raw), (expected, None), raw)
        with self.assertRaises(SettingValueError):
            self.coerce("infill_pattern", "honeycomb-ish-thing")

    def test_enum_printing_mode(self):
        for raw, expected in (("classic", "classic"), ("3D", "classic"), ("3d", "classic"),
                              ("5D", "cylindrical_full"), ("cylindrical_full", "cylindrical_full"),
                              ("5D Spiral", "cylindrical"), ("cylindrical", "cylindrical"),
                              ("спираль", "cylindrical")):
            self.assertEqual(self.coerce("printing_mode", raw), (expected, None), raw)

    def test_hard_limits(self):
        with self.assertRaises(SettingValueError) as ctx:
            self.coerce("layer_height", 0)
        self.assertIn("0.001", str(ctx.exception))
        with self.assertRaises(SettingValueError):
            self.coerce("infill_sparse_density", -5)
        with self.assertRaises(SettingValueError):
            self.coerce("cool_fan_speed", 150)
        with self.assertRaises(SettingValueError):
            self.coerce("cylindrical_mode_base_diameter", 2)

    def test_warning_limits(self):
        value, warning = self.coerce("layer_height", 0.5)
        self.assertEqual(value, 0.5)
        self.assertIn("0.32", warning)
        value, warning = self.coerce("infill_sparse_density", 150)
        self.assertIsNotNone(warning)
        value, warning = self.coerce("cylindrical_mode_base_diameter", 4.5)
        self.assertIsNotNone(warning)
        self.assertIsNone(self.coerce("layer_height", 0.2)[1])

    def test_formula_limits(self):
        entry = self.catalog.get("support_wall_count")
        self.assertIsInstance(entry["min_warn"], str)
        # без контекста пределы-формулы пропускаются
        self.assertEqual(self.coerce("support_wall_count", 0), (0, None))
        value, warning = self.catalog.coerce("support_wall_count", 0, context={"support_pattern": "concentric"})
        self.assertIsNotNone(warning)

    def test_list_and_str(self):
        self.assertEqual(self.coerce("infill_angles", "0,90")[0], "[0, 90]")
        self.assertEqual(self.coerce("infill_angles", [45, 135])[0], "[45, 135]")
        with self.assertRaises(SettingValueError):
            self.coerce("infill_angles", "[a]")

    def test_unknown_key(self):
        with self.assertRaises(SettingValueError) as ctx:
            self.coerce("layer_heigth", 0.2)
        self.assertIn("layer_height", str(ctx.exception))


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.catalog = get_catalog()

    def keys(self, query, **kwargs):
        return [item["key"] for item in self.catalog.search(query, **kwargs)]

    def test_shape(self):
        item = self.catalog.search("layer_height", limit=1)[0]
        for field in ("key", "label", "label_ru", "type", "unit", "default", "category", "important"):
            self.assertIn(field, item)
        self.assertEqual(item["key"], "layer_height")

    def test_russian(self):
        self.assertEqual(self.keys("заполнение")[0], "infill_sparse_density")
        self.assertEqual(self.keys("высота слоя")[0], "layer_height")
        self.assertEqual(self.keys("температура стола")[0], "material_bed_temperature")
        self.assertIn("support_enable", self.keys("поддержк", limit=3))
        self.assertEqual(self.keys("кайма")[0], "adhesion_type")
        self.assertIn("brim_width", self.keys("кайма", limit=5))
        self.assertIn("retraction_amount", self.keys("ретракт", limit=3))

    def test_english_and_ranking(self):
        self.assertEqual(self.keys("infill_sparse_density")[0], "infill_sparse_density")
        self.assertEqual(self.keys("retraction")[0], "retraction_amount")
        self.assertEqual(self.keys("fill_density")[0], "infill_sparse_density")
        self.assertEqual(self.keys("5D")[0], "printing_mode")

    def test_limit_and_category(self):
        self.assertEqual(len(self.keys("speed", limit=3)), 3)
        for item in self.catalog.search("speed", limit=20, category="cooling"):
            self.assertEqual(item["category"], "cooling")
        self.assertEqual(self.keys(""), [])


if __name__ == "__main__":
    unittest.main()
