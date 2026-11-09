// Runs the real triage SQL (migration, save, dashboard query, Keep) on SQLite.
import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { migrateTriage } from './migrate-triage.mjs';
import { saveTriage, triageSnapshot, TRIAGE_SQL } from './triage.js';
import { keepStmt } from './worker.js';

function fixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT NOT NULL DEFAULT 'Pending',
    triage_kept INTEGER NOT NULL DEFAULT 0);
    INSERT INTO scrape_tracker(appid, Mail_status, triage_kept) VALUES
      (1,'Pending',0),(2,'Pending',0),(3,'Pending',1),(4,'Writing',0),(5,'Pending',0),(6,'Pending',0);`);
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
const row = (db, id) => db.prepare('SELECT triage_decision d, triage_reason r, triage_criteria_id c, triaged_at t FROM scrape_tracker WHERE appid=?').get(id);
const index = { 1: 'G_1', 2: 'G_2', 3: 'G_3', 4: 'G_4', 5: 'G_5' };
const getJSON = async (_env, key) => key === 'index.json' ? index : null;

test('migration adds columns, backfills Keeps and imports only eligible JSON recommendations', async () => {
  const { db, sql } = fixture();
  const legacy = {
    1: { decision: 'pass', reason: 'fits', criteria: 'R1', reviewed_at: '2026-11-08T00:00:00.000Z' },
    2: { decision: 'flag', reason: 'small', criteria: 'R1' },
    3: { decision: 'flag', reason: 'kept by human', criteria: 'R1' },
    4: { decision: 'pass', reason: 'advanced', criteria: 'R2' },
    9: { decision: 'pass', reason: 'deleted game', criteria: 'R2' },
    5: { decision: 'bogus', reason: 'ignored' },
  };
  const first = await migrateTriage({}, legacy, sql);
  assert.equal(first.keptBackfilled, 1);
  assert.equal(first.imported, 2);
  assert.deepEqual(first.pending, { pass: 1, flag: 1, kept: 1 });
  assert.equal(row(db, 1).t, '2026-11-08T00:00:00.000Z');
  assert.equal(row(db, 3).d, 'kept');
  assert.equal(row(db, 4).d, null);
  assert.equal(db.prepare('SELECT count(*) n FROM triage_criteria').get().n, 2);
  // Re-running is a no-op and never overwrites a stored decision.
  db.exec("UPDATE scrape_tracker SET triage_decision='flag', triage_reason='newer' WHERE appid=1");
  const second = await migrateTriage({}, legacy, sql);
  assert.equal(second.imported, 0); assert.equal(second.keptBackfilled, 0);
  assert.equal(row(db, 1).r, 'newer');
  assert.throws(() => db.exec("UPDATE scrape_tracker SET triage_decision='maybe' WHERE appid=1"), /CHECK/);
  db.close();
});

test('save writes eligible rows, skips protected ones and preserves timestamps on identical resubmission', async () => {
  const { db, sql } = fixture();
  await migrateTriage({}, {}, sql);
  const body = { criteria: 'Rubric', results: [
    { appid: 1, decision: 'flag', reason: 'too small' },
    { appid: 2, decision: 'pass', reason: 'fits' },
    { appid: 3, decision: 'flag', reason: 'human kept' },
    { appid: 4, decision: 'pass', reason: 'already Writing' },
    { appid: 6, decision: 'pass', reason: 'no media' },
  ] };
  const result = await saveTriage({}, body, sql, getJSON);
  assert.deepEqual(result.saved, [1, 2]);
  assert.deepEqual(result.skipped.map(s => s.appid).sort(), [3, 4, 6]);
  assert.equal(row(db, 3).d, 'kept');
  assert.equal(row(db, 4).d, null);
  assert.match(row(db, 1).t, /^\d{4}-\d\d-\d\dT.*Z$/);
  const stamp = row(db, 1).t;
  db.exec("UPDATE scrape_tracker SET triaged_at='2000-01-01T00:00:00.000Z' WHERE appid=1");
  await saveTriage({}, body, sql, getJSON);
  assert.equal(row(db, 1).t, '2000-01-01T00:00:00.000Z', 'identical resubmission keeps its timestamp');
  await saveTriage({}, { criteria: 'Rubric', results: [{ appid: 1, decision: 'pass', reason: 'changed' }] }, sql, getJSON);
  assert.notEqual(row(db, 1).t, '2000-01-01T00:00:00.000Z');
  assert.ok(stamp);
  assert.equal(db.prepare('SELECT count(*) n FROM triage_criteria').get().n, 1);
  db.close();
});

test('dashboard query and Keep: flags show, Keep removes the row from Triage permanently', async () => {
  const { db, sql } = fixture();
  await migrateTriage({}, {}, sql);
  await saveTriage({}, { criteria: 'Rubric', results: [
    { appid: 1, decision: 'flag', reason: 'too small' }, { appid: 2, decision: 'pass', reason: 'fits' }] }, sql, getJSON);
  let [rows] = await sql({}, [[TRIAGE_SQL]]);
  let snap = triageSnapshot(rows);
  assert.deepEqual(snap.triage, [1]);
  assert.deepEqual(Object.keys(snap.triageReviews), ['1', '2']);
  assert.equal(snap.triageReviews[1].criteria, 'Rubric');
  await sql({}, [keepStmt(1)]);
  assert.deepEqual({ ...db.prepare('SELECT triage_decision d, triage_kept k FROM scrape_tracker WHERE appid=1').get() }, { d: 'kept', k: 1 });
  [rows] = await sql({}, [[TRIAGE_SQL]]);
  snap = triageSnapshot(rows);
  assert.deepEqual(snap.triage, []);
  const again = await saveTriage({}, { criteria: 'Rubric', results: [{ appid: 1, decision: 'flag', reason: 'retry' }] }, sql, getJSON);
  assert.deepEqual(again.saved, []);
  assert.equal(row(db, 1).d, 'kept');
  db.close();
});
