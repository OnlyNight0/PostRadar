# AGENTS.md — PostRadar

## Project
PostRadar is an experimental Telegram content-monitoring and publishing assistant.

Current stage: **v0.1 MVP / local-first prototype**.

The project has grown beyond its initial single-file prototype. Inspect the
current implementation before relying on this document for module details.

The immediate goal is NOT to build the final autonomous media system. The goal is to build the smallest reliable vertical slice that can:

1. monitor selected Telegram source channels;
2. detect new posts;
3. save source/post metadata locally;
4. preserve supported Telegram text formatting and links while processing a
   candidate;
5. send a candidate post to an admin Telegram bot;
6. allow a human admin to approve, reject, edit, or publish it;
7. publish approved content to a configured destination channel.

The system may later grow into a larger content discovery/editorial engine, but **do not prematurely implement future features**.

## Current product concept

PostRadar is a local-first source watcher with a structured Gemini processing
step and human review.

Primary workflow:

```text
Telegram source channels
        ↓
Telethon client
        ↓
protection check + Telegram entity/HTML normalization
        ↓
Gemini classification and editing (when configured)
        ↓
SQLite database and captured media
        ↓
admin bot (aiogram)
        ↓
approve / edit / reject / publish
        ↓
destination Telegram channel
```

The first version is intentionally simple.

## Technology choices

Use:

- Python 3.12+
- `asyncio`
- `Telethon` for reading source Telegram channels through a user client
- `aiogram 3` for the admin bot / publishing bot
- `SQLAlchemy 2.x` async ORM
- SQLite through `aiosqlite` for the MVP
- `pydantic-settings` for configuration
- `httpx` for future HTTP integrations
- `tenacity` for narrowly scoped retries where useful

Do NOT introduce FastAPI, Redis, Celery, Docker, PostgreSQL, n8n, Kafka, RabbitMQ, or other infrastructure unless the user explicitly asks for it or it becomes necessary for the current task.

## Architecture rules

Keep `main.py` small. It should be an application entry point, not the place where all business logic lives.

Prefer a structure similar to:

```text
PostRadar/
├── main.py
├── AGENTS.md
├── requirements.txt
├── .env.example
├── postradar/
│   ├── __init__.py
│   ├── config.py
│   ├── logging.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── models.py
│   │   └── session.py
│   ├── telegram/
│   │   ├── __init__.py
│   │   ├── source_client.py
│   │   ├── admin_bot.py
│   │   └── publisher.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── sanitizer.py
│   │   ├── monitor.py
│   │   └── candidates.py
│   └── bot/
│       ├── __init__.py
│       ├── handlers.py
│       ├── keyboards.py
│       └── states.py
└── tests/
```

This is a guideline, not a requirement to create every file immediately. Only create modules needed for the current task.

Avoid circular imports and global mutable state.

Prefer dependency injection through constructors/functions where practical.

## Core domain concepts

Keep the model simple.

### Source
A Telegram channel monitored by the Telethon client.

Possible fields:
- id
- telegram_chat_id
- username
- title
- enabled
- destination_channel_id
- created_at

### SourcePost
A captured original post or grouped album. The current model preserves original
plain text and source HTML separately from edited content; `sanitized_text` is
a legacy compatibility field, not a semantic sanitizer result for new captures.

Possible fields:
- id
- source_id
- telegram_message_id
- original_text
- source_html / edited_html
- content_type / classification_reason
- media_type
- media_reference/path if applicable
- published_at
- captured_at
- status

The pair `(source_id, telegram_message_id)` must be unique.

### Candidate
A post waiting for human review.

Possible statuses:
- NEW
- APPROVED
- REJECTED
- PUBLISHED
- FAILED

Do not over-model the database at this stage.

## Telegram responsibilities

### Telethon user client
Use Telethon only for source monitoring / reading data available to the authenticated Telegram user.

It should:
- listen for new posts from configured sources;
- identify the source and source message ID;
- collect text/caption and relevant metadata;
- avoid inserting the same source message twice.

Do not hard-code source channel IDs.

### aiogram admin bot
The admin bot is the control interface.

Initially it should be accessible only to one configured admin user.

Every handler that performs privileged actions must verify `ADMIN_USER_ID`.

Candidate controls will eventually include:
- Approve
- Reject
- Edit
- Publish

Implement only controls requested in the current task.

### Publishing
Publishing to destination channels is done with the bot account when the bot has the required administrator rights.

Do not silently publish content without an explicit human action in v0.1.

## Text processing and AI

New capture technically normalizes Telegram entities into validated HTML while
preserving original plain text, supported formatting, and safe hidden links.
Gemini performs one structured classification/editing operation when configured;
it is not a deterministic sanitizer. Local validation remains authoritative:
reject malformed or unsafe HTML, unexpected response fields, and introduced URLs.
On provider or validation failure, retain a safe source-based UNCERTAIN candidate
for human review. Never publish AI output without the existing explicit admin
action.

`postradar/services/sanitizer.py` and the old AI `edit()` interface remain for
compatibility. Do not assume the capture pipeline calls them or use them to
rewrite new source text. Keep original text and source HTML separate from edited
content, and do not remove meaningful links or attribution.

## Media and albums

Single-message media and albums use the existing capture and review paths.
Albums are grouped by Telegram `grouped_id`, store ordered media child rows, and
use an in-memory debounce window. This does not prove Telegram delivered every
album fragment. Late fragments block the affected candidate; failures to
download an individual item can leave a candidate with only the other saved
items. Do not claim complete-album recovery or silently fill missing fragments.
Respect source- and message-level content protection before downloading,
processing, previewing, or publishing content.

## Content and platform constraints

Do not implement bypasses for Telegram content-protection mechanisms.

Do not attempt to evade `noforwards` / protected-content restrictions.

Do not remove attribution merely to misrepresent authorship.

For copied third-party material, keep the architecture capable of:
- preserving source metadata internally;
- linking to the source when appropriate;
- using source posts as topic discovery rather than assuming unrestricted reuse.

Do not add scraping/evasion tricks unless explicitly requested and legitimate.

## Configuration

Never commit real tokens, API credentials, Telegram session strings, phone numbers, IDs, or secrets.

Use environment variables.

Expected variables will likely include:

```env
BOT_TOKEN=
ADMIN_USER_ID=

TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION=

DATABASE_URL=sqlite+aiosqlite:///./postradar.db

LOG_LEVEL=INFO
```

Create/update `.env.example` when configuration changes.

Do not create a real `.env` unless explicitly requested.

## Coding style

- Use async APIs end-to-end where available.
- Add type hints to public functions and non-trivial internal functions.
- Prefer small focused functions.
- Prefer explicit names over clever abstractions.
- Keep comments for non-obvious reasoning, not narration.
- Avoid large classes unless stateful behavior genuinely benefits from them.
- Avoid premature generic frameworks.
- Do not duplicate Telegram parsing logic across modules.

Use Python standard-library logging unless a later requirement justifies another logging framework.

## Error handling

Expected external failures must not crash the whole process unnecessarily.

Handle:
- Telegram connection interruptions;
- rate limits / flood waits;
- temporary API errors;
- deleted/inaccessible source messages;
- database uniqueness races.

Retry only transient failures. Do not retry programming/configuration errors forever.

Always log enough context to diagnose:
- source/channel identifier;
- message ID;
- action being attempted;
- exception.

Never log secrets.

## Database rules

For the MVP use SQLite.

Use migrations only if/when schema evolution becomes non-trivial. Do not introduce Alembic automatically for the first tiny schema unless requested.

Use uniqueness constraints for source message identity instead of relying only on application-side checks.

Database operations should be async.

SQLite compatibility initialization validates supported legacy schemas before
ordinary startup work. See [local operations](docs/local-operations.md) for
single-instance assumptions and safe database/media backup and restore guidance.

## Tests

For logic-heavy code, add focused tests.

Highest-value early tests:
- sanitizer rules;
- duplicate detection;
- source routing;
- authorization checks;
- candidate state transitions.

Do not create large test scaffolding for trivial glue code.

When changing sanitizer behavior, add/update tests showing:
- what is removed;
- what must remain untouched.

## Git / external services

Do not:
- push to GitHub;
- create repositories;
- change GitHub settings;
- deploy to Amvera;
- create or modify Neon/database resources;
- make production changes;

unless the user explicitly asks.

Local edits are allowed when the task requires them.

Never invent deployment state.

## Scope discipline

Before implementing a request:
1. inspect the existing project;
2. identify the smallest coherent change;
3. preserve working behavior;
4. avoid implementing future roadmap items not required now.

If a change implies a significant architectural decision, state it in the final report.

Do not rewrite working modules merely for stylistic reasons.

## Verification

After code changes, run the relevant checks available in the project.

At minimum when applicable:
- import / syntax check;
- focused tests;
- startup/config validation without contacting production resources.

Do not claim something works unless it was actually checked.

If a real Telegram credential is required for end-to-end verification and is unavailable, clearly distinguish:
- locally verified behavior;
- behavior that still needs live verification.

## Required final report from Codex

After each development task, finish with a concise report containing:

### Changed
What files/features were changed.

### Behavior
What the system now does.

### Verification
What commands/tests/checks were actually run and their results.

### Not changed
Relevant things intentionally left untouched.

### Next
The most logical next step, without implementing it unless requested.

If something failed or remains uncertain, say so explicitly. Do not hide errors.

## Current development priority

The first complete milestone should be:

```text
configured source channel
        ↓
Telethon receives new post
        ↓
protection check and Telegram formatting normalization
        ↓
Gemini classification/editing with local validation
        ↓
post is persisted once
        ↓
admin bot receives candidate
        ↓
admin chooses what to do
```

Prioritize reliability of the existing capture, review, and explicit publication
path before adding analytics, web sources, RSS, scoring, clustering, advanced
scheduling, or automatic posting. Do not describe Gemini as future work: it is
already part of the local pipeline when configured.
