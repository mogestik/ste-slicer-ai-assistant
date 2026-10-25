"""Прогон цепочки инструментов агента на 7 тестовых моделях без LLM (мок слайсера).

    python devtools/pipeline_dryrun.py [--only 07] [--from-target] [--keep DIR] [--verbose] [--all-rows]

Режим по умолчанию ("tools"): все решения принимают сами инструменты плагина, по тому же
пути, что описан в agent/prompts/system_ru.md:
  analyze_model -> set_print_mode(рекомендованный) -> recommend_material(требования ТЗ) ->
  set_material(первый в рейтинге) -> get_print_preset(задачи ТЗ, режим, материал, Base Diameter) ->
  select_profile(quality_type пресета) -> set_settings(настройки пресета + поправки шага 6) ->
  load_model -> suggest_orientation -> rotate_object(reset) -> move_object(frame=scene) ->
  start_slicing -> save_project -> check_3mf по devtools/targets/*.json.
Вместо LLM здесь только TZ_EXTRACT ниже: то, что модель должна вычитать из каждого ТЗ
(требования для recommend_material, задачи для get_print_preset). Поправки шага 6 промпта
(мелкая нагруженная деталь, тонкие стенки, поддержки) делает llm_step6() по цифрам из
analyze_model.

--from-target: материал и качество берутся из цели (старая проверка «трубопровода»:
геометрия -> обработчики -> запись и проверка 3MF), из геометрии — только Base Diameter.

По каждой модели скрипт печатает принятые решения и таблицу всех проверок, кроме ✓:
ключ -> что получилось у инструментов -> что ожидает цель (| что допустимо).
Код выхода 1, если не прошла проверка постановки/режима или любая проверка высокой
уверенности получила ✗. Модели кейса должны лежать в models/ (см. models/README.md).
"""

import argparse
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
PLUGIN_DIR = os.path.join(REPO_DIR, "plugin", "AIAssistant")
TARGETS_DIR = os.path.join(HERE, "targets")
sys.path.insert(0, PLUGIN_DIR)
sys.path.insert(0, HERE)

from agent import main_thread  # noqa: E402
from agent.slicer import set_adapter  # noqa: E402
from agent.tools import build_registry  # noqa: E402
from agent.tools.registry import ToolContext  # noqa: E402
import check_3mf  # noqa: E402
import mock_slicer  # noqa: E402
from inspect_3mf import load as load_project  # noqa: E402

PLACEMENT_CHECKS = ("printing_mode", "objects", "bottom_on_plate", "inside_volume", "orientation",
                    "axis_on_rotation_axis", "height_z", "center_xy")

# Шаг 6 из system_ru.md, числа — как в заметках task_presets.json.
SMALL_PART_CM3 = 20.0
THIN_WALL_MM = 2.5
LINE_WIDTH_MM = 0.4

# Что LLM должна вычитать из каждого ТЗ (шаг 1 промпта).
# Ключ — префикс файла цели. "tz" — исходный документ (текст есть и в models/_TZ_all.txt).
# "material" — аргументы recommend_material: требования в порядке важности по ТЗ и
# ограничения по температуре и плотности из ТЗ. "tasks" — задачи для get_print_preset.
TZ_EXTRACT = OrderedDict([
    ("01", {
        "tz": "models/1. Шестерня - 3D/Техническое задание для шестерни.docx",
        "material": {"needs": ["moisture", "stiffness", "uv", "acid", "toughness"],
                     "service_temp_c": 70, "min_tg_c": 85, "kind": "rigid"},
        "tasks": ["functional_load", "precise_fit"],
        "why": "§4: гидрофобный, высокий модуль, УФ, Tg >= 85-90; §2: +70 °C, подкормки (слабые кислоты, соли), "
               "микроудары и усталость зуба; §3: точность эвольвенты и шага; пиковый момент 75 Н·м",
    }),
    ("02", {
        "tz": "models/2. Колесо конвейерной линии - 5D/Техническое задание для колеса конвейерной линии.docx",
        "material": {"needs": ["grip", "clean", "toughness", "wear"], "service_temp_c": 22},
        "tasks": ["rotation_5d", "precise_fit"],
        "why": "§4: трение по бумаге, без следов и пыления, нет хрупкости, сжатие 18 кг; §2: +20 ± 2 °C, IPA; "
               "§3: отверстие ±0.1; ESD только «желательно» (проводящий наполнитель противоречит «без следов»)",
    }),
    ("03", {
        "tz": "models/3. Пыльник Шруса - 3D/Техническое задание для пыльника шруса.docx",
        "material": {"needs": ["flexibility", "oil", "fuel", "toughness", "acid"],
                     "service_temp_c": 75, "kind": "flexible"},
        "tasks": ["flexible", "chemical"],
        "why": "§3: эластичность 0..+75 °C, масло- и бензостойкость, гибкий и прочный; §2: смазка MoS2, "
               "реагенты (NaCl, CaCl2), бензин; жёсткие материалы запрещены (§1)",
    }),
    ("04", {
        "tz": "models/4. Фигурка/Техническое задание для декоративной фигурки.docx",
        "material": {"needs": ["toughness", "uv", "surface"], "service_temp_c": 60, "kind": "rigid"},
        "tasks": ["decorative"],
        "why": "§3: вязкость тонких элементов, УФ (не желтеть); §2: +15..+60 °C, спиртовые салфетки; "
               "§1: без видимых слоёв и следов поддержек, партия 15 шт.",
    }),
    ("05", {
        "tz": "models/5. Рабочее колесо насоса Wilo - 5D/Техническое задания для рабочего колеса насоса.docx",
        "material": {"needs": ["hot_water_glycol", "alkali", "moisture", "stiffness", "dielectric"],
                     "service_temp_c": 85, "max_density": 1.2, "kind": "rigid"},
        "tasks": ["rotation_5d", "chemical"],
        "why": "§4: pH 9.5-10.5 при +85 °C, водопоглощение <= 0.05 %, E >= 4500 МПа, ρ <= 1.2, диэлектрик, "
               "без ползучести (Vicat >= 130 и E — ни один материал не закрывает, честность)",
    }),
    ("06", {
        "tz": "models/6. Крыльчатка охлаждения электродвигателя - 3D/"
              "Техническое задание для крыльчатки охлаждения электродвигателя.docx",
        "material": {"needs": ["oil", "toughness", "stiffness"], "service_temp_c": 65,
                     "max_density": 1.25, "kind": "rigid"},
        "tasks": ["functional_load", "chemical", "heat_resistant"],
        "why": "§3: не размягчаться при +65 °C, без хрупкого разрушения и осколков, масляный туман СОЖ, озон, "
               "ρ <= 1.25; 2950 об/мин",
    }),
    ("07", {
        "tz": "models/7. Ось вращения передающих звёзд формовщика коробов Krones - 5D/"
              "Ось вращения передающих звёзд.docx",
        "material": {"needs": ["low_friction", "wear", "alkali", "toughness", "food_contact"],
                     "service_temp_c": 35, "kind": "rigid"},
        "tasks": ["rotation_5d", "functional_load", "precise_fit"],
        "why": "§4: низкое трение по POM без смазки, абразивная пыль, момент 18 Н·м, щёлочь pH 11, клей 160 °C "
               "кратковременно, пищевой контакт; §3: профиль ±0.15 без ступенек",
    }),
])


class FakeQtApp(object):
    def __init__(self):
        self.thread = threading.current_thread()
        self.queue = queue.Queue()

    def getMainThread(self):
        return self.thread

    def callLater(self, func, *args, **kwargs):
        self.queue.put((func, args, kwargs))


class Runner(object):
    def __init__(self, workdir, verbose=False):
        self.app = FakeQtApp()
        main_thread.set_app_provider(lambda: self.app)
        self.registry = build_registry()
        mock_slicer.install(self.registry, slice_step=0.001)
        self.ctx = ToolContext(session_id="dryrun", emit=lambda kind, **data: None,
                               services={"results_dir": workdir})
        self.verbose = verbose

    def call(self, name, args=None):
        box = {}

        def worker():
            box["result"] = self.registry.call(name, args or {}, self.ctx)
            self.app.queue.put(None)

        thread = threading.Thread(target=worker)
        thread.start()
        while True:
            item = self.app.queue.get(timeout=120)
            if item is None:
                break
            func, a, k = item
            func(*a, **k)
        thread.join()
        result = box["result"]
        if self.verbose:
            print("  %s(%s) -> %s" % (name, json.dumps(args, ensure_ascii=False), result.text[:300]))
        if not result.ok:
            raise RuntimeError("%s failed: %s" % (name, result.text))
        return json.loads(result.text)

    def close(self):
        main_thread.set_app_provider(None)
        set_adapter(None)


def _expected(check):
    expect = check.get("expect") or {}
    if "eq" in expect:
        return expect["eq"]
    if "one_of" in expect:
        return expect["one_of"][0]
    return None


def llm_step6(settings, tasks, mode, analysis):
    """Шаг 6 промпта («поправки по детали») по цифрам analyze_model. Возвращает заметки."""
    notes = []
    geometry = analysis["geometry"]
    volume = geometry.get("volume_cm3") or 0.0
    surface = geometry.get("surface_cm2") or 0.0
    if "functional_load" in tasks and 0 < volume < SMALL_PART_CM3:
        settings["infill_sparse_density"] = 100
        notes.append("мелкая нагруженная (%.1f см³ < %g) -> заполнение 100" % (volume, SMALL_PART_CM3))
    if surface > 0:
        wall = 20.0 * volume / surface   # средняя толщина стенки оболочки, мм: 2V/S
        if wall <= THIN_WALL_MM:
            lines = max(settings.get("wall_line_count", 2), int(round(wall / LINE_WIDTH_MM)))
            settings["infill_sparse_density"] = 100
            settings["wall_line_count"] = lines
            notes.append("тонкостенная (2V/S ≈ %.1f мм) -> заполнение 100, стенок %d" % (wall, lines))
    if mode == "classic":
        pose = analysis.get("best_orientation_3d") or {}
        if "needs_support" in pose:
            settings["support_enable"] = bool(pose["needs_support"])
            notes.append("поддержки по свесам позы '%s' (%s мм²) -> %s" % (
                pose.get("description"), pose.get("overhang_mm2"), settings["support_enable"]))
    return notes


def run_model(target_path, workdir, verbose, from_target):
    with open(target_path, encoding="utf-8") as handle:
        target = json.load(handle)
    prefix = os.path.basename(target_path)[:2]
    stl = os.path.join(REPO_DIR, target["stl"])
    checks = {c["id"]: c for c in target["checks"]}
    extract = TZ_EXTRACT.get(prefix)
    decisions = OrderedDict()
    runner = Runner(workdir, verbose)
    try:
        analysis = runner.call("analyze_model", {"path": stl})
        rec = analysis["recommended_mode"]
        mode = rec["printing_mode"]
        geo_settings = rec.get("settings") or {}
        runner.call("set_print_mode", {"mode": mode})
        decisions["mode"] = "%s (conf %s)" % (mode, rec.get("confidence"))

        if from_target or extract is None:
            material = _expected(checks.get("material", {}))
            if material:
                runner.call("set_material", {"material_id": material})
            quality = _expected(checks.get("quality", {}))
            if quality:
                runner.call("select_profile", {"name": quality})
            changes = OrderedDict(geo_settings)
            decisions["material"] = "%s (from target)" % material
            decisions["quality"] = "%s (from target)" % quality
        else:
            ranked = runner.call("recommend_material", extract["material"])["ranked"]
            material = ranked[0]["id"]
            runner.call("set_material", {"material_id": material})
            decisions["material"] = "%s  (next: %s)" % (material, ", ".join(r["id"] for r in ranked[1:3]))
            preset_args = {"tasks": extract["tasks"], "mode": mode, "material_id": material}
            if mode != "classic" and "cylindrical_mode_base_diameter" in geo_settings:
                preset_args["base_diameter"] = geo_settings["cylindrical_mode_base_diameter"]
            preset = runner.call("get_print_preset", preset_args)
            quality = preset["quality_type"]
            runner.call("select_profile", {"name": quality})
            decisions["quality"] = "%s (%s мм), tasks %s" % (quality, preset.get("layer_height"),
                                                             "+".join(extract["tasks"]))
            changes = OrderedDict(preset["settings"])
            for key, value in geo_settings.items():
                changes.setdefault(key, value)
            step6 = llm_step6(changes, extract["tasks"], mode, analysis)
            if step6:
                decisions["step6"] = "; ".join(step6)
        if changes:
            runner.call("set_settings", {"changes": changes})
            decisions["settings"] = ", ".join("%s=%s" % item for item in changes.items())

        loaded = runner.call("load_model", {"path": stl})
        object_id = str(loaded["loaded"][0]["id"])
        orient = runner.call("suggest_orientation", {"path": stl, "mode": mode})
        if "placement" in orient:
            transform = orient["placement"]["transform"]
            decisions["orientation"] = orient["placement"].get("description")
        else:
            transform = orient["candidates"][0]["transform"]
            decisions["orientation"] = orient["candidates"][0].get("description")
        steps = transform.get("rotation_steps") or []
        if steps:
            runner.call("rotate_object", {"object_id": object_id, "steps": steps, "reset_rotation": True})
        center = transform["bbox_center_scene"]
        runner.call("move_object", {"object_id": object_id, "frame": "scene", "x": center[0], "z": center[2]})
        runner.call("start_slicing", {"wait": True})
        project = os.path.join(workdir, os.path.splitext(os.path.basename(target_path))[0] + ".3mf")
        runner.call("save_project", {"path": project})
    finally:
        runner.close()
    rows = check_3mf.check_target(load_project(project, None), target)
    return mode, decisions, rows


def _expect_text(row):
    text = str(row.get("expected"))
    if row.get("acceptable"):
        text += "  | acc " + row["acceptable"]
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="только цели, имя файла которых начинается с этого префикса")
    parser.add_argument("--from-target", action="store_true",
                        help="материал и качество брать из цели, а не из инструментов базы знаний")
    parser.add_argument("--keep", help="сохранить полученные 3MF в эту папку")
    parser.add_argument("--all-rows", action="store_true", help="печатать и строки ✓")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not os.path.isdir(os.path.join(REPO_DIR, "models", "1. Шестерня - 3D")):
        print("Нет моделей кейса в models/ — положите их туда (см. models/README.md).")
        return 2

    workdir = args.keep or tempfile.mkdtemp(prefix="dryrun_")
    if not os.path.isdir(workdir):
        os.makedirs(workdir)
    failed = 0
    summary = []
    print("mode: %s" % ("from-target (material/quality from targets)" if args.from_target
                        else "tools (material/profile/settings from knowledge tools)"))
    try:
        for name in sorted(os.listdir(TARGETS_DIR)):
            if not name.endswith(".json") or (args.only and not name.startswith(args.only)):
                continue
            try:
                mode, decisions, rows = run_model(os.path.join(TARGETS_DIR, name), workdir, args.verbose,
                                                  args.from_target)
            except Exception as exc:  # сообщаем и идём к следующей модели
                failed += 1
                print("%-24s ERROR %s" % (name, exc))
                summary.append((name, None, None, None, None))
                continue
            placement = [r for r in rows if r.get("id") in PLACEMENT_CHECKS]
            bad_placement = [r for r in placement if r.get("status") == check_3mf.FAIL]
            fails = [r for r in rows if r.get("status") == check_3mf.FAIL]
            high_fails = [r for r in fails if r.get("confidence") == "high"]
            failed += bool(bad_placement or high_fails)
            score = check_3mf.total_score(rows)
            summary.append((name, score, len(fails), len(high_fails), len(placement) - len(bad_placement)))
            print("\n%s  score %.0f%%  placement %d/%d  ✗ %d (high %d)" % (
                name, score, len(placement) - len(bad_placement), len(placement), len(fails), len(high_fails)))
            for key, value in decisions.items():
                print("    %-11s %s" % (key, value))
            shown = [r for r in rows if args.all_rows or r.get("status") != check_3mf.PASS]
            if shown:
                print("    %s %-30s %-26s %s" % (" ", "key", "tools", "target expects"))
            for r in shown:
                print("    %s %-30s %-26s %s  [%s]" % (r.get("status"), r.get("id"), str(r.get("actual"))[:26],
                                                       _expect_text(r), r.get("confidence")))
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    print("\n%-24s %6s %4s %6s" % ("summary", "score", "✗", "✗high"))
    for name, score, fails, high, _ in summary:
        if score is None:
            print("%-24s ERROR" % name)
        else:
            print("%-24s %5.0f%% %4d %6d" % (name, score, fails, high))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
