"""Database-backed admin review delivery and post actions."""

import asyncio
import json
import logging
import re
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import joinedload, selectinload

from postradar.db.models import Category, Source, SourcePost, SourcePostMedia
from postradar.bot.keyboards import review_keyboard
from postradar.services.media import delete_media_file
from postradar.services.publication import (
    PublicationJournal, PublicationPart, PublicationPreflightError, REQUEST_TIMEOUT, fingerprint,
)
from postradar.services.telegram_markup import canonicalize, split_html, visible_length

logger = logging.getLogger(__name__)
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
PREVIEW_PENDING = "REVIEW_DELIVERY_PENDING"
PREVIEW_UNCERTAIN = "REVIEW_DELIVERY_UNCERTAIN"


def _admin_message_ids(post: SourcePost) -> list[int]:
    ids: list[int] = []
    if post.admin_message_id is not None:
        ids.append(post.admin_message_id)
    try:
        stored = json.loads(post.admin_message_ids or "[]")
    except (TypeError, ValueError):
        stored = []
    if isinstance(stored, list):
        ids.extend(value for value in stored if type(value) is int and value > 0)
    return list(dict.fromkeys(ids))


async def _notify_sent(
    callback: Callable[[Sequence[Message], int | None], Awaitable[None]] | None,
    messages: Sequence[Message],
    control_message_id: int | None = None,
) -> None:
    if callback is not None and messages:
        await callback(messages, control_message_id)


def candidate_text(post: SourcePost) -> str | None:
    """Return the current publish candidate, excluding original source text."""
    for value in (post.edited_text, post.sanitized_text):
        if isinstance(value, str) and value.strip():
            return value
    return None


def candidate_html(post: SourcePost) -> str | None:
    """Use validated markup; escape legacy plain text rather than interpreting it."""
    if post.edited_html is not None:
        return canonicalize(post.edited_html) if post.edited_html else None
    text = candidate_text(post)
    return escape(text, quote=False) if text else None


def review_html(post: SourcePost) -> str | None:
    content = candidate_html(post)
    if post.content_type == "UNCERTAIN":
        warning = "⚠️ Классификация: требуется проверка"
        return f"{warning}\n\n{content}" if content else warning
    return content


def fits_caption(text: str) -> bool:
    """Check Telegram's caption limit in UTF-16 code units."""
    return visible_length(text) <= CAPTION_LIMIT


def _text_chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    return split_html(text, limit)


async def _send_text_in_chunks(
    bot: Bot, chat_id: int, text: str, *, reply_markup: Any = None,
    on_sent: Callable[[Sequence[Message], int | None], Awaitable[None]] | None = None,
) -> Message:
    chunks = _text_chunks(text)
    if not chunks:
        raise ValueError("No visible text to send")
    for index, chunk in enumerate(chunks):
        message = await bot.send_message(
            chat_id=chat_id, text=chunk, parse_mode="HTML",
            **({"reply_markup": reply_markup} if reply_markup is not None and index == len(chunks) - 1 else {}),
        )
        await _notify_sent(
            on_sent, [message],
            message.message_id if reply_markup is not None and index == len(chunks) - 1 else None,
        )
    return message


def _media_group(
    items: list[SourcePostMedia], *, caption: str | None = None
) -> list[Any]:
    """Build Telegram media-group entries in persisted album order."""
    media = []
    for index, item in enumerate(sorted(items, key=lambda entry: entry.position)):
        upload = FSInputFile(item.media_path)
        item_caption = caption if index == 0 else None
        if item.media_type == "photo":
            media.append(InputMediaPhoto(media=upload, caption=item_caption, parse_mode="HTML"))
        elif item.media_type == "video":
            media.append(InputMediaVideo(media=upload, caption=item_caption, parse_mode="HTML"))
        elif item.media_type == "document":
            media.append(InputMediaDocument(media=upload, caption=item_caption, parse_mode="HTML"))
    return media


async def _send_one_media(
    bot: Bot,
    chat_id: int,
    item: SourcePostMedia,
    *,
    caption: str | None = None,
) -> Message:
    """Send a single surviving album item when Telegram cannot form a group."""
    send_media = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "document": bot.send_document,
    }[item.media_type]
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        item.media_type: FSInputFile(item.media_path),
    }
    if caption:
        kwargs["caption"] = caption
        kwargs["parse_mode"] = "HTML"
    return await send_media(**kwargs)


async def send_review_preview(
    bot: Bot, admin_id: int, post: SourcePost, *,
    on_sent: Callable[[Sequence[Message], int | None], Awaitable[None]] | None = None,
) -> Message:
    """Send a text or local-media preview, placing controls on the review text."""
    text = review_html(post)
    category_name = post.category.name if post.category is not None else None
    keyboard = review_keyboard(post.id, category_name)
    if post.media_type == "album":
        media_items = [item for item in post.media_items if item.media_path]
        if not media_items:
            if text:
                return await _send_text_in_chunks(
                    bot, chat_id=admin_id,
                    text=text,
                    reply_markup=keyboard,
                    on_sent=on_sent,
                )
            raise ValueError(f"SourcePost {post.id} has neither downloaded album media nor candidate text")
        for item in media_items:
            if not Path(item.media_path).is_file():
                raise FileNotFoundError(f"Stored album media file is missing: {item.media_path}")
        if len(media_items) >= 2:
            sent = await bot.send_media_group(
                chat_id=admin_id,
                media=_media_group(media_items),
            )
            await _notify_sent(on_sent, sent)
        else:
            sent = await _send_one_media(bot, admin_id, media_items[0])
            await _notify_sent(on_sent, [sent])
        context = f"Альбом · файлов: {len(media_items)}"
        if text:
            details = f"{text}\n\n{context}"
            if visible_length(details) > MESSAGE_LIMIT:
                await _send_text_in_chunks(bot, admin_id, text, on_sent=on_sent)
                details = context
        else:
            details = f"Текст публикации отсутствует.\n\n{context}"
        control_message = await _send_text_in_chunks(
            bot,
            chat_id=admin_id,
            text=details,
            reply_markup=keyboard,
            on_sent=on_sent,
        )
        logger.info("Album review delivered: source_post_id=%s items=%s", post.id, len(media_items))
        return control_message

    if post.media_type in {"photo", "video", "document"} and post.media_path:
        path = Path(post.media_path)
        if not path.is_file():
            raise FileNotFoundError(f"Stored media file is missing: {path}")
        upload = FSInputFile(path)
        if post.media_type == "photo":
            send_media = bot.send_photo
        elif post.media_type == "video":
            send_media = bot.send_video
        else:
            send_media = bot.send_document

        if text and fits_caption(text):
            sent = await send_media(
                chat_id=admin_id,
                **{post.media_type: upload},
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            await _notify_sent(on_sent, [sent], sent.message_id)
            return sent

        media_message = await send_media(
            chat_id=admin_id,
            **{post.media_type: upload},
            reply_markup=keyboard if not text else None,
        )
        await _notify_sent(on_sent, [media_message], media_message.message_id if not text else None)
        if text:
            return await _send_text_in_chunks(
                bot, chat_id=admin_id,
                text=text,
                reply_markup=keyboard,
                on_sent=on_sent,
            )
        return media_message

    if text:
        return await _send_text_in_chunks(
            bot, chat_id=admin_id,
            text=text,
            reply_markup=keyboard,
            on_sent=on_sent,
        )
    raise ValueError(f"SourcePost {post.id} has neither a media file nor candidate text")


async def deliver_new_posts(
    bot: Bot,
    session_factory: async_sessionmaker,
    admin_id: int,
    *,
    limit: int = 20,
    protection_check: Any | None = None,
    protection_block: Any | None = None,
    cursor_state: list[int] | None = None,
) -> int:
    """Deliver a bounded batch of NEW posts and mark successful previews REVIEW."""
    async with session_factory() as session:
        query = (
            select(SourcePost)
            .options(selectinload(SourcePost.media_items), joinedload(SourcePost.source))
            .where(SourcePost.status == "NEW")
            .order_by(SourcePost.id)
            .limit(limit)
        )
        if cursor_state is not None and cursor_state[0] > 0:
            query = query.where(SourcePost.id > cursor_state[0])
        posts = list(
            (
                await session.scalars(query)
            ).all()
        )
        if not posts and cursor_state is not None and cursor_state[0] > 0:
            # Wrap in this poll, not the next one, so a sole uncertain candidate
            # gets its next protection check on the next delivery cycle.
            cursor_state[0] = 0
            posts = list((await session.scalars(
                select(SourcePost)
                .options(selectinload(SourcePost.media_items), joinedload(SourcePost.source))
                .where(SourcePost.status == "NEW")
                .order_by(SourcePost.id)
                .limit(limit)
            )).all())
        if cursor_state is not None:
            if posts:
                cursor_state[0] = posts[-1].id

    delivered = 0
    for post in posts:
        acknowledged_ids: list[int] = []
        delivery_claimed = False
        try:
            if protection_check is not None:
                source_message_ids = [post.telegram_message_id]
                source_message_ids.extend(item.telegram_message_id for item in post.media_items)
                protection = await protection_check(
                    post.source.telegram_chat_id, source_message_ids
                )
                if protection is not False:
                    if protection_block is not None:
                        await protection_block(post.id, protection is True)
                    continue
            async with session_factory() as session:
                changed = await session.execute(update(SourcePost).where(
                    SourcePost.id == post.id, SourcePost.status == "NEW",
                ).values(status=PREVIEW_PENDING, admin_message_id=None, admin_message_ids=None))
                if changed.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                delivery_claimed = True

            async def record_messages(
                messages: Sequence[Message], control_message_id: int | None,
            ) -> None:
                acknowledged_ids.extend(
                    identity for identity in (getattr(item, "message_id", None) for item in messages)
                    if type(identity) is int and identity > 0 and identity not in acknowledged_ids
                )
                await _persist_preview_ids(
                    session_factory, post.id, acknowledged_ids, control_message_id,
                )

            message = await send_review_preview(bot, admin_id, post, on_sent=record_messages)
            async with session_factory() as session:
                current = await session.get(SourcePost, post.id)
                if current is None or current.status != PREVIEW_PENDING:
                    continue
                current.admin_message_id = message.message_id
                current.status = "REVIEW"
                await session.commit()
            delivered += 1
            logger.info("Review candidate delivered: source_post_id=%s", post.id)
        except Exception as error:
            if delivery_claimed and acknowledged_ids:
                try:
                    await _persist_preview_ids(session_factory, post.id, acknowledged_ids)
                except Exception as receipt_error:
                    logger.error("Admin preview receipt remains uncertain: source_post_id=%s exception=%s",
                                 post.id, type(receipt_error).__name__)
            try:
                async with session_factory() as session:
                    await session.execute(update(SourcePost).where(
                        SourcePost.id == post.id, SourcePost.status == PREVIEW_PENDING,
                    ).values(status=PREVIEW_UNCERTAIN))
                    await session.commit()
            except Exception as state_error:
                logger.error("Admin preview state remains pending: source_post_id=%s exception=%s",
                             post.id, type(state_error).__name__)
            logger.error(
                "Admin preview delivery failed: source_post_id=%s exception=%s error=%s",
                post.id,
                type(error).__name__,
                _safe_bot_error(error, bot),
            )
    return delivered


async def _persist_preview_ids(
    session_factory: async_sessionmaker, post_id: int, message_ids: Sequence[int],
    control_message_id: int | None = None,
) -> None:
    """Persist each acknowledged admin message before sending the next part."""
    for attempt in range(2):
        try:
            async with session_factory() as session:
                post = await session.get(SourcePost, post_id)
                if post is None or post.status not in {
                    PREVIEW_PENDING, PREVIEW_UNCERTAIN, "REVIEW", "PROTECTED",
                }:
                    return
                merged = list(dict.fromkeys(_admin_message_ids(post) + list(message_ids)))
                post.admin_message_ids = json.dumps(merged)
                if control_message_id is not None:
                    post.admin_message_id = control_message_id
                await session.commit()
            return
        except Exception:
            if attempt:
                raise


class AdminWorkflow:
    """Coordinate admin actions with persisted post state."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker,
        admin_id: int,
        media_dir: str | Path = "./data/media",
        source_monitor: Any | None = None,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.admin_id = admin_id
        self.media_dir = Path(media_dir)
        self.source_monitor = source_monitor
        self._publish_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._delivery_cursor = [0]
        self._preview_recovery_cursor = [0]
        self.publication = PublicationJournal(session_factory)

    async def deliver_new(self) -> int:
        async with self._delivery_lock:
            await self.publication.recover_expired()
            await self._reconcile_review_deliveries()
            recover = getattr(self.source_monitor, "recover_pending_captures", None)
            if recover is not None:
                await recover(limit=10)
            return await deliver_new_posts(
                self.bot,
                self.session_factory,
                self.admin_id,
                protection_check=self._source_protection,
                protection_block=self._block_for_protection,
                cursor_state=self._delivery_cursor,
            )

    async def _reconcile_review_deliveries(self, *, limit: int = 20) -> None:
        pending_states = (PREVIEW_PENDING, PREVIEW_UNCERTAIN)
        conditions = (
            SourcePost.status.in_(pending_states)
            | ((SourcePost.status == "PROTECTED") & (
                SourcePost.admin_message_ids.is_not(None) | SourcePost.admin_message_id.is_not(None)
            ))
        )
        async with self.session_factory() as session:
            query = select(
                SourcePost.id, SourcePost.status, SourcePost.admin_message_id, Source.telegram_chat_id,
            ).join(
                Source, Source.id == SourcePost.source_id,
            ).where(conditions).order_by(SourcePost.id).limit(limit)
            rows = list((await session.execute(query.where(
                SourcePost.id > self._preview_recovery_cursor[0],
            ))).all())
            if not rows and self._preview_recovery_cursor[0]:
                self._preview_recovery_cursor[0] = 0
                rows = list((await session.execute(query)).all())
        for post_id, status, control_message_id, chat_id in rows:
            self._preview_recovery_cursor[0] = post_id
            try:
                if status == "PROTECTED":
                    await self._delete_tracked_preview_messages(post_id)
                    continue
                protection = await self._source_protection(
                    chat_id, await self._preview_source_message_ids(post_id),
                )
                if protection is True:
                    await self._block_for_protection(post_id, True)
                elif protection is False and control_message_id is not None:
                    async with self.session_factory() as session:
                        await session.execute(update(SourcePost).where(
                            SourcePost.id == post_id,
                            SourcePost.status.in_(pending_states),
                        ).values(status="REVIEW"))
                        await session.commit()
                elif status == PREVIEW_PENDING:
                    async with self.session_factory() as session:
                        await session.execute(update(SourcePost).where(
                            SourcePost.id == post_id, SourcePost.status == PREVIEW_PENDING,
                        ).values(status=PREVIEW_UNCERTAIN))
                        await session.commit()
            except Exception as error:
                logger.warning("Review delivery reconciliation deferred: source_post_id=%s exception=%s",
                               post_id, type(error).__name__)

    async def _preview_source_message_ids(self, post_id: int) -> list[int]:
        async with self.session_factory() as session:
            post = await session.get(SourcePost, post_id)
            if post is None:
                return []
            return [post.telegram_message_id, *(item.telegram_message_id for item in post.media_items)]

    async def _delete_tracked_preview_messages(self, post_id: int) -> None:
        async with self.session_factory() as session:
            post = await session.get(SourcePost, post_id)
            if post is None:
                return
            message_ids = _admin_message_ids(post)
            admin_message_id = post.admin_message_id
        remaining: list[int] = []
        for identity in message_ids:
            try:
                await self.bot.delete_message(chat_id=self.admin_id, message_id=identity)
            except Exception:
                remaining.append(identity)
        async with self.session_factory() as session:
            post = await session.get(SourcePost, post_id)
            if post is None:
                return
            post.admin_message_ids = json.dumps(remaining) if remaining else None
            post.admin_message_id = (
                admin_message_id if admin_message_id in remaining
                else remaining[-1] if remaining else None
            )
            await session.commit()

    async def _track_review_preview_messages(
        self, post_id: int, messages: Sequence[Message], control_message_id: int | None,
    ) -> None:
        ids = [identity for identity in (getattr(item, "message_id", None) for item in messages)
               if type(identity) is int and identity > 0]
        if ids:
            await _persist_preview_ids(self.session_factory, post_id, ids, control_message_id)

    async def _source_protection(
        self, telegram_chat_id: int, message_ids: list[int]
    ) -> bool | None:
        if self.source_monitor is None:
            return None
        return await self.source_monitor.protection_for_source(telegram_chat_id, message_ids)

    async def _block_for_protection(self, post_id: int, protection_known: bool) -> None:
        if not protection_known:
            # Unknown remains fail-closed at the caller, while the candidate and
            # its review state remain available for a later metadata recheck.
            return
        status = "PROTECTED"
        paths: list[str] = []
        async with self.session_factory() as session:
            post = await session.get(SourcePost, post_id)
            if post is None or post.status not in {"NEW", "REVIEW", PREVIEW_PENDING, PREVIEW_UNCERTAIN, "PROTECTED"}:
                return
            post.status = status
            post.classification_reason = (
                "Telegram content protection is enabled; content was blocked."
                if protection_known
                else "Telegram protection metadata was unavailable; content was blocked."
            )
            if post.media_path:
                paths.append(post.media_path)
            paths.extend(item.media_path for item in post.media_items if item.media_path)
            post.original_text = None
            post.sanitized_text = None
            post.edited_text = None
            post.source_html = None
            post.edited_html = None
            post.media_path = None
            for item in post.media_items:
                item.media_path = None
            await session.commit()
        await self._delete_tracked_preview_messages(post_id)
        for path in set(paths):
            try:
                async with self.session_factory() as session:
                    referenced = await session.scalar(
                        select(SourcePost.id).where(SourcePost.media_path == path).limit(1)
                    )
                    if referenced is None:
                        referenced = await session.scalar(
                            select(SourcePostMedia.id)
                            .where(SourcePostMedia.media_path == path)
                            .limit(1)
                        )
                if referenced is None:
                    delete_media_file(self.media_dir, path)
            except Exception:
                logger.warning("Could not remove protected local media: source_post_id=%s", post_id)

    async def action_status(self, post_id: int) -> tuple[str | None, SourcePost | None, Source | None]:
        async with self.session_factory() as session:
            post = await session.get(SourcePost, post_id)
            if post is None:
                return "missing", None, None
            source = await session.get(Source, post.source_id)
            return post.status, post, source

    async def skip(self, post_id: int) -> str:
        transitioned = False
        async with self._publish_lock:
            async with self.session_factory() as session:
                post = await session.get(SourcePost, post_id)
                if post is None:
                    return "missing"
                if post.status == "SKIPPED":
                    pass
                elif post.status != "REVIEW":
                    return post.status
                else:
                    changed = await session.execute(update(SourcePost).where(
                        SourcePost.id == post_id, SourcePost.status == "REVIEW",
                    ).values(status="SKIPPED"))
                    if changed.rowcount != 1:
                        await session.refresh(post)
                        return post.status
                    await session.commit()
                    transitioned = True
        await self._cleanup_terminal_media(post_id, "SKIPPED")
        if transitioned:
            logger.info("Review candidate skipped: source_post_id=%s", post_id)
        return "skipped"

    async def save_edit(self, post_id: int, replacement: str) -> str:
        if not replacement.strip():
            return "empty"
        async with self._publish_lock:
            async with self.session_factory() as session:
                post = await session.get(SourcePost, post_id)
                if post is None:
                    return "missing"
                if post.status != "REVIEW":
                    return post.status
                changed = await session.execute(update(SourcePost).where(
                    SourcePost.id == post_id, SourcePost.status == "REVIEW",
                ).values(edited_text=replacement, edited_html=escape(replacement, quote=False)))
                if changed.rowcount != 1:
                    await session.refresh(post)
                    return post.status
                await session.commit()
            try:
                message = await self._refresh_preview(post)
            except Exception as error:
                logger.error(
                    "Could not refresh edited review preview: source_post_id=%s exception=%s error=%s",
                    post_id,
                    type(error).__name__,
                    _safe_bot_error(error, self.bot),
                )
                return "preview_failed"
            async with self.session_factory() as session:
                current = await session.get(SourcePost, post_id)
                if current is not None and current.status == "REVIEW":
                    current.admin_message_id = message.message_id
                    await session.commit()
        logger.info("Review candidate edited: source_post_id=%s", post_id)
        return "edited"

    async def _refresh_preview(self, post: SourcePost) -> Message:
        """Update the existing review message when possible, else send a new preview."""
        text = review_html(post)
        if text and visible_length(text) > MESSAGE_LIMIT:
            if post.admin_message_id is None:
                return await send_review_preview(
                    self.bot, self.admin_id, post,
                    on_sent=lambda messages, control_id: self._track_review_preview_messages(post.id, messages, control_id),
                )
            # The stored control message may be a media caption or separate text.
            # Retain existing media; clearing a caption on a text message is harmless
            # when Telegram rejects it, so no extra preview tracking is needed.
            try:
                await self.bot.edit_message_reply_markup(
                    chat_id=self.admin_id, message_id=post.admin_message_id, reply_markup=None,
                )
            except Exception:
                logger.debug("Could not remove controls before sending long edited preview")
            if post.media_type in {"photo", "video", "document"}:
                try:
                    await self.bot.edit_message_caption(
                        chat_id=self.admin_id, message_id=post.admin_message_id,
                        caption=None, reply_markup=None,
                    )
                except Exception:
                    logger.debug("Previous control message has no editable media caption")
            category_name = post.category.name if post.category is not None else None
            return await _send_text_in_chunks(
                self.bot, self.admin_id, text,
                reply_markup=review_keyboard(post.id, category_name),
                on_sent=lambda messages, control_id: self._track_review_preview_messages(post.id, messages, control_id),
            )
        if not text:
            return await send_review_preview(
                self.bot, self.admin_id, post,
                on_sent=lambda messages, control_id: self._track_review_preview_messages(post.id, messages, control_id),
            )
        category_name = post.category.name if post.category is not None else None
        keyboard = review_keyboard(post.id, category_name)
        if post.admin_message_id is not None:
            try:
                if post.media_type in {"photo", "video", "document"} and post.media_path:
                    if fits_caption(text):
                        return await self.bot.edit_message_caption(
                            chat_id=self.admin_id,
                            message_id=post.admin_message_id,
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                    await self.bot.edit_message_caption(
                        chat_id=self.admin_id,
                        message_id=post.admin_message_id,
                        caption=None,
                        reply_markup=None,
                    )
                    return await _send_text_in_chunks(
                        self.bot, self.admin_id, text, reply_markup=keyboard,
                        on_sent=lambda messages, control_id: self._track_review_preview_messages(post.id, messages, control_id),
                    )
                return await self.bot.edit_message_text(
                    chat_id=self.admin_id,
                    message_id=post.admin_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.debug("Existing review message could not be edited; sending a fresh preview")
                try:
                    await self.bot.edit_message_reply_markup(
                        chat_id=self.admin_id,
                        message_id=post.admin_message_id,
                        reply_markup=None,
                    )
                except Exception:
                    logger.debug("Could not remove controls from the previous review message")
        return await send_review_preview(
            self.bot, self.admin_id, post,
            on_sent=lambda messages, control_id: self._track_review_preview_messages(post.id, messages, control_id),
        )

    async def publish(self, post_id: int) -> str:
        """Claim durably, send each part once, and retain ambiguous outcomes."""
        async with self._publish_lock:
            async with self.session_factory() as session:
                post = await session.get(SourcePost, post_id)
                if post is None:
                    return "missing"
                if post.status == "PUBLISHED":
                    already_published = True
                elif post.status != "REVIEW":
                    return post.status
                else:
                    already_published = False
                    source = await session.get(Source, post.source_id)
                    if source is None:
                        return "missing"
                    source_message_ids = [post.telegram_message_id]
                    source_message_ids.extend(item.telegram_message_id for item in post.media_items)
            if already_published:
                result = "PUBLISHED"
            else:
                protection = await self._source_protection(source.telegram_chat_id, source_message_ids)
                if protection is not False:
                    await self._block_for_protection(post_id, protection is True)
                    return "protected" if protection is True else "protection_unverified"

                async def prepare(session, current):
                    # Read the latest edit and route only after the atomic database claim.
                    if current.category_id is not None:
                        category = await session.get(Category, current.category_id)
                        if category is None:
                            raise PublicationPreflightError("missing_category")
                        if not category.enabled:
                            raise PublicationPreflightError("category_disabled")
                        destination = category.destination_channel_id
                    else:
                        current_source = await session.get(Source, current.source_id)
                        destination = current_source.destination_channel_id if current_source else None
                    if destination is None:
                        raise PublicationPreflightError("missing_destination")
                    return destination, self._publication_parts(destination, current)

                result = await self.publication.publish(post_id, prepare)
        if result in {"published", "PUBLISHED"}:
            await self._cleanup_terminal_media(post_id, "PUBLISHED")
        return result

    async def confirm_publication(self, attempt_id: str, admin_id: int) -> str:
        if admin_id != self.admin_id:
            return "unauthorized"
        async with self._publish_lock:
            return await self.publication.confirm_published(attempt_id, admin_id)

    async def set_post_category(self, post_id: int, category_id: int) -> str:
        """Override only the selected SourcePost's captured category."""
        async with self._publish_lock:
            async with self.session_factory() as session:
                post = await session.get(SourcePost, post_id)
                if post is None:
                    raise ValueError("Публикация не найдена.")
                if post.status != "REVIEW":
                    raise ValueError("Категорию можно изменить только у публикации на проверке.")
                category = await session.get(Category, category_id)
                if category is None or not category.enabled:
                    raise ValueError("Выберите включённую категорию.")
                changed = await session.execute(update(SourcePost).where(
                    SourcePost.id == post_id, SourcePost.status == "REVIEW",
                ).values(category_id=category.id))
                if changed.rowcount != 1:
                    raise ValueError("Публикация уже отправляется. Изменение категории недоступно.")
                await session.commit()
                return category.name

    async def refresh_review_controls(self, post_id: int) -> None:
        """Update the stored admin review message after a route change."""
        async with self.session_factory() as session:
            post = await session.get(SourcePost, post_id)
            if post is None or post.status != "REVIEW" or post.admin_message_id is None:
                return
            keyboard = review_keyboard(
                post.id, post.category.name if post.category is not None else None
            )
            message_id = post.admin_message_id
        await self.bot.edit_message_reply_markup(
            chat_id=self.admin_id,
            message_id=message_id,
            reply_markup=keyboard,
        )

    async def _cleanup_terminal_media(self, post_id: int, terminal_status: str) -> None:
        """Remove terminal-post media safely, then clear its database reference."""
        try:
            async with self.session_factory() as session:
                post = await session.get(SourcePost, post_id)
                if post is None or post.status != terminal_status:
                    return
                media_paths = [
                    ("legacy", post.media_path)
                ] if post.media_path else []
                media_paths.extend(
                    (item.id, item.media_path)
                    for item in post.media_items
                    if item.media_path
                )
                if not media_paths:
                    return
        except Exception as error:
            logger.warning(
                "Could not load terminal media path: source_post_id=%s exception=%s",
                post_id,
                type(error).__name__,
            )
            return

        cleaned_paths: set[str] = set()
        for _key, media_path in media_paths:
            try:
                async with self.session_factory() as session:
                    referenced_by_other_post = await session.scalar(
                        select(SourcePost.id).where(
                            SourcePost.id != post_id, SourcePost.media_path == media_path,
                        ).limit(1)
                    )
                    if referenced_by_other_post is None:
                        referenced_by_other_post = await session.scalar(
                            select(SourcePostMedia.id).join(
                                SourcePost, SourcePost.id == SourcePostMedia.source_post_id,
                            ).where(
                                SourcePost.id != post_id, SourcePostMedia.media_path == media_path,
                            ).limit(1)
                        )
                if referenced_by_other_post is not None:
                    continue
                if delete_media_file(self.media_dir, media_path):
                    cleaned_paths.add(media_path)
                else:
                    logger.warning(
                        "Terminal media cleanup refused an unsafe path: source_post_id=%s",
                        post_id,
                    )
            except Exception as error:
                logger.warning(
                    "Terminal media cleanup failed: source_post_id=%s exception=%s",
                    post_id,
                    type(error).__name__,
                )

        if not cleaned_paths:
            return
        try:
            async with self.session_factory() as session:
                current = await session.get(SourcePost, post_id)
                if current is not None and current.status == terminal_status:
                    if current.media_path in cleaned_paths:
                        current.media_path = None
                    for item in current.media_items:
                        if item.media_path in cleaned_paths:
                            item.media_path = None
                    await session.commit()
        except Exception as error:
            logger.warning(
                "Could not clear cleaned media paths: source_post_id=%s exception=%s",
                post_id,
                type(error).__name__,
            )

    def _publication_parts(self, destination_id: int, post: SourcePost) -> list[PublicationPart]:
        """Plan and validate every send before the durable claim is committed."""
        text = candidate_html(post)
        parts: list[PublicationPart] = []

        def add(kind: str, method: Any, kwargs: dict[str, Any], identity: Any,
                expected_messages: int = 1) -> None:
            async def send() -> Any:
                return await method(chat_id=destination_id, request_timeout=REQUEST_TIMEOUT, **kwargs)
            parts.append(PublicationPart(kind, fingerprint([kind, identity]), send, expected_messages))

        def add_text() -> None:
            for chunk in _text_chunks(text or ""):
                add("text", self.bot.send_message, {"text": chunk, "parse_mode": "HTML"}, chunk)

        def add_media(item: Any, caption: str | None) -> None:
            path = item.media_path
            if not path or not Path(path).is_file():
                raise PublicationPreflightError("failed")
            method = {"photo": self.bot.send_photo, "video": self.bot.send_video,
                      "document": self.bot.send_document}[item.media_type]
            kwargs = {item.media_type: FSInputFile(path)}
            if caption:
                kwargs.update(caption=caption, parse_mode="HTML")
            add(item.media_type, method, kwargs, [path, caption])

        if post.media_type == "album":
            items = [item for item in post.media_items if item.media_path]
            for item in items:
                if not Path(item.media_path).is_file():
                    raise PublicationPreflightError("failed")
            caption = text if text and fits_caption(text) else None
            if len(items) >= 2:
                add("media_group", self.bot.send_media_group,
                    {"media": _media_group(items, caption=caption)},
                    [[item.telegram_message_id, item.media_type, item.media_path] for item in items] + [caption],
                    expected_messages=len(items))
            elif items:
                add_media(items[0], caption)
            if not items or (text and not fits_caption(text)):
                add_text()
        elif post.media_type in {"photo", "video", "document"}:
            add_media(post, text if text and fits_caption(text) else None)
            if text and not fits_caption(text):
                add_text()
        else:
            add_text()
        if not parts:
            raise PublicationPreflightError("failed")
        return parts


def _looks_like_permission_error(error: Exception) -> bool:
    value = f"{type(error).__name__} {error}".lower()
    return any(part in value for part in ("forbidden", "not enough rights", "chat_admin_required", "chat_write_forbidden"))


async def review_delivery_worker(
    workflow: AdminWorkflow,
    *,
    poll_interval: float = 5.0,
) -> None:
    """Poll NEW rows at a modest interval so restarts do not lose candidates."""
    while True:
        try:
            await workflow.deliver_new()
        except Exception as error:
            # SQLAlchemy exception text can contain SQL and bound values. Keep
            # this recurring diagnostic useful without retaining request data.
            logger.error(
                "Review delivery poll failed; will retry on the next interval "
                "(exception=%s)",
                type(error).__name__,
            )
        await asyncio.sleep(poll_interval)


def _safe_bot_error(error: Exception, bot: Bot) -> str:
    """Keep short API diagnostics while excluding bot tokens and request URLs."""
    message = " ".join(str(error).split())
    token = getattr(bot, "token", "")
    if token:
        message = message.replace(token, "[REDACTED]")
    message = re.sub(
        r"(?i)(https?://api\.telegram\.org/bot)[^/\s]+",
        r"\1[REDACTED]",
        message,
    )
    message = re.sub(r"(?i)(?:https?://|tg://)[^\s<>]+", "[REDACTED URL]", message)
    return message[:240] if message else "unavailable"
