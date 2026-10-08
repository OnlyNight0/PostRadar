"""Mocked album task ownership tests; no databases or providers are opened."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from postradar.telegram.source_client import SourceMonitor


class Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def scalar(self, *args): return None


class AlbumOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self, delay=0):
        client = SimpleNamespace(disconnect=AsyncMock())
        monitor = SourceMonitor(1, 'hash', 'session', Session, client=client, album_collection_delay=delay)
        monitor._block_incomplete_album = AsyncMock()
        return monitor

    def source(self): return SimpleNamespace(id=1, telegram_chat_id=-1001, category_id=None)
    def fragment(self, identity): return SimpleNamespace(id=identity, grouped_id=100)

    async def test_shutdown_drains_debounce_before_disconnect(self):
        monitor = self.monitor(delay=100)
        async def process(*args):
            monitor._client.disconnect.assert_not_awaited()
        monitor._process_album = AsyncMock(side_effect=process)
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        await monitor.close()
        monitor._process_album.assert_awaited_once()
        monitor._client.disconnect.assert_awaited_once()
        self.assertFalse(monitor._album_tasks)
        self.assertFalse(monitor._album_batches)
        await monitor._collect_album_message(self.source(), self.fragment(2), 101)
        self.assertFalse(monitor._album_batches)

    async def test_active_finalizer_remains_owned_and_shutdown_joins_it(self):
        monitor = self.monitor()
        entered, release = asyncio.Event(), asyncio.Event()
        async def process(*args):
            entered.set()
            await release.wait()
            monitor._client.disconnect.assert_not_awaited()
        monitor._process_album = AsyncMock(side_effect=process)
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        await entered.wait()
        self.assertEqual(len(monitor._album_tasks), 1)
        closing = asyncio.create_task(monitor.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await closing
        monitor._process_album.assert_awaited_once()
        self.assertFalse(monitor._album_tasks)

    async def test_duplicate_fragment_does_not_restart_debounce(self):
        monitor = self.monitor(delay=100)
        monitor._process_album = AsyncMock()
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        task = monitor._album_batches[(1, 100)][2]
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        self.assertIs(task, monitor._album_batches[(1, 100)][2])
        await monitor.close()
        monitor._process_album.assert_awaited_once()

    async def test_late_fragment_cancels_processing_and_blocks_album(self):
        monitor = self.monitor()
        entered = asyncio.Event()
        async def process(*args):
            entered.set()
            await asyncio.Event().wait()
        monitor._process_album = AsyncMock(side_effect=process)
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        await entered.wait()
        await monitor._collect_album_message(self.source(), self.fragment(2), 100)
        monitor._block_incomplete_album.assert_awaited_once()
        await monitor.close()
        self.assertFalse(monitor._album_tasks)
        self.assertEqual(monitor._process_album.await_count, 1)

    async def test_cancelled_processing_keeps_batch_for_shutdown_retry(self):
        monitor = self.monitor()
        entered = asyncio.Event()
        async def process(*args):
            entered.set()
            await asyncio.Event().wait()
        monitor._process_album = AsyncMock(side_effect=process)
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        await entered.wait()
        task = monitor._album_batches[(1, 100)][2]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertIn((1, 100), monitor._album_batches)
        monitor._process_album = AsyncMock()
        await monitor.close()
        monitor._process_album.assert_awaited_once()

    async def test_unknown_or_protected_album_never_enters_pipeline(self):
        for protection in (None, True):
            monitor = self.monitor()
            monitor.protection_for_source = AsyncMock(return_value=protection)
            with patch('postradar.telegram.source_client.persist_album', new=AsyncMock()) as persist, patch('postradar.telegram.source_client.mark_post_protected', new=AsyncMock()) as mark:
                await monitor._process_album((1, 100), self.source(), [self.fragment(1)])
                persist.assert_not_awaited()
                self.assertEqual(mark.await_args.kwargs['protection_known'], protection is True)
            await monitor.close()

    async def test_cancellation_of_close_does_not_disconnect_before_processing_finishes(self):
        monitor = self.monitor()
        entered, release = asyncio.Event(), asyncio.Event()
        async def process(*args):
            entered.set()
            await release.wait()
        monitor._process_album = AsyncMock(side_effect=process)
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        await entered.wait()
        closing = asyncio.create_task(monitor.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        monitor._client.disconnect.assert_not_awaited()
        release.set()
        with self.assertRaises(asyncio.CancelledError): await closing
        monitor._client.disconnect.assert_awaited_once()
        self.assertFalse(monitor._album_tasks)

    async def test_fragment_after_completed_album_is_blocked_and_duplicates_ignored(self):
        monitor = self.monitor()
        post = SimpleNamespace(telegram_message_id=1, media_items=[], status='NEW')
        class FoundSession(Session):
            async def scalar(self, *args): return post
        monitor._session_factory = FoundSession
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        monitor._block_incomplete_album.assert_not_awaited()
        await monitor._collect_album_message(self.source(), self.fragment(2), 100)
        monitor._block_incomplete_album.assert_awaited_once()
        self.assertFalse(monitor._album_tasks)
        await monitor.close()

    async def test_pipeline_cancellation_propagates_in_ai_download_and_commit(self):
        from postradar.services.ai_editor import ProcessingResult
        from postradar.telegram.source_client import persist_album
        from pathlib import Path
        for phase in ('ai', 'download', 'commit'):
            entered = asyncio.Event()
            sessions = []
            class PipelineSession(Session):
                def __init__(self): sessions.append(self); self.closed = False
                def add(self, row): pass
                async def commit(self):
                    if phase == 'commit':
                        entered.set()
                        await asyncio.Event().wait()
                async def __aexit__(self, *args): self.closed = True
            async def edit(*args):
                if phase == 'ai':
                    entered.set()
                    await asyncio.Event().wait()
                return ProcessingResult('CONTENT', 'Valid', 'caption')
            async def download(*args):
                if phase == 'download':
                    entered.set()
                    await asyncio.Event().wait()
                return None
            message = SimpleNamespace(id=1, media=object(), photo=object(), message='caption', date=None)
            with patch('postradar.telegram.source_client._process_text', side_effect=edit), patch('postradar.telegram.source_client.download_message_media', side_effect=download), patch('postradar.telegram.source_client.media_target_path', return_value=Path('/tmp/mock-album-path')):
                task = asyncio.create_task(persist_album(PipelineSession, self.source(), 100, [message], object()))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(all(session.closed for session in sessions))

    async def test_protection_is_rechecked_after_ai_before_downloading(self):
        from postradar.services.ai_editor import ProcessingResult
        from postradar.telegram.source_client import persist_album
        message = SimpleNamespace(id=1, media=object(), photo=object(), message='caption', date=None)
        with patch('postradar.telegram.source_client._process_text', new=AsyncMock(return_value=ProcessingResult('CONTENT', 'Valid', 'caption'))), patch('postradar.telegram.source_client.download_message_media', new=AsyncMock()) as download, patch('postradar.telegram.source_client.mark_post_protected', new=AsyncMock()) as mark:
            result = await persist_album(Session, self.source(), 100, [message], object(), protection_check=AsyncMock(return_value=None))
            self.assertFalse(result)
            download.assert_not_awaited()
            mark.assert_awaited_once()

    async def test_cancelled_obsolete_debounce_does_not_clear_processing_owner(self):
        monitor = self.monitor(delay=100)
        monitor._process_album = AsyncMock()
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        obsolete = monitor._album_batches[(1, 100)][2]
        await asyncio.sleep(0)
        await monitor._collect_album_message(self.source(), self.fragment(2), 100)
        monitor._album_processing.add((1, 100))
        await asyncio.gather(obsolete, return_exceptions=True)
        self.assertIn((1, 100), monitor._album_processing)
        monitor._album_processing.clear()
        await monitor.close()

    async def test_shutdown_timeout_joins_tasks_and_marks_interrupted_album(self):
        monitor = self.monitor()
        monitor._shutdown_timeout = 0.01
        entered = asyncio.Event()
        async def process(*args):
            entered.set()
            await asyncio.Event().wait()
        monitor._process_album = AsyncMock(side_effect=process)
        await monitor._collect_album_message(self.source(), self.fragment(1), 100)
        await entered.wait()
        await monitor.close()
        monitor._block_incomplete_album.assert_awaited_once()
        monitor._client.disconnect.assert_awaited_once()
        self.assertFalse(monitor._album_tasks)
