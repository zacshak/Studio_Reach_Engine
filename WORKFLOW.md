# Steam SRE — Studio Reach Engine: workflow

The pipeline runs **in the cloud**. Nightly GitHub Actions runs discovery.
Triage and email scraping are manual, not triggered by discovery. You review and drive the rest from the phone-friendly web app,
which fires drafting and sending as on-demand GitHub Actions runs. Your PC no longer needs
to be on.

All local/manual commands still exist through the one launcher: `python SRE.py <command>`.

```
 discover.yml (nightly) → SRE --discover → Turso scrape_tracker + R2 media
      │
      ├─ triage: an agent calls MCP submit_triage → scrape_tracker.triage_decision (pass / flag)
      │
      └─ email scraping (manual GitHub Actions run): hermes.yml or kimchi.yml
                                        │
                     Web app (Cloudflare Worker) — review on your phone
                                        │
   Triage tab ── Keep ──► Game Approval / No-Mail          (Reject anywhere = delete)
                              │ Accept
                              ▼
                          Writing ──(✍️ Draft button or MCP save_draft)──► Drafted
                                                                             │
                                          Mail Approval tab: Approve ◄───────┘
                                                                             │
                          Scheduled ◄────────────────────────────────────────┘
                              │ (📨 Send scheduled mails button → send.yml)
                              ▼
                          Sending (one row at a time) → Sent
                                                          │
                              review-mails.yml (nightly cron) ──► Replied
```

---

## State model (Turso `scrape_tracker`)

- **`scrape_status`**: `pending` (awaiting email scraping) · `seeded` (Steam listed a
  valid one) · `scraped` (Hermes/Kimchi found a valid one) · `no_email` · `invalid`
  (a non-empty value failed validation) · `failed`.
- **`Mail_status`**: valid leads follow `Pending` → `Writing` → `Drafted` →
  `Scheduled` → `Sending` → `Sent` → `Replied`; invalid leads branch to `Invalid` and
  stop. `Invalid` is quarantined and never rendered in the review views.

  | Status | Meaning | Set by |
  |---|---|---|
  | `Pending` | new lead awaiting your review | discovery |
  | `Writing` | accepted; needs a draft | dashboard **Accept** |
  | `Drafted` | draft saved; awaiting your approval | `draft.yml` or MCP `save_draft` |
  | `Scheduled` | approved; **waiting**. Nothing sends it until the Send button (or a manual `--send-mails`) runs | dashboard **Approve** |
  | `Sending` | hidden claim held only while one mail is being submitted | `send.yml`, per row |
  | `Sent` | Gmail accepted the message; R2 media purged | `send.yml` |
  | `Replied` | verified reply to the retained Message-ID | `review-mails.yml` |
  | `Invalid` | unusable or verified-invalid recipient | discovery, scrapers, `send.yml` |

  Backward moves: a temporary SMTP failure returns `Sending` → `Scheduled`; a permanent
  rejection returns the row to `Drafted`; a missing draft returns it to `Writing`;
  QEV `unknown` leaves it `Scheduled`. Reject deletes the row; it is not a status.
- **Triage** (in `scrape_tracker`): `triage_decision` is `pass` / `flag` from MCP
  `submit_triage`, or `kept` once you press Keep; `triage_reason` is the reviewer's reason;
  `triage_criteria_id` points at the rubric text in the `triage_criteria` table;
  `triaged_at` is the UTC timestamp. NULL means not triaged yet. A `kept` row never
  receives another AI recommendation. Deleting a row deletes its triage record with it.
  `triage_kept` (0/1) is still written alongside `kept` only so a rolled-back Worker keeps
  honouring Keeps; new code reads `triage_decision`.
- **Social enrichment**: `Require_Socials=1` queues MCP research.
  `Socials_Data` stores X, LinkedIn, Instagram, Discord, and Website JSON.
  `Socials_Request_ID` is nullable TEXT reserved for Send click deduplication.

Media (screenshots, sprite sheet, manifest with the drafted mail) lives in
**Cloudflare R2**, keyed by appid via `index.json`. The public Worker serves it through
its R2 binding. Cloud jobs use S3 credentials. Nightly sync queues inactive media for cleanup.

---

## 1. Discovery, with optional manual triage and scraping
Discovery runs nightly at `30 22 * * *` (22:30 UTC = 04:00 IST).
Triage, Hermes and Kimchi run only on demand. Hermes and Kimchi share a concurrency group.
Epic discovery (`epic-discovery.yml`) runs at the same time into its own separate database.
- Diff Steam's app list vs `known_comingsoon`, fetch details for the new appids into
  `newly_added`, seed `scrape_tracker` (`seeded` if Steam gave an email, else `pending`)
  with Steam's display `release_date`, and mirror each lead's media to R2. Unfinished
  fetch/upload work is kept in `pending_discovery` and retried on the next run.
- **Triage: MCP.** A calling agent inspects each Pending game's images and description
  and saves `pass`/`flag` recommendations with `submit_triage`, which stores them on the
  game's `scrape_tracker` row through the review Worker. See `db-mcp/README.md`.
  Non-destructive; it never deletes or changes mail status. The former GitHub Actions
  triage (`triage.yml`, `irrelevant.json`) has been retired.
- Hermes and Kimchi process `pending` and `failed` leads and record the best published
  contact email. Start them from GitHub Actions; the dashboard has no button for them.

## 2. Review — the web app
One card at a time, swipe to page. Four tabs; the app opens the first non-empty one in the
order Triage → Game Approval → Mail Approval → No-Mail.

### Triage
Pending leads with `triage_decision='flag'`. A `pass` leaves the lead in Game Approval /
No-Mail. Every card with a recommendation shows its decision, reason and criteria.
- **Keep** — sets `triage_decision='kept'`; the lead returns to its normal queue and stays there.
- **Reject** / **Reject all** — delete the lead everywhere (Turso rows + queued R2 cleanup).

### Game Approval (`Mail_status = Pending`, `scrape_status` `seeded` or `scraped`)
- **Accept** → `Mail_status = Writing`. Refused if the sprite sheet is missing in R2.
- **Reject** → deleted (rows + queued R2 cleanup).
- **✍️ Draft pending mails** button — shown here once the Approval queue is empty and
  `Writing` rows exist. Fires `draft.yml`, which drafts a cold mail for each `Writing`
  lead, writes it into the R2 manifest and moves the row to `Drafted`. Idempotent.
  Agents can draft the same leads through MCP `get_draft_context` / `save_draft`.

### No-Mail (`Mail_status = Pending`, `scrape_status` `pending`, `no_email` or `failed`)
Leads not eligible for outreach yet: awaiting scraping, no email found, or a scraper
failure. Invalid records are quarantined and hidden.
- **Reject** / **Reject all** → deleted. There is no Accept here.
- To find emails, run `hermes.yml` (Playwright + LLM) or `kimchi.yml` from GitHub Actions.
  A hit flips the lead to `scraped` and it moves to Game Approval. No hit → `no_email`.

### Mail Approval (`Mail_status = Drafted`)
- **Approve** / **Approve all** → `Mail_status = Scheduled`.
- **Reject** → deleted.
- **📨 Send scheduled mails** button — shown here while any `Scheduled` rows exist.

### Social research through MCP
The Send button queues the click's `Scheduled` snapshot once per lead.
The calling coding agent researches X, LinkedIn, Instagram, Discord, and Website,
then saves using MCP through the scoped review backend. A successful save clears `Require_Socials`.
Sending mail does not clear the request or its saved data.
There is no socials dashboard, manual ReqSocial action, or socials GitHub Actions job.
Hermes continues to scrape email addresses only.

Python initialization adds `Socials_Request_ID` without rebuilding the tracker or
resetting existing social state. The separate main migration initializes existing
flags to 0 and normalizes the JSON to the new fields. Run `node webapp/migrate-socials.mjs` as part of
the coordinated rollout, not on every Python initialization. When rolling back code,
leave the additive column and saved social state in place. Do not restart the retired
Hermes socials writer alongside MCP ownership.

## 3. Send — on demand (`send.yml`)
**📨 Send scheduled mails** button (Mail Approval) → fires `send.yml`. `Scheduled` only
means *waiting*; nothing is sent until this button (or a manual `--send-mails`) runs.
The run works through the `Scheduled` rows **one at a time**, paced 2–4 min apart. For
each row it reads the draft from the R2 manifest, verifies the address with QEV, then
claims that single row `Scheduled → Sending` just before SMTP submission and flips it
`Sending → Sent` after. Rows not yet reached stay `Scheduled`, so at most one row is
`Sending` at a time. It preserves `Require_Socials` and `Socials_Data` and purges the
sent lead's R2 media. Clicking Send also queues social research for the `Scheduled` rows.
No schedule: a human presses the button, so outbound always has a gate.

Temporary SMTP rejections (4xx) return the row to `Scheduled` and retry after 5 and
15 minutes. If both retries fail, it stays `Scheduled` for a later run. A permanent
message/recipient rejection returns it to `Drafted` for review. Authentication or
sender setup errors stop the batch without leaving the current row in `Sending`.

Every claimed send records an ownership token and Message-ID in `mail_send_attempt`.
If SMTP acceptance is uncertain or Sent cannot be recorded, media is retained and the
row remains `Sending`. The next send run checks Gmail Sent for that Message-ID and
completes only confirmed matches. Unresolved rows are not resent and do not block other
Scheduled rows. For legacy rows or unmatched messages, check Gmail Sent and resolve with
`python SRE.py --send-mails --resolve-sending APPID sent|retry`. Never choose `retry`
solely because an uncertain message is absent from search results.

The Actions summary reports sent, deferred, unknown, invalid, uncertain, and unprocessed
counts. There is no daily email-count cap. The job stops between mails after 330 minutes
to avoid the runner's 350-minute hard timeout. Unprocessed rows stay `Scheduled`.
QEV unknown/API failure never permits sending. Repeated addresses reuse verification
within the batch, and only definitive valid/invalid results are persisted.
`Sent` means Gmail accepted the message, not proof of final delivery to the recipient.
The metadata table is additive and does not change dashboard columns. A code rollback
can leave that table in place. Preserve unresolved `Sending` rows and their metadata
during rollback so delivery evidence is not lost.

## 4. Review replies — automatic, nightly (`review-mails.yml`)
Cron `30 18 * * *` (00:00 IST). Read-only IMAP scan of the Gmail inbox.
Verified replies to retained outgoing message IDs flip `Sent → Replied`. No mail is sent.
Older sends without retained message IDs require manual confirmation.

---

## Cold-mail templates (in R2)
The 4 templates are the drafter's source of truth at R2 key `cold_mails.txt`. Edit and push:
```
python SRE.py --sync-templates                          # push the tracked templates file
python SRE.py --sync-templates path\to\Cold_Mails.txt   # or push a specific file
```
No redeploy needed — the next draft run reads the new templates.

---

## Launcher reference (`python SRE.py <command>`)
| Command | Does |
|---|---|
| `--discover` | discovery → stage to Turso + R2 (the nightly job's first step) |
| `--draft-mails` | AI-draft cold mails for `Writing` leads → R2 manifests (`draft.yml`) |
| `--send-mails` | send `Scheduled` mails (`send.yml`); `--dry-run`, `--limit N` locally |
| `--review-mails` | check `Sent` leads for replies → `Replied` (`review-mails.yml`) |
| `--repair-invalid` | quarantine existing unsent rows with missing/invalid recipients |
| `--sync-templates` | push cold-mail templates → R2 |
| `--sync-media` | mirror local staged media → R2 |
| `--cleanup-r2 [--apply]` | audit/apply DB-authoritative R2 cleanup |
| `--snap-db` | dump live Turso DB → `last_cache.sqlite` (DB Browser) |
| `--review` | local Tkinter reviewer (Game / No-Mail / Mail approval) |
| `--delete [ids]` | local GUI review-before-delete those appids |
| `--irrelevants-list` | legacy local vision triage via headless `claude -p`; prints appids to feed `--delete` |
| `--noseed-urls` / `--ingest-mailids` | manual local email-scrape contract (superseded by Hermes in the cloud) |

---

## Secrets
**GitHub repo → Settings → Secrets → Actions** (the workflows):
`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `R2_PUBLIC_BASE`, `R2_BUCKET`, `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `TRIAGE_BASE_URL`, `TRIAGE_MODEL`,
`TRIAGE_API_KEY` (Hermes; also a local drafting fallback), `MAIL_WRITER_BASE_URL`, `MAIL_WRITER_MODEL`,
`MAIL_WRITER_API_KEY` (drafting), `QEV_API_KEY` (send-time email verification),
`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `KIMCHI_API_KEY`, and for Epic
`EPIC_TURSO_DATABASE_URL`, `EPIC_TURSO_AUTH_TOKEN`.

**Cloudflare review Worker secrets**: `TURSO_URL`, `TURSO_TOKEN`, `GH_REPO`
(`zacshak/Studio_Reach_Engine`), and `GH_PAT` with Actions write permission. Optional
var `GH_REF` sets the dispatched branch (default `master`).
R2 access uses the `MEDIA` binding. The dashboard and MCP are intentionally public.
Cloudflare Browser Integrity Check still applies to dashboard writes (`POST /api/act`),
so scripted non-browser clients get HTTP 403 there; only `GET /api/state` and `/media/`
are exempt (see `db-mcp/README.md`).

### Triage storage rollout

Triage moved from R2 `mcp-triage.json` / `irrelevant.json` into `scrape_tracker`.
1. `node webapp/migrate-triage.mjs` — adds the columns and `triage_criteria`, marks
   existing Keeps as `kept`, and imports `mcp-triage.json` (downloaded from the dashboard,
   or pass a file path). Additive and idempotent; it never overwrites a stored decision.
2. Deploy the review Worker (`webapp`), then the MCP Worker (`db-mcp`).
3. Run the migration again to import anything the old MCP wrote between the two deploys.
The old JSON files are no longer read and can be deleted from R2 afterwards. Rolling the
code back is safe: the new columns are ignored by older Workers, and Keeps still set
`triage_kept`.

### Cleanup deployment order

Run `node webapp/migrate-cleanup.mjs` before deploying updated cleanup or discovery code.
First stop old cleanup dispatch and allow in-flight cleanup invocations to finish.
Do not overlap old tokenless cleanup with the new token-aware Worker.
The migration adds lease ownership, an empty discovery retry table, and
insert/reactivation guards while durable cleanup jobs own a game ID.
It does not delete lead rows. If old cleanup jobs overlap live rows, it stops for review.
Nightly reconciliation queues cleanup rather than deleting objects from a stale snapshot.
The review Worker's minute cron removes objects and metadata before releasing the job.
To roll back application code, leave the additive cleanup guards and queue in place.
Do not restore tokenless cleanup while token-aware cleanup can run.

### Discovery partial completion

A confirmed HTTP 404 or 410 during a screenshot export records local missing-media
evidence. Sync skips that incomplete folder without publishing it or acknowledging
its `pending_discovery` job. The next discovery run fetches fresh Steam metadata
and retries the export. No game rows are deleted for this condition.

If all remaining jobs have this evidence and uploads succeeded, the workflow exits
zero with a warning and a **partial completion** Actions summary listing uploaded
counts and deferred appids. GitHub displays a green run, but the summary explicitly
does not claim every game was processed. Other export errors, unclassified pending
work, finder failures, database failures, and R2 failures still produce a failed run.
