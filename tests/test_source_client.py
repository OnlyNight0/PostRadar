"""Focused tests for source event parsing and persistence."""

import asyncio
import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import types
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from postradar.db.base import Base
from postradar.db.models import Category, Source, SourcePost, SourcePostMedia
from postradar.services.ai_editor import AIEditor, ProcessingResult
from postradar.telegram.source_client import (
    SourceMonitor,
    SourceResolutionError,
    detect_media_type,
    content_protection_state,
    load_enabled_sources,
    persist_message,
    persist_album,
    parse_source_identifier,
    require_user_account,
    validate_telegram_settings,
)


class SourceClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        no_sessions = patch("postradar.telegram.source_client.TelegramClient",
                            side_effect=AssertionError("Offline tests must inject a fake client"))
        no_sessions.start()
        self.addCleanup(no_sessions.stop)
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_media_type_detection(self) -> None:
        self.assertEqual(detect_media_type(SimpleNamespace(media=None)), "text")
        self.assertEqual(
            detect_media_type(SimpleNamespace(media=object(), photo=object())), "photo"
        )
        self.assertEqual(
            detect_media_type(SimpleNamespace(media=object(), video=object())), "video"
        )
        self.assertEqual(
            detect_media_type(
                SimpleNamespace(
                    media=object(), document=SimpleNamespace(mime_type="application/pdf")
                )
            ),
            "document",
        )
        self.assertEqual(detect_media_type(SimpleNamespace(media=object())), "other")

    async def test_duplicate_message_is_not_inserted_twice(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100123, enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        message = SimpleNamespace(
            id=7,
            message="caption text",
            date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            media=object(),
            photo=object(),
        )
        async def download(message, file):
            Path(file).write_bytes(b"fake media")
            return file
        client = SimpleNamespace(download_media=AsyncMock(side_effect=download))
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(await persist_message(self.factory, source, message, client, directory))
            self.assertFalse(await persist_message(self.factory, source, message, client, directory))
        async with self.factory() as session:
            count = await session.scalar(select(func.count()).select_from(SourcePost))
            saved = await session.scalar(select(SourcePost))
        self.assertEqual(count, 1)
        self.assertEqual(saved.original_text, "caption text")
        self.assertEqual(saved.sanitized_text, "caption text")
        self.assertEqual(saved.edited_text, "caption text")
        self.assertEqual(saved.media_type, "photo")

    async def test_complete_text_is_persisted_without_semantic_sanitization(self) -> None:
        original = (
            "Useful news text.\n\n"
            "Подписывайся: @example_channel\n"
            "https://t.me/example_channel"
        )
        async with self.factory() as session:
            source = Source(
                telegram_chat_id=-100456,
                username="example_channel",
                enabled=True,
            )
            session.add(source)
            await session.commit()
            await session.refresh(source)

        message = SimpleNamespace(id=12, message=original, date=None, media=None)
        self.assertTrue(await persist_message(self.factory, source, message))
        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost))
        self.assertEqual(saved.original_text, original)
        self.assertEqual(saved.sanitized_text, original)

    async def test_source_post_keeps_category_snapshot_after_source_is_reassigned(self) -> None:
        async with self.factory() as session:
            first_category = Category(name="First", enabled=True)
            second_category = Category(name="Second", enabled=True)
            session.add_all([first_category, second_category])
            await session.flush()
            source = Source(
                telegram_chat_id=-100745,
                enabled=True,
                category_id=first_category.id,
            )
            session.add(source)
            await session.commit()
            await session.refresh(source)
            first_category_id = first_category.id
            second_category_id = second_category.id

        first_message = SimpleNamespace(id=1, message="first post", date=None, media=None)
        self.assertTrue(await persist_message(self.factory, source, first_message))

        async with self.factory() as session:
            saved_source = await session.get(Source, source.id)
            saved_source.category_id = second_category_id
            await session.commit()
            await session.refresh(saved_source)
            source = saved_source

        second_message = SimpleNamespace(id=2, message="second post", date=None, media=None)
        self.assertTrue(await persist_message(self.factory, source, second_message))

        async with self.factory() as session:
            posts = list((await session.scalars(select(SourcePost).order_by(SourcePost.telegram_message_id))).all())
        self.assertEqual(posts[0].category_id, first_category_id)
        self.assertEqual(posts[1].category_id, second_category_id)

    async def test_three_album_messages_create_one_post_with_one_caption_and_one_ai_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with self.factory() as session:
                source = Source(telegram_chat_id=-100900, enabled=True)
                session.add(source)
                await session.commit()
                await session.refresh(source)
                source_id = source.id
            source = Source(id=source_id, telegram_chat_id=-100900, enabled=True)
            messages = [
                SimpleNamespace(
                    id=message_id,
                    grouped_id=5522,
                    message="Album caption" if message_id == 30 else None,
                    date=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    media=object(),
                    photo=object(),
                    file=SimpleNamespace(name=f"photo{message_id}.jpg", ext=".jpg"),
                )
                for message_id in (30, 31, 32)
            ]

            class FakeMediaClient:
                def __init__(self) -> None:
                    self.download_media = AsyncMock(side_effect=self._download)

                async def _download(self, message, file):
                    Path(file).write_bytes(b"photo")
                    return file

            client = FakeMediaClient()
            editor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult("CONTENT", "Standalone content", "Edited album caption")))
            self.assertTrue(
                await persist_album(
                    self.factory, source, 5522, messages, client, directory, editor
                )
            )
            self.assertFalse(
                await persist_album(
                    self.factory, source, 5522, messages, client, directory, editor
                )
            )
            self.assertFalse(
                await persist_album(
                    self.factory, source, 5522, messages[1:], client, directory, editor
                )
            )

            async with self.factory() as session:
                posts = list((await session.scalars(select(SourcePost))).all())
                media_items = list((await session.scalars(select(SourcePostMedia))).all())
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0].telegram_message_id, 30)
            self.assertEqual(posts[0].grouped_id, 5522)
            self.assertEqual(posts[0].original_text, "Album caption")
            self.assertEqual(posts[0].sanitized_text, "Album caption")
            self.assertEqual(posts[0].edited_text, "Edited album caption")
            self.assertEqual([item.telegram_message_id for item in media_items], [30, 31, 32])
            self.assertEqual([item.position for item in media_items], [0, 1, 2])
            editor.process.assert_awaited_once_with("Album caption", category_name=None, source_title=None, source_username=None)
            self.assertEqual(client.download_media.await_count, 3)

    async def test_failed_album_item_download_does_not_lose_other_items_or_caption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with self.factory() as session:
                source = Source(telegram_chat_id=-100904, enabled=True)
                session.add(source)
                await session.commit()
                await session.refresh(source)
            messages = [
                SimpleNamespace(
                    id=message_id,
                    grouped_id=9900,
                    message="Album caption" if index == 0 else None,
                    date=None,
                    media=object(),
                    video=object() if index == 1 else None,
                    photo=object() if index != 1 else None,
                    file=SimpleNamespace(name=f"item-{index}.bin", ext=".mp4" if index == 1 else ".jpg"),
                )
                for index, message_id in enumerate((51, 52, 53))
            ]

            class FakeMediaClient:
                async def download_media(self, message, file):
                    if message.id == 52:
                        raise RuntimeError("mocked item failure")
                    Path(file).write_bytes(b"media")
                    return file

            with self.assertLogs("postradar.telegram.source_client", level="ERROR"):
                self.assertTrue(
                    await persist_album(
                        self.factory,
                        source,
                        9900,
                        messages,
                        FakeMediaClient(),
                        directory,
                    )
                )
            async with self.factory() as session:
                post = await session.scalar(select(SourcePost))
                media_items = list((await session.scalars(select(SourcePostMedia))).all())
            self.assertEqual(post.original_text, "Album caption")
            self.assertEqual([item.telegram_message_id for item in media_items], [51, 53])

    async def test_album_identity_is_scoped_to_source_and_grouped_id(self) -> None:
        async with self.factory() as session:
            first = Source(telegram_chat_id=-100901, enabled=True)
            second = Source(telegram_chat_id=-100902, enabled=True)
            session.add_all([first, second])
            await session.commit()
            await session.refresh(first)
            await session.refresh(second)
        first_messages = [SimpleNamespace(id=1, message="one", date=None, media=None)]
        second_messages = [SimpleNamespace(id=2, message="two", date=None, media=None)]
        client = SimpleNamespace(download_media=AsyncMock())

        self.assertTrue(await persist_album(self.factory, first, 7001, first_messages, client))
        self.assertTrue(await persist_album(self.factory, first, 7002, second_messages, client))
        self.assertTrue(await persist_album(self.factory, second, 7001, first_messages, client))
        async with self.factory() as session:
            count = await session.scalar(select(func.count()).select_from(SourcePost))
        self.assertEqual(count, 3)

    async def test_album_debounce_collects_fragments_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with self.factory() as session:
                source = Source(telegram_chat_id=-100903, enabled=True)
                session.add(source)
                await session.commit()
                await session.refresh(source)

            class FakeClient:
                async def download_media(self, message, file):
                    Path(file).write_bytes(b"media")
                    return file

                async def get_entity(self, chat_id):
                    return SimpleNamespace(noforwards=False)

                async def get_messages(self, chat, ids):
                    return [SimpleNamespace(id=identity, noforwards=False) for identity in ids]

                async def disconnect(self):
                    pass

                def is_connected(self):
                    return False

            monitor = SourceMonitor(
                1,
                "api-hash",
                "session",
                self.factory,
                media_dir=directory,
                client=FakeClient(),
                album_collection_delay=0.1,
            )
            fragments = [
                SimpleNamespace(
                    id=message_id,
                    grouped_id=8001,
                    message="Caption" if message_id == 40 else None,
                    date=None,
                    media=object(),
                    photo=object(),
                    file=SimpleNamespace(name=f"{message_id}.jpg", ext=".jpg"),
                )
                for message_id in (40, 41, 42)
            ]
            for fragment in fragments:
                await monitor._collect_album_message(source, fragment, fragment.grouped_id)
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)

            async with self.factory() as session:
                post = await session.scalar(select(SourcePost))
                items = list((await session.scalars(select(SourcePostMedia))).all())
            self.assertEqual(post.grouped_id, 8001)
            self.assertEqual(len(items), 3)
            await monitor.close()

    async def test_media_only_message_with_no_caption_is_safe(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100789, username="media_source", enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        message = SimpleNamespace(id=13, message=None, date=None, media=object(), photo=object())
        async def download(message, file):
            Path(file).write_bytes(b"fake media")
            return file
        with tempfile.TemporaryDirectory() as directory:
            client = SimpleNamespace(download_media=AsyncMock(side_effect=download))
            self.assertTrue(await persist_message(self.factory, source, message, client, directory))
        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost))
        self.assertIsNone(saved.original_text)
        self.assertIsNone(saved.sanitized_text)
        self.assertEqual(saved.media_type, "photo")

    async def test_text_post_has_no_media_path_and_does_not_download(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100790, username="text_source", enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        message = SimpleNamespace(id=15, message="Only text", date=None, media=None)

        class FakeClient:
            async def download_media(self, message, file):
                raise AssertionError("text-only post must not download media")

        with tempfile.TemporaryDirectory() as media_directory:
            self.assertTrue(
                await persist_message(
                    self.factory,
                    source,
                    message,
                    media_client=FakeClient(),
                    media_dir=media_directory,
                )
            )
        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost))
        self.assertIsNone(saved.media_path)

    async def test_capture_does_not_call_legacy_sanitizer(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100987, username="source", enabled=True)
            session.add(source)
            await session.commit()
        message = SimpleNamespace(id=14, message="Original remains", date=None, media=None)
        with patch("postradar.services.sanitizer.sanitize_text", side_effect=AssertionError("legacy sanitizer must not run")) as sanitize:
            self.assertTrue(await persist_message(self.factory, source, message))
        sanitize.assert_not_called()
        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost))
        self.assertEqual(saved.original_text, "Original remains")
        self.assertEqual(saved.sanitized_text, "Original remains")

    async def test_media_download_path_is_persisted_and_duplicates_are_not_downloaded(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100246, username="photo_source", enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        message = SimpleNamespace(
            id=25,
            message="Caption",
            date=None,
            media=object(),
            photo=object(),
            file=SimpleNamespace(name=None, ext=".jpg"),
        )

        class FakeClient:
            download_count = 0

            async def download_media(self, message, file):
                self.download_count += 1
                Path(file).write_bytes(b"fake photo")
                return file

        client = FakeClient()
        with tempfile.TemporaryDirectory() as media_directory:
            inserted = await persist_message(
                self.factory, source, message, media_client=client, media_dir=media_directory
            )
            duplicate = await persist_message(
                self.factory, source, message, media_client=client, media_dir=media_directory
            )
            async with self.factory() as session:
                saved = await session.scalar(select(SourcePost))

            expected_path = Path(saved.media_path)
            self.assertEqual(expected_path.parent, Path(media_directory) / str(source.id))
            self.assertIn("capture-", expected_path.name)
            self.assertTrue(inserted)
            self.assertFalse(duplicate)
            self.assertEqual(client.download_count, 1)
            self.assertEqual(saved.media_path, str(expected_path))
            self.assertEqual(expected_path.read_bytes(), b"fake photo")

    async def test_media_download_failure_preserves_receipt_without_partial_candidate(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100357, username="video_source", enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        message = SimpleNamespace(
            id=26,
            message="Video caption",
            date=None,
            media=object(),
            video=object(),
            file=SimpleNamespace(name=None, ext=".mp4"),
        )

        class FailingClient:
            async def download_media(self, message, file):
                raise OSError("simulated download failure")

        with tempfile.TemporaryDirectory() as media_directory:
            with self.assertLogs("postradar.telegram.source_client", level="ERROR"):
                inserted = await persist_message(
                    self.factory,
                    source,
                    message,
                    media_client=FailingClient(),
                    media_dir=media_directory,
                )
            async with self.factory() as session:
                saved = await session.scalar(select(SourcePost))

        self.assertFalse(inserted)
        self.assertIsNone(saved.media_path)
        self.assertIsNone(saved.original_text)
        self.assertEqual(saved.status, "CAPTURE_PENDING")
        self.assertIsNotNone(saved.capture_next_attempt_at)

    async def test_ai_edit_is_persisted_without_changing_text_stages_or_reediting_duplicate(self) -> None:
        original = "OpenAI added a feature.\n\nПодписывайся: @example_channel"
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100468, username="example_channel", enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        create = AsyncMock(return_value=SimpleNamespace(text=' {"content_type":"CONTENT","reason":"News","edited_html":"OpenAI introduced a feature."} '))
        ai_editor = AIEditor(
            api_key="test-key",
            client=SimpleNamespace(
                aio=SimpleNamespace(models=SimpleNamespace(generate_content=create))
            ),
        )
        message = SimpleNamespace(id=30, message=original, date=None, media=None)

        self.assertTrue(await persist_message(self.factory, source, message, ai_editor=ai_editor))
        self.assertFalse(await persist_message(self.factory, source, message, ai_editor=ai_editor))
        create.assert_awaited_once()

        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost))
        self.assertEqual(saved.original_text, original)
        self.assertEqual(saved.sanitized_text, original)
        self.assertEqual(saved.edited_text, "OpenAI introduced a feature.")

    async def test_missing_api_key_fallback_still_persists_post(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100579, username="source", enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        message = SimpleNamespace(id=31, message="Sanitized source text", date=None, media=None)
        with self.assertLogs("postradar.services.ai_editor", level="WARNING"):
            editor = AIEditor(api_key="", enabled=True)
            self.assertTrue(await persist_message(self.factory, source, message, ai_editor=editor))
        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost))
        self.assertEqual(saved.original_text, "Sanitized source text")
        self.assertEqual(saved.sanitized_text, "Sanitized source text")
        self.assertEqual(saved.edited_text, "Sanitized source text")

    async def test_gemini_exception_does_not_lose_source_post(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100581, username="source", enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        create = AsyncMock(side_effect=RuntimeError("simulated API failure"))
        editor = AIEditor(
            api_key="test-key",
            client=SimpleNamespace(
                aio=SimpleNamespace(models=SimpleNamespace(generate_content=create))
            ),
        )
        message = SimpleNamespace(id=33, message="Sanitized text survives", date=None, media=None)
        with self.assertLogs("postradar.services.ai_editor", level="WARNING"):
            inserted = await persist_message(self.factory, source, message, ai_editor=editor)
        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost))

        self.assertTrue(inserted)
        self.assertEqual(saved.sanitized_text, "Sanitized text survives")
        self.assertEqual(saved.edited_text, "Sanitized text survives")

    async def test_media_only_post_does_not_call_ai_editor(self) -> None:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100680, username="media_source", enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)

        edit = AsyncMock(side_effect=AssertionError("media-only post must skip AI editing"))
        editor = SimpleNamespace(edit=edit)
        message = SimpleNamespace(id=32, message=None, date=None, media=object(), photo=object())
        async def download(message, file):
            Path(file).write_bytes(b"fake media")
            return file
        client = SimpleNamespace(download_media=AsyncMock(side_effect=download))
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(await persist_message(
                self.factory, source, message, media_client=client,
                media_dir=directory, ai_editor=editor,
            ))
        edit.assert_not_awaited()
        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost))
        self.assertIsNone(saved.edited_text)

    async def test_disabled_sources_are_not_loaded(self) -> None:
        async with self.factory() as session:
            session.add_all(
                [
                    Source(telegram_chat_id=-1001, enabled=True),
                    Source(telegram_chat_id=-1002, enabled=False),
                ]
            )
            await session.commit()
        sources = await load_enabled_sources(self.factory)
        self.assertEqual([source.telegram_chat_id for source in sources], [-1001])

    async def test_missing_credentials_fail_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "TELEGRAM_API_ID"):
            validate_telegram_settings(None, "", "")

    async def test_bot_identity_is_rejected(self) -> None:
        class FakeClient:
            async def get_me(self):
                return SimpleNamespace(bot=True)

        with self.assertRaisesRegex(RuntimeError, "belongs to a bot"):
            await require_user_account(FakeClient())

    async def test_user_identity_is_accepted(self) -> None:
        user = SimpleNamespace(bot=False)

        class FakeClient:
            async def get_me(self):
                return user

        self.assertIs(await require_user_account(FakeClient()), user)

    async def _protection_source(self) -> Source:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100909, enabled=True)
            session.add(source)
            await session.commit()
            await session.refresh(source)
            return source

    async def test_protection_state_checks_chat_and_message_and_fails_closed(self) -> None:
        unprotected_chat = SimpleNamespace(noforwards=False)
        self.assertFalse(content_protection_state(unprotected_chat, SimpleNamespace(noforwards=False)))
        self.assertTrue(content_protection_state(
            SimpleNamespace(noforwards=True), SimpleNamespace(noforwards=False)
        ))
        self.assertTrue(content_protection_state(
            unprotected_chat, SimpleNamespace(noforwards=True)
        ))
        self.assertIsNone(content_protection_state(None, SimpleNamespace(noforwards=False)))
        self.assertIsNone(content_protection_state(unprotected_chat, SimpleNamespace()))

    async def test_publish_guard_checks_protection_on_original_messages(self) -> None:
        chat = SimpleNamespace(noforwards=False)
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=chat),
            get_messages=AsyncMock(return_value=[
                SimpleNamespace(id=10, noforwards=False),
                SimpleNamespace(id=11, noforwards=True),
            ]),
        )
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client)

        self.assertTrue(await monitor.protection_for_source(-100100, [10, 11]))
        client.get_messages.assert_awaited_once_with(chat, ids=[10, 11])

    async def test_publish_guard_fails_closed_when_original_protection_cannot_be_read(self) -> None:
        chat = SimpleNamespace(noforwards=False)
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=chat),
            get_messages=AsyncMock(return_value=[SimpleNamespace(id=10, noforwards=None)]),
        )
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client)

        self.assertIsNone(await monitor.protection_for_source(-100100, [10]))

    async def test_protected_channel_message_is_marked_without_ai_or_media_access(self) -> None:
        source = await self._protection_source()
        editor = SimpleNamespace(process=AsyncMock())
        client = SimpleNamespace(download_media=AsyncMock(), is_connected=lambda: False)
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)
        message = SimpleNamespace(
            id=901, message="protected text", noforwards=False, grouped_id=None,
            media=object(), photo=object(),
        )

        await monitor._handle_source_message(
            source, message, SimpleNamespace(noforwards=True)
        )

        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 901))
        self.assertEqual(saved.status, "PROTECTED")
        self.assertIsNone(saved.original_text)
        self.assertIsNone(saved.source_html)
        self.assertIsNone(saved.media_path)
        editor.process.assert_not_awaited()
        client.download_media.assert_not_awaited()

    async def test_individually_protected_message_is_not_copied(self) -> None:
        source = await self._protection_source()
        editor = SimpleNamespace(process=AsyncMock())
        client = SimpleNamespace(download_media=AsyncMock(), is_connected=lambda: False)
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)
        message = SimpleNamespace(
            id=902, message="protected caption", noforwards=True, grouped_id=None,
            media=object(), photo=object(),
        )

        await monitor._handle_source_message(
            source, message, SimpleNamespace(noforwards=False)
        )

        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 902))
        self.assertEqual(saved.status, "PROTECTED")
        self.assertIsNone(saved.original_text)
        editor.process.assert_not_awaited()
        client.download_media.assert_not_awaited()

    async def test_protected_album_discards_buffer_and_never_processes_fragments(self) -> None:
        source = await self._protection_source()
        editor = SimpleNamespace(process=AsyncMock())
        client = SimpleNamespace(download_media=AsyncMock(), is_connected=lambda: False)
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)
        chat = SimpleNamespace(noforwards=False)
        first = SimpleNamespace(
            id=910, grouped_id=99, message="caption", noforwards=False,
            media=object(), photo=object(),
        )
        protected = SimpleNamespace(
            id=911, grouped_id=99, message="secret fragment", noforwards=True,
            media=object(), photo=object(),
        )

        await monitor._handle_source_message(source, first, chat)
        self.assertIn((source.id, 99), monitor._album_batches)
        await monitor._handle_source_message(source, protected, chat)

        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost).where(SourcePost.grouped_id == 99))
        self.assertEqual(saved.status, "PROTECTED")
        self.assertIsNone(saved.original_text)
        self.assertEqual(monitor._album_batches, {})
        editor.process.assert_not_awaited()
        client.download_media.assert_not_awaited()

    async def test_unavailable_protection_metadata_skips_without_logging_content(self) -> None:
        source = await self._protection_source()
        editor = SimpleNamespace(process=AsyncMock())
        client = SimpleNamespace(download_media=AsyncMock(), is_connected=lambda: False)
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)
        message = SimpleNamespace(
            id=903, message="private body that must not be logged", grouped_id=None,
            media=None,
        )

        with self.assertLogs("postradar.telegram.source_client", level="INFO") as logs:
            await monitor._handle_source_message(source, message, None)

        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 903))
        self.assertEqual(saved.status, "PROTECTION_CAPTURE_PENDING")
        self.assertIsNone(saved.original_text)
        self.assertNotIn("private body", " ".join(logs.output))
        editor.process.assert_not_awaited()
        client.download_media.assert_not_awaited()

    async def _create_capture_marker(
        self, message_id: int, *, grouped_id: int | None = None, enabled: bool = True
    ) -> Source:
        async with self.factory() as session:
            source = Source(telegram_chat_id=-100000 - message_id, enabled=enabled)
            session.add(source)
            await session.commit()
            await session.refresh(source)
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=SimpleNamespace())
        message = SimpleNamespace(
            id=message_id, grouped_id=grouped_id, message="private incoming text",
            media=None, noforwards=None,
        )
        await monitor._handle_source_message(source, message, None)
        return source

    async def test_pending_capture_recovers_in_place_after_protection_clears(self) -> None:
        source = await self._create_capture_marker(920)
        message = SimpleNamespace(
            id=920, grouped_id=None, message="Recovered source", date=None,
            media=None, noforwards=False, entities=[],
        )
        editor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult(
            "CONTENT", "Recovered safely", "<p>Recovered safely</p>"
        )))
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=message),
        )
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)
        async with self.factory() as session:
            marker = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 920))
            marker_id = marker.id

        self.assertEqual(await monitor.recover_pending_captures(), 1)
        async with self.factory() as session:
            recovered = await session.get(SourcePost, marker_id)
            count = await session.scalar(select(func.count()).select_from(SourcePost))
        self.assertEqual(count, 1)
        self.assertEqual(recovered.id, marker_id)
        self.assertEqual(recovered.status, "NEW")
        self.assertEqual(recovered.original_text, "Recovered source")
        editor.process.assert_awaited_once()
        self.assertEqual(await monitor.recover_pending_captures(), 0)
        editor.process.assert_awaited_once()

    async def test_pending_capture_keeps_retrying_when_protection_is_unknown(self) -> None:
        await self._create_capture_marker(921)
        message = SimpleNamespace(id=921, noforwards=None)
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=message),
        )
        editor = SimpleNamespace(process=AsyncMock())
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)

        self.assertEqual(await monitor.recover_pending_captures(), 0)
        async with self.factory() as session:
            marker = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 921))
        self.assertEqual(marker.status, "PROTECTION_CAPTURE_PENDING")
        self.assertIsNone(marker.original_text)
        editor.process.assert_not_awaited()

    async def test_pending_capture_confirmed_protected_is_scrubbed_and_never_processed(self) -> None:
        await self._create_capture_marker(922)
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=True)),
            get_messages=AsyncMock(),
        )
        editor = SimpleNamespace(process=AsyncMock())
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)

        self.assertEqual(await monitor.recover_pending_captures(), 0)
        async with self.factory() as session:
            marker = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 922))
        self.assertEqual(marker.status, "PROTECTED")
        self.assertIsNone(marker.original_text)
        client.get_messages.assert_not_awaited()
        editor.process.assert_not_awaited()

    async def test_historical_unverified_row_is_never_reactivated(self) -> None:
        source = await self._protection_source()
        async with self.factory() as session:
            session.add(SourcePost(
                source_id=source.id, telegram_message_id=923,
                status="PROTECTION_UNVERIFIED", original_text=None, edited_text=None,
            ))
            await session.commit()
        client = SimpleNamespace(get_entity=AsyncMock(), get_messages=AsyncMock())
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client)

        self.assertEqual(await monitor.recover_pending_captures(), 0)
        client.get_entity.assert_not_awaited()
        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 923))
        self.assertEqual(saved.status, "PROTECTION_UNVERIFIED")

    async def test_pending_marker_survives_restart_and_retries_after_fetch_failure(self) -> None:
        await self._create_capture_marker(924)
        failed_client = SimpleNamespace(get_entity=AsyncMock(side_effect=RuntimeError("offline")))
        first_monitor = SourceMonitor(1, "hash", "session", self.factory, client=failed_client)
        with self.assertLogs("postradar.telegram.source_client", level="WARNING"):
            self.assertEqual(await first_monitor.recover_pending_captures(), 0)

        message = SimpleNamespace(
            id=924, grouped_id=None, message="Recovered after restart", date=None,
            media=None, noforwards=False, entities=[],
        )
        editor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult(
            "CONTENT", "Recovered", "<p>Recovered</p>"
        )))
        second_monitor = SourceMonitor(1, "hash", "session", self.factory, client=SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=message),
        ), ai_editor=editor)
        self.assertEqual(await second_monitor.recover_pending_captures(), 1)
        editor.process.assert_awaited_once()

    async def test_concurrent_recovery_attempts_do_not_duplicate_processing(self) -> None:
        await self._create_capture_marker(925)
        message = SimpleNamespace(
            id=925, grouped_id=None, message="Recovered once", date=None,
            media=None, noforwards=False, entities=[],
        )
        async def process(*_args, **_kwargs):
            await asyncio.sleep(0)
            return ProcessingResult("CONTENT", "Recovered", "<p>Recovered</p>")
        editor = SimpleNamespace(process=AsyncMock(side_effect=process))
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=message),
        )
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)

        await asyncio.gather(
            monitor.recover_pending_captures(), monitor.recover_pending_captures()
        )
        editor.process.assert_awaited_once()
        async with self.factory() as session:
            count = await session.scalar(select(func.count()).select_from(SourcePost))
        self.assertEqual(count, 1)

    async def test_disabled_source_and_missing_message_do_not_create_candidates(self) -> None:
        await self._create_capture_marker(926, enabled=False)
        await self._create_capture_marker(927)
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=None),
        )
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client)

        self.assertEqual(await monitor.recover_pending_captures(limit=10), 0)
        self.assertEqual(client.get_entity.await_count, 1)
        async with self.factory() as session:
            missing = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 927))
            disabled = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 926))
        self.assertEqual(missing.status, "SKIPPED")
        self.assertEqual(disabled.status, "PROTECTION_CAPTURE_PENDING")

    async def test_album_pending_marker_is_never_recovered_as_a_single_message(self) -> None:
        await self._create_capture_marker(928, grouped_id=1234)
        client = SimpleNamespace(get_entity=AsyncMock(), get_messages=AsyncMock())
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client)

        self.assertEqual(await monitor.recover_pending_captures(), 0)
        client.get_entity.assert_not_awaited()
        async with self.factory() as session:
            marker = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 928))
        self.assertEqual(marker.status, "PROTECTION_ALBUM_PENDING")

    async def test_missing_recovered_media_keeps_marker_without_partial_candidate(self) -> None:
        await self._create_capture_marker(929)
        message = SimpleNamespace(
            id=929, grouped_id=None, message="caption", date=None,
            media=object(), photo=object(), noforwards=False, entities=[],
        )
        editor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult(
            "CONTENT", "Valid", "<p>Valid</p>"
        )))
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=message),
            download_media=AsyncMock(return_value=None),
        )
        with tempfile.TemporaryDirectory() as directory:
            monitor = SourceMonitor(
                1, "hash", "session", self.factory, media_dir=directory,
                client=client, ai_editor=editor,
            )
            with self.assertLogs("postradar.telegram.source_client", level="WARNING"):
                self.assertEqual(await monitor.recover_pending_captures(), 0)

        async with self.factory() as session:
            marker = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 929))
        self.assertEqual(marker.status, "CAPTURE_PENDING")
        self.assertIsNone(marker.original_text)
        self.assertIsNone(marker.media_path)

    async def test_unknown_protection_preserves_existing_candidate_and_media(self) -> None:
        source = await self._protection_source()
        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "existing.jpg"
            media_path.write_bytes(b"existing media")
            async with self.factory() as session:
                post = SourcePost(
                    source_id=source.id,
                    telegram_message_id=905,
                    status="REVIEW",
                    original_text="Saved source text",
                    sanitized_text="Saved sanitized text",
                    edited_text="Saved edit",
                    source_html="<p>saved source</p>",
                    edited_html="<p>saved edit</p>",
                    media_type="photo",
                    media_path=str(media_path),
                )
                session.add(post)
                await session.commit()

            monitor = SourceMonitor(1, "hash", "session", self.factory, media_dir=directory, client=SimpleNamespace())
            message = SimpleNamespace(id=905, grouped_id=None, noforwards=None, media=None)
            await monitor._handle_source_message(source, message, None)

            async with self.factory() as session:
                saved = await session.scalar(
                    select(SourcePost).where(SourcePost.telegram_message_id == 905)
                )
            self.assertEqual(saved.status, "REVIEW")
            self.assertEqual(saved.original_text, "Saved source text")
            self.assertEqual(saved.sanitized_text, "Saved sanitized text")
            self.assertEqual(saved.edited_text, "Saved edit")
            self.assertEqual(saved.source_html, "<p>saved source</p>")
            self.assertEqual(saved.edited_html, "<p>saved edit</p>")
            self.assertEqual(saved.media_path, str(media_path))
            self.assertTrue(media_path.is_file())

    async def test_unprotected_event_still_enters_normal_processing(self) -> None:
        source = await self._protection_source()
        message = SimpleNamespace(
            id=904, message="ordinary source text", noforwards=False, grouped_id=None,
            date=None, media=None, entities=[],
        )
        editor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult(
            "CONTENT", "Useful content", "ordinary source text"
        )))
        client = SimpleNamespace(download_media=AsyncMock(), is_connected=lambda: False,
            get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=message))
        monitor = SourceMonitor(1, "hash", "session", self.factory, client=client, ai_editor=editor)

        await monitor._handle_source_message(
            source, message, SimpleNamespace(noforwards=False)
        )

        async with self.factory() as session:
            saved = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 904))
        self.assertEqual(saved.status, "NEW")
        self.assertEqual(saved.original_text, "ordinary source text")
        editor.process.assert_awaited_once()

    async def test_source_identifier_accepts_username_link_and_numeric_id(self) -> None:
        self.assertEqual(parse_source_identifier("@sample_channel"), "@sample_channel")
        self.assertEqual(
            parse_source_identifier("https://t.me/sample_channel/123"), "@sample_channel"
        )
        self.assertEqual(parse_source_identifier("-100123456"), -100123456)
        private_reference = parse_source_identifier("https://t.me/c/123456/99")
        self.assertIsInstance(private_reference, types.PeerChannel)
        self.assertEqual(private_reference.channel_id, 123456)

    async def test_source_resolver_uses_user_client_and_canonical_peer_id(self) -> None:
        entity = types.Channel(
            id=123,
            title="Sample channel",
            photo=types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc),
            access_hash=987,
            broadcast=True,
            username="sample_channel",
        )

        class FakeClient:
            def __init__(self) -> None:
                self.get_entity = AsyncMock(return_value=entity)

        fake_client = FakeClient()
        monitor = SourceMonitor(1, "api-hash", "session", self.factory, client=fake_client)
        monitor._ready.set()

        references = (
            ("@sample_channel", "@sample_channel"),
            ("https://t.me/sample_channel/88", "@sample_channel"),
            ("-1000000000123", -1000000000123),
        )
        for identifier, _reference in references:
            resolved = await monitor.resolve_source(identifier)
            self.assertEqual(resolved.telegram_chat_id, -1000000000123)
            self.assertEqual(resolved.title, "Sample channel")
            self.assertEqual(resolved.username, "sample_channel")
        self.assertEqual(
            [call.args[0] for call in fake_client.get_entity.await_args_list],
            [reference for _identifier, reference in references],
        )

    async def test_source_resolver_returns_safe_errors_for_invalid_or_inaccessible_source(self) -> None:
        class FakeClient:
            async def get_entity(self, reference):
                raise RuntimeError("private details")

        monitor = SourceMonitor(1, "api-hash", "session", self.factory, client=FakeClient())
        monitor._ready.set()
        with self.assertRaisesRegex(SourceResolutionError, "cannot access"):
            await monitor.resolve_source("@sample_channel")
        with self.assertRaisesRegex(SourceResolutionError, "t.me"):
            await monitor.resolve_source("https://example.com/not-telegram")
