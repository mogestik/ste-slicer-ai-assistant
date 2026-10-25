"""Фото во вложениях: загрузка -> уменьшение/кодирование -> data URL в image_url.

В STE Slicer фото уменьшаются через QImage (PyQt5); на голом стенде без Qt и Pillow
маленькие JPG/PNG/WEBP уходят как есть, а большие отклоняются.

Запуск из корня репозитория: python -m unittest tests.test_attachments
"""

import base64
import io
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_DIR, "plugin", "AIAssistant")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from agent import attachments  # noqa: E402
from agent.attachments import (AttachmentError, UploadStore, build_user_content, encode_image,  # noqa: E402
                               image_dimensions, sniff_image_format)

try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def write_png(path, width, height, rgba=False, noise=False):
    channels = 4 if rgba else 3
    rows = []
    for y in range(height):
        if noise:
            row = bytes(((x * 7 + y * 13) * 31) % 256 for x in range(width * channels))
        else:
            row = b"\x80" * (width * channels)
        rows.append(b"\x00" + row)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6 if rgba else 2, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) +
                     chunk(b"IEND", b""))


def write_fake_jpeg(path, width, height, padding=0):
    """Заголовок JPEG с сегментами APP0 и SOF0 (хватает, чтобы узнать формат и размеры)."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    with open(path, "wb") as handle:
        handle.write(b"\xff\xd8" + app0 + sof0 + b"\x00" * padding + b"\xff\xd9")


def no_resizer():
    def unavailable(path, max_side):
        raise ImportError("no resizer")
    return mock.patch.multiple(attachments, _encode_with_qt=unavailable, _encode_with_pillow=unavailable)


class AttachmentImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiassistant_att_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_sniff_and_dimensions(self):
        write_png(self.path("a.png"), 30, 20)
        write_fake_jpeg(self.path("b.jpg"), 4000, 3000)
        with open(self.path("c.jpg"), "w") as handle:
            handle.write("not an image")
        self.assertEqual(sniff_image_format(self.path("a.png")), "png")
        self.assertEqual(sniff_image_format(self.path("b.jpg")), "jpeg")
        self.assertIsNone(sniff_image_format(self.path("c.jpg")))
        self.assertEqual(tuple(image_dimensions(self.path("a.png"))), (30, 20))
        self.assertEqual(tuple(image_dimensions(self.path("b.jpg"))), (4000, 3000))

    def test_fallback_without_resizer_sends_small_files_unchanged(self):
        write_png(self.path("small.png"), 40, 30)
        write_fake_jpeg(self.path("big_dims.jpg"), 4000, 3000, padding=1000)
        with no_resizer():
            mime, data, warning = encode_image(self.path("small.png"), 1280)
            self.assertEqual(mime, "image/png")
            with open(self.path("small.png"), "rb") as handle:
                self.assertEqual(data, handle.read())
            self.assertIsNone(warning)
            mime, _data, warning = encode_image(self.path("big_dims.jpg"), 1280)
            self.assertEqual(mime, "image/jpeg")
            self.assertIn("без уменьшения", warning)

    def test_fallback_refuses_large_and_unsupported_files(self):
        write_fake_jpeg(self.path("huge.jpg"), 6000, 4000, padding=attachments.MAX_RAW_IMAGE_BYTES + 10)
        with open(self.path("pic.bmp"), "wb") as handle:
            handle.write(b"BM" + b"\x00" * 100)
        with open(self.path("text.png"), "w") as handle:
            handle.write("hello")
        with no_resizer():
            with self.assertRaises(AttachmentError) as ctx:
                encode_image(self.path("huge.jpg"), 1280)
            self.assertIn("слишком большое", str(ctx.exception))
            with self.assertRaises(AttachmentError) as ctx:
                encode_image(self.path("pic.bmp"), 1280)
            self.assertIn("JPG или PNG", str(ctx.exception))
        with self.assertRaises(AttachmentError):
            encode_image(self.path("text.png"), 1280)

    @unittest.skipUnless(HAVE_PIL, "Pillow не установлен")
    def test_pillow_downscales_to_max_side(self):
        write_png(self.path("wide.png"), 2000, 500, noise=True)
        with mock.patch.object(attachments, "_encode_with_qt", side_effect=ImportError):
            mime, data, warning = encode_image(self.path("wide.png"), 1000)
        self.assertEqual(mime, "image/jpeg")
        self.assertIsNone(warning)
        out = self.path("out.jpg")
        with open(out, "wb") as handle:
            handle.write(data)
        self.assertEqual(tuple(image_dimensions(out)), (1000, 250))

    @unittest.skipUnless(HAVE_PIL, "Pillow не установлен")
    def test_pillow_flattens_transparency(self):
        write_png(self.path("alpha.png"), 50, 50, rgba=True)
        with mock.patch.object(attachments, "_encode_with_qt", side_effect=ImportError):
            mime, data, _warning = encode_image(self.path("alpha.png"), 1280)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(data[:3], b"\xff\xd8\xff")

    def test_encoder_failure_falls_through(self):
        write_png(self.path("small.png"), 10, 10)

        def broken(path, max_side):
            raise RuntimeError("codec crashed")
        with mock.patch.multiple(attachments, _encode_with_qt=broken, _encode_with_pillow=broken):
            mime, _data, _warning = encode_image(self.path("small.png"), 1280)
        self.assertEqual(mime, "image/png")

    def test_upload_to_user_content(self):
        store = UploadStore(self.path("uploads"))
        write_png(self.path("photo.png"), 64, 48)
        with open(self.path("photo.png"), "rb") as handle:
            raw = handle.read()
        image = store.save_stream("s1", "фото детали.png", io.BytesIO(raw), len(raw))
        model = store.save_stream("s1", "gear.stl", io.BytesIO(b"solid x\nendsolid x\n"), 19)
        with no_resizer():
            content = build_user_content("Что это?", [image, model], 1280)
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("фото детали.png", content[0]["text"])
        self.assertIn("load_model", content[0]["text"])
        url = content[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), raw)

    def test_failed_image_becomes_a_note(self):
        store = UploadStore(self.path("uploads"))
        bogus = b"not really a png"
        record = store.save_stream("s1", "x.png", io.BytesIO(bogus), len(bogus))
        content = build_user_content("", [record], 1280)
        self.assertIsInstance(content, str)
        self.assertIn("не передано", content)


if __name__ == "__main__":
    unittest.main()
