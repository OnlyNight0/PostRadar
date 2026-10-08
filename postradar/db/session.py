"""Async SQLAlchemy engine and local schema initialization."""

import logging
import sqlite3
from collections.abc import Iterable

from sqlalchemy import event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateIndex
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.engine import make_url

from postradar.db.base import Base
from postradar.db import models as _models  # noqa: F401 — register model metadata

logger = logging.getLogger(__name__)
SQLITE_BUSY_TIMEOUT_MS = 5_000


class SchemaCompatibilityError(RuntimeError):
    """An existing SQLite schema cannot be upgraded without explicit repair."""


_ADDITIVE_COLUMNS = {
    "sources": {"category_id"},
    "source_posts": {
        "media_path", "edited_text", "source_html", "edited_html", "content_type",
        "classification_reason", "admin_message_id", "admin_message_ids", "grouped_id",
        "category_id", "capture_attempts", "capture_next_attempt_at",
        "capture_lease_until", "capture_token", "capture_error",
    },
}


def _sqlite_affinity(type_name: str) -> str:
    name = type_name.upper()
    if "INT" in name:
        return "INTEGER"
    if any(part in name for part in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if any(part in name for part in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    if "BLOB" in name or not name.strip():
        return "BLOB"
    return "NUMERIC"


def _normalized_predicate(value: object) -> str:
    rendered = "" if value is None else str(value)
    return "".join(rendered.lower().replace('"', "").split()).strip("()")


def _schema_preflight(sync_connection) -> None:
    """Reject partial legacy tables before create_all or ALTER TABLE can mutate them."""
    inspector = inspect(sync_connection)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        return

    issues: list[str] = []
    core_tables = {"sources", "source_posts"}
    if not existing_tables.intersection(core_tables):
        raise SchemaCompatibilityError(
            "Unsupported SQLite database: existing tables were found, but no PostRadar core "
            "tables exist. Startup made no schema changes; verify DATABASE_URL before proceeding."
        )
    missing_core = sorted(core_tables - existing_tables)
    if missing_core:
        issues.append(f"missing core tables: {', '.join(missing_core)}")
    publication_tables = {"publish_attempts", "publish_attempt_parts"}
    if existing_tables.intersection(publication_tables) and not publication_tables <= existing_tables:
        issues.append(
            "publication journal tables are incomplete; both publish_attempts and "
            "publish_attempt_parts must be present together"
        )
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        actual_columns = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        optional_additions = _ADDITIVE_COLUMNS.get(table_name, set())
        missing = sorted(
            column.name for column in table.columns
            if column.name not in actual_columns and column.name not in optional_additions
        )
        if missing:
            issues.append(f"{table_name} missing required columns: {', '.join(missing)}")
        expected_pk = tuple(column.name for column in table.primary_key.columns)
        actual_pk = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
        if expected_pk != actual_pk:
            issues.append(
                f"{table_name} primary key is {actual_pk or 'missing'}; expected {expected_pk}"
            )
        actual_fks = inspector.get_foreign_keys(table_name)
        for foreign_key in table.foreign_keys:
            if foreign_key.parent.name not in actual_columns:
                continue
            expected_fk = (
                foreign_key.parent.name,
                foreign_key.column.table.name,
                foreign_key.column.name,
            )
            if not any((
                tuple(item.get("constrained_columns") or ()) == (expected_fk[0],)
                and item.get("referred_table") == expected_fk[1]
                and tuple(item.get("referred_columns") or ()) == (expected_fk[2],)
            ) for item in actual_fks):
                issues.append(
                    f"{table_name}.{expected_fk[0]} is missing its foreign key to "
                    f"{expected_fk[1]}.{expected_fk[2]}"
                )
        for column_name, actual in actual_columns.items():
            expected = table.columns.get(column_name)
            if expected is None:
                continue
            actual_affinity = _sqlite_affinity(str(actual["type"]))
            expected_affinity = _sqlite_affinity(expected.type.compile(dialect=sync_connection.dialect))
            # Historical admin preview IDs were TEXT. Numeric strings remain
            # readable through SQLAlchemy's Integer result processor.
            if table_name == "source_posts" and column_name == "admin_message_id":
                if actual_affinity in {"INTEGER", "TEXT"}:
                    continue
            if actual_affinity != expected_affinity:
                issues.append(
                    f"{table_name}.{column_name} has SQLite {actual_affinity} affinity; "
                    f"expected {expected_affinity}"
                )
        if table_name == "source_posts" and "admin_message_id" in actual_columns:
            if _sqlite_affinity(str(actual_columns["admin_message_id"]["type"])) == "TEXT":
                invalid_id = sync_connection.exec_driver_sql(
                    "SELECT EXISTS (SELECT 1 FROM source_posts "
                    "WHERE admin_message_id IS NOT NULL AND "
                    "(trim(CAST(admin_message_id AS TEXT)) = '' OR "
                    "trim(CAST(admin_message_id AS TEXT)) GLOB '*[^0-9]*'))"
                ).scalar_one()
                if invalid_id:
                    issues.append(
                        "source_posts.admin_message_id contains non-numeric TEXT values; "
                        "convert these IDs explicitly before startup"
                    )

    if issues:
        detail = "; ".join(issues)
        raise SchemaCompatibilityError(
            f"Unsupported SQLite schema: {detail}. Startup made no schema changes. "
            "Back up the database and repair it with a reviewed migration."
        )


def _existing_index_definitions(inspector, table_name: str) -> tuple[list[dict], list[dict]]:
    return inspector.get_indexes(table_name), inspector.get_unique_constraints(table_name)


def _definition_matches(
    columns: tuple[str, ...], unique: bool, predicate: object,
    actual_indexes: Iterable[dict], actual_constraints: Iterable[dict],
) -> bool:
    expected_predicate = _normalized_predicate(predicate)
    for index in actual_indexes:
        actual_predicate = _normalized_predicate(
            index.get("dialect_options", {}).get("sqlite_where")
        )
        if (
            tuple(index.get("column_names") or ()) == columns
            and bool(index.get("unique")) == unique
            and actual_predicate == expected_predicate
        ):
            return True
    if unique and not expected_predicate:
        return any(
            tuple(constraint.get("column_names") or ()) == columns
            for constraint in actual_constraints
        )
    return False


async def _ensure_expected_indexes(connection) -> None:
    """Create additive indexes; duplicate data produces an actionable safe error."""
    for table_name, table in Base.metadata.tables.items():
        actual_indexes, actual_constraints = await connection.run_sync(
            lambda sync_connection: _existing_index_definitions(
                inspect(sync_connection), table_name,
            )
        )
        indexes_by_name = {item["name"]: item for item in actual_indexes}
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            columns = tuple(column.name for column in index.columns)
            predicate = index.dialect_options["sqlite"].get("where")
            same_name = indexes_by_name.get(index.name)
            if same_name is not None and not _definition_matches(
                columns, bool(index.unique), predicate, [same_name], (),
            ):
                raise SchemaCompatibilityError(
                    f"Unsupported SQLite schema: index {index.name} has an unexpected definition. "
                    "Back up the database and repair it with a reviewed migration."
                )
            if _definition_matches(
                columns, bool(index.unique), predicate, actual_indexes, actual_constraints,
            ):
                continue
            statement = str(CreateIndex(index).compile(dialect=connection.dialect))
            try:
                await connection.exec_driver_sql(statement)
            except SQLAlchemyError as error:
                kind = type(error).__name__
                raise SchemaCompatibilityError(
                    f"Cannot create required SQLite index {index.name} on {table_name} "
                    f"({kind}). Check for duplicate or conflicting rows, back up the database, "
                    "and resolve them with a reviewed migration."
                ) from error
            actual_indexes.append({
                "name": index.name, "column_names": list(columns),
                "unique": bool(index.unique),
                "dialect_options": {"sqlite_where": predicate},
            })
            indexes_by_name[index.name] = actual_indexes[-1]

        for constraint in sorted(
            (item for item in table.constraints if item.__class__.__name__ == "UniqueConstraint"),
            key=lambda item: item.name or "",
        ):
            columns = tuple(column.name for column in constraint.columns)
            name = constraint.name or f"uq_{table_name}_{'_'.join(columns)}"
            same_name = indexes_by_name.get(name)
            if same_name is not None and not _definition_matches(
                columns, True, None, [same_name], (),
            ):
                raise SchemaCompatibilityError(
                    f"Unsupported SQLite schema: unique index {name} has an unexpected definition. "
                    "Back up the database and repair it with a reviewed migration."
                )
            if _definition_matches(columns, True, None, actual_indexes, actual_constraints):
                continue
            quoted_name = connection.dialect.identifier_preparer.quote(name)
            quoted_table = connection.dialect.identifier_preparer.quote(table_name)
            quoted_columns = ", ".join(
                connection.dialect.identifier_preparer.quote(column) for column in columns
            )
            try:
                await connection.exec_driver_sql(
                    f"CREATE UNIQUE INDEX {quoted_name} ON {quoted_table} ({quoted_columns})"
                )
            except SQLAlchemyError as error:
                kind = type(error).__name__
                raise SchemaCompatibilityError(
                    f"Cannot enforce unique identity on {table_name} ({kind}). Check for duplicate "
                    "rows, back up the database, and resolve them with a reviewed migration."
                ) from error
            actual_indexes.append({
                "name": name, "column_names": list(columns), "unique": True,
                "dialect_options": {},
            })
            indexes_by_name[name] = actual_indexes[-1]


def _validate_complete_schema(sync_connection) -> None:
    inspector = inspect(sync_connection)
    issues: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            issues.append(f"missing table {table_name}")
            continue
        actual_columns = {item["name"]: item for item in inspector.get_columns(table_name)}
        for column in table.columns:
            actual = actual_columns.get(column.name)
            if actual is None:
                issues.append(f"{table_name} missing column {column.name}")
                continue
            expected_affinity = _sqlite_affinity(column.type.compile(dialect=sync_connection.dialect))
            actual_affinity = _sqlite_affinity(str(actual["type"]))
            if table_name == "source_posts" and column.name == "admin_message_id":
                if actual_affinity in {"INTEGER", "TEXT"}:
                    continue
            if actual_affinity != expected_affinity:
                issues.append(
                    f"{table_name}.{column.name} has SQLite {actual_affinity} affinity; "
                    f"expected {expected_affinity}"
                )
    if issues:
        raise SchemaCompatibilityError(
            "SQLite schema verification failed: " + "; ".join(issues) + ". "
            "Back up the database and repair it with a reviewed migration."
        )


def _create_engine(database_url: str, *, enable_wal: bool = False) -> AsyncEngine:
    """Create an async engine with local SQLite concurrency settings."""
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    connect_args = {"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000} if is_sqlite else {}
    engine = create_async_engine(
        database_url,
        connect_args=connect_args,
        hide_parameters=True,
    )

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
    """Validate and initialize schema transactionally without rebuilding tables."""
    engine = _create_engine(database_url, enable_wal=True)
    try:
        if engine.dialect.name != "sqlite":
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            return

        async with engine.connect() as connection:
            # Acquire the SQLite writer reservation before inspection so two
            # local instances cannot both race through additive migrations.
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                await connection.run_sync(_schema_preflight)
                await connection.run_sync(Base.metadata.create_all)
                added_columns: list[tuple[str, str]] = []
                additive_columns = {
                    "media_path": "TEXT",
                    "edited_text": "TEXT",
                    "source_html": "TEXT",
                    "edited_html": "TEXT",
                    "content_type": "VARCHAR(32)",
                    "classification_reason": "TEXT",
                    "admin_message_id": "INTEGER",
                    "admin_message_ids": "TEXT",
                    "grouped_id": "INTEGER",
                    "category_id": "INTEGER REFERENCES categories(id) ON DELETE RESTRICT",
                    "capture_attempts": "INTEGER",
                    "capture_next_attempt_at": "DATETIME",
                    "capture_lease_until": "DATETIME",
                    "capture_token": "VARCHAR(32)",
                    "capture_error": "VARCHAR(120)",
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
                            added_columns.append((table_name, column_name))
                await _ensure_expected_indexes(connection)
                await connection.run_sync(_validate_complete_schema)
                await connection.commit()
                for table_name, column_name in added_columns:
                    logger.info(
                        "Added nullable %s.%s SQLite column", table_name, column_name
                    )
            except Exception:
                await connection.rollback()
                raise
    finally:
        await engine.dispose()
