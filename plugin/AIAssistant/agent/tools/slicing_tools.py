"""Инструменты нарезки, итогов печати и сохранения файлов.

start_slicing работает в потоке вызывающего: нарезку запускаем в главном потоке Qt, а
дальше рабочий поток опрашивает SliceMonitor (его кормят сигналы бэкенда) и шлёт в
панель события `slicing_progress`; при успехе — `slicing_result` (карточка итога в чате).

Запись файлов специально вызываем из рабочего потока: GCodeWriter и запись проекта 3MF
сами переходят в поток Qt (@call_on_qt_thread) и зависают, если вызвать их из него.
"""

import os
import re
import threading
import time

from .. import log
from ..main_thread import run_in_main_thread
from ..slicer import get_adapter
from ..slicer.base import ATSS_NOTE, FIVE_D_MODES, tool_errors
from ..slicer.monitor import WaitOutcome, stage_text, wait_for_slice
from .registry import ToolError

GROUP = "slicing"
START_RETRIES = 3
STALL_LIMIT_S = 300.0      # 5D: glicer без прогресса — почти наверняка нет лицензии ATSS
HINT_AFTER_S = 60.0


def _object(properties, required=None):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def register(registry):
    registry.tool(
        "start_slicing",
        "Слайсинг сцены текущими настройками (с предпроверкой). wait=true — ждёт и возвращает время печати и "
        "расход материала. Изменение сцены/настроек после слайсинга сбрасывает G-code.",
        _object({
            "wait": {"type": "boolean", "default": True},
            "timeout_s": {"type": "integer", "minimum": 10, "maximum": 3600, "default": 900},
        }), main_thread=False, group=GROUP,
    )(_start_slicing)

    registry.tool(
        "get_slicing_status",
        "Статус слайсинга: состояние, прогресс 0..1, этап, ошибка (вне области, нет лицензии ATSS…).",
        _object({}), main_thread=True, group=GROUP,
    )(_get_slicing_status)

    registry.tool(
        "cancel_slicing",
        "Остановить текущий слайсинг.",
        _object({}), main_thread=True, group=GROUP,
    )(_cancel_slicing)

    registry.tool(
        "get_slicing_result",
        "Итог слайсинга: время печати, длина/масса/стоимость материала, слои, время по типам линий.",
        _object({}), main_thread=True, group=GROUP,
    )(_get_slicing_result)

    registry.tool(
        "export_gcode",
        "Сохранить G-code последнего слайсинга (3D и 5D); без path — в папку результатов. Возвращает путь, "
        "размер, слои.",
        _object({
            "path": {"type": "string", "description": ".gcode или папка"},
        }), main_thread=False, group=GROUP,
    )(_export_gcode)

    registry.tool(
        "save_project",
        "Сохранить проект 3MF (модели, принтер, режим, профиль, материал, настройки); без path — в папку "
        "результатов.",
        _object({
            "path": {"type": "string", "description": ".3mf или папка"},
        }), main_thread=False, group=GROUP,
    )(_save_project)


# Нарезка

def _emit_progress(ctx, status, hint=None):
    message = hint or status.get("stage") or stage_text(status.get("mode"), status.get("progress"),
                                                         status.get("state"))
    ctx.emit("slicing_progress", progress=status.get("progress") or 0.0, state=status.get("state"),
             message=message)


@tool_errors
def _start_slicing(args, ctx):
    adapter = get_adapter()
    timeout = float(args.get("timeout_s") or 900)
    precheck = run_in_main_thread(adapter.slicing_precheck)
    if precheck.get("errors"):
        raise ToolError("Слайсинг не запущен: " + " ".join(precheck["errors"]))
    ctx.emit("slicing_progress", progress=0.0, state="processing", message="Запуск слайсинга…")

    start = None
    for attempt in range(START_RETRIES):
        start = run_in_main_thread(adapter.start_slicing)
        if start.get("started"):
            break
        time.sleep(1.0 + attempt)
    if not start or not start.get("started"):
        ctx.emit("slicing_progress", progress=0.0, state="error", message="Бэкенд не начал слайсинг")
        raise ToolError("Бэкенд слайсера не начал слайсинг (занят обработкой предыдущего результата или "
                        "нечего нарезать). Повтори start_slicing через несколько секунд.")
    warnings = list(precheck.get("warnings") or [])

    if not args.get("wait", True):
        watcher = threading.Thread(target=_watch, name="AIAssistant-slice-watch",
                                   args=(adapter, start["generation"], timeout, ctx))
        watcher.daemon = True
        watcher.start()
        return {"started": True, "mode": start.get("mode"), "backend": start.get("backend"),
                "hint": "Слайсинг идёт в фоне — проверяй get_slicing_status, затем get_slicing_result.",
                "warnings": warnings}
    try:
        return _wait_and_finish(adapter, start["generation"], timeout, ctx, ctx.cancel_event, warnings)
    except ToolError as exc:
        # Сразу после правки сцены или настроек бэкенд иногда сбрасывается (перезапуск проверки
        # ошибок). Один автоматический перезапуск избавляет модель от повторного одинакового
        # вызова, который LoopGuard всё равно отклонил бы.
        if "бэкенд сбросил состояние" not in str(exc) or (ctx.cancel_event is not None and ctx.cancel_event.is_set()):
            raise
        time.sleep(3.0)
        restart = run_in_main_thread(adapter.start_slicing)
        if not restart.get("started"):
            raise
        warnings.append("Первый запуск слайсинга был прерван сбросом бэкенда; перезапущен автоматически.")
        return _wait_and_finish(adapter, restart["generation"], timeout, ctx, ctx.cancel_event, warnings)


def _watch(adapter, generation, timeout, ctx):
    try:
        _wait_and_finish(adapter, generation, timeout, ctx, None, [])
    except ToolError as exc:
        ctx.emit("slicing_progress", progress=0.0, state="error", message=str(exc))
    except Exception as exc:  # фоновый поток не должен умирать молча
        ctx.emit("slicing_progress", progress=0.0, state="error", message="Ошибка наблюдения: %s" % exc)


def _wait_and_finish(adapter, generation, timeout, ctx, cancel_event, warnings):
    outcome, status, hint = wait_for_slice(
        adapter.monitor_snapshot, generation, timeout, cancel_event=cancel_event,
        on_update=lambda st, h: _emit_progress(ctx, st, h), stall_limit=STALL_LIMIT_S, hint_after=HINT_AFTER_S)
    if outcome == WaitOutcome.DONE:
        result = run_in_main_thread(adapter.slicing_result)
        ctx.emit("slicing_progress", progress=1.0, state="done", message="Слайсинг завершён")
        ctx.emit("slicing_result", **_card(result))
        if warnings:
            result["warnings"] = warnings
        result["next"] = "Сохрани результат: export_gcode и save_project (до любых изменений сцены/настроек)."
        return result
    if outcome in (WaitOutcome.CANCELLED, WaitOutcome.TIMEOUT, WaitOutcome.STALLED):
        _safe_cancel(adapter)
    message = _failure_message(outcome, status, timeout)
    ctx.emit("slicing_progress", progress=status.get("progress") or 0.0, state="error", message=message)
    raise ToolError(message)


def _failure_message(outcome, status, timeout):
    five_d = status.get("mode") in FIVE_D_MODES
    in_glicer = five_d and 0.45 <= (status.get("progress") or 0.0) < 0.65
    if outcome == WaitOutcome.ERROR:
        return "Слайсинг завершился ошибкой: %s" % (status.get("error") or "бэкенд сообщил об ошибке "
                                                     "(подробности в окне слайсера)")
    if outcome == WaitOutcome.INTERRUPTED:
        return ("Слайсинг прерван: бэкенд сбросил состояние (изменились сцена или настройки, или слайсинг "
                "остановлен в интерфейсе). %sЗапусти start_slicing ещё раз."
                % ((status.get("error") + ". ") if status.get("error") else ""))
    if outcome == WaitOutcome.DISABLED:
        return "Слайсинг заблокирован: в сцене загружен G-code. Удали его (delete_objects all=true)."
    if outcome == WaitOutcome.CANCELLED:
        return "Слайсинг отменён пользователем."
    if outcome == WaitOutcome.STALLED or (outcome == WaitOutcome.TIMEOUT and in_glicer):
        return ("5D-слайсинг завис на этапе glicer (прогресс %d%% без изменений). Почти наверняка не "
                "активирована лицензия ATSS (проверь окно ATSS на экране). Слайсинг остановлен. %s"
                % (int((status.get("progress") or 0) * 100), ATSS_NOTE))
    return ("Слайсинг не завершился за %d с (прогресс %d%%, этап: %s). Слайсинг остановлен; уменьши модель/"
            "качество или увеличь timeout_s." % (timeout, int((status.get("progress") or 0) * 100),
                                                  status.get("stage")))


def _safe_cancel(adapter):
    try:
        run_in_main_thread(adapter.cancel_slicing)
    except Exception:
        pass


def _card(result):
    """Поля карточки итога нарезки в панели (событие slicing_result, см. events.py)."""
    card = {"print_time_s": result.get("print_time_s"), "print_time_text": result.get("print_time_text"),
            "material": [dict((k, m.get(k)) for k in ("extruder", "name", "weight_g", "length_m", "cost"))
                         for m in result.get("material") or []],
            "mode": result.get("mode")}
    if result.get("layers"):
        card["layers"] = result["layers"]
    return card


@tool_errors
def _get_slicing_status(args, ctx):
    status = get_adapter().slicing_status()
    if status.get("state") == "processing" and status.get("mode") in FIVE_D_MODES:
        status["note"] = "5D: при зависании на 50-60 % проверь лицензию ATSS."
    return status


@tool_errors
def _cancel_slicing(args, ctx):
    status = get_adapter().cancel_slicing()
    ctx.emit("slicing_progress", progress=0.0, state="not_started", message="Слайсинг остановлен")
    return {"cancelled": True, "status": status}


@tool_errors
def _get_slicing_result(args, ctx):
    return get_adapter().slicing_result()


# Сохранение файлов

@tool_errors
def _export_gcode(args, ctx):
    adapter = get_adapter()
    path = resolve_output_path(args.get("path"), ".gcode", ctx, adapter)
    result = adapter.export_gcode(path)
    result["note"] = "Скрипты пост-обработки (PostProcessing) при сохранении через ассистента не применяются."
    return result


@tool_errors
def _save_project(args, ctx):
    adapter = get_adapter()
    path = resolve_output_path(args.get("path"), ".3mf", ctx, adapter)
    return adapter.save_project(path)


def resolve_output_path(path, extension, ctx, adapter=None, now=None):
    """Абсолютный путь файла: указанный файл, папка (+ имя по умолчанию) или папка результатов плагина."""
    results_dir = ctx.services.get("results_dir") or os.path.join(os.path.expanduser("~"), "AIAssistant")
    path = (path or "").strip().strip('"')
    if path:
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(results_dir, path)
        if os.path.isdir(path) or path.endswith(("/", "\\")):
            path = os.path.join(path, _default_name(adapter, extension, now))
        elif not path.lower().endswith(extension):
            path += extension
    else:
        path = os.path.join(results_dir, _default_name(adapter, extension, now))
    directory = os.path.dirname(path)
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory)
    except OSError as exc:
        raise ToolError("Не удалось создать папку %s: %s" % (directory, exc))
    return os.path.abspath(path)


def _default_name(adapter, extension, now=None):
    base = "project"
    mode = None
    if adapter is not None:
        try:
            objects = run_in_main_thread(adapter.list_objects)
            if objects:
                base = os.path.splitext(objects[0]["name"])[0] or base
            mode = run_in_main_thread(adapter.print_mode)
        except Exception:
            log.exception("Cannot build a file name from the scene, using the default one")
    base = re.sub(r"[^\w.-]+", "_", base, flags=re.UNICODE).strip("_") or "project"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    parts = [base[:60]] + ([mode] if mode else []) + [stamp]
    return "_".join(parts) + extension
