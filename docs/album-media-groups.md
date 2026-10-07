# Telegram album handling

PostRadar collects new Telegram messages that share a `grouped_id` for a short
1.5 second debounce window. It then stores one `SourcePost` for the album,
using the lowest Telegram message ID as the representative
`telegram_message_id`. The nullable `grouped_id` plus a per-source unique
index prevents duplicate album candidates. Messages from separate Sources
remain separate even when their `grouped_id` values match.

Album media is stored as ordered `SourcePostMedia` child rows. The old
`SourcePost.media_path` field remains in place for existing and new single
photo/video/document posts. Startup adds the nullable `grouped_id` column to
existing `source_posts`, creates the grouped-identity index, and creates the
new child table. This is additive: existing rows and media paths are retained.
The normal startup command applies the local schema additions:

```sh
PYTHONPATH=. ./venv/bin/python main.py
```

Album text is selected once from non-empty captions in message-ID order;
identical captions are included once. Sanitization and Gemini editing run once
for the resulting candidate. Successfully downloaded photo, video, and document
items retain their order. A failed item download is logged and does not prevent
the other items or text from being stored.

The admin receives one media-group preview followed by one review control
message. Publishing sends the saved items as one media group, with a caption on
the first item when it fits Telegram's caption limit. Longer text is sent in
full as separate message chunk(s). Successful Publish or Skip cleans all
album media paths inside `MEDIA_DIR`.

The debounce buffer is in memory until the album is persisted. A process crash
during that short collection window can lose unpersisted fragments; PostRadar
does not backfill old Telegram messages or use an external queue. Once a
SourcePost exists, its Category, ordered media rows, paths, and review status
are persisted and survive restart.
