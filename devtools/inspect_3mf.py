#!/usr/bin/env python3
"""Показывает, что внутри проекта 3MF из STE Slicer: принтер, режим 3D/5D, профиль, материал,
настройки, объекты и их положение.

    python3 inspect_3mf.py project.3mf                  # для человека
    python3 inspect_3mf.py project.3mf --json > p.json  # для скриптов
    python3 inspect_3mf.py project.3mf --keys layer_height,infill_sparse_density

Действующие значения считаются с приоритетом стеков STE Slicer (user > quality_changes > quality >
material > variant > definition_changes > definition; сначала стек экструдера, потом глобальный).
Формулы вычисляются по *.def.json из архива; если это невозможно, показываются источник и сама формула.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ste3mf  # noqa: E402


def load(path, resources=None):
    """Загружает проект; если определений в архиве нет, берёт их из файлов слайсера."""
    p = ste3mf.load_project(path)
    if p.global_stack is not None:
        def_id = p.machine_definition()
        if def_id and def_id not in p.containers:
            res_dir = resources or ste3mf.find_resources_dir()
            if res_dir:
                ids = [def_id] + [p.slot(e, "definition")[0] for e in p.extruders.values()]
                fb = ste3mf.load_fallback_definitions(res_dir, [i for i in ids if i])
                p = ste3mf.Project(path, fallback_definitions=fb)
                p.warnings.append("definitions are not in the archive: used %s" % res_dir)
            else:
                p.warnings.append("definitions are not in the archive and slicer sources not found: "
                                  "formula values cannot be computed")
    return p


def fmt_val(r):
    if r is None:
        return "-"
    if r.get("ok"):
        v = r["value"]
        if isinstance(v, float):
            v = ("%.4f" % v).rstrip("0").rstrip(".")
        s = str(v)
    else:
        s = "? (%s)" % (r.get("raw") if r.get("raw") is not None else "n/a")
    src = r.get("source") or ""
    return "%s  [%s]" % (s, src) if src else s


def print_human(s, out=None):
    out = out or sys.stdout
    w = out.write
    w("== %s ==\n" % s["file"])
    if s.get("version"):
        w("STE Slicer version: %s\n" % s["version"].get("steslicer_version"))
    for msg in s["warnings"]:
        w("WARNING: %s\n" % msg)
    m = s.get("machine")
    if not m:
        w("No machine/global stack: not a project 3MF.\n")
    else:
        w("Machine:   %s  (definition %s%s)\n" % (m["name"], m["definition"],
                                                  "" if m["definition_in_archive"] else ", NOT in archive"))
        pm = s["printing_mode"]
        mode_h = {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D (Spiral Full)"}
        w("Mode:      printing_mode = %s (%s)  [%s]\n" % (pm.get("value"), mode_h.get(pm.get("value"), "?"),
                                                         pm.get("source")))
        q = s["quality"]
        w("Quality:   %s (quality_type=%s, id=%s)" % (q["name"], q["quality_type"], q["id"]))
        if q["quality_changes_name"]:
            w("  custom profile: %s (%s)" % (q["quality_changes_name"], q["quality_changes_id"]))
        w("\n")
        for pos, e in s["extruders"].items():
            mt = e["material"]
            w("Extruder %s: %s enabled=%s  material=%s (%s, %s, GUID %s)%s  variant=%s quality=%s\n" % (
                pos, e["stack_id"], e["enabled"], mt["id"], mt["name"], mt["brand"], mt["GUID"],
                "" if mt["in_archive"] else " NOT in archive", e["variant"], e["quality"]))
        w("\n-- Explicit changes --\n")
        ch = s["changes"]
        if not ch["global"] and not any(ch["extruders"].values()):
            w("  (none)\n")
        for slot, data in ch["global"].items():
            w("  global.%s (%s):\n" % (slot, data["id"]))
            for k, v in sorted(data["values"].items()):
                w("    %-40s = %s\n" % (k, v))
        for pos, blk in ch["extruders"].items():
            for slot, data in blk.items():
                w("  extruder%s.%s (%s):\n" % (pos, slot, data["id"]))
                for k, v in sorted(data["values"].items()):
                    w("    %-40s = %s\n" % (k, v))
        w("\n-- Effective values (value [source]) --\n")
        for k, row in s["effective"].items():
            g = row.get("global")
            ex = [(kk, vv) for kk, vv in row.items() if kk.startswith("extruder")]
            if ex:
                parts = "   ".join("%s: %s" % (kk.replace("extruder", "E"), fmt_val(vv)) for kk, vv in ex)
                w("  %-36s %s\n" % (k, parts))
            else:
                w("  %-36s %s\n" % (k, fmt_val(g)))
        if s.get("shadowed"):
            w("\n-- Global user values hidden by extruder containers (no effect!) --\n")
            for x in s["shadowed"]:
                w("  %s: global user %s, but extruder %s uses %s = %s\n" % (
                    x["key"], x["global_user_value"], x["extruder"], x["winning_container"], x["winning_raw"]))
    pl = s["plate"]
    w("\n-- Objects (plate %.1f x %.1f, centre = 3MF (%.2f, %.2f)) --\n" % (
        pl["machine_width"], pl["machine_depth"], pl["plate_center_3mf"][0], pl["plate_center_3mf"][1]))
    if not s["objects"]:
        w("  (no objects)\n")
    for o in s["objects"]:
        w("  #%d %s: %d triangles\n" % (o["index"], o["name"], o["triangles"]))
        if "world_bbox_3mf" in o:
            b = o["world_bbox_3mf"]
            pos = o["position"]
            w("     size X x Y x Z: %.2f x %.2f x %.2f mm\n" % tuple(b["size"]))
            w("     bbox centre rel. plate centre: X %+.2f  Y %+.2f, bottom Z %.2f\n" % (
                pos["center_rel_plate_center_xy"][0], pos["center_rel_plate_center_xy"][1], pos["bottom_z"]))
        r = o["rotation_vs_source_stl"]
        w("     rotation vs source STL: euler XYZ %s deg, axis %s angle %.1f deg%s%s\n" % (
            r["euler_xyz_deg"], r["axis_angle"]["axis"], r["axis_angle"]["angle_deg"],
            "" if r["scale"] == [1.0, 1.0, 1.0] else ", scale %s" % r["scale"],
            ", MIRRORED" if r["mirrored"] else ""))
        up = o["source_axis_pointing_up"]
        w("     source axis pointing up: %s (deviation %.1f deg), vector %s\n" % (
            up["nearest"], up["deviation_deg"], up["vector"]))
        if o["per_object_settings"]:
            w("     per-object settings: %s\n" % o["per_object_settings"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="проект .3mf")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    ap.add_argument("--keys", help="ключи настроек через запятую (по умолчанию встроенный список)")
    ap.add_argument("--resources", help="папка resources STE Slicer (определения, если их нет в архиве)")
    a = ap.parse_args(argv)
    p = load(a.project, a.resources)
    keys = [k.strip() for k in a.keys.split(",")] if a.keys else None
    s = p.summary(keys=keys)
    if a.json:
        json.dump(s, sys.stdout, ensure_ascii=False, indent=1, default=str)
        sys.stdout.write("\n")
    else:
        print_human(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
