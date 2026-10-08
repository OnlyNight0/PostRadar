"""Offline publication crash boundaries using isolated SQLite and mocked Bot API."""

import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from postradar.bot.handlers import create_admin_router
from postradar.bot.review import AdminWorkflow
from postradar.db.base import Base
from postradar.db.models import PublishAttempt, PublishAttemptPart, Source, SourcePost
from postradar.db.session import init_db
from postradar.services.publication import now


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.url = f"sqlite+aiosqlite:///{self.directory.name}/test.db"
        self.engine = create_async_engine(self.url, connect_args={"timeout": 5})
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.bot = SimpleNamespace(**{name: AsyncMock(return_value=SimpleNamespace(message_id=91))
            for name in ('send_message', 'send_photo', 'send_video', 'send_document', 'send_media_group')})
        self.monitor = SimpleNamespace(protection_for_source=AsyncMock(return_value=False))
        self.workflow = self.new_workflow()
        async with self.factory() as session:
            source = Source(telegram_chat_id=-1001, destination_channel_id=-1002)
            session.add(source)
            await session.flush()
            post = SourcePost(source_id=source.id, telegram_message_id=1, status='REVIEW',
                              edited_text='Candidate', original_text='Source', media_type='text')
            session.add(post)
            await session.commit()
            self.post_id, self.source_id = post.id, source.id

    def new_workflow(self):
        # Independent owner/session factory, sharing only the test SQLite database.
        return AdminWorkflow(self.bot, async_sessionmaker(self.engine, expire_on_commit=False), 123,
                             media_dir=self.directory.name, source_monitor=self.monitor)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.directory.cleanup()

    async def post(self):
        async with self.factory() as session:
            return await session.get(SourcePost, self.post_id)

    async def attempt(self):
        async with self.factory() as session:
            return await session.scalar(select(PublishAttempt).where(PublishAttempt.source_post_id == self.post_id))

    async def change(self, **values):
        async with self.factory() as session:
            post = await session.get(SourcePost, self.post_id)
            for key, value in values.items(): setattr(post, key, value)
            await session.commit()

    async def interrupted(self, states, *, expired=True):
        async with self.factory() as session:
            post = await session.get(SourcePost, self.post_id)
            post.status = 'PUBLISHING'
            attempt = PublishAttempt(id=uuid4().hex, source_post_id=self.post_id,
                destination_id=-1002, payload_hash='a' * 64, owner_id='old-owner', state='ACTIVE',
                started_at=now(), lease_until=now() + timedelta(seconds=-1 if expired else 300),
                parts=[PublishAttemptPart(position=i, kind='text', payload_hash='b' * 64,
                    state=state, telegram_message_ids=json.dumps([91 + i]) if state == 'CONFIRMED' else None)
                    for i, state in enumerate(states)])
            session.add(attempt)
            await session.commit()
            return attempt.id

    async def test_single_send_has_durable_claim_and_started_part_before_api(self):
        async def send(**kwargs):
            attempt = await self.attempt()
            self.assertEqual((await self.post()).status, 'PUBLISHING')
            self.assertEqual(attempt.parts[0].state, 'STARTED')
            self.assertEqual(attempt.destination_id, -1002)
            return SimpleNamespace(message_id=91)
        self.bot.send_message.side_effect = send
        self.assertEqual(await self.workflow.publish(self.post_id), 'published')
        attempt = await self.attempt()
        self.assertEqual(attempt.state, 'CONFIRMED')
        self.assertEqual(json.loads(attempt.parts[0].telegram_message_ids), [91])
        self.assertEqual((await self.post()).status, 'PUBLISHED')
        self.assertNotIn('Candidate', attempt.payload_hash)
        self.assertEqual(await self.new_workflow().publish(self.post_id), 'PUBLISHED')
        self.bot.send_message.assert_awaited_once()

    async def test_long_formatted_text_has_ordered_receipts(self):
        await self.change(edited_html='<b>' + 'A' * 5000 + '</b>')
        self.assertEqual(await self.workflow.publish(self.post_id), 'published')
        attempt = await self.attempt()
        self.assertEqual([part.position for part in attempt.parts], [0, 1])
        self.assertTrue(all(part.state == 'CONFIRMED' for part in attempt.parts))
        self.assertEqual(self.bot.send_message.await_count, 2)
        for call in self.bot.send_message.await_args_list:
            self.assertTrue(call.kwargs['text'].startswith('<b>'))
            self.assertTrue(call.kwargs['text'].endswith('</b>'))

    async def test_media_then_text_records_two_parts(self):
        path = Path(self.directory.name) / 'photo.jpg'
        path.write_bytes(b'offline')
        await self.change(media_type='photo', media_path=str(path), edited_text='A' * 1100)
        self.assertEqual(await self.workflow.publish(self.post_id), 'published')
        self.assertEqual([part.kind for part in (await self.attempt()).parts], ['photo', 'text'])
        self.assertFalse(path.exists())

    async def test_missing_media_is_definite_pre_send_failure(self):
        await self.change(media_type='photo', media_path=str(Path(self.directory.name) / 'missing'))
        self.assertEqual(await self.workflow.publish(self.post_id), 'failed')
        self.assertEqual((await self.post()).status, 'REVIEW')
        self.assertIsNone(await self.attempt())
        self.bot.send_photo.assert_not_awaited()

    async def test_claim_commit_failure_never_sends(self):
        with patch.object(AsyncSession, 'commit', new=AsyncMock(side_effect=RuntimeError('database unavailable'))):
            self.assertEqual(await self.workflow.publish(self.post_id), 'failed')
        self.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.post()).status, 'REVIEW')

    async def test_started_commit_ack_failure_still_proves_no_api_call(self):
        original = AsyncSession.commit
        count = 0
        async def commit(session):
            nonlocal count
            count += 1
            await original(session)
            if count == 2: raise RuntimeError('commit response lost before send')
        with patch.object(AsyncSession, 'commit', new=commit):
            self.assertEqual(await self.workflow.publish(self.post_id), 'failed')
        self.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.post()).status, 'REVIEW')
        self.assertEqual((await self.attempt()).parts[0].state, 'NOT_SENT')

    async def test_timeout_is_unknown_and_repeat_is_blocked(self):
        self.bot.send_message.side_effect = TimeoutError('may have been accepted')
        self.assertEqual(await self.workflow.publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.assertEqual((await self.post()).status, 'PUBLISH_UNCERTAIN')
        self.assertEqual((await self.attempt()).parts[0].state, 'UNKNOWN')
        self.assertEqual(await self.new_workflow().publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.bot.send_message.assert_awaited_once()

    async def test_successful_send_then_receipt_commit_failure_blocks_repeat(self):
        original = AsyncSession.commit
        count = 0
        async def commit(session):
            nonlocal count
            count += 1
            if count == 3: raise RuntimeError('receipt commit failed')
            await original(session)
        with patch.object(AsyncSession, 'commit', new=commit):
            self.assertEqual(await self.workflow.publish(self.post_id), 'PUBLISH_UNCERTAIN')
        attempt = await self.attempt()
        self.assertEqual(attempt.parts[0].state, 'UNKNOWN')
        self.assertIsNone(attempt.parts[0].telegram_message_ids)
        self.assertEqual(await self.new_workflow().publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.bot.send_message.assert_awaited_once()

    async def test_final_commit_failure_recovers_all_receipts_without_resending(self):
        original = AsyncSession.commit
        count = 0
        async def commit(session):
            nonlocal count
            count += 1
            if count == 4: raise RuntimeError('final commit failed')
            await original(session)
        with patch.object(AsyncSession, 'commit', new=commit):
            self.assertEqual(await self.workflow.publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.assertEqual((await self.attempt()).parts[0].state, 'CONFIRMED')
        await self.new_workflow().publication.recover_expired()
        self.assertEqual((await self.post()).status, 'PUBLISHED')
        self.bot.send_message.assert_awaited_once()

    async def test_later_chunk_failure_preserves_confirmed_receipt_and_blocks_whole_repeat(self):
        await self.change(edited_text='A' * 5000)
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=91), TimeoutError('unknown')]
        self.assertEqual(await self.workflow.publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.assertEqual([part.state for part in (await self.attempt()).parts], ['CONFIRMED', 'UNKNOWN'])
        self.assertEqual(await self.workflow.publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_explicit_rejection_is_reviewable_but_partial_rejection_is_blocked(self):
        rejection = TelegramForbiddenError(method=SendMessage(chat_id=-1002, text='Candidate'), message='rejected')
        self.bot.send_message.side_effect = rejection
        self.assertEqual(await self.workflow.publish(self.post_id), 'permission_error')
        self.assertEqual((await self.post()).status, 'REVIEW')
        self.assertEqual((await self.attempt()).parts[0].state, 'REJECTED')
        await self.change(edited_text='A' * 5000)
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=91), rejection]
        self.assertEqual(await self.workflow.publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.assertEqual((await self.post()).status, 'PUBLISH_UNCERTAIN')

    async def test_cancellation_during_send_propagates_and_persists_unknown(self):
        entered = asyncio.Event()
        async def send(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.bot.send_message.side_effect = send
        task = asyncio.create_task(self.workflow.publish(self.post_id))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual((await self.post()).status, 'PUBLISH_UNCERTAIN')
        self.assertEqual((await self.attempt()).parts[0].state, 'UNKNOWN')

    async def test_cancellation_after_ack_before_receipt_is_uncertain(self):
        with patch.object(self.workflow.publication, '_confirm_part', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError): await self.workflow.publish(self.post_id)
        self.assertEqual((await self.attempt()).parts[0].state, 'UNKNOWN')
        self.bot.send_message.assert_awaited_once()

    async def test_restart_started_part_remains_blocked_and_live_owner_is_not_stolen(self):
        await self.interrupted(['STARTED'], expired=False)
        other = self.new_workflow()
        self.assertEqual(await other.publication.recover_expired(), 0)
        self.assertEqual(await other.publish(self.post_id), 'PUBLISHING')
        async with self.factory() as session:
            attempt = await session.scalar(select(PublishAttempt))
            attempt.lease_until = now() - timedelta(seconds=1)
            await session.commit()
        self.assertEqual(await other.publication.recover_expired(), 1)
        self.assertEqual((await self.attempt()).parts[0].state, 'UNKNOWN')
        self.assertEqual(await other.publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.bot.send_message.assert_not_awaited()

    async def test_restart_all_confirmed_finalizes_without_send(self):
        await self.interrupted(['CONFIRMED', 'CONFIRMED'])
        self.assertEqual(await self.new_workflow().publication.recover_expired(), 1)
        self.assertEqual((await self.post()).status, 'PUBLISHED')
        self.bot.send_message.assert_not_awaited()

    async def test_restart_never_started_attempt_is_reviewable(self):
        await self.interrupted(['READY'])
        await self.new_workflow().publication.recover_expired()
        self.assertEqual((await self.post()).status, 'REVIEW')
        self.assertEqual((await self.attempt()).state, 'ABORTED')
        self.bot.send_message.assert_not_awaited()

    async def test_concurrent_owners_cannot_send_twice_or_edit_active_snapshot(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(**kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=91)
        self.bot.send_message.side_effect = send
        task = asyncio.create_task(self.workflow.publish(self.post_id))
        await entered.wait()
        other = self.new_workflow()
        self.assertEqual(await other.publish(self.post_id), 'PUBLISHING')
        self.assertEqual(await other.save_edit(self.post_id, 'Changed'), 'PUBLISHING')
        self.assertEqual(await other.skip(self.post_id), 'PUBLISHING')
        with self.assertRaises(ValueError): await other.set_post_category(self.post_id, 1)
        release.set()
        self.assertEqual(await task, 'published')
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.post()).edited_text, 'Candidate')

    async def test_simultaneous_database_claims_have_one_winner(self):
        results = await asyncio.gather(self.workflow.publish(self.post_id), self.new_workflow().publish(self.post_id))
        self.assertEqual(results.count('published'), 1)
        self.assertTrue(all(result in {'published', 'PUBLISHING', 'PUBLISHED'} for result in results))
        self.bot.send_message.assert_awaited_once()

    async def test_operator_confirmation_requires_authorization_and_never_sends(self):
        identity = await self.interrupted(['STARTED'])
        await self.workflow.publication.recover_expired()
        self.assertEqual(await self.workflow.confirm_publication(identity, 999), 'unauthorized')
        self.assertEqual(await self.workflow.confirm_publication(identity, 123), 'published')
        attempt = await self.attempt()
        self.assertEqual((attempt.state, attempt.resolved_by), ('RESOLVED_PUBLISHED', 123))
        self.assertEqual(attempt.parts[0].state, 'UNKNOWN')  # Operator assertion is not an API receipt.
        self.assertEqual(await self.workflow.confirm_publication(identity, 123), 'not_uncertain')
        self.bot.send_message.assert_not_awaited()

    async def test_reconciliation_command_is_admin_only_and_explicit(self):
        identity = await self.interrupted(['STARTED'])
        await self.workflow.publication.recover_expired()
        router = create_admin_router(self.workflow, 123)
        handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == 'confirm_published')
        message = SimpleNamespace(from_user=SimpleNamespace(id=999), text='/confirm_published ' + identity, answer=AsyncMock())
        await handler(message)
        self.assertEqual((await self.post()).status, 'PUBLISH_UNCERTAIN')
        message.from_user.id = 123
        await handler(message)
        self.assertEqual((await self.post()).status, 'PUBLISHED')
        self.bot.send_message.assert_not_awaited()

    async def test_protected_or_unknown_sources_create_no_attempt(self):
        for state in (True, None):
            await self.change(status='REVIEW')
            self.monitor.protection_for_source.return_value = state
            self.assertEqual(await self.workflow.publish(self.post_id), 'protected' if state is True else 'protection_unverified')
            self.assertIsNone(await self.attempt())
        self.bot.send_message.assert_not_awaited()

    async def test_additive_migration_preserves_existing_published_data(self):
        legacy_url = f'sqlite+aiosqlite:///{self.directory.name}/legacy.db'
        engine = create_async_engine(legacy_url)
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                table for name, table in Base.metadata.tables.items() if not name.startswith('publish_attempt')]))
            await connection.execute(text("INSERT INTO sources (id, telegram_chat_id, enabled) VALUES (1, -1001, 1)"))
            await connection.execute(text("INSERT INTO source_posts (id, source_id, telegram_message_id, status, original_text) VALUES (1, 1, 1, 'PUBLISHED', 'Historical')"))
        await engine.dispose()
        await init_db(legacy_url)
        await init_db(legacy_url)
        engine = create_async_engine(legacy_url)
        try:
            async with engine.connect() as connection:
                tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
                indexes = await connection.run_sync(lambda sync: inspect(sync).get_indexes('publish_attempts'))
                row = (await connection.execute(text('SELECT status, original_text FROM source_posts WHERE id=1'))).one()
            self.assertIn('publish_attempts', tables)
            self.assertIn('publish_attempt_parts', tables)
            self.assertIn('uq_publish_unresolved_post', {index['name'] for index in indexes})
            self.assertEqual(tuple(row), ('PUBLISHED', 'Historical'))
        finally:
            await engine.dispose()

    async def test_persistent_database_failure_keeps_started_claim_until_restart_recovery(self):
        original = AsyncSession.commit
        count = 0
        async def commit(session):
            nonlocal count
            count += 1
            if count >= 3: raise RuntimeError('database offline after send')
            await original(session)
        with patch.object(AsyncSession, 'commit', new=commit):
            self.assertEqual(await self.workflow.publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.assertEqual((await self.post()).status, 'PUBLISHING')
        self.assertEqual((await self.attempt()).parts[0].state, 'STARTED')
        async with self.factory() as session:
            attempt = await session.scalar(select(PublishAttempt))
            attempt.lease_until = now() - timedelta(seconds=1)
            await session.commit()
        await self.new_workflow().publication.recover_expired()
        self.assertEqual((await self.post()).status, 'PUBLISH_UNCERTAIN')
        self.assertEqual(await self.new_workflow().publish(self.post_id), 'PUBLISH_UNCERTAIN')
        self.bot.send_message.assert_awaited_once()

    async def test_destination_snapshot_survives_management_change_between_parts(self):
        await self.change(edited_text='A' * 5000)
        calls = 0
        async def send(**kwargs):
            nonlocal calls
            calls += 1
            self.assertEqual(kwargs['chat_id'], -1002)
            if calls == 1:
                async with self.factory() as session:
                    source = await session.get(Source, self.source_id)
                    source.destination_channel_id = -1003
                    await session.commit()
            return SimpleNamespace(message_id=90 + calls)
        self.bot.send_message.side_effect = send
        self.assertEqual(await self.workflow.publish(self.post_id), 'published')
        self.assertEqual((await self.attempt()).destination_id, -1002)
        self.assertEqual(calls, 2)

    async def test_media_group_stores_each_returned_id_and_later_text_receipt(self):
        from postradar.db.models import SourcePostMedia
        items = []
        for position in range(2):
            path = Path(self.directory.name) / f'album-{position}.jpg'
            path.write_bytes(b'offline')
            items.append(SourcePostMedia(telegram_message_id=10 + position,
                         media_type='photo', media_path=str(path), position=position))
        async with self.factory() as session:
            post = await session.get(SourcePost, self.post_id)
            post.media_type = 'album'
            post.edited_text = 'A' * 1100
            post.media_items = items
            await session.commit()
        self.bot.send_media_group.return_value = [SimpleNamespace(message_id=92), SimpleNamespace(message_id=93)]
        self.assertEqual(await self.workflow.publish(self.post_id), 'published')
        parts = (await self.attempt()).parts
        self.assertEqual([part.kind for part in parts], ['media_group', 'text'])
        self.assertEqual(json.loads(parts[0].telegram_message_ids), [92, 93])
        self.assertEqual(json.loads(parts[1].telegram_message_ids), [91])

    async def test_unresolved_admin_report_contains_no_candidate_or_source_text(self):
        await self.interrupted(['CONFIRMED', 'STARTED'])
        router = create_admin_router(self.workflow, 123)
        handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == 'publications')
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock())
        await handler(message)
        rendered = message.answer.await_args.args[0]
        self.assertIn('UNKNOWN', rendered)
        self.assertIn('91', rendered)
        self.assertIn('/confirm_published', rendered)
        self.assertNotIn('Candidate', rendered)
        self.assertNotIn('Source', rendered)
        self.bot.send_message.assert_not_awaited()
