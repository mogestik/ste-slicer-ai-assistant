"""Разбор G-code, который пишет STE Slicer (только stdlib, Python 3.5+).

Урезанная версия devtools/check_gcode.py. Что пишет слайсер:
  * заголовок GCodeWriter: ;PREVIEW: (не всегда), ;VERSION:, ;PRINTING_MODE:[режим], ;MINX..;MAXZ —
    габариты сцены в системе Y-up относительно центра стола (MINX/MAXX = X принтера,
    MINY/MAXY = Z сцены = -Y принтера, MINZ/MAXZ = высота);
  * 3D (CuraEngine): ;FLAVOR: ;TIME: ;Filament used: ;Layer height: ;LAYER_COUNT: ;LAYER:n;
    G54 (+G4 P100), без движений по A/C; стол M140/M190 только в classic;
  * 5D (CliParser): G56 (5D Spiral / основание), G55 (сердечник 5D Full), G0 A90, движения X Y Z A B C;
  * ;SETTING_3 {json} в конце: сериализованные контейнеры user + quality_changes.
"""

import configparser
import json
import math
import re

WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:nan|inf|infinity))",
                     re.IGNORECASE)
HEADER_RE = re.compile(r"(PREVIEW|VERSION|PRINTING_MODE|MINX|MINY|MINZ|MAXX|MAXY|MAXZ|FLAVOR|TIME|"
                       r"Filament used|Layer height|LAYER_COUNT|PRINT\.TIME)\s*:?\s*(.*)$")
_ESC = re.compile(r"\\\\|\\n|\\r")
HUGE = 1000.0


class GcodeStats(object):
    def __init__(self):
        self.lines = 0
        self.header = {}
        self.setting_lines = []
        self.layers = 0
        self.moves = 0
        self.extrusions = 0
        self.max_e = 0.0
        self.bad_numbers = 0
        self.huge = 0
        self.ranges = {}
        self.axes = set()
        self.a_values = set()
        self.coord_systems = set()
        self.hotend = []  # (S, tool)
        self.bed = []
        self.has_start = False
        self.has_end = False
        self.ellipse_ratio = 0.0  # 5D: максимум (x/a)^2 + (y/b)^2 по движениям печати, a/b задаёт вызывающий


def parse_file(path, half_w=175.5, half_d=150.15):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_lines(handle, half_w, half_d)


def parse_lines(lines, half_w=175.5, half_d=150.15):
    st = GcodeStats()
    in_print = False
    relative = False
    tool = 0
    for raw in lines:
        st.lines += 1
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            body = line[1:]
            if body.startswith("SETTING_"):
                st.setting_lines.append(body)
                continue
            if body.startswith("LAYER:"):
                st.layers += 1
                in_print = True
                continue
            m = HEADER_RE.match(body)
            if m:
                key = m.group(1)
                if key not in st.header:
                    st.header[key] = m.group(2).strip() if key != "PREVIEW" else "<%d chars>" % len(m.group(2))
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
            continue
        words = {}
        bad = False
        for letter, text in WORD_RE.findall(code[len(cmd):]):
            try:
                value = float(text)
            except ValueError:
                value = float("nan")
            if math.isnan(value) or math.isinf(value):
                bad = True
                continue
            words[letter.upper()] = value
        if bad:
            st.bad_numbers += 1
        if cmd == "G90":
            relative = False
        elif cmd == "G91":
            relative = True
        elif cmd in ("G54", "G55", "G56", "G57", "G58", "G59"):
            st.coord_systems.add(cmd)
        elif cmd in ("M104", "M109") and "S" in words:
            st.hotend.append((words["S"], int(words.get("T", tool))))
        elif cmd in ("M140", "M190") and "S" in words:
            st.bed.append(words["S"])
        elif cmd in ("G0", "G1", "G2", "G3", "G00", "G01"):
            st.moves += 1
            for axis in "XYZABC":
                if axis in words:
                    st.axes.add(axis)
                    # поворотные оси (A/B/C) — накопленные градусы: в 5D Spiral C растёт на 360 за оборот
                    if axis in "XYZ" and abs(words[axis]) > HUGE:
                        st.huge += 1
            if "A" in words:
                st.a_values.add(round(words["A"], 2))
            if relative:
                continue
            if "E" in words and cmd in ("G1", "G01") and in_print:
                st.extrusions += 1
                st.max_e = max(st.max_e, words["E"])
            if in_print:
                for axis in "XYZABC":
                    if axis in words:
                        lo, hi = st.ranges.get(axis, (words[axis], words[axis]))
                        st.ranges[axis] = (min(lo, words[axis]), max(hi, words[axis]))
                if "X" in words and "Y" in words:
                    st.ellipse_ratio = max(st.ellipse_ratio,
                                           (words["X"] / half_w) ** 2 + (words["Y"] / half_d) ** 2)
    return st


def header_float(st, key):
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", st.header.get(key) or "")
    return float(match.group(0)) if match else None


def header_mode(st):
    mode = st.header.get("PRINTING_MODE")
    return mode.strip().strip("[]") if mode else None


def header_bbox_printer(st):
    """Габариты сцены от GCodeWriter -> система принтера относительно центра стола: ([x0,y0,z0], [x1,y1,z1]) или None."""
    keys = ("MINX", "MINY", "MINZ", "MAXX", "MAXY", "MAXZ")
    values = [header_float(st, k) for k in keys]
    if any(v is None for v in values):
        return None
    minx, miny, minz, maxx, maxy, maxz = values
    return [minx, -maxy, minz], [maxx, -miny, maxz]


def settings_block(st):
    """Содержимое ;SETTING_3 -> {"global": {...}, "extruders": [{...}, ...]}, {"error": ...} или None."""
    if not st.setting_lines:
        return None
    payload = ""
    for body in st.setting_lines:
        m = re.match(r"SETTING_(\d+) (.*)$", body)
        if m:
            payload += m.group(2)
    try:
        data = json.loads(_ESC.sub(lambda m: {"\\\\": "\\", "\\n": "\n", "\\r": "\r"}[m.group(0)], payload))
    except ValueError as exc:
        return {"error": str(exc)}

    def values(text):
        cp = configparser.ConfigParser(interpolation=None, comment_prefixes=())
        cp.read_string(text)
        return dict(cp["values"]) if cp.has_section("values") else {}

    try:
        return {"global": values(data.get("global_quality", "")),
                "extruders": [values(text) for text in data.get("extruder_quality", [])]}
    except configparser.Error as exc:
        return {"error": str(exc)}
