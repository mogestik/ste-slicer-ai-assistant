"""Вложения пользователя: хранение загруженных файлов и превращение их во вход для LLM.

* Фото (JPG/PNG/...) уменьшаем до `image_max_side` (через QImage, если есть PyQt5, иначе
  через Pillow) и один раз отправляем как data URL в `image_url`. Если уменьшать нечем,
  небольшие JPG/PNG/WEBP уходят как есть (см. encode_image).
* 3D-модели (STL/OBJ/3MF) остаются на диске: модели пишем текстовую пометку с путём,
  чтобы она сама вызвала `load_model`.
* Документы (TXT/MD/DOCX, например ТЗ) вставляем в сообщение текстом.
"""

import base64
import io
import os
import re
import struct
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from xml.etree import ElementTree

from . import log

MODEL_EXTENSIONS = (".stl", ".obj", ".3mf")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
DOCUMENT_EXTENSIONS = (".txt", ".md", ".docx")
ALLOWED_EXTENSIONS = MODEL_EXTENSIONS + IMAGE_EXTENSIONS + DOCUMENT_EXTENSIONS

MAX_UPLOAD_BYTES = 300 * 1024 * 1024
MAX_RAW_IMAGE_BYTES = 1536 * 1024          # если уменьшать нечем, фото больше этого не принимаем
JPEG_QUALITY = 85
MAX_DOCUMENT_CHARS = 15000
_CHUNK = 256 * 1024

# Форматы, которые без уменьшения можно отправить как есть (их принимают vision-модели с OpenAI API).
_MIME_BY_FORMAT = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


class AttachmentError(Exception):
    pass


def classify(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in MODEL_EXTENSIONS:
        return "model"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    return None


def safe_filename(name):
    """Оставляет имя файла (кириллица допустима), убирает путь и запрещённые символы."""
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:120] or "file"


class UploadStore(object):
    """Загруженные файлы на диске, по папке на диалог."""

    def __init__(self, root_dir):
        self.root_dir = root_dir
        self._records = OrderedDict()
        self._lock = threading.Lock()

    def save_stream(self, session_id, filename, stream, length):
        filename = safe_filename(filename)
        kind = classify(filename)
        if kind is None:
            raise AttachmentError("Неподдерживаемый тип файла: %s. Можно: %s"
                                  % (filename, ", ".join(ALLOWED_EXTENSIONS)))
        if length <= 0:
            raise AttachmentError("Пустой файл: %s" % filename)
        if length > MAX_UPLOAD_BYTES:
            raise AttachmentError("Файл слишком большой (%d МБ, максимум %d МБ)"
                                  % (length // 2 ** 20, MAX_UPLOAD_BYTES // 2 ** 20))
        upload_id = uuid.uuid4().hex[:12]
        directory = os.path.join(self.root_dir, _safe_id(session_id))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        path = os.path.join(directory, "%s_%s" % (upload_id, filename))
        remaining = length
        with open(path, "wb") as handle:
            while remaining > 0:
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                handle.write(chunk)
                remaining -= len(chunk)
        if remaining:
            os.remove(path)
            raise AttachmentError("Загрузка файла прервана: %s" % filename)
        record = {"id": upload_id, "session": session_id, "name": filename, "kind": kind,
                  "path": path, "size": length, "uploaded": time.time()}
        with self._lock:
            self._records[upload_id] = record
        log.info("Saved upload %s (%s, %d bytes) to %s", filename, kind, length, path)
        return record

    def get(self, upload_id):
        with self._lock:
            return self._records.get(upload_id)

    def list_for_session(self, session_id):
        with self._lock:
            return [dict(r) for r in self._records.values() if r["session"] == session_id]


def public_record(record):
    """Описание вложения для панели (абсолютные пути ей не нужны)."""
    result = {"id": record["id"], "name": record["name"], "kind": record["kind"], "size": record["size"]}
    if record["kind"] == "image":
        result["url"] = "/api/attachment?id=" + record["id"]
    return result


def build_user_content(text, records, image_max_side=1280):
    """Поле `content` в формате OpenAI для сообщения пользователя с вложениями."""
    notes = []
    images = []
    for record in records:
        if record["kind"] == "model":
            notes.append("[Вложение: 3D-модель «%s» (%s), сохранена локально: %s — чтобы работать с ней, "
                         "загрузи её в сцену инструментом load_model с этим путём]"
                         % (record["name"], _human_size(record["size"]), record["path"]))
        elif record["kind"] == "image":
            try:
                mime, data, warning = encode_image(record["path"], image_max_side or 1280)
            except (AttachmentError, OSError) as exc:
                notes.append("[Изображение «%s» не передано: %s]" % (record["name"], exc))
                continue
            images.append({"type": "image_url", "image_url": {"url": image_data_url(mime, data)}})
            notes.append("[Вложение: изображение «%s»%s]" % (record["name"], ("; " + warning) if warning else ""))
        else:
            try:
                document_text = extract_document_text(record["path"])
            except Exception as exc:
                log.exception("Cannot read document %s", record["path"])
                notes.append("[Документ «%s» не удалось прочитать: %s]" % (record["name"], exc))
                continue
            notes.append("[Вложение: документ «%s»]\n<<<\n%s\n>>>" % (record["name"], document_text))

    full_text = (text or "").strip()
    if notes:
        full_text = (full_text + "\n\n" if full_text else "") + "\n".join(notes)
    if not images:
        return full_text
    return [{"type": "text", "text": full_text or "(см. изображение)"}] + images


# Изображения

def encode_image(path, max_side):
    """Готовит фото для LLM, возвращает (mime, байты, предупреждение).

    Фото уменьшается до `max_side` пикселей и перекодируется в JPEG через QImage (в слайсере
    PyQt5 есть всегда) или через Pillow, если он установлен. Если нет ни того ни другого
    (голый стенд), JPEG/PNG/WEBP до MAX_RAW_IMAGE_BYTES уходит как есть с предупреждением,
    а файлы больше и неподходящие форматы отклоняются через AttachmentError.
    """
    fmt = sniff_image_format(path)
    if fmt is None:
        raise AttachmentError("файл не похож на изображение JPG/PNG")
    for encoder in (_encode_with_qt, _encode_with_pillow):
        try:
            result = encoder(path, max_side)
        except ImportError:
            continue
        except Exception as exc:  # битый файл или проблема с кодеком — пробуем следующий способ
            log.warning("Image encoder %s failed for %s: %s", encoder.__name__, path, exc)
            continue
        if result is not None:
            return result[0], result[1], None
    # Уменьшать нечем: отправляем оригинал, если он достаточно маленький.
    size = os.path.getsize(path)
    mime = _MIME_BY_FORMAT.get(fmt)
    if mime is None:
        raise AttachmentError("формат %s не поддерживается без конвертации — сохраните снимок как JPG или PNG"
                              % fmt.upper())
    if size > MAX_RAW_IMAGE_BYTES:
        raise AttachmentError("изображение слишком большое (%s, без уменьшения можно до %s) и его нечем уменьшить "
                              "— сохраните снимок поменьше" % (_human_size(size), _human_size(MAX_RAW_IMAGE_BYTES)))
    warning = None
    dims = image_dimensions(path)
    if dims and max(dims) > max_side:
        warning = "отправлено без уменьшения (%d×%d px, %s)" % (dims[0], dims[1], _human_size(size))
    with open(path, "rb") as handle:
        return mime, handle.read(), warning


def image_data_url(mime, data):
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def sniff_image_format(path):
    """Формат по сигнатуре файла: 'jpeg', 'png', 'webp', 'bmp', 'gif' или None."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM":
        return "bmp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def image_dimensions(path):
    """(ширина, высота) PNG или JPEG по заголовку файла (только stdlib) или None."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(min(os.path.getsize(path), 512 * 1024))
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            pos += 1 if marker == 0xFF else 2
            continue
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return width, height
        pos += 2 + length
    return None


def _encode_with_qt(path, max_side):
    from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PyQt5.QtGui import QColor, QImage, QPainter

    image = QImage(path)
    if image.isNull():
        return None
    if max(image.width(), image.height()) > max_side:
        image = image.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if image.hasAlphaChannel():
        # В JPEG нет альфы: рисуем поверх белого, иначе прозрачное станет чёрным.
        flat = QImage(image.size(), QImage.Format_RGB32)
        flat.fill(QColor(255, 255, 255))
        painter = QPainter(flat)
        painter.drawImage(0, 0, image)
        painter.end()
        image = flat
    for fmt, mime, quality in (("JPEG", "image/jpeg", JPEG_QUALITY), ("PNG", "image/png", -1)):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        ok = image.save(buffer, fmt, quality)   # PNG — на случай, если нет плагина qjpeg
        buffer.close()
        if ok and data.size():
            return mime, bytes(data)
    return None


def _encode_with_pillow(path, max_side):
    from PIL import Image

    with Image.open(path) as image:
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            image = background
        else:
            image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        output = io.BytesIO()
        image.save(output, "JPEG", quality=JPEG_QUALITY)
        return "image/jpeg", output.getvalue()


# Документы

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_document_text(path, max_chars=MAX_DOCUMENT_CHARS):
    if path.lower().endswith(".docx"):
        text = _docx_text(path)
    else:
        with open(path, "rb") as handle:
            text = handle.read().decode("utf-8", "replace")
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… [документ обрезан]"
    return text


def _docx_text(path):
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    body = root.find(_W_NS + "body")
    lines = []
    for block in (body if body is not None else []):
        if block.tag == _W_NS + "p":
            lines.append(_paragraph_text(block))
        elif block.tag == _W_NS + "tbl":
            for row in block.iter(_W_NS + "tr"):
                cells = [" ".join(_paragraph_text(p) for p in cell.iter(_W_NS + "p")).strip()
                         for cell in row.iter(_W_NS + "tc")]
                lines.append(" | ".join(cells))
    return "\n".join(line for line in lines if line.strip())


def _paragraph_text(paragraph):
    return "".join(node.text or "" for node in paragraph.iter(_W_NS + "t"))


def _safe_id(value):
    return re.sub(r"[^A-Za-z0-9_-]", "_", value or "default")[:64]


def _human_size(size):
    if size >= 2 ** 20:
        return "%.1f МБ" % (size / 2.0 ** 20)
    if size >= 1024:
        return "%.0f КБ" % (size / 1024.0)
    return "%d Б" % size
