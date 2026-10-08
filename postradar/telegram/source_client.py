"""Telethon source channel monitor and message persistence."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from telethon import TelegramClient, errors, events, types, utils

from postradar.lifecycle import finish_cleanup

from postradar.db.models import Category, Source, SourcePost, SourcePostMedia
from postradar.services.media import delete_media_file, download_message_media, media_target_path
from postradar.services.ai_editor import ProcessingResult, uncertain
from postradar.services.capture import (
    CAPTURE_PENDING, CaptureClaim, CaptureJournal, PROCESS_TIMEOUT, capture_now,
)
from postradar.services.telegram_markup import normalize_message, plain_text, validate_edit

logger = logging.getLogger(__name__)
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
PROTECTION_CAPTURE_PENDING = "PROTECTION_CAPTURE_PENDING"
PROTECTION_ALBUM_PENDING = "PROTECTION_ALBUM_PENDING"


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


def content_protection_state(chat: Any, message: Any) -> bool | None:
    """Return True if protected, False only when both Telegram flags are known false."""
    chat_flag = getattr(chat, "noforwards", None)
    message_flag = getattr(message, "noforwards", None)
    if chat_flag is True or message_flag is True:
        return True
    if chat_flag is False and message_flag is False:
        return False
    return None


async def mark_post_protected(
    session_factory: async_sessionmaker,
    source: Source,
    message_id: int,
    media_dir: str | Path,
    *,
    grouped_id: int | None = None,
    protection_known: bool,
) -> None:
    """Record protection state without destroying data on an uncertain lookup."""
    status = "PROTECTED" if protection_known else (
        PROTECTION_ALBUM_PENDING if grouped_id is not None else PROTECTION_CAPTURE_PENDING
    )
    reason = (
        "Telegram content protection is enabled; content was skipped."
        if protection_known
        else "Telegram protection metadata was unavailable; content was skipped."
    )
    paths: list[str] = []
    async with session_factory() as session:
        post = None
        if grouped_id is not None:
            post = await session.scalar(select(SourcePost).where(
                SourcePost.source_id == source.id,
                SourcePost.grouped_id == grouped_id,
            ))
        if post is None:
            post = await session.scalar(select(SourcePost).where(
                SourcePost.source_id == source.id,
                SourcePost.telegram_message_id == message_id,
            ))
        terminal = post is not None and post.status in {"PUBLISHED", "SKIPPED", "FILTERED"}
        if post is None:
            post = SourcePost(
                source_id=source.id,
                telegram_message_id=message_id,
                grouped_id=grouped_id,
                status=status,
                classification_reason=reason,
                category_id=source.category_id,
            )
            session.add(post)
        elif not protection_known:
            # A failed metadata lookup is not evidence of protection. Leave existing
            # candidates and terminal records intact so the normal workflow can retry.
            return
        elif terminal:
            return
        else:
            post.status = status
            post.classification_reason = reason
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
    for path in set(paths):
        try:
            # Do not unlink a file if another post still references it.
            async with session_factory() as session:
                referenced = await session.scalar(
                    select(SourcePost.id).where(SourcePost.media_path == path).limit(1)
                )
                if referenced is None:
                    referenced = await session.scalar(
                        select(SourcePostMedia.id).where(SourcePostMedia.media_path == path).limit(1)
                    )
            if referenced is None:
                delete_media_file(media_dir, path)
        except Exception:
            logger.warning("Could not remove protected local media: source_id=%s", source.id)


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


def _normalize_source(message: Any, source_id: int) -> tuple[str | None, bool]:
    """Retain exact source text and bypass AI if technical normalization fails."""
    text = getattr(message, "message", None)
    try:
        markup = normalize_message(message)
        if text is not None and (markup is None or plain_text(markup) != text):
            raise ValueError("Normalization changed visible source text")
        return markup, False
    except Exception as error:
        logger.warning(
            "Source entity normalization failed; using escaped plain text for UNCERTAIN review: "
            "source_id=%s message_id=%s exception=%s",
            source_id, message.id, type(error).__name__,
        )
        return escape(text, quote=False) if text is not None else None, True


async def _process_text(
    session_factory: async_sessionmaker, source: Source,
    source_html: str | None, ai_editor: Any | None, identity: int,
) -> ProcessingResult:
    result = uncertain(source_html, "No usable text or AI processing unavailable")
    if source_html and plain_text(source_html).strip() and ai_editor is not None:
        async with session_factory() as session:
            category = await session.get(Category, source.category_id) if source.category_id else None
            category_name = category.name if category else None
        try:
            result = await ai_editor.process(
                source_html, category_name=category_name,
                source_title=source.title, source_username=source.username,
            )
            if result.content_type not in {"CONTENT", "AD", "SELF_PROMO", "UNCERTAIN"}:
                raise ValueError("Unknown classification")
            if result.content_type in {"CONTENT", "UNCERTAIN"}:
                result = ProcessingResult(
                    result.content_type, result.reason,
                    validate_edit(source_html, result.edited_html or ""),
                )
        except Exception as error:
            logger.warning(
                "AI processing/markup validation failed; using source fallback: source_id=%s message_identity=%s exception=%s",
                source.id, identity, type(error).__name__,
            )
            result = uncertain(source_html, "AI processing or output validation failed")
    logger.info("AI post classified: source_id=%s message_identity=%s content_type=%s", source.id, identity, result.content_type)
    if result.content_type in {"AD", "SELF_PROMO"}:
        logger.info("Source post filtered: source_id=%s message_identity=%s content_type=%s", source.id, identity, result.content_type)
    return result


async def persist_message(
    session_factory: async_sessionmaker,
    source: Source,
    message: Any,
    media_client: Any | None = None,
    media_dir: str | Path = "./data/media",
    ai_editor: Any | None = None,
    recovery_marker_id: int | None = None,
    *,
    capture_claim: CaptureClaim | None = None,
    protection_check: Any | None = None,
) -> bool:
    """Caller verifies protection; persist a receipt, then complete only this fenced claim."""
    if getattr(message, 'grouped_id', None) is not None:
        return False
    journal = CaptureJournal(session_factory)
    marker_id = capture_claim.post_id if capture_claim else recovery_marker_id
    if marker_id is None:
        marker_id = await journal.receipt(source, message.id)
    async with session_factory() as session:
        identity = await session.scalar(select(SourcePost.id).where(
            SourcePost.id == marker_id, SourcePost.source_id == source.id,
            SourcePost.telegram_message_id == message.id, SourcePost.grouped_id.is_(None),
        ))
    if identity is None:
        return False
    claim = capture_claim or await journal.claim(
        marker_id, verified_protection_marker=recovery_marker_id is not None,
    )
    if claim is None:
        return False
    temporary = final = None

    async def protection_allowed() -> bool:
        if protection_check is None:
            return True  # Internal direct callers must already have verified protection.
        state = await protection_check(source.telegram_chat_id, [message.id])
        if state is not False:
            await journal.defer(claim, 'protected' if state is True else 'protection_unknown',
                                terminal_status='PROTECTED' if state is True else None)
            return False
        return True

    async def cleanup() -> None:
        for path in (temporary, final):
            if path is not None:
                await journal.cleanup_owned_file(media_dir, path)

    try:
        async with asyncio.timeout(PROCESS_TIMEOUT):
            if not await protection_allowed():
                return False
            # The original category snapshot also controls the AI category context.
            snapshot = Source(id=source.id, telegram_chat_id=source.telegram_chat_id,
                              category_id=claim.category_id, title=source.title, username=source.username)
            source_html, normalization_failed = _normalize_source(message, source.id)
            result = (
                uncertain(source_html, "Source entity normalization failed")
                if normalization_failed else
                await _process_text(session_factory, snapshot, source_html, ai_editor, message.id)
            )
            filtered = result.content_type in {"AD", "SELF_PROMO"}
            if not await protection_allowed():
                return False
            media_type = detect_media_type(message)
            media = getattr(message, 'media', None)
            # Link previews and empty wrappers have no required attachment.
            required_media = media is not None and not isinstance(
                media, (types.MessageMediaWebPage, types.MessageMediaEmpty),
            )
            if not required_media:
                media_type = 'text'
            if not filtered and required_media:
                target = media_target_path(media_dir, source.id, message)
                if media_type not in {'photo', 'video', 'document'} or target is None or media_client is None:
                    await journal.defer(claim, 'media_unavailable', terminal_status='CAPTURE_FAILED')
                    return False
                final = target.with_name(f'{message.id}_capture-{claim.post_id}-{claim.token}_{target.name}')
                temporary = final.with_name('.part-' + final.name)
                if (not final.resolve().is_relative_to(Path(media_dir).expanduser().resolve())
                        or temporary.is_symlink() or final.is_symlink()):
                    await journal.defer(claim, 'unsafe_media_path', terminal_status='CAPTURE_FAILED')
                    return False
                downloaded = await download_message_media(media_client, message, temporary)
                if (downloaded is None or Path(downloaded).resolve() != temporary.resolve()
                        or not temporary.is_file() or temporary.is_symlink()):
                    raise OSError('Required capture media was not completely downloaded')
                downloaded_size = temporary.stat().st_size
                # Photo previews have multiple sizes; File.size need not match
                # the rendition Telethon selects. Document size is authoritative.
                expected_size = (getattr(getattr(message, 'file', None), 'size', None)
                                 if getattr(message, 'document', None) is not None else None)
                if (downloaded_size == 0 or (isinstance(expected_size, int)
                        and expected_size > 0 and downloaded_size != expected_size)):
                    raise OSError('Required capture media size is incomplete')
            if not await protection_allowed():
                return False
            date = getattr(message, 'date', None)
            if date is not None and date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            text = getattr(message, 'message', None)
            completed = await journal.complete(claim, {
                'original_text': text, 'sanitized_text': text, 'source_html': source_html,
                'edited_html': result.edited_html,
                'edited_text': plain_text(result.edited_html) if result.edited_html is not None else None,
                'content_type': result.content_type, 'classification_reason': result.reason,
                'media_type': media_type, 'media_path': str(final) if final else None,
                'published_at': date, 'status': 'FILTERED' if filtered else 'NEW',
            }, temporary, final)
            if not completed:
                await journal.defer(claim, 'claim_expired_or_source_disabled')
            return completed
    except asyncio.CancelledError:
        try:
            await finish_cleanup(journal.defer(claim, 'cancelled'))
        except Exception as error:
            logger.warning('Capture cancellation outcome deferred: post_id=%s exception=%s', marker_id, type(error).__name__)
        raise
    except Exception as error:
        logger.error('Scalar capture deferred: post_id=%s exception=%s', marker_id, type(error).__name__)
        try:
            await journal.defer(claim, type(error).__name__)
        except Exception as database_error:
            logger.warning('Capture retry persistence deferred: post_id=%s exception=%s', marker_id, type(database_error).__name__)
        return False
    finally:
        await finish_cleanup(cleanup())


async def persist_album(
    session_factory: async_sessionmaker,
    source: Source,
    grouped_id: int,
    messages: list[Any],
    media_client: Any,
    media_dir: str | Path = "./data/media",
    ai_editor: Any | None = None,
    protection_check: Any | None = None,
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
    markup_captions: list[str] = []
    normalization_failed = False
    for message in ordered_messages:
        caption = getattr(message, "message", None)
        if isinstance(caption, str) and caption.strip():
            markup, failed = _normalize_source(message, source.id)
            normalization_failed |= failed
            # Failed fragments cannot be deduplicated by their lost entity data.
            if failed or markup not in markup_captions:
                captions.append(caption)
                markup_captions.append(markup or "")
    original_text = "\n\n".join(captions) if captions else None
    published_at = getattr(ordered_messages[0], "date", None)
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)

    source_html = "\n\n".join(markup_captions) if captions else None
    result = (
        uncertain(source_html, "Source album entity normalization failed")
        if normalization_failed
        else await _process_text(session_factory, source, source_html, ai_editor, grouped_id)
    )
    filtered = result.content_type in {"AD", "SELF_PROMO"}

    if protection_check is not None:
        protection = await protection_check(source.telegram_chat_id, [message.id for message in ordered_messages])
        if protection is not False:
            await mark_post_protected(
                session_factory, source, representative_id, media_dir,
                grouped_id=grouped_id, protection_known=protection is True,
            )
            return False

    saved_items: list[tuple[Any, str, str, int]] = []
    for position, message in enumerate(ordered_messages if not filtered else []):
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
            sanitized_text=original_text,
            source_html=source_html,
            edited_html=result.edited_html,
            edited_text=plain_text(result.edited_html) if result.edited_html is not None else None,
            content_type=result.content_type,
            classification_reason=result.reason,
            media_type="album",
            category_id=source.category_id,
            published_at=published_at,
            captured_at=datetime.now(timezone.utc),
            status="FILTERED" if filtered else "NEW",
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
        self._recovery_cursor = 0
        self._capture_journal = CaptureJournal(session_factory)
        self._accepting = True
        self._event_handler = None
        self._event_tasks: set[asyncio.Task] = set()
        self._album_tasks: set[asyncio.Task] = set()
        self._album_processing: set[tuple[int, int]] = set()
        self._album_blocked: set[tuple[int, int]] = set()
        self._close_task: asyncio.Task | None = None
        self._shutdown_timeout = 60.0

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

    async def protection_for_source(
        self, telegram_chat_id: int, message_ids: list[int] | None = None
    ) -> bool | None:
        """Check source and original-message flags before review or publication."""
        try:
            chat = await self._client.get_entity(telegram_chat_id)
        except Exception:
            return None
        flag = getattr(chat, "noforwards", None)
        if flag is True:
            return True
        if flag is not False:
            return None
        if not message_ids:
            return False
        try:
            messages = await self._client.get_messages(chat, ids=message_ids)
        except Exception:
            return None
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        by_id = {getattr(item, "id", None): item for item in messages if item is not None}
        if any(identity not in by_id for identity in message_ids):
            return None
        states = [getattr(by_id[identity], "noforwards", None) for identity in message_ids]
        if any(state is True for state in states):
            return True
        if all(state is False for state in states):
            return False
        return None

    async def recover_pending_captures(self, *, limit: int = 10) -> int:
        """Retry a bounded fair batch; backoff and leases survive restarts."""
        if not self._accepting:
            return 0
        await self._capture_journal.exhaust_expired()
        timestamp = capture_now()
        async with self._session_factory() as session:
            query = (
                select(SourcePost.id, SourcePost.status)
                .join(Source, Source.id == SourcePost.source_id)
                .where(
                    SourcePost.status.in_([CAPTURE_PENDING, PROTECTION_CAPTURE_PENDING]),
                    SourcePost.grouped_id.is_(None), Source.enabled.is_(True),
                    or_(SourcePost.capture_next_attempt_at.is_(None), SourcePost.capture_next_attempt_at <= timestamp),
                    or_(SourcePost.capture_lease_until.is_(None), SourcePost.capture_lease_until <= timestamp),
                ).order_by(SourcePost.id).limit(limit)
            )
            rows = list((await session.execute(query.where(SourcePost.id > self._recovery_cursor))).all())
            if not rows and self._recovery_cursor:
                self._recovery_cursor = 0
                rows = list((await session.execute(query)).all())
        recovered = 0
        started = asyncio.get_running_loop().time()
        for marker_id, status in rows:
            if not self._accepting or asyncio.get_running_loop().time() - started >= 15:
                break
            self._recovery_cursor = marker_id
            try:
                if await (self._recover_scalar_capture(marker_id) if status == CAPTURE_PENDING
                          else self._recover_pending_capture(marker_id)):
                    recovered += 1
            except Exception as error:
                logger.warning('Capture recovery deferred: source_post_id=%s exception=%s', marker_id, type(error).__name__)
        return recovered

    async def _recover_scalar_capture(self, marker_id: int) -> bool:
        async with self._processing_lock:
            claim = await self._capture_journal.claim(marker_id)
            if claim is None:
                return False
            try:
                async with asyncio.timeout(PROCESS_TIMEOUT):
                    async with self._session_factory() as session:
                        marker = await session.get(SourcePost, marker_id)
                        source = await session.get(Source, marker.source_id)
                    if source is None or not source.enabled:
                        await self._capture_journal.defer(claim, 'source_disabled')
                        return False
                    chat = await self._client.get_entity(source.telegram_chat_id)
                    if getattr(chat, 'noforwards', None) is not False:
                        protected = getattr(chat, 'noforwards', None) is True
                        await self._capture_journal.defer(claim, 'protected' if protected else 'protection_unknown',
                            terminal_status='PROTECTED' if protected else None)
                        return False
                    message = await self._client.get_messages(chat, ids=marker.telegram_message_id)
                    if isinstance(message, (list, tuple)):
                        message = next((item for item in message if getattr(item, 'id', None) == marker.telegram_message_id), None)
                    if message is None or isinstance(message, types.MessageEmpty):
                        await self._capture_journal.defer(claim, 'message_missing', terminal_status='CAPTURE_MISSING')
                        return False
                    if getattr(message, 'id', None) != marker.telegram_message_id:
                        raise RuntimeError('Unexpected Telegram message identity')
                    state = content_protection_state(chat, message)
                    if state is not False:
                        await self._capture_journal.defer(claim, 'protected' if state else 'protection_unknown',
                            terminal_status='PROTECTED' if state is True else None)
                        return False
                    if getattr(message, 'grouped_id', None) is not None:
                        await self._capture_journal.defer(claim, 'album_membership_unverified',
                            terminal_status=PROTECTION_ALBUM_PENDING)
                        return False
                    return await persist_message(
                        self._session_factory, source, message, media_client=self._client,
                        media_dir=self._media_dir, ai_editor=self._ai_editor, capture_claim=claim,
                        protection_check=self.protection_for_source,
                    )
            except asyncio.CancelledError:
                try:
                    await finish_cleanup(self._capture_journal.defer(claim, 'cancelled'))
                except Exception as error:
                    logger.warning('Capture cancellation deferred: post_id=%s exception=%s', marker_id, type(error).__name__)
                raise
            except Exception as error:
                permanent = isinstance(error, (errors.ChannelPrivateError, errors.ChatAdminRequiredError,
                                                errors.MessageIdInvalidError))
                await self._capture_journal.defer(claim, type(error).__name__,
                    terminal_status='CAPTURE_FAILED' if permanent else None)
                logger.warning('Scalar recovery deferred: post_id=%s exception=%s', marker_id, type(error).__name__)
                return False

    async def _recover_pending_capture(self, marker_id: int) -> bool:
        async with self._processing_lock:
            async with self._session_factory() as session:
                marker = await session.get(SourcePost, marker_id)
                if marker is None or marker.status != PROTECTION_CAPTURE_PENDING:
                    return False
                source = await session.get(Source, marker.source_id)
                if source is None or not source.enabled:
                    return False
                source_chat_id = source.telegram_chat_id
                message_id = marker.telegram_message_id

            chat = await self._client.get_entity(source_chat_id)
            if getattr(chat, "noforwards", None) is True:
                await mark_post_protected(
                    self._session_factory,
                    source,
                    message_id,
                    self._media_dir,
                    protection_known=True,
                )
                return False
            if getattr(chat, "noforwards", None) is not False:
                return False

            message = await self._client.get_messages(chat, ids=message_id)
            if isinstance(message, (list, tuple)):
                message = next((item for item in message if getattr(item, "id", None) == message_id), None)
            if message is None:
                async with self._session_factory() as session:
                    marker = await session.get(SourcePost, marker_id)
                    if marker is not None and marker.status == PROTECTION_CAPTURE_PENDING:
                        marker.status = "SKIPPED"
                        marker.classification_reason = "Original Telegram message is unavailable; capture skipped."
                        await session.commit()
                return False
            if getattr(message, "id", None) != message_id:
                return False

            state = content_protection_state(chat, message)
            if state is True:
                await mark_post_protected(
                    self._session_factory,
                    source,
                    message_id,
                    self._media_dir,
                    protection_known=True,
                )
                return False
            if state is not False:
                return False
            if getattr(message, "grouped_id", None) is not None:
                async with self._session_factory() as session:
                    marker = await session.get(SourcePost, marker_id)
                    if marker is not None and marker.status == PROTECTION_CAPTURE_PENDING:
                        marker.status = PROTECTION_ALBUM_PENDING
                        await session.commit()
                return False

            # Recheck that source management did not disable it during the fetch.
            async with self._session_factory() as session:
                marker = await session.get(SourcePost, marker_id)
                source = await session.get(Source, source.id)
                if (
                    marker is None
                    or marker.status != PROTECTION_CAPTURE_PENDING
                    or source is None
                    or not source.enabled
                ):
                    return False
            return await persist_message(
                self._session_factory,
                source,
                message,
                media_client=self._client,
                media_dir=self._media_dir,
                ai_editor=self._ai_editor,
                recovery_marker_id=marker_id,
                protection_check=self.protection_for_source,
            )

    async def run(self) -> None:
        """Let Telethon reconnect internally; bound retries after an escaped failure."""
        for attempt in range(3):
            if not self._accepting:
                return
            try:
                await self._run_once()
                if not self._accepting:
                    return
                raise ConnectionError("Telegram monitor disconnected unexpectedly")
            except (OSError, TimeoutError, errors.ServerError, errors.FloodWaitError) as error:
                self._ready.clear()
                if not self._accepting or attempt == 2:
                    raise
                delay = getattr(error, "seconds", 2 ** attempt)
                logger.warning("Telegram monitor reconnecting: attempt=%s exception=%s", attempt + 1, type(error).__name__)
                await asyncio.sleep(delay)
            finally:
                self._ready.clear()

    async def _run_once(self) -> None:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("Telethon session is not authorized. Run: ./venv/bin/python scripts/authorize_telegram.py")
        await require_user_account(self._client)
        await self.refresh_enabled_sources()
        self._ready.set()
        logger.info("Telethon user session connected")

        async def on_new_message(event: Any) -> None:
            if not self._accepting:
                return
            task = asyncio.current_task()
            self._event_tasks.add(task)
            try:
                source = self._source_by_chat_id.get(event.chat_id)
                if source is None:
                    return
                try:
                    chat = await event.get_chat()
                except Exception:
                    chat = None
                await self._handle_source_message(source, event.message, chat)
            finally:
                self._event_tasks.discard(task)

        self._event_handler = on_new_message
        self._client.add_event_handler(on_new_message, events.NewMessage())
        try:
            await self._client.run_until_disconnected()
        finally:
            self._client.remove_event_handler(on_new_message)
            self._event_handler = None

    async def _handle_source_message(self, source: Source, message: Any, chat: Any) -> None:
        protection_state = content_protection_state(chat, message)
        if protection_state is not False:
            grouped_id = getattr(message, "grouped_id", None)
            if grouped_id is not None:
                await self._discard_album_batch(source.id, grouped_id)
            await mark_post_protected(
                self._session_factory,
                source,
                message.id,
                self._media_dir,
                grouped_id=grouped_id,
                protection_known=protection_state is True,
            )
            logger.info(
                "Source post skipped by Telegram content-protection guard: source_id=%s message_id=%s result=%s",
                source.id,
                message.id,
                "protected" if protection_state is True else "metadata_unavailable",
            )
            return
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
                    protection_check=self.protection_for_source,
                )
            if inserted:
                logger.info("Stored source post: source_id=%s message_id=%s", source.id, message.id)
            else:
                logger.info(
                    "Scalar capture deferred or already recorded: source_id=%s message_id=%s",
                    source.id,
                    message.id,
                )
        except Exception as error:
            logger.error('Scalar receipt/capture failed: source_id=%s message_id=%s exception=%s',
                         source.id, message.id, type(error).__name__)

    async def _block_incomplete_album(self, source: Source, grouped_id: int, message_id: int) -> None:
        """Quarantine an uncertain album without deleting its captured content."""
        async with self._session_factory() as session:
            post = await session.scalar(select(SourcePost).where(
                SourcePost.source_id == source.id, SourcePost.grouped_id == grouped_id,
            ))
            if post is None:
                session.add(SourcePost(
                    source_id=source.id, telegram_message_id=message_id, grouped_id=grouped_id,
                    category_id=source.category_id, status="ALBUM_INCOMPLETE",
                    classification_reason="Album capture was interrupted or received late fragments; manual investigation required.",
                ))
            elif post.status in {"NEW", "REVIEW"}:
                post.status = "ALBUM_INCOMPLETE"
            await session.commit()
        logger.warning("Album blocked as incomplete: source_id=%s grouped_id=%s", source.id, grouped_id)

    async def _collect_album_message(self, source: Source, message: Any, grouped_id: int) -> None:
        key = (source.id, grouped_id)
        if not self._accepting and asyncio.current_task() not in self._event_tasks:
            return
        if key in self._album_blocked:
            return
        # A completed album must never be rewritten using a later fragment.
        if key not in self._album_batches:
            late = False
            async with self._session_factory() as session:
                post = await session.scalar(select(SourcePost).where(
                    SourcePost.source_id == source.id, SourcePost.grouped_id == grouped_id,
                ))
                if post is not None:
                    known_ids = {post.telegram_message_id, *(item.telegram_message_id for item in post.media_items)}
                    late = message.id not in known_ids and post.status in {"NEW", "REVIEW"}
            if post is not None:
                if late:
                    await self._block_incomplete_album(source, grouped_id, message.id)
                return
        late_task = None
        async with self._album_lock:
            current = self._album_batches.get(key)
            messages = current[1] if current else {}
            if message.id in messages:
                return
            messages[message.id] = message
            if key in self._album_processing:
                self._album_blocked.add(key)
                late_task = current[2]
                late_task.cancel()
            else:
                if current:
                    current[2].cancel()
                task = asyncio.create_task(
                    self._finalize_album_after_delay(key), name=f"postradar-album-{source.id}-{grouped_id}",
                )
                self._album_tasks.add(task)
                task.add_done_callback(self._album_tasks.discard)
                self._album_batches[key] = (source, messages, task)
        if late_task is not None:
            await asyncio.gather(late_task, return_exceptions=True)
            await self._block_incomplete_album(source, grouped_id, min(messages))

    async def _discard_album_batch(self, source_id: int, grouped_id: int) -> None:
        key = (source_id, grouped_id)
        async with self._album_lock:
            self._album_blocked.add(key)
            batch = self._album_batches.pop(key, None)
            if batch is not None:
                batch[2].cancel()
        if batch is not None:
            await asyncio.gather(batch[2], return_exceptions=True)

    async def _finalize_album_after_delay(self, key: tuple[int, int]) -> None:
        completed = False
        processing_started = False
        batch = None
        try:
            await asyncio.sleep(self._album_collection_delay)
            async with self._album_lock:
                batch = self._album_batches.get(key)
                if batch is None or batch[2] is not asyncio.current_task() or key in self._album_blocked:
                    return
                self._album_processing.add(key)
                processing_started = True
            await self._process_album(key, batch[0], list(batch[1].values()))
            completed = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("Album processing failed: source_id=%s grouped_id=%s exception=%s", key[0], key[1], type(error).__name__)
        finally:
            if processing_started:
                self._album_processing.discard(key)
            if completed and self._album_batches.get(key) is batch:
                self._album_batches.pop(key, None)

    async def _process_album(self, key: tuple[int, int], source: Source, messages: list[Any]) -> None:
        async with self._processing_lock:
            if key in self._album_blocked:
                return
            protection = await self.protection_for_source(source.telegram_chat_id, [message.id for message in messages])
            if protection is not False:
                self._album_blocked.add(key)
                await mark_post_protected(
                    self._session_factory, source, min(message.id for message in messages),
                    self._media_dir, grouped_id=key[1], protection_known=protection is True,
                )
                return
            await persist_album(
                self._session_factory, source, key[1], messages,
                media_client=self._client, media_dir=self._media_dir, ai_editor=self._ai_editor,
                protection_check=self.protection_for_source,
            )

    async def _flush_pending_albums(self) -> None:
        async with self._album_lock:
            for key, (_source, _messages, task) in self._album_batches.items():
                if key not in self._album_processing:
                    task.cancel()
            tasks = list(self._album_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for key, (source, messages, _task) in list(self._album_batches.items()):
            if key in self._album_blocked:
                self._album_batches.pop(key, None)
                continue
            try:
                await self._process_album(key, source, list(messages.values()))
                self._album_batches.pop(key, None)
            except Exception as error:
                logger.error("Album shutdown drain failed: source_id=%s grouped_id=%s exception=%s", source.id, key[1], type(error).__name__)
                await self._block_incomplete_album(source, key[1], min(messages))

    async def _close(self) -> None:
        self._accepting = False
        self._ready.clear()
        if self._event_handler is not None:
            self._client.remove_event_handler(self._event_handler)
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                if self._event_tasks:
                    await asyncio.gather(*list(self._event_tasks), return_exceptions=True)
                await self._flush_pending_albums()
        except TimeoutError:
            logger.error("Source shutdown drain timed out; buffered work requires investigation")
            tasks = list(self._event_tasks | self._album_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for key, (source, messages, _task) in list(self._album_batches.items()):
                await self._block_incomplete_album(source, key[1], min(messages))
        finally:
            await self._client.disconnect()

    async def close(self) -> None:
        """Stop admission, join owned work, then disconnect; cancellation is preserved."""
        if self._close_task is None:
            self._accepting = False
            self._close_task = asyncio.create_task(self._close(), name="telethon-source-close")
        await finish_cleanup(self._close_task)
