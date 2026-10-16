export const CLEANUP_SCHEMA = `CREATE TABLE IF NOT EXISTS media_cleanup_jobs (
  appid INTEGER PRIMARY KEY,
  folder TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  lease_until TEXT,
  lease_token TEXT,
  last_error TEXT
)`;
export const DISCOVERY_SCHEMA = `CREATE TABLE IF NOT EXISTS pending_discovery (
  appid INTEGER PRIMARY KEY, name TEXT, discovered_on TEXT NOT NULL
)`;

// A pending job owns this appid until both objects and metadata are removed.
// Database triggers also protect inserts from older Python/Worker versions.
export const CLEANUP_GUARDS = [...['scrape_tracker', 'newly_added'].map(table =>
  `CREATE TRIGGER IF NOT EXISTS cleanup_blocks_${table}_insert BEFORE INSERT ON ${table}
   WHEN EXISTS (SELECT 1 FROM media_cleanup_jobs WHERE appid=NEW.appid)
   BEGIN SELECT RAISE(ABORT, 'media cleanup pending for this appid'); END`),
  `CREATE TRIGGER IF NOT EXISTS cleanup_blocks_reactivation BEFORE UPDATE OF Mail_status ON scrape_tracker
   WHEN NEW.Mail_status IN ('Pending','Writing','Drafted','Scheduled','Sending')
   AND EXISTS (SELECT 1 FROM media_cleanup_jobs WHERE appid=NEW.appid)
   BEGIN SELECT RAISE(ABORT, 'media cleanup pending for this appid'); END`];

const normalizeGuard = query => query.replace(/\bIF NOT EXISTS\b/gi, '').replace(/\s+/g, ' ').trim().replace(/;$/, '').toLowerCase();
export async function assertCleanupReady(env, sql) {
  const [guards, columns] = await sql(env, [
    ["SELECT sql FROM sqlite_master WHERE type='trigger'"],
    ["SELECT name FROM pragma_table_info('media_cleanup_jobs') WHERE name='lease_token'"]
  ]);
  const installed = new Set(guards.map(([query]) => normalizeGuard(query || '')));
  if (!columns.length || CLEANUP_GUARDS.some(query => !installed.has(normalizeGuard(query))))
    throw new Error('Cleanup disabled: run webapp/migrate-cleanup.mjs to install lease_token and cleanup guards.');
}

// Only called by scheduled (15 minute maximum) or post-response waitUntil (30s).
// R2 has no DB fencing token: keep the lease beyond the entire invocation lifetime.
// Do not reuse this cleanup loop in an unbounded HTTP/streaming handler.
// https://developers.cloudflare.com/workers/platform/limits/#wall-time-limits-by-invocation-type
export const CLEANUP_LEASE_SECONDS = 30 * 60;
const OWNED = 'appid=? AND lease_token=? AND lease_until>CURRENT_TIMESTAMP';

// The cleanup job and deletion commit together. Retrying a lost response is safe.
export function deletionStatements(appids, index) {
  const payload = JSON.stringify(appids.map(appid => ({ appid, folder: index[String(appid)] || '' })));
  const requested = "SELECT CAST(json_extract(value,'$.appid') AS INTEGER) FROM json_each(?)";
  const permitted = `${requested} EXCEPT SELECT appid FROM scrape_tracker WHERE Mail_status='Sending'`;
  return [
    [`INSERT INTO media_cleanup_jobs(appid, folder)
      SELECT s.appid, json_extract(j.value,'$.folder') FROM json_each(?) j
      JOIN scrape_tracker s ON s.appid=CAST(json_extract(j.value,'$.appid') AS INTEGER)
      WHERE s.Mail_status<>'Sending' ON CONFLICT(appid) DO NOTHING`, payload],
    [`DELETE FROM mail_send_attempt WHERE appid IN (${permitted})`, payload],
    [`DELETE FROM newly_added WHERE appid IN (${permitted})`, payload],
    [`DELETE FROM scrape_tracker WHERE appid IN (${permitted})`, payload],
    [`DELETE FROM pending_discovery WHERE appid IN (${permitted})`, payload],
    [`SELECT appid FROM scrape_tracker WHERE appid IN (${requested})`, payload]
  ];
}

export async function rejectRows(env, appids, sql, getJSON) {
  if (!appids.length) return { ok: true };
  const index = await getJSON(env, 'index.json') || {};
  const results = await sql(env, deletionStatements(appids, index), true);
  const remaining = results.at(-1).map(r => Number(r[0]));
  const blocked = new Set(remaining);
  return { ok: true, deleted: appids.filter(id => !blocked.has(id)),
    blocked: remaining, cleanup: 'queued' };
}

export async function cleanupJobs(env, sql, updateJSON) {
  await assertCleanupReady(env, sql);
  const [jobs] = await sql(env, [[`UPDATE media_cleanup_jobs
    SET lease_until=datetime('now',?), lease_token=lower(hex(randomblob(16)))
    WHERE appid IN (SELECT appid FROM media_cleanup_jobs
      WHERE available_at<=CURRENT_TIMESTAMP AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)
      ORDER BY created_at, appid LIMIT 5)
    RETURNING appid, folder, attempts, lease_token`, `+${CLEANUP_LEASE_SECONDS} seconds`]]);
  if (!jobs.length) return;
  let completed = 0, cancelled = 0;
  for (const [rawId, folder, attempts, token] of jobs) {
    const appid = Number(rawId);
    const ownedArgs = [appid, token];
    const lostLease = new Error('Cleanup lease no longer owned');
    const requireOwnership = async () => {
      const [owned] = await sql(env, [[`SELECT appid FROM media_cleanup_jobs WHERE ${OWNED}`, ...ownedArgs]]);
      if (!owned.length) throw lostLease;
    };
    const finish = async () => {
      const [removed] = await sql(env, [[`DELETE FROM media_cleanup_jobs WHERE ${OWNED} RETURNING appid`, ...ownedArgs]]);
      return removed.length;
    };
    let metadataStarted = false;
    try {
      await requireOwnership();
      const [existing] = await sql(env, [["SELECT appid FROM scrape_tracker WHERE appid=? AND Mail_status IN ('Pending','Writing','Drafted','Scheduled','Sending')", appid]]);
      if (existing.length) { cancelled += await finish(); continue; }
      if (folder) {
        await requireOwnership();
        const page = await env.MEDIA.list({ prefix: folder + '/', limit: 200 });
        await requireOwnership();
        if (page.objects.length) await env.MEDIA.delete(page.objects.map(o => o.key));
        if (page.truncated) {
          await sql(env, [[`UPDATE media_cleanup_jobs SET lease_until=NULL, lease_token=NULL WHERE ${OWNED}`, ...ownedArgs]]);
          continue;
        }
      }
      metadataStarted = true;
      await requireOwnership();
      await updateJSON(env, 'index.json', {}, current => {
        const next = { ...current };
        if (next[String(appid)] === folder) delete next[String(appid)];
        return next;
      });
      await requireOwnership();
      await updateJSON(env, 'irrelevant.json', [], current => current.filter(id => Number(id) !== appid));
      await requireOwnership();
      await updateJSON(env, 'mcp-triage.json', {}, current => Object.fromEntries(Object.entries(current).filter(([id]) => Number(id) !== appid)));
      completed += await finish();
    } catch (error) {
      if (error === lostLease) continue;
      const delay = Math.min(3600, 30 * 2 ** Math.min(Number(attempts), 7));
      await sql(env, [[`UPDATE media_cleanup_jobs SET attempts=attempts+1, last_error=?, available_at=datetime('now',?), lease_until=NULL, lease_token=NULL WHERE ${OWNED}`,
        String(error.message).slice(0, 500), `+${delay} seconds`, ...ownedArgs]]);
      console.error(JSON.stringify({ event: 'media_cleanup_retry', appid, message: String(error.message).slice(0, 200) }));
      if (metadataStarted) throw error;
    }
  }
  console.log(JSON.stringify({ event: 'media_cleanup', completed, cancelled }));
}
