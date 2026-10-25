"""Чтение проекта 3MF из STE Slicer без слайсера (только stdlib, Python 3.5+).

Урезанная версия devtools/ste3mf.py (там же подробно разобрано устройство файла и вывод
систем координат):

* стеки контейнеров (SteSlicer/*.global.cfg, *.extruder.cfg), контейнеры экземпляров
  (*.inst.cfg), материалы (*.xml.fdm_material) и полные определения принтера и
  экструдеров (*.def.json), которые лежат в архиве;
* действующие значения настроек с приоритетом стеков, как в слайсере (user >
  quality_changes > quality > material > variant > definition_changes > definition;
  сначала стек экструдера, потом глобальный; settable_per_extruder, limit_to_extruder и
  глобальные формулы 'resolve'); формулы считает ограниченный вычислитель;
* объекты: габариты в мировых координатах (система 3MF: Z вверх, начало — передний левый
  угол стола, центр стола = (W/2, D/2) = ось вращения 5D) и поворот детали относительно
  исходного STL.

Если в архиве нет *.def.json (например, проект записан тестовым моком), определения
берём из справочника настроек плагина: формулы работают, но 'resolve' и
limit_to_extruder там неизвестны, поэтому такие значения помечены как приблизительные
(`Project.definitions_source == "catalog"`).
"""

import ast
import configparser
import json
import math
import sys
import xml.etree.ElementTree as ET
import zipfile

NS_MATERIAL = "http://www.ultimaker.com/material"
STACK_SLOTS = ("user", "quality_changes", "quality", "material", "variant", "definition_changes", "definition")
DEFAULT_MACHINE_WIDTH = 351.0
DEFAULT_MACHINE_DEPTH = 300.3

# XmlMaterialProfile.__material_settings_setting_map (common, not machine specific settings)
MATERIAL_SETTING_MAP = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
}

# F: сцена (Y вверх) -> 3MF (Z вверх): (x, y, z) -> (x, -z, y); F^-1 — то, как STLReader переводит STL в сцену.
F_SCENE_TO_3MF = ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))
F_INV = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))
IDENTITY = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), (0.0, 0.0, 0.0))


class ProjectError(Exception):
    """Файл не читается как 3MF."""


class Unresolved(Exception):
    """Формулу не получилось вычислить без слайсера."""


# Немного линейной алгебры

def _mul3(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _vec3(a, v):
    return [a[i][0] * v[0] + a[i][1] * v[1] + a[i][2] * v[2] for i in range(3)]


def _det3(a):
    return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))


def _compose(a, b):
    lin = _mul3(a[0], b[0])
    return lin, [x + y for x, y in zip(_vec3(a[0], b[1]), a[1])]


def parse_3mf_transform(text):
    """3MF "m00 m01 m02 m10 ... m32" (строки-векторы) -> (lin, t) для векторов-столбцов."""
    if not text or not text.strip():
        return IDENTITY
    v = [float(x) for x in text.split()]
    if len(v) != 12:
        raise ValueError("bad 3MF transform: %r" % text)
    return [[v[0], v[3], v[6]], [v[1], v[4], v[7]], [v[2], v[5], v[8]]], [v[9], v[10], v[11]]


def rotation_info(lin):
    """Масштаб по столбцам, признак отражения, чистый поворот и внешние углы Эйлера XYZ (градусы)."""
    scales = [math.sqrt(sum(lin[r][c] ** 2 for r in range(3))) for c in range(3)]
    rot = [[lin[r][c] / (scales[c] or 1.0) for c in range(3)] for r in range(3)]
    mirrored = _det3(lin) < 0
    if mirrored:
        rot = [[-x for x in row] for row in rot]
    sy = max(-1.0, min(1.0, -rot[2][0]))
    ry = math.degrees(math.asin(sy))
    if abs(sy) < 0.999999:
        rx = math.degrees(math.atan2(rot[2][1], rot[2][2]))
        rz = math.degrees(math.atan2(rot[1][0], rot[0][0]))
    else:
        rx = math.degrees(math.atan2(-rot[1][2], rot[1][1]))
        rz = 0.0
    return {"scale": [round(s, 4) for s in scales], "mirrored": mirrored, "rotation": rot,
            "euler_xyz_deg": [round(a, 1) + 0.0 for a in (rx, ry, rz)]}


_AXES = (((1, 0, 0), "+X"), ((-1, 0, 0), "-X"), ((0, 1, 0), "+Y"), ((0, -1, 0), "-Y"),
         ((0, 0, 1), "+Z"), ((0, 0, -1), "-Z"))


def nearest_axis_name(v):
    dot, name = max((sum(a * b for a, b in zip(v, vec)), nm) for vec, nm in _AXES)
    return name, round(math.degrees(math.acos(max(-1.0, min(1.0, dot)))), 1)


# Значения
def convert_value(raw, setting_type=None):
    """Значение из файла (строка) -> значение Python по типу настройки."""
    if not isinstance(raw, str):
        return raw
    s = raw.strip()
    if setting_type == "bool":
        return s.lower() in ("true", "1", "yes")
    if setting_type in ("float", "int"):
        try:
            return float(s) if setting_type == "float" else int(float(s))
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
    """Нестрогое сравнение для ожидаемых значений (числа с допуском, логические, строки)."""
    if isinstance(a, bool) or isinstance(b, bool):
        def as_bool(x):
            if isinstance(x, bool):
                return x
            text = str(x).strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off", "none", ""):
                return False
            return None
        return as_bool(a) is not None and as_bool(a) == as_bool(b)
    try:
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_tol)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


# Ограниченное вычисление формул

_NODE_NAMES = ["Expression", "BoolOp", "BinOp", "UnaryOp", "IfExp", "Compare", "Call", "Name", "Load",
               "Attribute", "Subscript", "Index", "Slice", "List", "Tuple", "Dict", "Set", "ListComp",
               "GeneratorExp", "comprehension", "Store", "keyword", "And", "Or", "Not", "Add", "Sub", "Mult",
               "Div", "FloorDiv", "Mod", "Pow", "USub", "UAdd", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "In",
               "NotIn", "Is", "IsNot", "Constant"]
if sys.version_info < (3, 8):
    _NODE_NAMES += ["Num", "Str", "NameConstant"]
_ALLOWED_NODES = tuple(getattr(ast, n) for n in _NODE_NAMES if hasattr(ast, n))

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
            if not (isinstance(node.value, ast.Name) and node.value.id == "math") or node.attr.startswith("_"):
                raise Unresolved("attribute access")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise Unresolved("dunder name")


class _Names(dict):
    """Пространство имён, в котором неизвестные имена ищутся как настройки."""

    def __init__(self, resolver, pos, funcs):
        dict.__init__(self, funcs)
        self._resolver = resolver
        self._pos = pos

    def __missing__(self, key):
        r = self._resolver.resolve(key, self._pos)
        if not r["ok"]:
            raise Unresolved("cannot resolve %s: %s" % (key, r.get("note")))
        return r["value"]


# Контейнеры
class Container(object):
    def __init__(self, cid, kind, name="", metadata=None, values=None):
        self.id = cid
        self.kind = kind
        self.name = name
        self.metadata = metadata or {}
        self.values = values or {}
        self.settings = {}  # только у определений: ключ -> свойства


class Stack(object):
    def __init__(self, sid, name, metadata, container_ids):
        self.id = sid
        self.name = name
        self.metadata = metadata
        self.container_ids = container_ids


def _flatten_settings(settings, out):
    for key, props in settings.items():
        if isinstance(props, dict):
            out[key] = dict((k, v) for k, v in props.items() if k != "children")
            _flatten_settings(props.get("children") or {}, out)
    return out


def _cfg(text, comments=True):
    cp = configparser.ConfigParser(interpolation=None, comment_prefixes=("#", ";") if comments else ())
    cp.read_string(text)
    return lambda section: dict(cp[section]) if cp.has_section(section) else {}


def catalog_definition(catalog_settings):
    """Псевдоконтейнер определения из записей settings_catalog.json (запасной вариант)."""
    c = Container("catalog", "definition", "plugin settings catalog")
    for key, entry in catalog_settings.items():
        props = {"type": entry.get("type"), "default_value": entry.get("default"),
                 "settable_per_extruder": bool(entry.get("per_extruder"))}
        if entry.get("formula"):
            props["value"] = entry["formula"]
        c.settings[key] = props
    return c


class Project(object):
    """Разобранный проект 3MF. `fallback_definition` — контейнер на случай, если в архиве нет *.def.json."""

    def __init__(self, path, fallback_definition=None):
        self.path = path
        self.warnings = []
        self.containers = {}
        self.global_stack = None
        self.extruders = {}
        self.objects = []
        self.version = {}
        self._fallback = fallback_definition
        try:
            archive = zipfile.ZipFile(path, "r")
        except (zipfile.BadZipFile, OSError, IOError) as exc:
            raise ProjectError("не удалось открыть 3MF (zip): %s" % exc)
        with archive:
            self.files = archive.namelist()
            self._read_containers(archive)
            self._read_model(archive)
        self.definitions_source = "archive"
        def_id = self.machine_definition()
        if def_id and def_id not in self.containers:
            self.definitions_source = "catalog" if fallback_definition is not None else "none"
        self.resolver = SettingResolver(self)

    # Контейнеры
    def _read_containers(self, archive):
        prefix = None
        for candidate in ("SteSlicer/", "Cura/"):
            if any(n.startswith(candidate) for n in self.files):
                prefix = candidate
                break
        if prefix is None:
            self.warnings.append("в 3MF нет папки SteSlicer/ — это 3MF только с сеткой, без настроек слайсера")
            return
        for name in self.files:
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            base = name[len(prefix):]
            text = archive.read(name).decode("utf-8", "replace")
            try:
                if base.endswith((".global.cfg", ".extruder.cfg", ".stack.cfg")):
                    self._read_stack(base, text)
                elif base.endswith(".inst.cfg"):
                    sec = _cfg(text, comments=False)
                    general, md = sec("general"), sec("metadata")
                    cid = base[:-len(".inst.cfg")]
                    self.containers[cid] = Container(cid, md.get("type", "?"), general.get("name", ""), md,
                                                     sec("values"))
                elif base.endswith(".xml.fdm_material"):
                    self._read_material(base[:-len(".xml.fdm_material")], text)
                elif base.endswith(".def.json"):
                    cid = base[:-len(".def.json")]
                    data = json.loads(text)
                    c = Container(cid, "definition", data.get("name", cid), data.get("metadata") or {})
                    c.settings = _flatten_settings(data.get("settings") or {}, {})
                    self.containers[cid] = c
                elif base == "version.ini":
                    self.version = _cfg(text)("versions")
            except Exception as exc:
                self.warnings.append("не удалось разобрать %s: %s" % (name, exc))

    def _read_stack(self, base, text):
        sec = _cfg(text)
        md, general = sec("metadata"), sec("general")
        items = sorted(sec("containers").items(), key=lambda kv: int(kv[0]))
        stack = Stack(general.get("id", base.split(".")[0]), general.get("name", ""), md, [v for _k, v in items])
        if base.endswith(".global.cfg") or md.get("type") == "machine":
            self.global_stack = stack
        else:
            self.extruders[md.get("position", str(len(self.extruders)))] = stack

    def _read_material(self, cid, text):
        root = ET.fromstring(text.encode("utf-8"))
        ns = {"m": NS_MATERIAL}

        def find(path):
            el = root.find(path, ns)
            return el.text.strip() if el is not None and el.text else ""

        md = {"brand": find("m:metadata/m:name/m:brand"), "material": find("m:metadata/m:name/m:material"),
              "label": find("m:metadata/m:name/m:label"), "GUID": find("m:metadata/m:GUID"), "base_file": cid}
        values = {}
        for el in root.findall("m:settings/m:setting", ns):
            key = el.get("key")
            if key in MATERIAL_SETTING_MAP and el.text is not None:
                values[MATERIAL_SETTING_MAP[key]] = el.text.strip()
        if find("m:properties/m:diameter"):
            values["material_diameter"] = find("m:properties/m:diameter")
        name = md["label"] or ("%s %s" % (md["brand"], md["material"])).strip()
        self.containers[cid] = Container(cid, "material", name, md, values)

    def container(self, cid):
        if cid in self.containers:
            return self.containers[cid]
        for key, c in self.containers.items():  # id материалов вида "<base_file>_<machine>"
            if c.kind == "material" and cid.startswith(key):
                return c
        return None

    def slot(self, stack, slot):
        """(id контейнера, Container или None) для слота стека."""
        if stack is None:
            return None, None
        i = STACK_SLOTS.index(slot)
        if i >= len(stack.container_ids):
            return None, None
        cid = stack.container_ids[i]
        if cid.startswith("empty"):
            return cid, None
        c = self.container(cid)
        if c is None and slot == "definition" and self._fallback is not None:
            c = self._fallback
        return cid, c

    def stack_containers(self, stack):
        return [(STACK_SLOTS[i] if i < len(STACK_SLOTS) else "slot%d" % i,) + self.slot(stack, STACK_SLOTS[i])
                for i in range(min(len(stack.container_ids), len(STACK_SLOTS)))]

    def machine_definition(self):
        return self.slot(self.global_stack, "definition")[0] if self.global_stack else None

    def plate_size(self):
        result = []
        for key, default in (("machine_width", DEFAULT_MACHINE_WIDTH), ("machine_depth", DEFAULT_MACHINE_DEPTH)):
            r = self.resolver.resolve(key)
            ok = r["ok"] and isinstance(r["value"], (int, float)) and not isinstance(r["value"], bool)
            result.append(float(r["value"]) if ok else default)
        return tuple(result)

    # Объекты и сетки
    def _read_model(self, archive):
        if "3D/3dmodel.model" not in self.files:
            self.warnings.append("в 3MF нет 3D/3dmodel.model")
            return
        objects = {}
        items = []
        cur = None
        with archive.open("3D/3dmodel.model") as handle:
            for event, el in ET.iterparse(handle, events=("start", "end")):
                tag = el.tag.split("}")[-1]
                if event == "start":
                    if tag == "object":
                        cur = {"id": el.get("id"), "name": el.get("name") or "", "verts": [], "triangles": 0,
                               "components": [], "settings": {}}
                    continue
                if cur is not None and tag == "vertex":
                    cur["verts"].append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                    el.clear()
                elif cur is not None and tag == "triangle":
                    cur["triangles"] += 1
                    el.clear()
                elif cur is not None and tag == "component":
                    cur["components"].append((el.get("objectid"), parse_3mf_transform(el.get("transform", ""))))
                elif cur is not None and tag == "metadata":
                    key = el.get("name", "")
                    cur["settings"][key.split(":", 1)[-1]] = (el.text or "").strip()
                elif cur is not None and tag == "object":
                    objects[cur["id"]] = cur
                    cur = None
                elif tag == "item":
                    items.append((el.get("objectid"), parse_3mf_transform(el.get("transform", ""))))
        for index, (oid, transform) in enumerate(items, 1):
            if oid in objects:
                self.objects.append(SceneObject(objects, oid, transform, index))
            else:
                self.warnings.append("элемент сборки ссылается на отсутствующий объект %s" % oid)


class SceneObject(object):
    def __init__(self, objects, oid, transform, index):
        obj = objects[oid]
        self.index = index
        self.name = obj["name"] or "Object %d" % index
        self.transform = transform
        self.settings = dict(obj["settings"])
        self.triangles = 0
        self.world_verts = []
        self._collect(objects, oid, transform, 0)

    def _collect(self, objects, oid, tr, depth):
        obj = objects.get(oid)
        if obj is None or depth > 16:
            return
        self.triangles += obj["triangles"]
        lin, t = tr
        for v in obj["verts"]:
            self.world_verts.append(tuple(lin[i][0] * v[0] + lin[i][1] * v[1] + lin[i][2] * v[2] + t[i]
                                          for i in range(3)))
        if depth:
            for key, value in obj["settings"].items():
                self.settings.setdefault(key, value)
        for cid, ctr in obj["components"]:
            self._collect(objects, cid, _compose(tr, ctr), depth + 1)

    def world_bbox(self):
        if not self.world_verts:
            return None
        return ([min(v[i] for v in self.world_verts) for i in range(3)],
                [max(v[i] for v in self.world_verts) for i in range(3)])

    def rotation_vs_source(self):
        """R = M_lin * F^-1: поворот детали относительно осей исходного STL (система принтера)."""
        return rotation_info(_mul3(self.transform[0], F_INV))

    def ellipse_ratio(self, w, d):
        a, b = w / 2.0, d / 2.0
        return max(((v[0] - a) / a) ** 2 + ((v[1] - b) / b) ** 2 for v in self.world_verts) \
            if self.world_verts else None


# Вычисление действующих значений

class SettingResolver(object):
    """Поиск значений как в GlobalStack / ExtruderStack, без слайсера (см. описание модуля)."""

    def __init__(self, project):
        self.p = project
        self._cache = {}
        self._busy = set()
        self._resolving = set()

    def _defs_for(self, pos):
        res = []
        if pos is not None and pos in self.p.extruders:
            c = self.p.slot(self.p.extruders[pos], "definition")[1]
            if c is not None:
                res.append(c)
        c = self.p.slot(self.p.global_stack, "definition")[1]
        if c is not None:
            res.append(c)
        return res

    def prop(self, key, name, pos=None):
        for d in self._defs_for(pos):
            if key in d.settings and name in d.settings[key]:
                return d.settings[key][name]
        return None

    def known(self, key):
        return any(key in d.settings for d in self._defs_for("0"))

    def is_per_extruder(self, key):
        v = self.prop(key, "settable_per_extruder")
        if v is None:
            # без определений лучшая догадка — цепочка экструдера (она сама проваливается в глобальную)
            return self.known(key) or not self._defs_for("0")
        return convert_value(v, "bool")

    def _chain(self, pos):
        chain = []
        if pos is not None and pos in self.p.extruders:
            for slot, cid, c in self.p.stack_containers(self.p.extruders[pos]):
                chain.append(("extruder%s.%s" % (pos, slot), cid, c))
        if self.p.global_stack is not None:
            for slot, cid, c in self.p.stack_containers(self.p.global_stack):
                chain.append(("global.%s" % slot, cid, c))
        return chain

    def raw_lookup(self, key, pos=None):
        """Первый контейнер, где задано значение: (источник, id контейнера, сырое значение, вид)."""
        for label, cid, c in self._chain(pos):
            if c is None:
                continue
            if c.kind == "definition":
                props = c.settings.get(key)
                if props is None:
                    continue
                if props.get("value") not in (None, "None"):
                    return label, cid, props["value"], "definition.value"
                if "default_value" in props:
                    return label, cid, props["default_value"], "definition.default_value"
                continue
            if key in c.values:
                return label, cid, c.values[key], c.kind
        return None, None, None, None

    def resolve(self, key, pos=None):
        """{"ok", "value", "source", "raw", "note"} of a setting (pos=None: global stack)."""
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
            return self.resolve(key, None)
        if pos is None and key not in self._resolving:
            resolve_f = self.prop(key, "resolve")
            kind = self.raw_lookup(key, None)[3]
            explicitly_set = kind is not None and not kind.startswith("definition")
            if resolve_f not in (None, "None", "") and not explicitly_set:
                self._resolving.add(key)
                try:
                    return {"ok": True, "value": self._coerce(key, self.evaluate(resolve_f, None)),
                            "source": "global.resolve", "raw": resolve_f}
                except Unresolved as exc:
                    return {"ok": False, "value": None, "source": "global.resolve", "raw": resolve_f,
                            "note": str(exc)}
                finally:
                    self._resolving.discard(key)
        lte = self.prop(key, "limit_to_extruder")
        if lte not in (None, "None", "", "=-1", "-1") and self.is_per_extruder(key):
            try:
                n = self.evaluate(lte, pos)
                if isinstance(n, (int, float)) and not isinstance(n, bool) and int(n) == -1:
                    n_s = self._functions(pos)["defaultExtruderPosition"]()
                else:
                    n_s = str(n).strip()
                if n_s in self.p.extruders and n_s != pos:
                    return self.resolve(key, n_s)
            except (Unresolved, ValueError, TypeError):
                pass
        source, cid, raw, kind = self.raw_lookup(key, pos)
        if source is None:
            return {"ok": False, "value": None, "source": None, "note": "unknown setting"}
        res = {"source": source, "container": cid, "raw": raw}
        if isinstance(raw, str) and (raw.startswith("=") or kind == "definition.value"):
            try:
                res.update(ok=True, value=self._coerce(key, self.evaluate(raw, pos)), note="formula")
            except Unresolved as exc:
                res.update(ok=False, value=None, note="formula not evaluated: %s" % exc)
            return res
        res.update(ok=True, value=convert_value(raw, self.prop(key, "type", pos)), note=kind)
        return res

    def resolve_raw(self, key, pos):
        """Как getValueInExtruder: свои контейнеры, потом глобальные, без settable_per_extruder и resolve."""
        ck = ("raw", key, pos)
        if ck in self._cache:
            return self._cache[ck]
        if ck in self._busy:
            raise Unresolved("recursion in %s" % key)
        self._busy.add(ck)
        try:
            source, _cid, raw, kind = self.raw_lookup(key, pos)
            if source is None:
                raise Unresolved("unknown setting %s" % key)
            if isinstance(raw, str) and (raw.startswith("=") or kind == "definition.value"):
                value = self._coerce(key, self.evaluate(raw, pos))
            else:
                value = convert_value(raw, self.prop(key, "type", pos))
        finally:
            self._busy.discard(ck)
        self._cache[ck] = value
        return value

    def _coerce(self, key, v):
        t = self.prop(key, "type")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if t == "float":
                return float(v)
            if t == "int":
                return int(round(v))
            if t in ("extruder", "optional_extruder"):
                return str(int(v))
        return v

    def _functions(self, pos):
        p = self.p

        def enabled_positions():
            count = self.resolve("machine_extruder_count", None)
            n = count["value"] if count.get("ok") else len(p.extruders)
            return [k for k in sorted(p.extruders, key=int)
                    if str(p.extruders[k].metadata.get("enabled", "True")) == "True" and int(k) < int(n)]

        def default_pos():
            positions = enabled_positions()
            return positions[0] if positions else "0"

        def extruder_value(position, key):
            position = str(int(position))
            return self.resolve_raw(key, default_pos() if position == "-1" else position)

        def extruder_values(key):
            values = [self.resolve_raw(key, k) for k in enabled_positions()]
            if not values:
                r = self.resolve(key, None)
                if not r["ok"]:
                    raise Unresolved("extruderValues(%s)" % key)
                values.append(r["value"])
            return values

        def resolve_or_value(key):
            r = self.resolve(key, None)
            if not r["ok"]:
                raise Unresolved("resolveOrValue(%s)" % key)
            return r["value"]

        def any_extruder_with_material(material_key):
            for k in enabled_positions():
                m = p.slot(p.extruders[k], "material")[1]
                if m is not None and str(m.metadata.get(material_key, "")).lower() in ("true", "1", "yes"):
                    return True
            return False

        def any_extruder_nr_with_or_default(key):
            for k in enabled_positions():
                r = self.resolve(key, k)
                if r["ok"] and r["value"]:
                    return int(k)
            return int(default_pos())

        def unsupported(*_args, **_kwargs):
            raise Unresolved("valueFromContainer is not supported offline")

        funcs = dict(_SAFE_BUILTINS)
        funcs.update({"math": math, "extruderValue": extruder_value, "extruderValues": extruder_values,
                      "resolveOrValue": resolve_or_value, "defaultExtruderPosition": default_pos,
                      "anyExtruderWithMaterial": any_extruder_with_material,
                      "anyExtruderNrWithOrDefault": any_extruder_nr_with_or_default,
                      "valueFromContainer": unsupported, "extruderValueFromContainer": unsupported,
                      "defaultValueInExtruder": unsupported})
        return funcs

    def evaluate(self, formula, pos):
        code = str(formula)
        code = (code[1:] if code.startswith("=") else code).strip()
        try:
            tree = ast.parse(code, mode="eval")
        except SyntaxError as exc:
            raise Unresolved("syntax: %s" % exc)
        _check_ast(tree)
        try:
            return eval(compile(tree, "<setting>", "eval"), {"__builtins__": {}},
                        _Names(self, pos, self._functions(pos)))
        except Unresolved:
            raise
        except Exception as exc:
            raise Unresolved("%s: %s" % (type(exc).__name__, exc))

    def shadowed_user_values(self):
        """Глобальные пользовательские значения, которые не действуют, потому что их перекрывает экструдер."""
        res = []
        user = self.p.slot(self.p.global_stack, "user")[1]
        if user is None:
            return res
        for key, raw in sorted(user.values.items()):
            if not self.is_per_extruder(key):
                continue
            for pos in sorted(self.p.extruders):
                src, _cid, eraw, _kind = self.raw_lookup(key, pos)
                if src and src.startswith("extruder"):
                    res.append({"key": key, "global_user": raw, "extruder": pos, "winner": src, "value": eraw})
        return res


def load_project(path, catalog_settings=None):
    """Проект с определениями из архива, а если их нет — со справочником в роли запасного варианта."""
    project = Project(path)
    if project.definitions_source == "none" and catalog_settings:
        project = Project(path, fallback_definition=catalog_definition(catalog_settings))
    return project

