import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { sql } from './worker.js';

// Moves triage recommendations from R2 mcp-triage.json into scrape_tracker.
// Additive and idempotent: safe to run before deploying the new Workers and again
// afterwards to pick up anything the old MCP wrote in between. It never overwrites a
// decision already stored in the database and never touches non-Pending rows.
// Rollback: leave the columns and table in place; older Workers ignore them.
export const TRIAGE_COLUMNS = [
  ['triage_decision', "TEXT CHECK (triage_decision IN ('pass','flag','kept'))"],
  ['triage_reason', 'TEXT'],
  ['triage_criteria_id', 'INTEGER'],
  ['triaged_at', 'TEXT'],
];
export const TRIAGE_CRITERIA_SCHEMA = `CREATE TABLE IF NOT EXISTS triage_criteria (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)`;

export async function migrateTriage(env, recommendations = {}, run = sql) {
  const [columns] = await run(env, [['PRAGMA table_info(scrape_tracker)']]);
  const have = new Set(columns.map(row => row[1]));
  if (!have.has('appid')) throw new Error('scrape_tracker not found');
  for (const [name, decl] of TRIAGE_COLUMNS)
    if (!have.has(name)) await run(env, [[`ALTER TABLE scrape_tracker ADD COLUMN ${name} ${decl}`]]);
  await run(env, [[TRIAGE_CRITERIA_SCHEMA]]);
  // Existing human Keeps become the authoritative 'kept' decision.
  const [kept] = await run(env, [["UPDATE scrape_tracker SET triage_decision='kept' WHERE triage_kept=1 AND coalesce(triage_decision,'')<>'kept' RETURNING appid"]]);

  const entries = Object.entries(recommendations || {}).filter(([id, r]) =>
    Number.isSafeInteger(Number(id)) && ['pass', 'flag'].includes(r?.decision));
  let imported = 0;
  for (let i = 0; i < entries.length; i += 100) {
    const statements = [];
    for (const [id, r] of entries.slice(i, i + 100)) {
      const criteria = String(r.criteria || '').trim() || '(criteria not recorded)';
      statements.push(['INSERT INTO triage_criteria(text) VALUES (?) ON CONFLICT(text) DO NOTHING', criteria]);
      statements.push([`UPDATE scrape_tracker SET triage_decision=?, triage_reason=?,
          triage_criteria_id=(SELECT id FROM triage_criteria WHERE text=?), triaged_at=?
        WHERE appid=? AND Mail_status='Pending' AND triage_kept=0 AND triage_decision IS NULL
        RETURNING appid`,
        r.decision, String(r.reason || ''), criteria, String(r.reviewed_at || new Date().toISOString()), Number(id)]);
    }
    const result = await run(env, statements, true);
    imported += result.filter((rows, k) => k % 2 === 1 && rows.length).length;
  }
  const [[counts]] = await run(env, [["SELECT sum(triage_decision='pass'), sum(triage_decision='flag'), sum(triage_decision='kept') FROM scrape_tracker WHERE Mail_status='Pending'"]]);
  return { keptBackfilled: kept.length, recommendations: entries.length, imported,
    pending: { pass: Number(counts[0] || 0), flag: Number(counts[1] || 0), kept: Number(counts[2] || 0) } };
}

// Usage: node webapp/migrate-triage.mjs [path/to/mcp-triage.json]
// Without a path it downloads the live file from the dashboard's public media route.
// Database settings come from ../.env (TURSO_DATABASE_URL, TURSO_AUTH_TOKEN) or the
// process environment.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const envFile = new URL('../.env', import.meta.url);
  const values = existsSync(envFile) ? Object.fromEntries((await readFile(envFile, 'utf8')).split(/\r?\n/)
    .filter(line => line && !line.startsWith('#') && line.includes('=')).map(line => {
      const pos = line.indexOf('='); return [line.slice(0, pos).trim(), line.slice(pos + 1).trim().replace(/^['"]|['"]$/g, '')];
    })) : {};
  const env = { TURSO_URL: values.TURSO_DATABASE_URL || process.env.TURSO_DATABASE_URL,
    TURSO_TOKEN: values.TURSO_AUTH_TOKEN || process.env.TURSO_AUTH_TOKEN };
  if (!env.TURSO_URL || !env.TURSO_TOKEN) throw new Error('Missing database configuration');
  const source = process.argv[2];
  let recommendations;
  if (source) recommendations = JSON.parse(await readFile(source, 'utf8'));
  else {
    const response = await fetch('https://sre-review.zacgamedev.com/media/mcp-triage.json');
    if (!response.ok && response.status !== 404) throw new Error(`mcp-triage.json HTTP ${response.status}`);
    recommendations = response.status === 404 ? {} : await response.json();
  }
  console.log(JSON.stringify(await migrateTriage(env, recommendations)));
}
