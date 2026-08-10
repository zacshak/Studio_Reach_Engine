# Epic Games Scraper

This is an independent Epic Games upcoming-title pipeline. It does not import
Steam modules and never opens or writes the Steam database.

## Commands

Print the current Epic upcoming catalog without changing state:

```powershell
python EpicGamesScraper/fetch_upcoming.py
python EpicGamesScraper/fetch_upcoming.py --json
```

Create a baseline in the separate database:

```powershell
python EpicGamesScraper/discover_epic.py --bootstrap
```

On later runs, only newly observed Epic products are reported:

```powershell
python EpicGamesScraper/discover_epic.py
```

The default database is `EpicGamesScraper/epic_cache.sqlite`. Override it with
`--db` or `EPIC_DB_PATH`. The database contains `known_comingsoon`,
`snapshot_runs`, `epic_tracker`, and `epic_details`; it does not contain Steam
tables.

For the scheduled runner, set `EPIC_TURSO_DATABASE_URL` and
`EPIC_TURSO_AUTH_TOKEN`. Local SQLite remains the fallback; the GitHub Actions
workflow sets `EPIC_TURSO_REQUIRED=1` so a missing secret fails safely.

Each discovery run uses two catalog routes: `/search` for the paginated upcoming
list, then `/offers/{offer_id}` once for every new or previously failed detail
record. Successful detail records are not fetched again on later runs.

The collector uses the current Epic catalog search provider at
`https://api.egdata.app` because Epic's storefront GraphQL is an undocumented,
Cloudflare-protected web endpoint. Set `EPIC_CATALOG_BASE_URL` or
`--source-url` to switch providers later without changing the DB contract.

Email scraping, review UI, drafting, and sending are intentionally not wired to
the Steam workflow yet. They can be added as separate Epic modules against
`epic_tracker`.
