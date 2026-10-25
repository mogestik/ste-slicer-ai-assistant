#!/usr/bin/env python3
"""Проверка G-code, который выгрузил STE Slicer (3D — CuraEngine, 5D — CliParser), на здравый смысл.

    python3 check_gcode.py result.gcode
    python3 check_gcode.py result.gcode --project project.3mf      # + согласованность с 3MF
    python3 check_gcode.py result.gcode --json

Что пишет STE Slicer (plugins/GCodeWriter/GCodeWriter.py и бэкенды):
  * заголовок от GCodeWriter: ;PREVIEW:<png в base64> (не всегда), ;VERSION:7.10.12,
    ;PRINTING_MODE:[classic|cylindrical|cylindrical_full], ;MINX..;MAXZ (габариты сцены, а сцена Y-up!)
  * 3D (CuraEngine, flavor RepRap): ;FLAVOR:RepRap ;TIME:<с> ;Filament used: <м> ;Layer height: ;LAYER_COUNT: ;LAYER:n,
    стартовый код steprinter_v5: ";STE Slicer 5D Gcode / START / ... / {coordinate_system}" с G54 (+G4 P100)
  * 5D: G55 (сердечник 5D Full) / G56 (5D Spiral, основание), "G0 A90" (cylindrical_rotate), движения со
    словами X Y Z A B C, C записано как C/3 (machine_c_axis_divider = 3, steslicer/Utils/CliParser.py)
  * в конце: {prefix_end_gcode} END, затем ;SETTING_3 {json} (user + quality_changes), если что-то меняли.
Статус: ✓ норма, ~ предупреждение или не проверить, ✗ ошибка.
"""

import argparse
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, PARTIAL, FAIL = "✓", "~", "✗"
SCORE = {PASS: 1.0, PARTIAL: 0.5, FAIL: 0.0}

H530 = {"width": 351.0, "depth": 300.3, "height_3d": 300.0, "height_5d": 231.1,
        "free_x_scene": (-169.5, 150.5)}
HUGE = 1000.0
WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:nan|inf|infinity))",
                     re.IGNORECASE)
_ESC = re.compile(r"\\\\|\\n|\\r")


def unescape_setting(s):
    return _ESC.sub(lambda m: {"\\\\": "\\", "\\n": "\n", "\\r": "\r"}[m.group(0)], s)


class GcodeStats(object):
    def __init__(self):
        self.lines = 0
        self.header = {}
        self.setting_lines = []
        self.layers = 0
        self.layer_count_decl = None
        self.moves = 0
        self.extrusions = 0
        self.bad_numbers = []
        self.huge = []
        self.ranges = {}
        self.axes_seen = set()
        self.a_values = set()
        self.coord_systems = []
        self.hotend_temps = []  # (cmd, S, T)
        self.bed_temps = []
        self.fan = []
        self.max_e = 0.0
        self.relative_moves = 0
        self.tool_changes = set()
        self.has_start = False
        self.has_end = False
        self.print_moves_xy = []


def parse(text):
    st = GcodeStats()
    in_print = False
    relative = False
    tool = 0
    for raw in text.splitlines():
        st.lines += 1
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            body = line[1:]
            if body.startswith("SETTING_"):
                st.setting_lines.append(body)
                continue
            m = re.match(r"(PREVIEW|VERSION|PRINTING_MODE|MINX|MINY|MINZ|MAXX|MAXY|MAXZ|FLAVOR|TIME|"
                         r"Filament used|Layer height|LAYER_COUNT|Generated with|PRINT\.TIME|TIME_ELAPSED)"
                         r"\s*:?\s*(.*)$", body)
            if m:
                key = m.group(1)
                if key == "TIME_ELAPSED":
                    st.header["TIME_ELAPSED_last"] = m.group(2)
                elif key == "LAYER_COUNT":
                    st.layer_count_decl = m.group(2) or None
                elif key not in st.header:
                    st.header[key] = m.group(2) if key != "PREVIEW" else "<%d chars>" % len(m.group(2))
                continue
            if body.startswith("LAYER:"):
                st.layers += 1
                in_print = True
                continue
            if body.strip() == "prefix_end_gcode":
                in_print = False
                st.has_end = True
            continue
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        up = code.upper()
        if up == "START":
            st.has_start = True
            continue
        if up == "END":
            st.has_end = True
            in_print = False
            continue
        cmd = up.split()[0]
        if re.match(r"^T\d+$", cmd):
            tool = int(cmd[1:])
            st.tool_changes.add(tool)
            continue
        words = {}
        for k, v in WORD_RE.findall(code[len(cmd):]):
            k = k.upper()
            try:
                f = float(v)
            except ValueError:
                f = float("nan")
            if math.isnan(f) or math.isinf(f):
                st.bad_numbers.append((st.lines, raw[:80]))
                continue
            words[k] = f
        if (not st.bad_numbers or st.bad_numbers[-1][0] != st.lines) and \
                re.search(r"\bnan\b|\binf\b", code, re.IGNORECASE):
            st.bad_numbers.append((st.lines, raw[:80]))
        if cmd in ("G90",):
            relative = False
        elif cmd == "G91":
            relative = True
        elif cmd in ("G54", "G55", "G56", "G57", "G58", "G59"):
            st.coord_systems.append(cmd)
        elif cmd in ("M104", "M109"):
            if "S" in words:
                st.hotend_temps.append((cmd, words["S"], int(words.get("T", tool))))
        elif cmd in ("M140", "M190"):
            if "S" in words:
                st.bed_temps.append((cmd, words["S"]))
        elif cmd in ("M106", "M107"):
            st.fan.append((cmd, words.get("S", 0.0)))
        elif cmd in ("G0", "G1", "G2", "G3", "G00", "G01"):
            st.moves += 1
            for ax in "XYZABC":
                if ax in words:
                    st.axes_seen.add(ax)
                    if ax not in "ABC" and abs(words[ax]) > HUGE:  # поворотные оси — накопленные градусы
                        st.huge.append((st.lines, raw[:80]))
            if "A" in words:
                st.a_values.add(round(words["A"], 2))
            if relative:
                st.relative_moves += 1
                continue
            if "E" in words and cmd in ("G1", "G01") and in_print:
                st.extrusions += 1
                st.max_e = max(st.max_e, words["E"])
            if in_print:
                for ax in "XYZABC":
                    if ax in words:
                        lo, hi = st.ranges.get(ax, (words[ax], words[ax]))
                        st.ranges[ax] = (min(lo, words[ax]), max(hi, words[ax]))
                if "X" in words or "Y" in words:
                    st.print_moves_xy.append((words.get("X"), words.get("Y")))
    return st


def parse_settings_block(st):
    if not st.setting_lines:
        return None
    payload = ""
    for body in st.setting_lines:
        m = re.match(r"SETTING_(\d+) (.*)$", body)
        if m:
            payload += m.group(2)
    try:
        data = json.loads(unescape_setting(payload))
    except ValueError as e:
        return {"error": str(e)}
    import configparser
    res = {"global": {}, "extruders": []}
    for key, target in (("global_quality", None),):
        if key in data:
            cp = configparser.ConfigParser(interpolation=None, comment_prefixes=())
            cp.read_string(data[key])
            res["global"] = dict(cp["values"]) if cp.has_section("values") else {}
            res["global_meta"] = dict(cp["metadata"]) if cp.has_section("metadata") else {}
    for ex in data.get("extruder_quality", []):
        cp = configparser.ConfigParser(interpolation=None, comment_prefixes=())
        cp.read_string(ex)
        res["extruders"].append(dict(cp["values"]) if cp.has_section("values") else {})
    return res


def _row(cid, status, actual, expected="", weight=1.0, note=""):
    return {"id": cid, "status": status, "actual": actual, "expected": expected, "weight": weight, "note": note}


def check(text, project=None, mode_override=None):
    rows = []
    if not text.strip():
        return [_row("not_empty", FAIL, "empty file", "G-code", 5)], None
    st = parse(text)
    rows.append(_row("not_empty", PASS if st.moves > 0 else FAIL, "%d lines, %d moves" % (st.lines, st.moves),
                     "> 0 moves", 5))
    h = st.header
    # mode
    mode_hdr = None
    if "PRINTING_MODE" in h:
        mode_hdr = h["PRINTING_MODE"].strip().strip("[]")
    proj_mode = None
    if project is not None:
        r = project.resolver.resolve("printing_mode")
        proj_mode = r.get("value") if r.get("ok") else None
    mode = mode_override or mode_hdr or proj_mode
    rows.append(_row("header", PASS if ("VERSION" in h and "PRINTING_MODE" in h) else
                     (PARTIAL if ("FLAVOR" in h or "VERSION" in h) else FAIL),
                     ", ".join("%s" % k for k in h if k != "PREVIEW") or "none",
                     ";VERSION + ;PRINTING_MODE (GCodeWriter)", 2,
                     "no ;PRINTING_MODE: file not written by STE Slicer GCodeWriter?" if "PRINTING_MODE" not in h else ""))
    if proj_mode is not None:
        rows.append(_row("mode_vs_3mf", PASS if mode_hdr == proj_mode else (PARTIAL if mode_hdr is None else FAIL),
                         mode_hdr, proj_mode, 4))
    is5d = mode in ("cylindrical", "cylindrical_full", "spherical", "spherical_full", "conical", "conical_full")
    # время и материал
    t = h.get("TIME") or h.get("PRINT.TIME")
    try:
        tv = float(t) if t is not None else None
    except ValueError:
        tv = None
    if tv is not None:
        rows.append(_row("print_time", PASS if tv > 0 else FAIL, "%s s (%.1f h)" % (t, tv / 3600), "> 0", 1))
    else:
        rows.append(_row("print_time", PARTIAL if is5d else FAIL, "missing", ";TIME:", 1,
                         "5D G-code from CliParser may carry no ;TIME header" if is5d else ""))
    fu = h.get("Filament used")
    if fu:
        nums = re.findall(r"[-+]?\d*\.?\d+", fu)
        ok = bool(nums) and all(float(x) >= 0 for x in nums) and any(float(x) > 0 for x in nums)
        rows.append(_row("filament_used", PASS if ok else FAIL, fu, "> 0 m", 1))
    else:
        rows.append(_row("filament_used", PARTIAL, "missing", ";Filament used:", 1,
                         "computed from E instead: max E %.1f mm" % st.max_e))
    # слои и экструзия
    rows.append(_row("layers", PASS if st.layers > 0 else FAIL, "%d ;LAYER: markers" % st.layers, "> 0", 2))
    rows.append(_row("extrusion", PASS if st.extrusions > 0 and st.max_e > 0 else FAIL,
                     "%d extruding moves, max E %.2f" % (st.extrusions, st.max_e), "> 0", 3))
    # numbers
    rows.append(_row("no_nan_inf", PASS if not st.bad_numbers else FAIL,
                     "%d bad" % len(st.bad_numbers), "0", 4,
                     "; ".join("line %d: %s" % x for x in st.bad_numbers[:3])))
    rows.append(_row("no_huge_coords", PASS if not st.huge else FAIL, "%d > %g mm" % (len(st.huge), HUGE), "0", 4,
                     "; ".join("line %d: %s" % x for x in st.huge[:3])))
    # рабочая область
    W, D = H530["width"], H530["depth"]
    if project is not None:
        W, D = project.plate_size()
    rng = st.ranges
    rtxt = ", ".join("%s %.1f..%.1f" % (ax, rng[ax][0], rng[ax][1]) for ax in "XYZABC" if ax in rng)
    if not rng:
        rows.append(_row("work_area", PARTIAL, "no absolute moves after ;LAYER:", "", 3))
    elif not is5d:
        xlo, xhi = W / 2 + H530["free_x_scene"][0], W / 2 + H530["free_x_scene"][1]
        x, y, z = rng.get("X", (xlo, xlo)), rng.get("Y", (0, 0)), rng.get("Z", (0, 0))
        hard = x[0] >= -0.5 and x[1] <= W + 0.5 and y[0] >= -0.5 and y[1] <= D + 0.5 and z[0] >= -0.5 \
            and z[1] <= H530["height_3d"] + 0.5
        soft = x[0] >= xlo - 0.5 and x[1] <= xhi + 0.5
        rows.append(_row("work_area", PASS if hard and soft else (PARTIAL if hard else FAIL), rtxt,
                         "X %.1f..%.1f (disallowed strips excluded), Y 0..%.1f, Z 0..%.0f" % (
                             xlo, xhi, D, H530["height_3d"]), 3,
                         "" if soft else "inside the plate but in the disallowed X strips (or extruder offset)"))
    else:
        a, b = W / 2, D / 2
        er = 0.0
        for xv, yv in st.print_moves_xy:
            if xv is not None and yv is not None:
                er = max(er, (xv / a) ** 2 + (yv / b) ** 2)
        z = rng.get("Z", (0, 0))
        ok = er <= 1.0 + 1e-6 and z[1] <= max(H530["height_5d"], 300.0) and z[0] >= -1.0
        rows.append(_row("work_area", PASS if ok else PARTIAL, rtxt + ", ellipse ratio %.3f" % er,
                         "|X|≤%.1f, |Y|≤%.1f ellipse, Z ≤ %.1f" % (a, b, H530["height_5d"]), 2,
                         "5D coordinates are relative to G55/G56 (tool/mandrel); bounds are a heuristic"))
    # системы координат и поворотные оси
    cs = sorted(set(st.coord_systems))
    if is5d:
        has_a = bool(st.a_values) or "A" in st.axes_seen
        has_c = "C" in st.axes_seen
        rows.append(_row("rotary_axes", PASS if has_a and has_c else (PARTIAL if has_a or has_c else FAIL),
                         "A values %s, C used: %s" % (sorted(st.a_values)[:5], has_c),
                         "A90 (cylindrical_rotate) and C moves", 4))
        want = {"cylindrical": {"G56"}, "cylindrical_full": {"G55", "G56"}}.get(mode, {"G55"})
        rows.append(_row("coordinate_system", PASS if want & set(cs) else FAIL, ",".join(cs) or "none",
                         "/".join(sorted(want)), 2, "G55 core of 5D Full, G56 5D Spiral / basement (РЭ с.20)"))
    else:
        rows.append(_row("coordinate_system", PASS if "G54" in cs else PARTIAL, ",".join(cs) or "none", "G54", 1,
                         "H530 3D uses G54 + G4 P100"))
        rot = "C" in st.axes_seen or any(abs(v) > 0.01 for v in st.a_values)
        rows.append(_row("no_rotary_in_3d", PARTIAL if rot else PASS, "A %s, C %s" % (
            sorted(st.a_values)[:5], "C" in st.axes_seen), "no A/C motion", 1))
    # temperatures
    hot = [s for c, s, tl in st.hotend_temps if s > 0 and tl == 0]
    if project is not None:
        r = project.resolver
        cand = []
        for k in ("material_print_temperature", "material_print_temperature_layer_0",
                  "material_initial_print_temperature"):
            v = r.resolve(k, "0")
            if v.get("ok"):
                cand.append(float(v["value"]))
        if hot and cand:
            bad = [s for s in hot if not any(abs(s - c) <= 0.5 for c in cand)]
            rows.append(_row("hotend_temp_vs_3mf", PASS if not bad else FAIL, sorted(set(hot)),
                             "∈ %s" % sorted(set(cand)), 3, "unexpected: %s" % sorted(set(bad)) if bad else ""))
        else:
            rows.append(_row("hotend_temp_vs_3mf", FAIL if not hot else PARTIAL,
                             sorted(set(hot)) or "no M104/M109", sorted(set(cand)), 3))
        bed = [s for c, s in st.bed_temps if s > 0]
        if not is5d:
            v = r.resolve("material_bed_temperature")
            want_bed = float(v["value"]) if v.get("ok") else None
            if want_bed is None or not bed:
                rows.append(_row("bed_temp_vs_3mf", PARTIAL, sorted(set(bed)) or "no M140/M190", want_bed, 2))
            else:
                ok = all(abs(s - want_bed) <= 0.5 for s in bed)
                rows.append(_row("bed_temp_vs_3mf", PASS if ok else FAIL, sorted(set(bed)), want_bed, 2))
        else:
            rows.append(_row("no_bed_heating_5d", PASS if not bed else PARTIAL, sorted(set(bed)) or "none",
                             "none (machine_heated_bed only in classic)", 1))
    else:
        rows.append(_row("hotend_temp", PASS if hot and all(150 <= s <= 350 for s in hot) else FAIL,
                         sorted(set(hot)) or "no M104/M109", "150..350 °C (H530 nozzle ≤350)", 2))
    # встроенные настройки
    sb = parse_settings_block(st)
    if sb is None:
        rows.append(_row("settings_block", PARTIAL, "no ;SETTING_3", "present when the profile was changed", 0.5))
    elif "error" in sb:
        rows.append(_row("settings_block", FAIL, "broken JSON: %s" % sb["error"], "", 1))
    else:
        note = ""
        st_ok = PASS
        if project is not None:
            diffs = []
            gs = project.global_stack
            if gs is not None:
                _, user = project.slot(gs, "user")
                want = dict(user.values) if user else {}
                for k, v in want.items():
                    if k in sb["global"] and str(sb["global"][k]).strip() != str(v).strip():
                        diffs.append("%s: gcode %s vs 3mf %s" % (k, sb["global"][k], v))
                for i, ex_vals in enumerate(sb["extruders"]):
                    ex = project.extruders.get(str(i))
                    if ex is None:
                        continue
                    _, eu = project.slot(ex, "user")
                    for k, v in (eu.values if eu else {}).items():
                        if k in ex_vals and str(ex_vals[k]).strip() != str(v).strip():
                            diffs.append("E%d %s: gcode %s vs 3mf %s" % (i, k, ex_vals[k], v))
            if diffs:
                st_ok = FAIL
                note = "; ".join(diffs[:5])
        pm = sb["global"].get("printing_mode")
        rows.append(_row("settings_block", st_ok, "%d global, %s extruder keys%s" % (
            len(sb["global"]), [len(x) for x in sb["extruders"]], ", printing_mode=%s" % pm if pm else ""),
            "consistent with 3MF user changes" if project is not None else "parsable", 1, note))
    rows.append(_row("start_end", PASS if st.has_start and st.has_end else PARTIAL,
                     "START %s, END %s" % (st.has_start, st.has_end), "START ... END (steprinter_v5)", 0.5))
    return rows, st


def total_score(rows):
    tw = sum(r["weight"] for r in rows) or 1.0
    return 100.0 * sum(r["weight"] * SCORE[r["status"]] for r in rows) / tw


def errors(rows):
    return [r for r in rows if r["status"] == FAIL]


def format_table(rows, title, out=None):
    out = out or sys.stdout
    w = out.write
    w(title + "\n")
    w("-" * 110 + "\n")
    for r in rows:
        w("%s  %-22s %-46s %s\n" % (r["status"], r["id"], str(r["actual"])[:46], str(r["expected"])[:40]))
        if r.get("note"):
            w("     %s\n" % r["note"])
    w("-" * 110 + "\n")
    n = {s: sum(1 for r in rows if r["status"] == s) for s in (PASS, PARTIAL, FAIL)}
    w("%s %d   %s %d   %s %d   score %.1f%%\n" % (PASS, n[PASS], PARTIAL, n[PARTIAL], FAIL, n[FAIL], total_score(rows)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gcode")
    ap.add_argument("--project", help="проект .3mf для проверки согласованности")
    ap.add_argument("--mode", choices=["classic", "cylindrical", "cylindrical_full"],
                    help="режим печати вручную (иначе из ;PRINTING_MODE или из 3MF)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="код выхода 1 при любом ✗")
    a = ap.parse_args(argv)
    with open(a.gcode, encoding="utf-8", errors="replace") as f:
        text = f.read()
    project = None
    if a.project:
        from inspect_3mf import load
        project = load(a.project)
    rows, st = check(text, project, a.mode)
    title = "%s%s" % (os.path.basename(a.gcode), "  +  %s" % os.path.basename(a.project) if a.project else "")
    if a.json:
        json.dump({"title": title, "score": round(total_score(rows), 2), "rows": rows,
                   "header": st.header if st else {}}, sys.stdout, ensure_ascii=False, indent=1, default=str)
        sys.stdout.write("\n")
    else:
        format_table(rows, title)
    if a.strict and errors(rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
