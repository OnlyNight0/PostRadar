"""Durable scalar recovery tests: temporary SQLite, fake clients, no sessions/services."""

import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import types
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telethon import errors

from postradar.bot.handlers import create_admin_router
from postradar.bot.review import AdminWorkflow
from postradar.db.base import Base
from postradar.db.models import Category, Source, SourcePost
from postradar.db.session import init_db
from postradar.services.ai_editor import ProcessingResult
from postradar.services.capture import CaptureJournal, MAX_ATTEMPTS, capture_now
from postradar.telegram.source_client import SourceMonitor, persist_message


class CaptureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.url = f'sqlite+aiosqlite:///{self.directory.name}/capture.db'
        self.engine = create_async_engine(self.url, connect_args={'timeout': 5})
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.factory() as session:
            self.source = Source(telegram_chat_id=-1001, destination_channel_id=-1002, enabled=True)
            session.add(self.source)
            await session.commit()
        self.message = SimpleNamespace(id=11, message='Source text', date=None, grouped_id=None,
                                       media=None, noforwards=False, entities=[])
        self.chat = SimpleNamespace(noforwards=False)
        async def download(message, file):
            Path(file).write_bytes(b'valid media')
            return file
        self.client = SimpleNamespace(get_entity=AsyncMock(return_value=self.chat),
            get_messages=AsyncMock(return_value=self.message), download_media=AsyncMock(side_effect=download))
        self.editor = SimpleNamespace(process=AsyncMock(return_value=ProcessingResult('CONTENT', 'Valid', 'Edited text')))
        self.journal = CaptureJournal(self.factory)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.directory.cleanup()

    def monitor(self):
        return SourceMonitor(1, 'fake-hash', 'unused-session', self.factory, client=self.client,
                             ai_editor=self.editor, media_dir=self.directory.name)

    async def save(self, **kwargs):
        return await persist_message(self.factory, self.source, self.message, self.client,
                                     self.directory.name, self.editor, **kwargs)

    async def post(self):
        async with self.factory() as session:
            return await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 11))

    async def due(self, *, expire=False):
        async with self.factory() as session:
            post = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == 11))
            post.capture_next_attempt_at = capture_now() - timedelta(seconds=1)
            if expire: post.capture_lease_until = capture_now() - timedelta(seconds=1)
            await session.commit()

    def media(self):
        self.message.media, self.message.photo = object(), object()

    async def test_receipt_and_claim_are_durable_before_ai_and_not_reviewable(self):
        async def process(*args, **kwargs):
            post = await self.post()
            self.assertEqual(post.status, 'CAPTURE_PENDING')
            self.assertIsNone(post.original_text)
            self.assertIsNone(post.source_html)
            self.assertIsNone(post.media_path)
            self.assertIsNotNone(post.capture_token)
            bot = SimpleNamespace(send_message=AsyncMock())
            workflow = AdminWorkflow(bot, self.factory, 123, source_monitor=self.monitor())
            self.assertEqual(await workflow.deliver_new(), 0)
            bot.send_message.assert_not_awaited()
            return ProcessingResult('CONTENT', 'Valid', 'Edited text')
        self.editor.process.side_effect = process
        self.assertTrue(await self.save())
        post = await self.post()
        self.assertEqual((post.status, post.capture_attempts), ('NEW', 1))
        self.assertIsNone(post.capture_token)
        self.assertEqual(post.original_text, 'Source text')

    async def test_receipt_commit_failure_prevents_processing_and_has_no_durable_recovery(self):
        with patch.object(AsyncSession, 'commit', new=AsyncMock(side_effect=RuntimeError('receipt failed'))):
            with self.assertRaises(RuntimeError): await self.save()
        self.assertIsNone(await self.post())
        self.editor.process.assert_not_awaited()
        self.client.download_media.assert_not_awaited()

    async def test_interruption_after_receipt_restarts_without_duplicate_row(self):
        with patch.object(CaptureJournal, 'claim', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError): await self.save()
        before = await self.post()
        self.assertEqual(before.status, 'CAPTURE_PENDING')
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)
        self.assertEqual((await self.post()).id, before.id)
        self.editor.process.assert_awaited_once()

    async def test_cancellation_during_gemini_keeps_receipt_and_backoff(self):
        entered = asyncio.Event()
        async def process(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.editor.process.side_effect = process
        task = asyncio.create_task(self.save())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        post = await self.post()
        self.assertEqual(post.status, 'CAPTURE_PENDING')
        self.assertIsNone(post.original_text)
        self.assertGreater(post.capture_next_attempt_at, capture_now())
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        await self.due()
        self.editor.process.side_effect = None
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)

    async def test_hard_crash_claim_requires_lease_expiry_before_restart(self):
        marker = await self.journal.receipt(self.source, 11)
        claim = await self.journal.claim(marker)
        self.assertIsNotNone(claim)
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        await self.due(expire=True)
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)
        self.assertEqual((await self.post()).capture_attempts, 2)

    async def test_duplicate_source_events_across_owners_have_one_processing_winner(self):
        await asyncio.gather(self.monitor()._handle_source_message(self.source, self.message, self.chat),
                             self.monitor()._handle_source_message(self.source, self.message, self.chat))
        self.editor.process.assert_awaited_once()
        async with self.factory() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(SourcePost)), 1)
        self.assertEqual((await self.post()).status, 'NEW')

    async def test_concurrent_recovery_claims_process_once(self):
        await self.journal.receipt(self.source, 11)
        await asyncio.gather(self.monitor().recover_pending_captures(), self.monitor().recover_pending_captures())
        self.editor.process.assert_awaited_once()
        self.assertEqual((await self.post()).capture_attempts, 1)

    async def test_stale_worker_cannot_overwrite_new_result_or_delete_winner_media(self):
        self.media()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def process(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return ProcessingResult('CONTENT', 'Old', 'Old edit')
            return ProcessingResult('CONTENT', 'New', 'New edit')
        self.editor.process.side_effect = process
        old = asyncio.create_task(self.save())
        await entered.wait()
        await self.due(expire=True)
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)
        winner = await self.post()
        winner_path = Path(winner.media_path)
        self.assertTrue(winner_path.is_file())
        release.set()
        self.assertFalse(await old)
        saved = await self.post()
        self.assertEqual(saved.edited_text, 'New edit')
        self.assertEqual(saved.media_path, str(winner_path))
        self.assertEqual(winner_path.read_bytes(), b'valid media')
        media_files = [path for path in Path(self.directory.name).rglob('*') if path.name.endswith('.jpg')]
        self.assertEqual(media_files, [winner_path])

    async def test_media_failure_preserves_metadata_only_receipt_and_retries(self):
        self.media()
        async def partial(message, file):
            Path(file).write_bytes(b'partial')
            raise OSError('interrupted')
        self.client.download_media.side_effect = partial
        self.assertFalse(await self.save())
        post = await self.post()
        self.assertEqual(post.status, 'CAPTURE_PENDING')
        self.assertIsNone(post.original_text)
        self.assertIsNone(post.media_path)
        self.assertEqual(list(Path(self.directory.name).rglob('*.jpg')), [])
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        await self.due()
        async def success(message, file):
            Path(file).write_bytes(b'complete')
            return file
        self.client.download_media.side_effect = success
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)
        self.assertEqual((await self.post()).id, post.id)
        self.assertEqual(Path((await self.post()).media_path).read_bytes(), b'complete')

    async def test_media_none_and_unsupported_media_never_become_text_only_candidates(self):
        self.media()
        self.client.download_media.side_effect = None
        self.client.download_media.return_value = None
        self.assertFalse(await self.save())
        self.assertEqual((await self.post()).status, 'CAPTURE_PENDING')
        await self.due()
        self.message.photo = None
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        self.assertEqual((await self.post()).status, 'CAPTURE_FAILED')

    async def test_link_preview_is_text_without_a_required_download(self):
        self.message.media = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
        self.assertTrue(await self.save())
        post = await self.post()
        self.assertEqual((post.status, post.media_type), ('NEW', 'text'))
        self.assertEqual(post.edited_text, 'Edited text')
        self.assertIsNone(post.media_path)
        self.client.download_media.assert_not_awaited()

    async def test_empty_or_truncated_media_never_enters_review(self):
        self.media()
        self.message.photo = None
        self.message.document = SimpleNamespace(mime_type='application/pdf')
        self.message.file = SimpleNamespace(size=10, ext='.pdf')
        async def truncated(message, file):
            Path(file).write_bytes(b'partial')
            return file
        self.client.download_media.side_effect = truncated
        self.assertFalse(await self.save())
        self.assertEqual((await self.post()).status, 'CAPTURE_PENDING')
        self.assertIsNone((await self.post()).media_path)
        self.assertEqual(list(Path(self.directory.name).rglob('*.jpg')), [])
        await self.due()
        async def empty(message, file):
            Path(file).write_bytes(b'')
            return file
        self.client.download_media.side_effect = empty
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        self.assertEqual((await self.post()).status, 'CAPTURE_PENDING')

    async def test_claim_commit_failure_keeps_receipt_without_processing(self):
        await self.journal.receipt(self.source, 11)
        with patch.object(AsyncSession, 'commit', new=AsyncMock(side_effect=RuntimeError('claim failed'))):
            with self.assertRaises(RuntimeError):
                await self.save()
        post = await self.post()
        self.assertEqual((post.status, post.capture_attempts), ('CAPTURE_PENDING', 0))
        self.assertIsNone(post.capture_token)
        self.editor.process.assert_not_awaited()
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)

    async def test_final_commit_failure_retries_without_orphan_files(self):
        self.media()
        original, count = AsyncSession.commit, 0
        async def commit(session):
            nonlocal count
            count += 1
            if count == 3: raise RuntimeError('final capture write failed')
            await original(session)
        with patch.object(AsyncSession, 'commit', new=commit):
            self.assertFalse(await self.save())
        self.assertEqual((await self.post()).status, 'CAPTURE_PENDING')
        self.assertEqual(list(Path(self.directory.name).rglob('*.jpg')), [])
        await self.due()
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)

    async def test_committed_result_with_lost_commit_ack_keeps_successful_media(self):
        self.media()
        original, count = AsyncSession.commit, 0
        async def commit(session):
            nonlocal count
            count += 1
            await original(session)
            if count == 3: raise RuntimeError('commit ack lost')
        with patch.object(AsyncSession, 'commit', new=commit):
            self.assertFalse(await self.save())
        post = await self.post()
        self.assertEqual(post.status, 'NEW')
        self.assertTrue(Path(post.media_path).is_file())
        self.assertFalse(await self.save())
        self.editor.process.assert_awaited_once()

    async def test_provider_transient_and_permanent_errors_preserve_uncertain_fallback(self):
        for code in (504, 400):
            self.message.id = code
            class ProviderError(Exception): pass
            error = ProviderError('provider unavailable')
            error.code = code
            self.editor.process.side_effect = error
            self.assertTrue(await self.save())
            async with self.factory() as session:
                post = await session.scalar(select(SourcePost).where(SourcePost.telegram_message_id == code))
            self.assertEqual((post.status, post.content_type), ('NEW', 'UNCERTAIN'))
            self.assertEqual(post.edited_text, 'Source text')

    async def test_filtered_ad_and_self_promo_recovery_downloads_nothing(self):
        self.media()
        for i, kind in enumerate(('AD', 'SELF_PROMO')):
            self.message.id = 100 + i
            identity = await self.journal.receipt(self.source, self.message.id)
            self.editor.process.return_value = ProcessingResult(kind, 'Filtered', None)
            self.assertEqual(await self.monitor().recover_pending_captures(), 1)
            async with self.factory() as session:
                post = await session.get(SourcePost, identity)
            self.assertEqual((post.status, post.content_type), ('FILTERED', kind))
        self.client.download_media.assert_not_awaited()

    async def test_protected_recovery_never_fetches_or_processes_content(self):
        await self.journal.receipt(self.source, 11)
        self.chat.noforwards = True
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        self.assertEqual((await self.post()).status, 'PROTECTED')
        self.client.get_messages.assert_not_awaited()
        self.editor.process.assert_not_awaited()

    async def test_protection_changed_during_ai_blocks_media_and_storage(self):
        self.media()
        check = AsyncMock(side_effect=[False, True])
        self.assertFalse(await self.save(protection_check=check))
        self.assertEqual((await self.post()).status, 'PROTECTED')
        self.assertIsNone((await self.post()).original_text)
        self.client.download_media.assert_not_awaited()

    async def test_unknown_protection_keeps_receipt_with_backoff_and_no_ai(self):
        await self.journal.receipt(self.source, 11)
        self.chat.noforwards = None
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        post = await self.post()
        self.assertEqual(post.status, 'CAPTURE_PENDING')
        self.assertGreater(post.capture_next_attempt_at, capture_now())
        self.editor.process.assert_not_awaited()

    async def test_deleted_message_and_permanent_source_error_are_operator_visible(self):
        await self.journal.receipt(self.source, 11)
        self.client.get_messages.return_value = None
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        self.assertEqual((await self.post()).status, 'CAPTURE_MISSING')
        self.message.id = 12
        identity = await self.journal.receipt(self.source, 12)
        self.client.get_entity.side_effect = errors.ChannelPrivateError(request=None)
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        async with self.factory() as session:
            self.assertEqual((await session.get(SourcePost, identity)).status, 'CAPTURE_FAILED')

    async def test_disabled_source_defers_without_consuming_attempts(self):
        await self.journal.receipt(self.source, 11)
        async with self.factory() as session:
            source = await session.get(Source, self.source.id)
            source.enabled = False
            await session.commit()
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        self.assertEqual((await self.post()).capture_attempts, 0)
        self.client.get_entity.assert_not_awaited()
        async with self.factory() as session:
            source = await session.get(Source, self.source.id)
            source.enabled = True
            await session.commit()
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)

    async def test_original_category_snapshot_survives_restart_and_source_changes(self):
        async with self.factory() as session:
            old, new = Category(name='Original'), Category(name='Changed')
            session.add_all([old, new])
            await session.flush()
            source = await session.get(Source, self.source.id)
            source.category_id = old.id
            await session.commit()
            self.source.category_id = old.id
            new_id = new.id
        identity = await self.journal.receipt(self.source, 11)
        async with self.factory() as session:
            source = await session.get(Source, self.source.id)
            source.category_id = new_id
            await session.commit()
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)
        self.assertEqual(self.editor.process.await_args.kwargs['category_name'], 'Original')
        async with self.factory() as session:
            self.assertEqual((await session.get(SourcePost, identity)).category_id, self.source.category_id)

    async def test_exhausted_retry_count_stops_and_does_not_starve_other_receipts(self):
        self.media()
        self.client.download_media.side_effect = OSError('failed')
        self.assertFalse(await self.save())
        for _attempt in range(MAX_ATTEMPTS - 1):
            await self.due()
            self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        post = await self.post()
        self.assertEqual((post.status, post.capture_attempts), ('CAPTURE_FAILED', MAX_ATTEMPTS))
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        self.assertEqual(self.client.download_media.await_count, MAX_ATTEMPTS)
        self.message = SimpleNamespace(id=12, message='Healthy', date=None, grouped_id=None, media=None, noforwards=False, entities=[])
        self.client.get_messages.return_value = self.message
        await self.journal.receipt(self.source, 12)
        self.assertEqual(await self.monitor().recover_pending_captures(), 1)

    async def test_batch_budget_advances_only_past_visited_receipts(self):
        first = await self.journal.receipt(self.source, 11)
        second = await self.journal.receipt(self.source, 12)
        monitor = self.monitor()
        # Elapsed time is checked between attempts, so the unvisited row must
        # remain ahead of the cursor when the batch budget expires.
        clock = unittest.mock.Mock(side_effect=[0, 0, 16])
        with patch('postradar.telegram.source_client.asyncio', new=SimpleNamespace(
                get_running_loop=lambda: SimpleNamespace(time=clock))), patch.object(
                monitor, '_recover_scalar_capture', new=AsyncMock(return_value=False)) as recover:
            self.assertEqual(await monitor.recover_pending_captures(), 0)
        recover.assert_awaited_once_with(first)
        self.assertEqual(monitor._recovery_cursor, first)
        with patch.object(monitor, '_recover_scalar_capture', new=AsyncMock(return_value=False)) as recover:
            await monitor.recover_pending_captures()
        recover.assert_awaited_once_with(second)

    async def test_album_and_historical_markers_are_never_restored(self):
        async with self.factory() as session:
            session.add_all([SourcePost(source_id=self.source.id, telegram_message_id=20, status='PROTECTION_UNVERIFIED'),
                             SourcePost(source_id=self.source.id, telegram_message_id=21, status='PROTECTION_ALBUM_PENDING', grouped_id=99)])
            await session.commit()
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        self.client.get_entity.assert_not_awaited()
        await self.journal.receipt(self.source, 11)
        self.message.grouped_id = 100
        self.assertEqual(await self.monitor().recover_pending_captures(), 0)
        self.assertEqual((await self.post()).status, 'PROTECTION_ALBUM_PENDING')
        self.editor.process.assert_not_awaited()

    async def test_admin_capture_report_excludes_source_content(self):
        await self.journal.receipt(self.source, 11)
        workflow = AdminWorkflow(SimpleNamespace(), self.factory, 123, source_monitor=self.monitor())
        router = create_admin_router(workflow, 123)
        callback = next(item.callback for item in router.message.handlers if item.callback.__name__ == 'captures')
        message = SimpleNamespace(from_user=SimpleNamespace(id=999), answer=AsyncMock())
        await callback(message)
        message.answer.assert_not_awaited()
        message.from_user.id = 123
        await callback(message)
        report = message.answer.await_args.args[0]
        self.assertIn('CAPTURE_PENDING', report)
        self.assertNotIn('Source text', report)

    async def test_capture_report_paging_can_reach_later_failed_receipts(self):
        async with self.factory() as session:
            session.add_all([SourcePost(source_id=self.source.id, telegram_message_id=number,
                                        status='CAPTURE_FAILED', capture_error='retry_exhausted')
                             for number in range(30, 51)])
            await session.commit()
        workflow = AdminWorkflow(SimpleNamespace(), self.factory, 123, source_monitor=self.monitor())
        router = create_admin_router(workflow, 123)
        callback = next(item.callback for item in router.message.handlers if item.callback.__name__ == 'captures')
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), text='/captures', answer=AsyncMock())
        await callback(message)
        report = message.answer.await_args.args[0]
        self.assertIn('/captures 20', report)
        self.assertNotIn('сообщение 50', report)
        message.text = '/captures 20'
        await callback(message)
        self.assertIn('сообщение 50', message.answer.await_args.args[0])

    async def test_additive_capture_migration_is_idempotent_and_preserves_legacy_content(self):
        legacy = f'sqlite+aiosqlite:///{self.directory.name}/legacy.db'
        engine = create_async_engine(legacy)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for column in ('capture_attempts', 'capture_next_attempt_at', 'capture_lease_until', 'capture_token', 'capture_error'):
                await connection.execute(text(f'ALTER TABLE source_posts DROP COLUMN {column}'))
            await connection.execute(text("INSERT INTO sources (id, telegram_chat_id, enabled) VALUES (1,-1001,1)"))
            await connection.execute(text("INSERT INTO source_posts (id,source_id,telegram_message_id,status,original_text) VALUES (1,1,1,'PUBLISHED','Historical')"))
        await engine.dispose()
        await init_db(legacy)
        await init_db(legacy)
        engine = create_async_engine(legacy)
        try:
            async with engine.connect() as connection:
                columns = await connection.run_sync(lambda sync: {column['name'] for column in inspect(sync).get_columns('source_posts')})
                row = (await connection.execute(text('SELECT status, original_text, capture_token, capture_attempts FROM source_posts'))).one()
            self.assertTrue({'capture_attempts', 'capture_next_attempt_at', 'capture_lease_until', 'capture_token', 'capture_error'} <= columns)
            self.assertEqual(tuple(row), ('PUBLISHED', 'Historical', None, None))
        finally:
            await engine.dispose()
