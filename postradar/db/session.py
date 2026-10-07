"""Async SQLAlchemy engine and local schema initialization."""

import logging
import sqlite3

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.engine import make_url

from postradar.db.base import Base
from postradar.db import models as _models  # noqa: F401 — register model metadata

logger = logging.getLogger(__name__)
SQLITE_BUSY_TIMEOUT_MS = 5_000


def _create_engine(database_url: str, *, enable_wal: bool = False) -> AsyncEngine:
    """Create an async engine with local SQLite concurrency settings."""
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    connect_args = {"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000} if is_sqlite else {}
    engine = create_async_engine(database_url, connect_args=connect_args)

    if is_sqlite:
        database = url.database or ""
        query_values = [
            value
            for values in url.query.values()
            for value in (values if isinstance(values, tuple) else (values,))
        ]
        use_wal = enable_wal and (
            database not in {"", ":memory:"}
            and not database.startswith("file::memory:")
            and "memory" not in query_values
        )

        @event.listens_for(engine.sync_engine, "connect")
        def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                cursor.execute("PRAGMA foreign_keys=ON")
                if use_wal:
                    try:
                        cursor.execute("PRAGMA journal_mode=WAL")
                        mode = cursor.fetchone()
                        if mode and str(mode[0]).lower() == "wal":
                            logger.debug("SQLite WAL journal mode enabled")
                    except sqlite3.OperationalError as error:
                        if "locked" not in str(error).lower():
                            raise
                        logger.warning(
                            "Could not enable SQLite WAL because the database is busy; "
                            "continuing with the current journal mode"
                        )
            finally:
                cursor.close()

    return engine


def create_session_factory(database_url: str) -> async_sessionmaker:
    """Create a session factory for application database work."""
    engine = _create_engine(database_url)
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(database_url: str) -> None:
    """Create MVP tables and safely add current nullable SQLite columns."""
    engine = _create_engine(database_url, enable_wal=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if connection.dialect.name == "sqlite":
                columns = await connection.run_sync(
                    lambda sync_connection: {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("source_posts")
                    }
                )
                additive_columns = {
                    "media_path": "TEXT",
                    "edited_text": "TEXT",
                    "admin_message_id": "INTEGER",
                    "grouped_id": "INTEGER",
                    "category_id": "INTEGER REFERENCES categories(id) ON DELETE RESTRICT",
                }
                for table_name in ("source_posts", "sources"):
                    columns = await connection.run_sync(
                        lambda sync_connection, table_name=table_name: {
                            column["name"]
                            for column in inspect(sync_connection).get_columns(table_name)
                        }
                    )
                    table_columns = additive_columns if table_name == "source_posts" else {
                        "category_id": additive_columns["category_id"]
                    }
                    for column_name, column_type in table_columns.items():
                        if column_name not in columns:
                            await connection.execute(
                                text(
                                    f"ALTER TABLE {table_name} ADD COLUMN "
                                    f"{column_name} {column_type}"
                                )
                            )
                            logger.info(
                                "Added nullable %s.%s SQLite column", table_name, column_name
                            )
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_source_post_grouped_identity "
                        "ON source_posts (source_id, grouped_id) "
                        "WHERE grouped_id IS NOT NULL"
                    )
                )
    finally:
        await engine.dispose()
