"""Библиотека: чтение проекта 3MF из STE Slicer (форк Cura 3.6) без самого слайсера.

Только стандартная библиотека (без numpy), Python 3.8+.

Что лежит в проекте 3MF, который пишет ``ThreeMFWorkspaceWriter``
(plugins/3MFWriter/ThreeMFWorkspaceWriter.py + ThreeMFWriter.py):

    [Content_Types].xml              Default rels / model content types
    _rels/.rels                      Relationship -> /3D/3dmodel.model
    3D/3dmodel.model                 meshes + per-node transform + per-object settings (libSavitar)
    SteSlicer/<global id>.global.cfg global stack: [general] id/name, [metadata] type=machine,
                                     [containers] 0..6 = user, quality_changes, quality, material,
                                     variant, definition_changes, definition
    SteSlicer/<extruder id>.extruder.cfg   the same for each extruder, [metadata] position=0/1, machine=..
    SteSlicer/<id>.inst.cfg          InstanceContainers (user, quality_changes, quality,
                                     definition_changes, variant): [general] name/definition,
                                     [metadata] type/quality_type/position, [values] key = value|=formula
    SteSlicer/<base_file>.xml.fdm_material  material profile (XML), file name = root material id
    SteSlicer/<definition id>.def.json      FULL (inheritance-resolved) definition: machine + extruders
    SteSlicer/preferences.cfg        visible settings, active_mode ...
    SteSlicer/version.ini            [versions] steslicer_version, build_type, is_debug_mode

Пустые контейнеры (empty_quality, empty_material, empty_variant, empty_quality_changes) в архив
НЕ пишутся, на них только ссылаются по id в файлах стеков.

Координаты:
    Сцена Cura — Y вверх, начало в центре стола. Вершины в 3dmodel.model — *локальные* данные
    сетки узла (Y вверх; при загрузке из STL центрированы по габаритам), преобразование элемента
    M = T(machine_width/2, machine_depth/2, 0) * F * W_node, где F: (x, y, z)_сцена -> (x, -z, y)
    (Y-up -> Z-up). Значит, мировые координаты после M — Z вверх, начало в переднем левом углу стола:
        3D (classic, machine_center_is_zero=False):  X/Y/Z в G-code == x/y/z мира 3MF
        5D (center_is_zero=True):                    X/Y в G-code   == x - W/2, y - D/2 в 3MF
    Читатель STL переводит STL (x, y, z) -> сцена (x, z, -y) = F^-1, поэтому поворот детали
    относительно *исходного STL* в осях принтера: R_print = M_lin * F^-1.
"""

import ast
import configparser
import io
import json
import math
import os
import re
import xml.etree.ElementTree as ET
import zipfile

NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_CURA = "http://software.ultimaker.com/xml/cura/3mf/2015/10"
NS_MATERIAL = "http://www.ultimaker.com/material"

STACK_SLOTS = ["user", "quality_changes", "quality", "material", "variant",
               "definition_changes", "definition"]

DEFAULT_MACHINE_WIDTH = 351.0
DEFAULT_MACHINE_DEPTH = 300.3

# Настройки, которые по умолчанию печатаем и сравниваем.
KEY_SETTINGS = [
    "printing_mode", "layer_height", "layer_height_0", "cylindrical_layer_height",
    "line_width", "wall_line_count", "wall_thickness", "top_layers", "bottom_layers",
    "top_bottom_thickness", "infill_sparse_density", "infill_pattern",
    "material_print_temperature", "material_print_temperature_layer_0",
    "material_bed_temperature", "speed_print", "cool_fan_enabled", "cool_fan_speed",
    "retraction_enable", "retraction_amount", "retraction_speed",
    "support_enable", "support_type", "support_angle", "support_tree_enable",
    "support_extruder_nr", "support_enable_cylindrical", "support_enable_top_support",
    "adhesion_type", "brim_width", "cylindrical_raft_type",
    "cylindrical_mode_base_diameter", "non_printing_base_diameter", "cylindrical_mode_overlap",
    "xy_offset", "ironing_enabled", "z_seam_type", "magic_spiralize",
    "machine_nozzle_size", "material_diameter",
]

# XmlMaterialProfile.__material_settings_setting_map
MATERIAL_SETTING_MAP = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "processing temperature graph": "material_flow_temp_graph",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
    "adhesion tendency": "material_adhesion_tendency",
    "surface energy": "material_surface_energy",
    "shrinkage percentage": "material_shrinkage_percentage",
}


class Unresolved(Exception):
    """Формулу не получилось вычислить без слайсера."""


# Немного линейной алгебры (3x3 / 3x4, списки по строкам)

def mat_mul3(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mat_vec3(a, v):
    return [a[i][0] * v[0] + a[i][1] * v[1] + a[i][2] * v[2] for i in range(3)]


def transpose3(a):
    return [[a[j][i] for j in range(3)] for i in range(3)]


def det3(a):
    return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))


def compose(a, b):
    """Композиция аффинных преобразований: a, b = (lin3x3, t3); возвращает a*b."""
    lin = mat_mul3(a[0], b[0])
    t = [x + y for x, y in zip(mat_vec3(a[0], b[1]), a[1])]
    return (lin, t)


IDENTITY = ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0])
# F: scene (Y-up) -> 3MF (Z-up): (x, y, z) -> (x, -z, y)
F_SCENE_TO_3MF = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]
# F^-1 — как STLReader из Uranium переводит STL в сцену: (x, y, z) -> (x, z, -y)
F_INV = transpose3(F_SCENE_TO_3MF)


def parse_3mf_transform(text):
    """Преобразование 3MF "m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32" (строки-векторы).
    Возвращает (lin, t) для векторов-столбцов: p' = lin * p + t.
    Cura пишет data[0,0] data[1,0] data[2,0] data[0,1] ... data[0,3] data[1,3] data[2,3]."""
    if not text or not text.strip():
        return IDENTITY
    v = [float(x) for x in text.split()]
    if len(v) != 12:
        raise ValueError("bad 3MF transform: %r" % text)
    lin = [[v[0], v[3], v[6]], [v[1], v[4], v[7]], [v[2], v[5], v[8]]]
    return (lin, [v[9], v[10], v[11]])


def rotation_info(lin):
    """Разложение матрицы 3x3: масштаб по столбцам, отражение, чистый поворот, углы Эйлера XYZ, ось и угол."""
    cols = [[lin[r][c] for r in range(3)] for c in range(3)]
    scales = [math.sqrt(sum(x * x for x in col)) for col in cols]
    d = det3(lin)
    rot = [[lin[r][c] / (scales[c] or 1.0) for c in range(3)] for r in range(3)]
    mirrored = d < 0
    if mirrored:  # для вывода углов делаем из неё настоящий поворот
        rot = [[-rot[r][c] for c in range(3)] for r in range(3)]
    # внешние повороты: X, потом Y, потом Z (R = Rz * Ry * Rx)
    sy = -rot[2][0]
    sy = max(-1.0, min(1.0, sy))
    ry = math.degrees(math.asin(sy))
    if abs(sy) < 0.999999:
        rx = math.degrees(math.atan2(rot[2][1], rot[2][2]))
        rz = math.degrees(math.atan2(rot[1][0], rot[0][0]))
    else:  # складывание рамок (gimbal lock)
        rx = math.degrees(math.atan2(-rot[1][2], rot[1][1]))
        rz = 0.0
    tr = rot[0][0] + rot[1][1] + rot[2][2]
    angle = math.degrees(math.acos(max(-1.0, min(1.0, (tr - 1) / 2))))
    axis = [rot[2][1] - rot[1][2], rot[0][2] - rot[2][0], rot[1][0] - rot[0][1]]
    n = math.sqrt(sum(x * x for x in axis))
    if n > 1e-9:
        axis = [x / n for x in axis]
    elif angle > 90:  # около 180 градусов: ось по диагонали
        k = max(range(3), key=lambda i: rot[i][i])
        axis = [0.0, 0.0, 0.0]
        axis[k] = math.sqrt(max(0.0, (rot[k][k] + 1) / 2))
        for i in range(3):
            if i != k:
                axis[i] = rot[i][k] / (2 * axis[k]) if axis[k] else 0.0
    else:
        axis = [0.0, 0.0, 1.0]
    return {
        "scale": [round(s, 6) for s in scales],
        "mirrored": mirrored,
        "rotation": [[round(x, 6) for x in row] for row in rot],
        "euler_xyz_deg": [round(rx, 3), round(ry, 3), round(rz, 3)],
        "axis_angle": {"axis": [round(x, 5) for x in axis], "angle_deg": round(angle, 3)},
    }


AXIS_NAMES = {(1, 0, 0): "+X", (-1, 0, 0): "-X", (0, 1, 0): "+Y", (0, -1, 0): "-Y",
              (0, 0, 1): "+Z", (0, 0, -1): "-Z"}


def axis_from_name(name):
    name = name.strip().upper()
    sign = -1.0 if name.startswith("-") else 1.0
    letter = name[-1]
    v = [0.0, 0.0, 0.0]
    v["XYZ".index(letter)] = sign
    return v


def nearest_axis_name(v):
    best = None
    for vec, name in AXIS_NAMES.items():
        dot = sum(a * b for a, b in zip(v, vec))
        if best is None or dot > best[0]:
            best = (dot, name)
    ang = math.degrees(math.acos(max(-1.0, min(1.0, best[0]))))
    return best[1], round(ang, 2)


def angle_between(a, b):
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    c = sum(x * y for x, y in zip(a, b)) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


# Приведение значений

def convert_value(raw, setting_type=None):
    """Значение из файла (строка) -> значение Python по типу настройки."""
    if not isinstance(raw, str):
        return raw
    s = raw.strip()
    if setting_type == "bool":
        return s.lower() in ("true", "1", "yes")
    if setting_type == "float":
        try:
            return float(s)
        except ValueError:
            pass
    if setting_type == "int":
        try:
            return int(float(s))
        except ValueError:
            pass
    if setting_type in ("str", "enum", "extruder", "optional_extruder"):
        return s
    if s in ("True", "False"):
        return s == "True"
    try:
        return ast.literal_eval(s)
    except Exception:
        return s


def values_equal(a, b, rel=1e-3, abs_tol=1e-6):
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b) if isinstance(a, bool) and isinstance(b, bool) else str(a) == str(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_tol)
    try:
        fa, fb = float(a), float(b)
        return math.isclose(fa, fb, rel_tol=rel, abs_tol=abs_tol)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


# Безопасное вычисление формул

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.IfExp, ast.Compare, ast.Call,
    ast.Name, ast.Load, ast.Attribute, ast.Subscript, ast.Index if hasattr(ast, "Index") else ast.Slice,
    ast.Slice, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.GeneratorExp,
    ast.comprehension, ast.Store, ast.keyword, ast.And, ast.Or, ast.Not, ast.Add, ast.Sub, ast.Mult,
    ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Eq, ast.NotEq, ast.Lt,
    ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Constant,
)
if hasattr(ast, "Num"):  # py < 3.8 compatibility (harmless otherwise)
    _ALLOWED_NODES = _ALLOWED_NODES + (ast.Num, ast.Str, ast.NameConstant)

_SAFE_BUILTINS = {
    "min": min, "max": max, "round": round, "abs": abs, "int": int, "float": float, "str": str,
    "bool": bool, "len": len, "sum": sum, "any": any, "all": all, "map": map, "list": list,
    "sorted": sorted, "range": range, "True": True, "False": False, "None": None,
}


def _check_ast(tree):
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise Unresolved("forbidden syntax %s" % type(node).__name__)
        if isinstance(node, ast.Attribute):
            if not (isinstance(node.value, ast.Name) and node.value.id == "math"):
                raise Unresolved("attribute access")
            if node.attr.startswith("_"):
                raise Unresolved("private attribute")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise Unresolved("dunder name")


class _Names(dict):
    """Пространство имён, в котором неизвестные имена ищутся как настройки."""

    def __init__(self, resolver, pos, funcs):
        super().__init__()
        self._resolver = resolver
        self._pos = pos
        self.update(funcs)

    def __missing__(self, key):
        r = self._resolver.resolve(key, self._pos)
        if not r["ok"]:
            raise Unresolved("cannot resolve %s: %s" % (key, r.get("note")))
        return r["value"]


# Модель проекта

def _flatten_definition_settings(settings, out=None):
    out = {} if out is None else out
    for key, props in settings.items():
        if not isinstance(props, dict):
            continue
        p = {k: v for k, v in props.items() if k != "children"}
        out[key] = p
        _flatten_definition_settings(props.get("children", {}) or {}, out)
    return out


class Container(object):
    def __init__(self, cid, kind, name="", metadata=None, values=None, file=None, definition=None):
        self.id = cid
        self.kind = kind  # user / quality_changes / quality / material / variant / definition_changes / definition
        self.name = name
        self.metadata = metadata or {}
        self.values = values or {}  # ключ -> строка из файла (у материалов — значение Python)
        self.file = file
        self.definition = definition  # у контейнеров экземпляров: id определения
        self.settings = {}  # только у определений: ключ -> свойства

    def to_json(self, with_values=True):
        d = {"id": self.id, "type": self.kind, "name": self.name, "file": self.file}
        if self.definition:
            d["definition"] = self.definition
        md = {k: v for k, v in self.metadata.items() if k not in ("description", "adhesion_info")}
        if md:
            d["metadata"] = md
        if with_values and self.values:
            d["values"] = dict(self.values)
        return d


class Stack(object):
    def __init__(self, sid, name, kind, metadata, container_ids, file):
        self.id = sid
        self.name = name
        self.kind = kind  # global или extruder
        self.metadata = metadata
        self.container_ids = container_ids  # список в порядке STACK_SLOTS
        self.file = file

    @property
    def position(self):
        return self.metadata.get("position")


class Project(object):
    """Разобранный проект 3MF."""

    def __init__(self, path, data=None, fallback_definitions=None):
        self.path = path
        self.files = []
        self.warnings = []
        self.containers = {}  # id -> Container
        self.global_stack = None
        self.extruders = {}  # позиция (строкой) -> Stack
        self.preferences = {}
        self.version = {}
        self.objects = []
        self.unit = "millimeter"
        self.content_types_ok = False
        self.rels_ok = False
        self._fallback_definitions = fallback_definitions or {}
        zf = zipfile.ZipFile(io.BytesIO(data) if data is not None else path, "r")
        with zf:
            self.files = zf.namelist()
            self._read_containers(zf)
            self._read_model(zf)
        self.resolver = SettingResolver(self)

    # Контейнеры
    def _read_containers(self, zf):
        names = self.files
        self.content_types_ok = "[Content_Types].xml" in names
        self.rels_ok = "_rels/.rels" in names
        prefix = None
        for p in ("SteSlicer/", "Cura/"):
            if any(n.startswith(p) for n in names):
                prefix = p
                break
        self.settings_prefix = prefix
        if prefix is None:
            self.warnings.append("no SteSlicer/ (or Cura/) folder: this is a plain mesh 3MF, not a project")
            return
        if prefix != "SteSlicer/":
            self.warnings.append("settings folder is %r, STE Slicer writes 'SteSlicer/'" % prefix)

        for name in names:
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            base = name[len(prefix):]
            raw = zf.read(name)
            text = raw.decode("utf-8", "replace")
            try:
                if base.endswith(".global.cfg") or base.endswith(".extruder.cfg") or base.endswith(".stack.cfg"):
                    self._read_stack(base, text, name)
                elif base.endswith(".inst.cfg"):
                    self._read_instance(base, text, name)
                elif base.endswith(".xml.fdm_material"):
                    self._read_material(base, text, name)
                elif base.endswith(".def.json"):
                    self._read_definition(base, text, name)
                elif base == "preferences.cfg":
                    cp = configparser.ConfigParser(interpolation=None)
                    cp.read_string(text)
                    self.preferences = {s: dict(cp[s]) for s in cp.sections()}
                elif base == "version.ini":
                    cp = configparser.ConfigParser(interpolation=None)
                    cp.read_string(text)
                    self.version = dict(cp["versions"]) if cp.has_section("versions") else {}
            except Exception as e:  # не останавливаемся, но сообщаем
                self.warnings.append("cannot parse %s: %s" % (name, e))

        for pos, ext in self.extruders.items():
            if self.global_stack and ext.metadata.get("machine") not in (None, self.global_stack.id):
                self.warnings.append("extruder %s belongs to machine %r, global stack is %r"
                                     % (pos, ext.metadata.get("machine"), self.global_stack.id))

    def _read_stack(self, base, text, fname):
        cp = configparser.ConfigParser(interpolation=None, empty_lines_in_values=False)
        cp.read_string(text)
        md = dict(cp["metadata"]) if cp.has_section("metadata") else {}
        general = dict(cp["general"]) if cp.has_section("general") else {}
        ids = []
        if cp.has_section("containers"):
            items = sorted(cp["containers"].items(), key=lambda kv: int(kv[0]))
            ids = [v for _, v in items]
        elif general.get("containers"):  # очень старый формат
            ids = [x for x in general["containers"].split(",") if x]
        kind = "global" if (base.endswith(".global.cfg") or md.get("type") == "machine") else "extruder"
        st = Stack(general.get("id", base.split(".")[0]), general.get("name", ""), kind, md, ids, fname)
        if kind == "global":
            if self.global_stack is not None:
                self.warnings.append("more than one global stack (%s, %s)" % (self.global_stack.id, st.id))
            self.global_stack = st
        else:
            pos = md.get("position", str(len(self.extruders)))
            self.extruders[pos] = st

    def _read_instance(self, base, text, fname):
        # Как в InstanceContainer._readAndValidateSerialized: без префиксов комментариев (значение может начинаться с ';')
        cp = configparser.ConfigParser(interpolation=None, comment_prefixes=())
        cp.read_string(text)
        general = dict(cp["general"]) if cp.has_section("general") else {}
        md = dict(cp["metadata"]) if cp.has_section("metadata") else {}
        values = dict(cp["values"]) if cp.has_section("values") else {}
        cid = base[: -len(".inst.cfg")]
        c = Container(cid, md.get("type", "?"), general.get("name", ""), md, values, fname,
                      general.get("definition"))
        self.containers[cid] = c

    def _read_material(self, base, text, fname):
        cid = base[: -len(".xml.fdm_material")]
        root = ET.fromstring(text.encode("utf-8"))
        ns = {"m": NS_MATERIAL}

        def find_text(path):
            el = root.find(path, ns)
            return el.text.strip() if el is not None and el.text else ""

        md = {
            "brand": find_text("m:metadata/m:name/m:brand"),
            "material": find_text("m:metadata/m:name/m:material"),
            "color_name": find_text("m:metadata/m:name/m:color"),
            "label": find_text("m:metadata/m:name/m:label"),
            "GUID": find_text("m:metadata/m:GUID"),
            "version": find_text("m:metadata/m:version"),
            "description": find_text("m:metadata/m:description"),
            "adhesion_info": find_text("m:metadata/m:adhesion_info"),
            "base_file": cid,
            "type": "material",
        }
        values = {}
        for el in root.findall("m:settings/m:setting", ns):  # общие настройки (не привязанные к принтеру)
            key = el.get("key")
            if key in MATERIAL_SETTING_MAP and el.text is not None:
                values[MATERIAL_SETTING_MAP[key]] = el.text.strip()
        diam = find_text("m:properties/m:diameter")
        if diam:
            values["material_diameter"] = diam
        dens = find_text("m:properties/m:density")
        if dens:
            md["density"] = dens
        name = md["label"] or ("%s %s" % (md["brand"], md["material"])).strip()
        c = Container(cid, "material", name, md, values, fname)
        self.containers[cid] = c

    def _read_definition(self, base, text, fname):
        cid = base[: -len(".def.json")]
        data = json.loads(text)
        md = data.get("metadata", {}) or {}
        c = Container(cid, "definition", data.get("name", cid), md, {}, fname)
        c.settings = _flatten_definition_settings(data.get("settings", {}) or {})
        self.containers[cid] = c

    # Работа с контейнерами
    def container(self, cid):
        if cid in self.containers:
            return self.containers[cid]
        if cid in self._fallback_definitions:
            return self._fallback_definitions[cid]
        # id материала бывает вида "<base_file>_<machine>" — сводим к базовому файлу
        for k, c in self.containers.items():
            if c.kind == "material" and cid.startswith(k):
                return c
        return None

    def stack_containers(self, stack):
        """Список (слот, id, Container|None) по приоритету, сверху вниз."""
        res = []
        for i, cid in enumerate(stack.container_ids):
            slot = STACK_SLOTS[i] if i < len(STACK_SLOTS) else "slot%d" % i
            c = None if cid.startswith("empty") else self.container(cid)
            res.append((slot, cid, c))
        return res

    def slot(self, stack, slot):
        if stack is None:
            return None, None
        i = STACK_SLOTS.index(slot)
        if i >= len(stack.container_ids):
            return None, None
        cid = stack.container_ids[i]
        return cid, (None if cid.startswith("empty") else self.container(cid))

    # Объекты и сетки
    def _read_model(self, zf):
        if "3D/3dmodel.model" not in self.files:
            self.warnings.append("3D/3dmodel.model is missing")
            return
        objects = {}
        cur = None
        verts = None
        tris = 0
        build_items = []
        root_unit = None
        with zf.open("3D/3dmodel.model") as fh:
            for event, el in ET.iterparse(fh, events=("start", "end")):
                tag = el.tag.split("}")[-1]
                if event == "start":
                    if tag == "model" and root_unit is None:
                        root_unit = el.get("unit", "millimeter")
                    elif tag == "object":
                        cur = {"id": el.get("id"), "name": el.get("name") or "", "type": el.get("type"),
                               "verts": [], "triangles": 0, "components": [], "settings": {}}
                        verts = cur["verts"]
                        tris = 0
                    continue
                # события конца элемента
                if tag == "vertex" and cur is not None:
                    verts.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                    el.clear()
                elif tag == "triangle" and cur is not None:
                    tris += 1
                    el.clear()
                elif tag == "component" and cur is not None:
                    cur["components"].append((el.get("objectid"), parse_3mf_transform(el.get("transform", ""))))
                elif tag == "metadata" and cur is not None:
                    key = el.get("name", "")
                    if ":" in key:
                        key = key.split(":", 1)[1]
                    cur["settings"][key] = (el.text or "").strip()
                elif tag == "object" and cur is not None:
                    cur["triangles"] = tris
                    objects[cur["id"]] = cur
                    cur = None
                elif tag == "item":
                    build_items.append((el.get("objectid"), parse_3mf_transform(el.get("transform", ""))))
        self.unit = root_unit or "millimeter"
        if self.unit != "millimeter":
            self.warnings.append("model unit is %r (STE Slicer always writes millimeter)" % self.unit)
        for n, (oid, tr) in enumerate(build_items, 1):
            if oid not in objects:
                self.warnings.append("build item refers to missing object %s" % oid)
                continue
            self.objects.append(SceneObject(self, objects, oid, tr, n))

    # Сведения о принтере
    def machine_definition(self):
        if not self.global_stack:
            return None
        cid, c = self.slot(self.global_stack, "definition")
        return cid

    def plate_size(self):
        w = self.resolver.resolve("machine_width")
        d = self.resolver.resolve("machine_depth")
        wv = w["value"] if w["ok"] and isinstance(w["value"], (int, float)) else DEFAULT_MACHINE_WIDTH
        dv = d["value"] if d["ok"] and isinstance(d["value"], (int, float)) else DEFAULT_MACHINE_DEPTH
        return float(wv), float(dv)

    # Сводка
    def summary(self, keys=None, include_all_changes=True):
        keys = list(keys or KEY_SETTINGS)
        r = self.resolver
        out = {"file": os.path.basename(str(self.path)), "archive_files": self.files,
               "warnings": list(self.warnings), "version": self.version}
        gs = self.global_stack
        if gs is None:
            out["machine"] = None
        else:
            def_id = self.machine_definition()
            dc = self.container(def_id) if def_id else None
            q_id, q = self.slot(gs, "quality")
            qc_id, qc = self.slot(gs, "quality_changes")
            out["machine"] = {
                "stack_id": gs.id, "name": gs.name, "definition": def_id,
                "definition_name": dc.name if dc else None,
                "definition_in_archive": bool(dc and dc.id in self.containers),
            }
            out["printing_mode"] = r.resolve("printing_mode")
            out["quality"] = {
                "id": q_id, "name": q.name if q else None,
                "quality_type": (q.metadata.get("quality_type") if q else None)
                or (qc.metadata.get("quality_type") if qc else None),
                "quality_changes_id": qc_id if qc else None,
                "quality_changes_name": qc.name if qc else None,
            }
            out["extruders"] = {}
            for pos in sorted(self.extruders):
                ex = self.extruders[pos]
                m_id, m = self.slot(ex, "material")
                v_id, v = self.slot(ex, "variant")
                eq_id, eq = self.slot(ex, "quality")
                eqc_id, eqc = self.slot(ex, "quality_changes")
                d_id, dd = self.slot(ex, "definition")
                out["extruders"][pos] = {
                    "stack_id": ex.id, "name": ex.name, "enabled": ex.metadata.get("enabled", "True"),
                    "definition": d_id,
                    "material": {"id": m_id, "base_file": m.metadata.get("base_file") if m else None,
                                 "name": m.name if m else None,
                                 "brand": m.metadata.get("brand") if m else None,
                                 "series": m.metadata.get("material") if m else None,
                                 "GUID": m.metadata.get("GUID") if m else None,
                                 "in_archive": m is not None},
                    "variant": v_id,
                    "quality": eq_id,
                    "quality_changes": eqc_id if eqc else None,
                }
            # явные изменения
            changes = {"global": {}, "extruders": {}}
            for slot in ("user", "quality_changes", "definition_changes"):
                cid, c = self.slot(gs, slot)
                if c is not None and c.values:
                    changes["global"][slot] = {"id": cid, "name": c.name, "values": dict(c.values)}
            for pos in sorted(self.extruders):
                ex = self.extruders[pos]
                changes["extruders"][pos] = {}
                for slot in ("user", "quality_changes", "definition_changes"):
                    cid, c = self.slot(ex, slot)
                    if c is not None and c.values:
                        changes["extruders"][pos][slot] = {"id": cid, "name": c.name, "values": dict(c.values)}
            out["changes"] = changes
            # действующие значения
            all_keys = list(keys)
            if include_all_changes:
                for blk in [changes["global"]] + list(changes["extruders"].values()):
                    for slot, data in blk.items():
                        if slot == "definition_changes":
                            continue
                        for k in data["values"]:
                            if k not in all_keys:
                                all_keys.append(k)
            eff = {}
            for k in all_keys:
                g = r.resolve(k, None)
                row = {"global": g}
                for pos in sorted(self.extruders):
                    if r.is_per_extruder(k):
                        row["extruder" + pos] = r.resolve(k, pos)
                eff[k] = row
            out["effective"] = eff
            out["shadowed"] = r.shadowed_user_values()
        w, d = self.plate_size()
        out["plate"] = {"machine_width": w, "machine_depth": d,
                        "plate_center_3mf": [w / 2, d / 2]}
        out["objects"] = [o.summary(w, d) for o in self.objects]
        return out


class SceneObject(object):
    def __init__(self, project, objects, oid, transform, index):
        self.project = project
        self.index = index
        self.object_id = oid
        obj = objects[oid]
        self.name = obj["name"] or "Object %d" % index
        self.transform = transform
        self.settings = dict(obj["settings"])
        self.triangles = 0
        self.local_verts = []  # вершины верхнего объекта (локальные координаты)
        self.world_verts = []
        self.children = []
        self._collect(objects, oid, transform, top=True)

    def _collect(self, objects, oid, tr, top=False, depth=0):
        if depth > 16:
            return
        obj = objects.get(oid)
        if obj is None:
            return
        self.triangles += obj["triangles"]
        lin, t = tr
        for v in obj["verts"]:
            self.world_verts.append(tuple(lin[i][0] * v[0] + lin[i][1] * v[1] + lin[i][2] * v[2] + t[i]
                                          for i in range(3)))
        if top:
            self.local_verts = obj["verts"]
        else:
            self.children.append({"object_id": oid, "triangles": obj["triangles"], "settings": obj["settings"]})
        for cid, ctr in obj["components"]:
            self._collect(objects, cid, compose(tr, ctr), depth=depth + 1)

    @staticmethod
    def _bbox(verts):
        if not verts:
            return None
        mn = [min(v[i] for v in verts) for i in range(3)]
        mx = [max(v[i] for v in verts) for i in range(3)]
        return mn, mx

    def world_bbox(self):
        return self._bbox(self.world_verts)

    def local_bbox(self):
        return self._bbox(self.local_verts)

    def rotation_vs_source(self):
        """R_print = M_lin * F^-1: поворот детали относительно осей исходного STL."""
        return mat_mul3(self.transform[0], F_INV)

    def up_axis_in_source(self):
        """Какое направление исходного STL смотрит в +Z принтера (единичный вектор в координатах STL)."""
        r = self.rotation_vs_source()
        info = rotation_info(r)
        rot = info["rotation"]
        return [rot[2][0], rot[2][1], rot[2][2]]

    def map_source_point(self, p_src, src_bbox_center):
        """Переводит точку из координат исходного STL в мировые координаты 3MF, считая, что сетка в 3MF —
        это сетка STL, центрированная по габаритам (так её загружает STE Slicer)."""
        lb = self.local_bbox()
        lc = [(a + b) / 2 for a, b in zip(*lb)] if lb else [0, 0, 0]
        rel = [p - c for p, c in zip(p_src, src_bbox_center)]
        local = [x + y for x, y in zip(mat_vec3(F_INV, rel), lc)]
        lin, t = self.transform
        return [x + y for x, y in zip(mat_vec3(lin, local), t)]

    def map_source_dir(self, d_src):
        return mat_vec3(self.transform[0], mat_vec3(F_INV, d_src))

    def source_extents_from_local(self):
        """Габариты локальной сетки обратно в осях исходного STL (по модулю)."""
        lb = self.local_bbox()
        if not lb:
            return None
        ext_local = [b - a for a, b in zip(*lb)]
        # local = F_INV * src  ->  src = F * local
        e = mat_vec3(F_SCENE_TO_3MF, ext_local)
        return [abs(x) for x in e]

    def ellipse_ratio(self, w, d):
        """Максимум (x/a)^2 + (y/b)^2 по вершинам относительно центра стола (рабочая зона 5D — эллипс)."""
        a, b = w / 2, d / 2
        if not self.world_verts:
            return None
        return max(((v[0] - a) / a) ** 2 + ((v[1] - b) / b) ** 2 for v in self.world_verts)

    def summary(self, w, d):
        wb = self.world_bbox()
        lb = self.local_bbox()
        lin, t = self.transform
        rinfo = rotation_info(lin)
        rsrc = rotation_info(self.rotation_vs_source())
        up = self.up_axis_in_source()
        up_name, up_dev = nearest_axis_name(up)
        res = {
            "index": self.index, "name": self.name, "object_id": self.object_id,
            "triangles": self.triangles, "vertices": len(self.world_verts),
            "transform_3mf": {"matrix": [[round(x, 6) for x in row] + [round(t[i], 4)]
                                         for i, row in enumerate(lin)]},
            "transform_decomposed": rinfo,
            "rotation_vs_source_stl": rsrc,
            "source_axis_pointing_up": {"vector": [round(x, 5) for x in up], "nearest": up_name,
                                        "deviation_deg": up_dev},
            "per_object_settings": self.settings,
        }
        if self.children:
            res["children"] = self.children
        if lb:
            res["local_bbox"] = {"min": [round(x, 3) for x in lb[0]], "max": [round(x, 3) for x in lb[1]],
                                 "size": [round(b - a, 3) for a, b in zip(*lb)],
                                 "center": [round((a + b) / 2, 3) for a, b in zip(*lb)]}
        if wb:
            mn, mx = wb
            c = [(a + b) / 2 for a, b in zip(mn, mx)]
            res["world_bbox_3mf"] = {"min": [round(x, 3) for x in mn], "max": [round(x, 3) for x in mx],
                                     "size": [round(b - a, 3) for a, b in zip(mn, mx)],
                                     "center": [round(x, 3) for x in c]}
            res["position"] = {
                "center_rel_plate_center_xy": [round(c[0] - w / 2, 3), round(c[1] - d / 2, 3)],
                "bottom_z": round(mn[2], 3),
                "translation_rel_plate_center": [round(t[0] - w / 2, 3), round(t[1] - d / 2, 3), round(t[2], 3)],
                "ellipse_ratio_5d": round(self.ellipse_ratio(w, d), 4),
            }
        return res


# Поиск значений (приоритет стеков контейнеров)

class SettingResolver(object):
    """Повторяет поиск значений GlobalStack / ExtruderStack, но без слайсера.

    Порядок в стеке (верхний побеждает): user, quality_changes, quality, material, variant,
    definition_changes, definition. ExtruderStack проваливается в глобальный стек; ключ с
    settable_per_extruder=False, запрошенный у экструдера, берётся из глобального; глобальный
    стек для ключей со свойством 'resolve' отвечает формулой resolve (GlobalStack.getProperty)."""

    def __init__(self, project):
        self.p = project
        self._cache = {}
        self._busy = set()
        self._resolving = set()

    # Определения
    def _defs_for(self, pos):
        res = []
        if pos is not None and pos in self.p.extruders:
            _, c = self.p.slot(self.p.extruders[pos], "definition")
            if c is not None:
                res.append(c)
        if self.p.global_stack is not None:
            _, c = self.p.slot(self.p.global_stack, "definition")
            if c is not None:
                res.append(c)
        return res

    def prop(self, key, name, pos=None):
        for d in self._defs_for(pos):
            if key in d.settings and name in d.settings[key]:
                return d.settings[key][name]
        return None

    def setting_type(self, key, pos=None):
        return self.prop(key, "type", pos)

    def known(self, key):
        return any(key in d.settings for d in self._defs_for("0") + self._defs_for(None))

    def is_per_extruder(self, key):
        v = self.prop(key, "settable_per_extruder")
        if v is None:
            return True if self.known(key) else False
        return convert_value(v, "bool")

    # Цепочка контейнеров
    def _chain(self, pos):
        chain = []
        if pos is not None and pos in self.p.extruders:
            ex = self.p.extruders[pos]
            for slot, cid, c in self.p.stack_containers(ex):
                chain.append(("extruder%s.%s" % (pos, slot), cid, c))
        if self.p.global_stack is not None:
            for slot, cid, c in self.p.stack_containers(self.p.global_stack):
                chain.append(("global.%s" % slot, cid, c))
        return chain

    def raw_lookup(self, key, pos=None):
        """Первый контейнер в цепочке, где задано значение. Возвращает (источник, id контейнера, сырое значение, вид)."""
        for label, cid, c in self._chain(pos):
            if c is None:
                continue
            if c.kind == "definition":
                props = c.settings.get(key)
                if props is None:
                    continue
                if "value" in props and props["value"] not in (None, "None"):
                    return label, cid, props["value"], "definition.value"
                if "default_value" in props:
                    return label, cid, props["default_value"], "definition.default_value"
                continue
            if key in c.values:
                return label, cid, c.values[key], c.kind
        return None, None, None, None

    def resolve(self, key, pos=None):
        if pos is not None and pos not in self.p.extruders:
            pos = None
        ck = (key, pos, key in self._resolving)
        if ck in self._cache:
            return self._cache[ck]
        if ck in self._busy:
            return {"ok": False, "value": None, "note": "recursion", "source": None}
        self._busy.add(ck)
        try:
            res = self._resolve(key, pos)
        finally:
            self._busy.discard(ck)
        self._cache[ck] = res
        return res

    def _resolve(self, key, pos):
        if pos is not None and not self.is_per_extruder(key):
            r = dict(self.resolve(key, None))
            r["note"] = (r.get("note") or "") + " (settable_per_extruder=False -> global)"
            return r
        if pos is None and key not in self._resolving:
            resolve_f = self.prop(key, "resolve")
            src, _, _, kind = self.raw_lookup(key, None)
            # GlobalStack._shouldResolve: явно заданное значение (state != Default) важнее 'resolve'
            explicitly_set = kind is not None and not kind.startswith("definition")
            if resolve_f not in (None, "None", "") and not explicitly_set:
                self._resolving.add(key)
                try:
                    v = self.evaluate(resolve_f, None)
                    return {"ok": True, "value": self._coerce(key, v), "source": "global.resolve",
                            "raw": resolve_f, "note": "GlobalStack 'resolve' formula"}
                except Unresolved as e:
                    return {"ok": False, "value": None, "source": "global.resolve", "raw": resolve_f,
                            "note": str(e)}
                finally:
                    self._resolving.discard(key)
        lte = self.prop(key, "limit_to_extruder")
        if lte not in (None, "None", "", "=-1", "-1") and self.is_per_extruder(key):
            try:
                n = self.evaluate(lte, pos)
                if isinstance(n, (int, float)) and not isinstance(n, bool) and int(n) == -1:
                    n_s = self._functions(pos)["defaultExtruderPosition"]()  # -1 -> экструдер по умолчанию
                else:
                    n_s = str(n).strip()
                if n_s != "-1" and n_s in self.p.extruders and n_s != pos:
                    r = dict(self.resolve(key, n_s))
                    r["note"] = (r.get("note") or "") + " (limit_to_extruder=%s)" % n_s
                    return r
            except (Unresolved, ValueError, TypeError):
                pass
        source, cid, raw, kind = self.raw_lookup(key, pos)
        if source is None:
            return {"ok": False, "value": None, "source": None, "note": "unknown setting"}
        res = {"source": source, "container": cid, "raw": raw}
        if isinstance(raw, str) and (raw.startswith("=") or kind == "definition.value"):
            try:
                v = self.evaluate(raw, pos)
                res.update(ok=True, value=self._coerce(key, v), note="formula")
            except Unresolved as e:
                res.update(ok=False, value=None, note="formula not evaluated: %s" % e)
            return res
        res.update(ok=True, value=convert_value(raw, self.setting_type(key, pos)), note=kind)
        return res

    def resolve_raw(self, key, pos):
        """Как SteSlicerFormulaFunctions.getValueInExtruder: extruder.getRawProperty(key) (свои контейнеры, потом
        глобальный стек, без settable_per_extruder и resolve), значения для формулы берутся у экструдера."""
        ck = ("raw", key, pos)
        if ck in self._cache:
            return self._cache[ck]
        if ck in self._busy:
            raise Unresolved("recursion in %s" % key)
        self._busy.add(ck)
        try:
            source, cid, raw, kind = self.raw_lookup(key, pos)
            if source is None:
                raise Unresolved("unknown setting %s" % key)
            if isinstance(raw, str) and (raw.startswith("=") or kind == "definition.value"):
                v = self._coerce(key, self.evaluate(raw, pos))
            else:
                v = convert_value(raw, self.setting_type(key, pos))
        finally:
            self._busy.discard(ck)
        self._cache[ck] = v
        return v

    def _coerce(self, key, v):
        t = self.setting_type(key)
        try:
            if t == "float" and isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            if t == "int" and isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(round(v))
            if t in ("extruder", "optional_extruder") and isinstance(v, (int, float)):
                return str(int(v))
        except Exception:
            pass
        return v

    # Формулы
    def _functions(self, pos):
        p = self.p

        def enabled_positions():
            res = []
            cnt = self.resolve("machine_extruder_count", None)
            n = cnt["value"] if cnt.get("ok") else len(p.extruders)
            for k in sorted(p.extruders, key=lambda x: int(x)):
                if str(p.extruders[k].metadata.get("enabled", "True")) != "True":
                    continue
                if int(k) >= int(n):
                    continue
                res.append(k)
            return res

        def default_pos():
            ep = enabled_positions()
            return ep[0] if ep else "0"

        def extruder_value(position, key):
            position = str(int(position))
            if position == "-1":
                position = default_pos()
            return self.resolve_raw(key, position)

        def extruder_values(key):
            vals = []
            for k in enabled_positions():
                vals.append(self.resolve_raw(key, k))
            if not vals:
                r = self.resolve(key, None)
                if not r["ok"]:
                    raise Unresolved("extruderValues(%s)" % key)
                vals.append(r["value"])
            return vals

        def resolve_or_value(key):
            r = self.resolve(key, None)
            if not r["ok"]:
                raise Unresolved("resolveOrValue(%s)" % key)
            return r["value"]

        def any_extruder_with_material(material_key):
            for k in enabled_positions():
                _, m = p.slot(p.extruders[k], "material")
                if m is not None and str(m.metadata.get(material_key, "")).lower() in ("true", "1", "yes"):
                    return True
            return False

        def any_extruder_nr_with_or_default(key):
            for k in enabled_positions():
                r = self.resolve(key, k)
                if r["ok"] and r["value"]:
                    return int(k)
            return int(default_pos())

        def unsupported(*a, **kw):
            raise Unresolved("valueFromContainer is not supported offline")

        funcs = dict(_SAFE_BUILTINS)
        funcs.update({
            "math": math,
            "extruderValue": extruder_value,
            "extruderValues": extruder_values,
            "resolveOrValue": resolve_or_value,
            "defaultExtruderPosition": default_pos,
            "anyExtruderWithMaterial": any_extruder_with_material,
            "anyExtruderNrWithOrDefault": any_extruder_nr_with_or_default,
            "valueFromContainer": unsupported,
            "extruderValueFromContainer": unsupported,
            "defaultValueInExtruder": unsupported,
        })
        return funcs

    def evaluate(self, formula, pos):
        code = formula[1:] if formula.startswith("=") else formula
        code = code.strip()
        try:
            tree = ast.parse(code, mode="eval")
        except SyntaxError as e:
            raise Unresolved("syntax: %s" % e)
        _check_ast(tree)
        names = _Names(self, pos, self._functions(pos))
        try:
            return eval(compile(tree, "<setting>", "eval"), {"__builtins__": {}}, names)
        except Unresolved:
            raise
        except Exception as e:
            raise Unresolved("%s: %s" % (type(e).__name__, e))

    # Диагностика
    def shadowed_user_values(self):
        """Глобальные значения userChanges, которые не действуют, потому что их перекрывает контейнер экструдера."""
        res = []
        gs = self.p.global_stack
        if gs is None:
            return res
        _, user = self.p.slot(gs, "user")
        if user is None:
            return res
        for key, raw in user.values.items():
            if not self.is_per_extruder(key):
                continue
            for pos in sorted(self.p.extruders):
                src, cid, eraw, kind = self.raw_lookup(key, pos)
                if src and src.startswith("extruder"):
                    res.append({"key": key, "global_user_value": raw, "extruder": pos,
                                "winning_container": "%s (%s)" % (src, cid), "winning_raw": eraw})
        return res


def load_project(path, fallback_definitions=None):
    return Project(path, fallback_definitions=fallback_definitions)


# Определения из файлов слайсера (запасной вариант и генерация фикстур)

def _merge(first, second):
    import copy
    result = copy.deepcopy(first)
    for k, v in second.items():
        if k in result and isinstance(v, dict) and isinstance(result[k], dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def _find_in(d, key):
    if key in d:
        return d[key]
    for v in d.values():
        if isinstance(v, dict):
            r = _find_in(v, key)
            if r is not None:
                return r
    return None


def find_resources_dir(start=None):
    """Ищет папку resources слайсера: переменная STE_RESOURCES, установленный STE Slicer, исходники рядом."""
    env = os.environ.get("STE_RESOURCES")
    if env and os.path.isdir(env):
        return env
    installed = r"C:\Program Files\Stereotech STE Slicer 7.10\resources"
    if os.path.isdir(os.path.join(installed, "definitions")):
        return installed
    here = os.path.abspath(start or os.path.dirname(__file__))
    for _ in range(6):
        cand = os.path.join(here, "STE-Slicer-master", "resources")
        if os.path.isdir(cand):
            return cand
        here = os.path.dirname(here)
    return None


def resolve_definition_json(def_id, resources_dir):
    """Как DefinitionContainer._resolveInheritance + _preprocessParsedJson в Uranium."""
    def load(i):
        for sub in ("definitions", "extruders"):
            fp = os.path.join(resources_dir, sub, i + ".def.json")
            if os.path.isfile(fp):
                with open(fp, encoding="utf-8") as f:
                    return json.load(f, object_pairs_hook=__import__("collections").OrderedDict)
        raise FileNotFoundError(i)

    def rec(i):
        d = load(i)
        if "inherits" in d:
            d = _merge(rec(d["inherits"]), d)
        return d

    parsed = rec(def_id)
    for key, value in (parsed.get("overrides") or {}).items():
        s = _find_in(parsed["settings"], key)
        if s is not None:
            s.update(value)
    return parsed


FUNCTION_PROPS = ("value", "enabled", "resolve", "limit_to_extruder", "minimum_value", "maximum_value",
                  "minimum_value_warning", "maximum_value_warning")


def serialize_definition(parsed):
    """Повторяет DefinitionContainer.serialize(): {"name", "version", "metadata", "settings"}, где каждая настройка —
    {"label", "children", <свойство>: str(значение)}, а свойства-функции записаны как "=<код>"."""
    def ser(props):
        out = {"label": str(props.get("label", "")), "children": {}}
        for ck, cv in (props.get("children") or {}).items():
            out["children"][ck] = ser(cv)
        for pk, pv in props.items():
            if pk == "children":
                continue
            out[pk] = "=" + str(pv) if pk in FUNCTION_PROPS else str(pv)
        return out

    md = dict(parsed.get("metadata", {}))
    return {"name": parsed.get("name"), "version": 2, "metadata": md,
            "settings": {k: ser(v) for k, v in parsed.get("settings", {}).items()}}


def load_fallback_definitions(resources_dir, ids=("hte530v5.2.4", "stereotech_v5.2.4_extruder_main",
                                                  "stereotech_v5.2.4_extruder_second")):
    """Контейнеры определений из файлов слайсера — на случай, если в 3MF нет *.def.json."""
    res = {}
    for i in ids:
        try:
            ser = serialize_definition(resolve_definition_json(i, resources_dir))
        except FileNotFoundError:
            continue
        c = Container(i, "definition", ser.get("name", i), ser.get("metadata", {}), {}, None)
        c.settings = _flatten_definition_settings(ser["settings"])
        res[i] = c
    return res
