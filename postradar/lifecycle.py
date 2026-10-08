"""Application task and signal ownership."""

import asyncio
import logging
import signal
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)


@contextmanager
def shutdown_signals() -> Iterator[None]:
    """Cancel the owner once; aiogram must not install competing handlers."""
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    installed = []
    def stop() -> None:
        if owner is not None and not owner.done() and not owner.cancelling():
            owner.cancel()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, stop)
            except (NotImplementedError, RuntimeError):
                continue
            installed.append((sig, previous))
        yield
    finally:
        for sig, previous in installed:
            loop.remove_signal_handler(sig)
            signal.signal(sig, previous)


async def finish_cleanup(work: Awaitable[Any]) -> None:
    """Join cleanup despite additional cancellation, then propagate cancellation."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def supervise(
    monitor: Any, dispatcher: Any, bot: Any, workflow: Any,
    review_worker: Callable[[Any], Awaitable[None]],
) -> None:
    """Any service completion ends the run; keep Telethon alive during draining."""
    monitor_task = asyncio.create_task(monitor.run(), name="telethon-source-monitor")
    polling_task = asyncio.create_task(dispatcher.start_polling(
        bot, close_bot_session=False, handle_signals=False, handle_as_tasks=False,
    ), name="aiogram-admin-polling")
    review_task = asyncio.create_task(review_worker(workflow), name="review-delivery-worker")
    tasks = (monitor_task, polling_task, review_task)

    async def stop_services() -> None:
        review_task.cancel()
        await asyncio.gather(review_task, return_exceptions=True)
        try:
            if not polling_task.done():
                stop = asyncio.create_task(dispatcher.stop_polling())
                try:
                    async with asyncio.timeout(10):
                        await asyncio.wait((stop, polling_task), return_when=asyncio.FIRST_COMPLETED)
                        if stop.done():
                            try:
                                stop.result()
                            except RuntimeError:
                                polling_task.cancel()  # Polling has not started yet.
                        await asyncio.gather(polling_task, return_exceptions=True)
                except TimeoutError:
                    logger.warning("Admin polling shutdown timed out")
                    polling_task.cancel()
                    await asyncio.gather(polling_task, return_exceptions=True)
                finally:
                    stop.cancel()
                    await asyncio.gather(stop, return_exceptions=True)
        finally:
            try:
                await monitor.close()
            finally:
                monitor_task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task in done:
                if task.cancelled():
                    raise asyncio.CancelledError
                error = task.exception()
                if error is not None:
                    logger.error("Application service failed: service=%s exception=%s", task.get_name(), type(error).__name__)
                    raise error
        logger.info("Application service completed; stopping peers")
    finally:
        await finish_cleanup(stop_services())
