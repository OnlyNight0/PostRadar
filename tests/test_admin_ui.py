"""Focused tests for the operator-facing admin bot UI."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from postradar.bot.admin_bot import admin_commands, register_admin_commands
from postradar.bot.keyboards import main_menu_keyboard, review_keyboard
from postradar.bot.management_handlers import _category_list_text, _source_list_text


class AdminUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_list_keeps_full_short_and_long_usernames(self) -> None:
        category = SimpleNamespace(name="Test A")
        sources = [
            SimpleNamespace(
                id=1,
                title="TestTarget",
                username="TestTargetChannelFuck",
                category=category,
                enabled=True,
            ),
            SimpleNamespace(
                id=2,
                title="Long username",
                username="abcdefghijklmnopqrstuvwxyzABCDEF",
                category=category,
                enabled=False,
            ),
        ]

        rendered = _source_list_text(sources)

        self.assertIn("@TestTargetChannelFuck", rendered)
        self.assertIn("@abcdefghijklmnopqrstuvwxyzABCDEF", rendered)
        self.assertIn("Категория: Test A · включён", rendered)
        self.assertIn("Категория: Test A · выключен", rendered)

    async def test_category_list_is_russian_and_avoids_raw_id_when_title_exists(self) -> None:
        item = SimpleNamespace(
            category=SimpleNamespace(
                id=7,
                name="Moscow",
                destination_title="Выйти в Москву",
                destination_channel_id=-1001234567890,
                enabled=True,
            ),
            source_count=4,
        )

        rendered = _category_list_text([item])

        self.assertIn("Категории:", rendered)
        self.assertIn("Выйти в Москву", rendered)
        self.assertIn("Источников: 4", rendered)
        self.assertNotIn("-1001234567890", rendered)

    async def test_main_and_review_keyboards_use_russian_labels_without_changing_callbacks(self) -> None:
        menu = main_menu_keyboard()
        menu_buttons = [button for row in menu.inline_keyboard for button in row]
        self.assertEqual(
            [button.text for button in menu_buttons],
            ["Источники", "Категории", "На проверке", "Статус"],
        )
        self.assertEqual(
            [button.callback_data for button in menu_buttons],
            ["menu:sources", "menu:categories", "menu:pending", "menu:status"],
        )

        review = review_keyboard(17, "Moscow")
        review_buttons = [button for row in review.inline_keyboard for button in row]
        self.assertEqual(
            [button.text for button in review_buttons],
            ["Опубликовать", "Изменить", "Пропустить", "Категория: Moscow"],
        )
        self.assertEqual(
            [button.callback_data for button in review_buttons],
            ["publish:17", "edit:17", "skip:17", "route:17"],
        )

    async def test_bot_command_menu_contains_only_implemented_commands(self) -> None:
        commands = admin_commands()
        self.assertEqual(
            [command.command for command in commands],
            ["start", "menu", "sources", "categories", "cancel"],
        )
        self.assertTrue(all(command.description for command in commands))
        self.assertTrue(all(any(char.isalpha() and ord(char) > 127 for char in command.description) for command in commands))

    async def test_command_registration_failure_is_logged_and_nonfatal(self) -> None:
        bot = SimpleNamespace(set_my_commands=AsyncMock(side_effect=RuntimeError("offline")))

        with self.assertLogs("postradar.bot.admin_bot", level="WARNING") as logs:
            await register_admin_commands(bot)

        bot.set_my_commands.assert_awaited_once()
        self.assertIn("command menu registration failed", logs.output[0])


if __name__ == "__main__":
    unittest.main()
