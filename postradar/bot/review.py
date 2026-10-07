"""Database-backed admin review delivery and post actions."""

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from postradar.db.models import Category, Source, SourcePost, SourcePostMedia
from postradar.bot.keyboards import review_keyboard
from postradar.services.media import delete_media_file

logger = logging.getLogger(__name__)
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def candidate_text(post: SourcePost) -> str | None:
    """Return the current publish candidate, excluding original source text."""
    for value in (post.edited_text, post.sanitized_text):
        if isinstance(value, str) and value.strip():
            return value
    return None


def fits_caption(text: str) -> bool:
    """Check Telegram's caption limit in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2 <= CAPTION_LIMIT


def _text_chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split text at Unicode character boundaries within Telegram's UTF-16 limit."""
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for character in text:
        character_units = len(character.encode("utf-16-le")) // 2
        if current and units + character_units > limit:
            chunks.append("".join(current))
            current = []
            units = 0
        current.append(character)
        units += character_units
    if current:
        chunks.append("".join(current))
    return chunks


async def _send_text_in_chunks(bot: Bot, chat_id: int, text: str) -> None:
    for chunk in _text_chunks(text):
        await bot.send_message(chat_id=chat_id, text=chunk)


def _media_group(
    items: list[SourcePostMedia], *, caption: str | None = None
) -> list[Any]:
    """Build Telegram media-group entries in persisted album order."""
    media = []
    for index, item in enumerate(sorted(items, key=lambda entry: entry.position)):
        upload = FSInputFile(item.media_path)
        item_caption = caption if index == 0 else None
        if item.media_type == "photo":
            media.append(InputMediaPhoto(media=upload, caption=item_caption))
        elif item.media_type == "video":
            media.append(InputMediaVideo(media=upload, caption=item_caption))
        elif item.media_type == "document":
            media.append(InputMediaDocument(media=upload, caption=item_caption))
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
    return await send_media(**kwargs)


async def send_review_preview(bot: Bot, admin_id: int, post: SourcePost) -> Message:
    """Send a text or local-media preview, placing controls on the review text."""
    text = candidate_text(post)
    category_name = post.category.name if post.category is not None else None
    keyboard = review_keyboard(post.id, category_name)
    if post.media_type == "album":
        media_items = [item for item in post.media_items if item.media_path]
        if not media_items:
            if text:
                return await bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=keyboard,
                )
            raise ValueError(f"SourcePost {post.id} has neither downloaded album media nor candidate text")
        for item in media_items:
            if not Path(item.media_path).is_file():
                raise FileNotFoundError(f"Stored album media file is missing: {item.media_path}")
        if len(media_items) >= 2:
            await bot.send_media_group(
                chat_id=admin_id,
                media=_media_group(media_items),
            )
        else:
            await _send_one_media(bot, admin_id, media_items[0])
        context = f"Альбом · файлов: {len(media_items)}"
        if text:
            details = f"{text}\n\n{context}"
            if len(details.encode("utf-16-le")) // 2 > MESSAGE_LIMIT:
                await _send_text_in_chunks(bot, admin_id, text)
                details = context
        else:
            details = f"Текст публикации отсутствует.\n\n{context}"
        control_message = await bot.send_message(
            chat_id=admin_id,
            text=details,
            reply_markup=keyboard,
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
            return await send_media(
                chat_id=admin_id,
                **{post.media_type: upload},
                caption=text,
                reply_markup=keyboard,
            )

        media_message = await send_media(
            chat_id=admin_id,
            **{post.media_type: upload},
            reply_markup=keyboard if not text else None,
        )
        if text:
            return await bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=keyboard,
            )
        return media_message

    if text:
        return await bot.send_message(
            chat_id=admin_id,
            text=text,
            reply_markup=keyboard,
        )
    raise ValueError(f"SourcePost {post.id} has neither a media file nor candidate text")


async def deliver_new_posts(
    bot: Bot,
    session_factory: async_sessionmaker,
    admin_id: int,
    *,
    limit: int = 20,
) -> int:
    """Deliver a bounded batch of NEW posts and mark successful previews REVIEW."""
    async with session_factory() as session:
        posts = list(
            (
                await session.scalars(
                    select(SourcePost)
                    .options(selectinload(SourcePost.media_items))
                    .where(SourcePost.status == "NEW")
                    .order_by(SourcePost.id)
                    .limit(limit)
                )
            ).all()
        )

    delivered = 0
    for post in posts:
        try:
            message = await send_review_preview(bot, admin_id, post)
            async with session_factory() as session:
                current = await session.get(SourcePost, post.id)
                if current is None or current.status != "NEW":
                    continue
                current.admin_message_id = message.message_id
                current.status = "REVIEW"
                await session.commit()
            delivered += 1
            logger.info("Review candidate delivered: source_post_id=%s", post.id)
        except Exception as error:
            logger.error(
                "Admin preview delivery failed: source_post_id=%s exception=%s error=%s",
                post.id,
                type(error).__name__,
                _safe_bot_error(error, bot),
            )
    return delivered


class AdminWorkflow:
    """Coordinate admin actions with persisted post state."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker,
        admin_id: int,
        media_dir: str | Path = "./data/media",
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.admin_id = admin_id
        self.media_dir = Path(media_dir)
        self._publish_lock = asyncio.Lock()

    async def deliver_new(self) -> int:
        return await deliver_new_posts(self.bot, self.session_factory, self.admin_id)

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
                    post.status = "SKIPPED"
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
                post.edited_text = replacement
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
        text = candidate_text(post)
        if not text:
            return await send_review_preview(self.bot, self.admin_id, post)
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
                            reply_markup=keyboard,
                        )
                    await self.bot.edit_message_caption(
                        chat_id=self.admin_id,
                        message_id=post.admin_message_id,
                        caption=None,
                        reply_markup=None,
                    )
                    return await self.bot.send_message(
                        chat_id=self.admin_id, text=text, reply_markup=keyboard
                    )
                return await self.bot.edit_message_text(
                    chat_id=self.admin_id,
                    message_id=post.admin_message_id,
                    text=text,
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
        return await send_review_preview(self.bot, self.admin_id, post)

    async def publish(self, post_id: int) -> str:
        """Publish once per process and update status only after Telegram confirms."""
        already_published = False
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
                    if post.category_id is not None:
                        category = await session.get(Category, post.category_id)
                        if category is None:
                            return "missing_category"
                        if not category.enabled:
                            return "category_disabled"
                        if category.destination_channel_id is None:
                            return "missing_destination"
                        destination_id = category.destination_channel_id
                    else:
                        source = await session.get(Source, post.source_id)
                        if source is None or source.destination_channel_id is None:
                            return "missing_destination"
                        destination_id = source.destination_channel_id
                    text = candidate_text(post)
                    media_type = post.media_type
                    media_path = post.media_path
                    media_items = list(post.media_items)

            if already_published:
                pass
            else:
                try:
                    await self._publish_content(
                        destination_id, post_id, text, media_type, media_path, media_items
                    )
                except Exception as error:
                    logger.error(
                        "Publishing failed: source_post_id=%s exception=%s error=%s",
                        post_id,
                        type(error).__name__,
                        _safe_bot_error(error, self.bot),
                    )
                    return "permission_error" if _looks_like_permission_error(error) else "failed"

                async with self.session_factory() as session:
                    current = await session.get(SourcePost, post_id)
                    if current is None:
                        return "missing"
                    if current.status != "REVIEW":
                        return current.status
                    current.status = "PUBLISHED"
                    await session.commit()

        await self._cleanup_terminal_media(post_id, "PUBLISHED")
        if already_published:
            return "PUBLISHED"
        logger.info("Source post published: source_post_id=%s", post_id)
        return "published"

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
                post.category_id = category.id
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

    async def _publish_content(
        self,
        destination_id: int,
        post_id: int,
        text: str | None,
        media_type: str | None,
        media_path: str | None,
        media_items: list[SourcePostMedia] | None = None,
    ) -> None:
        if media_type == "album":
            available_items = [item for item in (media_items or []) if item.media_path]
            if not available_items:
                if not text:
                    raise ValueError(f"SourcePost {post_id} has no album media or candidate text")
                await self.bot.send_message(chat_id=destination_id, text=text)
                return
            for item in available_items:
                if not Path(item.media_path).is_file():
                    raise FileNotFoundError(
                        f"SourcePost {post_id} has no available album media file: {item.media_path}"
                    )
            if len(available_items) >= 2:
                media = _media_group(
                    available_items,
                    caption=text if text and fits_caption(text) else None,
                )
                await self.bot.send_media_group(chat_id=destination_id, media=media)
            else:
                item = available_items[0]
                if text and fits_caption(text):
                    await _send_one_media(self.bot, destination_id, item, caption=text)
                else:
                    await _send_one_media(self.bot, destination_id, item)
            if text and not fits_caption(text):
                await _send_text_in_chunks(self.bot, destination_id, text)
            return
        if media_type in {"photo", "video", "document"}:
            if not media_path or not Path(media_path).is_file():
                raise FileNotFoundError(f"SourcePost {post_id} has no available local media file")
            upload = FSInputFile(media_path)
            send_media = {
                "photo": self.bot.send_photo,
                "video": self.bot.send_video,
                "document": self.bot.send_document,
            }[media_type]
            if text and fits_caption(text):
                await send_media(
                    chat_id=destination_id,
                    **{media_type: upload},
                    caption=text,
                )
            else:
                await send_media(chat_id=destination_id, **{media_type: upload})
                if text:
                    await self.bot.send_message(chat_id=destination_id, text=text)
            return
        if not text:
            raise ValueError(f"SourcePost {post_id} has no candidate text to publish")
        await self.bot.send_message(chat_id=destination_id, text=text)


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
        except Exception:
            logger.exception("Review delivery poll failed; will retry on the next interval")
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
    return message[:240] if message else "unavailable"
