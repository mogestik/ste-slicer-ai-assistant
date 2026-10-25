# Расширение STE Slicer: меню «AI Assistant», панель чата в главном окне и локальный сервер API/MCP.

import os

from UM.Application import Application
from UM.Extension import Extension
from UM.Message import Message

from .agent import log
from .agent.config import PreferencesBackend
from .agent.service import AssistantService

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class AIAssistant(Extension):
    def __init__(self):
        super().__init__()
        self.setMenuName("AI Assistant")
        self.addMenuItem("Open chat", self.showChatWindow)
        self.addMenuItem("Settings", self.showSettings)
        self.addMenuItem("User guide (PDF)", self.showManual)
        self._panel = None
        self._bridge = None

        self._service = None
        self._app = Application.getInstance()
        # Настройки регистрируем сразу, иначе значения из preferences.cfg при загрузке потеряются.
        self._settings_backend = PreferencesBackend(self._app)
        self._app.applicationShuttingDown.connect(self._onApplicationShuttingDown)
        self._connectStartupSignal()

    # Пункты меню

    def showChatWindow(self):
        # В этой сборке слайсера нет QtWebEngine, поэтому чат — своя QML-панель.
        self._withPanel(lambda panel: panel.present())

    def showSettings(self):
        self._withPanel(lambda panel: panel.openSettings())

    def showManual(self):
        self._ensurePanel()
        if self._bridge is not None:
            error = self._bridge.openManual()
            if error:
                Message(error, title="AI Assistant").show()

    def _withPanel(self, action):
        try:
            self._ensurePanel()
            if self._panel is None:
                raise RuntimeError("QML panel was not created (see the log)")
            action(self._panel)
        except Exception as exc:
            log.exception("Cannot open the chat panel")
            Message("Панель AI Assistant не открылась: %s. Подробности — в журнале слайсера." % exc,
                    title="AI Assistant").show()

    def _ensurePanel(self):
        if self._panel is not None:
            return
        from .agent.chat_window import ChatBridge
        window = self._app.getMainWindow()
        if window is None:
            raise RuntimeError("main window is not ready")
        self._bridge = ChatBridge(self._ensureService)
        path = os.path.join(PLUGIN_DIR, "qml", "ChatWindow.qml")
        panel = self._app.createQmlComponent(path, {"manager": self._bridge})
        if panel is None:
            return
        panel.setParentItem(window.contentItem())
        self._panel = panel

    def _ensureService(self):
        if self._service is None:
            self._service = AssistantService(
                settings_backend=self._settings_backend,
                host_name="%s %s" % (self._app.getApplicationName(), self._app.getVersion()),
            )
        return self._service

    def _connectStartupSignal(self):
        """Адаптер слайсера создаём, когда приложение полностью запустилось.

        Адаптер подписывается на сигналы всех бэкендов нарезки; если создать его при старте,
        он увидит и нарезки, запущенные из интерфейса до открытия чата. Плагины грузятся раньше,
        чем готовы MachineManager и BackendManager (SteSlicerApplication.run), поэтому в
        конструкторе его создавать нельзя. initializationFinished испускается в конце run(),
        сигналы UM — запасной вариант для других сборок.
        """
        for name in ("initializationFinished", "applicationRunning", "engineCreatedSignal"):
            signal = getattr(self._app, name, None)
            if signal is None or not hasattr(signal, "connect"):
                continue
            try:
                signal.connect(self._onApplicationStarted)
                return
            except Exception:
                log.exception("Cannot connect to %s", name)
        log.warning("No application start signal: the slicer adapter is created on first use")

    def _onApplicationStarted(self, *args):
        try:
            from .agent.slicer import get_adapter
            get_adapter()
        except Exception:
            log.exception("Cannot initialise the slicer adapter at startup")
        try:
            self._ensurePanel()   # свёрнутый язычок «AI Assistant» у правого края
        except Exception:
            log.exception("Cannot create the chat panel at startup")

    def _onApplicationShuttingDown(self):
        if self._service is not None:
            self._service.stop()
            self._service = None
