"""Имитация STE Slicer в памяти — для стенда и тестов.

`MockSlicerAdapter` реализует тот же интерфейс, что и настоящий
`agent.slicer.qt_adapter.QtSlicerAdapter`, поэтому настоящие обработчики инструментов
работают поверх него без изменений. Что есть: настройки с проверкой (типы, пределы,
доступность по справочнику, если он есть), профили, настоящий список материалов H530,
режимы 3D/5D со сменой бэкенда, сцена с настоящими сетками (STL/OBJ разбираются и
центрируются; 3MF даёт по объекту на элемент build с исходной сеткой, поворотом узла и
масштабом единиц как в ThreeMFReader), имитация нарезки с событиями прогресса и
правдоподобным результатом, выгрузка G-code и проект 3MF в той же раскладке, что у
ThreeMFWorkspaceWriter.

Правила потоков проверяются: методы «[main]» падают с AssertionError, если их вызвать не
из (имитированного) главного потока Qt, а методы «[worker]» — если вызвать из него. Так
неправильный флаг main_thread у инструмента сразу виден на стенде.

Переменные окружения:
    AIASSISTANT_MOCK_NO_ATSS=1     5D-нарезка зависает на этапе glicer (как без лицензии)
    AIASSISTANT_MOCK_SLICE_STEP=s  длительность одного шага прогресса (по умолчанию 0.15 с)
"""

import io
import math
import os
import re
import struct
import threading
import time
import zipfile

from agent import main_thread
from agent.main_thread import run_in_main_thread
from agent.slicer import coords, set_adapter
from agent.slicer.base import (ATSS_NOTE, FIVE_D_MODES, MODE_REPORT_KEYS, PRINT_MODE_TITLES, PRINT_MODES,
                               TARGET_MACHINE, ObjectIds, SlicerAdapter, SlicerError, resolve_object)
from agent.slicer.monitor import DONE, NOT_STARTED, PROCESSING, SliceMonitor
from agent.slicer.qt_adapter import format_duration, inspect_3mf, inspect_gcode, pick_material
from agent.slicer.values import check_limits, json_safe, option_pairs, ValueProblem

AXIS_HINT = ("поставь ось вращения на ось стола по suggest_orientation: move_object(frame=scene, x, z). "
             "Если уже поставил так — это норма для несимметричной детали (ось не совпадает с центром "
             "габаритов).")

MACHINE_NAME = "Stereotech Hybrid 530 V5.2.4"
BACKENDS = {"classic": "CuraEngineBackend", "cylindrical": "CLIParserBackend",
            "cylindrical_full": "CylindricalBackend"}
WIDTH, DEPTH = 351.0, 300.3

# quality_type, название, высота слоя, доп. значения (как в resources/quality/*.inst.cfg)
QUALITIES = [
    ("high", "Extra Fine", 0.06, {}),
    ("normal", "Fine", 0.1, {}),
    ("fast", "Normal", 0.15, {"infill_sparse_density": 10.0, "wall_thickness": 1.0, "cool_min_layer_time": 3.0}),
    ("draft", "Draft", 0.2, {}),
    ("verydraft", "Extra Fast", 0.3, {}),
    ("coarse", "Coarse", 0.4, {}),
    ("extra coarse", "Extra Coarse", 0.6, {}),
]

# id, название, бренд, полимер, серия, плотность, T сопла, T стола, T ожидания, обдув %, ретракт мм, ретракт мм/с
# (материалы, доступные в STE Slicer 7.10.12 для H530 V5.2.4, диаметр 1.75)
MATERIALS = [
    ("proto_pla", "Proto PLA", "Stereotech", "PLA", "Proto", 1.25, 210, 60, 175, 100, 0.85, 30),
    ("proto_pva", "Proto PVA", "Stereotech", "PVA", "Proto", 1.05, 210, 60, 175, 75, 4.0, 30),
    ("enduse_abs", "Enduse ABS", "Stereotech", "ABS", "Enduse", 1.05, 250, 100, 175, 0, 3.0, 25),
    ("enduse_pa6", "Enduse PA6", "Stereotech", "PA6", "Enduse", 1.05, 260, 95, 175, 0, 2.11, 20),
    ("enduse_pc", "Enduse PC", "Stereotech", "PC", "Enduse", 1.2, 280, 105, 175, 0, 5.0, 25),
    ("enduse_petg", "Enduse PETG", "Stereotech", "PETG", "Enduse", 1.3, 235, 60, 175, 20, 1.5, 11.5),
    ("enduse_pp", "Enduse PP", "Stereotech", "PP", "Enduse", 0.9, 225, 50, 175, 0, 3.55, 30),
    ("enduse_sbs", "Enduse SBS", "Stereotech", "SBS", "Enduse", 1.01, 230, 80, 175, 20, 2.0, 20),
    ("enduse_tpu_d70", "Enduse TPU D70", "Stereotech", "TPU", "Enduse", 1.23, 220, 60, 175, 20, 1.4, 27.5),
    ("fiberpart_abs_g4", "Fiberpart ABS G4", "Stereotech", "ABS", "Fiberpart", 1.1, 240, 100, 175, 0, 6.0, 25),
    ("fiberpart_abs_g13", "Fiberpart ABS G13", "Stereotech", "ABS", "Fiberpart", 1.15, 270, 105, 175, 0, 6.5, 25),
    ("fiberpart_abspa_g8", "Fiberpart ABSPA G8", "Stereotech", "ABSPA", "Fiberpart", 1.12, 280, 115, 175, 0, 7.0,
     20),
    ("fiberpart_pa12_g12", "Fiberpart PA12 G12", "Stereotech", "PA12", "Fiberpart", 1.05, 250, 105, 175, 0, 5.0,
     30),
    ("fiberpart_pc_g20", "Fiberpart PC G20", "Stereotech", "PC", "Fiberpart", 1.18, 290, 115, 175, 0, 6.4, 30),
    ("fiberpart_pp_g30", "Fiberpart PP G30", "Stereotech", "PP", "Fiberpart", 1.18, 270, 110, 175, 0, 3.0, 25),
    ("fiberpart_tpu_c5", "Fiberpart TPU C5", "Stereotech", "TPU", "Fiberpart", 1.16, 230, 95, 175, 20, 1.5, 18),
    ("fiberpart_tpu_g30", "Fiberpart TPU G30", "Stereotech", "TPU", "Fiberpart", 1.45, 240, 80, 175, 0, 7.5, 40),
    ("sealant_tpu", "Sealant TPU", "Stereotech", "TPU", "Sealant", 1.15, 220, 60, 175, 20, 2.0, 10),
    ("sealant_tpu93", "Sealant TPU93", "Stereotech", "TPU93", "Sealant", 1.13, 200, 70, 175, 20, 2.0, 10),
    ("sealant_tpu_A95", "Sealant TPU A95", "Stereotech", "TPU", "Sealant", 1.15, 215, 45, 175, 20, 5.7, 37),
    ("sealant_tpe", "Sealant TPE", "Stereotech", "TPE", "Sealant", 1.1, 230, 70, 175, 30, 2.0, 20),
    ("sealant_sebs", "Sealant SEBS", "Stereotech", "SEBS", "Sealant", 0.95, 235, 100, 175, 20, 0.0, 0),
    ("sealant_tpv80", "Sealant TPV80", "Stereotech", "TPV80", "Sealant", 1.13, 280, 50, 175, 0, 2.0, 10),
    ("metalcast_316l", "Metalcast 316L", "Stereotech", "316L", "Metalcast", 7.5, 240, 43, 100, 20, 3.0, 8),
    ("metalcast_al99", "Metalcast AL99", "Stereotech", "AL99", "Metalcast", 2.54, 160, 50, 100, 20, 4.0, 15),
    ("generic_pla_175", "PLA", "Generic", "PLA", "PLA", 1.24, 200, 60, 175, 100, 1.0, 40),
    ("generic_abs_175", "ABS", "Generic", "ABS", "ABS", 1.10, 230, 80, 200, 100, 1.0, 40),
    ("generic_petg_175", "PETG", "Generic", "PETG", "PETG", 1.38, 215, 70, 120, 100, 1.0, 40),
    ("generic_cpe_175", "CPE", "Generic", "CPE", "CPE", 1.27, 240, 70, 175, 100, 1.0, 40),
    ("generic_hips_175", "HIPS", "Generic", "HIPS", "HIPS", 1.24, 215, 60, 160, 100, 1.0, 40),
    ("generic_nylon_175", "Nylon", "Generic", "Nylon", "Nylon", 1.14, 245, 60, 175, 100, 8.0, 25),
    ("generic_pc_175", "PC", "Generic", "PC", "PC", 1.19, 270, 107, 175, 100, 1.0, 40),
    ("generic_pva_175", "PVA", "Generic", "PVA", "PVA", 1.23, 215, 60, 175, 100, 1.0, 40),
    ("generic_tpu_175", "TPU 95A", "Generic", "TPU", "TPU 95A", 1.22, 228, 0, 175, 100, 1.0, 40),
]

# Минимальные определения на случай, если справочника настроек нет.
FALLBACK_SETTINGS = {
    "printing_mode": {"type": "enum", "label": "Printing Mode", "label_ru": "Режим печати", "default":
                      "cylindrical_full", "per_extruder": False,
                      "options": [["classic", "3D"], ["cylindrical", "5D Spiral"], ["cylindrical_full", "5D"]]},
    "layer_height": {"type": "float", "unit": "mm", "label": "Layer Height", "label_ru": "Высота слоя",
                     "default": 0.1, "min": 0.001, "min_warn": 0.04, "max_warn": 0.32, "per_extruder": False},
    "layer_height_0": {"type": "float", "unit": "mm", "label": "Initial Layer Height", "default": 0.3,
                       "min": 0.001, "per_extruder": False},
    "wall_thickness": {"type": "float", "unit": "mm", "label": "Wall Thickness", "default": 0.8, "min": 0,
                       "per_extruder": True},
    "wall_line_count": {"type": "int", "label": "Wall Line Count", "label_ru": "Количество линий стенки",
                        "default": 2, "min": 0, "max_warn": 10, "per_extruder": True},
    "top_bottom_thickness": {"type": "float", "unit": "mm", "label": "Top/Bottom Thickness", "default": 0.8,
                             "min": 0, "per_extruder": True},
    "infill_sparse_density": {"type": "float", "unit": "%", "label": "Infill Density",
                              "label_ru": "Плотность заполнения", "default": 20, "min": 0, "max_warn": 100,
                              "per_extruder": True},
    "infill_pattern": {"type": "enum", "label": "Infill Pattern", "default": "grid", "per_extruder": True,
                       "options": [["grid", "Grid"], ["lines", "Lines"], ["triangles", "Triangles"],
                                   ["cubic", "Cubic"], ["gyroid", "Gyroid"], ["concentric", "Concentric"]]},
    "support_enable": {"type": "bool", "label": "Generate Support", "label_ru": "Генерация поддержек",
                       "default": False, "per_extruder": False},
    "support_type": {"type": "enum", "label": "Support Placement", "default": "everywhere", "per_extruder": False,
                     "options": [["buildplate", "Touching Buildplate"], ["everywhere", "Everywhere"]]},
    "support_angle": {"type": "float", "unit": "°", "label": "Support Overhang Angle", "default": 50, "min": 0,
                      "max": 90, "per_extruder": True},
    "adhesion_type": {"type": "enum", "label": "Build Plate Adhesion Type", "default": "raft",
                      "default_3d": "brim", "per_extruder": False,
                      "options": [["skirt", "Skirt"], ["brim", "Brim"], ["raft", "Raft"], ["none", "None"]]},
    "material_print_temperature": {"type": "float", "unit": "°C", "label": "Printing Temperature",
                                   "label_ru": "Температура печати", "default": 210, "min": -273.15,
                                   "min_warn": 150, "max_warn": 350, "max": 365, "per_extruder": True},
    "material_bed_temperature": {"type": "float", "unit": "°C", "label": "Build Plate Temperature", "default": 60,
                                 "min": -273.15, "max_warn": 130, "max": 200, "per_extruder": True},
    "speed_print": {"type": "float", "unit": "mm/s", "label": "Print Speed", "default": 60, "min": 0.1,
                    "max_warn": 150, "per_extruder": True},
    "cool_fan_speed": {"type": "float", "unit": "%", "label": "Fan Speed", "default": 100, "min": 0, "max": 100,
                       "per_extruder": True},
    "retraction_amount": {"type": "float", "unit": "mm", "label": "Retraction Distance", "default": 1,
                          "min_warn": -0.0001, "max_warn": 10, "per_extruder": True},
    "retraction_speed": {"type": "float", "unit": "mm/s", "label": "Retraction Speed", "default": 40,
                         "min": 0.0001, "per_extruder": True},
    "retraction_enable": {"type": "bool", "label": "Enable Retraction", "default": True, "per_extruder": True},
    "cylindrical_mode_base_diameter": {"type": "float", "unit": "mm", "label": "Base Diameter",
                                       "label_ru": "Диаметр основания", "default": 6, "min": 4, "min_warn": 5,
                                       "per_extruder": False, "mode_5d": True},
    "cylindrical_mode_overlap": {"type": "float", "unit": "mm", "label": "Cylindrical Parts Overlap", "default": 0,
                                 "min": 0, "max": 10, "max_warn": 2, "per_extruder": False, "mode_5d": True},
    "non_printing_base_diameter": {"type": "float", "unit": "mm", "label": "Non Printing Base Diameter",
                                   "default": 6, "min": 4, "per_extruder": False, "mode_5d": True},
    "cylindrical_layer_height": {"type": "float", "unit": "mm", "label": "5D Surface Layer Height",
                                 "default": 0.1, "min": 0.001, "per_extruder": False, "mode_5d": True},
    "cylindrical_raft_type": {"type": "enum", "label": "5D Raft Type", "default": "solid", "default_3d": "none",
                              "per_extruder": False, "mode_5d": True,
                              "options": [["solid", "Solid"], ["continuous", "Continuous"], ["gapped", "Gapped"],
                                          ["none", "None"]]},
    "support_enable_cylindrical": {"type": "bool", "label": "Enable 5D Support", "default": False,
                                   "per_extruder": False, "mode_5d": True},
    "support_enable_top_support": {"type": "bool", "label": "Enable Top Support", "default": False,
                                   "per_extruder": False, "mode_5d": True},
    "machine_height": {"type": "float", "unit": "mm", "label": "Machine Height", "default": 231.1,
                       "default_3d": 300, "per_extruder": False},
    "machine_shape": {"type": "enum", "label": "Build Plate Shape", "default": "elliptic",
                      "default_3d": "rectangular", "per_extruder": False,
                      "options": [["rectangular", "Rectangular"], ["elliptic", "Elliptic"]]},
    "machine_center_is_zero": {"type": "bool", "label": "Is Center Origin", "default": True,
                               "default_3d": False, "per_extruder": False},
    "machine_width": {"type": "float", "unit": "mm", "label": "Machine Width", "default": WIDTH,
                      "per_extruder": False},
    "machine_depth": {"type": "float", "unit": "mm", "label": "Machine Depth", "default": DEPTH,
                      "per_extruder": False},
    "machine_nozzle_size": {"type": "float", "unit": "mm", "label": "Nozzle Diameter", "default": 0.4,
                            "per_extruder": True},
    "material_diameter": {"type": "float", "unit": "mm", "label": "Diameter", "default": 1.75,
                          "per_extruder": True},
}

# Значения, которые зависят от режима печати независимо от умолчаний справочника (формулы принтера).
MODE_FORMULAS = {
    "machine_height": lambda mode: 300.0 if mode == "classic" else 231.1,
    "machine_shape": lambda mode: "rectangular" if mode == "classic" else "elliptic",
    "machine_center_is_zero": lambda mode: mode != "classic",
    "machine_heated_bed": lambda mode: mode == "classic",
    "adhesion_type": lambda mode: "raft" if mode == "cylindrical_full" else "brim",
    "cylindrical_raft_type": lambda mode: "solid" if mode == "cylindrical_full" else "none",
    "machine_width": lambda mode: WIDTH,
    "machine_depth": lambda mode: DEPTH,
}
FIVE_D_ONLY_PREFIXES = ("cylindrical_", "non_printing_base", "support_enable_top_support", "top_support_")


def _catalog():
    try:
        from agent.knowledge.settings_catalog import get_catalog
        return get_catalog()
    except Exception:
        return None


def _check_thread(kind):
    """Методы [main] должны работать в имитированном главном потоке Qt, методы [worker] — вне его."""
    if main_thread._get_app() is None:
        return
    in_main = main_thread.is_main_thread()
    if kind == "main" and not in_main:
        raise AssertionError("mock slicer: [main] method called outside the main thread")
    if kind == "worker" and in_main:
        raise AssertionError("mock slicer: [worker] method called from the main thread (would dead-lock)")


# Работа с сетками

def read_mesh(path):
    """(вершины, грани) STL/OBJ в системе самого файла (= система принтера, Z вверх)."""
    lower = path.lower()
    if lower.endswith(".stl"):
        return _read_stl(path)
    if lower.endswith(".obj"):
        return _read_obj(path)
    raise SlicerError("Формат файла не поддерживается слайсером: %s" % os.path.basename(path))


def read_nodes(path):
    """Узлы сцены, в которые превращается файл: [NodeSpec]; STL/OBJ — один узел, 3MF — по узлу на элемент build."""
    if path.lower().endswith(".3mf"):
        return _read_3mf(path)
    vertices, faces = read_mesh(path)
    return [NodeSpec(vertices, faces)] if faces else []


class NodeSpec(object):
    """Узел в том виде, как его создаёт читатель слайсера, в системе принтера мока.

    `vertices` — локальная сетка (оси принтера); в мир узел переводит её как
    rotation * (scale * v) + translation. `translation=None`: сетка центрирована, узел ставит
    расстановщик (STL/OBJ); иначе сохраняется положение из файла (3MF).
    """

    def __init__(self, vertices, faces, rotation=None, scale=(1.0, 1.0, 1.0), translation=None):
        self.vertices = vertices
        self.faces = faces
        self.rotation = rotation or [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        self.scale = [float(f) for f in scale]
        self.translation = translation


# Сцена -> принтер: (x, y, z) -> (x, -z, y) (coords.scene_to_printer) в виде матрицы.
P_SCENE_TO_PRINTER = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]


def _read_3mf(path):
    """Элементы build из 3MF — так, как их загружают ThreeMFReader + Savitar (Cura 3.6).

    Каждый <build><item> — отдельный узел. Данные сетки — исходные координаты объекта
    (компоненты слиты со своими преобразованиями; настоящий читатель делает из них дочерние
    узлы группы); преобразование узла — Flip * T(-width/2, -depth/2) * Unit * T(c) * Item
    (Flip — поворот на -90 градусов вокруг X, то есть (x, y, z) -> сцена (x, z, -y); c — центр
    габаритов сетки, особенность ThreeMFReader._read). Uranium раскладывает это на поворот и
    масштаб: поворот узла — Flip * R_item, масштаб — единицы (умноженные на масштаб элемента).

    Мок хранит координаты принтера: P * Flip = I, поэтому локальная сетка — P * raw, поворот
    узла — R_item * P^T, точка в мире — k * (L * raw + t_item + c) + (-width/2, -depth/2, 0).
    """
    from agent.geometry import threemf
    from agent.geometry import vecmath
    from agent.geometry.mesh import MeshError
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open(threemf.MODEL_PATH) as handle:
                unit, objects, items = threemf._parse(handle)
    except (zipfile.BadZipfile, KeyError, MeshError) as exc:
        raise SlicerError("Не удалось прочитать 3MF %s: %s" % (os.path.basename(path), exc))
    k = threemf.UNITS_MM.get(unit, 1.0)     # неизвестные единицы — миллиметры (как ThreeMFReader._getScaleFromUnit)
    nodes = []
    for object_id, item in items:
        flat = []
        try:
            threemf._emit(objects, object_id, threemf.IDENTITY_TRANSFORM, flat)
        except MeshError as exc:
            raise SlicerError("Не удалось прочитать 3MF %s: %s" % (os.path.basename(path), exc))
        if not flat:
            continue      # узлы без сетки ThreeMFReader пропускает
        raw = [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]
        faces = [(i, i + 1, i + 2) for i in range(0, len(raw), 3)]
        linear = threemf.linear_part(item)
        rotation = vecmath.orthonormalize(linear)
        residual = vecmath.mat_mul(vecmath.transpose(rotation), linear)    # = S * Shear (верхнетреугольная)
        scale = [residual[i][i] for i in range(3)]
        shear = [[residual[r][c] / scale[r] for c in range(3)] for r in range(3)]
        if any(abs(shear[r][c]) > 1e-9 for r in range(3) for c in range(3) if r != c):
            raw = [vecmath.mat_vec(shear, v) for v in raw]   # сдвига в моке нет — оставляем его в сетке
        lo = [min(v[i] for v in raw) for i in range(3)]
        hi = [max(v[i] for v in raw) for i in range(3)]
        centre = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
        local = [(v[0], -v[2], v[1]) for v in raw]                           # P * raw
        node_rotation = coords.matmul([list(row) for row in rotation], coords.transpose(P_SCENE_TO_PRINTER))
        node_scale = (k * scale[0], k * scale[2], k * scale[1])               # P * S * P^T
        translation = [k * (item[9] + centre[0]) - WIDTH / 2.0, k * (item[10] + centre[1]) - DEPTH / 2.0,
                       k * (item[11] + centre[2])]
        nodes.append(NodeSpec(local, faces, node_rotation, node_scale, translation))
    return nodes


def _read_stl(path):
    with open(path, "rb") as handle:
        data = handle.read()
    vertices = []
    count = struct.unpack("<I", data[80:84])[0] if len(data) >= 84 else 0
    if count and len(data) == 84 + count * 50:
        for i in range(count):
            offset = 84 + i * 50 + 12
            values = struct.unpack("<9f", data[offset:offset + 36])
            vertices.extend([values[0:3], values[3:6], values[6:9]])
    else:
        for line in data.decode("ascii", "replace").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
    faces = [(i, i + 1, i + 2) for i in range(0, len(vertices) - 2, 3)]
    return vertices, faces


def _read_obj(path):
    vertices, faces = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "v" and len(parts) >= 4:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "f" and len(parts) >= 4:
                idx = [int(p.split("/")[0]) for p in parts[1:]]
                idx = [i - 1 if i > 0 else len(vertices) + i for i in idx]
                for k in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[k], idx[k + 1]))
    # OBJReader из Uranium, как и читатель STL, переводит (x, y, z) -> сцена (x, z, -y): система файла = система принтера.
    return vertices, faces


def _transform(vertices, rotation, scale, translation):
    """Мировые координаты принтера: R * (S * v) + t."""
    if coords.numpy is not None:
        np = coords.numpy
        verts = np.asarray(vertices, dtype=np.float64).reshape(-1, 3) * np.asarray(scale)
        return verts.dot(np.asarray(rotation).T) + np.asarray(translation)
    result = []
    for v in vertices:
        s = (v[0] * scale[0], v[1] * scale[1], v[2] * scale[2])
        r = coords.apply_matrix(rotation, s)
        result.append((r[0] + translation[0], r[1] + translation[1], r[2] + translation[2]))
    return result


def _bbox(vertices):
    box = coords.bounding_box(vertices)
    if box is None:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    return box


class MockObject(object):
    def __init__(self, name, vertices, faces, center=True):
        """`center`: сдвинуть локальную сетку к центру её габаритов (как читатели STL/OBJ); False —
        оставить исходные координаты (у 3MF данные сетки — исходные координаты объекта)."""
        self.name = name
        if center:
            lo, hi = _bbox(vertices)
            offset = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
        else:
            offset = [0.0, 0.0, 0.0]
        if coords.numpy is not None:
            self.local = coords.numpy.asarray(vertices, dtype=coords.numpy.float64).reshape(-1, 3) - offset
        else:
            self.local = [(v[0] - offset[0], v[1] - offset[1], v[2] - offset[2]) for v in vertices]
        self.faces = faces
        self.rotation = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        self.scale = [1.0, 1.0, 1.0]
        self.translation = [0.0, 0.0, 0.0]

    @classmethod
    def from_spec(cls, name, spec):
        obj = cls(name, spec.vertices, spec.faces, center=spec.translation is None)
        obj.rotation = [list(row) for row in spec.rotation]
        obj.scale = list(spec.scale)
        if spec.translation is not None:
            obj.translation = list(spec.translation)
        return obj

    def world(self):
        return _transform(self.local, self.rotation, self.scale, self.translation)

    def bbox(self):
        return _bbox(self.world())

    def place(self, center_xy=None, drop=True):
        lo, hi = self.bbox()
        if center_xy is not None:
            self.translation[0] += center_xy[0] - (lo[0] + hi[0]) / 2.0
            self.translation[1] += center_xy[1] - (lo[1] + hi[1]) / 2.0
        if drop:
            self.translation[2] -= lo[2]


# Сам адаптер

class MockSlicerAdapter(SlicerAdapter):
    name = "mock"

    def __init__(self, slice_step=None, atss_license=None):
        self._lock = threading.RLock()
        self._catalog = _catalog()
        self.mode_default = "cylindrical_full"
        self.user = {"global": {}, "extruder_0": {}, "extruder_1": {}}
        self.quality_type = "normal"
        self.custom_profiles = {}        # name -> {"quality_type", "values": {stack: {key: value}}}
        self.active_custom = None
        self.materials = {0: "generic_pla_175", 1: "generic_pla_175"}
        self.objects = []
        self._ids = ObjectIds()
        self.monitor = SliceMonitor()
        self.monitor.set_current(BACKENDS[self.mode_default])
        self.monitor.set_mode(self.mode_default)
        self.monitor.on_state(BACKENDS[self.mode_default], NOT_STARTED)
        self.gcode = None
        self._slice_thread = None
        self._slice_cancel = threading.Event()
        step = os.environ.get("AIASSISTANT_MOCK_SLICE_STEP")
        self.slice_step = slice_step if slice_step is not None else (float(step) if step else 0.15)
        if atss_license is None:
            atss_license = os.environ.get("AIASSISTANT_MOCK_NO_ATSS", "") not in ("1", "true", "yes")
        self.atss_license = atss_license
        self.auto_slice = False

    # Модель настроек

    def _definition(self, key):
        if self._catalog is not None:
            entry = self._catalog.get(key)
            if entry is not None:
                merged = dict(FALLBACK_SETTINGS.get(key) or {})
                merged.update(entry)
                return merged
        return FALLBACK_SETTINGS.get(key)

    def _has(self, key):
        return self._definition(key) is not None

    def _mode(self):
        value = self.user["global"].get("printing_mode")
        if value is None and self.active_custom:
            value = self.custom_profiles[self.active_custom]["values"].get("global", {}).get("printing_mode")
        return value or self.mode_default

    def print_mode(self):
        _check_thread("main")
        return self._mode()

    def _quality(self):
        for quality in QUALITIES:
            if quality[0] == self.quality_type:
                return quality
        return QUALITIES[1]

    def _quality_values(self):
        quality = self._quality()
        values = {"layer_height": quality[2]}
        values.update(quality[3])
        return values

    def _material(self, index):
        root_id = self.materials.get(index)
        for item in MATERIALS:
            if item[0] == root_id:
                return item
        return MATERIALS[-9]

    def _material_values(self, index):
        m = self._material(index)
        return {"material_print_temperature": float(m[6]), "material_bed_temperature": float(m[7]),
                "material_standby_temperature": float(m[8]), "cool_fan_speed": float(m[9]),
                "retraction_amount": float(m[10]), "retraction_speed": float(m[11])}

    def _route(self, key, scope, extruder_index):
        definition = self._definition(key) or {}
        per_extruder = bool(definition.get("per_extruder"))
        if not per_extruder:
            note = ("настройка общая для принтера (не задаётся на экструдер) — используется глобальный стек"
                    if scope == "extruder" else None)
            return "global", None, note
        index = int(extruder_index or 0)
        if index not in (0, 1):
            raise SlicerError("Экструдера %s нет: у принтера экструдеры 0..1" % extruder_index)
        return "extruder_%d" % index, index, None

    def _effective(self, key, stack_name, index):
        """(значение, источник) — порядок контейнеров настоящих стеков, упрощённо."""
        mode = self._mode()
        custom = self.custom_profiles.get(self.active_custom, {}).get("values", {}) if self.active_custom else {}
        layers = [(self.user.get(stack_name, {}), "пользовательское изменение"),
                  (custom.get(stack_name, {}), "пользовательский профиль")]
        if index is not None:
            layers.append((self._quality()[3], "профиль качества"))
            layers.append((self._material_values(index), "материал"))
            layers.append((self.user["global"], "глобальный стек: пользовательское изменение"))
            layers.append((custom.get("global", {}), "глобальный стек: пользовательский профиль"))
        else:
            layers.append((self._quality()[3], "профиль качества"))
        for values, source in layers:
            if key in values:
                return values[key], source
        if key == "layer_height":
            return self._quality()[2], "профиль качества"
        if key == "cylindrical_layer_height":
            return self._effective("layer_height", "global", None)[0], "формула (= layer_height)"
        if key in MODE_FORMULAS:
            return MODE_FORMULAS[key](mode), "определение принтера (зависит от режима)"
        definition = self._definition(key) or {}
        if mode == "classic" and "default_3d" in definition:
            return definition["default_3d"], "определение принтера"
        return definition.get("default"), "определение принтера"

    def _enabled(self, key, definition):
        mode = self._mode()
        if key == "printing_mode":
            return True
        if key.startswith(FIVE_D_ONLY_PREFIXES) or definition.get("mode_5d") is True:
            return mode in FIVE_D_MODES
        if key in ("material_bed_temperature", "material_bed_temperature_layer_0"):
            return mode == "classic"
        if key in ("support_enable_cylindrical",):
            return mode in FIVE_D_MODES
        return True

    @staticmethod
    def _limit(definition, name):
        value = definition.get(name)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            return None   # пределы-формулы мок не вычисляет

    def _info(self, key, stack_name, index, note=None):
        definition = self._definition(key) or {}
        value, source = self._effective(key, stack_name, index)
        info = {
            "key": key, "label": definition.get("label") or key, "description": definition.get("desc") or "",
            "type": definition.get("type") or "str", "unit": definition.get("unit") or "",
            "enabled": self._enabled(key, definition), "settable_per_extruder": bool(definition.get("per_extruder")),
            "minimum_value": self._limit(definition, "min"), "maximum_value": self._limit(definition, "max"),
            "minimum_value_warning": self._limit(definition, "min_warn"),
            "maximum_value_warning": self._limit(definition, "max_warn"),
            "value": json_safe(value), "stack": stack_name, "source": source, "validation": "valid",
        }
        if info["type"] == "enum":
            info["options"] = [list(p) for p in option_pairs([o[:2] for o in definition.get("options") or []])]
        if info["type"] in ("extruder", "optional_extruder"):
            info["extruder_count"] = 2
        if info["type"] in ("float", "int") and isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if check_limits(value, info):
                    info["validation"] = "warning"
            except ValueProblem:
                info["validation"] = "error"
        if note:
            info["note"] = note
        return info

    def setting_info(self, key, scope="global", extruder_index=0):
        _check_thread("main")
        if not self._has(key):
            return None
        stack_name, index, note = self._route(key, scope, extruder_index)
        return self._info(key, stack_name, index, note)

    def setting_value_getter(self, scope="global", extruder_index=0):
        def getter(key):
            if not self._has(key):
                return None
            stack_name, index, _note = self._route(key, scope, extruder_index)
            return self._effective(key, stack_name, index)[0]
        return getter

    def set_setting(self, key, value, scope="global", extruder_index=0):
        _check_thread("main")
        if not self._has(key):
            raise SlicerError("Неизвестная настройка %s" % key)
        stack_name, index, note = self._route(key, scope, extruder_index)
        with self._lock:
            old_mode = self._mode()
            self.user[stack_name][key] = value
            if key == "printing_mode" and value != old_mode:
                self._switch_backend(old_mode)
            self._invalidate()
        return self._info(key, stack_name, index, note)

    def reset_settings(self, keys=None, scope="global", extruder_index=0):
        _check_thread("main")
        with self._lock:
            old_mode = self._mode()
            if keys:
                reset, unknown = [], []
                for key in keys:
                    if not self._has(key):
                        unknown.append(key)
                        continue
                    stacks = ["extruder_%d" % int(extruder_index)] if scope == "extruder" else list(self.user)
                    for name in stacks:
                        self.user[name].pop(key, None)
                    stack_name, index, _note = self._route(key, scope, extruder_index)
                    reset.append({"key": key, "value": json_safe(self._effective(key, stack_name, index)[0]),
                                  "stack": stack_name})
                if self._mode() != old_mode:
                    self._switch_backend(old_mode)
                self._invalidate()
                return {"reset": reset, "unknown": unknown}
            stacks = ["extruder_%d" % int(extruder_index)] if scope == "extruder" else list(self.user)
            count = sum(len(self.user[name]) for name in stacks)
            for name in stacks:
                self.user[name] = {}
            result = {"reset_all": True, "cleared_values": count}
            if scope != "extruder":
                self.user["global"]["printing_mode"] = old_mode
                result["kept"] = {"printing_mode": old_mode}
            self._invalidate()
            return result

    def search_settings(self, query, limit=15):
        _check_thread("main")
        words = [w for w in query.lower().split() if w]
        keys = self._catalog.keys() if self._catalog is not None else list(FALLBACK_SETTINGS)
        found = []
        for key in keys:
            definition = self._definition(key) or {}
            text = " ".join([key, definition.get("label") or "", definition.get("label_ru") or "",
                             definition.get("desc") or ""]).lower()
            if words and all(w in text for w in words):
                found.append(key)
        found.sort(key=len)
        return [{"key": k, "label": (self._definition(k) or {}).get("label"),
                 "type": (self._definition(k) or {}).get("type"), "unit": (self._definition(k) or {}).get("unit", ""),
                 "value": json_safe(self.setting_value_getter()(k))} for k in found[:limit]]

    def setting_errors(self):
        _check_thread("main")
        errors = []
        for stack_name, values in self.user.items():
            index = None if stack_name == "global" else int(stack_name.split("_")[1])
            for key in values:
                info = self._info(key, stack_name, index)
                if info["validation"] == "error" and info["enabled"]:
                    errors.append({"key": key, "label": info["label"], "stack": stack_name, "value": info["value"],
                                   "minimum_value": info["minimum_value"], "maximum_value": info["maximum_value"]})
        return errors

    # Принтер и конфигурация

    def _build_volume(self):
        mode = self._mode()
        return {"width_x": WIDTH, "depth_y": DEPTH, "height_z": MODE_FORMULAS["machine_height"](mode),
                "shape": MODE_FORMULAS["machine_shape"](mode)}

    def config(self):
        _check_thread("main")
        mode = self._mode()
        extruders = []
        for index in (0, 1):
            m = self._material(index)
            extruders.append({"index": index, "name": "Main Extruder" if index == 0 else "Extruder 2",
                              "enabled": True, "material_id": m[0], "material": m[1], "nozzle_mm": 0.4,
                              "filament_diameter_mm": 1.75})
        user_changes = {"count": sum(len(v) for v in self.user.values())}
        for name, values in self.user.items():
            if values:
                user_changes[name] = dict((k, json_safe(v)) for k, v in values.items())
        quality = self._quality()
        result = {
            "machine": MACHINE_NAME + " (имитация)", "definition_id": TARGET_MACHINE, "print_mode": mode,
            "print_mode_title": PRINT_MODE_TITLES.get(mode, mode),
            "quality": {"quality_type": quality[0], "name": self.active_custom or quality[1],
                        "custom_profile": self.active_custom,
                        "layer_height_mm": self._effective("layer_height", "global", None)[0]},
            "extruders": extruders, "active_extruder": 0, "build_volume_mm": self._build_volume(),
            "user_changes": user_changes, "objects_on_plate": len(self.objects), "backend": BACKENDS[mode],
            "slicing": self.monitor.snapshot().get("state"),
        }
        warnings = []
        if mode in FIVE_D_MODES:
            warnings.append(ATSS_NOTE)
        errors = self.setting_errors()
        if errors:
            result["setting_errors"] = errors
        if warnings:
            result["warnings"] = warnings
        return result

    def list_machines(self):
        _check_thread("main")
        return [{"id": "Stereotech Hybrid 530 V5.2.4", "name": MACHINE_NAME, "definition_id": TARGET_MACHINE,
                 "active": True}]

    def select_machine(self, definition_id):
        _check_thread("main")
        if definition_id != TARGET_MACHINE:
            raise SlicerError("В имитации доступен только %s (%s)." % (MACHINE_NAME, TARGET_MACHINE))
        return {"changed": False, "machine": MACHINE_NAME, "definition_id": TARGET_MACHINE}

    # Режим печати

    def _switch_backend(self, old_mode):
        new_mode = self._mode()
        self.monitor.set_mode(new_mode)
        if BACKENDS[new_mode] != BACKENDS[old_mode]:
            self.monitor.set_current(BACKENDS[new_mode])
            self.monitor.on_state(BACKENDS[new_mode], NOT_STARTED)

    def _mode_state(self):
        settings = {}
        for key in MODE_REPORT_KEYS:
            if self._has(key):
                definition = self._definition(key) or {}
                settings[key] = {"value": json_safe(self._effective(key, "global", None)[0]),
                                 "enabled": self._enabled(key, definition)}
        return {"print_mode": self._mode(), "backend": BACKENDS[self._mode()],
                "build_volume_mm": self._build_volume(), "settings": settings}

    def set_print_mode(self, mode):
        _check_thread("main")
        if mode not in PRINT_MODES:
            raise SlicerError("Неизвестный режим %s" % mode)
        before = self._mode_state()
        self.set_setting("printing_mode", mode)
        return before

    def print_mode_report(self):
        _check_thread("main")
        state = self._mode_state()
        state["objects_outside"] = ["%s (%s)" % (o["id"], o["name"]) for o in self.list_objects()
                                    if o.get("outside_build_area")]
        return state

    # Профили

    def list_profiles(self):
        _check_thread("main")
        qualities = [{"quality_type": q[0], "name": q[1], "layer_height_mm": q[2], "available": True,
                      "active": q[0] == self.quality_type and self.active_custom is None} for q in QUALITIES]
        customs = [{"name": name, "quality_type": p["quality_type"], "available": True,
                    "active": name == self.active_custom} for name, p in sorted(self.custom_profiles.items())]
        return {"quality_profiles": qualities, "custom_profiles": customs,
                "active": {"quality_type": self.quality_type, "name": self.active_custom or self._quality()[1],
                           "custom": self.active_custom,
                           "user_changes": sum(len(v) for v in self.user.values())},
                "note": "Названия качеств обманчивы: normal = «Fine» 0.1 мм, fast = «Normal» 0.15 мм, high = "
                        "«Extra Fine» 0.06 мм. Ориентируйся на layer_height_mm."}

    def select_quality(self, quality_type):
        _check_thread("main")
        wanted = quality_type.strip().lower()
        for q in QUALITIES:
            if q[0] == wanted or q[1].lower() == wanted:
                old_mode = self._mode()
                self.quality_type = q[0]
                self.active_custom = None
                if self._mode() != old_mode:
                    self._switch_backend(old_mode)
                self._invalidate()
                return {"quality_type": q[0], "name": q[1], "layer_height_mm":
                        self._effective("layer_height", "global", None)[0],
                        "user_changes_kept": sum(len(v) for v in self.user.values())}
        raise SlicerError("Профиль качества «%s» не найден. Доступны: %s"
                          % (quality_type, ", ".join("%s (%s, %s мм)" % q[:3] for q in QUALITIES)))

    def select_custom_profile(self, name):
        _check_thread("main")
        match = [n for n in self.custom_profiles if n.lower() == name.strip().lower()]
        if not match:
            raise SlicerError("Пользовательский профиль «%s» не найден. Есть: %s"
                              % (name, ", ".join(sorted(self.custom_profiles)) or "нет"))
        old_mode = self._mode()
        self.active_custom = match[0]
        self.quality_type = self.custom_profiles[match[0]]["quality_type"]
        if self._mode() != old_mode:
            self._switch_backend(old_mode)
        self._invalidate()
        return {"custom_profile": match[0], "quality_type": self.quality_type,
                "layer_height_mm": self._effective("layer_height", "global", None)[0],
                "user_changes_kept": sum(len(v) for v in self.user.values())}

    def create_profile(self, name):
        _check_thread("main")
        name = (name or "").strip()
        if not name:
            raise SlicerError("Укажите имя профиля.")
        unique, n = name, 1
        while unique in self.custom_profiles:
            n += 1
            unique = "%s #%d" % (name, n)
        base = self.custom_profiles.get(self.active_custom, {}).get("values", {}) if self.active_custom else {}
        values = dict((stack, dict(base.get(stack, {}))) for stack in self.user)
        moved = 0
        for stack, changes in self.user.items():
            values[stack].update(changes)
            moved += len(changes)
        self.custom_profiles[unique] = {"quality_type": self.quality_type, "values": values}
        self.active_custom = unique
        self.user = {"global": {}, "extruder_0": {}, "extruder_1": {}}
        return {"name": unique, "activated": True, "settings_moved_to_profile": moved,
                "quality_type": self.quality_type,
                "note": None if unique == name else "Имя занято, профиль создан как «%s»." % unique}

    def save_profile(self):
        _check_thread("main")
        if not self.active_custom:
            raise SlicerError("Активен встроенный профиль качества, его нельзя перезаписать. Создайте "
                              "пользовательский профиль (create_profile).")
        profile = self.custom_profiles[self.active_custom]["values"]
        count = 0
        for stack, changes in self.user.items():
            profile.setdefault(stack, {}).update(changes)
            count += len(changes)
        self.user = {"global": {}, "extruder_0": {}, "extruder_1": {}}
        return {"profile": self.active_custom, "saved_changes": count}

    # Материалы

    def list_materials(self, extruder_index=0):
        _check_thread("main")
        if int(extruder_index) not in (0, 1):
            raise SlicerError("Экструдера %s нет: у принтера экструдеры 0..1" % extruder_index)
        active = self.materials[int(extruder_index)]
        result = []
        for m in MATERIALS:
            result.append({"id": m[0], "name": m[1], "brand": m[2], "type": m[3], "series": m[4],
                           "diameter_mm": 1.75, "density_g_cm3": m[5], "compatible": True,
                           "active": m[0] == active, "description": ""})
        return result

    def set_material(self, material_id, extruder_index=0):
        _check_thread("main")
        index = int(extruder_index)
        materials = self.list_materials(index)
        root_id = material_id if any(m["id"] == material_id for m in materials) else pick_material(materials,
                                                                                                    material_id)
        previous = self.materials[index]
        self.materials[index] = root_id
        self._invalidate()
        values = self._material_values(index)
        result = {"extruder": index, "material_id": root_id, "name": self._material(index)[1], "previous": previous,
                  "quality_type": self.quality_type,
                  "material_print_temperature": self._effective("material_print_temperature", "extruder_%d" % index,
                                                                index)[0],
                  "material_bed_temperature": values["material_bed_temperature"],
                  "cool_fan_speed": values["cool_fan_speed"], "retraction_amount": values["retraction_amount"]}
        if self.user["extruder_%d" % index]:
            result["note"] = ("У экструдера есть пользовательские изменения (%s) — они перекрывают значения "
                              "материала." % ", ".join(sorted(self.user["extruder_%d" % index])))
        return result

    # Сцена

    def _object_dict(self, obj, detailed=False):
        world = obj.world()
        lo, hi = _bbox(world)
        center = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
        axis, angle = coords.rotation_axis_angle(obj.rotation)
        info = {
            "id": self._ids.short_id(obj), "name": obj.name,
            "center_mm": coords.round_list(center), "size_mm": coords.round_list([hi[i] - lo[i] for i in range(3)]),
            "bottom_z_mm": round(lo[2], 2) + 0.0, "on_bed": abs(lo[2]) < 0.05,
            "min_mm": coords.round_list(lo), "max_mm": coords.round_list(hi),
            "rotation": {"angle_deg": round(angle, 2), "axis": coords.round_list(axis, 3) if axis else None},
            "scale": coords.round_list(obj.scale, 4),
            "outside_build_area": self._outside(world, lo, hi),
            "centered_on_5d_axis": abs(center[0]) < 0.5 and abs(center[1]) < 0.5,
        }
        if detailed:
            info["triangles"] = len(obj.faces)
            info["volume_cm3"] = round(coords.mesh_volume(world, obj.faces) / 1000.0, 3)
            info["bed_contact_area_mm2"] = round(coords.bottom_contact_area(world, obj.faces, up_axis=2), 1)
            if coords.numpy is not None:
                radius = float(coords.numpy.sqrt(world[:, 0] ** 2 + world[:, 1] ** 2).max())
            else:
                radius = max(math.sqrt(v[0] ** 2 + v[1] ** 2) for v in world)
            info["max_radius_from_5d_axis_mm"] = round(radius, 2)
            info["build_volume_mm"] = self._build_volume()
        return info

    def _outside(self, world, lo, hi):
        mode = self._mode()
        height = MODE_FORMULAS["machine_height"](mode)
        if hi[2] > height + 1e-6:
            return True
        if mode == "classic":
            return lo[0] < -169.5 or hi[0] > 150.5 or lo[1] < -DEPTH / 2 or hi[1] > DEPTH / 2
        a, b = WIDTH / 2.0, DEPTH / 2.0
        if coords.numpy is not None:
            return bool(((world[:, 0] / a) ** 2 + (world[:, 1] / b) ** 2).max() > 1.0)
        return any((v[0] / a) ** 2 + (v[1] / b) ** 2 > 1.0 for v in world)

    def _brief(self):
        return [{"id": self._ids.short_id(o), "name": o.name, "obj": o} for o in self.objects]

    def _find(self, ref):
        return resolve_object(self._brief(), ref)["obj"]

    def list_objects(self):
        _check_thread("main")
        return [self._object_dict(o) for o in self.objects]

    def object_info(self, ref, detailed=True):
        _check_thread("main")
        return self._object_dict(self._find(ref), detailed=detailed)

    def get_object_world_vertices(self, ref, frame="scene"):
        _check_thread("main")
        world = self._find(ref).world()
        points = [tuple(float(c) for c in v) for v in (world.tolist() if hasattr(world, "tolist") else world)]
        if frame == "scene":
            points = [coords.printer_to_scene(p) for p in points]
        return points

    def get_object_mesh(self, ref, frame="scene"):
        _check_thread("main")
        obj = self._find(ref)
        return self.get_object_world_vertices(ref, frame), [tuple(f) for f in obj.faces]

    def get_object_local_mesh(self, ref):
        """Данные сетки с масштабом узла, без поворота и смещения (оси сцены)."""
        _check_thread("main")
        obj = self._find(ref)
        local = obj.local.tolist() if hasattr(obj.local, "tolist") else obj.local
        sx, sy, sz = obj.scale
        points = [coords.printer_to_scene((float(v[0]) * sx, float(v[1]) * sy, float(v[2]) * sz)) for v in local]
        return points, [tuple(f) for f in obj.faces]

    def load_model(self, path, clear_scene=False, timeout=120.0, cancel_event=None, center=None):
        _check_thread("worker")
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise SlicerError("Файл не найден: %s" % path)
        nodes = read_nodes(path)                   # как ReadMeshJob: разбор в рабочем потоке
        if not nodes:
            raise SlicerError("Файл прочитан, но объектов в сцене не появилось (пустая модель).")
        time.sleep(0.2)
        if cancel_event is not None and cancel_event.is_set():
            raise SlicerError("Загрузка прервана пользователем.")
        return run_in_main_thread(self._add_objects, os.path.basename(path), nodes, clear_scene, center)

    def _add_objects(self, name, nodes, clear_scene, center):
        _check_thread("main")
        with self._lock:
            if clear_scene:
                self.objects = []
            added = []
            for spec in nodes:
                obj = MockObject.from_spec(name, spec)   # каждому узлу имя файла (как _readMeshFinished)
                if spec.translation is None:
                    # Как расстановщик слайсера: рядом с уже стоящими объектами.
                    offset_x = 0.0
                    for other in self.objects:
                        lo, hi = other.bbox()
                        offset_x = max(offset_x, hi[0] + 10.0)
                    lo, hi = obj.bbox()
                    obj.place(center_xy=(offset_x + (hi[0] - lo[0]) / 2.0 if self.objects else 0.0, 0.0),
                              drop=True)
                else:
                    obj.place(drop=True)   # 3MF: положение из файла сохраняем, объект кладём на стол
                self.objects.append(obj)
                added.append(obj)
            mode = self._mode()
            if center is None:
                center = mode in FIVE_D_MODES and len(added) == 1 and len(self.objects) == 1
            if center:
                for obj in added:
                    obj.place(center_xy=(0.0, 0.0), drop=True)
            self._invalidate()
        notes = []
        if center:
            notes.append("Объект поставлен центром на ось (X=0, Y=0) и на стол.")
        if mode in FIVE_D_MODES and not center:
            notes.append("Режим 5D: модель должна стоять осью вращения на оси стола — после загрузки " + AXIS_HINT)
        return {"loaded": [self._object_dict(obj, detailed=True) for obj in added], "print_mode": mode,
                "notes": notes}

    def _result_with(self, obj, warnings):
        result = self._object_dict(obj)
        if result["outside_build_area"]:
            warnings.append("Объект выходит за рабочую область — он не будет нарезан.")
        if self._mode() in FIVE_D_MODES and not result["centered_on_5d_axis"]:
            warnings.append("Режим 5D: центр габаритов не в X=0, Y=0 — " + AXIS_HINT)
        if warnings:
            result["warnings"] = warnings
        return result

    def move_object(self, ref, x=None, y=None, z=None, relative=False):
        _check_thread("main")
        obj = self._find(ref)
        lo, hi = obj.bbox()
        center = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
        if relative:
            delta = (x or 0.0, y or 0.0, z or 0.0)
        else:
            delta = ((center[0] if x is None else x) - center[0], (center[1] if y is None else y) - center[1],
                     (0.0 if z is None else z) - lo[2])
        for i in range(3):
            obj.translation[i] += delta[i]
        warnings = []
        if obj.bbox()[0][2] > 0.05:
            warnings.append("Объект поднят над столом, но включено автоматическое опускание — слайсер опустит его.")
            obj.place(drop=True)
        self._invalidate()
        return self._result_with(obj, warnings)

    def _rotate(self, obj, matrix, center_xy=None, drop=True, reset=False):
        """`matrix` — в системе принтера, вокруг неподвижных мировых осей."""
        lo, hi = obj.bbox()
        keep = ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0)
        base = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]] if reset else obj.rotation
        obj.rotation = coords.matmul(matrix, base)
        obj.place(center_xy=center_xy if center_xy is not None else keep, drop=drop)
        self._invalidate()

    def lay_flat(self, ref):
        _check_thread("main")
        obj = self._find(ref)
        normal, area = coords.largest_flat_face(obj.world(), obj.faces)
        if normal is None:
            raise SlicerError("Не удалось найти плоскую грань.")
        self._rotate(obj, coords.rotation_between(normal, (0.0, 0.0, -1.0)))
        result = self._result_with(obj, [])
        result["note"] = "Уложен на плоскую грань площадью %.0f мм²." % area
        return result

    def apply_rotation_matrix(self, ref, matrix, frame="scene", drop_to_bed=True, center_xy=None, reset=False):
        _check_thread("main")
        try:
            coords.check_rotation_matrix(matrix)
        except ValueError as exc:
            raise SlicerError("Некорректная матрица поворота: %s" % exc)
        if frame == "scene":   # мок хранит координаты принтера: M_p = P M_s P^T
            p = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]
            matrix = coords.matmul(p, coords.matmul(matrix, coords.transpose(p)))
        obj = self._find(ref)
        self._rotate(obj, matrix, center_xy=center_xy, drop=drop_to_bed, reset=reset)
        return self._result_with(obj, [])

    def scale_object(self, ref, factors):
        _check_thread("main")
        obj = self._find(ref)
        lo, hi = obj.bbox()
        keep = ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0)
        obj.scale = [float(f) for f in factors]
        obj.place(center_xy=keep, drop=True)
        self._invalidate()
        return self._result_with(obj, [])

    def center_for_5d(self, ref):
        _check_thread("main")
        obj = self._find(ref)
        obj.place(center_xy=(0.0, 0.0), drop=True)
        self._invalidate()
        result = self._result_with(obj, [])
        result["note"] = ("Центр габаритов по X/Y совмещён с осью вращения (0, 0), низ на столе. Ориентацию "
                          "(ось детали вертикально) задаёт отдельный шаг.")
        return result

    def arrange_all(self, timeout=20.0):
        _check_thread("worker")
        time.sleep(0.2)   # ArrangeObjectsJob работает асинхронно
        return run_in_main_thread(self._arrange)

    def _arrange(self):
        _check_thread("main")
        if not self.objects:
            raise SlicerError("На столе нет объектов.")
        widths = [o.bbox()[1][0] - o.bbox()[0][0] for o in self.objects]
        total = sum(widths) + 10.0 * (len(widths) - 1)
        x = -total / 2.0
        for obj, width in zip(self.objects, widths):
            obj.place(center_xy=(x + width / 2.0, 0.0), drop=True)
            x += width + 10.0
        self._invalidate()
        result = {"objects": self.list_objects()}
        if self._mode() in FIVE_D_MODES:
            result["warning"] = ("Режим 5D: авторасстановка ставит модели по 3D-сетке. Для 5D-печати модель "
                                 "должна стоять на оси — " + AXIS_HINT)
        return result

    def duplicate_object(self, ref, copies, timeout=60.0):
        _check_thread("worker")
        if self._mode() in FIVE_D_MODES:
            raise SlicerError("Копирование доступно только в 3D: в 5D деталь стоит на оси вращения, копии не помещаются.")
        return run_in_main_thread(lambda: self._duplicate(ref, int(copies)))

    def _duplicate(self, ref, copies):
        import copy as _copy
        source = self._find(ref)
        for _i in range(copies):
            clone = _copy.copy(source)
            clone.rotation = [list(r) for r in source.rotation]
            clone.scale = list(source.scale)
            clone.translation = list(source.translation)
            self.objects.append(clone)
        # простая укладка рядами в прямоугольник стола, зазор 8 мм
        x, y, row_h = -WIDTH / 2.0 + 8.0, -DEPTH / 2.0 + 8.0, 0.0
        for obj in self.objects:
            lo, hi = obj.bbox()
            w, d = hi[0] - lo[0], hi[1] - lo[1]
            if x + w > WIDTH / 2.0 - 8.0:
                x, y, row_h = -WIDTH / 2.0 + 8.0, y + row_h + 8.0, 0.0
            obj.place(center_xy=(x + w / 2.0, y + d / 2.0), drop=True)
            x, row_h = x + w + 8.0, max(row_h, d)
        self._invalidate()
        objects = self.list_objects()
        outside = [o["id"] for o in objects if o.get("outside_build_area") in (True, "True")]
        brief = [{"id": o.get("id"), "center_mm": o.get("center_mm"), "outside_build_area": o.get("outside_build_area")}
                 for o in objects]
        return {"objects": brief, "copies_requested": copies, "total": len(objects),
                "inside_build_area": len(objects) - len(outside), "outside_build_area_ids": outside}

    def delete_objects(self, refs=None, delete_all=False):
        _check_thread("main")
        if delete_all:
            removed = list(self.objects)
        else:
            if not refs:
                raise SlicerError("Укажите object_ids или all=true.")
            removed = []
            for ref in refs:
                obj = self._find(ref)
                if obj not in removed:
                    removed.append(obj)
        names = ["%s (%s)" % (self._ids.short_id(o), o.name) for o in removed]
        self.objects = [o for o in self.objects if o not in removed]
        self._invalidate()
        return {"deleted": names, "remaining": len(self.objects)}

    # Нарезка

    def _invalidate(self):
        """Любая правка сцены или настроек сбрасывает G-code (needsSlicing -> NotStarted)."""
        self._slice_cancel.set()
        backend = BACKENDS[self._mode()]
        state = self.monitor.snapshot().get("state")
        if state in ("done", "processing"):
            self.monitor.on_state(backend, NOT_STARTED)
            self.monitor.on_progress(backend, 0.0)
        self.gcode = None

    def monitor_snapshot(self):
        return self.monitor.snapshot()

    def slicing_status(self):
        _check_thread("main")
        return self.monitor.snapshot()

    def slicing_precheck(self):
        _check_thread("main")
        errors, warnings = [], []
        mode = self._mode()
        objects = self.list_objects()
        if not objects:
            errors.append("На столе нет моделей для печати. Загрузите модель (load_model).")
        else:
            outside = [o for o in objects if o["outside_build_area"]]
            if len(outside) == len(objects):
                errors.append("Все модели вне рабочей области (%s): переместите/поверните/уменьшите их. Рабочая "
                              "область сейчас %s." % (", ".join(o["id"] + " " + o["name"] for o in outside),
                                                     self._build_volume()))
            elif outside:
                warnings.append("Модели вне рабочей области не будут нарезаны: %s"
                                % ", ".join(o["id"] + " " + o["name"] for o in outside))
            if mode in FIVE_D_MODES:
                off_axis = [o for o in objects if not o["centered_on_5d_axis"]]
                if off_axis:
                    warnings.append("5D: центр габаритов не в X=0, Y=0: %s — " % ", ".join(o["id"] + " " + o["name"] for o in off_axis) + AXIS_HINT)
        setting_errors = self.setting_errors()
        if setting_errors:
            errors.append("Ошибки в настройках (слайсер откажется резать): %s" % "; ".join(
                "%s «%s» = %s (допустимо %s..%s, %s)" % (e["key"], e["label"], e["value"], e["minimum_value"],
                                                          e["maximum_value"], e["stack"]) for e in setting_errors))
        if mode in FIVE_D_MODES:
            warnings.append(ATSS_NOTE)
        return {"errors": errors, "warnings": warnings, "mode": mode, "objects": len(objects)}

    def start_slicing(self):
        _check_thread("main")
        mode = self._mode()
        backend = BACKENDS[mode]
        self._slice_cancel.set()
        self._slice_cancel = threading.Event()
        generation = self.monitor.mark_started(backend, mode)
        self.monitor.on_state(backend, NOT_STARTED)      # stopSlicing() внутри slice()
        self.monitor.on_state(backend, PROCESSING)
        started = self.monitor.finish_start()
        snapshot = self._result_inputs()
        thread = threading.Thread(target=self._simulate, name="MockSlicer-slice",
                                  args=(backend, mode, self._slice_cancel, snapshot))
        thread.daemon = True
        thread.start()
        self._slice_thread = thread
        return {"generation": generation, "started": started, "backend": backend, "mode": mode}

    def _simulate(self, backend, mode, cancel, inputs):
        if mode == "classic":
            stages = [(i / 10.0) for i in range(1, 11)]
        else:
            stages = [0.1, 0.15, 0.25, 0.35, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0]
        for value in stages:
            if cancel.wait(self.slice_step):
                return
            if mode in FIVE_D_MODES and value >= 0.55 and not self.atss_license:
                while not cancel.wait(0.2):   # glicer бесконечно ждёт окна лицензии ATSS
                    pass
                return
            if value >= 1.0:
                break
            self.monitor.on_progress(backend, value)
            self.monitor.on_state(backend, PROCESSING)
        with self._lock:
            if cancel.is_set():
                return
            self.gcode = self._make_gcode(mode, inputs)
            self.last_result = inputs["result"]
            self.monitor.on_state(backend, DONE)
            self.monitor.on_progress(backend, 1.0)

    def _result_inputs(self):
        mode = self._mode()
        getter = self.setting_value_getter()
        layer = float(getter("layer_height") or 0.1)
        infill = float(getter("infill_sparse_density") or 20)
        speed = float(getter("speed_print") or 60)
        volume = 0.0
        height = 0.0
        for obj in self.objects:
            info = self._object_dict(obj, detailed=True)
            if info["outside_build_area"]:
                continue
            volume += info["volume_cm3"]
            height = max(height, info["size_mm"][2])
        materials = []
        material = self._material(0)
        fill = 0.3 + 0.7 * infill / 100.0
        grams = volume * fill * material[5] + (3.0 if mode == "cylindrical_full" else 0.5)
        length = grams / material[5] / (math.pi * 0.875 ** 2) * 1000.0 / 1000.0
        materials.append({"extruder": 0, "material_id": material[0], "name": material[1],
                          "length_m": round(length, 2), "weight_g": round(grams, 1),
                          "volume_cm3": round(grams / material[5], 2), "cost": None})
        seconds = int(grams * 75 * (0.15 / layer) ** 0.6 * (60.0 / speed) + 120)
        layers = int(math.ceil(height / layer)) if height else 0
        return {"result": {"print_time_s": seconds, "print_time_text": format_duration(seconds),
                           "material": materials, "mode": mode, "state": "done", "job_name":
                           os.path.splitext(self.objects[0].name)[0] if self.objects else "",
                           "layers": layers},
                "layer": layer, "layers": layers}

    def _make_gcode(self, mode, inputs):
        result = inputs["result"]
        lines = [";FLAVOR:RepRap", ";TIME:%d" % result["print_time_s"],
                 ";Filament used: %.2fm" % result["material"][0]["length_m"], ";Layer height: %s" % inputs["layer"],
                 ";Generated with Cura_SteamEngine (mock)", ";STE Slicer 5D Gcode", "G1 F200 E15",
                 # {coordinate_system}: CLIParserBackend/StartSliceJob.py:914-919 -> G56 для обоих режимов 5D;
                 # CuraEngineBackend/StartSliceJob.py:528-536 (сердечник 5D Full) -> G56 с цилиндрической подложкой
                 # (по умолчанию "solid"), G55 только при cylindrical_raft_type = none; classic на гибриде -> G54.
                 "G54" if mode == "classic" else "G56",
                 ";LAYER_COUNT:%d" % inputs["layers"]]
        for n in range(min(inputs["layers"], 400)):
            lines.append(";LAYER:%d" % n)
            lines.append("G1 X%.2f Y%.2f Z%.2f E%.4f" % (10.0, 10.0, (n + 1) * inputs["layer"], 0.1 * n))
        if mode in FIVE_D_MODES:
            lines.append("G0 A90")
            lines.append(";cylindrical layers (mock)")
            lines.append("G1 X5 Y0 Z10 A180 E1.0")
        lines.append(";END")
        return "\n".join(lines) + "\n"

    def cancel_slicing(self):
        _check_thread("main")
        self._slice_cancel.set()
        backend = BACKENDS[self._mode()]
        self.monitor.on_state(backend, NOT_STARTED)
        return self.monitor.snapshot()

    def slicing_result(self):
        _check_thread("main")
        if self.monitor.snapshot().get("state") != "done" or not getattr(self, "last_result", None):
            raise SlicerError("Результата слайсинга нет: запустите start_slicing и дождитесь завершения.")
        return dict(self.last_result)

    def _ready(self):
        _check_thread("main")
        if self.monitor.snapshot().get("state") != "done" or not self.gcode:
            raise SlicerError("G-code ещё не готов (состояние слайсинга: %s). Запустите start_slicing и дождитесь "
                              "done; любое изменение сцены/настроек сбрасывает G-code."
                              % self.monitor.snapshot().get("state"))
        mode = self._mode()
        header = ";PRINTING_MODE:[%s]\n;VERSION:7.10.12 (mock)\n" % mode
        return header + self.gcode, mode

    def export_gcode(self, path):
        _check_thread("worker")
        text, mode = run_in_main_thread(self._ready)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        result = inspect_gcode(path)
        result["mode"] = mode
        return result

    def save_project(self, path):
        _check_thread("worker")
        data = run_in_main_thread(self._project_bytes)
        with open(path, "wb") as handle:
            handle.write(data)
        result = inspect_3mf(path)
        result["objects"] = len(self.objects)
        if not self.objects:
            result["warning"] = "В проекте нет моделей."
        return result

    def _project_bytes(self):
        """Проект 3MF в раскладке ThreeMFWorkspaceWriter (см. devtools/ste3mf.py)."""
        _check_thread("main")
        buffer = io.BytesIO()
        machine_id = MACHINE_NAME
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml",
                             '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.'
                             'org/package/2006/content-types"><Default Extension="rels" ContentType="application/'
                             'vnd.openxmlformats-package.relationships+xml" /><Default Extension="model" '
                             'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml" /></Types>')
            archive.writestr("_rels/.rels",
                             '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.'
                             'openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.'
                             'model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" '
                             '/></Relationships>')
            archive.writestr("3D/3dmodel.model", self._model_xml())
            stacks = [("global", None)] + [("extruder_%d" % i, i) for i in (0, 1)]
            for stack_name, index in stacks:
                user_id = "%s_%s_user" % (machine_id, stack_name)
                quality_id = self.quality_type if index is None else "%s_%d" % (self.quality_type, index)
                qc_id = "empty_quality_changes"
                if self.active_custom:
                    qc_id = "%s_%s" % (stack_name, self.active_custom.lower().replace(" ", "_"))
                    archive.writestr("SteSlicer/%s.inst.cfg" % qc_id, _inst_cfg(
                        self.active_custom, "quality_changes", self.custom_profiles[self.active_custom]["values"]
                        .get(stack_name, {}), index, self.quality_type))
                material_id = "empty_material" if index is None else self.materials[index]
                containers = [user_id, qc_id, quality_id, material_id, "empty_variant",
                              "%s_settings" % stack_name, TARGET_MACHINE if index is None else
                              ("stereotech_v5.2.4_extruder_main" if index == 0 else
                               "stereotech_v5.2.4_extruder_second")]
                archive.writestr("SteSlicer/%s.inst.cfg" % user_id,
                                 _inst_cfg("empty", "user", self.user[stack_name], index, None))
                if index is not None:
                    archive.writestr("SteSlicer/%s.xml.fdm_material" % material_id,
                                     _material_xml(self._material(index)))
                if index is None:
                    archive.writestr("SteSlicer/%s.inst.cfg" % quality_id,
                                     _inst_cfg(self._quality()[1], "quality", self._quality_values(), None,
                                               self.quality_type))
                stack_id = machine_id if index is None else "%s_extruder_%d" % (machine_id, index)
                lines = ["[general]", "version = 4", "name = %s" % stack_id, "id = %s" % stack_id, "",
                         "[metadata]", "type = %s" % ("machine" if index is None else "extruder_train")]
                if index is not None:
                    lines += ["position = %d" % index, "machine = %s" % machine_id]
                lines += ["", "[containers]"] + ["%d = %s" % (i, c) for i, c in enumerate(containers)] + [""]
                suffix = "global" if index is None else "extruder"
                archive.writestr("SteSlicer/%s.%s.cfg" % (stack_id, suffix), "\n".join(lines))
            archive.writestr("SteSlicer/version.ini", "[versions]\nsteslicer_version = 7.10.12 (mock)\n"
                                                       "build_type = mock\nis_debug_mode = False\n")
        return buffer.getvalue()

    def _model_xml(self):
        parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/'
                 'core/2015/02"><resources>']
        items = []
        for n, obj in enumerate(self.objects, 1):
            local = obj.local.tolist() if hasattr(obj.local, "tolist") else obj.local
            parts.append('<object id="%d" name="%s" type="model"><mesh><vertices>' % (n, _xml_escape(obj.name)))
            for v in local:    # данные сетки храним в системе сцены (Y вверх), как и слайсер
                s = coords.printer_to_scene(v)
                parts.append('<vertex x="%.4f" y="%.4f" z="%.4f" />' % s)
            parts.append("</vertices><triangles>")
            for f in obj.faces:
                parts.append('<triangle v1="%d" v2="%d" v3="%d" />' % tuple(f))
            parts.append("</triangles></mesh></object>")
            # преобразование элемента M = T(W/2, D/2, 0) * F * W_node: линейная часть R_p * S_p * F (к вершинам сцены)
            f_matrix = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]
            rs = [[obj.rotation[r][c] * obj.scale[c] for c in range(3)] for r in range(3)]
            lin = coords.matmul(rs, f_matrix)
            t = (obj.translation[0] + WIDTH / 2.0, obj.translation[1] + DEPTH / 2.0, obj.translation[2])
            values = [lin[0][0], lin[1][0], lin[2][0], lin[0][1], lin[1][1], lin[2][1], lin[0][2], lin[1][2],
                      lin[2][2], t[0], t[1], t[2]]
            items.append('<item objectid="%d" transform="%s" />' % (n, " ".join("%.6f" % v for v in values)))
        parts.append("</resources><build>%s</build></model>" % "".join(items))
        return "".join(parts)


def _inst_cfg(name, container_type, values, position, quality_type):
    lines = ["[general]", "version = 4", "name = %s" % name, "definition = %s" % TARGET_MACHINE, "",
             "[metadata]", "type = %s" % container_type, "setting_version = 5"]
    if quality_type:
        lines.append("quality_type = %s" % quality_type)
    if position is not None:
        lines.append("position = %d" % position)
    lines += ["", "[values]"]
    for key, value in sorted(values.items()):
        lines.append("%s = %s" % (key, value))
    return "\n".join(lines) + "\n"


def _material_xml(item):
    """Минимальный .xml.fdm_material — так слайсер кладёт активный материал в проект."""
    (root_id, name, brand, polymer, _series, density, t_nozzle, t_bed, t_standby, fan, retract,
     retract_speed) = item[:12]
    settings = [("print temperature", t_nozzle), ("heated bed temperature", t_bed),
                ("standby temperature", t_standby), ("print cooling", fan), ("retraction amount", retract),
                ("retraction speed", retract_speed)]
    return ('<?xml version="1.0" encoding="utf-8"?>\n<fdmmaterial xmlns="http://www.ultimaker.com/material" '
            'version="1.3"><metadata><name><brand>%s</brand><material>%s</material><color>Generic</color>'
            '<label>%s</label></name><GUID>mock-%s</GUID><version>1</version></metadata><properties>'
            '<density>%s</density><diameter>1.75</diameter></properties><settings>%s</settings></fdmmaterial>\n'
            % (_xml_escape(brand), _xml_escape(polymer), _xml_escape(name), root_id, density,
               "".join('<setting key="%s">%s</setting>' % (key, value) for key, value in settings)))


def _xml_escape(text):
    return re.sub(r"[<>&\"]", lambda m: {"<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;"}[m.group(0)], text)


def install(registry=None, **kwargs):
    """Ставит мок вместо адаптера слайсера (обработчики в реестре остаются настоящими) и возвращает его."""
    adapter = MockSlicerAdapter(**kwargs)
    set_adapter(adapter)
    return adapter
