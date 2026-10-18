# Dashboard save and deletion behavior

Review actions remain public, as requested by the owner. Access is unchanged.

Successful rejection starts a bounded cleanup pass in the background immediately. The persistent one-minute schedule resumes remaining jobs, retries failures, and recovers if the request's background work is interrupted. A newly configured Cloudflare schedule may take time to propagate, so deletion does not depend on its first activation.

Reject deletes from scrape_tracker and newly_added and queues media_cleanup_jobs in one database transaction. known_comingsoon is never modified. Sending rows are protected. Cleanup jobs contain the captured R2 folder, so removing a row does not lose its cleanup target. No R2 deletion happens during the user request. Retrying a confirmed or uncertain delete is safe. Up to 10,000 IDs use a handful of set-based statements per request. The browser continues with additional requests for larger selections.

Run `node migrate-cleanup.mjs` before the first deployment of this version. This adds a separate table and does not rebuild or change game tables. The table has no foreign key to the deleted row.

The Worker has a one-minute scheduled trigger. Each invocation leases up to five jobs. R2 outages retry with backoff, interrupted leases expire, and metadata failures retain the jobs. Large folders are removed over successive invocations. Cleanup checks whether a game exists again before touching its folder. Index removal checks the captured folder name. Legacy and MCP flags are cleared after object removal. This is eventual cleanup, so storage usage decreases after database deletion rather than before the UI responds.

Single-card actions persist their intent in localStorage before advancing immediately. A visible Saving count tracks the serial background writes. Pending cards stay hidden during refreshes. Failed actions return to the queue with their actual error and cannot receive a conflicting action until retried. Reloading preserves unconfirmed intents for manual retry. Bulk actions still wait for server confirmation. Bulk operations and workflow dispatch cannot overtake pending or unresolved card actions. GitHub workflow dispatches are deliberately not replayed automatically after a lost response because a duplicate may start another workflow. Check GitHub Actions before manually starting it again. Media requests have timeouts and display a retryable error rather than an unexplained blank card.

Tests use synthetic SQLite data and mocked storage to verify 3,000-row bulk deletion, rollback, Sending protection, storage outages, cleanup leases, and client refresh recovery. Live smoke checks must not delete real games or dispatch email workflows. The whole repository is not certified free of bugs by these focused checks.

Rollback: retain media_cleanup_jobs and the scheduled cleanup implementation until outstanding jobs are drained. Rolling back to synchronous deletion while jobs are queued can leave stored media unprocessed. No schema rollback is required for older readers.
