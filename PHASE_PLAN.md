# SRE → hosted web app: phase plan

Goal: morning cron updates the DB in the cloud; review + send from phone/laptop.
Stack (Path A, keep Python): **Turso** (DB) · **Streamlit** (UI) · **GitHub Actions** (cron).
Free tiers throughout; the only non-free bit is triage (Claude vision → Anthropic API, ~cents/day).

---

## Phase 1 — DB goes remote  ← DONE (2026-06-28)
Turso DB live (`turso-db`, ap-south-1) is now the **single source of truth**. Local
`cache.sqlite` deleted (git-removed) and `turso_push.py` removed — remote-only.
- `pipeline.py`: `_rw()`/`_ro()` go to Turso via `libsql`; a `_Conn`/`_Rows` proxy buffers
  libsql cursors so every sqlite3-style call site works unchanged. `pipeline.connect()` added.
- Discovery migrated: `batch_fetch` / `find_new` / `lead_discovery` now use `pipeline.connect()`
  (was `sqlite3.connect(cache.sqlite)`), so `SRE --discover` writes Turso. Trigger
  `trg_sync_scrape_tracker` fires on Turso inserts (verified). Tables on Turso: newly_added 375,
  known_comingsoon 52k, scrape_tracker 451, snapshot_runs 6.
- Tests force `pipeline.TURSO_URL=""` → isolated temp sqlite (never prod). 15/17 pass; the 2
  failing reference the long-removed `country` column (pre-existing rot, unrelated).
- Inspect the DB via the Turso dashboard (app.turso.tech) SQL console — no local SQL files.

The unlock: get the DB off the local file so the cloud app + your phone share one source of truth.
No FastAPI (Streamlit imports `pipeline.py` directly — a separate API would just duplicate it).

**Done (code):**
- `pipeline.py` connections are env-gated: `TURSO_DATABASE_URL` set → remote Turso (via `libsql`,
  a sqlite3 drop-in); empty → local `cache.sqlite`, byte-identical to before. Local dev needs no creds.
- `turso_push.py` — uploads the current `cache.sqlite` into Turso (`--check` to test the connection first).

**You do (the only part I can't — free account):**
```bash
# 1. install the Turso CLI  (Windows, in PowerShell)
winget install Turso.Turso          # or: irm https://tur.so/install.ps1 | iex
# 2. sign up / log in (opens browser, free)
turso auth signup
# 3. create the DB
turso db create sre
# 4. grab the two values
turso db show sre --url              # -> libsql://sre-<you>.turso.io
turso db tokens create sre           # -> the auth token
```
Put both in the repo-root `.env` (already gitignored):
```
TURSO_DATABASE_URL=libsql://sre-<you>.turso.io
TURSO_AUTH_TOKEN=<token>
```
Then:
```bash
python turso_push.py --check     # "connected to Turso OK"
python turso_push.py             # loads your data up
python SRE.py --review           # same GUI, now reading/writing Turso
```
Phase 1 done when the GUI works with `.env` Turso vars set.

---

## Phase 2 — Streamlit UI
**2a DONE (2026-06-28):** full review app at `Web_POC/streamlit_app.py` — sidebar sections
Game Approval / No-Mail / Mail Approval, reusing `Reviewer_Interface` (same actions as the Tkinter
GUI), writing to Turso. Boots clean (health ok). Run `streamlit run Web_POC/streamlit_app.py`;
open the printed Network URL on a phone over the same wifi to review from phone TODAY — no deploy
needed (images served from the PC).

**2b — FOLDED INTO PHASE 3** (decided 2026-06-28): deploy + media→R2 will be built with cloud
discovery, since that's what uploads the media. Until then, local app + phone-over-wifi covers review.
- Push repo to GitHub; deploy on Streamlit Community Cloud (free); Turso creds as app secrets.
- Media: cloud has no local `Studios_To_Review/` (gitignored, ephemeral disk). Spritesheets must
  go to object storage (Cloudflare R2); staging uploads them, `Reviewer_Interface` returns R2 URLs
  in cloud mode. Build this with Phase 3 (cloud discovery is what uploads the media) to avoid
  building the R2 plumbing twice.

## Phase 3 — cloud automation
GitHub Actions cron each morning: discover → triage (Anthropic API) → stage. Paced sender +
reply-check run server-side. You wake up, open the app, review the night's batch, tap send.

---
### Audit (2026-06-28) — production check
- Connection hygiene: every `_rw()`/`_ro()` is `closing()`-wrapped; discovery scripts `conn.close()`. ✓
- `init_tracker()` is a true no-op on Turso (column order matches schema → no destructive rebuild);
  ~1.3s/process startup cost (round trips + trigger recreate). Acceptable. ✓
- Secrets: `.env` untracked; token value not in any tracked file (only the var name in code). ✓
- Bloat removed: unused `import sqlite3` ×3, dead `DB_PATH` constants, misleading "cache DB" print.
- 15/17 tests pass; the 2 failures predate the migration (they use the removed `country` column).

### Notes / ceilings
- `_ro()` read-only contract is relaxed in Turso mode (Hermes runs locally anyway).
- Remote = one network round-trip per query; fine for a personal tool. If it drags, switch
  `libsql.connect` to an embedded replica (local file that syncs).
- Deploy needs `libsql` in requirements; local Windows already has the wheel.
- **Transient-network resilience: deferred to Phase 3.** No retry around remote queries — a TCP
  reset mid-run would abort. Current usage is interactive (re-runnable), so handle it at the
  cron level (GitHub Actions job retry) when the automated runner is built, not per-query here.
