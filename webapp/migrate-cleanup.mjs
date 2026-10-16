import { readFile } from 'node:fs/promises';
import { sql } from './worker.js';
import { CLEANUP_SCHEMA } from './deletion.js';

const content = await readFile(new URL('../.env', import.meta.url), 'utf8');
const values = Object.fromEntries(content.split(/\r?\n/).filter(line => line && !line.startsWith('#') && line.includes('=')).map(line => {
  const pos = line.indexOf('='); return [line.slice(0, pos).trim(), line.slice(pos+1).trim().replace(/^['"]|['"]$/g, '')];
}));
const env = { TURSO_URL: values.TURSO_DATABASE_URL, TURSO_TOKEN: values.TURSO_AUTH_TOKEN };
if (!env.TURSO_URL || !env.TURSO_TOKEN) throw new Error('Missing database configuration');
await sql(env, [[CLEANUP_SCHEMA]]);
const result = await sql(env, [['SELECT count(*) FROM media_cleanup_jobs']]);
console.log('Cleanup schema ready. Pending jobs:', result[0][0][0]);
