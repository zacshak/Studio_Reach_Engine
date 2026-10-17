# SRE database MCP

Optional OAuth resource-server support is prepared but disabled by default. See
[OAUTH.md](OAUTH.md) for the architecture, provider requirements, local tests, and
remaining owner/identity-provider decisions. Public mode bypasses this authentication.

Independent, public Streamable HTTP MCP Worker at `/mcp`. No client authentication is required. The dashboard and sending workflows do not depend on it. Only the Steam database is in scope.

Database tools: `list_tables`, `describe_table`, and `query`. Queries accept SQLite SELECT/WITH SQL, positional `?` parameters, `page_size` (default 100, maximum 2000 per response), and `offset`. Results contain column names once, row arrays, `has_more`, and `next_offset`. There is no cumulative row limit. Joins, sorting, date predicates, batch IDs, JSON functions, and aggregates work through SQL.

## Images and agent-driven triage

All eleven tools are public. `get_game_media(appid)` returns the existing four-panel screenshot sprite sheet as an MCP image, the description, and available image names. It selects the named sprite sheet rather than assuming the fourth file is correct. `get_game_image(appid, image_name)` returns another explicitly requested image from that game's manifest. Images are limited to 5 MiB each and restricted to supported image formats.

`submit_triage({criteria, results: [{appid, decision: "flag" | "pass", reason}]})` saves recommendations in R2 `mcp-triage.json`. Submit up to 100 games per call, repeating for additional games. Criteria come from the user's current request, not a fixed genre rubric. No server-side LLM is called. The calling agent must inspect the game and apply the user's criteria.

The dashboard merges these recommendations with legacy GHA flags. `flag` goes to Triage for the human's Keep/Reject decision. `pass` clears an AI flag and leaves the game in Approval or No-Mail, according to its existing database state. Both views show the recommendation reason and criteria. Database rows and mail statuses are never changed by MCP triage. Games already human-kept, deleted, lacking indexed media, or no longer Pending are skipped. The dashboard also checks eligibility so a racing submission cannot resurrect a human decision. Subsequent submissions replace an earlier recommendation for the same eligible game. Conditional R2 writes prevent concurrent batches from losing each other's results. Obsolete recommendations are pruned on successful submissions.

The existing GHA remains on-demand only for compatibility. It is not called by MCP, and MCP recommendations take precedence over its flags. Database credentials remain read-only. The Worker now also has an R2 binding to read game media and write the recommendation document.

Repeat the same query with `next_offset` until `has_more` is false. Always specify a deterministic ORDER BY. These are live reads, not a frozen snapshot. For records changing between requests, prefer key-based iteration:

```sql
SELECT appid, game_name, Mail_status
FROM scrape_tracker
WHERE appid > ?
ORDER BY appid
```

Pass the last returned appid on the next call and reset offset to zero. Updates and deletions can still occur during a full read. No promise of a transaction spanning multiple calls is made.

Response budgets are about 1 MiB, upstream responses are bounded at 8 MiB, requests at 64 KiB, and database requests at 25 seconds. Oversized responses return an actionable error or a continuation offset, never silently truncated field values. Reduce page size or retrieve large fields using SQLite `substr()` chunks. Expensive queries may need narrower filters. Integer values outside JavaScript's safe range are returned as strings.

## Agent-driven mail drafting

1. Query `scrape_tracker` for `Mail_status='Writing'`.
2. Call `get_draft_context({appid})`. It returns game data, the actual sprite image,
   template, observation keys and a revision. Additional images remain available
   through `get_game_image`. An existing template selection is honored. Otherwise
   the template is selected deterministically by appid across published templates.
3. Study the image and fill only the requested `observation_N` values.
4. Call `save_draft({appid, revision, placeholder_values})`.

The review backend renders fixed template text itself, saves to R2 conditionally,
and atomically changes only the matching eligible Writing row to Drafted.
It never sends email or dispatches a workflow. It does not auto-trigger socials.
Stale context and competing saved drafts are rejected. Retry the identical payload
after an uncertain response. A context request after a partial save returns the
original recovery payload without asking the agent to generate another draft.

The MCP database token stays read-only. Scoped drafting calls use the `REVIEW`
service binding to the existing review Worker, not arbitrary writable SQL.
Deploy the review backend and updated GHA draft writer before enabling the new MCP
binding. The GHA writer must have first-writer-wins draft publication and guarded
MCP recovery. The separate pending audit cleanup migration must also be rolled out
in its documented order. No new schema or secret is required by MCP drafting itself.

## Agent-driven social research

There are no social controls or social views in the dashboard, and no Hermes Socials GHA.
Clicking Send scheduled mails marks that click's Scheduled rows as requested, once per game.
The immutable `Socials_Request_ID` prevents repeated Send attempts from requeuing completed research.
Requests remain pending even if GitHub dispatch fails or mail delivery succeeds.
Existing Scheduled/Sent records are not backfilled by deployment. A later Send click can
request an existing Scheduled row because that is a future send attempt.

- `get_social_requests({after_appid:0,page_size:100})` returns requested games and their
  existing JSON. Follow `next_after_appid` to read every page. There is no cumulative cap.
- The calling agent performs its own research. MCP does not search or invoke an LLM.
- `save_social_data({appid,request_id,socials})` accepts exactly X, LinkedIn, Instagram,
  Discord, and Website, each an HTTP(S) URL or null. A successful save clears Require_Socials,
  including an all-null result. It never changes mail status, recipients, or draft contents.
- `get_social_data({appid})` returns pending or completed JSON for presentation to the user.
- Identical save retries are idempotent. Different competing results, stale IDs, and deleted
  records cannot overwrite a completed result.

Rollout: stop the retired socials workflow and ensure no old Send run remains active.
Pause dashboard writes, run `node webapp/migrate-socials.mjs`, release the Python sender
that preserves requests, and deploy the review Worker and MCP. The migration saves a
local backup of the previous social fields, clears legacy requests, preserves the four
social links, removes Email and initializes Website to null where absent. It does not
queue historical sends. Keep the additive column on rollback and do not restore the old
Hermes writer or sender that clears social requests.

## Security

Use a separate Turso token with `--read-only` and `data_read` permissions only on `scrape_tracker`, `newly_added`, and `known_comingsoon` (the actual production spelling). This credential is the authorization boundary, including for joins, subqueries, and SQL functions. Never reuse the application's write token. The deployment script verifies that writes and access to a non-approved table are denied before deploying.

The configuration sets `PUBLIC_ACCESS=true`. Anyone can read personal/contact data in the three approved tables without credentials, including from browser clients. Arbitrary SQL writes remain blocked by the restricted Turso credential. Public drafting tools can save drafts and advance eligible Writing rows through the scoped review backend. Setting PUBLIC_ACCESS to false restores the existing authentication checks. Successful deployment generates a gitignored `mcp-client.json` containing only the endpoint URL.

## Setup and deployment

```powershell
npm.cmd ci
npm.cmd test
.\node_modules\.bin\wrangler.cmd deploy --dry-run
python deploy.py --env-file A:/Studio_Reach_Engine/.env --database sre
```

Turso CLI login in WSL and Wrangler login are prerequisites. Deployment creates a dedicated `sre-db-mcp` Worker. It does not alter the database schema or rows. Credential verification includes an UPDATE with `WHERE 0`, which changes no rows even if a wrongly scoped token is supplied. `credentials.json` is generated locally and ignored by Git. Keep it private. Configure the MCP client with the deployed `/mcp` URL and its `MCP_AUTH_TOKEN` as the bearer header. Rotating the MCP token invalidates existing client connections on their next request.

The compatibility date is October 14 UTC, accepted by Cloudflare during creation on October 15 IST.
