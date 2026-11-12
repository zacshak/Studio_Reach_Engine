# SRE database MCP

## Custom domains and non-browser clients

MCP: `https://sre-mcp.zacgamedev.com/mcp`

Dashboard and media: `https://sre-review.zacgamedev.com`

The existing `workers.dev` URLs stay enabled. The dashboard uses relative media
and API URLs, so existing bookmarks remain valid. Browser storage is per origin:
finish any pending saves on the old dashboard before switching bookmarks.

Cloudflare Browser Integrity Check is disabled only for `/mcp` on the MCP
hostname, `/media/` on the review hostname, and `GET /api/state` on the review
hostname. Dashboard write endpoints are not exempted. The exact configuration rule is
in `browser-integrity-rule.json`. Install it in the zone's `http_config_settings`
ruleset without replacing unrelated rules. Wrangler deployment does not install
this zone rule. The usual Wrangler OAuth token lacks Config Settings permission.

Applied on 2026-11-06 in zone `zacgamedev.com` as configuration rule
`9866606fef794931b3abc6db2f574f6a` (SRE public MCP and media clients).
The default-Python MCP discovery, table listing, sprite image and identical direct
media download checks passed after activation. Both dashboard origins also passed
read checks. The exemption does not apply to the legacy `workers.dev` endpoints.

Agents can GET `https://sre-review.zacgamedev.com/api/state` with default Python
headers. Its `triageReviews` map contains the stored decisions for every `Triaged` row
(not yet accepted), including both pass and flag, and `untriaged` counts Pending rows. For the complete
record, including advanced rows, query `scrape_tracker.triage_decision` instead. Fetch current state
before reviewing new candidates, and preserve existing recommendations unless the
user explicitly requests a re-review. This is a live snapshot, not a write lock.

Run `python db-mcp/check_public_access.py` from the repository root after applying
the rule. It reads real media with default Python headers and verifies the old
dashboard, without saving reviews or sending emails. A deployed domain alone does
not establish that the Browser Integrity Check exemption has been applied.

Optional OAuth resource-server support is prepared but disabled by default. See
[OAUTH.md](OAUTH.md) for the architecture, provider requirements, local tests, and
remaining owner/identity-provider decisions. Public mode bypasses this authentication.

Independent, public Streamable HTTP MCP Worker at `/mcp`. No client authentication is required. The dashboard and sending workflows do not depend on it. Only the Steam database is in scope.

Database tools: `list_tables`, `describe_table`, and `query`. Queries accept SQLite SELECT/WITH SQL, positional `?` parameters, `page_size` (default 100, maximum 2000 per response), and `offset`. Results contain column names once, row arrays, `has_more`, and `next_offset`. There is no cumulative row limit. Joins, sorting, date predicates, batch IDs, JSON functions, and aggregates work through SQL.

## Images and agent-driven triage

All 24 tools are public. `get_game_media(appid)` returns the existing four-panel screenshot sprite sheet as an MCP image, the description, and available image names. It selects the named sprite sheet rather than assuming the fourth file is correct. `get_game_image(appid, image_name)` returns another explicitly requested image from that game's manifest. Images are limited to 5 MiB each and restricted to supported image formats.

The **`triage_games` prompt** (argument `count`) gives any agent the owner's standing triage rules ([TRIAGE.md](../TRIAGE.md)) and the procedure: look at every game's sprite sheet, pull 3 to 5 more images with `get_game_image` on any doubt (never flag out of doubt, never decide from the description alone), then save with `submit_triage`. The rules are kept in `triage-rules.js`; a test keeps TRIAGE.md identical.

`submit_triage({criteria, results: [{appid, decision: "flag" | "pass", reason}]})` saves recommendations on each game's `scrape_tracker` row (`triage_decision`, `triage_reason`, `triage_criteria_id` → `triage_criteria.text`, `triaged_at`). The MCP forwards the batch to the review Worker's `/api/triage/save` through the `REVIEW` service binding, so its own Turso token stays read-only. Submit up to 100 games per call, repeating for additional games. Criteria come from the user's current request, not a fixed genre rubric. No server-side LLM is called. The calling agent must inspect the game and apply the user's criteria.

Each saved game moves `Pending` → `Triaged`. `flag` goes to the dashboard Flagged view for the human's Pass/Delete decision; `pass` goes to the Passed view. Both views show the reason and criteria. Only `Pending` (untriaged) games can be triaged: games already `Triaged`, accepted, deleted or lacking indexed media are skipped and listed in `skipped`, so a human Keep can never be overwritten. Eligibility is checked inside the same guarded `UPDATE`. Each batch is one database transaction. An identical resubmission after an uncertain response is reported as saved and keeps its original timestamp. Decisions are deleted together with their game, and remain on the row (queryable, and in `--snap-db` snapshots) after the game is accepted. Pass (dashboard Flagged view or `keep_games`) turns `flag` into `pass` and rewrites the reason to `Passed by user (AI flagged: …)`.

The former GitHub Actions triage (`triage.yml`, `irrelevant.json`) has been retired. Database credentials remain read-only. The Worker's R2 binding is used to read game media. To check whether a game was triaged, query `scrape_tracker.triage_decision` (NULL means not yet).

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

Any MCP agent can draft with its own model; no pipeline AI key is needed. The quickest
way is the **`draft_mails` prompt** with a `count` (e.g. 20): it gives the agent the
whole procedure below and ends with a report of drafted and skipped games. Skipped
games stay `Accepted`. Nothing is approved or sent.

1. Call `list_draftable_games({count})` (max 25 per call; call again for more). It
   returns the next Accepted games whose media is ready and lists the others under
   `not_ready` with a reason. (Or query `scrape_tracker` for `Mail_status='Accepted'`.)
2. Call `get_draft_context({appid})`. It returns game data, the actual sprite image,
   template, observation keys and a revision. Additional images remain available
   through `get_game_image`. An existing template selection is honored. Otherwise
   the template is selected deterministically by appid across published templates.
3. Study the image and fill only the requested `observation_N` values.
4. Call `save_draft({appid, revision, placeholder_values})`.

The review backend renders fixed template text itself, saves to R2 conditionally,
and atomically changes only the matching eligible Accepted row to Drafted.
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

## End-to-end workflow tools

These tools let an agent run the whole pipeline. Every write goes through the review
Worker (`REVIEW` binding) and uses the same guarded SQL as the dashboard buttons, so a
row can only move from the state the dashboard would accept. All are public.

| Step | Tool | Effect |
|---|---|---|
| Overview | `get_queues` | appid lists and counts for untriaged (Pending), approval / nomail (Triaged as pass, with / without email), triage (Triaged as flag), accepted, mail_approval (Drafted), scheduled, sending |
| Triage detail | `get_triage(appids≤100)` | decision, reason, full criteria text, timestamp, mail status |
| Triage Keep | `keep_games(appids≤100)` | flagged Triaged games: `flag` → `pass`, reason records the override |
| Accept | `accept_games(appids≤100)` | Triaged → Accepted; only `pass` games with an email and the sprite sheet (refuses untriaged, flagged and no-email games) |
| Reject | `reject_games(appids≤1000, confirm="DELETE")` | deletes rows and queues R2 cleanup; Sending rows are protected |
| Find email | `save_email_result(appid, status, emails?, website?)` | records `scraped` (1–5 published addresses, best first), `no_email` or `failed` for Pending or Triaged rows still awaiting an email |
| Draft | `get_draft_context` / `save_draft` | see below |
| Read draft | `get_draft(appid)` | subject, body, recipient, template, mail status |
| Approve | `approve_mails(appids≤100)` | Drafted → Scheduled |
| Redraft | `return_to_accepted(appids≤100)` | clears the saved draft, then Drafted → Accepted |
| Jobs | `run_workflow(discover\|hermes\|kimchi\|draft\|epic)` | dispatches on `master`; not retried automatically |
| Job status | `get_workflow_runs(workflow, limit)` | recent runs with status and conclusion (also `send`) |
| Send | `send_scheduled_mails(confirm="SEND_REAL_EMAILS")` | starts `send.yml`: **sends real email** to every Scheduled row and queues their social research |
| Storage | `get_r2_folder(appid \| folder)` | lists a game's R2 objects, or a deleted game's old folder to confirm cleanup |

`reject_games` and `send_scheduled_mails` need their exact `confirm` value so a stray call
cannot delete games or send mail. The workflow dispatch and run-status calls use the
review Worker's `GH_PAT`, which needs Actions read and write on the repository.

## Security

Use a separate Turso token with `--read-only` and `data_read` permissions only on `scrape_tracker`, `newly_added`, and `known_comingsoon` (the actual production spelling). This credential is the authorization boundary, including for joins, subqueries, and SQL functions. Table-valued functions are denied too: `json_each` fails with "access to json_each.value is prohibited", so pass ID lists as positional `IN (?,?,…)` parameters (up to 1000). Never reuse the application's write token. The deployment script verifies that writes and access to a non-approved table are denied before deploying.

The configuration sets `PUBLIC_ACCESS=true`. Anyone can read personal/contact data in the three approved tables without credentials, including from browser clients. Arbitrary SQL writes remain blocked by the restricted Turso credential. Public drafting and workflow tools can change review state through the scoped review backend: accept, keep, reject (delete) games, approve mails, dispatch workflows and start sending. Anyone who can reach `/mcp` can do the same, which the owner accepted. Setting PUBLIC_ACCESS to false restores the existing authentication checks. Successful deployment generates a gitignored `mcp-client.json` containing only the endpoint URL.

## Setup and deployment

```powershell
npm.cmd ci
npm.cmd test
.\node_modules\.bin\wrangler.cmd deploy --dry-run
python deploy.py --env-file A:/Studio_Reach_Engine/.env --database sre
```

Turso CLI login in WSL and Wrangler login are prerequisites. Deployment creates a dedicated `sre-db-mcp` Worker. It does not alter the database schema or rows. Credential verification includes an UPDATE with `WHERE 0`, which changes no rows even if a wrongly scoped token is supplied. `credentials.json` is generated locally and ignored by Git. Keep it private. With the current `PUBLIC_ACCESS=true`, configure the MCP client with only the `/mcp` URL; no authorization header is needed. If public access is turned off, use the deployed `MCP_AUTH_TOKEN` as the bearer header. Rotating that token invalidates existing client connections on their next request.

The compatibility date is October 14 UTC, accepted by Cloudflare during creation on October 15 IST.
