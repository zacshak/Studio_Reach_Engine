import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { CLEANUP_SCHEMA, CLEANUP_GUARDS, DISCOVERY_SCHEMA, CLEANUP_LEASE_SECONDS, assertCleanupReady, rejectRows, cleanupJobs } from './deletion.js';
import { migrateCleanup } from './migrate-cleanup.mjs';

function fixture({ guards = true, legacy = false } = {}) {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT);
    CREATE TABLE newly_added(appid INTEGER PRIMARY KEY);
    CREATE TABLE mail_send_attempt(appid INTEGER PRIMARY KEY REFERENCES scrape_tracker(appid)); ${legacy ? CLEANUP_SCHEMA.replace('  lease_token TEXT,\n', '') : CLEANUP_SCHEMA}; ${DISCOVERY_SCHEMA}`);
  if (guards) for (const query of CLEANUP_GUARDS) db.exec(query);
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
  assert.deepEqual((await rejectRows({}, [2], sql, async () => ({}))).blocked, [2]);
  assert.equal(db.prepare('SELECT count(*) n FROM scrape_tracker').get().n, 2);
  db.close();
});

test('cleanup guard prevents resurrection until media and metadata removal completes', async () => {
  const { db, sql } = fixture();
  for (const query of CLEANUP_GUARDS) db.exec(query);
  db.exec("INSERT INTO scrape_tracker VALUES(1,'Pending'); INSERT INTO newly_added VALUES(1);");
  await rejectRows({}, [1], sql, async () => ({1:'Game_1'}));
  const insert = () => db.exec("INSERT INTO scrape_tracker VALUES(1,'Pending')");
  assert.throws(insert, /cleanup pending/);
  assert.throws(() => db.exec('INSERT INTO newly_added VALUES(1)'), /cleanup pending/);
  await cleanupJobs({MEDIA:{list:async () => {
    assert.throws(insert, /cleanup pending/);
    return {objects:[], truncated:false};
  }}}, sql, async () => { assert.throws(insert, /cleanup pending/); });
  insert();
  assert.equal(db.prepare('SELECT count(*) n FROM scrape_tracker').get().n, 1);
  db.close();
});

test('mixed rejection reports committed deletion separately from protected Sending', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO scrape_tracker VALUES(1,'Pending'),(2,'Sending'); INSERT INTO newly_added VALUES(1),(2);");
  db.exec("INSERT INTO pending_discovery VALUES(1,'One','2026-10-16'),(2,'Two','2026-10-16');");
  const result = await rejectRows({}, [1,2], sql, async () => ({1:'Game_1',2:'Game_2'}));
  assert.deepEqual(result.deleted, [1]);
  assert.deepEqual(result.blocked, [2]);
  assert.deepEqual(db.prepare('SELECT appid FROM scrape_tracker').all().map(r=>r.appid), [2]);
  assert.deepEqual(db.prepare('SELECT appid FROM pending_discovery').all().map(r=>r.appid), [2]);
  db.close();
});

test('inactive cleanup blocks reactivation until completion without deleting the history row', async () => {
  const {db, sql} = fixture();
  for (const query of CLEANUP_GUARDS) db.exec(query);
  db.exec("INSERT INTO scrape_tracker VALUES(1,'Sent'); INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1');");
  assert.throws(() => db.exec("UPDATE scrape_tracker SET Mail_status='Pending' WHERE appid=1"), /cleanup pending/);
  let deleted = false;
  await cleanupJobs({MEDIA:{list:async()=>({objects:[{key:'Game_1/a.png'}], truncated:false}), delete:async()=>{deleted=true;}}}, sql, async()=>{});
  assert.equal(deleted, true);
  assert.equal(db.prepare('SELECT Mail_status FROM scrape_tracker').get().Mail_status, 'Sent');
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 0);
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
  db.exec("INSERT INTO scrape_tracker VALUES(1,'Pending'); INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1');");
  await cleanupJobs({ MEDIA: { list: () => { throw new Error('must preserve new game'); } } }, sql, () => {});
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 0);
  db.exec("DELETE FROM scrape_tracker; INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1');");
  await assert.rejects(cleanupJobs({ MEDIA: { list: async () => ({ objects: [], truncated: false }) } }, sql, async () => { throw new Error('metadata outage'); }), /metadata outage/);
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 1);
  db.close();
});

test('cleanup fails closed with missing or replaced guards and without lease_token', async () => {
  for (const missing of ['all', 'cleanup_blocks_scrape_tracker_insert', 'cleanup_blocks_newly_added_insert', 'cleanup_blocks_reactivation', 'wrong-definition', 'lease_token']) {
    const { db, sql } = fixture({ guards: missing !== 'all', legacy: missing === 'lease_token' });
    if (missing.startsWith('cleanup_blocks_')) db.exec(`DROP TRIGGER ${missing}`);
    if (missing === 'wrong-definition') db.exec("DROP TRIGGER cleanup_blocks_reactivation; CREATE TRIGGER cleanup_blocks_reactivation BEFORE UPDATE ON scrape_tracker BEGIN SELECT 1; END;");
    db.exec("INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1');");
    let storageCalls = 0;
    const env = { MEDIA: { list: async () => { storageCalls++; return { objects: [], truncated: false }; }, delete: async () => { storageCalls++; } } };
    await assert.rejects(cleanupJobs(env, sql, async () => { storageCalls++; }), /migrate-cleanup/);
    assert.equal(storageCalls, 0, missing);
    assert.equal(db.prepare('SELECT lease_until FROM media_cleanup_jobs').get().lease_until, null);
    db.close();
  }
});

test('additive migration preserves existing jobs, leases and retries and is repeatable', async () => {
  const { db, sql } = fixture({ guards: false, legacy: true });
  db.exec("INSERT INTO scrape_tracker VALUES(1,'Sent'); INSERT INTO media_cleanup_jobs(appid,folder,attempts,last_error,lease_until) VALUES(1,'Game_1',3,'old failure',datetime('now','+10 minutes'));");
  const before = db.prepare('SELECT * FROM media_cleanup_jobs').get();
  assert.equal(await migrateCleanup({}, sql), 1);
  assert.equal(await migrateCleanup({}, sql), 1);
  const after = db.prepare('SELECT * FROM media_cleanup_jobs').get();
  assert.equal(after.lease_token, null);
  delete after.lease_token;
  assert.deepEqual(after, before);
  await assertCleanupReady({}, sql);
  assert.throws(() => db.exec("UPDATE scrape_tracker SET Mail_status='Pending'"), /cleanup pending/);
  db.close();
});

test('migration refuses active overlaps and resumes after operator resolution', async () => {
  const { db, sql } = fixture({ guards: false, legacy: true });
  db.exec("INSERT INTO scrape_tracker VALUES(1,'Pending'); INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Game_1');");
  await assert.rejects(migrateCleanup({}, sql), /overlap active/);
  await assert.rejects(assertCleanupReady({}, sql), /migrate-cleanup/);
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 1);
  db.exec('DELETE FROM media_cleanup_jobs');
  assert.equal(await migrateCleanup({}, sql), 0);
  db.close();
});

test('each job claim has a unique token and a lease longer than scheduled execution', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'One_1'),(2,'Two_2');");
  let inspected = false;
  await cleanupJobs({ MEDIA: { list: async () => {
    if (!inspected) {
      const jobs = db.prepare("SELECT lease_token, unixepoch(lease_until)-unixepoch('now') ttl FROM media_cleanup_jobs").all();
      assert.equal(new Set(jobs.map(job => job.lease_token)).size, 2);
      for (const job of jobs) {
        assert.match(job.lease_token, /^[a-f0-9]{32}$/);
        assert.ok(job.ttl > 15 * 60 && job.ttl <= CLEANUP_LEASE_SECONDS);
      }
      inspected = true;
    }
    return { objects: [], truncated: false };
  } } }, sql, async () => {});
  assert.equal(inspected, true);
  db.close();
});

// Force expiry instead of waiting 30 minutes. Worker B finishes A's old job,
// then a newly inserted game is rejected into a different job for the same ID.
async function replaceExpiredJob(db, sql) {
  db.exec("UPDATE media_cleanup_jobs SET lease_until=datetime('now','-1 second') WHERE appid=1");
  await cleanupJobs({ MEDIA: { list: async () => ({ objects: [], truncated: false }) } }, sql, async () => {});
  assert.equal(db.prepare('SELECT count(*) n FROM media_cleanup_jobs').get().n, 0);
  db.exec("INSERT INTO scrape_tracker VALUES(1,'Pending'); INSERT INTO newly_added VALUES(1);");
  await rejectRows({}, [1], sql, async () => ({ 1: 'New_1' }));
  // Give the replacement a different live owner as well.
  db.exec("UPDATE media_cleanup_jobs SET lease_token='replacement-owner', lease_until=datetime('now','+30 minutes') WHERE appid=1");
  return db.prepare('SELECT * FROM media_cleanup_jobs').get();
}

for (const phase of ['list', 'completion', 'retry', 'release', 'index.json', 'irrelevant.json', 'mcp-triage.json']) {
  test(`expired worker cannot alter a replacement job or later R2 state after ${phase}`, async () => {
    const { db, sql } = fixture();
    db.exec("INSERT INTO media_cleanup_jobs(appid,folder) VALUES(1,'Old_1');");
    let replaced = false, replacement, deleted = 0;
    const metadata = [];
    const replace = async () => {
      replaced = true;
      replacement = await replaceExpiredJob(db, sql);
    };
    const interceptedSQL = async (env, statements, atomic) => {
      const query = statements[0][0];
      if (!replaced && ((phase === 'completion' && query.startsWith('DELETE FROM media_cleanup_jobs')) ||
        (phase === 'retry' && query.includes('SET attempts=attempts+1')) ||
        (phase === 'release' && query.includes('SET lease_until=NULL')))) await replace();
      return sql(env, statements, atomic);
    };
    await cleanupJobs({ MEDIA: {
      list: async () => {
        if (phase === 'list') await replace();
        if (phase === 'retry') throw new Error('synthetic listing failure');
        return { objects: [{ key: 'Old_1/a.png' }], truncated: phase === 'release' };
      },
      delete: async () => { assert.equal(replaced, false, 'stale worker must not delete R2'); deleted++; }
    } }, interceptedSQL, async (_env, key) => {
      assert.equal(replaced, false, 'stale worker must not start another metadata mutation');
      metadata.push(key);
      if (phase === key) await replace();
    });
    assert.equal(replaced, true);
    assert.deepEqual(db.prepare('SELECT * FROM media_cleanup_jobs').get(), replacement);
    if (phase === 'list' || phase === 'retry') { assert.equal(deleted, 0); assert.deepEqual(metadata, []); }
    if (phase === 'release') assert.deepEqual(metadata, []);
    if (phase === 'index.json') assert.deepEqual(metadata, ['index.json']);
    if (phase === 'irrelevant.json') assert.deepEqual(metadata, ['index.json', 'irrelevant.json']);
    db.close();
  });
}

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
