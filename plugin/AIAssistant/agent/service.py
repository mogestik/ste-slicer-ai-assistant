"""Сборка сервиса: настройки, инструменты, диалоги, MCP и HTTP-сервер.

Используется и расширением слайсера (AIAssistant.py), и стендом разработчика,
поэтому в обоих случаях работает один и тот же код.
"""

import os
import tempfile

from . import log
from .attachments import UploadStore
from .chat_agent import SessionManager
from .config import SettingsStore
from .http_server import AssistantServer
from .mcp_server import McpServer
from .tools import build_registry

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_data_dir():
    return os.path.join(tempfile.gettempdir(), "steslicer_ai_assistant")


class AssistantService(object):
    def __init__(self, settings_backend=None, data_dir=None, registry=None, host_name="STE Slicer",
                 client_factory=None):
        self.data_dir = data_dir or default_data_dir()
        self.settings = SettingsStore(settings_backend)
        self.registry = registry or build_registry()
        self.uploads = UploadStore(os.path.join(self.data_dir, "uploads"))
        services = {"host_name": host_name, "data_dir": self.data_dir,
                    "results_dir": os.path.join(self.data_dir, "results")}
        self.sessions = SessionManager(self.settings, self.registry, self.uploads, services=services,
                                       client_factory=client_factory)
        self.mcp = McpServer(self.registry, services=self.sessions.services)
        self.server = AssistantServer(self.sessions, self.settings, self.mcp, self.uploads, host_name=host_name)

    @property
    def url(self):
        return self.server.url

    def start(self, port=None):
        """Запускает HTTP-сервер (повторный вызов ничего не делает) и возвращает базовый URL."""
        if not self.server.running:
            preferred = self.settings.get("port") if port is None else port
            self.server.start(preferred)
        return self.server.url

    def stop(self):
        for session in self.sessions.list_sessions():
            self.sessions.stop(session["id"])
        self.server.stop()
        log.info("AI Assistant service stopped")
