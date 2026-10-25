#!/usr/bin/env python3
"""Сравнивает проект 3MF из STE Slicer с ожидаемыми параметрами (цель) или с другим 3MF.

    python3 check_3mf.py project.3mf --target targets/01_gear.json
    python3 check_3mf.py project.3mf --reference other.3mf          # эталон или регрессия
    python3 check_3mf.py project.3mf --target t.json --json         # вывод в JSON
    python3 check_3mf.py project.3mf --target t.json --min-score 80 # код выхода 1, если оценка ниже

Статус: ✓ — как ожидалось, ~ — допустимая альтернатива или не удалось посчитать, ✗ — расхождение.
Оценка = sum(вес * {✓:1, ~:0.5, ✗:0}) / sum(вес) * 100.
Цели в targets/ — наше инженерное прочтение ТЗ, а не официальный эталон.
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ste3mf  # noqa: E402
from inspect_3mf import load  # noqa: E402

PASS, PARTIAL, FAIL = "✓", "~", "✗"
SCORE = {PASS: 1.0, PARTIAL: 0.5, FAIL: 0.0}
H530_DISALLOWED_X = (-169.5, 150.5)  # диапазон x сцены, свободный от machine_disallowed_areas (только 3D)


# Сравнение с ожиданием

def match(expect, value):
    """True/False: выполняется ли ожидание (словарь) для значения."""
    if expect is None:
        return True
    if value is None:
        return False
    ok = True
    if "eq" in expect:
        ok = ok and ste3mf.values_equal(value, expect["eq"])
    if "one_of" in expect:
        ok = ok and any(ste3mf.values_equal(value, x) for x in expect["one_of"])
    if "approx" in expect:
        try:
            ok = ok and abs(float(value) - float(expect["approx"])) <= float(expect.get("tol", 1e-6))
        except (TypeError, ValueError):
            ok = False
    for k, op in (("min", lambda v, x: v >= x - 1e-9), ("max", lambda v, x: v <= x + 1e-9)):
        if k in expect:
            try:
                ok = ok and op(float(value), float(expect[k]))
            except (TypeError, ValueError):
                ok = False
    return ok


def describe(expect):
    if expect is None:
        return "-"
    parts = []
    if "eq" in expect:
        parts.append("= %s" % expect["eq"])
    if "one_of" in expect:
        parts.append("∈ {%s}" % ", ".join(str(x) for x in expect["one_of"]))
    if "approx" in expect:
        parts.append("≈ %s ± %s" % (expect["approx"], expect.get("tol", 0)))
    if "min" in expect and "max" in expect:
        parts.append("%s..%s" % (expect["min"], expect["max"]))
    elif "min" in expect:
        parts.append("≥ %s" % expect["min"])
    elif "max" in expect:
        parts.append("≤ %s" % expect["max"])
    for k, v in expect.items():
        if k not in ("eq", "one_of", "approx", "tol", "min", "max"):
            parts.append("%s=%s" % (k, v))
    return " ".join(parts)


def fmt(v):
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".")
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(fmt(x) for x in v) + "]"
    return str(v)


def row(check, status, actual, expected=None, note=""):
    return {"id": check.get("id", check.get("kind")), "kind": check.get("kind"), "status": status,
            "actual": actual, "expected": expected if expected is not None else describe(check.get("expect")),
            "acceptable": describe(check.get("acceptable")) if check.get("acceptable") else "",
            "weight": float(check.get("weight", 1)), "confidence": check.get("confidence", ""),
            "source": check.get("source", ""), "note": note}


def grade(check, value):
    if match(check.get("expect"), value):
        return PASS
    if check.get("acceptable") and match(check["acceptable"], value):
        return PARTIAL
    return FAIL


# Проверки по цели

def _first_object(p):
    return p.objects[0] if p.objects else None


def check_target(p, target):
    rows = []
    r = p.resolver
    w, d = p.plate_size()
    for c in target.get("checks", []):
        kind = c.get("kind")
        try:
            rows.append(_check_one(p, r, w, d, c, kind, target))
        except Exception as e:  # отчёт целиком не должен падать из-за одной проверки
            rows.append(row(c, PARTIAL, "error", note="%s: %s" % (type(e).__name__, e)))
    return rows


def _check_one(p, r, w, d, c, kind, target):
    if kind == "machine":
        v = p.machine_definition()
        return row(c, grade(c, v), v)

    if kind == "setting":
        key = c["key"]
        where = c.get("where", "auto")
        pos = None
        if where == "auto":
            pos = "0" if r.is_per_extruder(key) else None
        elif where.startswith("extruder"):
            pos = where[len("extruder"):] or "0"
        res = r.resolve(key, pos)
        if not res.get("ok"):
            return row(c, PARTIAL, "? %s" % (res.get("raw") or res.get("note")),
                       note="value not computable offline: %s" % res.get("note"))
        st = grade(c, res["value"])
        return row(c, st, fmt(res["value"]), note="[%s%s]" % ("E" + pos + " " if pos else "", res.get("source")))

    if kind == "material":
        pos = c.get("extruder", "0")
        ex = p.extruders.get(pos)
        if ex is None:
            return row(c, FAIL, "no extruder %s" % pos)
        mid, m = p.slot(ex, "material")
        base = m.metadata.get("base_file") if m else mid
        st = grade(c, base)
        if st == FAIL and m is not None:  # засчитываем и совпадение по GUID или названию
            for alt in (m.metadata.get("GUID"), m.name):
                if alt and grade(c, alt) != FAIL:
                    st = grade(c, alt)
        return row(c, st, "%s (%s)" % (base, m.name if m else "?"))

    if kind in ("quality_type", "quality"):
        _, q = p.slot(p.global_stack, "quality") if p.global_stack else (None, None)
        _, qc = p.slot(p.global_stack, "quality_changes") if p.global_stack else (None, None)
        qt = (q.metadata.get("quality_type") if q else None) or (qc.metadata.get("quality_type") if qc else None)
        note = "custom profile: %s" % qc.name if qc else ""
        return row(c, grade(c, qt), qt, note=note)

    if kind == "quality_changes_name":
        _, qc = p.slot(p.global_stack, "quality_changes") if p.global_stack else (None, None)
        v = qc.name if qc else None
        return row(c, grade(c, v), v)

    if kind == "object_count":
        return row(c, grade(c, len(p.objects)), len(p.objects))

    o = _first_object(p)
    if o is None:
        return row(c, FAIL, "no objects")
    wb = o.world_bbox()

    if kind == "bottom_on_plate":
        return row(c, grade(c, wb[0][2]), fmt(wb[0][2]))

    if kind == "dimensions":
        size = [b - a for a, b in zip(*wb)]
        exp = c.get("expect", {})
        acc = c.get("acceptable")

        def ok(e):
            if e is None:
                return False
            good = True
            tol = float(e.get("tol", 0.5))
            if "size_z" in e:
                good = good and abs(size[2] - float(e["size_z"])) <= tol
            if "size_xy_sorted" in e:
                xs = sorted(size[:2])
                good = good and all(abs(a - float(b)) <= tol for a, b in zip(xs, sorted(e["size_xy_sorted"])))
            return good
        st = PASS if ok(exp) else (PARTIAL if ok(acc) else FAIL)
        return row(c, st, "X %s × Y %s × Z %s" % tuple(fmt(x) for x in size))

    if kind == "up_axis":
        up = o.up_axis_in_source()
        exp = c.get("expect", {})
        tol = float(exp.get("tol_deg", 5))
        ang = ste3mf.angle_between(up, ste3mf.axis_from_name(exp["axis"]))
        name, dev = ste3mf.nearest_axis_name(up)
        st = PASS if ang <= tol else FAIL
        if st == FAIL and c.get("acceptable"):
            acc = c["acceptable"]
            atol = float(acc.get("tol_deg", tol))
            if any(ste3mf.angle_between(up, ste3mf.axis_from_name(a)) <= atol for a in acc.get("any_of", [])):
                st = PARTIAL
        return row(c, st, "%s (%.1f° from %s)" % (name, ang, exp["axis"]),
                   expected="STL %s up ±%s°" % (exp["axis"], tol),
                   note="up vector in STL coords %s" % fmt([round(x, 3) for x in up]))

    if kind == "center_xy":
        cx = (wb[0][0] + wb[1][0]) / 2 - w / 2
        cy = (wb[0][1] + wb[1][1]) / 2 - d / 2
        dist = math.hypot(cx, cy)
        exp = c.get("expect", {})
        acc = c.get("acceptable") or {}
        st = PASS if dist <= exp.get("max_mm", 5) else (PARTIAL if dist <= acc.get("max_mm", -1) else FAIL)
        return row(c, st, "offset %s mm (X %+.2f, Y %+.2f)" % (fmt(dist), cx, cy),
                   expected="≤ %s mm from plate centre" % exp.get("max_mm", 5))

    if kind == "inside_build_volume":
        mode = r.resolve("printing_mode")
        mode_v = mode.get("value") if mode.get("ok") else None
        hr = r.resolve("machine_height")
        h = hr["value"] if hr.get("ok") else (300.0 if mode_v == "classic" else 231.1)
        notes = []
        ok = wb[1][2] <= h + 1e-6 and wb[0][2] >= -0.2
        if not ok:
            notes.append("height %.1f > %.1f or below plate" % (wb[1][2], h))
        if mode_v == "classic":
            xmin, xmax = w / 2 + H530_DISALLOWED_X[0], w / 2 + H530_DISALLOWED_X[1]
            if wb[0][0] < xmin or wb[1][0] > xmax or wb[0][1] < 0 or wb[1][1] > d:
                ok = False
                notes.append("XY bbox outside [%.1f..%.1f]x[0..%.1f]" % (xmin, xmax, d))
            actual = "bbox X %.1f..%.1f Y %.1f..%.1f Z ≤ %.1f" % (wb[0][0], wb[1][0], wb[0][1], wb[1][1], wb[1][2])
        else:
            er = o.ellipse_ratio(w, d)
            if er > 1.0:
                ok = False
                notes.append("outside the 5D ellipse (ratio %.3f)" % er)
            actual = "ellipse ratio %.3f, Z ≤ %.1f" % (er, wb[1][2])
        return row(c, PASS if ok else FAIL, actual, expected="inside %s volume" % (mode_v or "?"),
                   note="; ".join(notes))

    if kind == "axis_on_rotation_axis":
        ax = c["axis_stl"]
        src_c = target.get("stl_bbox_center")
        notes = []
        if src_c is None:
            return row(c, PARTIAL, "?", note="target has no stl_bbox_center")
        ext = o.source_extents_from_local()
        if target.get("stl_extents"):
            if any(abs(a - b) > 1.0 for a, b in zip(ext, target["stl_extents"])):
                notes.append("mesh in 3MF does not look like the STL re-centred (pre-rotated?)")
        pt = o.map_source_point(ax["point"], src_c)
        dr = o.map_source_dir(ax["dir"])
        ang = ste3mf.angle_between(dr, [0, 0, 1])
        ang = min(ang, 180 - ang)
        # расстояние по горизонтали от (почти вертикальной) оси до центра стола в точке оси
        off = math.hypot(pt[0] - w / 2, pt[1] - d / 2)
        exp = c.get("expect", {})
        acc = c.get("acceptable") or {}
        if off <= exp.get("max_offset_mm", 0.5) and ang <= exp.get("max_angle_deg", 1.0):
            st = PASS
        elif off <= acc.get("max_offset_mm", -1) and ang <= acc.get("max_angle_deg", -1):
            st = PARTIAL
        else:
            st = FAIL
        if notes and st == PASS:
            st = PARTIAL  # для этой сетки соответствию STL->3MF доверять нельзя
        return row(c, st, "axis offset %.2f mm, tilt %.2f°" % (off, ang),
                   expected="offset ≤ %s mm, tilt ≤ %s°" % (exp.get("max_offset_mm"), exp.get("max_angle_deg")),
                   note="; ".join(notes))

    if kind == "temperature_vs_material":
        key = c.get("key", "material_print_temperature")
        res = r.resolve(key, "0")
        try:
            mat_default = r.resolve_raw("default_" + key if not key.startswith("default_") else key, "0")
        except ste3mf.Unresolved as e:
            return row(c, PARTIAL, "?", note=str(e))
        if not res.get("ok"):
            return row(c, PARTIAL, "?", note=res.get("note"))
        diff = abs(float(res["value"]) - float(mat_default))
        exp = c.get("expect", {})
        acc = c.get("acceptable") or {}
        st = PASS if diff <= exp.get("tol", 15) else (PARTIAL if diff <= acc.get("tol", -1) else FAIL)
        return row(c, st, "%s (material %s)" % (fmt(res["value"]), fmt(mat_default)),
                   expected="|Δ| ≤ %s °C" % exp.get("tol", 15))

    return row(c, PARTIAL, "?", note="unknown check kind %r" % kind)


# Сравнение с другим проектом

def compare_reference(p, ref, keys=None, pos_tol=1.0, rot_tol=2.0):
    rows = []
    rp, rr = p.resolver, ref.resolver

    def add(cid, kind, status, actual, expected, weight=1.0, note=""):
        rows.append({"id": cid, "kind": kind, "status": status, "actual": actual, "expected": expected,
                     "acceptable": "", "weight": weight, "confidence": "reference",
                     "source": os.path.basename(str(ref.path)), "note": note})

    a, b = p.machine_definition(), ref.machine_definition()
    add("machine", "machine", PASS if a == b else FAIL, a, b, 3)
    for key, wt in (("printing_mode", 5),):
        va, vb = rp.resolve(key).get("value"), rr.resolve(key).get("value")
        add(key, "setting", PASS if ste3mf.values_equal(va, vb) else FAIL, va, vb, wt)
    # материал по экструдерам
    for pos in sorted(set(p.extruders) | set(ref.extruders)):
        ma = p.slot(p.extruders[pos], "material") if pos in p.extruders else (None, None)
        mb = ref.slot(ref.extruders[pos], "material") if pos in ref.extruders else (None, None)
        ia = ma[1].metadata.get("base_file") if ma[1] else ma[0]
        ib = mb[1].metadata.get("base_file") if mb[1] else mb[0]
        ga = ma[1].metadata.get("GUID") if ma[1] else None
        gb = mb[1].metadata.get("GUID") if mb[1] else None
        same = ia == ib or (ga and ga == gb)
        add("material_E%s" % pos, "material", PASS if same else FAIL, ia, ib, 4 if pos == "0" else 1)
    # quality
    qa = p.slot(p.global_stack, "quality")[1] if p.global_stack else None
    qb = ref.slot(ref.global_stack, "quality")[1] if ref.global_stack else None
    ta = qa.metadata.get("quality_type") if qa else None
    tb = qb.metadata.get("quality_type") if qb else None
    add("quality_type", "quality", PASS if ta == tb else FAIL, ta, tb, 1)
    qca = p.slot(p.global_stack, "quality_changes")[1] if p.global_stack else None
    qcb = ref.slot(ref.global_stack, "quality_changes")[1] if ref.global_stack else None
    na, nb = (qca.name if qca else None), (qcb.name if qcb else None)
    if na or nb:
        add("quality_changes", "quality", PASS if na == nb else PARTIAL, na, nb, 0.5,
            "custom profile name (values are compared below)")
    # настройки: список ключей + всё, что явно изменено хотя бы в одном из проектов
    all_keys = list(keys or ste3mf.KEY_SETTINGS)
    for proj in (p, ref):
        stacks = ([proj.global_stack] if proj.global_stack else []) + list(proj.extruders.values())
        for st in stacks:
            for slot in ("user", "quality_changes"):
                _, cont = proj.slot(st, slot)
                if cont is not None:
                    for k in cont.values:
                        if k not in all_keys:
                            all_keys.append(k)
    for key in all_keys:
        if key == "printing_mode":
            continue
        pos = "0" if rp.is_per_extruder(key) or rr.is_per_extruder(key) else None
        ra, rb = rp.resolve(key, pos), rr.resolve(key, pos)
        if not ra.get("ok") or not rb.get("ok"):
            if ra.get("raw") == rb.get("raw") and ra.get("raw") is not None:
                st = PASS
            else:
                st = PARTIAL
            add(key, "setting", st, ra.get("value", ra.get("raw")), rb.get("value", rb.get("raw")), 1,
                "not fully computable")
            continue
        st = PASS if ste3mf.values_equal(ra["value"], rb["value"], rel=1e-3, abs_tol=1e-3) else FAIL
        add(key, "setting", st, fmt(ra["value"]), fmt(rb["value"]), 1,
            "%s%s vs %s" % ("E0 " if pos else "", ra.get("source"), rb.get("source")))
    # objects
    add("object_count", "object_count", PASS if len(p.objects) == len(ref.objects) else FAIL,
        len(p.objects), len(ref.objects), 1)
    wa, da = p.plate_size()
    wb_, db_ = ref.plate_size()
    for i, (oa, ob) in enumerate(zip(p.objects, ref.objects), 1):
        ta_, tb_ = oa.triangles, ob.triangles
        st = PASS if ta_ == tb_ else (PARTIAL if abs(ta_ - tb_) <= 0.01 * max(ta_, tb_) else FAIL)
        add("obj%d.triangles" % i, "mesh", st, ta_, tb_, 0.5, "different mesh?" if st == FAIL else "")
        ba, bb = oa.world_bbox(), ob.world_bbox()
        sa = [y - x for x, y in zip(*ba)]
        sb = [y - x for x, y in zip(*bb)]
        dz = abs(sa[2] - sb[2])
        dxy = max(abs(x - y) for x, y in zip(sa[:2], sb[:2]))
        dxy_sw = max(abs(sa[0] - sb[1]), abs(sa[1] - sb[0]))
        st = PASS if dz <= 0.5 and dxy <= 0.5 else (PARTIAL if dz <= 0.5 and dxy_sw <= 0.5 else FAIL)
        add("obj%d.size" % i, "dimensions", st, fmt([round(x, 2) for x in sa]), fmt([round(x, 2) for x in sb]), 2,
            "same height, rotated about Z" if st == PARTIAL else "")
        ca = [(x + y) / 2 for x, y in zip(*ba)]
        cb = [(x + y) / 2 for x, y in zip(*bb)]
        off = math.hypot((ca[0] - wa / 2) - (cb[0] - wb_ / 2), (ca[1] - da / 2) - (cb[1] - db_ / 2))
        st = PASS if off <= pos_tol else (PARTIAL if off <= 10 * pos_tol else FAIL)
        add("obj%d.position" % i, "placement", st, "Δxy %.2f mm" % off, "≤ %s mm" % pos_tol, 2)
        dzb = abs(ba[0][2] - bb[0][2])
        add("obj%d.bottom_z" % i, "placement", PASS if dzb <= 0.2 else FAIL, fmt(ba[0][2]), fmt(bb[0][2]), 0.5)
        ua, ub = oa.up_axis_in_source(), ob.up_axis_in_source()
        up_ang = ste3mf.angle_between(ua, ub)
        ra_ = ste3mf.rotation_info(oa.rotation_vs_source())["rotation"]
        rb_ = ste3mf.rotation_info(ob.rotation_vs_source())["rotation"]
        rel = ste3mf.mat_mul3(ste3mf.transpose3(ra_), rb_)
        full = ste3mf.rotation_info(rel)["axis_angle"]["angle_deg"]
        if up_ang <= rot_tol and full <= rot_tol:
            st = PASS
        elif up_ang <= rot_tol:
            st = PARTIAL
        else:
            st = FAIL
        add("obj%d.orientation" % i, "up_axis", st, "up Δ %.1f°, full Δ %.1f°" % (up_ang, full),
            "≤ %s°" % rot_tol, 4, "differs only by rotation about the vertical axis" if st == PARTIAL else "")
        pa, pb = oa.settings, ob.settings
        keys_o = sorted(set(pa) | set(pb))
        if keys_o:
            diff = [k for k in keys_o if not ste3mf.values_equal(pa.get(k), pb.get(k))]
            add("obj%d.per_object_settings" % i, "per_object", PASS if not diff else FAIL,
                fmt(pa) if pa else "{}", fmt(pb) if pb else "{}", 1, "differs: %s" % diff if diff else "")
    return rows


# Вывод

def total_score(rows):
    tw = sum(r["weight"] for r in rows) or 1.0
    return 100.0 * sum(r["weight"] * SCORE[r["status"]] for r in rows) / tw


def format_table(rows, title="", out=None):
    out = out or sys.stdout
    w = out.write
    if title:
        w(title + "\n")
    cols = [("", 1), ("check", 30), ("actual", 34), ("expected", 34), ("w", 4), ("conf", 9)]
    w("  ".join(name.ljust(width) for name, width in cols) + "\n")
    w("-" * 122 + "\n")
    for r in rows:
        vals = [r["status"], str(r["id"]), str(r["actual"]), str(r["expected"]), fmt(r["weight"]),
                str(r["confidence"])]
        line = "  ".join(v[:width].ljust(width) for v, (_, width) in zip(vals, cols))
        w(line.rstrip() + "\n")
        extra = []
        if r.get("acceptable") and r["status"] != PASS:
            extra.append("acceptable: " + r["acceptable"])
        if r.get("note"):
            extra.append(r["note"])
        if extra:
            w("     " + " | ".join(extra) + "\n")
    n = {s: sum(1 for r in rows if r["status"] == s) for s in (PASS, PARTIAL, FAIL)}
    w("-" * 122 + "\n")
    w("%s %d   %s %d   %s %d   score %.1f%%\n" % (PASS, n[PASS], PARTIAL, n[PARTIAL], FAIL, n[FAIL],
                                                 total_score(rows)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="проект .3mf для проверки")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--target", help="ожидаемые параметры, JSON (devtools/targets/*.json)")
    g.add_argument("--reference", help="другой проект .3mf для сравнения")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    ap.add_argument("--min-score", type=float, default=None, help="код выхода 1, если оценка ниже")
    ap.add_argument("--resources", help="папка resources STE Slicer (определения, если их нет в архиве)")
    a = ap.parse_args(argv)

    p = load(a.project, a.resources)
    if a.target:
        with open(a.target, encoding="utf-8") as f:
            target = json.load(f)
        rows = check_target(p, target)
        title = "%s  vs  target %s (%s)\n%s" % (os.path.basename(a.project), os.path.basename(a.target),
                                               target.get("model", ""), target.get("status", ""))
    else:
        ref = load(a.reference, a.resources)
        rows = compare_reference(p, ref)
        title = "%s  vs  reference %s" % (os.path.basename(a.project), os.path.basename(a.reference))
    score = total_score(rows)
    if a.json:
        json.dump({"title": title, "score": round(score, 2), "warnings": p.warnings, "rows": rows},
                  sys.stdout, ensure_ascii=False, indent=1, default=str)
        sys.stdout.write("\n")
    else:
        for msg in p.warnings:
            print("WARNING:", msg)
        format_table(rows, title)
    if a.min_score is not None and score < a.min_score:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
