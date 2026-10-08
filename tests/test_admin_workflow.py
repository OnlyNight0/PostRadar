"""Mocked tests for the admin review workflow."""

import tempfile
import unittest
from pathlib import Path
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import FSInputFile
from aiogram.types import InputMediaDocument
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from postradar.bot.admin_bot import validate_admin_settings
from postradar.bot.handlers import create_admin_router
from postradar.bot.review import AdminWorkflow, deliver_new_posts, fits_caption
from postradar.db.base import Base
from postradar.db.models import Category, Source, SourcePost, SourcePostMedia
from postradar.services.ai_editor import ProcessingResult
from postradar.telegram.source_client import SourceMonitor, mark_post_protected


class FakeBot:
    def __init__(self) -> None:
        self.send_message = AsyncMock(side_effect=self._message)
        self.send_photo = AsyncMock(side_effect=self._message)
        self.send_video = AsyncMock(side_effect=self._message)
        self.send_document = AsyncMock(side_effect=self._message)
        self.send_media_group = AsyncMock(side_effect=self._media_group)
        self.delete_message = AsyncMock()
        self.edit_message_text = AsyncMock(side_effect=self._message)
        self.edit_message_caption = AsyncMock(side_effect=self._message)
        self._next_id = 100

    async def _message(self, **kwargs):
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id, kwargs=kwargs)

    async def _media_group(self, **kwargs):
        messages = []
        for _item in kwargs["media"]:
            self._next_id += 1
            messages.append(SimpleNamespace(message_id=self._next_id, kwargs=kwargs))
        return messages


class AdminWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.bot = FakeBot()
        self._message_id = 777
        self._media_directory = tempfile.TemporaryDirectory()
        self.media_dir = Path(self._media_directory.name) / "media"
        self.media_dir.mkdir()
        self.source = Source(telegram_chat_id=-100111, destination_channel_id=-100999)
        async with self.factory() as session:
            session.add(self.source)
            await session.commit()
            await session.refresh(self.source)
        self.workflow = AdminWorkflow(self.bot, self.factory, 12345, media_dir=self.media_dir)
        self.workflow.source_monitor = SimpleNamespace(
            protection_for_source=AsyncMock(return_value=False)
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._media_directory.cleanup()

    def create_media(self, name: str = "local-media.bin") -> Path:
        path = self.media_dir / name
        path.write_bytes(b"test media")
        return path

    async def add_post(
        self,
        *,
        status: str = "NEW",
        media_type: str | None = "text",
        media_path: str | None = None,
        edited_text: str | None = "Edited candidate",
        sanitized_text: str | None = "Sanitized candidate",
        original_text: str | None = "Original source",
        admin_message_id: int | None = None,
    ) -> SourcePost:
        async with self.factory() as session:
            post = SourcePost(
                source_id=self.source.id,
                telegram_message_id=self._message_id,
                status=status,
                media_type=media_type,
                media_path=media_path,
                admin_message_id=admin_message_id,
                edited_text=edited_text,
                sanitized_text=sanitized_text,
                original_text=original_text,
            )
            session.add(post)
            await session.commit()
            await session.refresh(post)
            self._message_id += 1
            return post

    async def saved_post(self, post_id: int) -> SourcePost:
        async with self.factory() as session:
            return await session.scalar(select(SourcePost).where(SourcePost.id == post_id))

    async def add_album(
        self,
        *,
        status: str = "REVIEW",
        item_count: int = 3,
        edited_text: str | None = "Edited album caption",
        admin_message_id: int | None = None,
    ) -> tuple[SourcePost, list[Path]]:
        paths = [self.create_media(f"album-{self._message_id}-{index}.jpg") for index in range(item_count)]
        async with self.factory() as session:
            post = SourcePost(
                source_id=self.source.id,
                telegram_message_id=self._message_id,
                grouped_id=self._message_id + 10000,
                status=status,
                media_type="album",
                original_text="Original album caption",
                sanitized_text="Sanitized album caption",
                edited_text=edited_text,
                admin_message_id=admin_message_id,
                media_items=[
                    SourcePostMedia(
                        telegram_message_id=self._message_id + index,
                        media_type="photo",
                        media_path=str(path),
                        position=index,
                    )
                    for index, path in enumerate(paths)
                ],
            )
            session.add(post)
            await session.commit()
            await session.refresh(post)
            self._message_id += item_count + 1
            return post, paths

    async def test_new_post_is_delivered_and_marked_review(self) -> None:
        post = await self.add_post()
        delivered = await deliver_new_posts(self.bot, self.factory, 12345)
        saved = await self.saved_post(post.id)

        self.assertEqual(delivered, 1)
        self.assertEqual(saved.status, "REVIEW")
        self.assertIsNotNone(saved.admin_message_id)
        self.bot.send_message.assert_awaited_once()
        arguments = self.bot.send_message.await_args.kwargs
        self.assertEqual(arguments["chat_id"], 12345)
        self.assertEqual(arguments["text"], "Edited candidate")
        self.assertIn("publish:", arguments["reply_markup"].inline_keyboard[0][0].callback_data)

    async def test_protection_is_rechecked_before_review_delivery(self) -> None:
        post = await self.add_post(status="NEW")
        self.workflow.source_monitor.protection_for_source.return_value = True

        self.assertEqual(await self.workflow.deliver_new(), 0)

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "PROTECTED")
        self.assertIsNone(saved.original_text)
        self.assertIsNone(saved.edited_text)
        self.bot.send_message.assert_not_awaited()

    async def test_unavailable_protection_blocks_review_delivery(self) -> None:
        post = await self.add_post(status="NEW")
        self.workflow.source_monitor.protection_for_source.return_value = None

        self.assertEqual(await self.workflow.deliver_new(), 0)

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "NEW")
        self.assertEqual(saved.original_text, "Original source")
        self.assertEqual(saved.edited_text, "Edited candidate")
        self.bot.send_message.assert_not_awaited()

        self.workflow.source_monitor.protection_for_source.return_value = False
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.assertEqual((await self.saved_post(post.id)).status, "REVIEW")

    async def test_unknown_new_candidate_does_not_starve_later_candidates(self) -> None:
        first = await self.add_post(status="NEW")
        second = await self.add_post(status="NEW")
        cursor = [0]
        checks = AsyncMock(side_effect=[None, False])

        self.assertEqual(await deliver_new_posts(
            self.bot, self.factory, 12345, limit=1,
            protection_check=checks, protection_block=self.workflow._block_for_protection,
            cursor_state=cursor,
        ), 0)
        self.assertEqual(await deliver_new_posts(
            self.bot, self.factory, 12345, limit=1,
            protection_check=checks, protection_block=self.workflow._block_for_protection,
            cursor_state=cursor,
        ), 1)

        self.assertEqual((await self.saved_post(first.id)).status, "NEW")
        self.assertEqual((await self.saved_post(second.id)).status, "REVIEW")
        self.assertEqual(checks.await_count, 2)

    async def test_recovered_capture_is_delivered_for_review_only_once(self) -> None:
        async with self.factory() as session:
            marker = SourcePost(
                source_id=self.source.id,
                telegram_message_id=self._message_id,
                status="PROTECTION_CAPTURE_PENDING",
                category_id=self.source.category_id,
            )
            session.add(marker)
            await session.commit()
            await session.refresh(marker)
            marker_id = marker.id
            message_id = marker.telegram_message_id
            self._message_id += 1

        message = SimpleNamespace(
            id=message_id, grouped_id=None, message="Recovered candidate", date=None,
            media=None, noforwards=False, entities=[],
        )
        chat = SimpleNamespace(noforwards=False)
        editor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult(
            "CONTENT", "Recovered", "<p>Recovered candidate</p>"
        )))
        client = SimpleNamespace(
            get_entity=AsyncMock(return_value=chat),
            get_messages=AsyncMock(return_value=message),
        )
        self.workflow.source_monitor = SourceMonitor(
            1, "hash", "session", self.factory, client=client, ai_editor=editor
        )

        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.assertEqual(await self.workflow.deliver_new(), 0)
        saved = await self.saved_post(marker_id)
        self.assertEqual(saved.status, "REVIEW")
        self.assertEqual(saved.telegram_message_id, message_id)
        self.bot.send_message.assert_awaited_once()

    async def test_protection_is_rechecked_before_publishing_existing_review(self) -> None:
        post = await self.add_post(status="REVIEW")
        self.workflow.source_monitor.protection_for_source.return_value = True

        self.assertEqual(await self.workflow.publish(post.id), "protected")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "PROTECTED")
        self.assertIsNone(saved.original_text)
        self.assertIsNone(saved.edited_text)
        self.bot.send_message.assert_not_awaited()

    async def test_confirmed_protection_keeps_media_referenced_by_another_post(self) -> None:
        media_path = self.create_media("shared-photo.jpg")
        post = await self.add_post(
            status="REVIEW", media_type="photo", media_path=str(media_path)
        )
        other = await self.add_post(
            status="NEW", media_type="photo", media_path=str(media_path)
        )
        self.workflow.source_monitor.protection_for_source.return_value = True

        self.assertEqual(await self.workflow.publish(post.id), "protected")

        saved = await self.saved_post(post.id)
        self.assertIsNone(saved.original_text)
        self.assertIsNone(saved.media_path)
        self.assertEqual((await self.saved_post(other.id)).media_path, str(media_path))
        self.assertTrue(media_path.is_file())

    async def test_terminal_cleanup_keeps_media_referenced_by_another_post(self) -> None:
        media_path = self.create_media("shared-terminal-photo.jpg")
        skipped = await self.add_post(status="REVIEW", media_type="photo", media_path=str(media_path))
        other = await self.add_post(status="NEW", media_type="photo", media_path=str(media_path))

        self.assertEqual(await self.workflow.skip(skipped.id), "skipped")

        self.assertTrue(media_path.is_file())
        self.assertEqual((await self.saved_post(other.id)).media_path, str(media_path))

    async def test_unavailable_protection_metadata_blocks_existing_review(self) -> None:
        post = await self.add_post(status="REVIEW")
        self.workflow.source_monitor.protection_for_source.return_value = None

        self.assertEqual(await self.workflow.publish(post.id), "protection_unverified")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "REVIEW")
        self.assertEqual(saved.edited_text, "Edited candidate")
        self.bot.send_message.assert_not_awaited()

        self.workflow.source_monitor.protection_for_source.return_value = False
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual((await self.saved_post(post.id)).status, "PUBLISHED")

    async def test_ambiguous_preview_failure_is_not_resent_on_next_poll(self) -> None:
        post = await self.add_post()
        self.bot.send_message.side_effect = RuntimeError("private chat not started")
        with self.assertLogs("postradar.bot.review", level="ERROR"):
            self.assertEqual(await self.workflow.deliver_new(), 0)
        self.assertEqual((await self.saved_post(post.id)).status, "REVIEW_DELIVERY_UNCERTAIN")
        self.assertEqual(await self.workflow.deliver_new(), 0)
        self.bot.send_message.assert_awaited_once()

    async def test_long_media_preview_keeps_full_text_and_attaches_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"photo")
            long_text = "Full preview text " * 100
            post = await self.add_post(
                media_type="photo",
                media_path=str(path),
                edited_text=long_text,
                original_text="Long preview test",
            )
            await self.workflow.deliver_new()

        self.assertNotIn("caption", self.bot.send_photo.await_args.kwargs)
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], long_text)
        self.assertIsNotNone(self.bot.send_message.await_args.kwargs["reply_markup"])
        self.assertEqual((await self.saved_post(post.id)).status, "REVIEW")

    async def test_empty_edited_text_uses_sanitized_candidate(self) -> None:
        post = await self.add_post(
            edited_text="  ", sanitized_text="Sanitized fallback", original_text="Keep original"
        )
        await self.workflow.deliver_new()
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], "Sanitized fallback")
        self.assertEqual((await self.saved_post(post.id)).original_text, "Keep original")

    async def test_text_publish_succeeds_and_marks_published(self) -> None:
        post = await self.add_post(status="REVIEW", media_type="text")
        result = await self.workflow.publish(post.id)

        self.assertEqual(result, "published")
        self.assertEqual((await self.saved_post(post.id)).status, "PUBLISHED")
        args = self.bot.send_message.await_args.kwargs
        self.assertEqual(args["chat_id"], -100999)
        self.assertEqual(args["text"], "Edited candidate")

    async def test_publish_uses_the_post_category_destination(self) -> None:
        async with self.factory() as session:
            source_default = Category(name="Source default", destination_channel_id=-100111)
            post_route = Category(name="Post route", destination_channel_id=-100222)
            session.add_all([source_default, post_route])
            await session.flush()
            source = await session.get(Source, self.source.id)
            source.category_id = source_default.id
            await session.commit()
            post_route_id = post_route.id
        post = await self.add_post(status="REVIEW", media_type="text")
        async with self.factory() as session:
            saved = await session.get(SourcePost, post.id)
            saved.category_id = post_route_id
            await session.commit()

        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], -100222)

    async def test_missing_category_destination_does_not_fall_back_to_legacy_route(self) -> None:
        async with self.factory() as session:
            category = Category(name="Unconfigured", enabled=True)
            session.add(category)
            await session.commit()
            category_id = category.id
        post = await self.add_post(status="REVIEW", media_type="text")
        async with self.factory() as session:
            saved = await session.get(SourcePost, post.id)
            saved.category_id = category_id
            await session.commit()

        self.assertEqual(await self.workflow.publish(post.id), "missing_destination")
        self.assertEqual((await self.saved_post(post.id)).status, "REVIEW")
        self.bot.send_message.assert_not_awaited()

    async def test_per_post_category_override_does_not_change_source_default(self) -> None:
        async with self.factory() as session:
            default = Category(name="Default", destination_channel_id=-100111)
            override = Category(name="Override", destination_channel_id=-100222)
            session.add_all([default, override])
            await session.flush()
            source = await session.get(Source, self.source.id)
            source.category_id = default.id
            session.add(
                SourcePost(
                    source_id=source.id,
                    telegram_message_id=self._message_id,
                    category_id=default.id,
                    status="REVIEW",
                    sanitized_text="text",
                )
            )
            await session.commit()
            post = await session.scalar(
                select(SourcePost).where(SourcePost.telegram_message_id == self._message_id)
            )
            source_id, post_id, override_id, default_id = source.id, post.id, override.id, default.id

        self.assertEqual(await self.workflow.set_post_category(post_id, override_id), "Override")
        async with self.factory() as session:
            saved_source = await session.get(Source, source_id)
            saved_post = await session.get(SourcePost, post_id)
        self.assertEqual(saved_source.category_id, default_id)
        self.assertEqual(saved_post.category_id, override_id)

    async def test_photo_video_and_document_publish_from_local_files(self) -> None:
        for media_type, bot_method in (
            ("photo", self.bot.send_photo),
            ("video", self.bot.send_video),
            ("document", self.bot.send_document),
        ):
            path = self.create_media(f"{media_type}.bin")
            post = await self.add_post(
                status="REVIEW", media_type=media_type, media_path=str(path),
                original_text=f"Original {media_type}",
            )
            self.assertEqual(await self.workflow.publish(post.id), "published")
            self.assertIsInstance(bot_method.await_args.kwargs[media_type], FSInputFile)
            self.assertEqual((await self.saved_post(post.id)).status, "PUBLISHED")

    async def test_album_preview_delivers_one_group_and_one_control_candidate(self) -> None:
        post, _paths = await self.add_album(status="NEW", item_count=3)

        self.assertEqual(await self.workflow.deliver_new(), 1)

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "REVIEW")
        self.bot.send_media_group.assert_awaited_once()
        self.assertEqual(len(self.bot.send_media_group.await_args.kwargs["media"]), 3)
        self.bot.send_message.assert_awaited_once()
        controls = self.bot.send_message.await_args.kwargs
        self.assertIn("Edited album caption", controls["text"])
        self.assertEqual(controls["reply_markup"].inline_keyboard[0][0].callback_data, f"publish:{post.id}")
        self.assertEqual(saved.admin_message_id, self.bot._next_id)
        self.assertEqual(json.loads(saved.admin_message_ids), [101, 102, 103, 104])

    async def test_protected_album_preview_deletes_every_admin_message(self) -> None:
        post, _paths = await self.add_album(status="NEW", item_count=3)
        self.assertEqual(await self.workflow.deliver_new(), 1)
        saved = await self.saved_post(post.id)
        message_ids = json.loads(saved.admin_message_ids)
        self.workflow.source_monitor.protection_for_source.return_value = True

        self.assertEqual(await self.workflow.publish(post.id), "protected")

        self.assertEqual(
            {call.kwargs["message_id"] for call in self.bot.delete_message.await_args_list},
            set(message_ids),
        )
        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "PROTECTED")
        self.assertIsNone(saved.admin_message_ids)
        self.assertIsNone(saved.admin_message_id)

    async def test_source_protection_event_removes_saved_album_preview_messages(self) -> None:
        post, _paths = await self.add_album(status="NEW", item_count=3)
        self.assertEqual(await self.workflow.deliver_new(), 1)
        saved = await self.saved_post(post.id)
        message_ids = json.loads(saved.admin_message_ids)

        await mark_post_protected(
            self.factory, self.source, post.telegram_message_id, self.media_dir,
            grouped_id=post.grouped_id, protection_known=True,
        )
        await self.workflow.deliver_new()

        self.assertEqual(
            {call.kwargs["message_id"] for call in self.bot.delete_message.await_args_list},
            set(message_ids),
        )

    async def test_failed_preview_candidates_do_not_starve_newer_candidates(self) -> None:
        first = await self.add_post(status="NEW", media_type="photo", media_path="/missing/1.jpg")
        second = await self.add_post(status="NEW", media_type="photo", media_path="/missing/2.jpg")
        healthy = await self.add_post(status="NEW", edited_text="Healthy candidate")
        cursor = [0]

        self.assertEqual(await deliver_new_posts(self.bot, self.factory, 12345, limit=2, cursor_state=cursor), 0)
        self.assertEqual(await deliver_new_posts(self.bot, self.factory, 12345, limit=2, cursor_state=cursor), 1)

        self.assertEqual((await self.saved_post(first.id)).status, "REVIEW_DELIVERY_UNCERTAIN")
        self.assertEqual((await self.saved_post(second.id)).status, "REVIEW_DELIVERY_UNCERTAIN")
        self.assertEqual((await self.saved_post(healthy.id)).status, "REVIEW")

    async def test_acknowledged_preview_survives_one_database_receipt_failure(self) -> None:
        post = await self.add_post(status="NEW")
        original_commit, calls = AsyncSession.commit, 0

        async def commit_once(session):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("receipt commit failed after Telegram acknowledgment")
            await original_commit(session)

        with patch.object(AsyncSession, "commit", new=commit_once):
            self.assertEqual(await self.workflow.deliver_new(), 1)

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "REVIEW")
        self.assertEqual(json.loads(saved.admin_message_ids), [101])
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_final_review_commit_failure_reconciles_control_ack_without_resend(self) -> None:
        post = await self.add_post(status="NEW")
        original_commit, calls = AsyncSession.commit, 0

        async def commit_once(session):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("review commit failed after Telegram acknowledgment")
            await original_commit(session)

        with patch.object(AsyncSession, "commit", new=commit_once):
            self.assertEqual(await self.workflow.deliver_new(), 0)
        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "REVIEW_DELIVERY_UNCERTAIN")
        self.assertEqual(json.loads(saved.admin_message_ids), [101])

        self.assertEqual(await self.workflow.deliver_new(), 0)
        self.assertEqual((await self.saved_post(post.id)).status, "REVIEW")
        self.assertEqual((await self.saved_post(post.id)).admin_message_id, 101)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_album_partial_preview_is_tracked_and_never_resent(self) -> None:
        post, _paths = await self.add_album(status="NEW", item_count=3)
        self.bot.send_message.side_effect = RuntimeError("control message timed out")
        with self.assertLogs("postradar.bot.review", level="ERROR"):
            self.assertEqual(await self.workflow.deliver_new(), 0)
        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "REVIEW_DELIVERY_UNCERTAIN")
        self.assertEqual(json.loads(saved.admin_message_ids), [101, 102, 103])

        self.bot.send_message.side_effect = None
        self.assertEqual(await self.workflow.deliver_new(), 0)
        self.assertEqual(self.bot.send_media_group.await_count, 1)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_album_publish_sends_one_ordered_group_and_cannot_repeat(self) -> None:
        post, paths = await self.add_album()

        self.assertEqual(await self.workflow.publish(post.id), "published")
        sent = self.bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0].caption, "Edited album caption")
        self.assertEqual([Path(item.media.path).name for item in sent], [path.name for path in paths])
        self.assertEqual((await self.saved_post(post.id)).status, "PUBLISHED")
        self.assertTrue(all(not path.exists() for path in paths))

        self.assertEqual(await self.workflow.publish(post.id), "PUBLISHED")
        self.bot.send_media_group.assert_awaited_once()

    async def test_document_album_publishes_as_one_document_media_group(self) -> None:
        post, paths = await self.add_album(item_count=2)
        async with self.factory() as session:
            saved = await session.get(SourcePost, post.id)
            for item in saved.media_items:
                item.media_type = "document"
            await session.commit()

        self.assertEqual(await self.workflow.publish(post.id), "published")
        media = self.bot.send_media_group.await_args.kwargs["media"]
        self.assertTrue(all(isinstance(item, InputMediaDocument) for item in media))
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_album_long_caption_is_sent_in_full_after_media_group(self) -> None:
        long_text = "Ж" * 1100
        post, _paths = await self.add_album(edited_text=long_text)

        self.assertEqual(await self.workflow.publish(post.id), "published")

        self.assertIsNone(self.bot.send_media_group.await_args.kwargs["media"][0].caption)
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], long_text)

    async def test_album_publish_failure_keeps_all_media_and_blocked_status(self) -> None:
        post, paths = await self.add_album()
        self.bot.send_media_group.side_effect = RuntimeError("temporary send failure")

        with self.assertLogs("postradar", level="ERROR"):
            self.assertEqual(await self.workflow.publish(post.id), "PUBLISH_UNCERTAIN")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "PUBLISH_UNCERTAIN")
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual([item.media_path for item in saved.media_items], [str(path) for path in paths])
        self.assertEqual(await self.workflow.publish(post.id), "PUBLISH_UNCERTAIN")

    async def test_skipping_album_cleans_every_media_file(self) -> None:
        post, paths = await self.add_album(item_count=4)

        self.assertEqual(await self.workflow.skip(post.id), "skipped")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "SKIPPED")
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(all(item.media_path is None for item in saved.media_items))

    async def test_editing_album_updates_only_edited_text(self) -> None:
        post, _paths = await self.add_album(admin_message_id=456)

        self.assertEqual(await self.workflow.save_edit(post.id, "Admin album edit"), "edited")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.edited_text, "Admin album edit")
        self.assertEqual(saved.original_text, "Original album caption")
        self.assertEqual(saved.sanitized_text, "Sanitized album caption")
        self.assertEqual(saved.status, "REVIEW")
        self.bot.send_media_group.assert_not_awaited()

    async def test_category_override_on_album_changes_only_album_post(self) -> None:
        async with self.factory() as session:
            source_default = Category(name="Album source default", destination_channel_id=-100101)
            override = Category(name="Album override", destination_channel_id=-100202)
            session.add_all([source_default, override])
            await session.flush()
            source = await session.get(Source, self.source.id)
            source.category_id = source_default.id
            await session.commit()
            default_id, override_id = source_default.id, override.id
        post, _paths = await self.add_album()
        async with self.factory() as session:
            saved = await session.get(SourcePost, post.id)
            saved.category_id = default_id
            await session.commit()

        self.assertEqual(await self.workflow.set_post_category(post.id, override_id), "Album override")
        async with self.factory() as session:
            source = await session.get(Source, self.source.id)
            saved = await session.get(SourcePost, post.id)
        self.assertEqual(source.category_id, default_id)
        self.assertEqual(saved.category_id, override_id)

    async def test_persisted_review_album_and_order_survive_database_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'restart.db'}"
            engine1 = create_async_engine(database_url)
            async with engine1.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory1 = async_sessionmaker(engine1, expire_on_commit=False)
            media_paths = []
            async with factory1() as session:
                source = Source(telegram_chat_id=-100744, destination_channel_id=-100999)
                session.add(source)
                await session.flush()
                post = SourcePost(
                    source_id=source.id,
                    telegram_message_id=200,
                    grouped_id=900200,
                    media_type="album",
                    status="REVIEW",
                    category_id=None,
                    edited_text="Saved album",
                    media_items=[],
                )
                for position in range(3):
                    media_path = Path(directory) / f"restart-{position}.jpg"
                    media_path.write_bytes(b"album")
                    media_paths.append(media_path)
                    post.media_items.append(
                        SourcePostMedia(
                            telegram_message_id=200 + position,
                            media_type="photo",
                            media_path=str(media_path),
                            position=position,
                        )
                    )
                session.add(post)
                await session.commit()
                post_id = post.id
            await engine1.dispose()

            engine2 = create_async_engine(database_url)
            factory2 = async_sessionmaker(engine2, expire_on_commit=False)
            bot = FakeBot()
            workflow = AdminWorkflow(
                bot, factory2, 12345, media_dir=directory,
                source_monitor=SimpleNamespace(protection_for_source=AsyncMock(return_value=False)),
            )
            status, saved, _source = await workflow.action_status(post_id)
            self.assertEqual(status, "REVIEW")
            self.assertEqual([item.position for item in saved.media_items], [0, 1, 2])
            self.assertTrue(all(Path(item.media_path).exists() for item in saved.media_items))
            await engine2.dispose()

    async def test_long_media_caption_is_sent_in_full_as_separate_text(self) -> None:
        path = self.create_media("long-caption.jpg")
        long_text = "Ж" * 1025
        self.assertFalse(fits_caption(long_text))
        post = await self.add_post(
            status="REVIEW",
            media_type="photo",
            media_path=str(path),
            edited_text=long_text,
        )
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertNotIn("caption", self.bot.send_photo.await_args.kwargs)
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], long_text)

    async def test_publish_failure_keeps_post_blocked(self) -> None:
        post = await self.add_post(status="REVIEW")
        self.bot.send_message.side_effect = RuntimeError("temporary error")
        with self.assertLogs("postradar", level="ERROR"):
            self.assertEqual(await self.workflow.publish(post.id), "PUBLISH_UNCERTAIN")
        self.assertEqual((await self.saved_post(post.id)).status, "PUBLISH_UNCERTAIN")
        self.assertEqual(await self.workflow.publish(post.id), "PUBLISH_UNCERTAIN")

    async def test_missing_destination_keeps_post_reviewable(self) -> None:
        post = await self.add_post(status="REVIEW")
        async with self.factory() as session:
            source = await session.get(Source, self.source.id)
            source.destination_channel_id = None
            await session.commit()
        self.assertEqual(await self.workflow.publish(post.id), "missing_destination")
        self.assertEqual((await self.saved_post(post.id)).status, "REVIEW")
        self.bot.send_message.assert_not_awaited()

    async def test_skip_and_stale_publish_are_safe(self) -> None:
        post = await self.add_post(status="REVIEW")
        self.assertEqual(await self.workflow.skip(post.id), "skipped")
        self.assertEqual(await self.workflow.publish(post.id), "SKIPPED")
        self.assertEqual((await self.saved_post(post.id)).status, "SKIPPED")
        self.bot.send_message.assert_not_awaited()

    async def test_publish_removes_local_media_and_clears_database_path(self) -> None:
        path = self.create_media("published.jpg")
        post = await self.add_post(status="REVIEW", media_type="photo", media_path=str(path))

        self.assertEqual(await self.workflow.publish(post.id), "published")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "PUBLISHED")
        self.assertFalse(path.exists())
        self.assertIsNone(saved.media_path)

    async def test_skip_removes_local_media_and_clears_database_path(self) -> None:
        path = self.create_media("skipped.mp4")
        post = await self.add_post(status="REVIEW", media_type="video", media_path=str(path))

        self.assertEqual(await self.workflow.skip(post.id), "skipped")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "SKIPPED")
        self.assertFalse(path.exists())
        self.assertIsNone(saved.media_path)

    async def test_publish_failure_keeps_media_and_blocked_status(self) -> None:
        path = self.create_media("failed-publish.jpg")
        post = await self.add_post(status="REVIEW", media_type="photo", media_path=str(path))
        self.bot.send_photo.side_effect = RuntimeError("temporary send failure")

        with self.assertLogs("postradar", level="ERROR"):
            self.assertEqual(await self.workflow.publish(post.id), "PUBLISH_UNCERTAIN")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "PUBLISH_UNCERTAIN")
        self.assertEqual(saved.media_path, str(path))
        self.assertTrue(path.exists())
        self.assertEqual(await self.workflow.publish(post.id), "PUBLISH_UNCERTAIN")

    async def test_nonterminal_posts_keep_media(self) -> None:
        new_path = self.create_media("new.jpg")
        new_post = await self.add_post(status="NEW", media_type="photo", media_path=str(new_path))
        review_path = self.create_media("review.jpg")
        review_post = await self.add_post(
            status="REVIEW", media_type="photo", media_path=str(review_path)
        )

        self.assertEqual(await self.workflow.publish(new_post.id), "NEW")
        self.assertEqual(await self.workflow.skip(new_post.id), "NEW")
        await self.workflow.action_status(review_post.id)
        self.assertEqual(await self.workflow.deliver_new(), 1)

        self.assertTrue(new_path.exists())
        self.assertTrue(review_path.exists())
        self.assertEqual((await self.saved_post(new_post.id)).status, "REVIEW")
        self.assertEqual((await self.saved_post(new_post.id)).media_path, str(new_path))
        self.assertEqual((await self.saved_post(review_post.id)).media_path, str(review_path))

    async def test_cleanup_refuses_paths_outside_configured_media_directory(self) -> None:
        outside_path = self.media_dir.parent / "outside.jpg"
        outside_path.write_bytes(b"keep")
        traversal_path = self.media_dir / ".." / "outside.jpg"
        post = await self.add_post(
            status="REVIEW", media_type="photo", media_path=str(traversal_path)
        )

        with self.assertLogs("postradar.bot.review", level="WARNING"):
            self.assertEqual(await self.workflow.publish(post.id), "published")

        saved = await self.saved_post(post.id)
        self.assertEqual(saved.status, "PUBLISHED")
        self.assertEqual(saved.media_path, str(traversal_path))
        self.assertTrue(outside_path.exists())

    async def test_cleanup_failure_does_not_revert_terminal_status(self) -> None:
        publish_path = self.create_media("cleanup-publish.jpg")
        publish_post = await self.add_post(
            status="REVIEW", media_type="photo", media_path=str(publish_path)
        )
        skip_path = self.create_media("cleanup-skip.jpg")
        skip_post = await self.add_post(
            status="REVIEW", media_type="photo", media_path=str(skip_path)
        )

        with patch(
            "postradar.bot.review.delete_media_file", side_effect=PermissionError("read only")
        ):
            with self.assertLogs("postradar.bot.review", level="WARNING"):
                self.assertEqual(await self.workflow.publish(publish_post.id), "published")
                self.assertEqual(await self.workflow.skip(skip_post.id), "skipped")

        self.assertEqual((await self.saved_post(publish_post.id)).status, "PUBLISHED")
        self.assertEqual((await self.saved_post(skip_post.id)).status, "SKIPPED")
        self.assertTrue(publish_path.exists())
        self.assertTrue(skip_path.exists())

    async def test_published_post_cannot_be_published_twice(self) -> None:
        post = await self.add_post(status="REVIEW")
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.bot.send_message.reset_mock()
        self.assertEqual(await self.workflow.publish(post.id), "PUBLISHED")
        self.bot.send_message.assert_not_awaited()

    async def test_edit_changes_only_edited_text_and_refreshes_review(self) -> None:
        post = await self.add_post(
            status="REVIEW", admin_message_id=456, original_text="Original edit test"
        )
        self.bot.edit_message_text.return_value = SimpleNamespace(message_id=456)
        result = await self.workflow.save_edit(post.id, "Admin replacement")
        saved = await self.saved_post(post.id)

        self.assertEqual(result, "edited")
        self.assertEqual(saved.edited_text, "Admin replacement")
        self.assertEqual(saved.original_text, "Original edit test")
        self.assertEqual(saved.sanitized_text, "Sanitized candidate")
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["text"], "Admin replacement")

    async def test_unauthorized_callback_cannot_publish(self) -> None:
        router = create_admin_router(self.workflow, 12345)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=54321),
            data="publish:1",
            answer=AsyncMock(),
        )
        state = AsyncMock()
        await router.callback_query.handlers[0].callback(callback, state)
        callback.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
        self.bot.send_message.assert_not_awaited()

    async def test_cancel_handler_clears_edit_state(self) -> None:
        router = create_admin_router(self.workflow, 12345)
        message = SimpleNamespace(from_user=SimpleNamespace(id=12345), answer=AsyncMock())
        state = AsyncMock()
        cancel = next(handler.callback for handler in router.message.handlers
                      if handler.callback.__name__ == "cancel_edit")
        await cancel(message, state)
        state.clear.assert_awaited_once()
        message.answer.assert_awaited_once_with("Изменение отменено.")

    async def test_admin_configuration_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "BOT_TOKEN.*ADMIN_USER_ID"):
            validate_admin_settings("", None)


if __name__ == "__main__":
    unittest.main()
