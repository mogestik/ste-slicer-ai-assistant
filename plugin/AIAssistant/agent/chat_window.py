"""Python-часть панели чата (QML).

В STE Slicer нет QtWebEngine, поэтому чат и настройки рисует qml/ChatWindow.qml, а с
плагином панель общается через локальный HTTP API. Этот QObject доступен в QML как
`manager`: запускает сервер, выбирает и загружает файлы (QML не умеет читать бинарные
файлы), открывает руководство и подстраивает 3D-вид под ширину панели.
"""

import json
import os
import threading
import urllib.parse
import urllib.request

from PyQt5.QtCore import QObject, QRectF, QSettings, QUrl, pyqtSignal, pyqtSlot

from . import log

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANUAL_FILE = "AI_Assistant_user_guide_ru.pdf"


class ChatBridge(QObject):
    # путь, id вложения, текст ошибки (пустой, если всё хорошо).
    # Имена аргументов обязательны: обработчик onFileUploaded в QML видит только именованные.
    fileUploaded = pyqtSignal(str, str, str, arguments=["path", "attachmentId", "error"])

    def __init__(self, service_provider, parent=None):
        QObject.__init__(self, parent)
        self._service_provider = service_provider
        self._base_url = ""

    @pyqtSlot(float)
    def setViewportShare(self, share):
        """Сужает 3D-вид, чтобы панель справа (доля окна `share`) его не перекрывала."""
        try:
            from UM.Application import Application
            window = Application.getInstance().getMainWindow()
            if window is not None:
                share = max(0.0, min(0.8, float(share)))
                window.setViewportRect(QRectF(0.0, 0.0, 1.0 - share, 1.0))
        except Exception:
            log.exception("Cannot change the viewport")

    @pyqtSlot(result=str)
    def getTheme(self):
        try:
            value = QSettings("Stereotech", "AIAssistant").value("theme", "light")
            return value if value in ("dark", "light") else "light"
        except Exception:
            return "light"

    @pyqtSlot(str)
    def setTheme(self, name):
        try:
            QSettings("Stereotech", "AIAssistant").setValue("theme", name if name in ("dark", "light") else "light")
        except Exception:
            log.exception("Cannot save the theme")

    @pyqtSlot(result=str)
    def ensureServer(self):
        try:
            self._base_url = self._service_provider().start().rstrip("/")
            return self._base_url
        except Exception:
            log.exception("Cannot start the AI Assistant server")
            return ""

    @pyqtSlot(result=float)
    def topOffset(self):
        """Нижняя граница верхней панели слайсера в координатах окна — от неё начинается наша панель."""
        try:
            from PyQt5.QtCore import QPointF
            from UM.Application import Application
            window = Application.getInstance().getMainWindow()
            root = window.contentItem()
            stack = list(root.childItems())
            while stack:
                item = stack.pop()
                if item.metaObject().className().startswith("Topbar"):
                    return float(item.mapToItem(root, QPointF(0, item.height())).y())
                stack.extend(item.childItems())
        except Exception:
            log.exception("Cannot find the top bar")
        return 0.0

    @pyqtSlot(result=str)
    def openManual(self):
        """Открывает PDF-руководство системной программой; возвращает текст ошибки или ""."""
        path = os.path.join(PLUGIN_DIR, "docs", MANUAL_FILE)
        if not os.path.isfile(path):
            return "Руководство не найдено: %s" % path
        try:
            from PyQt5.QtGui import QDesktopServices
            if QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                return ""
        except Exception:
            log.exception("Cannot open the user guide")
        try:
            os.startfile(path)   # запасной вариант для Windows
            return ""
        except Exception as exc:
            return "Не удалось открыть руководство: %s" % exc

    @pyqtSlot(result="QVariantList")
    def pickFiles(self):
        from PyQt5.QtWidgets import QFileDialog
        paths, _filter = QFileDialog.getOpenFileNames(
            None, "Выберите модель, фото или ТЗ", "",
            "Модели, фото, документы (*.stl *.obj *.3mf *.jpg *.jpeg *.png *.webp *.bmp *.docx *.txt *.md);;"
            "Все файлы (*)")
        return list(paths)

    @pyqtSlot(str, str)
    def uploadFile(self, session_id, path):
        threading.Thread(target=self._upload, args=(session_id, path), name="AIAssistant-upload",
                         daemon=True).start()

    def _upload(self, session_id, path):
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            query = urllib.parse.urlencode({"session": session_id, "name": os.path.basename(path)})
            request = urllib.request.Request(
                "%s/api/upload?%s" % (self._base_url, query), data=data, method="POST",
                headers={"X-AI-Assistant": "1", "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(request, timeout=120) as response:
                attachment = json.loads(response.read().decode("utf-8"))["attachment"]
            self.fileUploaded.emit(path, attachment["id"], "")
        except Exception as exc:
            log.exception("Upload of %s failed", path)
            self.fileUploaded.emit(path, "", str(exc))
