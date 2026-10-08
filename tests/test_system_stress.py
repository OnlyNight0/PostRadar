"""Bounded offline load checks for the durable capture and Gemini path.

All provider latency and responses are synthetic. The temporary SQLite database
is isolated to this test; the reported measurements are regression observations,
not production capacity estimates.
"""

import asyncio
import json
import statistics
import tempfile
import time
import tracemalloc
import unittest
from types import SimpleNamespace

from google.genai.errors import ServerError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from postradar.db.base import Base
from postradar.db.models import Source, SourcePost
from postradar.services.ai_editor import AIEditor
from postradar.services.capture import CaptureJournal


def transient_error(status: int) -> ServerError:
    return ServerError(status, {
        "error": {"code": status, "message": "synthetic transient failure", "status": "UNAVAILABLE"}
    })


class SystemStressTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_capture_load_handles_slow_and_transient_provider(self):
        count = 36
        concurrency = 4
        with tempfile.TemporaryDirectory(prefix="postradar-stress-") as directory:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{directory}/stress.db", connect_args={"timeout": 10}
            )
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as session:
                    source = Source(telegram_chat_id=-7788, enabled=True)
                    session.add(source)
                    await session.commit()
                    source_id = source.id

                journal = CaptureJournal(factory)
                async with factory() as session:
                    source = await session.get(Source, source_id)
                    source_ids = [await journal.receipt(source, identity) for identity in range(1, count + 1)]

                claims = await asyncio.gather(*(journal.claim(identity) for identity in source_ids))
                self.assertTrue(all(claim is not None for claim in claims))

                active = 0
                peak_active = 0
                transient_attempts = 0
                async def generate_content(*, model, contents, config):
                    nonlocal active, peak_active, transient_attempts
                    active += 1
                    peak_active = max(peak_active, active)
                    try:
                        payload = json.loads(contents)
                        identity = int(payload["source_html"].split("-")[-1].split("<")[0])
                        await asyncio.sleep(0.018 if identity % 7 == 0 else 0.004)
                        if model == "primary" and identity % 11 == 0:
                            transient_attempts += 1
                            raise transient_error(503)
                        if model == "primary" and identity % 13 == 0:
                            transient_attempts += 1
                            raise transient_error(504)
                        return SimpleNamespace(text=json.dumps({
                            "content_type": "CONTENT",
                            "reason": "Synthetic stress response",
                            "edited_html": payload["source_html"],
                        }))
                    finally:
                        active -= 1

                editor = AIEditor(
                    api_key="offline-test-key", primary_model="primary", fallback_model="fallback",
                    client=SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
                        generate_content=generate_content,
                    ))),
                )
                slots = asyncio.Semaphore(concurrency)
                enqueued = time.perf_counter()
                queue_waits: list[float] = []
                process_times: list[float] = []
                tracemalloc.start()
                _, memory_peak_before = tracemalloc.get_traced_memory()

                async def process(identity, claim):
                    started_waiting = time.perf_counter()
                    async with slots:
                        started = time.perf_counter()
                        queue_waits.append(started - started_waiting)
                        html = f"<b>item-{identity}</b>"
                        outcome = await editor.process(html)
                        self.assertEqual(outcome.content_type, "CONTENT")
                        values = {
                            "original_text": f"item-{identity}", "sanitized_text": f"item-{identity}",
                            "source_html": html, "edited_html": outcome.edited_html,
                            "edited_text": f"item-{identity}", "content_type": outcome.content_type,
                            "classification_reason": outcome.reason, "media_type": None,
                            "media_path": None, "published_at": None, "status": "NEW",
                        }
                        self.assertTrue(await journal.complete(claim, values))
                        process_times.append(time.perf_counter() - started)

                await asyncio.gather(*(process(i, claim) for i, claim in enumerate(claims, 1)))
                elapsed = time.perf_counter() - enqueued
                _current_memory, memory_peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()

                async with factory() as session:
                    rows = await session.scalar(select(func.count()).select_from(SourcePost).where(SourcePost.status == "NEW"))
                    pending = await session.scalar(select(func.count()).select_from(SourcePost).where(SourcePost.status == "CAPTURE_PENDING"))
                self.assertEqual(rows, count)
                self.assertEqual(pending, 0)
                self.assertEqual(transient_attempts, 5)
                self.assertLessEqual(peak_active, concurrency)
                self.assertEqual(active, 0)

                p95_index = max(0, int(len(queue_waits) * 0.95) - 1)
                queue_p95 = sorted(queue_waits)[p95_index]
                process_p95 = sorted(process_times)[p95_index]
                print(
                    "STRESS_METRICS "
                    f"items={count} throughput_items_s={count / elapsed:.1f} "
                    f"queue_ms_p50={statistics.median(queue_waits) * 1000:.2f} "
                    f"queue_ms_p95={queue_p95 * 1000:.2f} "
                    f"process_ms_p95={process_p95 * 1000:.2f} "
                    f"provider_peak_concurrency={peak_active} "
                    f"synthetic_503_504={transient_attempts} "
                    f"tracemalloc_peak_bytes={memory_peak - memory_peak_before}"
                )
            finally:
                await engine.dispose()

    async def test_shutdown_with_queued_work_leaves_unclaimed_receipts_recoverable(self):
        """Cancellation drains admission while durable, unclaimed receipts remain safe."""
        with tempfile.TemporaryDirectory(prefix="postradar-shutdown-queue-") as directory:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{directory}/queue.db", connect_args={"timeout": 10}
            )
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as session:
                    source = Source(telegram_chat_id=-8899, enabled=True)
                    session.add(source)
                    await session.commit()
                    source_id = source.id
                journal = CaptureJournal(factory)
                async with factory() as session:
                    source = await session.get(Source, source_id)
                    ids = [await journal.receipt(source, identity) for identity in range(101, 109)]

                entered = asyncio.Event()
                slots = asyncio.Semaphore(2)
                started: list[int] = []
                accepting = True

                async def worker(post_id):
                    nonlocal accepting
                    async with slots:
                        if not accepting:
                            return
                        claim = await journal.claim(post_id)
                        if claim is None:
                            return
                        started.append(post_id)
                        if len(started) == 2:
                            entered.set()
                        await asyncio.Event().wait()

                tasks = [asyncio.create_task(worker(post_id)) for post_id in ids]
                await asyncio.wait_for(entered.wait(), timeout=5)
                accepting = False
                for task in tasks:
                    task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
                self.assertEqual(len(started), 2)

                async with factory() as session:
                    rows = list((await session.scalars(select(SourcePost).order_by(SourcePost.id))).all())
                self.assertEqual(len(rows), len(ids))
                self.assertTrue(all(row.status == "CAPTURE_PENDING" for row in rows))
                self.assertEqual(sum(row.capture_token is not None for row in rows), 2)
                self.assertEqual(sum(row.capture_token is None for row in rows), len(ids) - 2)
            finally:
                await engine.dispose()
