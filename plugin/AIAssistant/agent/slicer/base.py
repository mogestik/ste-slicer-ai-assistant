"""Интерфейс адаптера, ошибки и вспомогательные функции, общие для настоящего слайсера и мока."""

import functools
import itertools
import threading
import weakref


class SlicerError(Exception):
    """Ожидаемая ошибка операции со слайсером, текст для пользователя (по-русски)."""


class SlicerUnavailable(SlicerError):
    """Приложение слайсера недоступно."""


def tool_errors(handler):
    """Декоратор обработчиков: SlicerError -> ToolError (текст уходит модели как есть)."""
    @functools.wraps(handler)
    def wrapper(args, ctx):
        try:
            return handler(args, ctx)
        except SlicerError as exc:
            from ..tools.registry import ToolError
            raise ToolError(str(exc))
    return wrapper


STATE_NAMES = {1: "not_started", 2: "processing", 3: "done", 4: "error", 5: "disabled"}
PRINT_MODES = ("classic", "cylindrical", "cylindrical_full")
PRINT_MODE_TITLES = {
    "classic": "3D (classic) — послойная печать на столе",
    "cylindrical": "5D Spiral (cylindrical) — спиральные цилиндрические слои на оснастке, без сердечника",
    "cylindrical_full": "5D (cylindrical_full) — сердечник плоскими слоями + цилиндрические слои вокруг оси",
}
FIVE_D_MODES = ("cylindrical", "cylindrical_full")
TARGET_MACHINE = "hte530v5.2.4"

# Настройки, которые показываем после смены режима печати (значения читаем из стека).
MODE_REPORT_KEYS = (
    "machine_height", "machine_shape", "machine_center_is_zero", "adhesion_type", "cylindrical_raft_type",
    "cylindrical_mode_base_diameter", "cylindrical_mode_overlap", "non_printing_base_diameter",
    "cylindrical_layer_height", "support_enable_cylindrical", "support_enable_top_support",
)

ATSS_NOTE = ("Режимы 5D (cylindrical, cylindrical_full) нарезаются внешней программой glicer, которой нужна "
             "активированная лицензия ATSS. Без лицензии слайсинг зависает на ~50-60 % и завершается по таймауту.")


class ObjectIds(object):
    """Устойчивые короткие id ("1", "2", ...) для узлов сцены.

    Сам слайсер различает узлы по id(node): число длинное и после удаления узла может
    достаться другому. Короткие id модели проще переписывать без ошибок. Узлы храним
    слабыми ссылками.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._by_short = {}     # короткий id -> weakref(узел)
        self._by_python = {}    # id(node) -> (short id, weakref(node))

    def short_id(self, node):
        with self._lock:
            entry = self._by_python.get(id(node))
            if entry is not None and entry[1]() is node:
                return entry[0]
            short = str(next(self._counter))
            try:
                ref = weakref.ref(node)
            except TypeError:  # объекты без поддержки weakref (в тестах)
                ref = _StrongRef(node)
            self._by_python[id(node)] = (short, ref)
            self._by_short[short] = ref
            return short

    def node(self, short):
        with self._lock:
            ref = self._by_short.get(str(short))
        return ref() if ref is not None else None

    def forget_dead(self):
        with self._lock:
            for short, ref in list(self._by_short.items()):
                if ref() is None:
                    del self._by_short[short]
            for key, (short, ref) in list(self._by_python.items()):
                if ref() is None:
                    del self._by_python[key]


class _StrongRef(object):
    def __init__(self, obj):
        self._obj = obj

    def __call__(self):
        return self._obj


def resolve_object(objects, ref):
    """Выбирает объект из `objects` по короткому id, id слайсера или уникальному имени.

    `objects` — список словарей с "id", "name" (и, возможно, "slicer_id"). Если выбрать
    нельзя — SlicerError с подсказкой.
    """
    if not objects:
        raise SlicerError("На столе нет объектов. Сначала загрузите модель (load_model).")
    text = str(ref).strip() if ref is not None else ""
    if not text:
        if len(objects) == 1:
            return objects[0]
        raise SlicerError("Не указан объект. Объекты на столе: %s" % _listing(objects))
    for obj in objects:
        if obj["id"] == text or str(obj.get("slicer_id")) == text:
            return obj
    exact = [obj for obj in objects if obj["name"] == text]
    if len(exact) == 1:
        return exact[0]
    folded = [obj for obj in objects if obj["name"].lower() == text.lower()]
    if len(folded) == 1:
        return folded[0]
    if len(exact) > 1 or len(folded) > 1:
        raise SlicerError("Имя «%s» неоднозначно, укажите id. Объекты: %s" % (text, _listing(objects)))
    partial = [obj for obj in objects if text.lower() in obj["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    raise SlicerError("Объект «%s» не найден. Объекты на столе: %s" % (text, _listing(objects)))


def _listing(objects):
    return ", ".join("%s (%s)" % (obj["id"], obj["name"]) for obj in objects[:20])


class SlicerAdapter(object):
    """Интерфейс, который реализуют QtSlicerAdapter и мок стенда.

    Возвращаются словари и списки, которые можно сериализовать в JSON. Координаты —
    координаты принтера в мм (X вправо, Y назад, Z вверх, начало — центр стола на его
    поверхности), углы — в градусах.

    Пометки потоков: [main] — только из главного потока Qt; [worker] — только из рабочего
    потока (ждёт слайсер); [any] — из любого потока.
    """

    name = "abstract"

    # Принтер и конфигурация
    def config(self):                                         # [main]
        raise NotImplementedError

    def list_machines(self):                                  # [main]
        raise NotImplementedError

    def select_machine(self, definition_id):                  # [main]
        raise NotImplementedError

    # Настройки
    def setting_info(self, key, scope="global", extruder_index=0):   # [main] dict или None (ключ неизвестен)
        raise NotImplementedError

    def setting_value_getter(self, scope="global", extruder_index=0):   # [main] функция ключ -> значение
        raise NotImplementedError

    def set_setting(self, key, value, scope="global", extruder_index=0):   # [main] -> setting_info
        raise NotImplementedError

    def reset_settings(self, keys=None, scope="global", extruder_index=0):  # [main] -> dict
        raise NotImplementedError

    def search_settings(self, query, limit=15):              # [main] запасной поиск без справочника
        raise NotImplementedError

    def setting_errors(self):                                  # [main] список словарей
        raise NotImplementedError

    def print_mode(self):                                      # [main]
        raise NotImplementedError

    def set_print_mode(self, mode):                            # [main] -> dict (состояние до)
        raise NotImplementedError

    def print_mode_report(self):                               # [main] -> dict (состояние после)
        raise NotImplementedError

    # Профили и материалы
    def list_profiles(self):                                   # [main]
        raise NotImplementedError

    def select_quality(self, quality_type):                    # [main]
        raise NotImplementedError

    def select_custom_profile(self, name):                     # [main]
        raise NotImplementedError

    def create_profile(self, name):                            # [main]
        raise NotImplementedError

    def save_profile(self):                                    # [main]
        raise NotImplementedError

    def list_materials(self, extruder_index=0):                # [main]
        raise NotImplementedError

    def set_material(self, material_id, extruder_index=0):     # [main]
        raise NotImplementedError

    # Сцена
    def list_objects(self):                                    # [main]
        raise NotImplementedError

    def object_info(self, ref, detailed=True):                 # [main]
        raise NotImplementedError

    def load_model(self, path, clear_scene=False, timeout=120.0, cancel_event=None, center=None):   # [worker]
        """center: True/False — ставить ли объект на ось 5D; None — автоматически в режимах 5D."""
        raise NotImplementedError

    def move_object(self, ref, x=None, y=None, z=None, relative=False):   # [main]
        raise NotImplementedError

    def lay_flat(self, ref):                                   # [main] самой большой плоской гранью вниз
        raise NotImplementedError

    def scale_object(self, ref, factors):                      # [main] factors: (sx, sy, sz) по осям принтера
        raise NotImplementedError

    def center_for_5d(self, ref):                              # [main]
        raise NotImplementedError

    def arrange_all(self, timeout=20.0):                       # [worker]
        raise NotImplementedError

    def delete_objects(self, refs=None, delete_all=False):     # [main]
        raise NotImplementedError

    def get_object_world_vertices(self, ref, frame="scene"):   # [main]
        raise NotImplementedError

    def get_object_mesh(self, ref, frame="scene"):             # [main]
        raise NotImplementedError

    def get_object_local_mesh(self, ref):                      # [main] (vertices, faces)
        """Сетка в осях сцены (Y вверх) с масштабом узла, но без его поворота и смещения."""
        raise NotImplementedError

    def apply_rotation_matrix(self, ref, matrix, frame="scene", drop_to_bed=True, center_xy=None,
                              reset=False):                    # [main]
        """Поворот матрицей 3x3 вокруг неподвижных мировых осей (reset=True — от ориентации из файла)."""
        raise NotImplementedError

    # Нарезка
    def monitor_snapshot(self):                                # [any] снимок состояния (SliceMonitor)
        raise NotImplementedError

    def slicing_status(self):                                  # [main] снимок, дополненный данными сцены
        raise NotImplementedError

    def slicing_precheck(self):                                # [main] {"errors": [...], "warnings": [...]}
        raise NotImplementedError

    def start_slicing(self):                                   # [main] -> slicing_status
        raise NotImplementedError

    def cancel_slicing(self):                                  # [main]
        raise NotImplementedError

    def slicing_result(self):                                  # [main]
        raise NotImplementedError

    def export_gcode(self, path):                              # [worker]
        raise NotImplementedError

    def save_project(self, path):                              # [worker]
        raise NotImplementedError
