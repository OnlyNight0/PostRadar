"""Tests for safe additive SQLite schema initialization."""

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from postradar.db.session import SQLITE_BUSY_TIMEOUT_MS, create_session_factory, init_db


class DatabaseInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_posts_are_preserved_when_nullable_columns_are_added(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            database_url = f"sqlite+aiosqlite:///{database_path}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE sources ("
                        "id INTEGER PRIMARY KEY, telegram_chat_id INTEGER UNIQUE, "
                        "username TEXT, title TEXT, enabled BOOLEAN NOT NULL, "
                        "destination_channel_id INTEGER, created_at DATETIME)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO sources (id, telegram_chat_id, enabled, destination_channel_id) "
                        "VALUES (10, -10010, 1, -10020)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TABLE source_posts ("
                        "id INTEGER PRIMARY KEY, source_id INTEGER, telegram_message_id INTEGER)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO source_posts (id, source_id, telegram_message_id) "
                        "VALUES (1, 10, 20)"
                    )
                )
            await engine.dispose()

            await init_db(database_url)
            await init_db(database_url)

            engine = create_async_engine(database_url)
            async with engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync_connection: {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("source_posts")
                    }
                )
                source_columns = await connection.run_sync(
                    lambda sync_connection: {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("sources")
                    }
                )
                table_names = await connection.run_sync(
                    lambda sync_connection: inspect(sync_connection).get_table_names()
                )
                indexes = await connection.run_sync(
                    lambda sync_connection: inspect(sync_connection).get_indexes("source_posts")
                )
                row = (await connection.execute(text("SELECT * FROM source_posts"))).mappings().one()
                source_row = (
                    await connection.execute(text("SELECT * FROM sources WHERE id=10"))
                ).mappings().one()
            await engine.dispose()

            self.assertIn("media_path", columns)
            self.assertIn("edited_text", columns)
            self.assertIn("admin_message_id", columns)
            self.assertIn("category_id", columns)
            self.assertIn("grouped_id", columns)
            self.assertIn("category_id", source_columns)
            self.assertEqual(row["id"], 1)
            self.assertIsNone(row["media_path"])
            self.assertIsNone(row["edited_text"])
            self.assertIsNone(row["grouped_id"])
            self.assertEqual(source_row["destination_channel_id"], -10020)
            self.assertIsNone(source_row["category_id"])
            self.assertIn("source_post_media", table_names)
            self.assertIn("uq_source_post_grouped_identity", {index["name"] for index in indexes})

    async def test_existing_posts_are_preserved_when_admin_message_id_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "admin-legacy.db"
            database_url = f"sqlite+aiosqlite:///{database_path}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE source_posts ("
                        "id INTEGER PRIMARY KEY, source_id INTEGER, telegram_message_id INTEGER, "
                        "media_path TEXT, edited_text TEXT)"
                    )
                )
                await connection.execute(
                    text("INSERT INTO source_posts (id, source_id, telegram_message_id) VALUES (2, 1, 9)")
                )
            await engine.dispose()

            await init_db(database_url)

            engine = create_async_engine(database_url)
            async with engine.connect() as connection:
                column_types = await connection.run_sync(
                    lambda sync_connection: {
                        column["name"]: column["type"].compile(sync_connection.dialect).upper()
                        for column in inspect(sync_connection).get_columns("source_posts")
                    }
                )
                row = (await connection.execute(text("SELECT * FROM source_posts"))).mappings().one()
            await engine.dispose()

        self.assertIn("INTEGER", column_types["admin_message_id"])
        self.assertEqual(row["id"], 2)
        self.assertIsNone(row["admin_message_id"])

    async def test_sqlite_connection_has_busy_timeout_foreign_keys_and_wal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "pragmas.db"
            database_url = f"sqlite+aiosqlite:///{database_path}"
            await init_db(database_url)
            factory = create_session_factory(database_url)
            engine = factory.kw["bind"]
            try:
                async with engine.connect() as connection:
                    busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))
                    foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
                    journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
            finally:
                await engine.dispose()

        self.assertEqual(busy_timeout, SQLITE_BUSY_TIMEOUT_MS)
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(str(journal_mode).lower(), "wal")


if __name__ == "__main__":
    unittest.main()
