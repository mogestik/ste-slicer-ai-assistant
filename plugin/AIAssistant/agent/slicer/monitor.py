"""Отслеживание состояния нарезки и ожидание её конца (чистый Python, тестируется без Qt).

Настоящий адаптер кормит `SliceMonitor` сигналами всех бэкендов (backendStateChange,
processingProgress, backendError): после смены режима печати `app.getBackend()` уже
другой объект, поэтому события не текущих бэкендов записываем, но на статус они не
влияют. Обработчики работают в главном потоке Qt, снимки читаются из рабочих потоков.
"""

import threading
import time

from .base import FIVE_D_MODES, STATE_NAMES

NOT_STARTED, PROCESSING, DONE, ERROR, DISABLED = 1, 2, 3, 4, 5


def stage_text(mode, progress, state="processing"):
    """Понятное название этапа нарезки для полосы прогресса."""
    if state == "done":
        return "Слайсинг завершён"
    if state == "error":
        return "Ошибка слайсинга"
    if state != "processing":
        return "Слайсинг не запущен"
    progress = progress or 0.0
    if mode in FIVE_D_MODES:
        if mode == "cylindrical_full":
            if progress < 0.1:
                return "5D: подготовка модели (вырезание сердечника)"
            if progress < 0.2:
                return "5D: генерация цилиндрической подложки"
            if progress < 0.5:
                return "5D: нарезка сердечника (CuraEngine)"
        elif progress < 0.5:
            return "5D Spiral: подготовка модели"
        if progress < 0.6:
            return "5D: нарезка цилиндрических слоёв (glicer, нужна лицензия ATSS)"
        return "5D: генерация G-code (CliParser)"
    return "3D: нарезка слоёв (CuraEngine)"


class SliceMonitor(object):
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self.current_backend = None
        self._states = {}
        self._progress = {}
        self._error = None
        self._generation = 0
        self._started_at = None
        self._finished_at = None
        self._last_progress_change = None
        self._mode = None
        self._starting = False
        self._saw_processing = False

    # Приём событий (главный поток)
    def set_current(self, backend_id):
        with self._lock:
            self.current_backend = backend_id

    def set_mode(self, mode):
        with self._lock:
            self._mode = mode

    def mark_started(self, backend_id, mode=None):
        """Вызывается прямо перед forceSlice(): открывает новое «поколение» нарезки.

        forceSlice() синхронно испускает NotStarted (stopSlicing), а затем Processing.
        Сразу после него нужно вызвать `finish_start()` — он скажет, начал ли бэкенд работу.
        """
        with self._lock:
            self.current_backend = backend_id
            self._generation += 1
            self._starting = True
            self._saw_processing = False
            self._error = None
            self._started_at = self._clock()
            self._finished_at = None
            self._last_progress_change = self._started_at
            self._progress[backend_id] = 0.0
            if mode is not None:
                self._mode = mode
            return self._generation

    def finish_start(self):
        """Конец окна запуска; True, если бэкенд сообщил Processing."""
        with self._lock:
            self._starting = False
            # Считается только синхронный Processing из slice(). Если slice() вышел сразу («ещё занята
            # обработка слоёв»), состояние от прошлой нарезки (done/error) учитывать нельзя.
            started = self._saw_processing
            if not started:
                self._started_at = None
            return started

    def on_state(self, backend_id, state):
        state = int(state) if state is not None else None
        with self._lock:
            previous = self._states.get(backend_id)
            self._states[backend_id] = state
            if backend_id != self.current_backend:
                return
            if self._starting:
                if state == PROCESSING:
                    self._saw_processing = True
                return
            if state == PROCESSING and previous != PROCESSING:
                # Нарезку запустил интерфейс или авто-нарезка, а не инструмент.
                self._progress[backend_id] = 0.0
                self._generation += 1
                self._started_at = self._clock()
                self._finished_at = None
                self._last_progress_change = self._started_at
                self._error = None
            if state in (DONE, ERROR) and previous != state:
                self._finished_at = self._clock()
            if state == DONE:
                self._progress[backend_id] = 1.0

    def on_progress(self, backend_id, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            old = self._progress.get(backend_id)
            if (old is not None and value < old and self._states.get(backend_id) == PROCESSING
                    and backend_id == self.current_backend):
                # processingProgress — один общий Signal на все бэкенды (см. qt_adapter._BackendListener):
                # посреди 5D-нарезки CylindricalBackend перезапускает помощников CuraEngine/CLIParser
                # (_terminate() шлёт 0). Поэтому прогресс идущей нарезки делаем неубывающим.
                return
            self._progress[backend_id] = value
            if backend_id == self.current_backend and old != value:
                self._last_progress_change = self._clock()

    def on_error(self, backend_id, message, kind="backend"):
        with self._lock:
            if backend_id != self.current_backend:
                return
            self._error = {"message": message, "kind": kind}

    # Чтение состояния (любой поток)
    def snapshot(self):
        with self._lock:
            backend = self.current_backend
            state = self._states.get(backend)
            progress = self._progress.get(backend) or 0.0
            now = self._clock()
            name = STATE_NAMES.get(state, "unknown")
            result = {
                "state": name,
                "progress": round(max(0.0, min(1.0, progress)), 3),
                "backend": backend,
                "generation": self._generation,
                "mode": self._mode,
                "stage": stage_text(self._mode, progress, name),
            }
            if self._started_at is not None:
                end = self._finished_at if (state in (DONE, ERROR) and self._finished_at) else now
                result["elapsed_s"] = round(end - self._started_at, 1)
            if self._last_progress_change is not None and name == "processing":
                result["since_progress_change_s"] = round(now - self._last_progress_change, 1)
            if self._error:
                result["error"] = self._error["message"]
                result["error_kind"] = self._error["kind"]
            return result


class WaitOutcome(object):
    DONE = "done"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    STALLED = "stalled"
    DISABLED = "disabled"


def wait_for_slice(get_status, generation, timeout, cancel_event=None, on_update=None, poll=0.5,
                   stall_limit=300.0, hint_after=60.0, sleep=time.sleep, clock=time.time):
    """Ждёт окончания нарезки поколения `generation`.

    `get_status()` возвращает снимок (см. SliceMonitor.snapshot). `on_update(status, hint)`
    вызывается, когда меняется прогресс или состояние или появляется подсказка.
    Возвращает (исход, статус, подсказка). Зависание: в 5D, если прогресс стоит в окне
    glicer (0.45..0.65) дольше `stall_limit` секунд, причина почти всегда в лицензии ATSS;
    через `hint_after` секунд сначала выдаём мягкую подсказку.
    """
    deadline = clock() + timeout
    last_key = None
    hint = None
    hinted = False
    error_seen_at = None
    while True:
        status = get_status()
        state = status.get("state")
        if status.get("generation", generation) != generation and state == "processing":
            # Запущена более новая нарезка (например, авто-нарезкой) — следим за ней.
            generation = status.get("generation")
        key = (state, status.get("progress"))
        in_glicer = (status.get("mode") in FIVE_D_MODES and state == "processing"
                     and 0.45 <= (status.get("progress") or 0.0) < 0.65)
        stuck_for = status.get("since_progress_change_s") or 0.0
        if in_glicer and not hinted and stuck_for >= hint_after:
            hinted = True
            hint = ("glicer работает уже %d с без изменения прогресса. Если на экране открылось окно ATSS, "
                    "лицензия ATSS не активирована — 5D-слайсинг не завершится." % int(stuck_for))
            if on_update is not None:
                on_update(status, hint)
        elif key != last_key and on_update is not None:
            on_update(status, None)
        last_key = key

        if state == "done":
            return WaitOutcome.DONE, status, hint
        if state == "error":
            # Текст ошибки приходит сразу после смены состояния — даём ему немного времени.
            if status.get("error") or (error_seen_at is not None and clock() - error_seen_at > 1.0):
                return WaitOutcome.ERROR, status, hint
            if error_seen_at is None:
                error_seen_at = clock()
        if state == "not_started":
            return WaitOutcome.INTERRUPTED, status, hint
        if state == "disabled":
            return WaitOutcome.DISABLED, status, hint
        if cancel_event is not None and cancel_event.is_set():
            return WaitOutcome.CANCELLED, status, hint
        if in_glicer and stall_limit and stuck_for >= stall_limit:
            return WaitOutcome.STALLED, status, hint
        if clock() >= deadline:
            return WaitOutcome.TIMEOUT, status, hint
        sleep(poll)
