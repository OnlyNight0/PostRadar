"""Tests for Category, source, and destination management services."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from postradar.bot.management_handlers import create_management_router
from postradar.bot.review import AdminWorkflow
from postradar.db.base import Base
from postradar.db.models import Category, Source, SourcePost
from postradar.services.management import (
    DestinationSetupError,
    ManagementError,
    ManagementService,
    resolve_bot_destination,
)
from postradar.telegram.source_client import ResolvedSource
from postradar.bot.states import AddSource


class FakeSourceMonitor:
    def __init__(self) -> None:
        self.resolve_source = AsyncMock()
        self.refresh_enabled_sources = AsyncMock(return_value=1)


class FakeBot:
    def __init__(self) -> None:
        self.get_chat = AsyncMock(
            return_value=SimpleNamespace(type="channel", id=-100998, title="Destination")
        )
        self.get_me = AsyncMock(return_value=SimpleNamespace(id=555))
        self.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="administrator", can_post_messages=True)
        )


class ManagementServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.bot = FakeBot()
        self.monitor = FakeSourceMonitor()
        self.management = ManagementService(self.factory, self.bot, self.monitor)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_category_creation_and_case_insensitive_duplicate_rejection(self) -> None:
        category = await self.management.create_category("Moscow")
        self.assertEqual(category.name, "Moscow")
        with self.assertRaisesRegex(ManagementError, "уже существует"):
            await self.management.create_category("moscow")

    async def test_source_uses_exactly_one_category_and_soft_remove_keeps_history(self) -> None:
        first = await self.management.create_category("First")
        second = await self.management.create_category("Second")
        resolved = ResolvedSource(-100123, "Source channel", "source_channel")
        source = await self.management.create_source(resolved, first.id)
        self.assertEqual(source.category_id, first.id)

        updated = await self.management.set_source_category(source.id, second.id)
        self.assertEqual(updated.category_id, second.id)
        self.monitor.refresh_enabled_sources.assert_awaited()
        removed = await self.management.remove_source(source.id)
        self.assertEqual(removed.id, updated.id)
        saved_source = await self.management.get_source(source.id)
        self.assertFalse(saved_source.enabled)
        self.assertEqual(saved_source.category_id, second.id)
        self.monitor.refresh_enabled_sources.assert_awaited()

        async with self.factory() as session:
            session.add(
                SourcePost(
                    source_id=source.id,
                    telegram_message_id=1,
                    category_id=first.id,
                    status="REVIEW",
                )
            )
            await session.commit()
        self.assertEqual((await self.management.get_source(source.id)).id, source.id)

    async def test_duplicate_source_is_rejected(self) -> None:
        category = await self.management.create_category("News")
        resolved = ResolvedSource(-100123, "Source", "source_channel")
        await self.management.create_source(resolved, category.id)
        with self.assertRaisesRegex(ManagementError, "уже добавлен"):
            await self.management.create_source(resolved, category.id)

    async def test_category_cannot_be_deleted_when_referenced(self) -> None:
        category = await self.management.create_category("Used")
        await self.management.create_source(
            ResolvedSource(-100124, "Source", "source_channel"), category.id
        )
        with self.assertRaisesRegex(ManagementError, "публикациями"):
            await self.management.delete_category(category.id)

    async def test_empty_category_can_be_deleted(self) -> None:
        category = await self.management.create_category("Unused")
        await self.management.delete_category(category.id)
        self.assertIsNone(await self.management.get_category(category.id))

    async def test_source_resolution_errors_are_preserved_for_safe_admin_display(self) -> None:
        self.monitor.resolve_source.side_effect = ValueError("This Telegram link is invalid.")
        with self.assertRaisesRegex(ValueError, "link is invalid"):
            await self.management.resolve_source("not-a-source")

    async def test_add_source_bot_flow_resolves_then_saves_selected_category(self) -> None:
        category = await self.management.create_category("Local")
        resolved = ResolvedSource(-100789, "Bot-added source", "bot_added_source")
        self.monitor.resolve_source.return_value = resolved
        workflow = AdminWorkflow(self.bot, self.factory, 12345)
        router = create_management_router(self.management, workflow, 12345)

        class FakeState:
            def __init__(self) -> None:
                self.current = AddSource.waiting_for_identifier.state
                self.data = {}

            async def clear(self):
                self.current = None
                self.data.clear()

            async def set_state(self, state):
                self.current = state.state

            async def get_state(self):
                return self.current

            async def update_data(self, **kwargs):
                self.data.update(kwargs)

            async def get_data(self):
                return self.data

        state = FakeState()
        add_source_handler = next(
            item.callback for item in router.message.handlers
            if item.callback.__name__ == "add_source_identifier"
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=12345),
            text="https://t.me/bot_added_source",
            answer=AsyncMock(),
        )
        await add_source_handler(message, state)
        self.monitor.resolve_source.assert_awaited_once_with("https://t.me/bot_added_source")
        self.assertEqual(state.current, AddSource.choosing_category.state)

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=12345),
            data=f"source:addcat:{category.id}",
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        )
        await router.callback_query.handlers[0].callback(callback, state)
        source = await self.management.get_source(1)
        self.assertEqual(source.telegram_chat_id, -100789)
        self.assertEqual(source.category_id, category.id)
        self.assertIsNone(source.destination_channel_id)
        self.monitor.refresh_enabled_sources.assert_awaited_once()


class DestinationValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_destination_username_requires_admin_post_permission(self) -> None:
        bot = FakeBot()
        destination = await resolve_bot_destination(bot, "@destination")
        self.assertEqual(destination, (-100998, "Destination"))
        bot.get_chat.assert_awaited_once_with("@destination")
        bot.get_chat_member.assert_awaited_once_with(-100998, 555)

    async def test_destination_numeric_id_and_permission_failure(self) -> None:
        bot = FakeBot()
        await resolve_bot_destination(bot, "-100998")
        bot.get_chat.assert_awaited_once_with(-100998)
        bot.get_chat_member.return_value = SimpleNamespace(
            status="administrator", can_post_messages=False
        )
        with self.assertRaisesRegex(DestinationSetupError, "права администратора"):
            await resolve_bot_destination(bot, "-100998")

    async def test_destination_access_failure_is_clear(self) -> None:
        bot = FakeBot()
        bot.get_chat.side_effect = RuntimeError("internal API detail")
        with self.assertRaisesRegex(DestinationSetupError, "не может получить доступ"):
            await resolve_bot_destination(bot, "@destination")


class ManagementAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_management_callback_is_rejected(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monitor = FakeSourceMonitor()
        bot = FakeBot()
        management = ManagementService(factory, bot, monitor)
        workflow = AdminWorkflow(bot, factory, 12345)
        router = create_management_router(management, workflow, 12345)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=54321),
            data="src:toggle:1",
            answer=AsyncMock(),
        )

        await router.callback_query.handlers[0].callback(callback, AsyncMock())

        callback.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
        monitor.refresh_enabled_sources.assert_not_awaited()
        await engine.dispose()


class ManagementPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_categories_and_source_assignments_survive_database_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "categories.db"
            database_url = f"sqlite+aiosqlite:///{database_path}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                category = Category(name="Seen", destination_channel_id=-100333, enabled=True)
                session.add(category)
                await session.flush()
                source = Source(telegram_chat_id=-100456, category_id=category.id, enabled=True)
                session.add(source)
                await session.commit()
                category_id, source_id = category.id, source.id
            await engine.dispose()

            check_engine = create_async_engine(database_url)
            check_factory = async_sessionmaker(check_engine, expire_on_commit=False)
            async with check_factory() as session:
                saved_category = await session.get(Category, category_id)
                saved_source = await session.get(Source, source_id)
            self.assertEqual(saved_category.name, "Seen")
            self.assertEqual(saved_source.category_id, category_id)
            await check_engine.dispose()


if __name__ == "__main__":
    unittest.main()
