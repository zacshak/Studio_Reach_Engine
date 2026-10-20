# Steam SRE — Studio Reach Engine: workflow

The pipeline runs **in the cloud**. Nightly GitHub Actions runs discovery.
Triage and email scraping are manual, not triggered by discovery. You review and drive the rest from the phone-friendly web app,
which fires drafting and sending as on-demand GitHub Actions runs. Your PC no longer needs
to be on.

All local/manual commands still exist through the one launcher: `python SRE.py <command>`.

```
 discover.yml  →  SRE --discover  →  Turso scrape_tracker + R2 media
      │
      └──manual───► triage.yml  →  triage_cloud.py  →  R2 irrelevant.json
                         │
                         └──manual───► kimchi.yml  →  pending leads get scraped
                                        │
                     Web app (Cloudflare Worker) — review on your phone
                                        │
  Triage gate → Game Approval → No-Mail ──(🔎 Hermes button)──► emails found
                     │                                              │
                     ▼ Accept                                       ▼
                 Mail Approval ◄──(✍️ Draft button)── Gemini drafts ─┘
                     │
                     ▼ Approve  →  (📨 Send button)  →  send.yml  →  Sent
                                                                      │
                              review-mails.yml (nightly cron) ──► Replied
```

---

## State model (Turso `scrape_tracker`)

- **`scrape_status`**: `pending` (awaiting email scraping) · `seeded` (Steam listed a
  valid one) · `scraped` (Hermes/Kimchi found a valid one) · `no_email` · `invalid`
  (a non-empty value failed validation) · `failed`.
- **`Mail_status`**: valid leads follow `Pending` → `Writing` → `Drafted` →
  `Scheduled` → `Sent` → `Replied`; invalid leads branch `Pending` → `Invalid` and
  stop. `Sending` is a hidden internal claim state between `Scheduled` and `Sent`;
  `Invalid` is quarantined and never rendered in the review views.
- **Social enrichment**: `Require_Socials=1` queues MCP research.
  `Socials_Data` stores X, LinkedIn, Instagram, Discord, and Website JSON.
  `Socials_Request_ID` is nullable TEXT reserved for Send click deduplication.

Media (screenshots, sprite sheet, manifest with the drafted mail) lives in
**Cloudflare R2**, keyed by appid via `index.json`. The public Worker serves it through
its R2 binding. Cloud jobs use S3 credentials. Nightly sync queues inactive media for cleanup.

---

## 1. Discovery, with optional manual triage and scraping
Discovery runs nightly at `30 22 * * *` (22:30 UTC = 04:00 IST).
Triage and Kimchi run only on manual dispatch. Hermes and Kimchi share a concurrency group:
- Diff Steam's app list vs `known_comingsoon`, fetch details for the new appids into
  `newly_added`, seed `scrape_tracker` (`seeded` if Steam gave an email, else `pending`),
  and mirror each lead's media to R2.
- Triage reads the current `Mail_status='Pending'` set from `scrape_tracker`, loads each
  queued manifest + sprite sheet from R2, and merges the appids it judges irrelevant into
  `irrelevant.json`. Non-destructive — it only flags.
- Kimchi processes the remaining `pending` leads and records the best published contact email.

## 2. Review — the web app
One card at a time, swipe to page. Sections appear in order; you clear each on the phone.

### Triage gate
While `irrelevant.json` is non-empty the app shows ONLY the flagged leads:
- **Keep** — the AI was wrong; unflag, lead rejoins the normal queue.
- **Reject** — confirm irrelevant; purge the lead everywhere (Turso + R2).

### Game Approval (`Mail_status = Pending`)
- **Accept** → `Mail_status = Writing` (queues it for a draft).
- **Reject** → deleted (rows + R2 media).

### No-Mail (`scrape_status = pending`, `no_email`, or `failed`)
Leads that are not eligible for outreach yet: awaiting scraping, no email found,
or a scraper failure. Invalid records are quarantined and hidden.
- **🔎 Scrape emails (run Hermes)** button → fires `hermes.yml`. Hermes (headless Playwright
  + Gemini) walks every `pending` lead, scrapes the studio's site (DuckDuckGo search when
  there's no website) and extracts the best recruiting email. A hit flips the lead to
  `scraped` — it leaves No-Mail and appears in Game Approval. No hit → `no_email`.
- **Reject** → deleted.

### Mail Approval (`Mail_status = Writing`)
- **✍️ Draft pending** button → fires `draft.yml`. Gemini drafts a cold mail for each
  accepted lead missing one, picking templates in sequence (1,2,3,4,1…) and personalising
  the critique from the sprite sheet, and writes it into the lead's R2 manifest. Idempotent.
- **Approve** → `Mail_status = Scheduled`.
- **Reject** → deleted.

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
**📨 Send approved** button (Mail Approval) → fires `send.yml`. Sends every `Scheduled`
lead from Gmail, paced 2–4 min apart. Reads each draft from its
R2 manifest, atomically claims `Scheduled → Sending`, flips `Sending → Sent`, preserves
`Require_Socials` and `Socials_Data`, and purges the lead's R2 media. No schedule, a human
presses the button, so outbound always has a gate.

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
| `--noseed-urls` / `--ingest-mailids` | manual local email-scrape contract (superseded by Hermes in the cloud) |

---

## Secrets
**GitHub repo → Settings → Secrets → Actions** (the workflows):
`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `R2_PUBLIC_BASE`, `R2_BUCKET`, `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `TRIAGE_BASE_URL`, `TRIAGE_MODEL`,
`TRIAGE_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`.

**Cloudflare review Worker secrets**: `TURSO_URL`, `TURSO_TOKEN`, `GH_REPO`
(`zacshak/Studio_Reach_Engine`), and `GH_PAT` with Actions write permission.
R2 access uses the `MEDIA` binding. The dashboard and MCP are intentionally public.

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
