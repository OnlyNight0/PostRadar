"""Scalar capture receipts, bounded retries, and fenced SQLite ownership."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from postradar.db.models import Source, SourcePost, SourcePostMedia
from postradar.services.media import delete_media_file

logger = logging.getLogger(__name__)
CAPTURE_PENDING = 'CAPTURE_PENDING'
MAX_ATTEMPTS = 5
LEASE_SECONDS = 300
PROCESS_TIMEOUT = 240


def capture_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class CaptureClaim:
    post_id: int
    token: str
    attempt: int
    category_id: int | None


class CaptureJournal:
    def __init__(self, factory: async_sessionmaker) -> None:
        self.factory = factory

    async def receipt(self, source: Source, message_id: int) -> int:
        """Commit only identity/routing before any AI or download work."""
        async with self.factory() as session:
            existing = await session.scalar(select(SourcePost).where(
                SourcePost.source_id == source.id, SourcePost.telegram_message_id == message_id,
            ))
            if existing is not None:
                return existing.id
            post = SourcePost(source_id=source.id, telegram_message_id=message_id,
                              category_id=source.category_id, status=CAPTURE_PENDING,
                              capture_attempts=0)
            session.add(post)
            try:
                await session.commit()
                return post.id
            except IntegrityError:
                await session.rollback()
                existing_id = await session.scalar(select(SourcePost.id).where(
                    SourcePost.source_id == source.id, SourcePost.telegram_message_id == message_id,
                ))
                if existing_id is None:
                    raise
                return existing_id

    async def claim(self, post_id: int, *, verified_protection_marker: bool = False) -> CaptureClaim | None:
        timestamp = capture_now()
        statuses = [CAPTURE_PENDING]
        if verified_protection_marker:
            statuses.append('PROTECTION_CAPTURE_PENDING')
        async with self.factory() as session:
            eligible = (
                (SourcePost.id == post_id) & SourcePost.status.in_(statuses)
                & SourcePost.grouped_id.is_(None)
                & or_(SourcePost.capture_next_attempt_at.is_(None), SourcePost.capture_next_attempt_at <= timestamp)
                & or_(SourcePost.capture_lease_until.is_(None), SourcePost.capture_lease_until <= timestamp)
                & (func.coalesce(SourcePost.capture_attempts, 0) < MAX_ATTEMPTS)
                & exists(select(Source.id).where(Source.id == SourcePost.source_id, Source.enabled.is_(True)))
            )
            token = uuid4().hex
            claimed = await session.execute(update(SourcePost).where(eligible).values(
                status=CAPTURE_PENDING, capture_token=token,
                capture_lease_until=timestamp + timedelta(seconds=LEASE_SECONDS),
                capture_attempts=func.coalesce(SourcePost.capture_attempts, 0) + 1,
            ))
            if claimed.rowcount != 1:
                return None
            post = await session.get(SourcePost, post_id)
            await session.commit()
            return CaptureClaim(post_id, token, post.capture_attempts, post.category_id)

    @staticmethod
    def fence(claim: CaptureClaim) -> ColumnElement[bool]:
        return (
            (SourcePost.id == claim.post_id) & (SourcePost.status == CAPTURE_PENDING)
            & (SourcePost.capture_token == claim.token)
            & (SourcePost.capture_lease_until > capture_now())
        )

    async def defer(self, claim: CaptureClaim, error_code: str,
                    *, terminal_status: str | None = None) -> bool:
        exhausted = claim.attempt >= MAX_ATTEMPTS
        status = terminal_status or ('CAPTURE_FAILED' if exhausted else CAPTURE_PENDING)
        async with self.factory() as session:
            changed = await session.execute(update(SourcePost).where(self.fence(claim)).values(
                status=status, capture_error=error_code[:120], capture_token=None, capture_lease_until=None,
                capture_next_attempt_at=capture_now() + timedelta(seconds=min(3600, 30 * 2 ** (claim.attempt - 1)))
                    if status == CAPTURE_PENDING else None,
            ))
            await session.commit()
            return changed.rowcount == 1

    async def complete(self, claim: CaptureClaim, values: dict[str, Any],
                       temporary: Path | None = None, final: Path | None = None) -> bool:
        """Fence before file promotion; a unique final filename belongs only to this claim."""
        async with self.factory() as session:
            changed = await session.execute(update(SourcePost).where(
                self.fence(claim), exists(select(Source.id).where(
                    Source.id == SourcePost.source_id, Source.enabled.is_(True))),
            ).values(**values, capture_token=None, capture_lease_until=None,
                     capture_next_attempt_at=None, capture_error=None))
            if changed.rowcount != 1:
                return False
            if temporary is not None:
                if final is None:
                    raise ValueError("Capture media requires a final path")
                temporary.replace(final)
            await session.commit()
            return True

    async def cleanup_owned_file(self, media_dir: str | Path, path: Path) -> None:
        """A failed commit may have succeeded. Never unlink if reference checks fail."""
        try:
            async with self.factory() as session:
                referenced = await session.scalar(select(SourcePost.id).where(SourcePost.media_path == str(path)).limit(1))
                if referenced is None:
                    referenced = await session.scalar(select(SourcePostMedia.id).where(SourcePostMedia.media_path == str(path)).limit(1))
            if referenced is None:
                delete_media_file(media_dir, path)
        except Exception as error:
            logger.warning('Capture file cleanup deferred: exception=%s', type(error).__name__)

    async def exhaust_expired(self) -> None:
        async with self.factory() as session:
            timestamp = capture_now()
            await session.execute(update(SourcePost).where(
                SourcePost.status == CAPTURE_PENDING, SourcePost.capture_attempts >= MAX_ATTEMPTS,
                or_(SourcePost.capture_lease_until.is_(None), SourcePost.capture_lease_until <= timestamp),
            ).values(status='CAPTURE_FAILED', capture_token=None, capture_lease_until=None,
                     capture_next_attempt_at=None, capture_error='retry_exhausted'))
            await session.commit()
