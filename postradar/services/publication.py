"""Durable publication receipts. Unknown sends are never replayed automatically."""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
    TelegramUnauthorizedError, TelegramEntityTooLarge, TelegramRetryAfter,
)
from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from postradar.db.models import PublishAttempt, PublishAttemptPart, SourcePost
from postradar.lifecycle import finish_cleanup

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 60
LEASE_SECONDS = 300  # Longer than a bounded request and SQLite's busy timeout.
# Installed aiogram raises these only for parsed explicit rejection responses.
# Network/decoder/server exceptions and arbitrary exceptions remain ambiguous.
DEFINITE_REJECTIONS = (
    TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
    TelegramUnauthorizedError, TelegramEntityTooLarge, TelegramRetryAfter,
)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class PublicationPart:
    kind: str
    payload_hash: str
    send: Callable[[], Awaitable[Any]]
    expected_messages: int = 1


class PublicationPreflightError(ValueError):
    """No Telegram request has been issued."""


class PublicationJournal:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.factory = session_factory
        self.owner_id = uuid4().hex

    async def publish(
        self, post_id: int,
        prepare: Callable[[AsyncSession, SourcePost], Awaitable[tuple[int, list[PublicationPart]]]],
    ) -> str:
        attempt_id = uuid4().hex
        try:
            async with self.factory() as session:
                claimed = await session.execute(update(SourcePost).where(
                    SourcePost.id == post_id, SourcePost.status == "REVIEW",
                ).values(status="PUBLISHING"))
                if claimed.rowcount != 1:
                    post = await session.get(SourcePost, post_id)
                    return post.status if post else "missing"
                post = await session.get(SourcePost, post_id)
                destination, parts = await prepare(session, post)
                if not parts:
                    raise PublicationPreflightError("failed")
                started = now()
                session.add(PublishAttempt(
                    id=attempt_id, source_post_id=post_id, destination_id=destination,
                    owner_id=self.owner_id, state="ACTIVE", started_at=started,
                    lease_until=started + timedelta(seconds=LEASE_SECONDS),
                    payload_hash=fingerprint([destination, [part.payload_hash for part in parts]]),
                    parts=[PublishAttemptPart(position=i, kind=part.kind,
                        payload_hash=part.payload_hash, state="READY") for i, part in enumerate(parts)],
                ))
                await session.commit()  # Claim and plan are durable before any send.
        except PublicationPreflightError as error:
            return str(error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("Publication claim failed: post_id=%s exception=%s", post_id, type(error).__name__)
            return "failed"  # A possibly committed claim remains blocked until recovery.

        for position, part in enumerate(parts):
            try:
                await self._start_part(attempt_id, position)
            except asyncio.CancelledError:
                await finish_cleanup(self._stop_safely(attempt_id, "CancelledError", position, not_sent=True))
                raise
            except Exception as error:
                return await self._stop_safely(attempt_id, type(error).__name__, position, not_sent=True)
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    response = await part.send()
            except asyncio.CancelledError:
                await finish_cleanup(self._stop_safely(attempt_id, "CancelledError"))
                raise
            except Exception as error:
                definite = isinstance(error, DEFINITE_REJECTIONS)
                result = await self._stop_safely(attempt_id, type(error).__name__, position, definite)
                logger.error("Publication send stopped: post_id=%s part=%s exception=%s outcome=%s",
                             post_id, position, type(error).__name__, result)
                if result == "failed" and isinstance(error, (TelegramForbiddenError, TelegramUnauthorizedError)):
                    return "permission_error"
                return result
            try:
                responses = response if isinstance(response, (list, tuple)) else [response]
                ids = [message.message_id for message in responses]
                if (len(ids) != part.expected_messages
                        or any(type(identity) is not int or identity <= 0 for identity in ids)
                        or len(set(ids)) != len(ids)):
                    raise ValueError("Telegram acknowledgment lacks usable message IDs")
                await self._confirm_part(attempt_id, position, ids)
            except asyncio.CancelledError:
                await finish_cleanup(self._stop_safely(attempt_id, "CancelledError"))
                raise
            except Exception as error:
                # Even a positive acknowledgment is ambiguous after a failed receipt commit.
                await self._stop_safely(attempt_id, type(error).__name__)
                return "PUBLISH_UNCERTAIN"
        try:
            return await self._complete(attempt_id)
        except asyncio.CancelledError:
            await finish_cleanup(self._stop_safely(attempt_id, "CancelledError"))
            raise
        except Exception as error:
            await self._stop_safely(attempt_id, type(error).__name__)
            return "PUBLISH_UNCERTAIN"

    async def _start_part(self, attempt_id: str, position: int) -> None:
        async with self.factory() as session:
            timestamp = now()
            renewed = await session.execute(update(PublishAttempt).where(
                PublishAttempt.id == attempt_id, PublishAttempt.owner_id == self.owner_id,
                PublishAttempt.state == "ACTIVE", PublishAttempt.lease_until > timestamp,
            ).values(lease_until=timestamp + timedelta(seconds=LEASE_SECONDS)))
            attempt = await session.get(PublishAttempt, attempt_id)
            post = await session.get(SourcePost, attempt.source_post_id)
            if renewed.rowcount != 1 or post.status != "PUBLISHING":
                raise RuntimeError("Publication no longer owned or eligible")
            changed = await session.execute(update(PublishAttemptPart).where(
                PublishAttemptPart.attempt_id == attempt_id, PublishAttemptPart.position == position,
                PublishAttemptPart.state == "READY",
            ).values(state="STARTED", started_at=timestamp))
            if changed.rowcount != 1:
                raise RuntimeError("Publication part already attempted")
            await session.commit()

    async def _confirm_part(self, attempt_id: str, position: int, ids: list[int]) -> None:
        async with self.factory() as session:
            changed = await session.execute(update(PublishAttemptPart).where(
                PublishAttemptPart.attempt_id == attempt_id, PublishAttemptPart.position == position,
                PublishAttemptPart.state.in_(["STARTED", "UNKNOWN"]),
            ).values(state="CONFIRMED", telegram_message_ids=json.dumps(ids), confirmed_at=now()))
            if changed.rowcount != 1:
                raise RuntimeError("Publication receipt cannot be recorded")
            await session.commit()

    async def _stop_safely(self, attempt_id: str, error_type: str,
                           position: int | None = None, definite: bool = False,
                           not_sent: bool = False) -> str:
        try:
            return await self._stop(attempt_id, error_type, position, definite, not_sent)
        except Exception as error:
            logger.error("Publication outcome persistence failed: attempt=%s exception=%s", attempt_id, type(error).__name__)
            return "PUBLISH_UNCERTAIN"

    async def _stop(self, attempt_id: str, error_type: str,
                    position: int | None = None, definite: bool = False,
                    not_sent: bool = False) -> str:
        async with self.factory() as session:
            # Acquire a write claim before reading parts, so recovery cannot race this decision.
            locked = await session.execute(update(PublishAttempt).where(
                PublishAttempt.id == attempt_id, PublishAttempt.state == "ACTIVE",
                PublishAttempt.owner_id == self.owner_id,
            ).values(error_type=error_type[:120]))
            if locked.rowcount != 1:
                return "PUBLISH_UNCERTAIN"
            attempt = await session.get(PublishAttempt, attempt_id)
            for part in attempt.parts:
                if part.state == "STARTED":
                    part.state = (
                        "NOT_SENT" if not_sent and part.position == position else
                        "REJECTED" if definite and part.position == position else "UNKNOWN"
                    )
            unsafe = any(part.state in {"CONFIRMED", "UNKNOWN", "STARTED"} for part in attempt.parts)
            attempt.state = "UNKNOWN" if unsafe else "ABORTED"
            attempt.completed_at = now()
            attempt.lease_until = now()
            await session.execute(update(SourcePost).where(
                SourcePost.id == attempt.source_post_id, SourcePost.status == "PUBLISHING",
            ).values(status="PUBLISH_UNCERTAIN" if unsafe else "REVIEW"))
            await session.commit()
            return "PUBLISH_UNCERTAIN" if unsafe else "failed"

    async def _complete(self, attempt_id: str) -> str:
        async with self.factory() as session:
            locked = await session.execute(update(PublishAttempt).where(
                PublishAttempt.id == attempt_id, PublishAttempt.state == "ACTIVE",
                PublishAttempt.owner_id == self.owner_id,
            ).values(completed_at=now()))
            if locked.rowcount != 1:
                return "PUBLISH_UNCERTAIN"
            attempt = await session.get(PublishAttempt, attempt_id)
            if not all(part.state == "CONFIRMED" for part in attempt.parts):
                raise RuntimeError("Publication is not fully acknowledged")
            changed = await session.execute(update(SourcePost).where(
                SourcePost.id == attempt.source_post_id, SourcePost.status == "PUBLISHING",
            ).values(status="PUBLISHED"))
            if changed.rowcount != 1:
                raise RuntimeError("Publication post no longer eligible")
            attempt.state = "CONFIRMED"
            await session.commit()
            return "published"

    async def recover_expired(self, *, limit: int = 20) -> int:
        """Recover stale claims without stealing a live process's bounded send."""
        timestamp = now()
        # All receipts survived, but the final post-status commit may have failed.
        confirmed_unknown = (PublishAttempt.state == "UNKNOWN") & exists(select(PublishAttemptPart.id).where(
            PublishAttemptPart.attempt_id == PublishAttempt.id,
        )) & ~exists(select(PublishAttemptPart.id).where(
            PublishAttemptPart.attempt_id == PublishAttempt.id,
            PublishAttemptPart.state != "CONFIRMED",
        ))
        eligible = or_(
            (PublishAttempt.state == "ACTIVE") & (PublishAttempt.lease_until <= timestamp),
            confirmed_unknown,
        )
        async with self.factory() as session:
            ids = list((await session.scalars(select(PublishAttempt.id).where(
                eligible,
            ).order_by(PublishAttempt.started_at).limit(limit))).all())
        recovered = 0
        for identity in ids:
            async with self.factory() as session:
                claimed = await session.execute(update(PublishAttempt).where(
                    PublishAttempt.id == identity, eligible,
                ).values(completed_at=timestamp))
                if claimed.rowcount != 1:
                    continue
                attempt = await session.get(PublishAttempt, identity)
                for part in attempt.parts:
                    if part.state == "STARTED":
                        part.state = "UNKNOWN"
                all_confirmed = bool(attempt.parts) and all(part.state == "CONFIRMED" for part in attempt.parts)
                unsafe = any(part.state in {"UNKNOWN", "CONFIRMED"} for part in attempt.parts)
                attempt.state = "CONFIRMED" if all_confirmed else "UNKNOWN" if unsafe else "ABORTED"
                status = "PUBLISHED" if all_confirmed else "PUBLISH_UNCERTAIN" if unsafe else "REVIEW"
                await session.execute(update(SourcePost).where(
                    SourcePost.id == attempt.source_post_id,
                    SourcePost.status.in_(["PUBLISHING", "PUBLISH_UNCERTAIN"]),
                ).values(status=status))
                await session.commit()
                recovered += 1
        return recovered

    async def unresolved(self, *, limit: int = 20) -> list[PublishAttempt]:
        async with self.factory() as session:
            return list((await session.scalars(select(PublishAttempt).where(
                PublishAttempt.state.in_(["ACTIVE", "UNKNOWN"]),
            ).order_by(PublishAttempt.started_at).limit(limit))).all())

    async def confirm_published(self, attempt_id: str, admin_id: int) -> str:
        """Record an operator's destination check; never send or invent API receipts."""
        async with self.factory() as session:
            claimed = await session.execute(update(PublishAttempt).where(
                PublishAttempt.id == attempt_id, PublishAttempt.state == "UNKNOWN",
            ).values(state="RESOLVED_PUBLISHED", completed_at=now(), resolved_by=admin_id))
            if claimed.rowcount != 1:
                return "not_uncertain"
            attempt = await session.get(PublishAttempt, attempt_id)
            changed = await session.execute(update(SourcePost).where(
                SourcePost.id == attempt.source_post_id, SourcePost.status == "PUBLISH_UNCERTAIN",
            ).values(status="PUBLISHED"))
            if changed.rowcount != 1:
                await session.rollback()
                return "not_uncertain"
            await session.commit()
            return "published"
