"""Tests for media path generation and local Telethon download handling."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from postradar.services.media import download_message_media, media_target_path


class MediaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_message_has_no_media_path(self) -> None:
        message = SimpleNamespace(id=5, media=None)
        self.assertIsNone(media_target_path("data/media", 3, message))

    async def test_photo_path_is_deterministic(self) -> None:
        message = SimpleNamespace(
            id=17,
            media=object(),
            photo=object(),
            file=SimpleNamespace(name=None, ext=".jpg"),
        )
        first = media_target_path("data/media", 8, message)
        second = media_target_path("data/media", 8, message)
        self.assertEqual(first, second)
        self.assertEqual(first, Path("data/media/8/17_photo.jpg"))

    async def test_video_path_preserves_telethon_extension(self) -> None:
        message = SimpleNamespace(
            id=21,
            media=object(),
            video=object(),
            file=SimpleNamespace(name=None, ext=".mkv"),
        )
        self.assertEqual(
            media_target_path("media", 4, message),
            Path("media/4/21_video.mkv"),
        )

    async def test_document_filename_is_safe_and_keeps_extension(self) -> None:
        message = SimpleNamespace(
            id=22,
            media=object(),
            document=SimpleNamespace(mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            file=SimpleNamespace(name="../../Quarterly report.xlsx", ext=".xlsx"),
        )
        path = media_target_path("media", 4, message)
        self.assertEqual(path, Path("media/4/22_Quarterly_report.xlsx"))
        self.assertEqual(path.suffix, ".xlsx")

    async def test_telethon_download_creates_parent_and_returns_path(self) -> None:
        class FakeClient:
            async def download_media(self, message, file):
                Path(file).write_bytes(b"local fake media")
                return file

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source" / "post.jpg"
            saved_path = await download_message_media(FakeClient(), object(), target)
            self.assertEqual(saved_path, str(target))
            self.assertEqual(target.read_bytes(), b"local fake media")


if __name__ == "__main__":
    unittest.main()
