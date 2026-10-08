# Text processing and SQLite compatibility

New capture performs technical entity normalization only. `original_text` keeps
Telegram's exact plain text; `source_html` keeps its safe formatting and hidden
hrefs. Album captions retain message-ID order and deduplicate identical
normalized markup before one structured AI operation; different hidden hrefs or
formatting keep otherwise identical visible captions distinct. No legacy sanitizer runs before AI.

The installed Telethon 1.45 HTML unparser omits spoilers and adds whitespace to
pre blocks. Capture therefore adapts Telethon entities to aiogram's supported
UTF-16 HTML unparser, with escaped href attributes and canonical code-language
markup. Custom emoji remain visible glyphs; bot-specific emoji privileges are
not assumed. Unsafe source hrefs retain their visible labels without an active
link. The allowed HTML subset follows the [Telegram formatting rules](https://core.telegram.org/bots/api#formatting-options).

Gemini receives only Category name, source title/username and complete markup.
Its single structured response provides classification, short reason and edited
HTML using [SDK JSON schema output](https://googleapis.github.io/python-genai/#json-response-schema).
CONTENT enters NEW/review. AD and SELF_PROMO remain persisted as FILTERED and
skip media downloads. UNCERTAIN enters review with an admin warning. Missing
keys, disabled AI, media-only posts, exhausted transient primary/fallback errors,
incomplete/malformed JSON, invalid HTML and introduced URLs use UNCERTAIN with
source markup. Technical normalization failures fall back to escaped exact
plain source text, skip AI for the entire logical post/album, and retain media
for UNCERTAIN review. Reasons and identity/classification metadata are stored; logs
exclude post bodies, reasons, full URLs and raw processing-provider errors.

Validated `edited_html` is publishable content; `edited_text` is its decoded
plain text. `sanitized_text` is a **legacy compatibility field**: new captures
store complete source plain text there, without semantic deletion. The old
sanitizer and AI `edit()` API remain for compatibility, outside capture.

Startup adds four nullable columns to existing SQLite `source_posts`:
`source_html TEXT`, `edited_html TEXT`, `content_type VARCHAR(32)`, and
`classification_reason TEXT`. Initialization is idempotent and additive. No
backfill, row replacement, new classification of historical posts or database
wipe occurs. Existing plain-text candidates are escaped for HTML delivery.
Manual edits are plain text, update both edited fields, and preserve original
text, source markup and classification metadata.

Candidate messages/captions explicitly use HTML parse mode. Management UI has
no global parse-mode change. Limits count decoded UTF-16 units. Long content
splits at Unicode character boundaries with balanced tags reopened per chunk;
links spanning boundaries keep the same href. Review controls attach to the last
text chunk (or album context message). Long edits with an existing preview keep
the media/group in place and move controls to the final new text chunk. The
UNCERTAIN warning is review-only.
Publish remains an explicit admin action; successful Publish/Skip retains the
existing media cleanup and category routing.

Migration scope does not repair unrelated historical schema drift: existing
category columns do not receive ORM category indexes through `create_all`, and
the existing initializer assumes original MVP base columns/identity uniqueness
already exist. Databases artificially missing those base columns need separate
repair. The four new fields do not require such cleanup.

Live verification should use a test source/destination and check a Moscow event
with price and hidden ticket href, an obvious sponsored ad, a source-only
YouTube/Boosty promotion, a formatted album and an AI-unavailable fallback.
Inspect classification/persistence and review before manually publishing the
legitimate event/album; check rendered formatting and click-through URLs.
