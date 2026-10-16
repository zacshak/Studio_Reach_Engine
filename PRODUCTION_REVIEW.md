# Production reliability review, 16 October 2026

## Deployment update, 17 October 2026

Code release `a73c095` is pushed and its CI run `38955433071` passed.
The additive migration completed after write/cleanup maintenance mode was confirmed
and old cleanup leases were drained. Normal dashboard writes and cleanup resumed.

- Review Worker version: `31704d9f-55ee-4e32-8781-8b9beca31f65`.
- MCP Worker version: `b0ca7833-cb7b-493f-823a-cc5b9dd98bc9`.
- All eight MCP tools were called against the public deployed server.
- Actual synthetic drafts were saved and appeared in the dashboard state.
- Concurrent distinct saves produced one winner. Identical retries were idempotent.
- Concurrent dashboard rejection and MCP saving did not resurrect a row.
- Human Keep prevented a later MCP flag from restoring the game to Triage.
- A synthetic-row-only database trigger forced completion failure after the R2 save.
  After removing the trigger, the original saved draft recovered without regeneration.
- Social requests survived drafting. Malformed values and deleted-game contexts were rejected.
- MCP and dashboard returned identical image bytes. The official MCP client smoke test passed.
- No sending workflow was dispatched and no actual email was sent.

The four synthetic leads were rejected through the normal dashboard API. Their
cleanup runs through the normal durable queue. The test-only failure trigger was
removed and verified absent. The original checkpoint below describes the state
before this rollout, not the current deployment state.

## Verdict

The local patch addresses the concrete failures reproduced in this review.
It is a release candidate, not a claim that the live system is already fixed.
No production deployment, schema migration, email send, or lead deletion was performed during this review.

## Scope

Reviewed Steam and Epic discovery, email scraping, social enrichment, drafting,
verification, SMTP delivery, reply tracking, database transitions, media publication,
cleanup, dashboard retry behavior, MCP access, snapshots, and workflow wiring.
Assumed one operator with overlapping scheduled jobs and browser actions.
Existing public access was preserved as explicitly requested.

## Confirmed issues addressed

| Area | Failure | Correction |
| --- | --- | --- |
| Steam discovery | Seen IDs could lose unfinished fetch or upload work | Persistent pending discovery queue, transactional enqueue, publication acknowledgement, retry even without new IDs |
| Rejection | Recovery could recreate rejected records | Rejection also deletes pending discovery work, scraper results no longer create missing leads |
| Scraping | Late results could overwrite successful contacts or active sends | Conditional eligible-state updates and shared Hermes/Kimchi workflow concurrency |
| Database connection | A retry could reconnect halfway through a transaction | No transparent read retry inside a transaction and rollback of unfinished writes |
| Schema initialization | Reordering columns could drop custom columns and triggers | Removed order-only table rebuilding |
| Draft persistence | One storage or state error could abort the rest of the batch | Per-lead persistence handling with recoverable saved drafts |
| Draft publication | A late draft could recreate a deleted manifest or overwrite newer media | Conditional merge into an existing manifest |
| Sending | Local sender could substitute an unselected template | Explicit selected draft must exist |
| Missing draft | A Scheduled row became Drafted without content and could not regenerate | Guarded return to Writing |
| Replies | Old or unrelated messages could mark new sends Replied | Retained outgoing Message-ID, exact thread and sender checks, dated reply headers |
| Media sweep | Stale snapshots could delete concurrent uploads or index entries | Queue guarded cleanup rather than directly deleting R2 objects |
| Cleanup | Reinsertion and expired workers could interfere with deletion | Database insertion/reactivation guards, owner tokens, bounded leases and fail-closed migration checks |
| Bulk rejection | Partial deletion looked like total failure | Explicit deleted and blocked IDs, no automatic replay of protected IDs |
| Dashboard | Old retry intent could reverse a newer socials choice | Supersede older intent for the same game |
| Dashboard media | Stale manifests and broken images hid fresh drafts and failures | Refresh generations, stale response suppression, visible image retry |
| Socials | Malformed or substring-matched output could become permanent data | Exact keys, types, and complete observed values |
| Epic discovery | Incomplete catalog pages looked complete and failed details could lose retries | Total-count validation and persisted detail recovery |
| Snapshots | Tables could reflect different moments during export | One source read transaction and unique temporary output |
| CI and docs | MCP tests were omitted and workflow descriptions were stale | Added MCP test gate and corrected workflow documentation |

## Verification

All test runs use synthetic mail, isolated SQLite, and mocked external writes.
The existing local Workers runtime smoke test also passed with mocked outbound services.
Regression tests cover failures, retry, concurrent state changes, partial results,
missing media, and discovery interrupted before publication.
Final combined run: 186 Python tests and 70 JavaScript tests passed.
The patch whitespace check passed. Tests added for stale scraper errors also passed.

Live read-only observation during this review: 472 Pending, 114 Drafted,
2 Scheduled, 2,150 Sent, 218 Replied, and 56 Invalid rows.
There were 826 cleanup jobs, maximum recorded attempts zero, and no cleanup jobs
overlapping live tracker rows. These counts are an observation, not a completion guarantee.

## Required rollout

1. Review the patch separately from pre-existing uncommitted `db-mcp` changes.
2. Stop old cleanup dispatch and wait for in-flight old invocations to finish.
3. Run the additive cleanup migration. It creates the empty discovery queue,
   installs guards, and adds cleanup ownership. Do not backfill rejected/history IDs.
4. Deploy the updated Worker, then release the Python workflows. Resume cleanup.
5. Check a synthetic dashboard action, media loading, migration guards, and queue progress.
6. Run synthetic vision preflight before a real draft batch. Do not use a real send as a smoke test.

The cleanup lease exceeds the documented scheduled-worker invocation lifetime.
This assumption was checked against [Cloudflare's wall-time limits](https://developers.cloudflare.com/workers/platform/limits/#wall-time-limits-by-invocation-type).
Do not call this cleanup loop from an unbounded streaming handler.

## Boundaries and residual operational risks

- Older sent emails have no retained outgoing Message-ID. They are not guessed to be replies.
- Provider availability, mailbox deliverability, and the factual accuracy of AI observations cannot be guaranteed by unit tests.
- The intentionally public endpoints retain their public read and action capabilities.
- No real SMTP delivery, paid inference, production migration, or destructive production failure injection was used to validate this patch.
- Full desktop GUI rendering and mobile browser end-to-end behavior were not exercised.
- Existing cleanup jobs were not claimed to be drained. Verify completion after rollout.
- This was a reliability review, not an exhaustive penetration test or a guarantee of zero future bugs.
