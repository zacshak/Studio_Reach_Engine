// Runs the real state SQL (migration, triage save, dashboard query, Keep) on SQLite.
import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { migrateStates } from './migrate-states.mjs';
import { saveTriage, triageSnapshot, TRIAGE_SQL } from './triage.js';
import { keepStmt } from './worker.js';
import { CLEANUP_SCHEMA, CLEANUP_GUARDS } from './deletion.js';

function sqlFor(db) {
  return async (_env, stmts, atomic = false) => {
    if (atomic) db.exec('BEGIN IMMEDIATE');
    try {
      const rows = stmts.map(([query, ...args]) => db.prepare(query).all(...args).map(row => Object.values(row)));
      if (atomic) db.exec('COMMIT');
      return rows;
    } catch (error) { if (atomic) db.exec('ROLLBACK'); throw error; }
  };
}

// A database as production had it before this migration: 'kept' allowed, triage_kept,
// Writing rows, the old cleanup guard, and the SELECT * view.
function legacyFixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT NOT NULL DEFAULT 'Pending',
      triage_kept INTEGER NOT NULL DEFAULT 0, triage_decision TEXT CHECK (triage_decision IN ('pass','flag','kept')),
      triage_reason TEXT, triage_criteria_id INTEGER, triaged_at TEXT);
    CREATE VIEW active_leads AS SELECT * FROM scrape_tracker WHERE Mail_status NOT IN ('Sent','Replied');
    ${CLEANUP_SCHEMA};
    CREATE TRIGGER cleanup_blocks_reactivation BEFORE UPDATE OF Mail_status ON scrape_tracker
      WHEN NEW.Mail_status IN ('Pending','Writing','Drafted','Scheduled','Sending')
      AND EXISTS (SELECT 1 FROM media_cleanup_jobs WHERE appid=NEW.appid)
      BEGIN SELECT RAISE(ABORT, 'media cleanup pending for this appid'); END;
    INSERT INTO scrape_tracker(appid, Mail_status, triage_kept, triage_decision, triage_reason) VALUES
      (1,'Pending',0,NULL,NULL), (2,'Pending',0,'pass','fits'), (3,'Pending',0,'flag','small'),
      (4,'Pending',1,'kept','tiny'), (5,'Pending',1,'flag','legacy keep'), (6,'Writing',0,NULL,NULL),
      (7,'Sent',0,NULL,NULL), (8,'Triaged',0,NULL,NULL), (9,'Invalid',0,NULL,NULL);`);
  return { db, sql: sqlFor(db) };
}

// A fresh database as the new Python schema creates it (no triage columns yet).
function freshFixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT NOT NULL DEFAULT 'Pending');
    INSERT INTO scrape_tracker(appid, Mail_status) VALUES
      (1,'Pending'),(2,'Pending'),(3,'Pending'),(4,'Accepted'),(5,'Pending'),(6,'Pending');`);
  return { db, sql: sqlFor(db) };
}
const row = (db, id) => ({ ...db.prepare('SELECT Mail_status s, triage_decision d, triage_reason r, triaged_at t FROM scrape_tracker WHERE appid=?').get(id) });
const index = { 1: 'G_1', 2: 'G_2', 3: 'G_3', 4: 'G_4', 5: 'G_5' };
const getJSON = async (_env, key) => key === 'index.json' ? index : null;

test('migration splits Pending, renames Writing, turns Keeps into pass and drops triage_kept', async () => {
  const { db, sql } = legacyFixture();
  const first = await migrateStates({}, sql);
  assert.deepEqual([first.keptToPass, first.toTriaged, first.backToPending, first.writingToAccepted, first.droppedTriageKept],
    [2, 4, 1, 1, false]);
  assert.deepEqual(row(db, 1), { s: 'Pending', d: null, r: null, t: null });
  assert.deepEqual([row(db, 2).s, row(db, 2).d], ['Triaged', 'pass']);
  assert.deepEqual([row(db, 3).s, row(db, 3).d], ['Triaged', 'flag']);
  assert.deepEqual([row(db, 4).s, row(db, 4).d, row(db, 4).r], ['Triaged', 'pass', 'Kept by user (AI flagged: tiny)']);
  assert.deepEqual([row(db, 5).s, row(db, 5).d, row(db, 5).r], ['Triaged', 'pass', 'Kept by user (AI flagged: legacy keep)']);
  assert.equal(row(db, 6).s, 'Accepted');
  assert.equal(row(db, 7).s, 'Sent');
  assert.equal(row(db, 8).s, 'Pending', 'a Triaged row without a decision is not triaged');
  assert.equal(row(db, 9).s, 'Invalid');
  // The cleanup guard now covers the new active states.
  const guard = db.prepare("SELECT sql FROM sqlite_master WHERE name='cleanup_blocks_reactivation'").get().sql;
  assert.match(guard, /'Triaged','Accepted'/);
  db.exec("INSERT INTO media_cleanup_jobs(appid, folder) VALUES (7, 'G_7')");
  assert.throws(() => db.exec("UPDATE scrape_tracker SET Mail_status='Accepted' WHERE appid=7"), /media cleanup pending/);
  // Re-running changes nothing.
  const second = await migrateStates({}, sql);
  assert.deepEqual([second.keptToPass, second.toTriaged, second.backToPending, second.writingToAccepted], [0, 0, 0, 0]);
  assert.equal(row(db, 4).r, 'Kept by user (AI flagged: tiny)');
  // The legacy column is dropped only on request, and the SELECT * view keeps working.
  const third = await migrateStates({}, sql, { dropLegacy: true });
  assert.equal(third.droppedTriageKept, true);
  assert.equal(db.prepare("SELECT count(*) n FROM pragma_table_info('scrape_tracker') WHERE name='triage_kept'").get().n, 0);
  assert.equal(db.prepare('SELECT count(*) n FROM active_leads').get().n, 8);
  assert.equal((await migrateStates({}, sql, { dropLegacy: true })).droppedTriageKept, false);
  db.close();
});

test('migration on a fresh database adds the triage columns with the pass/flag check', async () => {
  const { db, sql } = freshFixture();
  await migrateStates({}, sql);
  assert.equal(db.prepare('SELECT count(*) n FROM triage_criteria').get().n, 0);
  db.exec("UPDATE scrape_tracker SET triage_decision='pass' WHERE appid=1");
  assert.throws(() => db.exec("UPDATE scrape_tracker SET triage_decision='kept' WHERE appid=1"), /CHECK/);
  db.close();
});

test('triage save moves only Pending rows to Triaged and is idempotent on retry', async () => {
  const { db, sql } = freshFixture();
  await migrateStates({}, sql);
  const body = { criteria: 'Rubric', results: [
    { appid: 1, decision: 'flag', reason: 'too small' },
    { appid: 2, decision: 'pass', reason: 'fits' },
    { appid: 4, decision: 'pass', reason: 'already Accepted' },
    { appid: 6, decision: 'pass', reason: 'no media' },
  ] };
  const result = await saveTriage({}, body, sql, getJSON);
  assert.deepEqual(result.saved, [1, 2]);
  assert.deepEqual(result.skipped.map(s => s.appid).sort(), [4, 6]);
  assert.deepEqual([row(db, 1).s, row(db, 1).d], ['Triaged', 'flag']);
  assert.deepEqual([row(db, 2).s, row(db, 2).d], ['Triaged', 'pass']);
  assert.deepEqual([row(db, 4).s, row(db, 4).d], ['Accepted', null]);
  assert.deepEqual([row(db, 6).s, row(db, 6).d], ['Pending', null]);
  assert.match(row(db, 1).t, /^\d{4}-\d\d-\d\dT.*Z$/);
  // An identical retry (lost response) reports saved and keeps the timestamp.
  db.exec("UPDATE scrape_tracker SET triaged_at='2000-01-01T00:00:00.000Z' WHERE appid=1");
  const retry = await saveTriage({}, { criteria: 'Rubric', results: [body.results[0]] }, sql, getJSON);
  assert.deepEqual(retry.saved, [1]);
  assert.equal(row(db, 1).t, '2000-01-01T00:00:00.000Z');
  // A different decision for an already Triaged game is refused.
  const changed = await saveTriage({}, { criteria: 'Rubric', results: [{ appid: 1, decision: 'pass', reason: 'changed' }] }, sql, getJSON);
  assert.deepEqual(changed.saved, []);
  assert.deepEqual([row(db, 1).d, row(db, 1).r], ['flag', 'too small']);
  assert.equal(db.prepare('SELECT count(*) n FROM triage_criteria').get().n, 1);
  db.close();
});

test('dashboard query and Keep: Keep turns a flag into pass with the override recorded', async () => {
  const { db, sql } = freshFixture();
  await migrateStates({}, sql);
  await saveTriage({}, { criteria: 'Rubric', results: [
    { appid: 1, decision: 'flag', reason: 'too small' }, { appid: 2, decision: 'pass', reason: 'fits' }] }, sql, getJSON);
  let [rows] = await sql({}, [[TRIAGE_SQL]]);
  let snap = triageSnapshot(rows);
  assert.deepEqual(snap.triage, [1]);
  assert.deepEqual(Object.keys(snap.triageReviews), ['1', '2']);
  assert.equal(snap.triageReviews[1].criteria, 'Rubric');
  const [kept] = await sql({}, [keepStmt(1)]);
  assert.deepEqual(kept.map(r => Number(r[0])), [1]);
  assert.deepEqual([row(db, 1).s, row(db, 1).d, row(db, 1).r], ['Triaged', 'pass', 'Kept by user (AI flagged: too small)']);
  [rows] = await sql({}, [[TRIAGE_SQL]]);
  assert.deepEqual(triageSnapshot(rows).triage, []);
  // Keep only applies to flagged Triaged games.
  for (const id of [1, 2, 3]) assert.deepEqual((await sql({}, [keepStmt(id)]))[0], []);
  assert.deepEqual([row(db, 3).s, row(db, 3).d], ['Pending', null]);
  db.close();
});
