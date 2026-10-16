import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { sql } from './worker.js';
import { CLEANUP_SCHEMA, CLEANUP_GUARDS, DISCOVERY_SCHEMA, assertCleanupReady } from './deletion.js';

// Rollout: stop old cleanup dispatch and drain in-flight invocations before
// migration/deployment. Old tokenless code cannot safely overlap token owners.
// Keep this additive column and the guards on rollback; never resume tokenless
// cleanup while token-aware cleanup can run. Do not shorten outstanding leases.
export async function migrateCleanup(env, run = sql) {
  await run(env, [[CLEANUP_SCHEMA], [DISCOVERY_SCHEMA]], true);
  const [columns] = await run(env, [["PRAGMA table_info(media_cleanup_jobs)"]]);
  if (!columns.some(row => row[1] === 'lease_token'))
    await run(env, [['ALTER TABLE media_cleanup_jobs ADD COLUMN lease_token TEXT']]);
  const [overlap] = await run(env, [["SELECT j.appid FROM media_cleanup_jobs j JOIN scrape_tracker s ON s.appid=j.appid WHERE s.Mail_status IN ('Pending','Writing','Drafted','Scheduled','Sending')"]]);
  if (overlap.length) throw new Error('Existing cleanup jobs overlap active games. Resolve those jobs before installing cleanup guards.');
  await run(env, CLEANUP_GUARDS.map(query => [query]), true);
  await assertCleanupReady(env, run);
  const [result] = await run(env, [['SELECT count(*) FROM media_cleanup_jobs']]);
  return result[0][0];
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const content = await readFile(new URL('../.env', import.meta.url), 'utf8');
  const values = Object.fromEntries(content.split(/\r?\n/).filter(line => line && !line.startsWith('#') && line.includes('=')).map(line => {
    const pos = line.indexOf('='); return [line.slice(0, pos).trim(), line.slice(pos+1).trim().replace(/^['"]|['"]$/g, '')];
  }));
  const env = { TURSO_URL: values.TURSO_DATABASE_URL, TURSO_TOKEN: values.TURSO_AUTH_TOKEN };
  if (!env.TURSO_URL || !env.TURSO_TOKEN) throw new Error('Missing database configuration');
  console.log('Cleanup schema ready. Pending jobs:', await migrateCleanup(env));
}
