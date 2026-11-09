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

Each discovery run pages through `GET /search/v2/search`
(`offerType=BASE_GAME&sortBy=upcoming`, 100 offers per page). Search results are
full offer documents, so tracker rows with missing or failed details that are in
the current catalog get their `epic_details` row from the search document. Only
persisted rows absent from the latest catalog use `GET /offers/{offer_id}`.
A changed offer ID also refreshes the details. Complete details for the same
offer are not fetched again. When the provider reports a total, pagination must
collect that many distinct offers before namespace deduplication. Incomplete or
changing totals fail before the snapshot is saved.

The collector uses the egdata Epic catalog API at `https://api.egdata.app`
([docs](https://docs.egdata.app)) because Epic's storefront GraphQL is an
undocumented, Cloudflare-protected web endpoint. Set `EPIC_CATALOG_BASE_URL` or
`--source-url` to switch providers later without changing the DB contract.

egdata notes that the client follows:

- The legacy `POST /search` route returns `410 Gone` (`DEPRECATED`) since
  November 2026; only search v2 works.
- egdata scores paginated catalog traversal and bursts of distinct
  `/offers/{id}` lookups. A suspicious client receives
  `{"error":{"code":"SESSION_CHALLENGE_REQUIRED"}}` with HTTP 200. The client
  then completes egdata's documented SHA-256 proof-of-work handshake at
  `POST /auth/challenge` and retries once with the `x-egdata-session` header.
- A high-risk client receives HTTP 204 (`X-Egdata-Mitigation: tarpit`). The
  client waits out egdata's 60-second behaviour window before retrying, and
  honours `Retry-After` on 429.
- Requests are spaced at least one second apart and send a stable
  `User-Agent`, as egdata asks of bulk consumers.

Email scraping, review UI, drafting, and sending are intentionally not wired to
the Steam workflow yet. They can be added as separate Epic modules against
`epic_tracker`.
