"""SlicerAdapter поверх настоящего API STE Slicer (UM / steslicer).

Импортируется только внутри слайсера (см. agent/slicer/__init__.py). Импорты UM обёрнуты,
поэтому модуль можно импортировать и без слайсера (например, линтером).

Методы главного потока работают со сценой, стеками контейнеров и бэкендами напрямую; их
вызывают инструменты с main_thread=True (реестр сам оборачивает вызов в
run_in_main_thread). Методы рабочего потока (load_model, arrange_all, export_gcode,
save_project) ждут результата и на каждое обращение к слайсеру переходят в главный поток
через run_in_main_thread.

Подводные камни, которые здесь обходим:
* printing_mode по умолчанию cylindrical_full и пишется в userChanges; «сбросить всё»
  его не трогает;
* объект бэкенда меняется вместе с режимом печати — подписываемся на сигналы всех
  бэкендов, события не текущего бэкенда SliceMonitor игнорирует;
* GCodeWriter и ThreeMFWorkspaceWriter помечены @call_on_qt_thread и зависают, если
  вызвать их из главного потока, — вызываем из рабочего, да ещё под таймаутом;
* писатель проекта 3MF берём из обработчика файлов рабочего пространства, а не через
  PluginRegistry.getPluginObject("3MFWriter") (там два объекта, попадается любой);
* смена материала или качества при пользовательских изменениях может открыть диалог
  «сохранить/сбросить» — передаём no_dialog=True / временно ставим "always_keep";
* readLocalFile асинхронный — ждём fileCompleted с таймаутом, .3mf открываем с
  skip_project_file_check=True (без диалога проекта).
"""

import math
import os
import threading
import time
import zipfile

from .. import log
from ..main_thread import is_main_thread, run_in_main_thread
from . import coords
from .base import (ATSS_NOTE, FIVE_D_MODES, MODE_REPORT_KEYS, PRINT_MODE_TITLES, PRINT_MODES, TARGET_MACHINE,
                   ObjectIds, SlicerAdapter, SlicerError, resolve_object)
from .monitor import SliceMonitor
from .values import json_safe, option_pairs, polymer_of

try:
    from UM.Application import Application
    from UM.Math.Matrix import Matrix
    from UM.Math.Quaternion import Quaternion
    from UM.Math.Vector import Vector
    from UM.Mesh.MeshWriter import MeshWriter
    from UM.Operations.GroupedOperation import GroupedOperation
    from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
    from UM.Operations.RotateOperation import RotateOperation
    from UM.Operations.TranslateOperation import TranslateOperation
    from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
    from UM.Scene.Selection import Selection
except ImportError:  # вне слайсера
    Application = None

AXIS_HINT = ("поставь ось вращения на ось стола по suggest_orientation: move_object(frame=scene, x, z). "
             "Если уже поставил так — это норма для несимметричной детали (ось не совпадает с центром "
             "габаритов).")

SOURCE_NAMES = {
    "user": "пользовательское изменение",
    "quality_changes": "пользовательский профиль",
    "quality": "профиль качества",
    "material": "материал",
    "variant": "сопло",
    "definition_changes": "настройки принтера",
    "definition": "определение принтера",
}
STACK_PROPERTIES = ("label", "description", "type", "unit", "enabled", "settable_per_extruder",
                    "minimum_value", "maximum_value", "minimum_value_warning", "maximum_value_warning")
ERROR_STATES = ("Exception", "MinimumError", "MaximumError")
WARNING_STATES = ("MinimumWarning", "MaximumWarning")
WRITE_TIMEOUT = 180.0
NON_MODEL_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".gcode", ".g", ".gz", ".cli", ".ufp")
GROUND_TOLERANCE = 0.01


class _BackendListener(object):
    """Получатель сигналов UM одного бэкенда через связанные методы (UM.Signal хранит их слабо).

    backendStateChange и processingProgress — атрибуты класса UM.Backend.Backend. Декоратор
    @signalemitter, который даёт каждому экземпляру свои сигналы, работает через
    Backend.__new__, а он у CuraEngineBackend(QObject, Backend) и остальных бэкендов не
    вызывается (в MRO раньше стоит sip.simplewrapper.__new__). Получается, что у всех бэкендов
    ОДИН общий Signal на каждое событие и отправителя не различить; такие события относим к
    бэкенду, который текущий в момент сигнала (`shared=True`). backendError объявлен в каждом
    классе бэкенда и отправителя определяет. Исключения наружу не выпускаем: UM.Signal
    вызывает слоты прямо внутри кода бэкенда.
    """

    def __init__(self, adapter, backend_id, shared=False):
        self._adapter = adapter
        self._backend_id = backend_id
        self._shared = shared

    def _target(self):
        if self._shared:
            return self._adapter.current_backend_id() or self._backend_id
        return self._backend_id

    def on_state(self, state, *args):
        try:
            self._adapter.monitor.on_state(self._target(), state)
        except Exception:
            log.exception("backendStateChange handler failed")

    def on_progress(self, amount, *args):
        try:
            self._adapter.monitor.on_progress(self._target(), amount)
        except Exception:
            log.exception("processingProgress handler failed")

    def on_error(self, job=None, *args):
        try:
            message, kind = self._adapter.describe_backend_error(job)
            if message:
                self._adapter.monitor.on_error(self._backend_id, message, kind)
        except Exception:
            log.exception("backendError handler failed")


class _LoadWaiter(object):
    def __init__(self, path):
        self.target = _norm_path(path)
        self.event = threading.Event()

    def on_completed(self, file_name):
        if _norm_path(file_name) == self.target:
            self.event.set()


def _norm_path(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


class QtSlicerAdapter(SlicerAdapter):
    name = "ste_slicer"

    def __init__(self):
        self._app = Application.getInstance()
        self._ids = ObjectIds()
        self.monitor = SliceMonitor()
        self._listeners = []
        self._connected = set()
        self._load_waiters = []
        self._signals_connected = False
        if threading.current_thread() is self._app.getMainThread():
            self._connect_signals()
        else:
            self._app.callLater(self._connect_signals)

    # Сигналы и служебное

    def _connect_signals(self):
        if self._signals_connected:
            return
        try:
            manager = self._app.getBackendManager()
            backends = manager.getBackends()
            for name in ("backendStateChange", "processingProgress", "backendError"):
                users = {}
                for backend_id, backend in backends.items():
                    signal = getattr(backend, name, None)
                    if signal is not None:
                        users.setdefault(id(signal), []).append((backend_id, signal))
                for key, entries in users.items():
                    if (name, key) in self._connected:
                        continue
                    backend_id, signal = entries[0]
                    listener = _BackendListener(self, backend_id, shared=len(entries) > 1)
                    slot = {"backendStateChange": listener.on_state, "processingProgress": listener.on_progress,
                            "backendError": listener.on_error}[name]
                    signal.connect(slot)
                    self._connected.add((name, key))
                    self._listeners.append(listener)
                    if len(entries) > 1:
                        log.info("%s is shared by backends %s", name, ", ".join(b for b, _s in entries))
            if "manager" not in self._connected:
                manager.currendBackendChanged.connect(self._on_backend_changed)  # опечатка в самом слайсере
                self._connected.add("manager")
            # BackendManager меняет бэкенд по globalContainerStackChanged, не испуская currendBackendChanged.
            stack_changed = getattr(self._app, "globalContainerStackChanged", None)
            if stack_changed is not None and "stack" not in self._connected:
                stack_changed.connect(self._on_backend_changed)
                self._connected.add("stack")
            self._signals_connected = True
            self._on_backend_changed()
            log.info("Slicer adapter subscribed to %d backend signals", len(self._listeners))
        except Exception:
            log.exception("Cannot subscribe to backend signals")

    def current_backend_id(self):
        try:
            return self._app.getBackend().getPluginId()
        except Exception:
            return None

    def _on_backend_changed(self, *args):
        try:
            backend = self._app.getBackend()
            self.monitor.set_current(backend.getPluginId())
            stack = self._app.getGlobalContainerStack()
            if stack is not None:
                self.monitor.set_mode(stack.getProperty("printing_mode", "value"))
        except Exception:
            log.exception("Cannot read the current backend")

    def _ensure_signals(self):
        if not self._signals_connected:
            self._connect_signals()

    def describe_backend_error(self, job):
        """Текст и вид ошибки для сигнала backendError(job): (сообщение, kind)."""
        if job is None:
            return "Бэкенд слайсера сообщил об ошибке.", "backend"
        try:
            if hasattr(job, "isCancelled") and job.isCancelled():
                return "Слайсинг прерван.", "cancelled"
        except Exception:
            log.exception("Cannot read the cancelled state of the slicing job")
        error = job.getError() if hasattr(job, "getError") else None
        result = job.getResult() if hasattr(job, "getResult") else None
        name = getattr(result, "name", str(result))
        job_type = type(job).__name__
        if name == "NothingToSlice":
            return ("Нечего слайсить: ни одна модель не помещается в рабочую область. Переместите, поверните или "
                    "уменьшите модель (в 5D — высота до 231 мм и модель по центру оси)."), "nothing_to_slice"
        if name == "MaterialIncompatible":
            return "Материал несовместим с выбранным принтером/конфигурацией. Выберите другой материал.", "material"
        if name == "SettingError":
            errors = self._safe(self.setting_errors, [])
            listing = ", ".join("%s (%s = %s)" % (e["key"], e.get("label"), e.get("value")) for e in errors[:10])
            return "В настройках есть ошибки%s." % (": " + listing if listing else ""), "settings"
        if name == "ObjectSettingError":
            return "Ошибки в настройках отдельных моделей (per-object settings).", "settings"
        if name == "BuildPlateError":
            return "Неверное положение башни очистки или точек прайма.", "settings"
        if name == "ObjectsWithDisabledExtruder":
            return "Модели назначены на выключенный экструдер.", "extruder"
        if job_type == "ProcessCliJob":
            return ("Ошибка этапа glicer/CliParser (5D): %s. Проверьте лицензию ATSS и что модель пересекает "
                    "цилиндр основания." % (error or name)), "glicer"
        if error is not None:
            return "Ошибка бэкенда слайсера (%s): %s" % (job_type, error), "backend"
        return "Бэкенд слайсера сообщил об ошибке (%s: %s)." % (job_type, name), "backend"

    @staticmethod
    def _require_worker(title):
        """Методы ожидания и записи из главного потока Qt зависают (или вешают интерфейс)."""
        if is_main_thread():
            raise SlicerError("%s нельзя выполнять в главном потоке Qt (инструмент должен быть зарегистрирован "
                              "с main_thread=False)." % title)

    @staticmethod
    def _safe(func, default):
        try:
            return func()
        except Exception:
            log.exception("Slicer adapter helper failed")
            return default

    def _global(self):
        stack = self._app.getGlobalContainerStack()
        if stack is None:
            raise SlicerError("В слайсере не выбран принтер. Добавьте Stereotech Hybrid 530 V5.2.4 (select_machine).")
        return stack

    def _extruder_stacks(self, global_stack=None):
        global_stack = global_stack or self._global()
        count = global_stack.getProperty("machine_extruder_count", "value") or len(global_stack.extruders)
        stacks = []
        for position, stack in global_stack.extruders.items():
            try:
                index = int(position)
            except ValueError:
                continue
            if index < int(count):
                stacks.append((index, stack))
        stacks.sort(key=lambda item: item[0])
        return stacks

    def _extruder(self, index, global_stack=None):
        global_stack = global_stack or self._global()
        stack = global_stack.extruders.get(str(int(index)))
        if stack is None:
            raise SlicerError("Экструдера %s нет: у принтера экструдеры 0..%d"
                              % (index, len(self._extruder_stacks(global_stack)) - 1))
        return stack

    def _machine_manager(self):
        return self._app.getMachineManager()

    def _has_setting(self, key, global_stack=None):
        global_stack = global_stack or self._global()
        return bool(global_stack.definition.findDefinitions(key=key))

    # Принтер и конфигурация

    def print_mode(self):
        return self._global().getProperty("printing_mode", "value")

    def config(self):
        stack = self._app.getGlobalContainerStack()
        if stack is None:
            return {"machine": None, "errors": ["В слайсере не выбран принтер."],
                    "hint": "Вызови select_machine, чтобы добавить Stereotech Hybrid 530 V5.2.4."}
        manager = self._machine_manager()
        mode = stack.getProperty("printing_mode", "value")
        from steslicer.Settings.ExtruderManager import ExtruderManager
        active_index = ExtruderManager.getInstance().activeExtruderIndex
        extruders = []
        for index, ext in self._extruder_stacks(stack):
            extruders.append({
                "index": index,
                "name": ext.getName(),
                "enabled": bool(ext.isEnabled),
                "material_id": ext.material.getMetaDataEntry("base_file"),
                "material": ext.material.getName(),
                "nozzle_mm": json_safe(ext.getProperty("machine_nozzle_size", "value")),
                "filament_diameter_mm": json_safe(ext.getProperty("material_diameter", "value")),
            })
        result = {
            "machine": stack.getName(),
            "definition_id": stack.definition.getId(),
            "print_mode": mode,
            "print_mode_title": PRINT_MODE_TITLES.get(mode, mode),
            "quality": {
                "quality_type": manager.activeQualityType,
                "name": manager.activeQualityOrQualityChangesName,
                "custom_profile": manager.activeQualityChangesGroup.name
                if manager.activeQualityChangesGroup is not None else None,
                "layer_height_mm": json_safe(stack.getProperty("layer_height", "value")),
            },
            "extruders": extruders,
            "active_extruder": active_index,
            "build_volume_mm": self._build_volume(stack),
            "user_changes": self._user_changes(stack),
            "objects_on_plate": len(self._printable_nodes()),
            "backend": self._safe(lambda: self._app.getBackend().getPluginId(), None),
            "slicing": self.monitor.snapshot().get("state"),
        }
        warnings = []
        if stack.definition.getId() != TARGET_MACHINE:
            warnings.append("Активен принтер %s (%s), а целевой — Stereotech Hybrid 530 V5.2.4 (%s). "
                            "Вызови select_machine." % (stack.getName(), stack.definition.getId(), TARGET_MACHINE))
        if active_index not in (0, None):
            warnings.append("Активен экструдер %s; конфиг 5D строится по активному экструдеру, держите 0."
                            % active_index)
        if not manager.activeQualityType:
            warnings.append("Профиль качества не поддерживается текущим материалом (Not supported).")
        if mode in FIVE_D_MODES:
            warnings.append(ATSS_NOTE)
        if self._app.getPreferences().getValue("general/auto_slice"):
            warnings.append("Включён автослайсинг: каждое изменение перезапускает слайсинг.")
        errors = self._safe(self.setting_errors, [])
        if errors:
            result["setting_errors"] = errors
        if warnings:
            result["warnings"] = warnings
        return result

    def _build_volume(self, stack):
        return {
            "width_x": json_safe(stack.getProperty("machine_width", "value")),
            "depth_y": json_safe(stack.getProperty("machine_depth", "value")),
            "height_z": json_safe(stack.getProperty("machine_height", "value")),
            "shape": stack.getProperty("machine_shape", "value"),
        }

    def _user_changes(self, stack, limit=60):
        result = {"count": 0}
        stacks = [("global", stack)] + [("extruder_%d" % i, ext) for i, ext in self._extruder_stacks(stack)]
        for name, item in stacks:
            container = item.userChanges
            keys = sorted(container.getAllKeys())
            result["count"] += len(keys)
            if keys:
                result[name] = dict((key, json_safe(item.getProperty(key, "value"))) for key in keys[:limit])
        return result

    def list_machines(self):
        registry = self._app.getContainerRegistry()
        active = self._app.getGlobalContainerStack()
        machines = []
        for stack in registry.findContainerStacks(type="machine"):
            machines.append({"id": stack.getId(), "name": stack.getName(), "definition_id": stack.definition.getId(),
                             "active": active is not None and stack.getId() == active.getId()})
        return machines

    def select_machine(self, definition_id):
        from steslicer.Settings.MachineManager import MachineManager
        manager = self._machine_manager()
        stack = self._app.getGlobalContainerStack()
        if stack is not None and stack.definition.getId() == definition_id:
            return {"changed": False, "machine": stack.getName(), "definition_id": definition_id}
        registry = self._app.getContainerRegistry()
        definitions = registry.findDefinitionContainersMetadata(id=definition_id)
        if not definitions:
            raise SlicerError("Определение принтера «%s» не найдено." % definition_id)
        existing = MachineManager.getMachine(definition_id)
        if existing is not None:
            manager.setActiveMachine(existing.getId())
            action = "activated"
        else:
            manager.addMachine(definitions[0].get("name", definition_id), definition_id)
            action = "added"
        stack = self._global()
        if stack.definition.getId() != definition_id:
            # setActiveMachine() молча ничего не делает при плохом стеке, addMachine() — если создать не вышло.
            raise SlicerError("Слайсер не переключился на принтер %s (активен %s); см. steslicer.log."
                              % (definition_id, stack.definition.getId()))
        from steslicer.Settings.ExtruderManager import ExtruderManager
        ExtruderManager.getInstance().setActiveExtruderIndex(0)
        return {"changed": True, "action": action, "machine": stack.getName(),
                "definition_id": stack.definition.getId(),
                "print_mode": stack.getProperty("printing_mode", "value"),
                "note": "У нового принтера режим по умолчанию cylindrical_full (5D) — задай режим явно (set_print_mode)."}

    # Настройки

    def _route(self, key, scope, extruder_index, global_stack):
        """(стек, подпись стека, заметка): где живёт значение `key` для этой области."""
        per_extruder = bool(global_stack.getProperty(key, "settable_per_extruder"))
        if not per_extruder:
            note = None
            if scope == "extruder":
                note = "настройка общая для принтера (не задаётся на экструдер) — используется глобальный стек"
            return global_stack, "global", note
        index = int(extruder_index or 0)
        note = None
        if scope != "extruder":
            limit = global_stack.getProperty(key, "limit_to_extruder")
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = -1
            if limit >= 0 and str(limit) in global_stack.extruders:
                index = limit
                note = "настройка относится к экструдеру %d (limit_to_extruder) — значение в его стеке" % index
        return self._extruder(index, global_stack), "extruder_%d" % index, note

    def setting_info(self, key, scope="global", extruder_index=0):
        global_stack = self._global()
        if not self._has_setting(key, global_stack):
            return None
        stack, stack_label, note = self._route(key, scope, extruder_index, global_stack)
        return self._describe(stack, key, stack_label, note)

    def _describe(self, stack, key, stack_label, note=None):
        info = {"key": key}
        for prop in STACK_PROPERTIES:
            try:
                info[prop] = json_safe(stack.getProperty(key, prop))
            except Exception:
                info[prop] = None
        info["value"] = json_safe(stack.getProperty(key, "value"))
        if info.get("type") == "enum":
            info["options"] = [list(pair) for pair in option_pairs(stack.getProperty(key, "options"))]
        if info.get("type") in ("extruder", "optional_extruder"):
            info["extruder_count"] = len(self._extruder_stacks())
        info["enabled"] = bool(info.get("enabled"))
        info["settable_per_extruder"] = bool(info.get("settable_per_extruder"))
        info["stack"] = stack_label
        info["source"] = self._value_source(stack, key)
        info["validation"] = self._validation_state(stack, key)
        if note:
            info["note"] = note
        return info

    def _value_source(self, stack, key):
        for container in stack.getContainers():
            try:
                if not container.hasProperty(key, "value"):
                    continue
            except Exception:
                continue
            container_type = container.getMetaDataEntry("type") or "definition"
            if container.__class__.__name__ == "DefinitionContainer":
                container_type = "definition"
            return SOURCE_NAMES.get(container_type, container_type)
        next_stack = stack.getNextStack() if hasattr(stack, "getNextStack") else None
        if next_stack is not None:
            return "глобальный стек: " + self._value_source(next_stack, key)
        return "значение по умолчанию"

    def _validation_state(self, stack, key):
        try:
            state = stack.getProperty(key, "validationState")
            if state is None:
                from UM.Settings.SettingDefinition import SettingDefinition
                definition = stack.getSettingDefinition(key)
                validator_type = SettingDefinition.getValidatorForType(definition.type) if definition else None
                if validator_type is None:
                    return "valid"
                state = validator_type(key)(stack)
            name = getattr(state, "value", str(state))
        except Exception:
            return "unknown"
        if name in ERROR_STATES:
            return "error"
        if name in WARNING_STATES:
            return "warning"
        return "valid"

    def setting_value_getter(self, scope="global", extruder_index=0):
        global_stack = self._global()

        def getter(key):
            try:
                if not self._has_setting(key, global_stack):
                    return None
                stack, _label, _note = self._route(key, scope, extruder_index, global_stack)
                return stack.getProperty(key, "value")
            except Exception:
                return None
        return getter

    def set_setting(self, key, value, scope="global", extruder_index=0):
        global_stack = self._global()
        if not self._has_setting(key, global_stack):
            raise SlicerError("Неизвестная настройка %s" % key)
        stack, stack_label, note = self._route(key, scope, extruder_index, global_stack)
        stack.setProperty(key, "value", value)   # SteSlicerContainerStack -> userChanges
        return self._describe(stack, key, stack_label, note)

    def reset_settings(self, keys=None, scope="global", extruder_index=0):
        global_stack = self._global()
        all_stacks = [global_stack] + [ext for _i, ext in self._extruder_stacks(global_stack)]
        if keys:
            reset, unknown = [], []
            for key in keys:
                if not self._has_setting(key, global_stack):
                    unknown.append(key)
                    continue
                stacks = [self._extruder(extruder_index, global_stack)] if scope == "extruder" else all_stacks
                for stack in stacks:
                    stack.userChanges.removeInstance(key)
                stack, label, _note = self._route(key, scope, extruder_index, global_stack)
                reset.append({"key": key, "value": json_safe(stack.getProperty(key, "value")), "stack": label})
            return {"reset": reset, "unknown": unknown}
        mode = global_stack.getProperty("printing_mode", "value")
        if scope == "extruder":
            stacks = [self._extruder(extruder_index, global_stack)]
        else:
            stacks = all_stacks
        count = 0
        for stack in stacks:
            count += len(stack.userChanges.getAllKeys())
            stack.userChanges.clear()
        result = {"reset_all": True, "cleared_values": count}
        if scope != "extruder":
            # Режим печати оставляем явным: по умолчанию он cylindrical_full (5D).
            global_stack.setProperty("printing_mode", "value", mode)
            result["kept"] = {"printing_mode": mode}
            manager = self._machine_manager()
            if hasattr(manager, "correctExtruderSettings"):
                manager.correctExtruderSettings()
        return result

    def search_settings(self, query, limit=15):
        global_stack = self._global()
        words = [w for w in query.lower().split() if w]
        found = []
        for definition in global_stack.definition.findDefinitions():
            if definition.type == "category":
                continue
            text = " ".join([definition.key, definition.label or "", definition.description or ""]).lower()
            if all(w in text for w in words):
                found.append(definition)
        found.sort(key=lambda d: (0 if all(w in (d.key + " " + (d.label or "")).lower() for w in words) else 1,
                                  len(d.key)))
        result = []
        for definition in found[:limit]:
            result.append({"key": definition.key, "label": definition.label, "type": definition.type,
                           "unit": definition.unit or "",
                           "value": json_safe(global_stack.getProperty(definition.key, "value"))})
        return result

    def setting_errors(self):
        global_stack = self._app.getGlobalContainerStack()
        if global_stack is None:
            return []
        checker = self._safe(self._app.getMachineErrorChecker, None)
        manager = self._machine_manager()
        if checker is not None and not checker.needToWaitForResult and not checker.hasError \
                and not manager.stacksHaveErrors:
            return []
        errors = []
        seen = set()
        stacks = [("global", global_stack)] + [("extruder_%d" % i, ext) for i, ext in
                                               self._extruder_stacks(global_stack) if ext.isEnabled]
        for label, stack in stacks:
            for key in stack.getErrorKeys():
                per_extruder = bool(global_stack.getProperty(key, "settable_per_extruder"))
                if label != "global" and not per_extruder:
                    continue  # глобальные настройки показываем один раз, из глобального стека
                if not stack.getProperty(key, "enabled"):
                    continue
                marker = (label, key)
                if marker in seen:
                    continue
                seen.add(marker)
                errors.append({
                    "key": key, "label": stack.getProperty(key, "label"), "stack": label,
                    "value": json_safe(stack.getProperty(key, "value")),
                    "minimum_value": json_safe(stack.getProperty(key, "minimum_value")),
                    "maximum_value": json_safe(stack.getProperty(key, "maximum_value")),
                })
        return errors

    # Режим печати

    def set_print_mode(self, mode):
        if mode not in PRINT_MODES:
            raise SlicerError("Неизвестный режим %s; допустимы: %s" % (mode, ", ".join(PRINT_MODES)))
        global_stack = self._global()
        before = self._mode_state(global_stack)
        global_stack.setProperty("printing_mode", "value", mode)
        from steslicer.Settings.ExtruderManager import ExtruderManager
        ExtruderManager.getInstance().setActiveExtruderIndex(0)   # конфиг glicer берётся по активному экструдеру
        return before

    def print_mode_report(self):
        global_stack = self._global()
        state = self._mode_state(global_stack)
        self._on_backend_changed()
        try:
            self._app.getBuildVolume().updateNodeBoundaryCheck()
        except Exception:
            log.exception("updateNodeBoundaryCheck failed")
        state["objects_outside"] = [obj["id"] + " (" + obj["name"] + ")" for obj in self.list_objects()
                                    if obj.get("outside_build_area")]
        return state

    def _mode_state(self, global_stack):
        settings = {}
        for key in MODE_REPORT_KEYS:
            if not self._has_setting(key, global_stack):
                continue
            settings[key] = {"value": json_safe(global_stack.getProperty(key, "value")),
                             "enabled": bool(global_stack.getProperty(key, "enabled"))}
        return {
            "print_mode": global_stack.getProperty("printing_mode", "value"),
            "backend": self._safe(lambda: self._app.getBackend().getPluginId(), None),
            "build_volume_mm": self._build_volume(global_stack),
            "settings": settings,
        }

    # Профили

    def _layer_height_of(self, group, global_stack):
        default = global_stack.definition.getProperty("layer_height", "value")
        node = getattr(group, "node_for_global", None)
        container = node.getContainer() if node is not None else None
        value = default
        if container is not None and container.hasProperty("layer_height", "value"):
            value = container.getProperty("layer_height", "value")
        if callable(value):
            try:
                value = value(global_stack)
            except Exception:
                value = default
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return None

    def list_profiles(self):
        global_stack = self._global()
        manager = self._machine_manager()
        quality_manager = self._app.getQualityManager()
        active_custom = manager.activeQualityChangesGroup
        active_type = manager.activeQualityType
        qualities = []
        for quality_type, group in quality_manager.getQualityGroups(global_stack).items():
            qualities.append({
                "quality_type": quality_type, "name": group.name,
                "layer_height_mm": self._layer_height_of(group, global_stack),
                "available": bool(group.is_available),
                "active": quality_type == active_type and active_custom is None,
            })
        qualities.sort(key=lambda q: (q["layer_height_mm"] is None, q["layer_height_mm"]))
        customs = []
        for name, group in quality_manager.getQualityChangesGroups(global_stack).items():
            customs.append({"name": name, "quality_type": group.quality_type, "available": bool(group.is_available),
                            "active": active_custom is not None and active_custom.name == name})
        customs.sort(key=lambda c: c["name"].lower())
        return {
            "quality_profiles": qualities,
            "custom_profiles": customs,
            "active": {"quality_type": active_type, "name": manager.activeQualityOrQualityChangesName,
                       "custom": active_custom.name if active_custom is not None else None,
                       "user_changes": manager.numUserSettings},
            "note": "Названия качеств обманчивы: normal = «Fine» 0.1 мм, fast = «Normal» 0.15 мм, high = «Extra "
                    "Fine» 0.06 мм. Ориентируйся на layer_height_mm.",
        }

    def select_quality(self, quality_type):
        global_stack = self._global()
        manager = self._machine_manager()
        groups = self._app.getQualityManager().getQualityGroups(global_stack)
        group = groups.get(quality_type)
        if group is None:
            wanted = quality_type.strip().lower()
            matches = [g for qt, g in groups.items() if g.name.lower() == wanted or qt.lower() == wanted]
            group = matches[0] if len(matches) == 1 else None
        if group is None:
            raise SlicerError("Профиль качества «%s» не найден. Доступны: %s" % (
                quality_type, ", ".join("%s (%s, %s мм)" % (qt, g.name, self._layer_height_of(g, global_stack))
                                        for qt, g in sorted(groups.items()))))
        if not group.is_available:
            raise SlicerError("Профиль %s недоступен для текущего материала/экструдеров." % group.quality_type)
        manager.setQualityGroup(group, no_dialog=True)
        return {"quality_type": manager.activeQualityType, "name": manager.activeQualityOrQualityChangesName,
                "layer_height_mm": json_safe(global_stack.getProperty("layer_height", "value")),
                "user_changes_kept": manager.numUserSettings}

    def select_custom_profile(self, name):
        global_stack = self._global()
        manager = self._machine_manager()
        groups = self._app.getQualityManager().getQualityChangesGroups(global_stack)
        group = groups.get(name)
        if group is None:
            matches = [g for n, g in groups.items() if n.lower() == name.strip().lower()]
            group = matches[0] if len(matches) == 1 else None
        if group is None:
            raise SlicerError("Пользовательский профиль «%s» не найден. Есть: %s"
                              % (name, ", ".join(sorted(groups)) or "нет"))
        if not group.is_available:
            raise SlicerError("Профиль «%s» основан на качестве %s, которое сейчас недоступно."
                              % (group.name, group.quality_type))
        manager.setQualityChangesGroup(group, no_dialog=True)
        return {"custom_profile": group.name, "quality_type": group.quality_type,
                "layer_height_mm": json_safe(global_stack.getProperty("layer_height", "value")),
                "user_changes_kept": manager.numUserSettings}

    def create_profile(self, name):
        global_stack = self._global()
        manager = self._machine_manager()
        quality_manager = self._app.getQualityManager()
        name = (name or "").strip()
        if not name:
            raise SlicerError("Укажите имя профиля.")
        if not manager.activeQualityOrQualityChangesName:
            raise SlicerError("Нет активного профиля качества — сначала выберите профиль (select_profile).")
        moved = manager.numUserSettings
        unique = self._app.getContainerRegistry().uniqueName(name)
        # То же, что «Создать профиль из текущих настроек»: пользовательские изменения всех стеков
        # сливаются в новые контейнеры quality_changes, а пользовательские контейнеры очищаются.
        quality_manager.createQualityChanges(name)
        quality_manager.initialize()   # перестраиваем поиск сразу, не дожидаясь таймера на 300 мс
        group = quality_manager.getQualityChangesGroups(global_stack).get(unique)
        activated = False
        if group is not None:
            manager.setQualityChangesGroup(group, no_dialog=True)
            activated = True
        return {"name": unique, "activated": activated, "settings_moved_to_profile": moved,
                "quality_type": manager.activeQualityType,
                "note": None if unique == name else "Имя занято, профиль создан как «%s»." % unique}

    def save_profile(self):
        global_stack = self._global()
        manager = self._machine_manager()
        if global_stack.qualityChanges.getId() == "empty_quality_changes":
            raise SlicerError("Активен встроенный профиль качества, его нельзя перезаписать. Создайте "
                              "пользовательский профиль (create_profile).")
        count = manager.numUserSettings
        from steslicer.Settings.ContainerManager import ContainerManager
        if not ContainerManager.getInstance().updateQualityChanges():
            raise SlicerError("Слайсер не смог обновить пользовательский профиль.")
        return {"profile": global_stack.qualityChanges.getName(), "saved_changes": count}

    # Материалы

    def _available_materials(self, extruder_index):
        global_stack = self._global()
        extruder = self._extruder(extruder_index, global_stack)
        nodes = self._app.getMaterialManager().getAvailableMaterialsForMachineExtruder(global_stack, extruder) or {}
        return extruder, nodes

    def list_materials(self, extruder_index=0):
        extruder, nodes = self._available_materials(extruder_index)
        active = extruder.material.getMetaDataEntry("base_file")
        result = []
        for root_id, node in nodes.items():
            meta = node.getMetadata() if hasattr(node, "getMetadata") else {}
            properties = meta.get("properties") or {}
            result.append({
                "id": root_id,
                "name": meta.get("name", root_id),
                "brand": meta.get("brand"),
                "type": polymer_of(meta.get("name"), meta.get("material"), meta.get("brand")),
                "series": meta.get("material"),
                "diameter_mm": json_safe(properties.get("diameter")),
                "density_g_cm3": json_safe(properties.get("density")),
                "compatible": meta.get("compatible", True) not in (False, "False", "false"),
                "active": root_id == active,
                "description": (meta.get("description") or "")[:200],
            })
        result.sort(key=lambda m: (m["brand"] != "Stereotech", m["name"].lower()))
        return result

    def set_material(self, material_id, extruder_index=0):
        extruder, nodes = self._available_materials(extruder_index)
        before = extruder.material.getMetaDataEntry("base_file")
        root_id = material_id if material_id in nodes else None
        if root_id is None:
            materials = self.list_materials(extruder_index)
            root_id = pick_material(materials, material_id)
        manager = self._machine_manager()
        preferences = self._app.getPreferences()
        old_choice = preferences.getValue("steslicer/choice_on_profile_override")
        preferences.setValue("steslicer/choice_on_profile_override", "always_keep")
        try:
            node = nodes.get(root_id)
            if node is not None and hasattr(manager, "setMaterial"):
                # Тот же путь, что у меню материалов (MaterialMenu.qml: setMaterial(position, container_node)).
                # setMaterialById заново ищет узел по *имени* варианта ("empty") и, если не находит,
                # молча ставит в стек empty_material.
                manager.setMaterial(str(int(extruder_index)), node)
            else:
                manager.setMaterialById(str(int(extruder_index)), root_id)
        finally:
            preferences.setValue("steslicer/choice_on_profile_override", old_choice)
        after = extruder.material.getMetaDataEntry("base_file")
        if after != root_id:
            raise SlicerError("Слайсер не применил материал %s (активен %s)." % (root_id, after))
        result = {
            "extruder": int(extruder_index), "material_id": after, "name": extruder.material.getName(),
            "previous": before, "quality_type": manager.activeQualityType,
            "material_print_temperature": json_safe(extruder.getProperty("material_print_temperature", "value")),
            "material_bed_temperature": json_safe(extruder.getProperty("material_bed_temperature", "value")),
            "cool_fan_speed": json_safe(extruder.getProperty("cool_fan_speed", "value")),
            "retraction_amount": json_safe(extruder.getProperty("retraction_amount", "value")),
        }
        if not manager.activeQualityType:
            result["warning"] = "Профиль качества стал «Not supported» — выберите профиль (select_profile)."
        if extruder.getTop().getAllKeys():
            result["note"] = ("У экструдера есть пользовательские изменения (%s) — они перекрывают значения "
                              "материала." % ", ".join(sorted(extruder.getTop().getAllKeys())[:10]))
        return result

    # Сцена

    def _printable_nodes(self):
        from steslicer.Scene.SteSlicerSceneNode import SteSlicerSceneNode
        root = self._app.getController().getScene().getRoot()
        plate = self._safe(lambda: self._app.getMultiBuildPlateModel().activeBuildPlate, 0)
        nodes = []
        for node in DepthFirstIterator(root):
            if not isinstance(node, SteSlicerSceneNode):
                continue
            if not node.callDecoration("isSliceable") and not node.callDecoration("isGroup"):
                continue
            parent = node.getParent()
            if parent is not None and parent.callDecoration("isGroup"):
                continue
            if not node.getMeshData() and not node.callDecoration("isGroup"):
                continue
            build_plate = node.callDecoration("getBuildPlateNumber")
            if build_plate is not None and build_plate != plate:
                continue
            nodes.append(node)
        return nodes

    def _brief_objects(self):
        result = []
        for node in self._printable_nodes():
            result.append({"id": self._ids.short_id(node), "name": node.getName() or "object",
                           "slicer_id": id(node), "node": node})
        return result

    def _node(self, ref):
        objects = self._brief_objects()
        return resolve_object(objects, ref)["node"]

    def _object_dict(self, node, detailed=False):
        bbox = node.getBoundingBox()
        info = {"id": self._ids.short_id(node), "name": node.getName() or "object"}
        if bbox is None:
            return info
        center = coords.scene_to_printer((bbox.center.x, bbox.center.y, bbox.center.z))
        info["center_mm"] = coords.round_list([center[0], center[1], center[2]])
        info["size_mm"] = coords.round_list([bbox.width, bbox.depth, bbox.height])
        info["bottom_z_mm"] = round(float(bbox.bottom), 2) + 0.0
        info["on_bed"] = abs(float(bbox.bottom)) < 0.05
        info["min_mm"] = coords.round_list(coords.scene_to_printer((bbox.left, bbox.bottom, bbox.front)))
        info["max_mm"] = coords.round_list(coords.scene_to_printer((bbox.right, bbox.top, bbox.back)))
        info["min_mm"], info["max_mm"] = ([min(a, b) for a, b in zip(info["min_mm"], info["max_mm"])],
                                          [max(a, b) for a, b in zip(info["min_mm"], info["max_mm"])])
        info["rotation"] = self._rotation_dict(node)
        scale = node.getScale()
        info["scale"] = coords.round_list([abs(scale.x), abs(scale.z), abs(scale.y)], 4)
        info["outside_build_area"] = bool(node.isOutsideBuildArea()) if hasattr(node, "isOutsideBuildArea") else None
        info["centered_on_5d_axis"] = abs(center[0]) < 0.5 and abs(center[1]) < 0.5
        if node.callDecoration("isGroup"):
            info["group_of"] = len(node.getChildren())
        stack = node.callDecoration("getStack")
        if stack is not None:
            for key in ("anti_overhang_mesh", "support_mesh", "infill_mesh", "cutting_mesh"):
                try:
                    if stack.getProperty(key, "value"):
                        info["mesh_type"] = key
                except Exception:
                    log.exception("Cannot read the mesh type %s", key)
        if detailed:
            info.update(self._mesh_metrics(node))
        return info

    def _rotation_dict(self, node):
        q = node.getOrientation()
        x, y, z, w = float(q.x), float(q.y), float(q.z), float(q.w)
        w = max(-1.0, min(1.0, w))
        angle = math.degrees(2 * math.acos(abs(w)))
        if angle < 0.01:
            return {"angle_deg": 0.0, "axis": None}
        s = math.sqrt(max(1e-12, 1 - w * w))
        axis = (x / s, y / s, z / s)
        if w < 0:
            axis = (-axis[0], -axis[1], -axis[2])
        return {"angle_deg": round(angle, 2), "axis": coords.round_list(coords.scene_to_printer(axis), 3)}

    def _node_mesh(self, node):
        """(вершины Nx3 в мировых координатах сцены, грани Mx3 или None) — массивы numpy."""
        return self._mesh_arrays(node, lambda n: n.getWorldTransformation())

    def _node_local_mesh(self, node):
        """Как _node_mesh, но в собственной системе узла: масштаб применён, поворота и смещения нет.

        Преобразование узла — T * R * Shear * S (Matrix.compose), поэтому масштаб — это
        Matrix.setByScaleVector(node.getScale()). Дочерние узлы группы сохраняют своё
        преобразование относительно группы (масштаб группы * локальное преобразование).
        """
        scale = Matrix()
        scale.setByScaleVector(node.getScale())
        if not node.callDecoration("isGroup"):
            return self._mesh_arrays(node, lambda n: scale)
        return self._mesh_arrays(node, lambda n: self._relative_transformation(n, node, scale))

    @staticmethod
    def _relative_transformation(child, group, base):
        chain = []
        while child is not None and child is not group:
            chain.append(child.getLocalTransformation())
            child = child.getParent()
        matrix = base
        for local in reversed(chain):
            matrix = matrix.multiply(local, copy=True)
        return matrix

    def _mesh_arrays(self, node, transformation_of):
        """(вершины, грани или None) узла (у группы — все её сетки), преобразованные через transformation_of(узел сетки)."""
        import numpy
        if node.callDecoration("isGroup"):
            all_vertices, all_faces, offset = [], [], 0
            for child in node.getChildren():
                if not child.getMeshData() and not child.callDecoration("isGroup"):
                    continue
                verts, faces = self._mesh_arrays(child, transformation_of)
                if verts is None:
                    continue
                if faces is None:
                    faces = numpy.arange(len(verts) // 3 * 3).reshape(-1, 3)
                all_vertices.append(verts)
                all_faces.append(faces + offset)
                offset += len(verts)
            if not all_vertices:
                return None, None
            return numpy.concatenate(all_vertices), numpy.concatenate(all_faces)
        mesh = node.getMeshData()
        if mesh is None:
            return None, None
        transformed = mesh.getTransformed(transformation_of(node))
        vertices = transformed.getVertices()
        faces = transformed.getIndices() if transformed.hasIndices() else None
        return vertices, faces

    def _mesh_metrics(self, node):
        verts, faces = self._node_mesh(node)
        if verts is None:
            return {}
        result = {
            "triangles": int(len(faces) if faces is not None else len(verts) // 3),
            "volume_cm3": round(coords.mesh_volume(verts, faces) / 1000.0, 3),
            "bed_contact_area_mm2": round(coords.bottom_contact_area(verts, faces, up_axis=1), 1),
        }
        try:
            import numpy
            radius = float(numpy.sqrt(verts[:, 0] ** 2 + verts[:, 2] ** 2).max())
            result["max_radius_from_5d_axis_mm"] = round(radius, 2)
        except Exception:
            log.exception("Cannot compute the radius from the 5D axis")
        global_stack = self._app.getGlobalContainerStack()
        if global_stack is not None:
            result["build_volume_mm"] = self._build_volume(global_stack)
        return result

    def list_objects(self):
        return [self._object_dict(node) for node in self._printable_nodes()]

    def object_info(self, ref, detailed=True):
        return self._object_dict(self._node(ref), detailed=detailed)

    def get_object_world_vertices(self, ref, frame="scene"):
        verts, _faces = self._node_mesh(self._node(ref))
        if verts is None:
            return []
        points = [tuple(v) for v in verts.tolist()]
        if frame == "printer":
            points = [coords.scene_to_printer(p) for p in points]
        return points

    def get_object_mesh(self, ref, frame="scene"):
        return self._mesh_lists(self._node_mesh(self._node(ref)), frame)

    def get_object_local_mesh(self, ref):
        return self._mesh_lists(self._node_local_mesh(self._node(ref)), "scene")

    @staticmethod
    def _mesh_lists(arrays, frame):
        verts, faces = arrays
        if verts is None:
            return [], []
        points = [tuple(v) for v in verts.tolist()]
        if frame == "printer":
            points = [coords.scene_to_printer(p) for p in points]
        if faces is None:
            face_list = [(i, i + 1, i + 2) for i in range(0, len(points) - 2, 3)]
        else:
            face_list = [tuple(f) for f in faces.tolist()]
        return points, face_list

    # Загрузка моделей
    def load_model(self, path, clear_scene=False, timeout=120.0, cancel_event=None, center=None):
        self._require_worker("Загрузку модели")
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            raise SlicerError("Файл не найден: %s" % path)
        extension = os.path.splitext(path)[1].lower()
        if extension in NON_MODEL_EXTENSIONS:
            # ImageReader.preRead открывает модальный диалог настроек; G-code и CLI очищают сцену и блокируют нарезку.
            raise SlicerError("Файл %s не является 3D-моделью (поддерживаются STL, OBJ, 3MF, X3D и т.п.)."
                              % os.path.basename(path))
        waiter = _LoadWaiter(path)

        def start():
            from PyQt5.QtCore import QUrl
            self._global()
            if self._app.getMeshFileHandler().getReaderForFile(path) is None:
                raise SlicerError("Формат файла не поддерживается слайсером: %s" % os.path.basename(path))
            if clear_scene:
                self._remove_nodes(self._printable_nodes())
            before = set(id(node) for node in self._printable_nodes())
            self._load_waiters.append(waiter)
            self._app.fileCompleted.connect(waiter.on_completed)
            self._app.readLocalFile(QUrl.fromLocalFile(path), True)   # True — без диалога «открыть как проект»
            return before

        def stop():
            try:
                self._app.fileCompleted.disconnect(waiter.on_completed)
            except (TypeError, RuntimeError):
                pass
            if waiter in self._load_waiters:
                self._load_waiters.remove(waiter)

        def still_loading():
            # SteSlicerApplication убирает файл из _currently_loading_files в начале _readMeshFinished;
            # если тот выходит раньше (модель слишком мала, нет принтера), fileCompleted так и не придёт.
            loading = getattr(self._app, "_currently_loading_files", None)
            if loading is None:
                return True
            return any(_norm_path(name) == waiter.target for name in loading)

        before = run_in_main_thread(start)
        deadline = time.time() + timeout
        finished_at = None
        try:
            while not waiter.event.wait(0.25):
                if cancel_event is not None and cancel_event.is_set():
                    raise SlicerError("Загрузка прервана пользователем.")
                if finished_at is None and not run_in_main_thread(still_loading):
                    finished_at = time.time()
                if finished_at is not None and time.time() - finished_at > 1.5:
                    break   # чтение закончилось без fileCompleted — _after_load расскажет, что загрузилось
                if time.time() > deadline:
                    raise SlicerError("Слайсер не закончил загрузку %s за %d с (слишком большая или повреждённая "
                                      "модель, или нет активного принтера)." % (os.path.basename(path), timeout))
        finally:
            run_in_main_thread(stop)
        time.sleep(0.2)   # даём отработать обработчикам смены сцены (выпуклая оболочка, проверка объёма)
        return run_in_main_thread(self._after_load, before, center)

    def _after_load(self, before, center):
        new_nodes = [node for node in self._printable_nodes() if id(node) not in before]
        if not new_nodes:
            raise SlicerError("Файл прочитан, но объектов в сцене не появилось (пустая/слишком маленькая модель "
                              "или неподдерживаемое содержимое).")
        mode = self.print_mode()
        if center is None:
            center = mode in FIVE_D_MODES and len(new_nodes) == 1 and len(self._printable_nodes()) == 1
        for node in new_nodes:
            self._place(node, center_xy=(0.0, 0.0) if center else None, drop=True)
        self._update_boundaries()
        objects = [self._object_dict(node, detailed=True) for node in new_nodes]
        notes = []
        if center:
            notes.append("Объект поставлен центром на ось (X=0, Y=0) и на стол.")
        if mode in FIVE_D_MODES and not center:
            notes.append("Режим 5D: модель должна стоять осью вращения на оси стола — после загрузки " + AXIS_HINT)
        return {"loaded": objects, "print_mode": mode, "notes": notes}

    # Перемещение, поворот, масштаб
    def _update_boundaries(self):
        try:
            self._app.getBuildVolume().updateNodeBoundaryCheck()
        except Exception:
            log.exception("updateNodeBoundaryCheck failed")

    def _place(self, node, center_xy=None, drop=True, keep_center_of=None):
        """Сдвигает `node`: центр габаритов в center_xy (координаты принтера) и/или низ на стол."""
        bbox = node.getBoundingBox()
        if bbox is None:
            return
        dx = dy = dz = 0.0
        if center_xy is not None:
            scene_x, _y, scene_z = coords.printer_to_scene((center_xy[0], center_xy[1], 0.0))
            dx = scene_x - bbox.center.x
            dz = scene_z - bbox.center.z
        elif keep_center_of is not None:
            dx = keep_center_of[0] - bbox.center.x
            dz = keep_center_of[1] - bbox.center.z
        if drop:
            dy = -bbox.bottom
        if abs(dx) > 1e-6 or abs(dy) > 1e-6 or abs(dz) > 1e-6:
            TranslateOperation(node, Vector(dx, dy, dz)).push()

    def move_object(self, ref, x=None, y=None, z=None, relative=False):
        node = self._node(ref)
        bbox = node.getBoundingBox()
        center = coords.scene_to_printer((bbox.center.x, bbox.center.y, bbox.center.z))
        warnings = []
        if relative:
            delta_printer = (x or 0.0, y or 0.0, z or 0.0)
        else:
            target = (center[0] if x is None else x, center[1] if y is None else y, 0.0 if z is None else z)
            delta_printer = (target[0] - center[0], target[1] - center[1], target[2] - bbox.bottom)
        delta = coords.printer_to_scene(delta_printer)
        TranslateOperation(node, Vector(delta[0], delta[1], delta[2])).push()
        new_bottom = node.getBoundingBox().bottom
        if new_bottom > 0.05 and self._app.getPreferences().getValue("physics/automatic_drop_down"):
            warnings.append("Объект поднят над столом, но включено автоматическое опускание — слайсер опустит "
                            "его на стол.")
        if new_bottom < -0.05:
            warnings.append("Часть объекта ниже стола (%.1f мм) — эта часть не будет напечатана." % new_bottom)
        self._update_boundaries()
        return self._result_with(node, warnings)

    def _result_with(self, node, warnings):
        result = self._object_dict(node)
        if result.get("outside_build_area"):
            warnings.append("Объект выходит за рабочую область — он не будет нарезан.")
        if self.print_mode() in FIVE_D_MODES and not result.get("centered_on_5d_axis"):
            warnings.append("Режим 5D: центр габаритов не в X=0, Y=0 — " + AXIS_HINT)
        if warnings:
            result["warnings"] = warnings
        return result

    def _apply_rotation(self, node, matrix_scene, drop=True, center_xy=None, reset=False):
        bbox = node.getBoundingBox()
        keep = (bbox.center.x, bbox.center.z)
        if reset:
            # Сначала отменяем текущую ориентацию: итог = M * R_текущая^T (повороты вокруг мировых осей).
            current = node.getOrientation()
            current_matrix = coords.quaternion_to_matrix((current.x, current.y, current.z, current.w))
            matrix_scene = coords.matmul(matrix_scene, coords.transpose(current_matrix))
        quat = coords.matrix_to_quaternion(matrix_scene)
        if abs(quat[3]) < 1.0 - 1e-12:
            rotation = Quaternion(quat[0], quat[1], quat[2], quat[3])
            # RotateOperation поворачивает вокруг начала координат узла (положение потом восстанавливает).
            RotateOperation(node, rotation, rotate_around_point=node.getPosition()).push()
        self._place(node, center_xy=center_xy, drop=drop, keep_center_of=None if center_xy is not None else keep)

    def lay_flat(self, ref):
        node = self._node(ref)
        verts, faces = self._node_mesh(node)
        if verts is None:
            raise SlicerError("У объекта нет сетки.")
        normal, area = coords.largest_flat_face(verts, faces)
        if normal is None:
            raise SlicerError("Не удалось найти плоскую грань.")
        self._apply_rotation(node, coords.rotation_between(normal, (0.0, -1.0, 0.0)), drop=True)
        self._update_boundaries()
        result = self._result_with(node, [])
        result["note"] = "Уложен на плоскую грань площадью %.0f мм²." % area
        return result

    def apply_rotation_matrix(self, ref, matrix, frame="scene", drop_to_bed=True, center_xy=None, reset=False):
        try:
            coords.check_rotation_matrix(matrix)
        except ValueError as exc:
            raise SlicerError("Некорректная матрица поворота: %s" % exc)
        if frame == "printer":
            matrix = coords.printer_matrix_to_scene(matrix)
        node = self._node(ref)
        self._apply_rotation(node, matrix, drop=drop_to_bed, center_xy=center_xy, reset=reset)
        self._update_boundaries()
        return self._result_with(node, [])

    def scale_object(self, ref, factors):
        """Абсолютный масштаб по собственным осям модели (оси файла x, y, z, где z вверх)."""
        from UM.Operations.SetTransformOperation import SetTransformOperation
        node = self._node(ref)
        bbox = node.getBoundingBox()
        keep = (bbox.center.x, bbox.center.z)
        sx, sy, sz = [abs(float(f)) for f in factors]
        # оси файла/принтера (x, y, z) -> локальные оси сцены (x, z, y); операция сохраняет поворот и отражение
        SetTransformOperation(node, scale=Vector(sx, sz, sy)).push()
        self._place(node, drop=True, keep_center_of=keep)
        self._update_boundaries()
        return self._result_with(node, [])

    def center_for_5d(self, ref):
        node = self._node(ref)
        self._place(node, center_xy=(0.0, 0.0), drop=True)
        self._update_boundaries()
        result = self._result_with(node, [])
        result["note"] = ("Центр габаритов по X/Y совмещён с осью вращения (0, 0), низ на столе. Ориентацию "
                          "(ось детали вертикально) задаёт отдельный шаг.")
        return result

    def arrange_all(self, timeout=20.0):
        self._require_worker("Авторасстановку")
        def snapshot():
            return [(id(n), tuple(round(float(c), 3) for c in (n.getPosition().x, n.getPosition().y,
                                                               n.getPosition().z)))
                    for n in self._printable_nodes()]

        before = run_in_main_thread(snapshot)
        if not before:
            raise SlicerError("На столе нет объектов.")
        run_in_main_thread(self._app.arrangeAll)   # starts ArrangeObjectsJob (asynchronous)
        deadline = time.time() + timeout
        last, stable_since = None, None
        while time.time() < deadline:
            time.sleep(0.3)
            current = run_in_main_thread(snapshot)
            if current != last:
                last, stable_since = current, time.time()
            elif current != before and time.time() - stable_since > 0.6:
                break
            elif current == before and time.time() - stable_since > 3.0:
                break   # ничего не сдвинулось (уже расставлено или объекты слишком большие)
        return run_in_main_thread(self._after_arrange)

    def duplicate_object(self, ref, copies, timeout=60.0):
        """Добавляет на стол ещё `copies` копий объекта (MultiplyObjectsJob, только 3D)."""
        self._require_worker("Копирование")
        if self.print_mode() in FIVE_D_MODES:
            raise SlicerError("Копирование доступно только в 3D: в 5D деталь стоит на оси вращения, копии не помещаются.")
        copies = int(copies)
        if not 1 <= copies <= 50:
            raise SlicerError("copies должен быть от 1 до 50.")

        def start():
            from steslicer.MultiplyObjectsJob import MultiplyObjectsJob
            node = self._node(ref)
            before = len(self._printable_nodes())
            MultiplyObjectsJob([node], copies, min_offset=8).start()
            return before

        before = run_in_main_thread(start)
        deadline = time.time() + timeout
        stable_since, last = time.time(), -1
        while time.time() < deadline:
            time.sleep(0.4)
            count = run_in_main_thread(lambda: len(self._printable_nodes()))
            if count != last:
                last, stable_since = count, time.time()
            elif count >= before + copies or time.time() - stable_since > 5.0:
                break
        return run_in_main_thread(lambda: self._after_duplicate(copies))

    def _after_duplicate(self, copies):
        self._update_boundaries()
        objects = self.list_objects()
        outside = [o["id"] for o in objects if o.get("outside_build_area") in (True, "True")]
        brief = [{"id": o.get("id"), "center_mm": o.get("center_mm"), "outside_build_area": o.get("outside_build_area")}
                 for o in objects]
        return {"objects": brief, "copies_requested": copies, "total": len(objects),
                "inside_build_area": len(objects) - len(outside), "outside_build_area_ids": outside,
                "note": ("Не поместившиеся копии лежат вне стола: удали их (delete_objects) и сообщи, сколько "
                         "столов нужно." if outside else "Все копии помещаются на стол.")}

    def _after_arrange(self):
        self._update_boundaries()
        objects = self.list_objects()
        result = {"objects": objects}
        if self.print_mode() in FIVE_D_MODES:
            result["warning"] = ("Режим 5D: авторасстановка ставит модели по 3D-сетке. Для 5D-печати модель "
                                 "должна стоять на оси — " + AXIS_HINT)
        return result

    def _remove_nodes(self, nodes):
        if not nodes:
            return
        operation = GroupedOperation()
        for node in nodes:
            operation.addOperation(RemoveSceneNodeOperation(node))
        operation.push()
        Selection.clear()
        self._ids.forget_dead()

    def delete_objects(self, refs=None, delete_all=False):
        if delete_all:
            nodes = self._printable_nodes()
        else:
            if not refs:
                raise SlicerError("Укажите object_ids или all=true.")
            nodes = []
            for ref in refs:
                node = self._node(ref)
                if node not in nodes:
                    nodes.append(node)
        names = ["%s (%s)" % (self._ids.short_id(n), n.getName()) for n in nodes]
        self._remove_nodes(nodes)
        return {"deleted": names, "remaining": len(self._printable_nodes())}

    # Нарезка

    def monitor_snapshot(self):
        return self.monitor.snapshot()

    def slicing_status(self):
        self._ensure_signals()
        self._on_backend_changed()
        status = self.monitor.snapshot()
        if status["state"] == "unknown":
            plate = self._safe(lambda: self._app.getMultiBuildPlateModel().activeBuildPlate, 0)
            gcode = getattr(self._app.getController().getScene(), "gcode_dict", {}).get(plate)
            status["state"] = "done" if gcode else "not_started"
            status["note"] = "Состояние восстановлено по наличию G-code (слайсинг был запущен вне ассистента)."
        return status

    def slicing_precheck(self):
        self._ensure_signals()
        errors, warnings = [], []
        global_stack = self._app.getGlobalContainerStack()
        if global_stack is None:
            return {"errors": ["В слайсере не выбран принтер."], "warnings": []}
        mode = global_stack.getProperty("printing_mode", "value")
        root = self._app.getController().getScene().getRoot()
        for node in DepthFirstIterator(root):
            if node.callDecoration("isBlockSlicing"):
                errors.append("В сцене загружен G-code — слайсинг заблокирован. Удалите его (delete_objects all).")
                break
        self._update_boundaries()
        objects = self.list_objects()
        printable = [o for o in objects if not o.get("mesh_type")]
        if not printable:
            errors.append("На столе нет моделей для печати. Загрузите модель (load_model).")
        else:
            outside = [o for o in printable if o.get("outside_build_area")]
            if outside and len(outside) == len(printable):
                errors.append("Все модели вне рабочей области (%s): переместите/поверните/уменьшите их. Рабочая "
                              "область сейчас %s." % (", ".join(o["id"] + " " + o["name"] for o in outside),
                                                     self._build_volume(global_stack)))
            elif outside:
                warnings.append("Модели вне рабочей области не будут нарезаны: %s"
                                % ", ".join(o["id"] + " " + o["name"] for o in outside))
            if mode in FIVE_D_MODES:
                off_axis = [o for o in printable if not o.get("centered_on_5d_axis")]
                if off_axis:
                    warnings.append("5D: центр габаритов не в X=0, Y=0: %s — " % ", ".join(o["id"] + " " + o["name"] for o in off_axis) + AXIS_HINT)
        # MachineManager.stacksHaveErrors — кэш, который обновляет асинхронный MachineErrorChecker (сразу после
        # правки он отстаёт); пока проверка идёт, setting_errors() сам проверяет стеки.
        setting_errors = self._safe(self.setting_errors, [])
        if setting_errors:
            errors.append("Ошибки в настройках (слайсер откажется резать): %s" % "; ".join(
                "%s «%s» = %s (допустимо %s..%s, %s)" % (e["key"], e["label"], e["value"], e["minimum_value"],
                                                          e["maximum_value"], e["stack"]) for e in setting_errors[:12]))
        for index, ext in self._extruder_stacks(global_stack):
            if ext.isEnabled and ext.material.getMetaDataEntry("compatible") in (False, "False", "false"):
                errors.append("Материал экструдера %d несовместим с принтером." % index)
        if mode in FIVE_D_MODES:
            warnings.append(ATSS_NOTE)
        return {"errors": errors, "warnings": warnings, "mode": mode, "objects": len(printable)}

    def start_slicing(self):
        self._ensure_signals()
        backend = self._app.getBackend()
        backend_id = backend.getPluginId()
        mode = self.print_mode()
        generation = self.monitor.mark_started(backend_id, mode)
        try:
            backend.forceSlice()
        finally:
            started = self.monitor.finish_start()
        return {"generation": generation, "started": started, "backend": backend_id, "mode": mode}

    def cancel_slicing(self):
        self._ensure_signals()
        backend = self._app.getBackend()
        backend.stopSlicing()
        return self.monitor.snapshot()

    def slicing_result(self):
        global_stack = self._global()
        info = self._app.getPrintInformation()
        status = self.slicing_status()
        seconds = int(info.currentPrintTime) if info.currentPrintTime is not None else 0
        amounts = list(getattr(info, "_material_amounts", None) or [])
        materials = []
        costs = list(info.materialCosts or [])
        names = list(info.materialNames or [])
        for index, ext in self._extruder_stacks(global_stack):
            if index >= len(amounts) or not amounts[index]:
                continue
            amount = float(amounts[index])    # mm^3
            density = float((ext.getMetaDataEntry("properties", {}) or {}).get("density", 0) or 0)
            diameter = float(ext.getProperty("material_diameter", "value") or 1.75)
            radius = diameter / 2.0
            name = ext.material.getName()
            cost = None
            if name in names and names.index(name) < len(costs):
                cost = round(float(costs[names.index(name)]), 2) or None
            materials.append({
                "extruder": index, "material_id": ext.material.getMetaDataEntry("base_file"), "name": name,
                "length_m": round(amount / (math.pi * radius * radius) / 1000.0, 2),
                "weight_g": round(amount * density / 1000.0, 1),
                "volume_cm3": round(amount / 1000.0, 2),
                "cost": cost,
            })
        if not materials:   # запасной вариант — списки из PrintInformation
            lengths = list(info.materialLengths or [])
            weights = list(info.materialWeights or [])
            for i, length in enumerate(lengths):
                if not length:
                    continue
                materials.append({"extruder": i, "name": names[i] if i < len(names) else None,
                                  "length_m": round(float(length), 2),
                                  "weight_g": round(float(weights[i]), 1) if i < len(weights) else None,
                                  "cost": round(float(costs[i]), 2) if i < len(costs) and costs[i] else None})
        if seconds <= 0 and not materials:
            raise SlicerError("Результата слайсинга нет: запустите start_slicing и дождитесь завершения.")
        features = {}
        try:
            for feature, duration in info.getFeaturePrintTimes().items():
                value = int(duration)
                if value > 0:
                    features[feature] = value
        except Exception:
            log.exception("Cannot read print times per feature")
        result = {"print_time_s": seconds, "print_time_text": format_duration(seconds), "material": materials,
                  "mode": global_stack.getProperty("printing_mode", "value"), "state": status.get("state"),
                  "job_name": info.jobName}
        if features:
            result["time_by_feature_s"] = features
        layers = self._count_layers()
        if layers:
            result["layers"] = layers
        if status.get("state") != "done":
            result["warning"] = "Слайсинг не в состоянии done — данные могут относиться к прошлому слайсингу."
        return result

    def _gcode_list(self):
        plate = self._safe(lambda: self._app.getMultiBuildPlateModel().activeBuildPlate, 0)
        return getattr(self._app.getController().getScene(), "gcode_dict", {}).get(plate)

    def _count_layers(self):
        gcode = self._gcode_list()
        if not gcode:
            return 0
        return sum(chunk.count(";LAYER:") for chunk in gcode if isinstance(chunk, str))

    def _gcode_ready(self):
        status = self.slicing_status()
        if status["state"] != "done":
            raise SlicerError("G-code ещё не готов (состояние слайсинга: %s). Запустите start_slicing и "
                              "дождитесь done; любое изменение сцены/настроек сбрасывает G-code." % status["state"])
        if not self._gcode_list():
            raise SlicerError("G-code пуст: сцена или настройки изменились после слайсинга. Перезапустите слайсинг.")
        return {"mode": self.print_mode(), "job_name": self._app.getPrintInformation().jobName}

    def export_gcode(self, path):
        self._require_worker("Запись G-code")
        meta = run_in_main_thread(self._gcode_ready)
        writer = run_in_main_thread(lambda: self._app.getMeshFileHandler().getWriter("GCodeWriter"))
        if writer is None:
            raise SlicerError("Плагин GCodeWriter не загружен.")
        temp = path + ".part"
        try:
            with open(temp, "wt", encoding="utf-8") as stream:   # same as LocalFileOutputDevice
                # Вызываем из рабочего потока: GCodeWriter._createSnapshot сам перейдёт в поток Qt.
                ok = _call_with_timeout(writer.write, (stream, None, MeshWriter.OutputMode.TextMode),
                                        WRITE_TIMEOUT, "запись G-code")
        except BaseException:
            _remove_quietly(temp)
            raise
        if not ok:
            _remove_quietly(temp)
            raise SlicerError("GCodeWriter: %s" % (writer.getInformation() or "не удалось записать G-code"))
        _replace(temp, path)
        result = inspect_gcode(path)
        result["mode"] = meta["mode"]
        return result

    def save_project(self, path):
        self._require_worker("Сохранение проекта")
        writer = run_in_main_thread(lambda: self._app.getWorkspaceFileHandler().getWriter("3MFWriter"))
        if writer is None:
            raise SlicerError("Плагин 3MFWriter (project) не загружен.")
        root = run_in_main_thread(lambda: self._app.getController().getScene().getRoot())
        objects = run_in_main_thread(self.list_objects)
        temp = path + ".part"
        try:
            with open(temp, "wb") as stream:
                # ThreeMFWorkspaceWriter.write помечен @call_on_qt_thread: вызывать его НУЖНО из рабочего потока,
                # из главного он зависнет. Исключение внутри call_on_qt_thread наружу не передаёт (ожидающий
                # поток просто висит) — поэтому таймаут.
                ok = _call_with_timeout(writer.write, (stream, [root], MeshWriter.OutputMode.BinaryMode),
                                        WRITE_TIMEOUT, "сохранение проекта 3MF")
        except BaseException:
            _remove_quietly(temp)
            raise
        if not ok:
            _remove_quietly(temp)
            raise SlicerError("3MFWriter: %s" % (writer.getInformation() or "не удалось сохранить проект"))
        _replace(temp, path)
        result = inspect_3mf(path)
        result["objects"] = len(objects)
        if not objects:
            result["warning"] = "В проекте нет моделей."
        return result


# Функции, которые работают и без Qt

def pick_material(materials, query):
    """Корневой id материала, лучше всего подходящего под `query` (id, имя, GUID или тип полимера)."""
    text = (query or "").strip()
    if not text:
        raise SlicerError("Не указан материал.")
    folded = text.lower()
    for key in ("id", "name"):
        matches = [m for m in materials if (m.get(key) or "").lower() == folded]
        if len(matches) == 1:
            return matches[0]["id"]
    matches = [m for m in materials if (m.get("type") or "").lower() == folded]
    if len(matches) == 1:
        return matches[0]["id"]
    if not matches:
        matches = [m for m in materials if folded in (m.get("name") or "").lower() or folded in m["id"].lower()]
        if len(matches) == 1:
            return matches[0]["id"]
    if matches:
        raise SlicerError("«%s» подходит нескольким материалам: %s. Укажите id."
                          % (text, ", ".join("%s (%s)" % (m["id"], m["name"]) for m in matches[:15])))
    raise SlicerError("Материал «%s» недоступен для этого экструдера (Ø1.75). Вызови list_materials." % text)


def format_duration(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return "%d ч %02d мин" % (hours, minutes)
    if minutes:
        return "%d мин" % minutes
    return "%d с" % seconds


def inspect_gcode(path):
    """Быстрая проверка записанного G-code на здравый смысл."""
    info = {"path": path, "size_bytes": os.path.getsize(path)}
    layers = 0
    header = {}
    moves = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith(";LAYER:"):
                layers += 1
            elif line.startswith(";PRINTING_MODE:"):
                header["printing_mode"] = line.split(":", 1)[1].strip().strip("[]")
            elif line.startswith(";FLAVOR:"):
                header["flavor"] = line.split(":", 1)[1].strip()
            elif line.startswith(";TIME:"):
                header["time_s"] = line.split(":", 1)[1].strip()
            elif line.startswith(("G0 A", "G1 A")) and "a_axis_moves" not in header:
                header["a_axis_moves"] = True
            elif line.startswith(("G0", "G1")):
                moves += 1
    info.update(header)
    info["layers"] = layers
    info["moves"] = moves
    info["looks_valid"] = moves > 0
    return info


def inspect_3mf(path):
    info = {"path": path, "size_bytes": os.path.getsize(path)}
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        raise SlicerError("Сохранённый файл не является корректным 3MF (zip).")
    info["has_model"] = "3D/3dmodel.model" in names
    info["has_settings"] = any(n.endswith(".global.cfg") for n in names)
    info["entries"] = len(names)
    info["looks_valid"] = info["has_model"] and info["has_settings"]
    return info


def _call_with_timeout(func, args, timeout, title):
    """Выполняет func(*args) во вспомогательном потоке; SlicerError, если не успела вовремя."""
    box = {}

    def runner():
        try:
            box["result"] = func(*args)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=runner, name="AIAssistant-writer")
    thread.daemon = True
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise SlicerError("%s не завершилась за %d с (главный поток слайсера занят или произошла ошибка; см. "
                          "steslicer.log)." % (title.capitalize(), timeout))
    if "error" in box:
        raise SlicerError("%s: %s: %s" % (title.capitalize(), type(box["error"]).__name__, box["error"]))
    return box.get("result")


def _replace(temp, path):
    try:
        os.replace(temp, path)
    except OSError as exc:   # например, файл открыт в другой программе (Windows)
        _remove_quietly(temp)
        raise SlicerError("Не удалось записать %s: %s (файл открыт в другой программе или нет прав?)" % (path, exc))


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass
