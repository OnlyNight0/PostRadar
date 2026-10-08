# Durable publication and recovery

Publication uses SQLite receipts and explicit human approval. It does not provide
exactly-once Telegram delivery: a timeout, crash, or failed receipt commit can leave
a request accepted by Telegram without proof in SQLite.

## Sending

After rechecking source/message protection, PostRadar atomically changes REVIEW to
PUBLISHING and inserts a publication attempt with its ordered parts. This transaction
commits before any Telegram send. A competing Publish action cannot claim that post.
Candidate edits, skips, and per-post category changes also require REVIEW in their
atomic database updates.

The attempt stores the destination snapshot and SHA-256 payload fingerprints, not
another copy of candidate text or media. Category/destination changes after claiming
cannot redirect that attempt. Each text chunk, individual media request, or media-group
request is one part. STARTED commits before a request; CONFIRMED plus returned message
IDs commits after acknowledgment. All parts must be CONFIRMED before PUBLISHED commits.
No database transaction spans a Telegram request.

Requests have a 60-second timeout. The owner renews a five-minute lease before each
part. Other processes do not recover a claim while its lease is live. Ownership and
post state are checked before each part; expired/closed attempts cannot start another
part. A request already in flight may still be accepted after timeout or cancellation.

## Outcomes and restart

- A local validation/claim failure sends nothing. Missing files or destinations leave
  the candidate on REVIEW when the database transaction rolls back.
- Parsed explicit Telegram rejection responses can return an otherwise unsent candidate
  to REVIEW. Network, decoding, server, and arbitrary send exceptions are uncertain.
- If any part is confirmed or possibly sent, incomplete publication is blocked as
  PUBLISH_UNCERTAIN. Confirmed IDs and local media are retained. No automatic resend
  or continuation occurs.
- Recovery runs at startup and through the existing review polling mechanism, in bounded
  batches. An interrupted ACTIVE claim remains PUBLISHING until its lease expires;
  this avoids stealing another process's live work.
- Expired STARTED parts become UNKNOWN. Fully durable CONFIRMED receipts allow the final
  PUBLISHED status to be repaired without sending. An attempt with only never-sent parts
  can return to REVIEW; any partial or ambiguous send remains blocked.
- Cancellation propagates after a best-effort, joined outcome update. If SQLite remains
  unavailable, the durable STARTED claim still blocks resending until recovery.

## Administrator reconciliation

Use `/publications` in the private admin chat to list up to 20 unresolved attempts.
The report shows attempt identity, destination, part states, and known message IDs;
it excludes candidate content and raw errors. It shows at most 20 parts per attempt.

After checking that the **entire** post is present in the destination, use
`/confirm_published ATTEMPT_ID`. This records the configured administrator's assertion,
marks the post PUBLISHED, and sends nothing. It never fabricates missing Telegram
receipts. Active or already closed attempts cannot be confirmed this way. Local media
is retained on manual resolution so it is not destructively removed based only on an
operator assertion.

There is deliberately no retry/reset command for uncertain attempts. Operators must
investigate missing/partial content; safe explicit retry with duplicate-risk acknowledgment
is a separate task. Old review controls remain harmless because server-side status checks
block Publish, Edit, Skip, and per-post routing for unresolved attempts.

## Schema compatibility

`publish_attempts` and `publish_attempt_parts` are additive tables created by the existing
idempotent SQLite initialization. A partial unique index allows only one unresolved
attempt per SourcePost. Existing SourcePost columns, IDs, statuses, and published data
are not rewritten or backfilled. No existing PUBLISHED row is treated as pending work.

This change does not journal capture or solve F3. Review-preview sends also retain their
existing behavior; these receipts cover destination publication only.
