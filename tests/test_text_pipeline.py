"""Offline classification, fallback, media and review/publish regressions."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.genai.errors import ServerError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telethon import types

from postradar.bot.review import AdminWorkflow, candidate_html
from postradar.db.base import Base
from postradar.db.models import Category, Source, SourcePost
from postradar.services.ai_editor import AIEditor, ProcessingResult
from postradar.services.telegram_markup import plain_text, canonicalize, visible_length
from postradar.telegram.source_client import persist_message, persist_album

URL = "https://tickets.example/event?date=1&ref=2"
MARKUP = '<b>Билеты 500 ₽</b> <a href="https://tickets.example/event?date=1&amp;ref=2">здесь</a>'


def editor_with_response(response: object) -> tuple[AIEditor, AsyncMock]:
    generate = AsyncMock(return_value=SimpleNamespace(text=response))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    return AIEditor(api_key="test-key", client=client), generate


def response(content_type: str = "CONTENT", edited_html: str | None = MARKUP) -> str:
    return json.dumps({"content_type": content_type, "reason": "Short classification reason", "edited_html": edited_html})


class TextPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.factory() as session:
            category = Category(name="Выйти в Москву", destination_channel_id=-100222)
            session.add(category)
            await session.flush()
            self.source = Source(telegram_chat_id=-100111, title="Moscow source", username="moscow_source", category_id=category.id)
            session.add(self.source)
            await session.commit()
        self.bot = SimpleNamespace(**{name: AsyncMock(return_value=SimpleNamespace(message_id=100)) for name in (
            "send_message", "send_photo", "send_video", "send_document", "send_media_group",
            "edit_message_text", "edit_message_caption", "edit_message_reply_markup",
        )})
        async def media_group(**kwargs):
            return [SimpleNamespace(message_id=100 + i) for i, _item in enumerate(kwargs["media"])]
        self.bot.send_media_group.side_effect = media_group
        self.workflow = AdminWorkflow(
            self.bot, self.factory, 123, media_dir=self.directory.name,
            source_monitor=SimpleNamespace(protection_for_source=AsyncMock(return_value=False)),
        )
        self.client = SimpleNamespace(download_media=AsyncMock(side_effect=self.download))

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.directory.cleanup()

    async def download(self, message, file):
        Path(file).write_bytes(b"mock media")
        return file

    def message(self, identity: int = 1, media_type: str | None = None) -> SimpleNamespace:
        text = "Билеты 500 ₽ здесь"
        return SimpleNamespace(
            id=identity, message=text, date=None, media=object() if media_type else None,
            photo=object() if media_type == "photo" else None,
            video=object() if media_type == "video" else None,
            document=SimpleNamespace(mime_type="application/pdf") if media_type == "document" else None,
            file=SimpleNamespace(name=None, ext=".bin"),
            entities=[types.MessageEntityBold(0, 12), types.MessageEntityTextUrl(13, 5, URL)],
        )

    async def saved(self, identity: int = 1) -> SourcePost:
        async with self.factory() as session:
            return await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == identity))

    async def capture(self, editor: AIEditor, message=None) -> SourcePost:
        message = message or self.message()
        assert await persist_message(self.factory, self.source, message, self.client, self.directory.name, editor)
        return await self.saved(message.id)

    async def test_event_content_reaches_review_with_ticket_link_and_safe_context(self) -> None:
        editor, generate = editor_with_response(response())
        post = await self.capture(editor)
        self.assertEqual(post.content_type, "CONTENT")
        self.assertEqual(post.edited_html, MARKUP)
        self.assertEqual(post.original_text, self.message().message)
        self.assertEqual(post.sanitized_text, post.original_text)
        self.assertEqual(await self.workflow.deliver_new(), 1)
        sent = self.bot.send_message.await_args.kwargs
        self.assertEqual(sent["text"], MARKUP)
        self.assertEqual(sent["parse_mode"], "HTML")
        payload = json.loads(generate.await_args.kwargs["contents"])
        self.assertEqual(payload["category"], "Выйти в Москву")
        self.assertEqual(payload["source_username"], "moscow_source")
        self.assertIn('href=', payload["source_html"])
        self.assertEqual(set(payload), {"category", "source_title", "source_username", "source_html"})
        generate.assert_awaited_once()
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], MARKUP)

    async def test_ad_persisted_filtered_without_review_or_media_download(self) -> None:
        editor, _ = editor_with_response(response("AD", None))
        message = self.message(media_type="photo")
        message.message, message.entities = "#реклама О рекламодателе Промокод скидка!", []
        post = await self.capture(editor, message)
        self.assertEqual((post.status, post.content_type), ("FILTERED", "AD"))
        self.assertTrue(post.classification_reason)
        self.assertIsNone(post.media_path)
        self.client.download_media.assert_not_awaited()
        self.assertEqual(await self.workflow.deliver_new(), 0)
        self.assertEqual(await self.workflow.publish(post.id), "FILTERED")
        self.bot.send_message.assert_not_awaited()

    async def test_source_self_promotion_is_persisted_filtered(self) -> None:
        editor, _ = editor_with_response(response("SELF_PROMO", None))
        message = self.message()
        message.message, message.entities = "Посмотрите наше видео о чёрных дырах на YouTube и Boosty", []
        post = await self.capture(editor, message)
        self.assertEqual((post.status, post.content_type), ("FILTERED", "SELF_PROMO"))
        self.assertEqual(await self.workflow.deliver_new(), 0)

    async def test_uncertain_review_warning_never_published_as_content(self) -> None:
        editor, _ = editor_with_response(response("UNCERTAIN"))
        post = await self.capture(editor)
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.assertIn("⚠️ Классификация: требуется проверка", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], MARKUP)

    async def test_processing_validation_failures_preserve_source_and_deliver_review(self) -> None:
        for index, output in enumerate([
            "not json", response(edited_html="<b>unclosed"),
            response(edited_html='<a href="https://invented.example/">new</a>'),
            response(edited_html='<a href="javascript:alert(1)">bad</a>'),
            response(edited_html=" "),
            json.dumps({"content_type": "UNKNOWN", "reason": "Unknown", "edited_html": "text"}),
        ], start=1):
            with self.subTest(output=index):
                editor, _ = editor_with_response(output)
                post = await self.capture(editor, self.message(index))
                self.assertEqual(post.content_type, "UNCERTAIN")
                self.assertEqual(post.edited_html, post.source_html)
                self.assertIn("tickets.example", post.edited_html)
                self.assertEqual(await self.workflow.deliver_new(), 1)

    async def test_missing_key_and_provider_failure_use_source_markup(self) -> None:
        for identity, missing_key in enumerate((True, False), start=1):
            editor, generate = editor_with_response(response())
            if missing_key:
                editor = AIEditor(api_key="", enabled=True, client=editor._client)
            else:
                generate.side_effect = TimeoutError("offline")
            post = await self.capture(editor, self.message(identity))
            self.assertEqual(post.content_type, "UNCERTAIN")
            self.assertEqual(post.edited_html, post.source_html)
            self.assertEqual(generate.await_count, 0 if missing_key else 2)
            self.assertEqual(await self.workflow.deliver_new(), 1)

    async def test_primary_transient_failure_uses_structured_fallback(self) -> None:
        editor, generate = editor_with_response(response())
        generate.side_effect = [ServerError(503, {"error": {"code": 503, "message": "offline"}}), SimpleNamespace(text=response())]
        post = await self.capture(editor)
        self.assertEqual(post.content_type, "CONTENT")
        self.assertEqual([call.kwargs["model"] for call in generate.await_args_list], [editor.primary_model, editor.fallback_model])
        self.assertEqual(generate.await_args.kwargs["config"].response_mime_type, "application/json")

    async def test_formatted_photo_video_document_captions_review_and_publish(self) -> None:
        for identity, kind in enumerate(("photo", "video", "document"), start=1):
            editor, _ = editor_with_response(response())
            post = await self.capture(editor, self.message(identity, kind))
            self.assertEqual(await self.workflow.deliver_new(), 1)
            method = getattr(self.bot, f"send_{kind}")
            self.assertEqual(method.await_args.kwargs["caption"], MARKUP)
            self.assertEqual(method.await_args.kwargs["parse_mode"], "HTML")
            self.assertEqual(await self.workflow.publish(post.id), "published")
            self.assertEqual(method.await_args.kwargs["caption"], MARKUP)
            self.assertEqual(method.await_args.kwargs["parse_mode"], "HTML")

    async def test_album_hidden_link_one_ai_call_ordered_caption_and_publish(self) -> None:
        editor, generate = editor_with_response(response())
        messages = [self.message(2, "photo"), self.message(1, "photo")]
        messages[0].message, messages[0].entities = None, []
        assert await persist_album(self.factory, self.source, 99, messages, self.client, self.directory.name, editor)
        post = await self.saved()
        generate.assert_awaited_once()
        self.assertIn("tickets.example", post.source_html)
        self.assertEqual([item.telegram_message_id for item in post.media_items], [1, 2])
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.bot.send_media_group.assert_awaited_once()
        self.assertEqual(await self.workflow.publish(post.id), "published")
        sent = self.bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual(sent[0].caption, MARKUP)
        self.assertEqual(sent[0].parse_mode, "HTML")

    async def test_album_accepts_nested_formatting_escaped_text_and_existing_hidden_link(self) -> None:
        from html import escape
        from postradar.services.telegram_markup import normalize_message

        first = self.message(1, "photo")
        first.message = "Tickets here"
        first.entities = [
            types.MessageEntityBold(0, len(first.message)),
            types.MessageEntityItalic(8, 4),
            types.MessageEntityTextUrl(8, 4, "https://tickets.example/event?a=1&b=2"),
        ]
        second = self.message(2, "photo")
        second.message = "& details <today>"
        second.entities = [types.MessageEntityBold(0, len(second.message))]
        source_html = "\n\n".join(map(normalize_message, (first, second)))
        self.assertIn("<i><a href=", source_html)
        self.assertIn("</a></i></b>", source_html)
        self.assertIn(escape(second.message, quote=False), source_html)
        editor, generate = editor_with_response(response(edited_html=source_html))

        self.assertTrue(await persist_album(
            self.factory, self.source, 99, [second, first], self.client,
            self.directory.name, editor,
        ))

        post = await self.saved()
        self.assertEqual(post.content_type, "CONTENT")
        self.assertEqual(post.source_html, source_html)
        self.assertEqual(post.edited_html, source_html)
        self.assertEqual(len(post.media_items), 2)
        generate.assert_awaited_once()

    async def test_album_invalid_nested_gemini_markup_falls_back_to_source(self) -> None:
        first = self.message(1, "photo")
        second = self.message(2, "photo")
        source_html = self.message().message
        invalid_html = "<b><i>Tickets</b></i>"
        editor, generate = editor_with_response(response(edited_html=invalid_html))

        with self.assertLogs("postradar.services.ai_editor", level="WARNING") as captured:
            self.assertTrue(await persist_album(
                self.factory, self.source, 99, [second, first], self.client,
                self.directory.name, editor,
            ))

        post = await self.saved()
        self.assertEqual(post.content_type, "UNCERTAIN")
        self.assertEqual(post.edited_html, post.source_html)
        self.assertNotEqual(post.source_html, source_html)
        self.assertIn("MarkupError", "\n".join(captured.output))
        self.assertEqual(len(post.media_items), 2)
        generate.assert_awaited_once()

    async def test_ad_album_avoids_all_downloads(self) -> None:
        editor, generate = editor_with_response(response("AD", None))
        assert await persist_album(self.factory, self.source, 99, [self.message(1, "photo"), self.message(2, "photo")], self.client, self.directory.name, editor)
        post = await self.saved()
        self.assertEqual(post.status, "FILTERED")
        self.assertEqual(post.media_items, [])
        generate.assert_awaited_once()
        self.client.download_media.assert_not_awaited()
        self.assertEqual(await self.workflow.deliver_new(), 0)

    async def test_media_only_cannot_be_filtered_even_if_mock_ai_returns_ad(self) -> None:
        editor, generate = editor_with_response(response("AD", None))
        message = self.message(media_type="photo")
        message.message, message.entities = None, []
        post = await self.capture(editor, message)
        generate.assert_not_awaited()
        self.assertEqual((post.status, post.content_type), ("NEW", "UNCERTAIN"))
        self.assertTrue(Path(post.media_path).exists())
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.assertIn("требуется проверка", self.bot.send_photo.await_args.kwargs["caption"])

    async def test_manual_plain_edit_is_escaped_and_preserves_source_classification(self) -> None:
        editor, _ = editor_with_response(response())
        post = await self.capture(editor)
        await self.workflow.deliver_new()
        replacement = "2 < 3 & 4 > 1 <b>literal</b>"
        self.assertEqual(await self.workflow.save_edit(post.id, replacement), "edited")
        saved = await self.saved()
        self.assertEqual(saved.edited_text, replacement)
        self.assertEqual(plain_text(saved.edited_html), replacement)
        self.assertEqual(saved.source_html, post.source_html)
        self.assertEqual(saved.original_text, post.original_text)
        self.assertEqual(saved.content_type, post.content_type)
        self.assertEqual(saved.classification_reason, post.classification_reason)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], saved.edited_html)

    async def test_legacy_plain_row_previews_publishes_and_edits_safely(self) -> None:
        async with self.factory() as session:
            post = SourcePost(source_id=self.source.id, telegram_message_id=1, category_id=self.source.category_id, sanitized_text="Legacy <b> & text", status="NEW")
            session.add(post)
            await session.commit()
        self.assertEqual(candidate_html(post), "Legacy &lt;b&gt; &amp; text")
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.assertEqual(await self.workflow.save_edit(post.id, "Legacy > edit"), "edited")
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], "Legacy &gt; edit")

    async def test_long_formatted_media_review_edit_and_publish_are_balanced(self) -> None:
        content = "Событие & даты\n" * 800
        message = self.message(media_type="video")
        message.message = content
        message.entities = [types.MessageEntityBold(0, len(content))]
        from html import escape
        edited = "<b>" + escape(content) + "</b>"
        editor, _ = editor_with_response(response(edited_html=edited))
        post = await self.capture(editor, message)
        await self.workflow.deliver_new()
        chunks = [call.kwargs["text"] for call in self.bot.send_message.await_args_list]
        self.assertEqual("".join(plain_text(chunk) for chunk in chunks), content)
        self.assertTrue(all(visible_length(chunk) <= 4096 and canonicalize(chunk) == chunk for chunk in chunks))
        self.bot.send_message.reset_mock()
        self.assertEqual(await self.workflow.publish(post.id), "published")
        chunks = [call.kwargs["text"] for call in self.bot.send_message.await_args_list]
        self.assertEqual("".join(plain_text(chunk) for chunk in chunks), content)
        self.assertTrue(all(call.kwargs["parse_mode"] == "HTML" for call in self.bot.send_message.await_args_list))

    async def test_all_supported_formatting_survives_capture_review_and_publish(self) -> None:
        text = "bold ital unde stri spoi code quot pre!"
        message = self.message()
        message.message = text
        message.entities = [
            types.MessageEntityBold(0, 4), types.MessageEntityItalic(5, 4),
            types.MessageEntityUnderline(10, 4), types.MessageEntityStrike(15, 4),
            types.MessageEntitySpoiler(20, 4), types.MessageEntityCode(25, 4),
            types.MessageEntityBlockquote(30, 4), types.MessageEntityPre(35, 4, "python"),
        ]
        from postradar.services.telegram_markup import normalize_message
        markup = normalize_message(message)
        editor, _ = editor_with_response(response(edited_html=markup))
        post = await self.capture(editor, message)
        self.assertEqual(post.source_html, markup)
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], markup)
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], markup)
        self.assertEqual(self.bot.send_message.await_args.kwargs["parse_mode"], "HTML")

    async def test_distinct_album_captions_combine_markup_in_message_order_once(self) -> None:
        first, second = self.message(1, "photo"), self.message(2, "photo")
        second.message = "Вторая подпись"
        second.entities = [types.MessageEntityItalic(0, len(second.message))]
        from postradar.services.telegram_markup import normalize_message
        expected = normalize_message(first) + "\n\n" + normalize_message(second)
        processor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult("CONTENT", "Event", expected)))
        assert await persist_album(self.factory, self.source, 99, [second, first], self.client, self.directory.name, processor)
        post = await self.saved()
        self.assertEqual(post.original_text, first.message + "\n\n" + second.message)
        self.assertEqual(post.source_html, expected)
        processor.process.assert_awaited_once()
        self.assertEqual(processor.process.await_args.args[0], expected)

    async def test_long_manual_edit_refreshes_safe_preview(self) -> None:
        editor, _ = editor_with_response(response())
        post = await self.capture(editor)
        await self.workflow.deliver_new()
        replacement = "Long < > & text\n" * 1000
        self.bot.send_message.reset_mock()
        self.assertEqual(await self.workflow.save_edit(post.id, replacement), "edited")
        chunks = [call.kwargs["text"] for call in self.bot.send_message.await_args_list]
        self.assertEqual("".join(plain_text(chunk) for chunk in chunks), replacement)
        self.assertIsNotNone(self.bot.send_message.await_args.kwargs["reply_markup"])
        self.assertEqual((await self.saved()).edited_text, replacement)

    async def test_legacy_plain_row_can_publish_without_being_edited(self) -> None:
        async with self.factory() as session:
            post = SourcePost(source_id=self.source.id, telegram_message_id=1, category_id=self.source.category_id, edited_text="Legacy <b> & text", status="NEW")
            session.add(post)
            await session.commit()
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], "Legacy &lt;b&gt; &amp; text")
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual(self.bot.send_message.await_args.kwargs["text"], "Legacy &lt;b&gt; &amp; text")

    async def test_normalization_failure_text_post_survives_without_ai(self) -> None:
        from html import escape
        from unittest.mock import patch

        message = self.message()
        message.message = "  Original <text> & source\n\n"
        editor, generate = editor_with_response(response("AD", None))
        with patch("postradar.telegram.source_client.normalize_message", side_effect=ValueError("secret post body/token must not be logged")):
            with self.assertLogs("postradar.telegram.source_client", level="WARNING") as logs:
                post = await self.capture(editor, message)
        generate.assert_not_awaited()
        self.assertNotIn("secret post body/token", " ".join(logs.output))
        self.assertIn("source_id=", " ".join(logs.output))
        self.assertEqual(post.original_text, message.message)
        self.assertEqual(post.sanitized_text, message.message)
        self.assertEqual(post.edited_text, message.message)
        self.assertEqual(post.source_html, escape(message.message, quote=False))
        self.assertEqual(post.edited_html, post.source_html)
        self.assertEqual((post.status, post.content_type), ("NEW", "UNCERTAIN"))
        self.assertEqual(await self.workflow.deliver_new(), 1)
        sent = self.bot.send_message.await_args.kwargs
        self.assertIn("требуется проверка", sent["text"])
        self.assertIn(post.edited_html, sent["text"])
        self.client.download_media.assert_not_awaited()

    async def test_normalization_failure_media_post_preserves_download_and_review(self) -> None:
        from unittest.mock import patch

        message = self.message(media_type="photo")
        editor, generate = editor_with_response(response("AD", None))
        with patch("postradar.telegram.source_client.normalize_message", side_effect=ValueError("bad entities")):
            post = await self.capture(editor, message)
        generate.assert_not_awaited()
        self.assertEqual((post.status, post.content_type), ("NEW", "UNCERTAIN"))
        self.assertEqual(post.original_text, message.message)
        self.assertEqual(plain_text(post.edited_html), message.message)
        self.client.download_media.assert_awaited_once()
        self.assertTrue(Path(post.media_path).is_file())
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.assertIn("требуется проверка", self.bot.send_photo.await_args.kwargs["caption"])
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.assertEqual(self.bot.send_photo.await_args.kwargs["caption"], post.edited_html)
        self.assertFalse(Path(post.media_path).exists())

    async def test_normalization_failure_album_survives_and_skips_ai_for_whole_album(self) -> None:
        from html import escape
        from unittest.mock import patch
        from postradar.services.telegram_markup import normalize_message

        first, second = self.message(1, "photo"), self.message(2, "video")
        second.message = "Second <caption> & details"
        editor, generate = editor_with_response(response("AD", None))

        def normalize_or_fail(message):
            if message.id == 2:
                raise ValueError("bad album entities")
            return normalize_message(message)

        with patch("postradar.telegram.source_client.normalize_message", side_effect=normalize_or_fail):
            assert await persist_album(self.factory, self.source, 99, [second, first], self.client, self.directory.name, editor)
        post = await self.saved()
        generate.assert_not_awaited()
        self.assertEqual((post.status, post.content_type), ("NEW", "UNCERTAIN"))
        self.assertEqual(post.original_text, first.message + "\n\n" + second.message)
        self.assertEqual(post.source_html, normalize_message(first) + "\n\n" + escape(second.message, quote=False))
        self.assertEqual(post.edited_html, post.source_html)
        self.assertEqual(self.client.download_media.await_count, 2)
        self.assertEqual([item.telegram_message_id for item in post.media_items], [1, 2])
        self.assertTrue(all(Path(item.media_path).is_file() for item in post.media_items))
        self.assertEqual(await self.workflow.deliver_new(), 1)
        self.bot.send_media_group.assert_awaited_once()
        self.assertIn("требуется проверка", self.bot.send_message.await_args.kwargs["text"])

    async def _capture_caption_album(self, messages: list[SimpleNamespace]) -> SourcePost:
        from postradar.services.telegram_markup import normalize_message

        markup = "\n\n".join(dict.fromkeys(normalize_message(message) for message in sorted(messages, key=lambda item: item.id)))
        processor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult("CONTENT", "Event", markup)))
        assert await persist_album(self.factory, self.source, 99, messages, self.client, self.directory.name, processor)
        processor.process.assert_awaited_once()
        self.assertEqual(processor.process.await_args.args[0], markup)
        post = await self.saved()
        self.assertEqual(post.source_html, markup)
        return post

    async def test_identical_visible_album_captions_preserve_different_hidden_hrefs(self) -> None:
        first, second = self.message(1, "photo"), self.message(2, "photo")
        first.message = second.message = "Билеты здесь"
        first.entities = [types.MessageEntityTextUrl(7, 5, "https://example.test/a")]
        second.entities = [types.MessageEntityTextUrl(7, 5, "https://example.test/b")]
        post = await self._capture_caption_album([second, first])
        self.assertEqual(post.original_text, "Билеты здесь\n\nБилеты здесь")
        self.assertIn('href="https://example.test/a"', post.source_html)
        self.assertIn('href="https://example.test/b"', post.source_html)
        self.assertLess(post.source_html.index("/a"), post.source_html.index("/b"))
        self.assertEqual(await self.workflow.deliver_new(), 1)

    async def test_identical_visible_album_captions_preserve_different_formatting(self) -> None:
        first, second = self.message(1, "photo"), self.message(2, "photo")
        first.message = second.message = "Билеты здесь"
        first.entities = [types.MessageEntityBold(0, 12)]
        second.entities = [types.MessageEntityItalic(0, 12)]
        post = await self._capture_caption_album([second, first])
        self.assertEqual(post.source_html, "<b>Билеты здесь</b>\n\n<i>Билеты здесь</i>")
        self.assertEqual(post.original_text, "Билеты здесь\n\nБилеты здесь")

    async def test_exact_duplicate_album_text_and_entities_are_included_once(self) -> None:
        first, second = self.message(1, "photo"), self.message(2, "photo")
        post = await self._capture_caption_album([second, first])
        self.assertEqual(post.original_text, first.message)
        self.assertEqual(post.source_html.count("tickets.example"), 1)
        self.assertEqual(len(post.media_items), 2)

    async def test_long_edited_photo_keeps_existing_media_and_moves_controls(self) -> None:
        editor, _ = editor_with_response(response())
        post = await self.capture(editor, self.message(media_type="photo"))
        await self.workflow.deliver_new()
        original_control_id = (await self.saved()).admin_message_id
        self.bot.send_photo.reset_mock()
        self.bot.send_message.reset_mock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=200)
        replacement = "Long edited <photo> & text\n" * 500
        self.assertEqual(await self.workflow.save_edit(post.id, replacement), "edited")
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_media_group.assert_not_awaited()
        self.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=123, message_id=original_control_id, reply_markup=None)
        self.bot.edit_message_caption.assert_awaited_once_with(chat_id=123, message_id=original_control_id, caption=None, reply_markup=None)
        calls = self.bot.send_message.await_args_list
        self.assertGreater(len(calls), 1)
        self.assertEqual("".join(plain_text(call.kwargs["text"]) for call in calls), replacement)
        self.assertTrue(all("reply_markup" not in call.kwargs for call in calls[:-1]))
        self.assertIsNotNone(calls[-1].kwargs["reply_markup"])
        self.assertEqual((await self.saved()).admin_message_id, 200)
        self.assertTrue(Path(post.media_path).is_file())
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.bot.send_photo.assert_awaited_once()

    async def test_long_edited_album_keeps_existing_group_and_moves_controls(self) -> None:
        editor, _ = editor_with_response(response())
        assert await persist_album(self.factory, self.source, 99, [self.message(1, "photo"), self.message(2, "photo")], self.client, self.directory.name, editor)
        post = await self.saved()
        await self.workflow.deliver_new()
        original_control_id = (await self.saved()).admin_message_id
        self.bot.send_media_group.reset_mock()
        self.bot.send_message.reset_mock()
        self.bot.send_message.return_value = SimpleNamespace(message_id=200)
        replacement = "Long edited <album> & text\n" * 500
        self.assertEqual(await self.workflow.save_edit(post.id, replacement), "edited")
        self.bot.send_media_group.assert_not_awaited()
        self.bot.send_photo.assert_not_awaited()
        self.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=123, message_id=original_control_id, reply_markup=None)
        calls = self.bot.send_message.await_args_list
        self.assertGreater(len(calls), 1)
        self.assertEqual("".join(plain_text(call.kwargs["text"]) for call in calls), replacement)
        self.assertTrue(all("reply_markup" not in call.kwargs for call in calls[:-1]))
        self.assertIsNotNone(calls[-1].kwargs["reply_markup"])
        self.assertEqual((await self.saved()).admin_message_id, 200)
        self.assertTrue(all(Path(item.media_path).is_file() for item in post.media_items))
        self.assertEqual(await self.workflow.publish(post.id), "published")
        self.bot.send_media_group.assert_awaited_once()
