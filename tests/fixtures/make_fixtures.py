"""Собирает синтетические фикстуры: проекты 3MF и G-code как из STE Slicer.

Раскладка архива повторяет plugins/3MFWriter (ThreeMFWriter + ThreeMFWorkspaceWriter) STE Slicer 7.10
(форк Cura 3.6). Определения берутся из файлов слайсера (resources/definitions установленного STE Slicer
или исходников; наследование и переопределения, сериализация как в DefinitionContainer.serialize()).

    python tests/fixtures/make_fixtures.py          # из корня репозитория, перезаписывает фикстуры

Сетки — упрощённые заменители с габаритами настоящих STL (в координатах STL), поэтому проверки
ориентации и постановки ведут себя как на настоящих моделях.
"""

import configparser
import io
import json
import math
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO_DIR, "devtools"))

import ste3mf  # noqa: E402

MACHINE_DEF = "hte530v5.2.4"
EXTRUDER_DEFS = {"0": "stereotech_v5.2.4_extruder_main", "1": "stereotech_v5.2.4_extruder_second"}
GLOBAL_ID = "Stereotech Hybrid 530 V5.2.4"
SETTING_VERSION = 5
W, D = 351.0, 300.3


# Геометрия

def cylinder_y(cx, cz, r, y0, y1, seg=32):
    """Замкнутый цилиндр вдоль +Y (координаты STL), «суп» из треугольников как в STL (3 вершины на грань)."""
    tris = []
    pts = [(cx + r * math.cos(2 * math.pi * i / seg), cz + r * math.sin(2 * math.pi * i / seg)) for i in range(seg)]
    for i in range(seg):
        (xa, za), (xb, zb) = pts[i], pts[(i + 1) % seg]
        tris.append(((xa, y0, za), (xb, y0, zb), (xb, y1, zb)))
        tris.append(((xa, y0, za), (xb, y1, zb), (xa, y1, za)))
        tris.append(((cx, y0, cz), (xb, y0, zb), (xa, y0, za)))
        tris.append(((cx, y1, cz), (xa, y1, za), (xb, y1, zb)))
    return tris


def rot_x(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def node_transform(tris_stl, r_print, offset_xy=(0.0, 0.0), lift=0.0):
    """Возвращает (локальные вершины, преобразование 3MF (lin, t)) так, как их записал бы STE Slicer:
    local = F^-1 (p - центр габаритов) (перестановка осей STLReader + центрирование MeshFileHandler),
    M_lin = R_print * F, смещение ставит центр габаритов в центр стола + offset, а низ — на z=lift."""
    pts = [p for t in tris_stl for p in t]
    mn = [min(p[i] for p in pts) for i in range(3)]
    mx = [max(p[i] for p in pts) for i in range(3)]
    c = [(a + b) / 2 for a, b in zip(mn, mx)]
    local = [ste3mf.mat_vec3(ste3mf.F_INV, [p[i] - c[i] for i in range(3)]) for p in pts]
    lin = ste3mf.mat_mul3(r_print, ste3mf.F_SCENE_TO_3MF)
    rotated = [ste3mf.mat_vec3(lin, v) for v in local]
    rmn = [min(v[i] for v in rotated) for i in range(3)]
    rmx = [max(v[i] for v in rotated) for i in range(3)]
    t = [W / 2 + offset_xy[0] - (rmn[0] + rmx[0]) / 2,
         D / 2 + offset_xy[1] - (rmn[1] + rmx[1]) / 2,
         lift - rmn[2]]
    return local, (lin, t)


def transform_string(lin, t):
    # как ThreeMFWriter._convertMatrixToString: data[0,0] data[1,0] data[2,0] data[0,1] ... data[0,3] data[1,3] data[2,3]
    vals = [lin[0][0], lin[1][0], lin[2][0], lin[0][1], lin[1][1], lin[2][1],
            lin[0][2], lin[1][2], lin[2][2], t[0], t[1], t[2]]
    return " ".join(repr(float(round(v, 9))) for v in vals)


def model_xml(local_verts, transform, settings=None):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<model unit="millimeter" xml:lang="en-US" xmlns="%s" xmlns:cura="%s">' % (ste3mf.NS_3MF, ste3mf.NS_CURA),
             '<resources>', '<object id="1" type="model">', '<mesh>', '<vertices>']
    for v in local_verts:
        lines.append('<vertex x="%.6g" y="%.6g" z="%.6g" />' % tuple(v))
    lines.append('</vertices>')
    lines.append('<triangles>')
    for i in range(0, len(local_verts), 3):
        lines.append('<triangle v1="%d" v2="%d" v3="%d" />' % (i, i + 1, i + 2))
    lines.append('</triangles>')
    lines.append('</mesh>')
    if settings:
        lines.append('<metadatagroup>')
        for k, v in sorted(settings.items()):
            lines.append('<metadata name="cura:%s" preserve="true" type="xs:string">%s</metadata>' % (k, v))
        lines.append('</metadatagroup>')
    lines += ['</object>', '</resources>', '<build>',
              '<item objectid="1" transform="%s" />' % transform_string(*transform), '</build>', '</model>']
    return "\n".join(lines)


# Контейнеры

def cfg(sections):
    cp = configparser.ConfigParser(interpolation=None)
    for name, values in sections:
        cp[name] = {k: str(v) for k, v in values.items()}
    s = io.StringIO()
    cp.write(s)
    return s.getvalue()


def instance_cfg(name, definition, metadata, values):
    md = {"setting_version": SETTING_VERSION}
    md.update(metadata)
    return cfg([("general", {"version": 4, "name": name, "definition": definition}),
                ("metadata", md), ("values", values)])


def stack_cfg(sid, name, metadata, containers):
    return cfg([("general", {"version": 4, "name": name, "id": sid}),
                ("metadata", metadata),
                ("containers", {str(i): c for i, c in enumerate(containers)})])


def build_project(path, *, mode=None, material="enduse_petg", quality="normal", quality_changes=None,
                  global_user=None, extruder_user=None, tris_stl=None, r_print=None, offset_xy=(0, 0),
                  per_object=None, resources=None, steslicer_version="7.10.12"):
    resources = resources or ste3mf.find_resources_dir(REPO_DIR)
    if resources is None:
        raise SystemExit("не найдена папка resources слайсера (задайте STE_RESOURCES)")
    global_user = dict(global_user or {})
    if mode is not None:
        global_user["printing_mode"] = mode
    extruder_user = dict(extruder_user or {})
    zf_buf = io.BytesIO()
    z = zipfile.ZipFile(zf_buf, "w", compression=zipfile.ZIP_DEFLATED)

    def put(name, data):
        zi = zipfile.ZipInfo(name)
        zi.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(zi, data)

    # что пишет ThreeMFWriter.write
    local, tr = node_transform(tris_stl, r_print or ste3mf.IDENTITY[0], offset_xy)
    put("3D/3dmodel.model", model_xml(local, tr, per_object))
    put("[Content_Types].xml", b'<?xml version="1.0" encoding="UTF-8"?> \n'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default ContentType="application/vnd.openxmlformats-package.relationships+xml" Extension="rels" />'
        b'<Default ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml" Extension="model" />'
        b'</Types>')
    put("_rels/.rels", b'<?xml version="1.0" encoding="UTF-8"?> \n'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rel0" Target="/3D/3dmodel.model" '
        b'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" /></Relationships>')

    # ThreeMFWorkspaceWriter: глобальный стек и его контейнеры
    qc_id = "empty_quality_changes"
    g_user_id = GLOBAL_ID + "_user"
    g_settings_id = GLOBAL_ID + "_settings"
    if quality_changes:
        qc_id = quality_changes["name"].lower().replace(" ", "_") + "_global"
    put("SteSlicer/%s.global.cfg" % GLOBAL_ID,
        stack_cfg(GLOBAL_ID, GLOBAL_ID, {"type": "machine", "setting_version": SETTING_VERSION},
                  [g_user_id, qc_id, quality, "empty_material", "empty_variant", g_settings_id, MACHINE_DEF]))
    put("SteSlicer/%s.inst.cfg" % g_user_id,
        instance_cfg(g_user_id, MACHINE_DEF, {"type": "user", "machine": GLOBAL_ID}, global_user))
    if quality_changes:
        put("SteSlicer/%s.inst.cfg" % qc_id,
            instance_cfg(quality_changes["name"], "fdmprinter",
                         {"type": "quality_changes", "quality_type": quality}, quality_changes.get("global", {})))
    with open(os.path.join(resources, "quality", {"normal": "normal", "fast": "fast", "draft": "draft",
                                                   "high": "high", "verydraft": "extra_fast"}[quality]
                           + ".inst.cfg"), encoding="utf-8") as f:
        put("SteSlicer/%s.inst.cfg" % quality, f.read())
    put("SteSlicer/%s.inst.cfg" % g_settings_id,
        instance_cfg(g_settings_id, MACHINE_DEF, {"type": "definition_changes"}, {}))
    put("SteSlicer/%s.def.json" % MACHINE_DEF,
        json.dumps(ste3mf.serialize_definition(ste3mf.resolve_definition_json(MACHINE_DEF, resources)),
                   separators=(", ", ": "), indent=4))

    # экструдеры
    for pos, edef in EXTRUDER_DEFS.items():
        eid = edef + " #2"  # registry.uniqueName(id определения) совпадает с самим определением
        e_user = eid + "_user"
        e_settings = eid + "_settings"
        mat = material if pos == "0" else "proto_pva"
        e_qc = "empty_quality_changes"
        if quality_changes:
            e_qc = quality_changes["name"].lower().replace(" ", "_") + "_" + pos
            put("SteSlicer/%s.inst.cfg" % e_qc,
                instance_cfg(quality_changes["name"], "fdmprinter",
                             {"type": "quality_changes", "quality_type": quality, "position": pos},
                             quality_changes.get(pos, {})))
        put("SteSlicer/%s.extruder.cfg" % eid,
            stack_cfg(eid, edef.replace("_", " "), {"type": "extruder_train", "machine": GLOBAL_ID,
                                                    "position": pos, "enabled": "True",
                                                    "setting_version": SETTING_VERSION},
                      [e_user, e_qc, "empty_quality", mat, "empty_variant", e_settings, edef]))
        put("SteSlicer/%s.inst.cfg" % e_user,
            instance_cfg(e_user, MACHINE_DEF, {"type": "user", "extruder": eid},
                         extruder_user if pos == "0" else {}))
        put("SteSlicer/%s.inst.cfg" % e_settings,
            instance_cfg(e_settings, edef, {"type": "definition_changes"}, {}))
        with open(os.path.join(resources, "materials", mat + ".xml.fdm_material"), encoding="utf-8") as f:
            put("SteSlicer/%s.xml.fdm_material" % mat, f.read())
        put("SteSlicer/%s.def.json" % edef,
            json.dumps(ste3mf.serialize_definition(ste3mf.resolve_definition_json(edef, resources)),
                       separators=(", ", ": "), indent=4))

    put("SteSlicer/preferences.cfg", cfg([
        ("general", {"version": 6, "visible_settings": "machine_settings;printing_mode_category;layer_height"}),
        ("physics", {"automatic_drop_down": "True"}),
        ("steslicer", {"active_mode": 1, "categories_expanded": ""}),
        ("metadata", {"setting_version": SETTING_VERSION})]))
    put("SteSlicer/version.ini", cfg([("versions", {"steslicer_version": steslicer_version,
                                                    "build_type": "", "is_debug_mode": "False"})]))
    z.close()
    with open(path, "wb") as f:
        f.write(zf_buf.getvalue())
    return path


GCODE_3D_TEMPLATE = """;PREVIEW:iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==
;VERSION:7.10.12
;PRINTING_MODE:[classic]
;MINX:-16.6
;MINY:-16.6
;MINZ:0.0
;MAXX:16.6
;MAXY:16.6
;MAXZ:10.1
;FLAVOR:RepRap
;TIME:1534
;Filament used: 0.93m
;Layer height: 0.15
;Generated with Cura_SteamEngine 3.6.0
T0
M140 S115
M105
M190 S115
M104 S290
M105
M109 S290
M82 ;absolute extrusion mode
;STE Slicer 5D Gcode
START
G1 F200 E15 ;extrude 15 mm of feed stock
G1 F200 E13.5 ;make retraction
G92 E0 ;zero the extruded length again
G54
G4 P100;Move to the first coordinate system
G92 E0
G92 E0
G1 F1500 E-1
;LAYER_COUNT:3
;LAYER:0
M107
G0 F3600 X159.2 Y135.6 Z0.3
;TYPE:WALL-OUTER
G1 F1500 E0
G1 F1800 X192.0 Y135.6 E1.2
G1 X192.0 Y164.7 E2.4
G1 X159.2 Y164.7 E3.6
G1 X159.2 Y135.6 E4.8
;LAYER:1
M106 S255
G0 X160.0 Y136.0 Z0.45
;TYPE:FILL
G1 X191.0 Y136.0 E6.0
G1 X191.0 Y164.0 E7.1
;LAYER:2
G0 X160.0 Y136.0 Z0.6
G1 X191.0 Y136.0 E8.2
;TIME_ELAPSED:1534.0
G1 F1500 E7.2
M107
M104 S0
M140 S0
;prefix_end_gcode
END
{settings}"""

GCODE_5D = """;VERSION:7.10.12
;PRINTING_MODE:[cylindrical]
;MINX:-29.5
;MINY:-29.5
;MINZ:0.0
;MAXX:29.5
;MAXY:29.5
;MAXZ:25.0
T0
M104 S230
M105
M109 S230
M82 ;absolute extrusion mode
;STE Slicer 5D Gcode
START
G1 F200 E15 ;extrude 15 mm of feed stock
G1 F200 E13.5 ;make retraction
G92 E0 ;zero the extruded length again
G56;Move to the first coordinate system
;LAYER_COUNT
;LAYER:0
G0 X0.00 Y-12.50 Z8.20 A90.00 C0.000 F3600
G1 X0.00 Y-12.50 Z8.20 C30.000 F1200 E1.20000
G1 C60.000 E2.40000
G1 C90.000 E3.60000
G1 C120.000 E4.80000
;LAYER:1
G0 Z8.40
G1 C150.000 E6.00000
G1 C180.000 E7.20000
;prefix_end_gcode
END
"""


def settings_block(global_values, extruder_values, quality_type="normal", quality_name="Fine #2"):
    """Как GCodeWriter._serialiseSettings: слитые user+quality_changes -> JSON -> экранированные строки ;SETTING_3."""
    import re

    def flat(values, extra_md):
        md = {"setting_version": SETTING_VERSION, "type": "quality_changes", "quality_type": quality_type}
        md.update(extra_md)
        return instance_cfg(quality_name, "fdmprinter", md, values)

    data = {"global_quality": flat(global_values, {}),
            "extruder_quality": [flat(v, {"position": str(i)}) for i, v in enumerate(extruder_values)]}
    js = json.dumps(data)
    esc = {re.escape("\\"): "\\\\", re.escape("\n"): "\\n", re.escape("\r"): "\\r"}
    js = re.compile("|".join(esc.keys())).sub(lambda m: esc[re.escape(m.group(0))], js)
    prefix = ";SETTING_3 "
    n = 80 - len(prefix)
    return "".join(prefix + js[i:i + n] + "\n" for i in range(0, len(js), n))


def main():
    os.makedirs(HERE, exist_ok=True)
    # 01 шестерня, 3D: диск на столе (+Y STL вверх = +90° вокруг X), по центру, Fiberpart PC G20
    gear = cylinder_y(0.0, 0.0, 16.55, 0.0, 10.1)
    build_project(os.path.join(HERE, "gear_3d.3mf"), mode="classic", material="fiberpart_pc_g20",
                  quality="fast", tris_stl=gear, r_print=rot_x(90),
                  global_user={"support_enable": "False", "adhesion_type": "brim"},
                  extruder_user={"infill_sparse_density": "100", "wall_line_count": "4",
                                 "material_print_temperature": "290"})
    # 02 колесо конвейера, 5D Spiral: ось (Y STL) вертикально, на оси вращения, Base Diameter 16
    wheel = cylinder_y(29.5, 29.5, 29.5, 0.0, 25.0)
    build_project(os.path.join(HERE, "wheel_5d.3mf"), mode="cylindrical", material="fiberpart_tpu_c5",
                  quality="draft", tris_stl=wheel, r_print=rot_x(90),
                  global_user={"cylindrical_mode_base_diameter": "16", "support_enable": "False"},
                  extruder_user={"wall_line_count": "4", "infill_sparse_density": "60"},
                  per_object={"infill_sparse_density": "60"})
    with open(os.path.join(HERE, "gear_3d.gcode"), "w", encoding="utf-8", newline="\n") as f:
        f.write(GCODE_3D_TEMPLATE.replace("{settings}", settings_block(
            {"printing_mode": "classic", "support_enable": "False", "adhesion_type": "brim"},
            [{"infill_sparse_density": "100", "wall_line_count": "4", "material_print_temperature": "290"}, {}],
            quality_type="fast", quality_name="Normal #2")))
    with open(os.path.join(HERE, "wheel_5d.gcode"), "w", encoding="utf-8", newline="\n") as f:
        f.write(GCODE_5D)
    print("fixtures written to", HERE)


if __name__ == "__main__":
    main()
