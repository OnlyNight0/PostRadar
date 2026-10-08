"""Local PostRadar application entry point."""

import asyncio
import logging
from contextlib import AsyncExitStack

from postradar.bot.admin_bot import create_admin_bot, validate_admin_settings
from postradar.bot.review import review_delivery_worker
from postradar.config import Settings
from postradar.db.session import create_session_factory, init_db
from postradar.lifecycle import finish_cleanup, shutdown_signals, supervise
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
    resources = AsyncExitStack()
    logger = logging.getLogger(__name__)
    with shutdown_signals():
        try:
            await init_db(settings.database_url)
            session_factory = create_session_factory(settings.database_url)
            resources.push_async_callback(session_factory.kw["bind"].dispose)
            admin_id = settings.admin_user_id
            assert admin_id is not None
            ai_editor = AIEditor(
                api_key=settings.gemini_api_key,
                primary_model=settings.gemini_primary_model,
                fallback_model=settings.gemini_fallback_model,
                enabled=settings.ai_edit_enabled,
            )
            resources.push_async_callback(ai_editor.close)
            monitor = SourceMonitor(
                settings.telegram_api_id,
                settings.telegram_api_hash,
                settings.telegram_session,
                session_factory,
                media_dir=settings.media_dir,
                ai_editor=ai_editor,
            )
            resources.push_async_callback(monitor.close)
            bot, dispatcher, workflow = create_admin_bot(
                settings.bot_token,
                session_factory,
                admin_id,
                source_monitor=monitor,
                media_dir=settings.media_dir,
            )
            resources.push_async_callback(bot.session.close)
            logger.info("PostRadar starting source monitor and admin bot")
            logger.info("Open the PostRadar bot in Telegram and press Start once before expecting admin previews")
            run_network_diagnostic()
            await supervise(monitor, dispatcher, bot, workflow, review_delivery_worker)
        finally:
            await finish_cleanup(resources.aclose())
            logger.info("PostRadar stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
