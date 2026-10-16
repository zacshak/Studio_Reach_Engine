import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { CLEANUP_SCHEMA, rejectRows, cleanupJobs } from './deletion.js';

function fixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT);
    CREATE TABLE newly_added(appid INTEGER PRIMARY KEY);
    CREATE TABLE mail_send_attempt(appid INTEGER PRIMARY KEY REFERENCES scrape_tracker(appid)); ${CLEANUP_SCHEMA}`);
  const sql = async (_env, stmts, atomic = false) => {
    if (atomic) db.exec('BEGIN IMMEDIATE');
    try {
      const rows = stmts.map(([query, ...args]) => db.prepare(query).all(...args).map(row => Object.values(row)));
      if (atomic) db.exec('COMMIT');
      return rows;
    } catch (error) { if (atomic) db.exec('ROLLBACK'); throw error; }
  };
  return { db, sql };
}

test('3000 rows delete atomically with persistent cleanup jobs and no media operations', async () => {
  const { db, sql } = fixture();
  const add = db.prepare("INSERT INTO scrape_tracker VALUES (?, 'Pending')");
  const cache = db.prepare('INSERT INTO newly_added VALUES (?)');
  const ids = Array.from({ length: 3000 }, (_, i) => i + 1);
  for (const id of ids) { add.run(id); cache.run(id); }
  const env = { MEDIA: { delete() { throw new Error('must not delete media synchronously'); } } };
  const index = Object.fromEntries(ids.map(id => [id, `Game_${id}`]));
  await rejectRows(env, ids, sql, async () => index);
  assert.equal(db.prepare('SELECT count(*) n FROM scrape_tracker').get().n, 0);
  assert.equal(db.prepare('SELECT count(*) n FROM newly_added').get().n, 0);
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 3000);
  await rejectRows(env, ids, sql, async () => index);
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 3000);
  db.close();
});

test('transaction failure rolls back both cleanup scheduling and row deletion; Sending is protected', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO scrape_tracker VALUES(1,'Pending'), (2,'Sending'); INSERT INTO newly_added VALUES(1),(2);");
  db.exec("CREATE TRIGGER fail_delete BEFORE DELETE ON scrape_tracker WHEN old.appid=1 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END;");
  await assert.rejects(rejectRows({}, [1], sql, async () => ({ 1: 'Game_1' })), /synthetic failure/);
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 0);
  assert.equal(db.prepare('SELECT count(*) n FROM newly_added').get().n, 2);
  db.exec('DROP TRIGGER fail_delete');
  await assert.rejects(rejectRows({}, [2], sql, async () => ({})), /Sending/);
  assert.equal(db.prepare('SELECT count(*) n FROM scrape_tracker').get().n, 2);
  db.close();
});

test('cleanup retries storage failures and removes all metadata only after successful purge', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1');");
  const objects = { 'index.json': { 1: 'Game_1' }, 'irrelevant.json': [1], 'mcp-triage.json': { 1: { decision: 'flag' } } };
  let fail = true, deletes = 0;
  const env = { MEDIA: {
    list: async () => ({ objects: [{ key: 'Game_1/img.png' }], truncated: false }),
    delete: async () => { if (fail) throw new Error('synthetic R2 outage'); deletes++; }
  } };
  const update = async (_env, key, _fallback, mutate) => { objects[key] = mutate(objects[key]); };
  await cleanupJobs(env, sql, update);
  assert.equal(db.prepare('SELECT attempts FROM media_cleanup_jobs').get().attempts, 1);
  assert.deepEqual(objects['index.json'], { 1: 'Game_1' });
  fail = false;
  db.exec("UPDATE media_cleanup_jobs SET available_at=datetime('now','-1 minute')");
  await cleanupJobs(env, sql, update);
  assert.equal(deletes, 1);
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 0);
  assert.deepEqual(objects, { 'index.json': {}, 'irrelevant.json': [], 'mcp-triage.json': {} });
  db.close();
});

test('cleanup cancels if the game exists again and retains jobs after metadata failure', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1'); INSERT INTO scrape_tracker VALUES(1,'Pending');");
  await cleanupJobs({ MEDIA: { list: () => { throw new Error('must preserve new game'); } } }, sql, () => {});
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 0);
  db.exec("DELETE FROM scrape_tracker; INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1');");
  await assert.rejects(cleanupJobs({ MEDIA: { list: async () => ({ objects: [], truncated: false }) } }, sql, async () => { throw new Error('metadata outage'); }), /metadata outage/);
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 1);
  db.close();
});

test('large folders retain their job between pages and active leases are not claimed twice', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1');");
  let pages = 0, metadataWrites = 0;
  const env = { MEDIA: { list: async () => ({ objects: [], truncated: ++pages === 1 }), delete: async () => {} } };
  await cleanupJobs(env, sql, async () => { metadataWrites++; });
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 1);
  assert.equal(metadataWrites, 0);
  db.exec("UPDATE media_cleanup_jobs SET lease_until=datetime('now','+1 minute')");
  await cleanupJobs(env, sql, async () => { metadataWrites++; });
  assert.equal(pages, 1);
  db.exec('UPDATE media_cleanup_jobs SET lease_until=NULL');
  await cleanupJobs(env, sql, async () => { metadataWrites++; });
  assert.equal(pages, 2);
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 0);
  db.close();
});
