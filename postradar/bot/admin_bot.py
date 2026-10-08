"""Admin bot configuration and dispatcher construction."""

import logging
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import async_sessionmaker

from postradar.bot.handlers import create_admin_router
from postradar.bot.management_handlers import create_management_router
from postradar.bot.review import AdminWorkflow
from postradar.services.management import ManagementService

logger = logging.getLogger(__name__)


def admin_commands() -> list[BotCommand]:
    """Return only commands with registered admin handlers."""
    return [
        BotCommand(command="start", description="Открыть панель управления"),
        BotCommand(command="menu", description="Показать главное меню"),
        BotCommand(command="sources", description="Управление источниками"),
        BotCommand(command="categories", description="Управление категориями"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
        BotCommand(command="publications", description="Проверить незавершённые отправки"),
        BotCommand(command="confirm_published", description="Подтвердить пост после проверки канала"),
        BotCommand(command="captures", description="Проверить незавершённый захват"),
        BotCommand(command="reviewdeliveries", description="Сверить предпросмотры администратора"),
    ]


async def register_admin_commands(bot: Bot) -> None:
    """Register the command menu without making startup depend on it."""
    try:
        await bot.set_my_commands(admin_commands())
    except Exception as error:
        logger.warning("Telegram command menu registration failed: %s", type(error).__name__)


def validate_admin_settings(bot_token: str, admin_user_id: int | None) -> None:
    """Reject missing admin bot settings before constructing the client."""
    missing = []
    if not bot_token.strip():
        missing.append("BOT_TOKEN")
    if admin_user_id is None:
        missing.append("ADMIN_USER_ID")
    if missing:
        raise ValueError("Missing required admin bot configuration: " + ", ".join(missing))


def create_admin_bot(
    bot_token: str,
    session_factory: async_sessionmaker,
    admin_user_id: int,
    source_monitor: Any,
    media_dir: str | Path = "./data/media",
) -> tuple[Bot, Dispatcher, AdminWorkflow]:
    bot = Bot(token=bot_token)
    workflow = AdminWorkflow(
        bot, session_factory, admin_user_id, media_dir=media_dir,
        source_monitor=source_monitor,
    )
    management = ManagementService(session_factory, bot, source_monitor)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(create_admin_router(workflow, admin_user_id))
    dispatcher.include_router(create_management_router(management, workflow, admin_user_id))

    async def report_startup(**_kwargs: object) -> None:
        await workflow.publication.recover_expired()
        await register_admin_commands(bot)
        logger.info("PostRadar admin bot started")

    dispatcher.startup.register(report_startup)
    return bot, dispatcher, workflow
