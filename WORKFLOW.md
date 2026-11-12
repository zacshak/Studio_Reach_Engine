# Steam SRE — Studio Reach Engine: workflow

The pipeline runs **in the cloud**. Nightly GitHub Actions runs discovery.
Triage and email scraping are manual, not triggered by discovery. You review and drive the rest from the phone-friendly web app,
which fires drafting and sending as on-demand GitHub Actions runs. Your PC no longer needs
to be on.

All local/manual commands still exist through the one launcher: `python SRE.py <command>`.

```
 discover.yml (nightly) → SRE --discover → Turso scrape_tracker + R2 media
      │
      ├─ triage: an agent calls MCP submit_triage → Pending ──► Triaged (triage_decision pass / flag)
      │
      └─ email scraping (manual GitHub Actions run): hermes.yml or kimchi.yml
                                        │
                     Web app (Cloudflare Worker) — review on your phone
                                        │
   Pending view ── Accept (→ Triaged pass) ──► Passed view ◄── Pass / Flag ──► Flagged view
                              │ Accept (Triaged + pass + email only)   (Reject / Delete = delete)
                              ▼
                          Verifying ── verify.yml: email check + media ──► dead email: back to no-email
                              ▼
                          Accepted ──(✍️ Draft button, or any agent via MCP draft_mails)──► Drafted
                                                                             │
                                          Mail view: Approve ◄───────────────┘
                                                                             │
                          Scheduled ◄────────────────────────────────────────┘
                              │ (📨 Send scheduled mails button → send.yml)
                              ▼
                          Sending (one row at a time) → Sent
                                                          │  bounce (nightly reply check):
                                                          │  Steam email → no-email · researched → Invalid
                              review-mails.yml (nightly cron) ──► Replied
```

---

## State model (Turso `scrape_tracker`)

- **`scrape_status`**: `pending` (awaiting email scraping) · `seeded` (Steam listed a
  valid one) · `scraped` (Hermes/Kimchi found a valid one) · `no_email` · `invalid`
  (a non-empty value failed validation) · `failed`.
- **`Mail_status`**: valid leads follow `Pending` → `Triaged` → `Verifying` → `Accepted` → `Drafted` →
  `Scheduled` → `Sending` → `Sent` → `Replied`; invalid leads branch to `Invalid` and
  stop. `Invalid` is quarantined and never rendered in the review views.

  | Status | Meaning | Set by |
  |---|---|---|
  | `Pending` | new lead, not triaged yet | discovery |
  | `Triaged` | triaged; `triage_decision` says `pass` or `flag` | MCP `submit_triage` |
  | `Verifying` | accepted; `verify.yml` is checking the email and the media | dashboard **Accept** or MCP `accept_games` |
  | `Accepted` | email verified and media ready; needs a draft. Only `Triaged` + `pass` + an email can be accepted | `verify.yml` |
  | `Drafted` | draft saved; awaiting your approval | `draft.yml` or MCP `save_draft` |
  | `Scheduled` | approved; **waiting**. Nothing sends it until the Send button (or a manual `--send-mails`) runs | dashboard **Approve** |
  | `Sending` | hidden claim held only while one mail is being submitted | `send.yml`, per row |
  | `Sent` | Gmail accepted the message; R2 media purged | `send.yml` |
  | `Replied` | verified reply to the retained Message-ID | `review-mails.yml` |
  | `Invalid` | unusable or verified-invalid recipient | discovery, scrapers, `send.yml` |

  Backward moves: a temporary SMTP failure returns `Sending` → `Scheduled`; a permanent
  rejection returns the row to `Drafted`; a missing draft returns it to `Accepted`;
  a recipient that turns out to be missing returns an unsent row to `Triaged` (or
  `Pending` if it was never triaged); an inconclusive check at Accept returns
  `Verifying` → `Triaged` with the email kept. Reject deletes the row; it is not a status.

  **Dead addresses** (see *Email verification* and *Bounces* below) clear the email:
  `emails=''`, `scrape_status='no_email'`, back to `Triaged` (or `Pending`), with
  `email_issue` (JSON: email, reason, source `accept`/`send`/`bounce`, time) shown as a badge
  on the card. Research a new address, then Accept again. At send time and on a bounce, an
  address that was already researched (`scrape_status='scraped'`) makes the game `Invalid`
  instead, so it cannot loop. Addresses known to be invalid are never saved again (scrapers
  and MCP `save_email` skip them).
- **Triage** (in `scrape_tracker`): `triage_decision` is `pass` / `flag` from MCP
  `submit_triage`; `triage_reason` is the reviewer's reason; `triage_criteria_id` points
  at the rubric text in the `triage_criteria` table; `triaged_at` is the UTC timestamp.
  NULL means not triaged yet (`Pending`). Only `Pending` games can be triaged by the AI, so
  a `Triaged` game never receives another AI recommendation. A human can triage a Pending
  game as pass (dashboard Pending → Accept) and switch Passed ↔ Flagged; the reason then
  records the override, e.g. `Passed by user (AI flagged: …)`. Deleting a row deletes its
  triage record with it.
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
  and saves `pass`/`flag` decisions with `submit_triage`, which stores them on the
  game's `scrape_tracker` row through the review Worker and moves it `Pending` →
  `Triaged`. See `db-mcp/README.md`. It never deletes games or re-triages a `Triaged`
  game. The dashboard header shows how many games are still untriaged.
- Hermes and Kimchi process `pending` and `failed` leads (`Pending` or `Triaged`, not yet
  accepted) and record the best published contact email. Start them from GitHub Actions; the dashboard has no button for them.

Every step below can also be done by an agent through the MCP: see the workflow-tools
table in `db-mcp/README.md` (`get_queues`, `accept_games`, `approve_mails`,
`send_scheduled_mails`, …). The MCP uses the same guarded backend as the dashboard.

## 2. Review — the web app
`https://sre-review.zacgamedev.com` (public). It opens on **Home**; the other four views
show one card at a time (swipe or ←/→ to page). Games without an email are mixed into
Pending, Passed and Flagged and carry a **📭 No email** badge. Every triaged card shows
its decision, reason and criteria.

### 🏠 Home — control panel
The dashboard refreshes itself every 30 seconds while its tab is visible, and again when you
return to the tab. It keeps your current card and skips a refresh while an action is saving,
a delete is armed or a dialog is open. The ↻ button refreshes immediately.

Counts of Pending, Triaged, Passed, Flagged, No email (not yet accepted: Pending or
Triaged without an email), Verifying email, Accepted, Drafted, Scheduled, Sent, Replied and Invalid
(Sending only while non-zero). The Pending, Passed, Flagged, Drafted and Scheduled tiles
open their view. **✍️ Draft accepted mails** (fires `draft.yml`) shows while `Accepted`
rows exist; **📨 Send scheduled mails** shows while `Scheduled` rows exist.

### ⏳ Pending (`Mail_status = Pending`, not triaged yet)
- **Accept** → triages the game as `pass` by hand (`Triaged`, reason `Passed by user`);
  it moves to Passed.
- **Reject** → deleted (rows + queued R2 cleanup).

### ✅ Passed (`Mail_status = Triaged`, `triage_decision = pass`)
- **Accept** → `Mail_status = Verifying` and starts `verify.yml` (about a minute). One
  run at a time: games accepted while a run is working are picked up by that same run
  before it finishes (a queued run then finds nothing). The page polls it: verified → `Accepted`; a dead address → pop-up error, the email is cleared
  and the game stays in Passed with a "No email" badge, ready for a new address; an
  address nobody could check either way (blocked, timed out, QEV and Abstract out) is
  **not accepted**: it stays in Passed with its email and a "couldn't verify" badge, to
  retry later. A game
  without an email is refused at once with a pop-up. Missing media (e.g. purged after an
  earlier send) is rebuilt from Steam by the verify run.
- **Flag** → `triage_decision = flag` (reason records the override); it moves to Flagged.
- **Accept all** (two taps) → accepts every passed game with an email, 10 per request;
  games without an email are skipped and the result pop-up lists any refusals.

### 🚩 Flagged (`Mail_status = Triaged`, `triage_decision = flag`)
- **Pass** → `triage_decision = pass` (reason records the override); it moves to Passed.
- **Delete** / **Delete all** (two taps) → deleted (rows + queued R2 cleanup).

Override reasons keep the AI's view: passing an AI flag gives
`Passed by user (AI flagged: <reason>)`, and switching back to the AI's decision restores
its original reason. A refused action (wrong state, no email) answers HTTP 409 and is
not kept for retry.

### 📧 Mail (`Mail_status = Drafted`)
- **Approve** / **Approve all** → `Mail_status = Scheduled`.
- **Reject** → deleted.
- **📨 Send scheduled mails** button — shown here while any `Scheduled` rows exist.

To find emails, run `hermes.yml` (Playwright + LLM) or `kimchi.yml` from GitHub Actions,
or record one with MCP `save_email_result`. A hit removes the No email badge.

### Drafting with any agent through MCP
No pipeline AI key needed: the agent writes with its own model. Use the MCP prompt
`draft_mails` with a count (e.g. 20), or call `list_draftable_games` → `get_draft_context`
→ `save_draft` yourself. Each saved draft goes `Accepted` → `Drafted` for your review.
A game is skipped (left `Accepted`) when its media is not ready, the screenshots do not
show enough to fill the template honestly, or a save keeps failing; the agent reports
each skipped game with a reason. Details: `db-mcp/README.md`.

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
**📨 Send scheduled mails** button (Home or Mail view) → fires `send.yml`. `Scheduled` only
means *waiting*; nothing is sent until this button (or a manual `--send-mails`) runs.
The run works through the `Scheduled` rows **one at a time**, paced 2–4 min apart. For
each row it reads the draft from the R2 manifest (the address was verified at Accept; there
is no second check here, and a dead address found later is caught by the nightly bounce
scan), then claims that single row `Scheduled → Sending` just before SMTP submission and flips it
`Sending → Sent` after. Rows not yet reached stay `Scheduled`, so at most one row is
`Sending` at a time. It preserves `Require_Socials` and `Socials_Data` and purges the
sent lead's R2 media. Clicking Send also queues social research for the `Scheduled` rows.
No schedule: a human presses the button, so outbound always has a gate.

**Multiple sending accounts.** The accounts are rows in `sender_accounts` (email,
provider, `daily_cap`). Each mail goes from the account with the most room left in its
cap over the last 24 hours (ties: the one that sent least), so mail spreads across them.
When every account is full, the run stops and the rest stay `Scheduled` for a later Send.
The account used is stored with the send (`mail_send_attempt.sender`, `mail_send_log`), so
the Sent-folder check and the nightly reply and bounce check read that account's mailbox.
App passwords come from the `SENDER_ACCOUNTS` secret, a JSON object
`{"you@domain.com": "app password", ...}`; `GMAIL_USER` / `GMAIL_APP_PASSWORD` still work and
its inbox is still checked for replies to older mails. An account whose login fails is
dropped for that run and the others continue. Microsoft 365 accounts sign in with OAuth
(XOAUTH2 over SMTP/IMAP, `Mail_Sender/ms_oauth.py`, app "SRE Mailer" in the xrevise.cc
tenant): connect each once with a browser sign-in. Open the link from
`python Mail_Sender/ms_oauth.py --auth-url MAILBOX` (or run the **Connect Microsoft mailbox**
workflow, `ms-connect.yml`, with only the mailbox: it prints the link), sign in as that
mailbox, copy the final `.../nativeclient?code=...` address and run the workflow again with
it as `signed_in_address` within about 10 minutes. (Device-code sign-in is blocked in this
tenant, AADSTS530035.) The refresh token is stored encrypted (key: `MS_TOKEN_KEY` secret)
in `oauth_tokens` and renewed on every use. The mailbox needs Authenticated SMTP and IMAP
turned on in the Microsoft 365 admin center. `send.yml` with `check_accounts` logs in to
every account without sending. Home shows each account's sent / cap for the last 24 hours
and why it is not sending (cap 0, no password, Microsoft not connected, login error).
Change a cap with SQL: `UPDATE sender_accounts SET daily_cap=10 WHERE email='...'`.

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

The Actions summary reports sent, deferred, invalid, uncertain, and unprocessed
counts. There is no daily email-count cap. The job stops between mails after 330 minutes
to avoid the runner's 350-minute hard timeout. Unprocessed rows stay `Scheduled`.
**Email verification** (`Email_Verifier/`) runs once, when a game is accepted
(`verify.yml`, `Mail_Sender/verify_accepted.py`): `valid` → Accepted, `invalid` → email
cleared (no-email), `unknown` → not accepted (email kept, retry later). Each check is capped
at 60 s; definitive results are cached in `email_verification_cache`:
1. Own SMTP check (`smtp_check.py`, free): syntax → DNS MX (lowest preference first;
   A-record fallback; no domain / null MX → `invalid`) → connect to the MX on port 25,
   `MAIL FROM` our sender, `RCPT TO` the address, then `RCPT TO` a random address at the
   same domain. No message is ever sent. `valid` = the address is accepted and the random
   one is refused. `invalid` = only an explicit "no such mailbox" answer (5.1.x / 5.2.1 or
   that wording). Catch-all domains (the random address is accepted too) count as `valid`:
   no checker can prove those mailboxes, QEV included, and a bounce is caught nightly.
   Policy/IP blocks (5.7.x), greylisting (4xx) and timeouts are `unknown`. Role addresses (`info@`, `contact@`) are not penalised.
2. Only `unknown` results go to QuickEmailVerification (`QEV_API_KEY`, 100 free credits a
   day).
3. If QEV is not set, out of credits (402) or failing, Abstract's Email Reputation API
   (`ABSTRACT_API_KEY`, 100 free requests a **month**, 1/s) answers this and every later
   inconclusive address in the run. Only a clear `undeliverable` (no mailbox, no domain,
   bad format) counts as `invalid`; catch-all is `valid`; anything else stays `unknown`.
   If Abstract is not set or also fails (401 bad key, 422/403 quota), the result is
   `unknown` and the game is not accepted. A fallback error is never `valid`.
Port 25 must be reachable: it is on GitHub-hosted runners, usually not on home networks.
`python Email_Verifier/verify_single.py ADDRESS [--smtp-only | --qev]` checks one address;
`python Email_Verifier/verify_bulk.py FILE.csv [--smtp-only | --qev]` checks a CSV (`email` column)
and writes `FILE_verified.csv` with result, reason, accept_all and source columns.

**Bounces** (`Mail_Sender/bounces.py`, nightly in `review-mails.yml` before the reply
scan): reads bounce notices
(mailer-daemon / postmaster) from Gmail All Mail without marking them read. Only bounces
received after this was first switched on are handled (`mail_bounce_state`), each once
(`mail_bounce_log`). Only "mailbox / domain does not exist" (DSN 5.1.x, 5.2.1 or that
wording) counts; spam blocks, full mailboxes and delays are logged and ignored. The
matching `Sent` game follows the dead-address rule above and the address is cached as
invalid. `python Mail_Sender/bounces.py --report --since YYYY-MM-DD` is a read-only check.
`Sent` means Gmail accepted the message, not proof of final delivery to the recipient.
The metadata table is additive and does not change dashboard columns. A code rollback
can leave that table in place. Preserve unresolved `Sending` rows and their metadata
during rollback so delivery evidence is not lost.

## 4. Review replies — automatic, nightly (`review-mails.yml`)
Cron `30 18 * * *` (00:00 IST). Read-only IMAP scan of the Gmail inbox (nothing is
marked read). First new bounce notices are handled (see *Bounces* above), then
verified replies to retained outgoing message IDs flip `Sent → Replied`. No mail is sent.
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
| `--draft-mails` | AI-draft cold mails for `Accepted` leads → R2 manifests (`draft.yml`; `DRAFT_LIMIT` env / the `limit` input drafts only the first N) |
| `--verify-accepted` | check email + media of `Verifying` games → `Accepted` or back to no-email (`verify.yml`) |
| `--send-mails` | send `Scheduled` mails (`send.yml`); `--dry-run`, `--limit N` locally |
| `--review-mails` | handle new bounces, then check `Sent` leads for replies → `Replied` (`review-mails.yml`) |
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
`MAIL_WRITER_API_KEY` (drafting), `QEV_API_KEY` (optional fallback for addresses the SMTP check cannot settle),
`ABSTRACT_API_KEY` (optional second fallback once QEV is out of credits),
`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `SENDER_ACCOUNTS` (JSON of email to app password for the
rotating senders), `MS_TOKEN_KEY` (encrypts Microsoft sign-in tokens), `KIMCHI_API_KEY`, and for Epic
`EPIC_TURSO_DATABASE_URL`, `EPIC_TURSO_AUTH_TOKEN`.

**Cloudflare review Worker secrets**: `TURSO_URL`, `TURSO_TOKEN`, `GH_REPO`
(`zacshak/Studio_Reach_Engine`), and `GH_PAT` with Actions write permission. Optional
var `GH_REF` sets the dispatched branch (default `master`).
R2 access uses the `MEDIA` binding. The dashboard and MCP are intentionally public.
Cloudflare Browser Integrity Check still applies to dashboard writes (`POST /api/act`),
so scripted non-browser clients get HTTP 403 there; only `GET /api/state` and `/media/`
are exempt (see `db-mcp/README.md`).

### Triage storage and state rollout

Triage lives in `scrape_tracker` (the old R2 JSON files are retired). The `Triaged` /
`Accepted` states replaced `Pending`-with-a-decision and `Writing`, and the human Keep
became a plain `pass`. `node webapp/migrate-states.mjs` is idempotent:
1. Run it once — it installs the cleanup guard for the new states, turns `kept` into
   `pass`, moves triaged `Pending` rows to `Triaged` and `Writing` rows to `Accepted`.
2. Deploy the review Worker (`webapp`), then the MCP Worker (`db-mcp`), and merge the
   Python code to `master` (GitHub Actions run from `master`).
3. Run `node webapp/migrate-states.mjs --drop-legacy` once no old Python job is running;
   it drops the retired `triage_kept` column (old Python code would re-add it).

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
