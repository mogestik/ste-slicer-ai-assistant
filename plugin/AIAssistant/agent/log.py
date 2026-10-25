"""Логирование: внутри слайсера пишем через UM.Logger, на стенде и в тестах — через logging."""

import logging
import traceback

try:
    from UM.Logger import Logger as _UMLogger
except ImportError:  # запуск вне слайсера
    _UMLogger = None

_PREFIX = "[AIAssistant] "
_std_logger = logging.getLogger("ai_assistant")
_LEVELS = {
    "d": logging.DEBUG,
    "i": logging.INFO,
    "w": logging.WARNING,
    "e": logging.ERROR,
    "c": logging.CRITICAL,
}


def log(level, message, *args):
    """Пишет `message % args` с однобуквенным уровнем UM: "d", "i", "w", "e", "c"."""
    if args:
        try:
            message = message % args
        except (TypeError, ValueError):
            message = "%s %r" % (message, args)
    if _UMLogger is not None:
        # Отдаём уже отформатированную строку, чтобы UM.Logger не форматировал её второй раз.
        _UMLogger.log(level, _PREFIX + message)
    else:
        _std_logger.log(_LEVELS.get(level, logging.INFO), message)


def debug(message, *args):
    log("d", message, *args)


def info(message, *args):
    log("i", message, *args)


def warning(message, *args):
    log("w", message, *args)


def error(message, *args):
    log("e", message, *args)


def exception(message, *args):
    """Ошибка вместе с трейсбэком исключения, которое сейчас обрабатывается."""
    log("e", message, *args)
    log("e", traceback.format_exc().rstrip())
