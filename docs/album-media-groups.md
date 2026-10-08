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
Identical normalized captions are included once; captions with the same visible
text but different hidden hrefs or formatting remain distinct. Technical entity normalization preserves
formatting and hidden links; Gemini classifies and edits the combined candidate
in one structured operation. AD/SELF_PROMO albums remain persisted as FILTERED
and skip media downloads. Provider/validation failures enter UNCERTAIN review
with the source markup. See [text processing](text-processing.md). Successfully
downloaded photo, video, and document items retain their order. A failed item
download is logged and omitted; the current capture path can therefore persist
an album candidate with only the media items that were available. The debounce
window groups observed fragments but cannot prove that Telegram delivered every
fragment, so this is not a completeness guarantee.

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
