# Steam SRE — Studio Reach Engine: workflow

The pipeline now runs **in the cloud**. A nightly GitHub Actions chain runs discovery,
triage, then email scraping; you review and drive the rest from the phone-friendly web app
(**https://your-worker.example.com**), which fires drafting and sending as
on-demand GitHub Actions runs. Your PC no longer needs to be on.

All local/manual commands still exist through the one launcher: `python SRE.py <command>`.

```
 discover.yml  →  SRE --discover  →  Turso + R2 + staged-media artifact
      │
      └──success──► triage.yml  →  triage_cloud.py  →  R2 irrelevant.json
                         │
                         └──success──► kimchi.yml  →  pending leads get scraped
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

- **`scrape_status`**: `pending` (no email yet) · `seeded` (Steam listed one) · `scraped`
  (Hermes found one) · `no_email` · `failed`.
- **`Mail_status`**: `Pending` → `Writing` → `Scheduled` → `Sent` → `Replied`.

Media (screenshots, sprite sheet, manifest with the drafted mail) lives in **Cloudflare
R2**, keyed by appid via `index.json`. The app and the cloud jobs read/write there; the
runner keeps no state between runs.

---

## 1. Discover + Triage + Scrape — automatic chain (`discover.yml` → `triage.yml` → `kimchi.yml`)
Discovery runs nightly at `30 22 * * *` (22:30 UTC = 04:00 IST), then successful runs
automatically trigger Triage, whose success automatically triggers Kimchi:
- Diff Steam's app list vs `known_comingsoon`, fetch details for the new appids into
  `newly_added`, seed `scrape_tracker` (`seeded` if Steam gave an email, else `pending`),
  mirror each lead's media to R2, and upload the staged folders as a one-day artifact.
- A **vision model** (Gemini) reviews each staged game's sprite sheet + JSON and writes the
  appids it judges irrelevant to R2 `irrelevant.json`. Non-destructive — it only flags.
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

### No-Mail (`scrape_status = pending`)
Leads with no email anywhere on their Steam page.
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

## 3. Send — on demand (`send.yml`)
**📨 Send approved** button (Mail Approval) → fires `send.yml`. Sends every `Scheduled`
lead from Gmail, paced 2–4 min apart, capped at **50 / UTC day**. Reads each draft from its
R2 manifest, flips `Scheduled → Sent`, and purges the lead's R2 media. No schedule — a human
presses the button, so outbound always has a gate.

## 4. Review replies — automatic, nightly (`review-mails.yml`)
Cron `0 2 * * *`. Read-only IMAP scan of the Gmail inbox: any `Sent` lead whose address
replied flips `Sent → Replied`. No mail is sent.

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
| `--sync-templates` | push cold-mail templates → R2 |
| `--sync-media` | mirror local staged media → R2 |
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

**Streamlit app → Manage app → Settings → Secrets** (the review app + its buttons):
the same `TURSO_*` and `R2_*` (R2 **write** keys too — triage Reject, Hermes/draft/send
buttons need them), plus `GH_REPO` (= `Meshak2002/Studio_Reach_Engine`) and `GH_PAT` (a
fine-grained token, **Actions: write**) so the buttons can dispatch the workflows.
