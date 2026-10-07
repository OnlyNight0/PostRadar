"""Telethon source channel monitor and message persistence."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from telethon import TelegramClient, events, types, utils

from postradar.db.models import Source, SourcePost, SourcePostMedia
from postradar.services.media import delete_media_file, download_message_media, media_target_path
from postradar.services.sanitizer import sanitize_text

logger = logging.getLogger(__name__)
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


@dataclass(frozen=True)
class ResolvedSource:
    telegram_chat_id: int
    title: str
    username: str | None


class SourceResolutionError(ValueError):
    """Raised when an identifier cannot resolve to an accessible channel."""


def parse_source_identifier(value: str) -> int | str | types.PeerChannel:
    """Validate supported source references and normalize public t.me links."""
    value = value.strip()
    if not value:
        raise SourceResolutionError("Enter a Telegram username, t.me link, or numeric channel ID.")
    if re.fullmatch(r"-?\d+", value):
        chat_id = int(value)
        if chat_id == 0 or abs(chat_id) > 2**63 - 1:
            raise SourceResolutionError("The numeric Telegram ID is invalid.")
        return chat_id

    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            raise SourceResolutionError("Use a t.me or telegram.me link.")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise SourceResolutionError("The Telegram link does not identify a channel.")
        if parts[0] == "c":
            if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
                raise SourceResolutionError("The private channel link is malformed.")
            return types.PeerChannel(int(parts[1]))
        if parts[0] == "s":
            parts = parts[1:]
        if not parts or parts[0] == "joinchat" or parts[0].startswith("+"):
            raise SourceResolutionError(
                "Invite links cannot be resolved automatically; use the channel username or numeric ID."
            )
        value = "@" + parts[0]

    username = value[1:] if value.startswith("@") else value
    if not _USERNAME.fullmatch(username):
        raise SourceResolutionError("The Telegram username is invalid.")
    return "@" + username


def validate_telegram_settings(api_id: int | None, api_hash: str, session: str) -> None:
    """Fail before constructing Telethon when required credentials are absent."""
    missing = [
        name
        for name, value in (
            ("TELEGRAM_API_ID", api_id),
            ("TELEGRAM_API_HASH", api_hash.strip()),
            ("TELEGRAM_SESSION", session.strip()),
        )
        if not value
    ]
    if missing:
        raise ValueError("Missing required Telegram configuration: " + ", ".join(missing))


async def require_user_account(client: TelegramClient) -> Any:
    """Reject an authorized bot identity where source monitoring needs a user."""
    me = await client.get_me()
    if me is None:
        raise RuntimeError("Could not determine the identity for the configured Telethon session.")
    if me.bot:
        raise RuntimeError(
            "The configured Telethon session belongs to a bot. Re-authorize "
            "TELEGRAM_SESSION using scripts/authorize_telegram.py as a normal Telegram user account. "
            "If this session was already created for a bot, manually remove or rename its session "
            "file before re-authorizing; PostRadar will not delete or overwrite it automatically."
        )
    return me


async def load_enabled_sources(session_factory: async_sessionmaker) -> list[Source]:
    """Return only sources explicitly enabled for monitoring."""
    async with session_factory() as session:
        result = await session.scalars(select(Source).where(Source.enabled.is_(True)))
        return list(result.all())


def detect_media_type(message: Any) -> str:
    """Classify a Telethon message without downloading its media."""
    media = getattr(message, "media", None)
    if media is None:
        return "text"
    if getattr(message, "photo", None) is not None:
        return "photo"
    if getattr(message, "video", None) is not None:
        return "video"
    if getattr(message, "document", None) is not None:
        document = message.document
        mime_type = (getattr(document, "mime_type", "") or "").lower()
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("image/"):
            return "photo"
        return "document"
    return "other"


async def persist_message(
    session_factory: async_sessionmaker,
    source: Source,
    message: Any,
    media_client: Any | None = None,
    media_dir: str | Path = "./data/media",
    ai_editor: Any | None = None,
) -> bool:
    """Persist once; return False when the DB uniqueness constraint detects a duplicate."""
    text = getattr(message, "message", None)
    message_date = getattr(message, "date", None)
    if message_date is not None and message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)

    async with session_factory() as session:
        existing_id = await session.scalar(
            select(SourcePost.id).where(
                SourcePost.source_id == source.id,
                SourcePost.telegram_message_id == message.id,
            )
        )
        if existing_id is not None:
            return False

    media_path: str | None = None
    target_path = media_target_path(media_dir, source.id, message)
    if target_path is not None and media_client is not None:
        try:
            media_path = await download_message_media(media_client, message, target_path)
        except Exception:
            logger.exception(
                "Failed to download source media; storing post without media: "
                "source_chat_id=%s message_id=%s",
                source.telegram_chat_id,
                message.id,
            )

    async with session_factory() as session:
        try:
            sanitized_text = sanitize_text(text, source)
        except Exception:
            logger.exception(
                "Failed to sanitize source post; preserving original text: "
                "source_chat_id=%s message_id=%s",
                source.telegram_chat_id,
                message.id,
            )
            sanitized_text = text

        edited_text = sanitized_text
        if sanitized_text and sanitized_text.strip() and ai_editor is not None:
            try:
                edited_text = await ai_editor.edit(sanitized_text)
                if not edited_text or not edited_text.strip():
                    edited_text = sanitized_text
            except Exception as error:
                logger.warning(
                    "AI edit failed (%s); using sanitized text: source_chat_id=%s message_id=%s",
                    type(error).__name__,
                    source.telegram_chat_id,
                    message.id,
                )
                edited_text = sanitized_text
        session.add(
            SourcePost(
                source_id=source.id,
                telegram_message_id=message.id,
                original_text=text,
                sanitized_text=sanitized_text,
                edited_text=edited_text,
                media_type=detect_media_type(message),
                media_path=media_path,
                category_id=source.category_id,
                published_at=message_date,
                captured_at=datetime.now(timezone.utc),
                status="NEW",
            )
        )
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            existing_id = await session.scalar(
                select(SourcePost.id).where(
                    SourcePost.source_id == source.id,
                    SourcePost.telegram_message_id == message.id,
                )
            )
            if existing_id is not None:
                return False
            raise


async def persist_album(
    session_factory: async_sessionmaker,
    source: Source,
    grouped_id: int,
    messages: list[Any],
    media_client: Any,
    media_dir: str | Path = "./data/media",
    ai_editor: Any | None = None,
) -> bool:
    """Persist a grouped Telegram post as one candidate with ordered media rows."""
    ordered_messages = sorted(messages, key=lambda item: item.id)
    if not ordered_messages:
        return False
    representative_id = ordered_messages[0].id
    async with session_factory() as session:
        existing = await session.scalar(
            select(SourcePost.id).where(
                SourcePost.source_id == source.id,
                SourcePost.grouped_id == grouped_id,
            )
        )
        if existing is not None:
            return False

    captions: list[str] = []
    for message in ordered_messages:
        caption = getattr(message, "message", None)
        if isinstance(caption, str) and caption.strip() and caption not in captions:
            captions.append(caption)
    original_text = "\n\n".join(captions) if captions else None
    published_at = getattr(ordered_messages[0], "date", None)
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)

    sanitized_text = original_text
    if original_text:
        try:
            sanitized_text = sanitize_text(original_text, source)
        except Exception:
            logger.exception(
                "Failed to sanitize source album; preserving original text: "
                "source_chat_id=%s grouped_id=%s",
                source.telegram_chat_id,
                grouped_id,
            )
            sanitized_text = original_text

    edited_text = sanitized_text
    if sanitized_text and sanitized_text.strip() and ai_editor is not None:
        try:
            edited_text = await ai_editor.edit(sanitized_text)
            if not edited_text or not edited_text.strip():
                edited_text = sanitized_text
        except Exception as error:
            logger.warning(
                "AI edit failed for album (%s); using sanitized text: "
                "source_chat_id=%s grouped_id=%s",
                type(error).__name__,
                source.telegram_chat_id,
                grouped_id,
            )
            edited_text = sanitized_text

    saved_items: list[tuple[Any, str, str, int]] = []
    for position, message in enumerate(ordered_messages):
        media_type = detect_media_type(message)
        if media_type not in {"photo", "video", "document"}:
            continue
        target_path = media_target_path(media_dir, source.id, message)
        if target_path is None:
            continue
        try:
            media_path = await download_message_media(media_client, message, target_path)
            if media_path:
                saved_items.append((message, media_type, media_path, position))
        except Exception:
            logger.exception(
                "Failed to download album item: source_chat_id=%s grouped_id=%s message_id=%s",
                source.telegram_chat_id,
                grouped_id,
                message.id,
            )

    async with session_factory() as session:
        post = SourcePost(
            source_id=source.id,
            telegram_message_id=representative_id,
            grouped_id=grouped_id,
            original_text=original_text,
            sanitized_text=sanitized_text,
            edited_text=edited_text,
            media_type="album",
            category_id=source.category_id,
            published_at=published_at,
            captured_at=datetime.now(timezone.utc),
            status="NEW",
            media_items=[
                SourcePostMedia(
                    telegram_message_id=message.id,
                    media_type=media_type,
                    media_path=media_path,
                    position=position,
                )
                    for message, media_type, media_path, position in saved_items
            ],
        )
        session.add(post)
        try:
            await session.commit()
            logger.info(
                "Album finalized: source_id=%s grouped_id=%s items=%s",
                source.id,
                grouped_id,
                len(saved_items),
            )
            return True
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(SourcePost.id).where(
                    SourcePost.source_id == source.id,
                    (SourcePost.grouped_id == grouped_id)
                    | (SourcePost.telegram_message_id == representative_id),
                )
            )
            if existing is None:
                raise
            for _message, _media_type, media_path, _position in saved_items:
                delete_media_file(media_dir, media_path)
            return False


class SourceMonitor:
    """Own the Telethon client and route only configured source events."""

    def __init__(
        self,
        api_id: int | None,
        api_hash: str,
        session: str,
        session_factory: async_sessionmaker,
        media_dir: str | Path = "./data/media",
        ai_editor: Any | None = None,
        client: Any | None = None,
        album_collection_delay: float = 1.5,
    ) -> None:
        validate_telegram_settings(api_id, api_hash, session)
        self._client = client or TelegramClient(session, api_id, api_hash)
        self._session_factory = session_factory
        self._media_dir = media_dir
        self._ai_editor = ai_editor
        self._processing_lock = asyncio.Lock()
        self._album_lock = asyncio.Lock()
        self._album_collection_delay = max(0.0, album_collection_delay)
        self._album_batches: dict[
            tuple[int, int], tuple[Source, dict[int, Any], asyncio.Task[None]]
        ] = {}
        self._ready = asyncio.Event()
        self._source_by_chat_id: dict[int, Source] = {}

    async def resolve_source(self, identifier: str) -> ResolvedSource:
        """Resolve an accessible channel through the existing Telethon user client."""
        await self._ready.wait()
        reference = parse_source_identifier(identifier)
        try:
            entity = await self._client.get_entity(reference)
        except Exception as error:
            raise SourceResolutionError(
                "The Telethon user account cannot access that channel. Join it in Telegram and try again."
            ) from error
        if not isinstance(entity, types.Channel) or not (
            getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)
        ):
            raise SourceResolutionError("The selected Telegram chat is not a channel or supergroup.")
        return ResolvedSource(
            telegram_chat_id=utils.get_peer_id(entity),
            title=getattr(entity, "title", None) or "Untitled channel",
            username=getattr(entity, "username", None),
        )

    async def refresh_enabled_sources(self) -> int:
        """Refresh the event routing map after source management changes."""
        sources = await load_enabled_sources(self._session_factory)
        self._source_by_chat_id = {source.telegram_chat_id: source for source in sources}
        logger.info("Loaded %d enabled source(s) for Telethon monitoring", len(sources))
        return len(sources)

    async def run(self) -> None:
        """Connect, register new-message routing, and wait until disconnected."""
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized. Run: "
                "./venv/bin/python scripts/authorize_telegram.py"
            )
        await require_user_account(self._client)

        logger.info("Telethon user session connected")
        self._ready.set()
        await self.refresh_enabled_sources()
        if not self._source_by_chat_id:
            logger.warning("No enabled sources configured; monitor will wait for source management")

        async def on_new_message(event: Any) -> None:
            source = self._source_by_chat_id.get(event.chat_id)
            if source is None:
                return
            message = event.message
            grouped_id = getattr(message, "grouped_id", None)
            logger.info(
                "Received source post: source_chat_id=%s message_id=%s grouped=%s",
                source.telegram_chat_id,
                message.id,
                grouped_id is not None,
            )
            if grouped_id is not None:
                await self._collect_album_message(source, message, grouped_id)
                return
            try:
                async with self._processing_lock:
                    inserted = await persist_message(
                        self._session_factory,
                        source,
                        message,
                        media_client=self._client,
                        media_dir=self._media_dir,
                        ai_editor=self._ai_editor,
                    )
                if inserted:
                    logger.info("Stored source post: source_id=%s message_id=%s", source.id, message.id)
                else:
                    logger.info(
                        "Duplicate source post ignored: source_id=%s message_id=%s",
                        source.id,
                        message.id,
                    )
            except Exception:
                logger.exception(
                    "Failed to process source post: source_chat_id=%s message_id=%s",
                    source.telegram_chat_id,
                    message.id,
                )

        self._client.add_event_handler(on_new_message, events.NewMessage())
        await self._client.run_until_disconnected()

    async def _collect_album_message(self, source: Source, message: Any, grouped_id: int) -> None:
        """Collect album fragments and restart a bounded debounce timer."""
        key = (source.id, grouped_id)
        async with self._album_lock:
            current = self._album_batches.get(key)
            messages = current[1] if current else {}
            messages[message.id] = message
            if current:
                current[2].cancel()
            task = asyncio.create_task(
                self._finalize_album_after_delay(key),
                name=f"postradar-album-{source.id}-{grouped_id}",
            )
            self._album_batches[key] = (source, messages, task)
            if current is None:
                logger.info(
                    "Album collection started: source_id=%s grouped_id=%s",
                    source.id,
                    grouped_id,
                )

    async def _finalize_album_after_delay(self, key: tuple[int, int]) -> None:
        try:
            await asyncio.sleep(self._album_collection_delay)
            async with self._album_lock:
                batch = self._album_batches.get(key)
                if batch is None or batch[2] is not asyncio.current_task():
                    return
                self._album_batches.pop(key, None)
            await self._process_album(key, batch[0], list(batch[1].values()))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to process Telegram album: source_id=%s grouped_id=%s",
                key[0],
                key[1],
            )

    async def _process_album(
        self, key: tuple[int, int], source: Source, messages: list[Any]
    ) -> None:
        async with self._processing_lock:
            inserted = await persist_album(
                self._session_factory,
                source,
                key[1],
                messages,
                media_client=self._client,
                media_dir=self._media_dir,
                ai_editor=self._ai_editor,
            )
        if not inserted:
            logger.info(
                "Duplicate album ignored: source_id=%s grouped_id=%s",
                source.id,
                key[1],
            )

    async def _flush_pending_albums(self) -> None:
        async with self._album_lock:
            batches = list(self._album_batches.items())
            self._album_batches.clear()
            for _key, (_source, _messages, task) in batches:
                task.cancel()
        if batches:
            await asyncio.gather(*(batch[1][2] for batch in batches), return_exceptions=True)
        for key, (source, messages, _task) in batches:
            try:
                await self._process_album(key, source, list(messages.values()))
            except Exception:
                logger.exception(
                    "Failed to flush Telegram album during shutdown: source_id=%s grouped_id=%s",
                    source.id,
                    key[1],
                )

    async def close(self) -> None:
        """Disconnect the Telethon client if connected."""
        if self._client.is_connected():
            await self._client.disconnect()
        await self._flush_pending_albums()
