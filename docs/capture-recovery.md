# Durable scalar capture recovery

After verifying that both the Telegram source and the individual message are
unprotected, scalar capture commits a metadata-only `CAPTURE_PENDING` SourcePost.
It contains source/message identity and the original category snapshot, with no
copied text, HTML, or media. The existing identity constraint prevents duplicate
receipts. NEW/REVIEW delivery queries never select pending receipts.

The worker atomically claims the row using an attempt token and a five-minute lease.
No database transaction spans Gemini or Telegram calls. Whole recovery/processing
attempts have a four-minute timeout. Completion requires the same token, an unexpired
lease, a pending row, and an enabled source; expired workers cannot overwrite newer
results. The existing row and category snapshot are retained throughout recovery.
The category snapshot also controls the Gemini context.

Recovery reuses the existing review polling mechanism. It selects at most 10 due
markers and rotates by ID; it stops admitting further items after a 15-second batch
budget (an already started attempt can run until its own timeout). Backoff and lease
information survive restarts. Retry delays start at 30 seconds and double. After
five failed/interrupted attempts, capture becomes CAPTURE_FAILED. A hard-crashed
claim waits for lease expiry; no process steals live work. Disabled sources are
excluded and retain their markers without consuming further attempts.

Recovery re-fetches the original message and rechecks protection. Permanent source
access failures and exhausted retries become CAPTURE_FAILED; deleted messages become
CAPTURE_MISSING. Unknown protection defers processing. Confirmed protection blocks the
receipt. Protection is checked again around AI/media processing before storing content.
Diagnostics contain IDs and bounded error codes, not source material.

The current Gemini/HTML pipeline is unchanged. Its handled transient/permanent provider
errors and invalid output still produce safe UNCERTAIN source fallbacks. Those results
can complete capture if required media is available. Cancellation, timeout, required
media failure, or a failed final SQLite write retain recoverable work instead.

Each media attempt downloads to its own temporary filename inside MEDIA_DIR and
atomically promotes it to a unique final filename while holding the fenced SQLite
write claim. Empty downloads and truncated documents are rejected. Link previews are text,
not required attachments. No incomplete media post is downgraded to a text-only candidate. Cleanup
only targets the attempt's files and checks stored references; if reference checks
fail, files are retained rather than risking deletion of a successful commit.
A commit may succeed while its acknowledgment is lost: the persisted NEW/FILTERED
row excludes retries, and its media remains intact.

Use `/captures` in the private configured-admin chat to inspect up to 20 pending,
failed, or missing scalar captures; `/captures <last ID>` shows the next page.
No candidate content is included. There is no
automatic reactivation of failed or historical scrubbed records.

The F5 PROTECTION_CAPTURE_PENDING path still verifies original protection metadata
before promoting a newly observed marker into this capture pipeline. Historical
PROTECTION_UNVERIFIED rows are never recovered. PROTECTION_ALBUM_PENDING and incomplete
album markers remain blocked; observing one fragment is not proof of full membership.

## Migration and remaining boundaries

Five nullable SourcePost columns are added idempotently: capture_attempts,
capture_next_attempt_at, capture_lease_until, capture_token, and capture_error.
Existing rows, content, statuses, identities, and F2 publication receipts are not
rewritten or backfilled. Migrations are tested only against isolated SQLite fixtures.

Recovery starts after the first receipt commit. A crash or SQLite failure before
that commit can still lose an event; Telegram history reconciliation is not included.
A hard kill can also leave an attempt-owned temporary or unreferenced promoted file.
Normal cancellation/failure cleanup handles these where possible, but no broad file
cleanup or durable file journal is introduced. Restart can repeat Gemini work and
incur another provider request; it cannot duplicate SourcePost identities.

Album capture and completeness are unchanged. This implementation neither reconstructs
albums nor guarantees recovery of their missing members. F2 sending and reconciliation
are unchanged.
