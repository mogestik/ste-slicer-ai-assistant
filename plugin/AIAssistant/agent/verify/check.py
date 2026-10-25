"""Самопроверка сохранённых результатов: что реально лежит в проекте 3MF и G-code и что заявил агент.

`verify(path, expected=None, gcode_path=None)` возвращает компактный словарь для JSON:
режим, качество, материал, ключевые действующие настройки, объекты (размер, положение
относительно центра стола = оси вращения 5D, ориентация), совпадения и расхождения с
`expected`, сводку G-code и его согласованность с 3MF, а ещё `errors` (надо исправить)
и `warnings`.
"""

import os
from collections import OrderedDict

from . import gcode as gcode_mod
from .project3mf import load_project, nearest_axis_name, values_equal

FIVE_D_MODES = ("cylindrical", "cylindrical_full")
MODE_TITLES = {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D Full"}

# H530 в 3D: machine_disallowed_areas оставляют свободным X сцены в [-169.5; 150.5] (см. devtools/check_3mf.py).
H530_FREE_X = (-169.5, 150.5)
BOTTOM_TOL = 0.05     # mm
AXIS_TOL = 0.5        # мм, 5D: насколько центр габаритов может отстоять от оси вращения
GCODE_BBOX_TOL = 0.25  # габариты в заголовке округлены до 0.1 мм
GCODE_AUTO_WINDOW_S = 1800.0

COMMON_KEYS = ("layer_height", "wall_line_count", "top_layers", "bottom_layers", "infill_sparse_density",
               "infill_pattern", "material_print_temperature", "speed_print", "support_enable", "adhesion_type")
SUPPORT_KEYS = ("support_type", "support_extruder_nr", "support_angle")
CLASSIC_KEYS = ("material_bed_temperature",)
FIVE_D_KEYS = ("cylindrical_layer_height", "cylindrical_mode_base_diameter", "non_printing_base_diameter",
               "cylindrical_raft_type")
_SETTING_TOP_LEVEL = ("printing_mode", "material_id", "material", "quality_type", "quality", "settings")


def _num(v):
    if isinstance(v, float):
        return int(v) if v.is_integer() else round(v, 4)
    return v


class _Report(object):
    def __init__(self):
        self.errors = []
        self.warnings = []


def _value(project, key):
    """Действующее значение ключа: для настроек экструдера — экструдер 0, иначе глобальное. (ok, значение, result)."""
    r = project.resolver
    pos = "0" if ("0" in project.extruders and r.is_per_extruder(key)) else None
    res = r.resolve(key, pos)
    return res.get("ok", False), res.get("value"), res


def _changed_keys(project):
    keys = []
    stacks = [project.global_stack] + [project.extruders[p] for p in sorted(project.extruders)]
    for stack in stacks:
        if stack is None:
            continue
        for slot in ("user", "quality_changes"):
            c = project.slot(stack, slot)[1]
            for key in sorted(c.values) if c is not None else ():
                if key not in keys:
                    keys.append(key)
    return keys


def _material(project, pos):
    stack = project.extruders.get(pos)
    if stack is None:
        return None
    cid, m = project.slot(stack, "material")
    info = OrderedDict([("id", (m.metadata.get("base_file") if m else None) or cid)])
    if m is not None:
        info["name"] = m.name
    return info


def _matches_material(project, expected):
    stack = project.extruders.get("0")
    cid, m = project.slot(stack, "material") if stack is not None else (None, None)
    wanted = str(expected).strip().lower()
    candidates = [cid]
    if m is not None:
        candidates += [m.id, m.name, m.metadata.get("GUID"), m.metadata.get("base_file")]
    return any(c and str(c).strip().lower() == wanted for c in candidates)


def _quality(project):
    gs = project.global_stack
    q_id, q = project.slot(gs, "quality")
    qc_id, qc = project.slot(gs, "quality_changes")
    qtype = (q.metadata.get("quality_type") if q else None) or (qc.metadata.get("quality_type") if qc else None)
    if qtype is None and q_id and not q_id.startswith("empty"):
        qtype = q_id
    info = OrderedDict([("type", qtype)])
    if q is not None and q.name:
        info["name"] = q.name
    if qc is not None:
        info["custom_profile"] = qc.name
    return info


def _objects(project, mode, report):
    w, d = project.plate_size()
    five_d = mode in FIVE_D_MODES
    height = None
    ok, value, _res = _value(project, "machine_height")
    if ok and isinstance(value, (int, float)):
        height = float(value)
    else:
        height = 300.0 if mode == "classic" else 231.1
    result = []
    for obj in project.objects:
        bbox = obj.world_bbox()
        info = OrderedDict([("name", obj.name)])
        if bbox is None:
            report.errors.append("Объект «%s» без сетки." % obj.name)
            result.append(info)
            continue
        mn, mx = bbox
        size = [mx[i] - mn[i] for i in range(3)]
        cx, cy = (mn[0] + mx[0]) / 2.0 - w / 2.0, (mn[1] + mx[1]) / 2.0 - d / 2.0
        rot = obj.rotation_vs_source()
        up_vec = [rot["rotation"][2][0], rot["rotation"][2][1], rot["rotation"][2][2]]
        up_name, up_dev = nearest_axis_name(up_vec)
        info["size_mm"] = [round(x, 2) for x in size]
        info["center_mm"] = [round(cx, 2) + 0.0, round(cy, 2) + 0.0]
        info["bottom_z"] = round(mn[2], 3) + 0.0
        info["up_axis_of_source"] = up_name if up_dev < 1.0 else "%s (%.0f°)" % (up_name, up_dev)
        info["rotation_deg"] = rot["euler_xyz_deg"]
        if any(abs(s - 1.0) > 1e-3 for s in rot["scale"]):
            info["scale"] = rot["scale"]
        if rot["mirrored"]:
            report.warnings.append("Объект «%s» отзеркален." % obj.name)
        if obj.settings:
            info["per_object"] = dict(obj.settings)
        if abs(mn[2]) > BOTTOM_TOL:
            report.warnings.append("Объект «%s» не на столе: низ на Z=%.2f мм." % (obj.name, mn[2]))
        if five_d:
            radius = max(((v[0] - w / 2.0) ** 2 + (v[1] - d / 2.0) ** 2) ** 0.5 for v in obj.world_verts)
            info["max_radius_mm"] = round(radius, 2)
            offset = (cx ** 2 + cy ** 2) ** 0.5
            if offset > AXIS_TOL:
                report.warnings.append("5D: центр габаритов «%s» смещён от оси вращения на %.2f мм (X %+.2f, "
                                       "Y %+.2f) — ось детали должна быть на оси X=0, Y=0 (у несимметричной детали центр "
                                       "габаритов может не совпадать с осью — сверь с suggest_orientation)."
                                       % (obj.name, offset, cx, cy))
            ratio = obj.ellipse_ratio(w, d)
            if ratio is not None and ratio > 1.0 + 1e-6:
                report.errors.append("Объект «%s» выходит за рабочую область 5D (эллипс %.0f×%.0f мм)."
                                     % (obj.name, w, d))
        else:
            xlo, xhi = (w / 2.0 + H530_FREE_X[0], w / 2.0 + H530_FREE_X[1]) if _is_h530(project) else (0.0, w)
            if mn[0] < xlo - 1e-3 or mx[0] > xhi + 1e-3 or mn[1] < -1e-3 or mx[1] > d + 1e-3:
                report.errors.append("Объект «%s» выходит за рабочую область стола (X %.1f..%.1f, Y %.1f..%.1f "
                                     "от центра)." % (obj.name, mn[0] - w / 2, mx[0] - w / 2, mn[1] - d / 2,
                                                      mx[1] - d / 2))
        if mx[2] > height + 1e-3:
            report.errors.append("Объект «%s» выше рабочей области: %.1f > %.1f мм." % (obj.name, mx[2], height))
        result.append(info)
    if not project.objects:
        report.errors.append("В проекте нет моделей.")
    elif five_d and len(project.objects) > 1:
        report.warnings.append("5D: в проекте %d объектов, на оси вращения печатается одна деталь."
                               % len(project.objects))
    return result


def _is_h530(project):
    return "hte530" in (project.machine_definition() or "").lower()


def _settings(project, mode, keys_extra):
    keys = list(COMMON_KEYS)
    ok, support, _ = _value(project, "support_enable")
    if ok and support:
        keys += SUPPORT_KEYS
    keys += FIVE_D_KEYS if mode in FIVE_D_MODES else CLASSIC_KEYS
    ok, adhesion, _ = _value(project, "adhesion_type")
    if ok and adhesion == "brim":
        keys.append("brim_width")
    for key in keys_extra:
        if key not in keys and key != "printing_mode":
            keys.append(key)
    values = OrderedDict()
    unresolved = []
    for key in keys:
        ok, value, res = _value(project, key)
        if ok:
            values[key] = _num(value)
        elif res.get("note") != "unknown setting":
            values[key] = "?"
            unresolved.append(key)
    return values, unresolved


def _check_expected(project, expected, mode, quality, report):
    if not expected:
        return None
    expected = dict(expected)
    settings = dict(expected.pop("settings", None) or {})
    for key in list(expected):  # нестрого: ключи настроек можно передать и на верхнем уровне
        if key not in _SETTING_TOP_LEVEL:
            settings[key] = expected.pop(key)
    match, mismatch = [], []

    def compare(name, want, actual, ok):
        if ok:
            match.append(name)
        else:
            mismatch.append(OrderedDict([("key", name), ("expected", want), ("actual", actual)]))

    if expected.get("printing_mode") is not None:
        want = expected["printing_mode"]
        compare("printing_mode", want, mode, values_equal(mode, want))
    material = expected.get("material_id", expected.get("material"))
    if material is not None:
        actual = (_material(project, "0") or {}).get("id")
        compare("material_id", material, actual, _matches_material(project, material))
    quality_type = expected.get("quality_type", expected.get("quality"))
    if quality_type is not None:
        compare("quality_type", quality_type, quality.get("type"), values_equal(quality.get("type"), quality_type))
    for key, want in settings.items():
        ok, value, res = _value(project, key)
        if not ok:
            if res.get("note") == "unknown setting":
                mismatch.append(OrderedDict([("key", key), ("expected", want), ("actual", None),
                                             ("note", "нет такой настройки")]))
            else:
                mismatch.append(OrderedDict([("key", key), ("expected", want), ("actual", "?"),
                                             ("note", "значение не вычислено")]))
            continue
        overrides = [(o.name, o.settings[key]) for o in project.objects if key in o.settings]
        bad_override = [(n, v) for n, v in overrides if not values_equal(v, want)]
        if values_equal(value, want) and not bad_override:
            match.append(key)
            continue
        row = OrderedDict([("key", key), ("expected", want), ("actual", _num(value))])
        if bad_override:
            row["note"] = "переопределено для объекта: " + ", ".join("%s=%s" % x for x in bad_override)
        mismatch.append(row)
    for row in mismatch:
        report.errors.append("Расхождение с заявленным: %s = %s, а в файле %s." % (row["key"], row["expected"],
                                                                                  row["actual"]))
    return OrderedDict([("match", match), ("mismatch", mismatch)])


def _check_gcode(path, project, mode, report):
    w, d = project.plate_size() if project is not None else (351.0, 300.3)
    st = gcode_mod.parse_file(path, w / 2.0, d / 2.0)
    info = OrderedDict([("file", path)])
    g_mode = gcode_mod.header_mode(st)
    info["mode"] = g_mode
    t = gcode_mod.header_float(st, "TIME")
    if t is None:
        t = gcode_mod.header_float(st, "PRINT.TIME")
    if t is not None:
        info["time_s"] = int(t)
    fil = gcode_mod.header_float(st, "Filament used")
    if fil is not None:
        info["filament_m"] = fil
    lh = gcode_mod.header_float(st, "Layer height")
    if lh is not None:
        info["layer_height"] = lh
    info["layers"] = st.layers
    hot = sorted(set(_num(s) for s, tool in st.hotend if s > 0 and tool == 0))
    bed = sorted(set(_num(s) for s in st.bed if s > 0))
    info["hotend_c"] = hot
    if bed:
        info["bed_c"] = bed
    if st.ranges:
        info["bounds"] = ", ".join("%s %.1f..%.1f" % (ax, st.ranges[ax][0], st.ranges[ax][1])
                                   for ax in "XYZAC" if ax in st.ranges)
    if st.coord_systems:
        info["coord_systems"] = sorted(st.coord_systems)

    errors, warnings = [], []
    if st.moves == 0 or st.extrusions == 0:
        errors.append("в файле нет движений с экструзией")
    if st.bad_numbers:
        errors.append("%d строк с nan/inf" % st.bad_numbers)
    if st.huge:
        errors.append("%d движений с координатами > %d мм" % (st.huge, gcode_mod.HUGE))
    if g_mode is None:
        warnings.append("нет ;PRINTING_MODE (файл не из GCodeWriter STE Slicer?)")
    eff_mode = g_mode or mode
    five_d = eff_mode in FIVE_D_MODES
    if five_d:
        if not (st.a_values or "A" in st.axes) or "C" not in st.axes:
            warnings.append("5D без движений по A/C")
        if st.ellipse_ratio > 1.0 + 1e-6:
            warnings.append("XY выходят за эллипс 5D (эвристика)")
    else:
        if "C" in st.axes or any(abs(a) > 0.01 for a in st.a_values):
            warnings.append("в 3D есть движения по A/C")
        if st.ranges:
            xlo, xhi = w / 2.0 + H530_FREE_X[0], w / 2.0 + H530_FREE_X[1]
            x, y = st.ranges.get("X", (xlo, xlo)), st.ranges.get("Y", (0.0, 0.0))
            z = st.ranges.get("Z", (0.0, 0.0))
            if x[0] < xlo - 0.5 or x[1] > xhi + 0.5 or y[0] < -0.5 or y[1] > d + 0.5 or z[0] < -0.5:
                errors.append("координаты вне рабочей области стола")
    if not hot:
        warnings.append("нет команд нагрева сопла M104/M109")

    stale = []
    if project is not None:
        r = project.resolver
        if g_mode and mode and g_mode != mode:
            stale.append("режим %s, в 3MF %s" % (g_mode, mode))
        if hot:
            cand = []
            for key in ("material_print_temperature", "material_print_temperature_layer_0",
                        "material_initial_print_temperature"):
                res = r.resolve(key, "0")
                if res.get("ok") and isinstance(res.get("value"), (int, float)):
                    cand.append(float(res["value"]))
            bad = [s for s in hot if cand and not any(abs(s - c) <= 0.5 for c in cand)]
            if bad:
                stale.append("температура сопла %s, в 3MF %s" % (bad, sorted(set(_num(c) for c in cand))))
        if not five_d and bed:
            res = r.resolve("material_bed_temperature")
            if res.get("ok") and any(abs(s - float(res["value"])) > 0.5 for s in bed):
                stale.append("температура стола %s, в 3MF %s" % (bed, _num(res["value"])))
        if lh is not None and not five_d:
            res = r.resolve("layer_height")
            if res.get("ok") and abs(lh - float(res["value"])) > 1e-3:
                stale.append("высота слоя %s, в 3MF %s" % (lh, _num(res["value"])))
        stale += _compare_settings_block(st, project)
        stale += _compare_bbox(st, project, w, d)
    if stale:
        info["matches_3mf"] = False
        errors.append("не соответствует 3MF (G-code устарел или от другого проекта — перенарежь и сохрани "
                      "заново): " + "; ".join(stale))
    elif project is not None:
        info["matches_3mf"] = True
    report.errors.extend("G-code: " + e for e in errors)
    report.warnings.extend("G-code: " + w_ for w_ in warnings)
    return info


def _compare_settings_block(st, project):
    block = gcode_mod.settings_block(st)
    if block is None:
        return []
    if "error" in block:
        return ["блок ;SETTING_3 не читается: %s" % block["error"]]
    diffs = []
    stacks = [("", project.global_stack, block["global"])]
    for i, values in enumerate(block["extruders"]):
        stacks.append(("E%d " % i, project.extruders.get(str(i)), values))
    for prefix, stack, g_values in stacks:
        if stack is None:
            continue
        want = {}
        for slot in ("quality_changes", "user"):  # сплющиваем: user поверх quality_changes
            c = project.slot(stack, slot)[1]
            if c is not None:
                want.update(c.values)
        for key in sorted(set(want) | set(g_values)):
            a, b = g_values.get(key), want.get(key)
            if a is None or b is None or str(a).strip() != str(b).strip():
                diffs.append("%s%s: G-code %s, 3MF %s" % (prefix, key, a if a is not None else "—",
                                                         b if b is not None else "—"))
    if diffs:
        return ["настройки: " + ", ".join(diffs[:6]) + (" …" if len(diffs) > 6 else "")]
    return []


def _compare_bbox(st, project, w, d):
    hb = gcode_mod.header_bbox_printer(st)
    if hb is None or not project.objects:
        return []
    boxes = [o.world_bbox() for o in project.objects if o.world_bbox()]
    if not boxes:
        return []
    mn = [min(b[0][i] for b in boxes) for i in range(3)]
    mx = [max(b[1][i] for b in boxes) for i in range(3)]
    mn = [mn[0] - w / 2.0, mn[1] - d / 2.0, mn[2]]
    mx = [mx[0] - w / 2.0, mx[1] - d / 2.0, mx[2]]
    # габариты сцены в G-code могут быть больше (кайма, подложка), но модели должны в них помещаться
    inside = all(hb[0][i] <= mn[i] + GCODE_BBOX_TOL and hb[1][i] >= mx[i] - GCODE_BBOX_TOL for i in range(3))
    if inside:
        return []
    return ["габариты сцены в G-code X %.1f..%.1f Y %.1f..%.1f Z %.1f..%.1f не содержат модели из 3MF "
            "(X %.1f..%.1f Y %.1f..%.1f Z %.1f..%.1f)" % (hb[0][0], hb[1][0], hb[0][1], hb[1][1], hb[0][2], hb[1][2],
                                                           mn[0], mx[0], mn[1], mx[1], mn[2], mx[2])]


def verify(path, expected=None, gcode_path=None, catalog_settings=None):
    """Полная самопроверка; ProjectError, если 3MF не читается."""
    report = _Report()
    project = load_project(path, catalog_settings)
    out = OrderedDict([("file", path)])
    if project.global_stack is None:
        report.errors.append("Это не проект STE Slicer (нет стека настроек) — сохраняй через save_project.")
        out["objects"] = _objects(project, None, report) if project.objects else []
        out["errors"] = report.errors + project.warnings
        out["ok"] = False
        return out
    report.warnings.extend(project.warnings)
    if project.definitions_source == "catalog":
        report.warnings.append("В 3MF нет определений машины (*.def.json): значения по формулам посчитаны по "
                               "каталогу плагина — приблизительно.")
    elif project.definitions_source == "none":
        report.warnings.append("В 3MF нет определений машины: значения по формулам не вычислены.")
    ok, mode, _res = _value(project, "printing_mode")
    mode = mode if ok else None
    out["machine"] = project.global_stack.name or project.machine_definition()
    out["printing_mode"] = mode
    out["mode_title"] = MODE_TITLES.get(mode, "?")
    quality = _quality(project)
    out["quality"] = quality
    out["material"] = _material(project, "0")
    changed = _changed_keys(project)
    extra = list(changed)
    for key in ((expected or {}).get("settings") or {}):
        if key not in extra:
            extra.append(key)
    settings, unresolved = _settings(project, mode, extra)
    ok, support, _ = _value(project, "support_enable")
    ok_nr, support_nr, _ = _value(project, "support_extruder_nr")
    if ok and support and ok_nr and str(support_nr) == "1":
        out["material_support"] = _material(project, "1")
    out["settings"] = settings
    out["changed_keys"] = changed
    if unresolved:
        report.warnings.append("Не вычислены: %s." % ", ".join(unresolved))
    for row in project.resolver.shadowed_user_values():
        report.warnings.append("%s = %s задано глобально, но не действует: экструдер %s берёт %s из %s."
                               % (row["key"], row["global_user"], row["extruder"], row["value"], row["winner"]))
    out["objects"] = _objects(project, mode, report)
    expected_result = _check_expected(project, expected, mode, quality, report)
    if expected_result is not None:
        out["expected"] = expected_result
    if gcode_path:
        if os.path.isfile(gcode_path):
            out["gcode"] = _check_gcode(gcode_path, project, mode, report)
        else:
            report.errors.append("G-code не найден: %s" % gcode_path)
    out["errors"] = report.errors
    out["warnings"] = report.warnings
    out["ok"] = not report.errors
    return out


def verify_gcode(gcode_path, project_path=None, catalog_settings=None):
    """Проверка только G-code (при желании — против проекта)."""
    report = _Report()
    project = load_project(project_path, catalog_settings) if project_path else None
    mode = None
    if project is not None and project.global_stack is not None:
        ok, mode, _res = _value(project, "printing_mode")
        mode = mode if ok else None
    out = _check_gcode(gcode_path, project, mode, report)
    out["errors"] = report.errors
    out["warnings"] = report.warnings
    out["ok"] = not report.errors
    return out


def newest_file(directory, extension):
    """Самый свежий файл с расширением `extension` в `directory` (или None)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    paths = [os.path.join(directory, n) for n in names if n.lower().endswith(extension)]
    paths = [p for p in paths if os.path.isfile(p)]
    return max(paths, key=os.path.getmtime) if paths else None


def companion_gcode(project_path, directory):
    """Самый свежий G-code в `directory`, записанный не дальше GCODE_AUTO_WINDOW_S от проекта (или None)."""
    candidate = newest_file(directory, ".gcode")
    if candidate is None:
        return None
    if abs(os.path.getmtime(candidate) - os.path.getmtime(project_path)) > GCODE_AUTO_WINDOW_S:
        return None
    return candidate
