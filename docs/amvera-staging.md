# Amvera staging configuration

Configure the following environment variables in Amvera. Secret values are entered in the Amvera environment settings and must not be committed.

## Secrets

- `BOT_TOKEN`
- `TELEGRAM_API_HASH`
- `GEMINI_API_KEY`

## Normal configuration

Set the admin account ID and runtime paths explicitly:

```env
ADMIN_USER_ID=<admin Telegram user ID>
TELEGRAM_API_ID=<Telegram API ID>
TELEGRAM_SESSION=/data/postradar_prod
DATABASE_URL=sqlite+aiosqlite:////data/postradar.db
MEDIA_DIR=/data/media
GEMINI_PRIMARY_MODEL=gemini-3.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-3.8-flash
AI_EDIT_ENABLED=true
LOG_LEVEL=INFO
PYTHONUNBUFFERED=1
```

Provide actual secret values only in Amvera's environment configuration. `TELEGRAM_API_ID` and `ADMIN_USER_ID` are configuration values, not secrets.

## Persistent state

- Production uses a new, independent SQLite database at `/data/postradar.db`. The local development database is not uploaded automatically.
- Media for posts in `NEW` or `REVIEW` remains under `/data/media` until the post reaches a terminal state.
- Telethon uses `/data/postradar_prod` as its session path; Telethon stores it at `/data/postradar_prod.session`.
- The local Telethon session is separate. Never use the same session path or name for local and staging instances.
- Do not commit `.session` files, databases, `.env`, media, or credentials.
- Amvera's persistent `/data` mount is different from a repository-relative `data/` directory. The local defaults remain repository-relative and are unchanged.
