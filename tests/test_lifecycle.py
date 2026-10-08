"""Offline tests for service supervision, signals and partial startup cleanup."""

import asyncio
import signal
import unittest
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main as app
from postradar.lifecycle import shutdown_signals, supervise
from postradar.telegram.source_client import SourceMonitor


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def services(self, *, normal_polling=False, monitor_error=None):
        order = []
        stopped = asyncio.Event()
        polling_stopped = asyncio.Event()
        async def monitor_run():
            if monitor_error:
                raise monitor_error
            await stopped.wait()
        async def monitor_close():
            order.append('monitor_closed')
            stopped.set()
        async def polling(*args, **kwargs):
            self.assertFalse(kwargs['handle_signals'])
            self.assertFalse(kwargs['handle_as_tasks'])
            if not normal_polling:
                await polling_stopped.wait()
        async def stop_polling():
            order.append('polling_stopped')
            polling_stopped.set()
        async def review(_workflow):
            try:
                await asyncio.Event().wait()
            finally:
                order.append('review_stopped')
        monitor = SimpleNamespace(run=monitor_run, close=monitor_close)
        dispatcher = SimpleNamespace(start_polling=polling, stop_polling=stop_polling)
        return monitor, dispatcher, review, order

    async def test_normal_polling_completion_stops_all_peers(self):
        monitor, dispatcher, review, order = self.services(normal_polling=True)
        await asyncio.wait_for(supervise(monitor, dispatcher, object(), object(), review), 1)
        self.assertIn('monitor_closed', order)
        self.assertIn('review_stopped', order)
        self.assertFalse(any(task.get_name() in {'telethon-source-monitor', 'review-delivery-worker', 'aiogram-admin-polling'} for task in asyncio.all_tasks()))

    async def test_fatal_monitor_failure_stops_admin_polling_and_propagates(self):
        monitor, dispatcher, review, order = self.services(monitor_error=ValueError('fatal'))
        with self.assertRaises(ValueError):
            await supervise(monitor, dispatcher, object(), object(), review)
        self.assertIn('polling_stopped', order)
        self.assertIn('monitor_closed', order)

    async def test_review_worker_normal_completion_stops_services(self):
        monitor, dispatcher, _, order = self.services()
        async def review(_workflow):
            return
        await supervise(monitor, dispatcher, object(), object(), review)
        self.assertIn('monitor_closed', order)
        self.assertIn('polling_stopped', order)

    async def test_cancellation_during_shutdown_waits_for_cleanup(self):
        monitor, dispatcher, review, order = self.services()
        closing = asyncio.Event()
        release = asyncio.Event()
        original_close = monitor.close
        async def close():
            closing.set()
            await release.wait()
            await original_close()
        monitor.close = close
        task = asyncio.create_task(supervise(monitor, dispatcher, object(), object(), review))
        await asyncio.sleep(0)
        task.cancel()
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn('monitor_closed', order)

    async def test_sigterm_and_sigint_cancel_owner_and_restore_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            callbacks = {}
            loop = asyncio.get_running_loop()
            async def owner():
                with shutdown_signals():
                    await asyncio.Event().wait()
            with patch.object(loop, 'add_signal_handler', side_effect=lambda sig, fn: callbacks.update({sig: fn})), patch.object(loop, 'remove_signal_handler') as remove, patch('postradar.lifecycle.signal.signal') as restore:
                task = asyncio.create_task(owner())
                await asyncio.sleep(0)
                callbacks[sig]()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(remove.call_count, 2)
                self.assertEqual(restore.call_count, 2)

    async def test_monitor_retries_transient_errors_but_not_permanent_errors(self):
        monitor = SourceMonitor(1, 'hash', 'session', Mock(), client=Mock())
        calls = 0
        async def run_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('temporary')
            monitor._accepting = False
        monitor._run_once = run_once
        with patch('postradar.telegram.source_client.asyncio.sleep', new=AsyncMock()):
            await monitor.run()
        self.assertEqual(calls, 2)
        monitor._accepting = True
        monitor._run_once = AsyncMock(side_effect=ValueError('fatal'))
        with self.assertRaises(ValueError):
            await monitor.run()
        monitor._run_once.assert_awaited_once()

    async def test_monitor_exhausted_retries_are_reported(self):
        monitor = SourceMonitor(1, 'hash', 'session', Mock(), client=Mock())
        monitor._run_once = AsyncMock(side_effect=OSError('temporary'))
        with patch('postradar.telegram.source_client.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(OSError):
                await monitor.run()
        self.assertEqual(monitor._run_once.await_count, 3)

    async def test_partial_startup_releases_created_resources(self):
        order = []
        settings = SimpleNamespace(log_level='INFO', bot_token='test', admin_user_id=1, telegram_api_id=1, telegram_api_hash='hash', telegram_session='session', database_url='unused', gemini_api_key='', gemini_primary_model='test', gemini_fallback_model='test', ai_edit_enabled=False, media_dir='unused')
        async def dispose(): order.append('db')
        async def ai_close(): order.append('ai')
        async def monitor_close(): order.append('monitor')
        factory = SimpleNamespace(kw={'bind': SimpleNamespace(dispose=dispose)})
        with ExitStack() as stack:
            for name, value in {
                'Settings': Mock(return_value=settings), 'configure_logging': Mock(),
                'validate_admin_settings': Mock(), 'validate_telegram_settings': Mock(),
                'init_db': AsyncMock(), 'create_session_factory': Mock(return_value=factory),
                'AIEditor': Mock(return_value=SimpleNamespace(close=ai_close)),
                'SourceMonitor': Mock(return_value=SimpleNamespace(close=monitor_close)),
                'create_admin_bot': Mock(side_effect=RuntimeError('startup failed')),
                'shutdown_signals': nullcontext,
            }.items():
                stack.enter_context(patch.object(app, name, value))
            with self.assertRaises(RuntimeError):
                await app.main()
        self.assertEqual(order, ['monitor', 'ai', 'db'])

    async def test_cancellation_during_monitor_startup_closes_client(self):
        connected = asyncio.Event()
        async def connect():
            connected.set()
            await asyncio.Event().wait()
        client = SimpleNamespace(connect=connect, disconnect=AsyncMock())
        monitor = SourceMonitor(1, 'hash', 'session', Mock(), client=client)
        task = asyncio.create_task(monitor.run())
        await connected.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        await monitor.close()
        client.disconnect.assert_awaited_once()

    async def test_main_startup_cancellation_does_not_construct_services(self):
        settings = SimpleNamespace(log_level='INFO', bot_token='test', admin_user_id=1, telegram_api_id=1, telegram_api_hash='hash', telegram_session='session', database_url='unused')
        entered = asyncio.Event()
        async def init_db(_url):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(app, 'Settings', return_value=settings), patch.object(app, 'configure_logging'), patch.object(app, 'validate_admin_settings'), patch.object(app, 'validate_telegram_settings'), patch.object(app, 'init_db', side_effect=init_db), patch.object(app, 'create_session_factory') as factory, patch.object(app, 'shutdown_signals', nullcontext):
            task = asyncio.create_task(app.main())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            factory.assert_not_called()
