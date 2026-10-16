export const CLEANUP_SCHEMA = `CREATE TABLE IF NOT EXISTS media_cleanup_jobs (
  appid INTEGER PRIMARY KEY,
  folder TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  lease_until TEXT,
  last_error TEXT
)`;

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
    [`SELECT appid FROM scrape_tracker WHERE appid IN (${requested})`, payload]
  ];
}

export async function rejectRows(env, appids, sql, getJSON) {
  if (!appids.length) return { ok: true };
  const index = await getJSON(env, 'index.json') || {};
  const results = await sql(env, deletionStatements(appids, index), true);
  const remaining = results.at(-1).map(r => Number(r[0]));
  if (remaining.length) throw new Error(`These games are currently Sending and were not deleted: ${remaining.join(', ')}`);
  return { ok: true, cleanup: 'queued' };
}

export async function cleanupJobs(env, sql, updateJSON) {
  const [jobs] = await sql(env, [[`UPDATE media_cleanup_jobs SET lease_until=datetime('now','+10 minutes')
    WHERE appid IN (SELECT appid FROM media_cleanup_jobs
      WHERE available_at<=CURRENT_TIMESTAMP AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)
      ORDER BY created_at, appid LIMIT 5)
    RETURNING appid, folder, attempts`]]);
  if (!jobs.length) return;
  const done = [], cancelled = [];
  for (const [rawId, folder, attempts] of jobs) {
    const appid = Number(rawId);
    try {
      const [existing] = await sql(env, [['SELECT appid FROM scrape_tracker WHERE appid=?', appid]]);
      if (existing.length) { cancelled.push(appid); continue; }
      if (folder) {
        const page = await env.MEDIA.list({ prefix: folder + '/', limit: 200 });
        if (page.objects.length) await env.MEDIA.delete(page.objects.map(o => o.key));
        if (page.truncated) {
          await sql(env, [['UPDATE media_cleanup_jobs SET lease_until=NULL WHERE appid=?', appid]]);
          continue;
        }
      }
      done.push([appid, folder]);
    } catch (error) {
      const delay = Math.min(3600, 30 * 2 ** Math.min(Number(attempts), 7));
      await sql(env, [["UPDATE media_cleanup_jobs SET attempts=attempts+1, last_error=?, available_at=datetime('now',?), lease_until=NULL WHERE appid=?",
        String(error.message).slice(0, 500), `+${delay} seconds`, appid]]);
      console.error(JSON.stringify({ event: 'media_cleanup_retry', appid, message: String(error.message).slice(0, 200) }));
    }
  }
  if (done.length) {
    const ids = new Set(done.map(([id]) => id));
    await updateJSON(env, 'index.json', {}, current => {
      const next = { ...current };
      for (const [id, folder] of done) if (next[String(id)] === folder) delete next[String(id)];
      return next;
    });
    await updateJSON(env, 'irrelevant.json', [], current => current.filter(id => !ids.has(Number(id))));
    await updateJSON(env, 'mcp-triage.json', {}, current => Object.fromEntries(Object.entries(current).filter(([id]) => !ids.has(Number(id)))));
  }
  const finished = [...cancelled, ...done.map(([id]) => id)];
  if (finished.length) await sql(env, [['DELETE FROM media_cleanup_jobs WHERE appid IN (SELECT value FROM json_each(?))', JSON.stringify(finished)]]);
  console.log(JSON.stringify({ event: 'media_cleanup', completed: done.length, cancelled: cancelled.length }));
}
