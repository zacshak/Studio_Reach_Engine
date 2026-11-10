import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { sql } from './worker.js';
import { CLEANUP_GUARDS } from './deletion.js';

// Mail_status migration: Pending splits into Pending (not triaged) and Triaged (has a
// pass/flag decision), Writing is renamed Accepted, and the human Keep ('kept' /
// triage_kept) becomes a plain 'pass' whose reason records the override.
// Idempotent. Run order for a live rollout:
//   1. node webapp/migrate-states.mjs            (schema, guard trigger, row states)
//   2. deploy both Workers, merge the Python code to master
//   3. node webapp/migrate-states.mjs --drop-legacy   (drops triage_kept; only after
//      every running Python/GHA job uses the new code, which no longer re-adds it)
export const TRIAGE_COLUMNS = [
  ['triage_decision', "TEXT CHECK (triage_decision IN ('pass','flag'))"],
  ['triage_reason', 'TEXT'],
  ['triage_criteria_id', 'INTEGER'],
  ['triaged_at', 'TEXT'],
];
export const TRIAGE_CRITERIA_SCHEMA = `CREATE TABLE IF NOT EXISTS triage_criteria (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)`;
const KEPT_REASON = "'Kept by user (AI flagged: ' || coalesce(triage_reason,'') || ')'";
const REACTIVATION_GUARD = CLEANUP_GUARDS.find(query => query.includes('cleanup_blocks_reactivation'));

export async function migrateStates(env, run = sql, { dropLegacy = false } = {}) {
  const columns = async () => new Set((await run(env, [['PRAGMA table_info(scrape_tracker)']]))[0].map(row => row[1]));
  let have = await columns();
  if (!have.has('appid')) throw new Error('scrape_tracker not found');
  for (const [name, decl] of TRIAGE_COLUMNS)
    if (!have.has(name)) await run(env, [[`ALTER TABLE scrape_tracker ADD COLUMN ${name} ${decl}`]]);
  await run(env, [[TRIAGE_CRITERIA_SCHEMA]]);

  // The cleanup guard must protect the new active states before any row moves to them.
  const [cleanup] = await run(env, [["SELECT name FROM sqlite_master WHERE type='table' AND name='media_cleanup_jobs'"]]);
  if (cleanup.length)
    await run(env, [['DROP TRIGGER IF EXISTS cleanup_blocks_reactivation'], [REACTIVATION_GUARD]], true);

  const legacyKept = have.has('triage_kept') ? ' OR triage_kept=1' : '';
  const statements = [
    [`UPDATE scrape_tracker SET triage_decision='pass', triage_reason=${KEPT_REASON}
      WHERE (triage_decision='kept'${legacyKept}) AND coalesce(triage_decision,'')<>'pass'
      RETURNING appid`],
    ["UPDATE scrape_tracker SET Mail_status='Triaged' WHERE Mail_status='Pending' AND triage_decision IS NOT NULL RETURNING appid"],
    ["UPDATE scrape_tracker SET Mail_status='Pending' WHERE Mail_status='Triaged' AND triage_decision IS NULL RETURNING appid"],
    ["UPDATE scrape_tracker SET Mail_status='Accepted' WHERE Mail_status='Writing' RETURNING appid"],
  ];
  const [kept, triaged, untriaged, accepted] = await run(env, statements, true);

  let dropped = false;
  if (dropLegacy && have.has('triage_kept')) {
    await run(env, [['ALTER TABLE scrape_tracker DROP COLUMN triage_kept']]);
    dropped = !(await columns()).has('triage_kept');
  }
  const [counts] = await run(env, [['SELECT Mail_status, coalesce(triage_decision, \'\'), count(*) FROM scrape_tracker GROUP BY 1, 2 ORDER BY 1, 2']]);
  return { keptToPass: kept.length, toTriaged: triaged.length, backToPending: untriaged.length,
    writingToAccepted: accepted.length, droppedTriageKept: dropped,
    counts: counts.map(([status, decision, n]) => ({ status, decision: decision || null, n: Number(n) })) };
}

// Usage: node webapp/migrate-states.mjs [--drop-legacy]
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
  console.log(JSON.stringify(await migrateStates(env, sql, { dropLegacy: process.argv.includes('--drop-legacy') }), null, 1));
}
