# SRE database MCP

Independent, authenticated Streamable HTTP MCP Worker at `/mcp`. The dashboard and sending workflows do not depend on it. Only the Steam database is in scope.

Three tools: `list_tables`, `describe_table`, and `query`. Queries accept SQLite SELECT/WITH SQL, positional `?` parameters, `page_size` (default 100, maximum 2000 per response), and `offset`. Results contain column names once, row arrays, `has_more`, and `next_offset`. There is no cumulative row limit. Joins, sorting, date predicates, batch IDs, JSON functions, and aggregates work through SQL.

Repeat the same query with `next_offset` until `has_more` is false. Always specify a deterministic ORDER BY. These are live reads, not a frozen snapshot. For records changing between requests, prefer key-based iteration:

```sql
SELECT appid, game_name, Mail_status
FROM scrape_tracker
WHERE appid > ?
ORDER BY appid
```

Pass the last returned appid on the next call and reset offset to zero. Updates and deletions can still occur during a full read. No promise of a transaction spanning multiple calls is made.

Response budgets are about 1 MiB, upstream responses are bounded at 8 MiB, requests at 64 KiB, and database requests at 25 seconds. Oversized responses return an actionable error or a continuation offset, never silently truncated field values. Reduce page size or retrieve large fields using SQLite `substr()` chunks. Expensive queries may need narrower filters. Integer values outside JavaScript's safe range are returned as strings.

## Security

Use a separate Turso token with `--read-only` and `data_read` permissions only on `scrape_tracker`, `newly_added`, and `known_comingsoon` (the actual production spelling). This credential is the authorization boundary, including for joins, subqueries, and SQL functions. Never reuse the application's write token. The deployment script verifies that writes and access to a non-approved table are denied before deploying.

The Worker requires `Authorization: Bearer <MCP_AUTH_TOKEN>`. Clients must support custom bearer headers. This is not an OAuth login server. Browser cross-origin access is rejected. Credentials and row contents are not logged. Authenticated clients can read personal/contact data in all three tables. Do not share the bearer token publicly. Successful deployment generates a gitignored `mcp-client.json` connection file containing the URL and bearer header.

## Setup and deployment

```powershell
npm.cmd ci
npm.cmd test
.\node_modules\.bin\wrangler.cmd deploy --dry-run
python deploy.py --env-file A:/Studio_Reach_Engine/.env --database sre
```

Turso CLI login in WSL and Wrangler login are prerequisites. Deployment creates a dedicated `sre-db-mcp` Worker. It does not alter the database schema or rows. Credential verification includes an UPDATE with `WHERE 0`, which changes no rows even if a wrongly scoped token is supplied. `credentials.json` is generated locally and ignored by Git. Keep it private. Configure the MCP client with the deployed `/mcp` URL and its `MCP_AUTH_TOKEN` as the bearer header. Rotating the MCP token invalidates existing client connections on their next request.

The compatibility date is October 14 UTC, accepted by Cloudflare during creation on October 15 IST.
