"""Стенд разработчика: HTTP-сервер плагина, агент и MCP без STE Slicer.

    python devtools/run_standalone.py [--port 8765] [--secrets PATH]

* Используются тот же AssistantService и те же обработчики инструментов, что в плагине;
  заменён только адаптер слайсера (agent/slicer) — на MockSlicerAdapter из mock_slicer.py
  (настройки, профили, материалы H530, режимы 3D/5D, сцена с настоящими сетками,
  имитация нарезки, G-code и проект 3MF).
* Главный поток Qt тоже имитируется: в нём крутится очередь callLater, так что
  `run_in_main_thread` работает так же, как внутри слайсера.
* Доступ к LLM берём из файла .env.local в корне репозитория (LLM_BASE_URL, LLM_API_KEY,
  LLM_MODEL) и сохраняем в файл настроек во временной папке. В репозиторий ключ не попадает:
  .env.local в .gitignore.
"""

import argparse
import logging
import os
import queue
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
PLUGIN_DIR = os.path.join(ROOT_DIR, "plugin", "AIAssistant")
DEFAULT_SECRETS = os.path.join(ROOT_DIR, ".env.local")

sys.path.insert(0, PLUGIN_DIR)  # пакет `agent` импортируем прямо из папки плагина
sys.path.insert(0, HERE)

from agent import main_thread  # noqa: E402
from agent.config import JsonFileBackend  # noqa: E402
from agent.service import AssistantService, default_data_dir  # noqa: E402
from agent.tools import build_registry  # noqa: E402
import mock_slicer  # noqa: E402


class FakeQtApplication(object):
    """Минимальная замена UM.Application: callLater и getMainThread."""

    def __init__(self):
        self._main_thread = threading.current_thread()
        self._queue = queue.Queue()

    def getMainThread(self):
        return self._main_thread

    def callLater(self, func, *args, **kwargs):
        self._queue.put((func, args, kwargs))

    def run_event_loop(self, stop_event):
        while not stop_event.is_set():
            try:
                func, args, kwargs = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            func(*args, **kwargs)


def read_env_file(path):
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def build_service(data_dir, secrets_path):
    registry = build_registry()
    mock_slicer.install(registry)   # настоящие обработчики теперь работают с мок-адаптером
    backend = JsonFileBackend(os.path.join(data_dir, "standalone_settings.json"))
    service = AssistantService(settings_backend=backend, data_dir=data_dir, registry=registry,
                               host_name="standalone dev stand (mock slicer)")
    secrets = read_env_file(secrets_path)
    overrides = {}
    for env_key, setting in (("LLM_BASE_URL", "base_url"), ("LLM_API_KEY", "api_key"), ("LLM_MODEL", "model")):
        if secrets.get(env_key):
            overrides[setting] = secrets[env_key]
    if overrides:
        service.settings.update(overrides)
        print("LLM settings loaded from %s (key %s)" % (secrets_path, "set" if "api_key" in overrides else "missing"))
    return service


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=None, help="HTTP-порт (по умолчанию из настроек плагина, 8765)")

    parser.add_argument("--secrets", default=DEFAULT_SECRETS, help="файл с доступом к LLM (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL)")
    parser.add_argument("--data-dir", default=os.path.join(default_data_dir(), "standalone"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    app = FakeQtApplication()
    main_thread.set_app_provider(lambda: app)
    service = build_service(args.data_dir, args.secrets)
    url = service.start(port=args.port)
    print("AI Assistant stand: %s  (MCP: %smcp)" % (url, url), flush=True)

    stop_event = threading.Event()
    try:
        app.run_event_loop(stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()


if __name__ == "__main__":
    main()
