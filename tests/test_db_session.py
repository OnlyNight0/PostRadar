"""Tests for safe additive SQLite schema initialization."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from postradar.db.session import (
    SQLITE_BUSY_TIMEOUT_MS, SchemaCompatibilityError, create_session_factory, init_db,
)


async def create_legacy_core(
    connection, *, admin_message_id_type: str | None = None, unique_identity: bool = True,
) -> None:
    await connection.execute(text(
        "CREATE TABLE sources ("
        "id INTEGER PRIMARY KEY, telegram_chat_id INTEGER NOT NULL UNIQUE, "
        "username VARCHAR(255), title VARCHAR(255), enabled BOOLEAN NOT NULL, "
        "destination_channel_id INTEGER, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    ))
    admin_column = f", admin_message_id {admin_message_id_type}" if admin_message_id_type else ""
    await connection.execute(text(
        "CREATE TABLE source_posts ("
        "id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE, "
        "telegram_message_id INTEGER NOT NULL, original_text TEXT, sanitized_text TEXT, "
        "media_type VARCHAR(50), published_at DATETIME, "
        "captured_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "status VARCHAR(32) NOT NULL DEFAULT 'NEW'"
        f"{admin_column}{', UNIQUE(source_id, telegram_message_id)' if unique_identity else ''})"
    ))


class DatabaseInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_schema_contains_current_tables_types_and_identity_indexes(self) -> None:
        from postradar.db.base import Base

        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'fresh.db'}"
            await init_db(database_url)
            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    table_names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
                    columns = await connection.run_sync(lambda sync: {
                        name: {item["name"]: item["type"].compile(sync.dialect).upper()
                               for item in inspect(sync).get_columns(name)}
                        for name in Base.metadata.tables
                    })
                    indexes = await connection.run_sync(lambda sync: {
                        name: inspect(sync).get_indexes(name) for name in Base.metadata.tables
                    })
                    unique_constraints = await connection.run_sync(lambda sync: {
                        name: inspect(sync).get_unique_constraints(name)
                        for name in Base.metadata.tables
                    })
            finally:
                await engine.dispose()

        self.assertEqual(table_names, set(Base.metadata.tables))
        for table_name, table in Base.metadata.tables.items():
            self.assertEqual(set(columns[table_name]), set(table.columns.keys()))
        self.assertEqual(columns["source_posts"]["admin_message_id"], "INTEGER")
        self.assertEqual(columns["source_posts"]["admin_message_ids"], "TEXT")
        self.assertEqual(columns["publish_attempts"]["payload_hash"], "VARCHAR(64)")
        self.assertEqual(columns["publish_attempt_parts"]["telegram_message_ids"], "TEXT")
        self.assertIn("uq_source_post_grouped_identity", {i["name"] for i in indexes["source_posts"]})
        self.assertIn("ix_sources_category_id", {i["name"] for i in indexes["sources"]})
        self.assertIn("ix_source_posts_category_id", {i["name"] for i in indexes["source_posts"]})
        self.assertIn("uq_publish_unresolved_post", {i["name"] for i in indexes["publish_attempts"]})
        self.assertIn(
            ("source_id", "telegram_message_id"),
            {tuple(item["column_names"]) for item in unique_constraints["source_posts"]},
        )
        self.assertIn(
            ("source_post_id", "telegram_message_id"),
            {tuple(item["column_names"]) for item in unique_constraints["source_post_media"]},
        )

    async def test_concurrent_initialization_is_serialized_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'concurrent.db'}"
            await asyncio.gather(init_db(database_url), init_db(database_url))
            await init_db(database_url)

    async def test_unsupported_partial_schema_fails_before_any_schema_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'drift.db'}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await connection.execute(text(
                    "CREATE TABLE source_posts (id INTEGER PRIMARY KEY, source_id INTEGER)"
                ))
            await engine.dispose()

            with self.assertRaisesRegex(SchemaCompatibilityError, "missing required columns"):
                await init_db(database_url)

            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
                    columns = await connection.run_sync(lambda sync: {
                        item["name"] for item in inspect(sync).get_columns("source_posts")
                    })
            finally:
                await engine.dispose()
            self.assertEqual(names, {"source_posts"})
            self.assertEqual(columns, {"id", "source_id"})

    async def test_missing_core_table_is_not_silently_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'missing-core.db'}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await create_legacy_core(connection)
                await connection.execute(text("DROP TABLE source_posts"))
            await engine.dispose()

            with self.assertRaisesRegex(SchemaCompatibilityError, "missing core tables"):
                await init_db(database_url)

            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            finally:
                await engine.dispose()
            self.assertEqual(names, {"sources"})

    async def test_legacy_duplicate_identity_aborts_additive_migration_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'duplicate.db'}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await create_legacy_core(connection, unique_identity=False)
                await connection.execute(text(
                    "INSERT INTO sources (id, telegram_chat_id, enabled) VALUES (1, -1001, 1)"
                ))
                await connection.execute(text(
                    "INSERT INTO source_posts (id, source_id, telegram_message_id) VALUES (1,1,5),(2,1,5)"
                ))
            await engine.dispose()

            with self.assertRaisesRegex(SchemaCompatibilityError, "unique identity"):
                await init_db(database_url)

            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
                    columns = await connection.run_sync(lambda sync: {
                        item["name"] for item in inspect(sync).get_columns("source_posts")
                    })
                    count = await connection.scalar(text("SELECT count(*) FROM source_posts"))
            finally:
                await engine.dispose()
            self.assertEqual(tables, {"sources", "source_posts"})
            self.assertNotIn("grouped_id", columns)
            self.assertEqual(count, 2)

    async def test_application_engine_hides_bound_values_from_sql_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'diagnostic.db'}"
            factory = create_session_factory(database_url)
            engine = factory.kw["bind"]
            secret_like_content = "source text https://private.example/path"
            try:
                self.assertTrue(engine.sync_engine.hide_parameters)
                async with engine.begin() as connection:
                    await connection.execute(text(
                        "CREATE TABLE diagnostics (value TEXT UNIQUE)"
                    ))
                    await connection.execute(
                        text("INSERT INTO diagnostics (value) VALUES (:value)"),
                        {"value": secret_like_content},
                    )
                async with engine.connect() as connection:
                    with self.assertRaises(IntegrityError) as raised:
                        await connection.execute(
                            text("INSERT INTO diagnostics (value) VALUES (:value)"),
                            {"value": secret_like_content},
                        )
                diagnostic = str(raised.exception)
                self.assertNotIn(secret_like_content, diagnostic)
                self.assertIn("SQL parameters hidden", diagnostic)
            finally:
                await engine.dispose()

    async def test_existing_posts_are_preserved_when_nullable_columns_are_added(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            database_url = f"sqlite+aiosqlite:///{database_path}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await create_legacy_core(connection)
                await connection.execute(
                    text(
                        "INSERT INTO sources (id, telegram_chat_id, enabled, destination_channel_id) "
                        "VALUES (10, -10010, 1, -10020)"
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

            for name in ("source_html", "edited_html", "content_type", "classification_reason"):
                self.assertIn(name, columns)
                self.assertIsNone(row[name])
            self.assertIn("media_path", columns)
            self.assertIn("edited_text", columns)
            self.assertIn("admin_message_id", columns)
            self.assertIn("admin_message_ids", columns)
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
                await create_legacy_core(connection, admin_message_id_type="TEXT")
                await connection.execute(text(
                    "INSERT INTO sources (id, telegram_chat_id, enabled) VALUES (1, -1001, 1)"
                ))
                await connection.execute(text(
                    "INSERT INTO source_posts "
                    "(id, source_id, telegram_message_id, admin_message_id) "
                    "VALUES (2, 1, 9, '456')"
                ))
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
                factory = create_session_factory(database_url)
                async with factory() as session:
                    from postradar.db.models import SourcePost
                    loaded_post = await session.get(SourcePost, 2)
            await engine.dispose()
            await factory.kw["bind"].dispose()

        self.assertEqual(column_types["admin_message_id"], "TEXT")
        self.assertEqual(column_types["admin_message_ids"], "TEXT")
        self.assertEqual(row["id"], 2)
        self.assertEqual(row["admin_message_id"], "456")
        self.assertEqual(loaded_post.admin_message_id, 456)

    async def test_nonnumeric_legacy_admin_message_id_fails_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'bad-admin-id.db'}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await create_legacy_core(connection, admin_message_id_type="TEXT")
                await connection.execute(text(
                    "INSERT INTO sources (id, telegram_chat_id, enabled) VALUES (1, -1001, 1)"
                ))
                await connection.execute(text(
                    "INSERT INTO source_posts "
                    "(id, source_id, telegram_message_id, admin_message_id) "
                    "VALUES (2, 1, 9, 'preview-id-unknown')"
                ))
            await engine.dispose()

            with self.assertRaisesRegex(SchemaCompatibilityError, "non-numeric TEXT"):
                await init_db(database_url)

            engine = create_async_engine(database_url)
            try:
                async with engine.connect() as connection:
                    columns = await connection.run_sync(lambda sync: {
                        item["name"] for item in inspect(sync).get_columns("source_posts")
                    })
                    value = await connection.scalar(text(
                        "SELECT admin_message_id FROM source_posts WHERE id=2"
                    ))
            finally:
                await engine.dispose()
            self.assertNotIn("grouped_id", columns)
            self.assertEqual(value, "preview-id-unknown")

    async def test_populated_legacy_candidate_remains_usable_after_html_migration(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from postradar.db.base import Base
        from postradar.db.models import Source, SourcePost
        from postradar.bot.review import AdminWorkflow

        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite+aiosqlite:///{Path(directory) / 'legacy-full.db'}"
            engine = create_async_engine(database_url)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                for column in ("source_html", "edited_html", "content_type", "classification_reason"):
                    await connection.execute(text(f"ALTER TABLE source_posts DROP COLUMN {column}"))
                await connection.execute(text("INSERT INTO sources (id, telegram_chat_id, enabled, destination_channel_id) VALUES (1, -100111, 1, -100222)"))
                await connection.execute(text("INSERT INTO source_posts (id, source_id, telegram_message_id, original_text, sanitized_text, edited_text, status) VALUES (1, 1, 99, 'Original < > &', 'Legacy < > &', 'Edited < > &', 'NEW')"))
            await engine.dispose()
            await init_db(database_url)
            await init_db(database_url)
            factory = create_session_factory(database_url)
            try:
                async with factory() as session:
                    post = await session.get(SourcePost, 1)
                    self.assertEqual(post.original_text, "Original < > &")
                    self.assertEqual(post.edited_text, "Edited < > &")
                    self.assertIsNone(post.edited_html)
                    self.assertIsNone(post.content_type)
                bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)))
                workflow = AdminWorkflow(
                    bot, factory, 12345,
                    source_monitor=SimpleNamespace(
                        protection_for_source=AsyncMock(return_value=False)
                    ),
                )
                self.assertEqual(await workflow.deliver_new(), 1)
                self.assertEqual(bot.send_message.await_args.kwargs["text"], "Edited &lt; &gt; &amp;")
                self.assertEqual(await workflow.publish(1), "published")
                self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], -100222)
            finally:
                await factory.kw["bind"].dispose()

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
