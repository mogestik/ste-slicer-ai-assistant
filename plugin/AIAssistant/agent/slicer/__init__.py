"""Слой адаптера между обработчиками инструментов и API STE Slicer.

Обработчики (agent/tools/*_tools.py) никогда не импортируют UM/steslicer напрямую: они
вызывают методы активного адаптера, который возвращает `get_adapter()`.

* В STE Slicer это `QtSlicerAdapter` (qt_adapter.py), создаётся при первом обращении;
  все импорты UM/steslicer живут там.
* Стенд разработчика подставляет мок через `set_adapter()` (devtools/mock_slicer.py),
  и те же обработчики работают без слайсера.

Правила потоков (см. base.SlicerAdapter): методы с пометкой «главный поток» вызываются
только из главного потока Qt (инструменты с main_thread=True); методы с пометкой
«рабочий поток» ждут слайсер и из главного потока их вызывать НЕЛЬЗЯ.

Модели мы показываем координаты принтера (Z вверх), см. coords.py.
"""

import threading

from .base import SlicerAdapter, SlicerError, SlicerUnavailable  # noqa: F401

_adapter = None
_lock = threading.Lock()


def set_adapter(adapter):
    """Подставить адаптер (мок для стенда и тестов); None — снова искать слайсер автоматически."""
    global _adapter
    with _lock:
        _adapter = adapter


def get_adapter():
    """Активный адаптер; вне слайсера и без мока — SlicerUnavailable."""
    global _adapter
    adapter = _adapter
    if adapter is not None:
        return adapter
    with _lock:
        if _adapter is None:
            _adapter = _create_qt_adapter()
        return _adapter


def _create_qt_adapter():
    try:
        from UM.Application import Application
    except ImportError:
        raise SlicerUnavailable("Слайсер недоступен: плагин запущен вне STE Slicer, а имитация слайсера не подключена.")
    if Application.getInstance() is None:
        raise SlicerUnavailable("Приложение STE Slicer ещё не запущено.")
    from .qt_adapter import QtSlicerAdapter
    return QtSlicerAdapter()


def get_object_world_vertices(object_id, frame="scene"):
    """Вершины сетки объекта в мировых координатах, список (x, y, z).

    Для модуля геометрии. `frame="scene"` — координаты сцены Uranium (Y вверх, Z к
    зрителю, начало в центре стола); `frame="printer"` — координаты принтера (X вправо,
    Y назад, Z вверх). Только из главного потока Qt (из рабочих — через
    main_thread.run_in_main_thread).
    """
    return get_adapter().get_object_world_vertices(object_id, frame=frame)


def get_object_mesh(object_id, frame="scene"):
    """(вершины, грани) объекта в мировых координатах; грань — тройка индексов. Главный поток."""
    return get_adapter().get_object_mesh(object_id, frame=frame)


def get_object_local_mesh(object_id):
    """(вершины, грани) объекта в его собственной системе: оси сцены (Y вверх), масштаб узла
    применён, поворот и смещение узла — нет (для групп — дочерние узлы с их преобразованиями
    относительно группы). Главный поток."""
    return get_adapter().get_object_local_mesh(object_id)


def apply_object_rotation(object_id, matrix, frame="scene", drop_to_bed=True, center_xy=None, reset=False):
    """Поворачивает объект матрицей 3x3 (по строкам) в системе `frame`. Главный поток.

    Поворот — вокруг центра габаритов объекта; затем объект кладётся на стол (drop_to_bed)
    и, если задан `center_xy` (x, y в координатах принтера), центр габаритов переносится туда
    (например, (0, 0) для 5D). reset=True сначала возвращает ориентацию из файла модели, и
    тогда `matrix` абсолютная (модуль геометрии считает ориентации от только что загруженной
    модели). Возвращает описание объекта (как list_objects).
    """
    return get_adapter().apply_rotation_matrix(object_id, matrix, frame=frame, drop_to_bed=drop_to_bed,
                                               center_xy=center_xy, reset=reset)
