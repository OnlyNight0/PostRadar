"""Local source and destination category management."""

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import joinedload

from postradar.db.models import Category, Source, SourcePost
from postradar.telegram.source_client import ResolvedSource, SourceResolutionError

_USERNAME = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
_NUMERIC_ID = re.compile(r"^-?\d{1,19}$")


class ManagementError(ValueError):
    """An expected validation or management error suitable for admin display."""


class DestinationSetupError(ManagementError):
    """A destination cannot be resolved or the bot cannot publish there."""


@dataclass(frozen=True)
class CategorySummary:
    category: Category
    source_count: int


class ManagementService:
    """Perform management operations with the existing Telethon and Bot clients."""

    def __init__(self, session_factory: async_sessionmaker, bot: Any, source_monitor: Any) -> None:
        self.session_factory = session_factory
        self.bot = bot
        self.source_monitor = source_monitor

    async def list_sources(self) -> list[Source]:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(Source).options(joinedload(Source.category)).order_by(Source.id)
            )
            return list(result.unique().all())

    async def get_source(self, source_id: int) -> Source | None:
        async with self.session_factory() as session:
            return await session.scalar(
                select(Source)
                .options(joinedload(Source.category))
                .where(Source.id == source_id)
            )

    async def list_categories(self, *, enabled_only: bool = False) -> list[CategorySummary]:
        async with self.session_factory() as session:
            statement = (
                select(Category, func.count(Source.id))
                .outerjoin(Source, Source.category_id == Category.id)
                .group_by(Category.id)
                .order_by(Category.name)
            )
            if enabled_only:
                statement = statement.where(Category.enabled.is_(True))
            rows = (await session.execute(statement)).all()
            return [CategorySummary(category, int(count)) for category, count in rows]

    async def get_category(self, category_id: int) -> Category | None:
        async with self.session_factory() as session:
            return await session.get(Category, category_id)

    async def create_category(self, name: str) -> Category:
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ManagementError("Название категории должно содержать от 1 до 120 символов.")
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(Category.id).where(func.lower(Category.name) == normalized.lower())
            )
            if existing is not None:
                raise ManagementError("Категория с таким названием уже существует.")
            category = Category(name=normalized, enabled=True)
            session.add(category)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ManagementError("Категория с таким названием уже существует.") from error
            await session.refresh(category)
            return category

    async def rename_category(self, category_id: int, name: str) -> Category:
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ManagementError("Название категории должно содержать от 1 до 120 символов.")
        async with self.session_factory() as session:
            category = await session.get(Category, category_id)
            if category is None:
                raise ManagementError("Категория не найдена.")
            duplicate = await session.scalar(
                select(Category.id).where(
                    func.lower(Category.name) == normalized.lower(),
                    Category.id != category_id,
                )
            )
            if duplicate is not None:
                raise ManagementError("Категория с таким названием уже существует.")
            category.name = normalized
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ManagementError("Категория с таким названием уже существует.") from error
            await session.refresh(category)
            return category

    async def delete_category(self, category_id: int) -> None:
        async with self.session_factory() as session:
            category = await session.get(Category, category_id)
            if category is None:
                raise ManagementError("Категория не найдена.")
            source_count = await session.scalar(
                select(func.count()).select_from(Source).where(Source.category_id == category_id)
            )
            post_count = await session.scalar(
                select(func.count()).select_from(SourcePost).where(SourcePost.category_id == category_id)
            )
            if source_count or post_count:
                raise ManagementError(
                    "Категория используется источниками или публикациями. Вместо удаления отключите её."
                )
            await session.delete(category)
            await session.commit()

    async def create_source(self, resolved: ResolvedSource, category_id: int) -> Source:
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(Source.id).where(Source.telegram_chat_id == resolved.telegram_chat_id)
            )
            if existing is not None:
                raise ManagementError("Этот источник Telegram уже добавлен.")
            category = await session.get(Category, category_id)
            if category is None or not category.enabled:
                raise ManagementError("Перед добавлением источника выберите включённую категорию.")
            source = Source(
                telegram_chat_id=resolved.telegram_chat_id,
                username=resolved.username,
                title=resolved.title,
                enabled=True,
                category_id=category.id,
            )
            session.add(source)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ManagementError("Этот источник Telegram уже добавлен.") from error
            await session.refresh(source)
        await self.source_monitor.refresh_enabled_sources()
        return source

    async def resolve_source(self, identifier: str) -> ResolvedSource:
        try:
            resolved = await self.source_monitor.resolve_source(identifier)
        except SourceResolutionError:
            raise
        async with self.session_factory() as session:
            exists = await session.scalar(
                select(Source.id).where(Source.telegram_chat_id == resolved.telegram_chat_id)
            )
            if exists is not None:
                raise ManagementError("Этот источник Telegram уже добавлен.")
        return resolved

    async def set_source_enabled(self, source_id: int, enabled: bool) -> Source:
        async with self.session_factory() as session:
            source = await session.get(Source, source_id)
            if source is None:
                raise ManagementError("Источник не найден.")
            source.enabled = enabled
            await session.commit()
            await session.refresh(source)
        await self.source_monitor.refresh_enabled_sources()
        return source

    async def remove_source(self, source_id: int) -> Source:
        """Soft-remove a Source so existing SourcePost history remains intact."""
        return await self.set_source_enabled(source_id, False)

    async def set_source_category(self, source_id: int, category_id: int) -> Source:
        async with self.session_factory() as session:
            source = await session.get(Source, source_id)
            category = await session.get(Category, category_id)
            if source is None:
                raise ManagementError("Источник не найден.")
            if category is None or not category.enabled:
                raise ManagementError("Выберите включённую категорию.")
            source.category_id = category.id
            await session.commit()
            await session.refresh(source)
        await self.source_monitor.refresh_enabled_sources()
        return await self.get_source(source_id) or source

    async def set_category_enabled(self, category_id: int, enabled: bool) -> Category:
        async with self.session_factory() as session:
            category = await session.get(Category, category_id)
            if category is None:
                raise ManagementError("Категория не найдена.")
            category.enabled = enabled
            await session.commit()
            await session.refresh(category)
            return category

    async def set_destination(self, category_id: int, identifier: str) -> Category:
        destination_id, destination_title = await resolve_bot_destination(self.bot, identifier)
        async with self.session_factory() as session:
            category = await session.get(Category, category_id)
            if category is None:
                raise ManagementError("Категория не найдена.")
            category.destination_channel_id = destination_id
            category.destination_title = destination_title
            await session.commit()
            await session.refresh(category)
            return category

    async def pending_counts(self) -> tuple[int, int]:
        async with self.session_factory() as session:
            new_count = await session.scalar(
                select(func.count()).select_from(SourcePost).where(SourcePost.status == "NEW")
            )
            review_count = await session.scalar(
                select(func.count()).select_from(SourcePost).where(SourcePost.status == "REVIEW")
            )
            return int(new_count or 0), int(review_count or 0)


async def resolve_bot_destination(bot: Any, identifier: str) -> tuple[int, str]:
    """Resolve a channel through Bot API and verify that the bot can publish."""
    normalized = identifier.strip()
    if _USERNAME.fullmatch(normalized):
        chat_reference: str | int = normalized
    elif _NUMERIC_ID.fullmatch(normalized) and int(normalized) != 0:
        chat_reference = int(normalized)
    else:
        raise DestinationSetupError("Введите @username или числовой ID канала Telegram.")

    try:
        chat = await bot.get_chat(chat_reference)
    except Exception as error:
        raise DestinationSetupError("Бот не может получить доступ к этому каналу назначения.") from error
    chat_type = getattr(getattr(chat, "type", None), "value", getattr(chat, "type", None))
    if chat_type != "channel":
        raise DestinationSetupError("Канал назначения должен быть каналом Telegram.")

    try:
        bot_user = await bot.get_me()
        membership = await bot.get_chat_member(chat.id, bot_user.id)
    except Exception as error:
        raise DestinationSetupError(
            "Не удалось проверить права бота. Добавьте бота в канал администратором."
        ) from error
    status = getattr(
        getattr(membership, "status", None), "value", getattr(membership, "status", None)
    )
    can_publish = status == "creator" or (
        status == "administrator" and getattr(membership, "can_post_messages", False) is True
    )
    if not can_publish:
        raise DestinationSetupError(
            "Боту нужны права администратора на публикацию сообщений в этом канале."
        )

    title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id)
    return int(chat.id), str(title)
