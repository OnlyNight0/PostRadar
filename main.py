"""Local PostRadar application entry point."""

import asyncio
import logging

from postradar.bot.admin_bot import create_admin_bot, validate_admin_settings
from postradar.bot.review import review_delivery_worker
from postradar.config import Settings
from postradar.db.session import create_session_factory, init_db
from postradar.logging import configure_logging
from postradar.services.ai_editor import AIEditor
from postradar.services.network_diagnostic import run_network_diagnostic
from postradar.telegram.source_client import SourceMonitor, validate_telegram_settings


async def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    validate_admin_settings(settings.bot_token, settings.admin_user_id)
    validate_telegram_settings(
        settings.telegram_api_id,
        settings.telegram_api_hash,
        settings.telegram_session,
    )
    await init_db(settings.database_url)
    session_factory = create_session_factory(settings.database_url)
    admin_id = settings.admin_user_id
    assert admin_id is not None
    ai_editor = AIEditor(
        api_key=settings.gemini_api_key,
        primary_model=settings.gemini_primary_model,
        fallback_model=settings.gemini_fallback_model,
        enabled=settings.ai_edit_enabled,
    )
    monitor = SourceMonitor(
        settings.telegram_api_id,
        settings.telegram_api_hash,
        settings.telegram_session,
        session_factory,
        media_dir=settings.media_dir,
        ai_editor=ai_editor,
    )
    bot, dispatcher, workflow = create_admin_bot(
        settings.bot_token,
        session_factory,
        admin_id,
        source_monitor=monitor,
        media_dir=settings.media_dir,
    )
    logger = logging.getLogger(__name__)
    try:
        logger.info("PostRadar starting source monitor and admin bot")
        logger.info(
            "Open the PostRadar bot in Telegram and press Start once before expecting admin previews"
        )
        run_network_diagnostic()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(monitor.run(), name="telethon-source-monitor")
            tasks.create_task(
                dispatcher.start_polling(bot, close_bot_session=False),
                name="aiogram-admin-polling",
            )
            tasks.create_task(review_delivery_worker(workflow), name="review-delivery-worker")
    finally:
        try:
            await bot.session.close()
        finally:
            try:
                await monitor.close()
            finally:
                try:
                    await ai_editor.close()
                finally:
                    await session_factory.kw["bind"].dispose()
                    logger.info("PostRadar stopped")


if __name__ == "__main__":
    asyncio.run(main())
