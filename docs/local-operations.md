# Local and single-instance operations

PostRadar currently uses SQLite, local media files, an aiogram long-polling
admin bot, and one Telethon user session. Run one application instance per bot
token and Telethon session. Two instances can contend for `getUpdates`, race on
the same Telegram account session, and compete for local review/capture work.
The SQLite initializer serializes schema setup, but it is not an application
singleton lock.

## Storage and startup

The local defaults are repository-relative: `./postradar.db` and
`./data/media`. They are suitable for local development only when the working
directory is stable and backed up. The Amvera manifest requests persistent
`/data` storage; its configured deployment paths must remain explicit:
`/data/postradar.db`, `/data/media`, and a distinct Telethon session base path
such as `/data/postradar_prod`. The local and deployed Telethon session files
must never share a path. Keep the database, media directory, and session on
persistent storage in the environment that owns them.

Startup checks configuration, initializes and validates the SQLite schema,
then starts the supervised Telethon monitor and bot polling. An
`Unsupported SQLite schema` error is actionable: stop startup, preserve a
backup, and arrange a reviewed migration. Do not remove tables or columns to
make startup proceed. `/captures` reports bounded scalar-capture recovery
state; `/publications` reports unresolved send attempts without candidate text.

## SQLite backup and restore

Initialization enables SQLite WAL for file-backed databases. The `-wal` file
may contain committed state not yet checkpointed into the main database; never
copy only the main `.db` file while the application is running. For a consistent
database-and-media backup, stop the single application instance cleanly, then
copy the database and `MEDIA_DIR` together. Alternatively, use SQLite's online
backup API to produce a standalone database backup, but quiesce the application
when a matching media snapshot is required. Do not treat `.db-wal` or `.db-shm`
as independent backups.

For restoration, stop the application, preserve the current files, restore the
database and matching media snapshot to their configured paths, and run
`PRAGMA integrity_check` against the restored copy before starting PostRadar.
Allow SQLite to recreate runtime sidecars. Keep backups protected like the
database because they contain copied source content and operational history.

## Dependency and environment notes

`requirements.txt` pins the direct runtime dependencies to the versions
observed in the Python 3.12 development environment and checked with the local
test suite. Their transitive dependencies are still resolved by pip, so this is
not a complete lockfile and does not guarantee byte-for-byte reproducible
builds. A full resolver lock should be generated and tested separately. Do not
install or upgrade packages in a running deployment as part of schema recovery.

The repository's `amvera.yaml` selects Python 3.12, `requirements.txt`,
`main.py`, and a persistent `/data` mount. The staging environment variable
example is documentation only; live Amvera settings and persistent files were
not inspected or changed.
