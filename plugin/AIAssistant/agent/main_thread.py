"""Мост из рабочих потоков (HTTP-сервер, цикл агента) в главный поток Qt.

Сцену, настройки и бэкенд слайсера можно трогать только из главного потока.
`run_in_main_thread` ставит функцию в очередь через `app.callLater` и ждёт её
завершения: возвращает результат или пробрасывает исключение в вызывающий поток.
"""

import threading
import traceback

DEFAULT_TIMEOUT = 60.0

_app_provider = None


class MainThreadTimeout(Exception):
    """Главный поток не выполнил вызов за отведённое время."""


def set_app_provider(provider):
    """Подменяет способ получить объект приложения (тесты, стенд).

    `provider` — функция, возвращающая объект с методами `callLater` и `getMainThread`;
    None — вызывать функции напрямую.
    """
    global _app_provider
    _app_provider = provider


def _get_app():
    if _app_provider is not None:
        return _app_provider()
    try:
        from UM.Application import Application
    except ImportError:
        return None
    return Application.getInstance()


def is_main_thread():
    app = _get_app()
    if app is None:
        return True
    return threading.current_thread() is app.getMainThread()


def run_in_main_thread(func, *args, timeout=DEFAULT_TIMEOUT, **kwargs):
    """Выполняет `func(*args, **kwargs)` в главном потоке Qt и возвращает результат.

    Если нас вызвали из главного потока или приложения нет, функция выполняется сразу.
    """
    app = _get_app()
    if app is None or threading.current_thread() is app.getMainThread():
        return func(*args, **kwargs)

    done = threading.Event()
    box = {}

    def runner():
        try:
            box["result"] = func(*args, **kwargs)
        except BaseException as exc:  # пробросим в вызывающем потоке
            box["error"] = exc
            box["traceback"] = traceback.format_exc()
        finally:
            done.set()

    app.callLater(runner)
    if not done.wait(timeout):
        raise MainThreadTimeout(
            "Главный поток слайсера не ответил за %.0f с (%s)" % (timeout, getattr(func, "__name__", func)))
    if "error" in box:
        error = box["error"]
        # Трейсбэк из главного потока пригодится в логах.
        error.main_thread_traceback = box.get("traceback")
        raise error
    return box.get("result")
