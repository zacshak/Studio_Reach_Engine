# Steam Studio Lead Research Agent

Finds **newly-appeared, unreleased** Steam game pages (coming-soon games, demos,
playtests — never already-released titles) and collects a recruiting-grade
contact for each studio. Built for reliability over speed: conservative rate
limiting, local caching, and hard guards so the data stays clean.

## System 1 — Lead Discovery (which new studios exist?)

- `find_new.py` — each run snapshots Steam's "Coming Soon" list and diffs it
  against everything seen before; new pages are written to `out/new_appids_<date>.txt`.
- `lead_discovery.py` — fetches full Steam details for those new appids and
  writes a clean Excel + CSV of pre-release leads.

`fetch_app.py` and `batch_fetch.py` are the shared fetch layer (single-app and
cached/rate-limited batch); `lead_discovery.py` builds on them.

> No website scraping anywhere — this agent only reads Steam's official JSON
> APIs. The studio email it reports comes straight from Steam's
> `support_info.email` field, not from crawling sites.

## Setup

```bash
pip install -r requirements.txt
```
No API key needed — the finder reads Steam's keyless Coming-Soon search.

## Daily usage

One command does everything (finder → leads → opens the Excel):
```bash
python run_daily.py
```
Safe to run twice a day (e.g. morning and night). Each run shares one timestamp
and writes its **own** files — it never overwrites a previous run:
```
out/new_appids_<date>_<HHMM>.txt
out/leads_<date>_<HHMM>.xlsx   (+ .csv)
```
The first run on a fresh machine only memorises the baseline (no leads yet);
new pages appear from the second run onward.

Run the two stages by hand instead:
```bash
python find_new.py          # find new pre-release pages (~17 min)
python lead_discovery.py    # build leads from the latest finder output
```

Preview without waiting (does NOT touch the database):
```bash
python lead_discovery.py --sample 25
```

## Data model (Turso, or local `cache.sqlite` fallback)

| Table | Purpose |
|---|---|
| `known_comingsoon` | every pre-release appid ever seen (the diff baseline) |
| `newly_added` | one row per **named pre-release lead**, every Steam field as its own column, stamped `discovered_on`. A guard rejects released/dead/nameless pages. |
| `snapshot_runs` | log of each finder run |

## Design guarantees
- **Pre-release only**: `newly_added` physically cannot hold a released, dead,
  or nameless page (`batch_fetch.is_prerelease_lead`).
- **Ban-safe**: ≥2s between Steam calls, hard 175-per-5-min cap, retry/back-off
  on 429/403, and a local cache so re-runs cost no calls.
- **No silent gaps**: the finder uses a stable sort and refuses a snapshot that
  comes back <90% complete (no fallback — it fails loudly).
- All timestamps are IST.
