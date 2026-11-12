import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { restoreGames, restoreStatus, WORKFLOWS } from './agent-actions.js';

function fixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT);
    INSERT INTO scrape_tracker VALUES (1, 'Triaged')`);
  const sql = async (_env, stmts) => stmts.map(([query, ...args]) => db.prepare(query).all(...args).map(r => Object.values(r)));
  const dispatched = [];
  const fetcher = async (url, init) => { dispatched.push([url, JSON.parse(init.body)]); return new Response(null, { status: 204 }); };
  return { db, sql, dispatched, fetcher, env: { GH_REPO: 'o/r', GH_PAT: 't' } };
}

test('restore queues only deleted games, records the decision and starts restore.yml once', async () => {
  const { db, sql, dispatched, fetcher, env } = fixture();
  const out = await restoreGames(env, { appids: [1, 5, 6, 5], decision: 'pass' }, sql, fetcher);
  assert.deepEqual(out.queued, [5, 6]);
  assert.deepEqual(out.already_present, [{ appid: 1, mail_status: 'Triaged' }]);
  assert.deepEqual(db.prepare('SELECT appid, decision, status FROM restore_requests ORDER BY appid').all().map(r => Object.values(r)),
    [[5, 'pass', 'queued'], [6, 'pass', 'queued']]);
  assert.equal(WORKFLOWS.restore, 'restore.yml');
  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0][0], /actions\/workflows\/restore\.yml\/dispatches$/);
});

test('re-requesting a failed restore re-queues it; nothing to do starts no workflow', async () => {
  const { db, sql, dispatched, fetcher, env } = fixture();
  await restoreGames(env, { appids: [5] }, sql, fetcher);
  db.exec("UPDATE restore_requests SET status='failed', detail='steam down', finished_at='x'");
  await restoreGames(env, { appids: [5], decision: 'pending' }, sql, fetcher);
  assert.deepEqual(Object.values(db.prepare('SELECT status, detail, decision, finished_at FROM restore_requests').get()),
    ['queued', null, null, null]);
  const none = await restoreGames(env, { appids: [1] }, sql, fetcher);
  assert.deepEqual(none.queued, []);
  assert.equal(dispatched.length, 2);
});

test('restore validates its input', async () => {
  const { sql, fetcher, env } = fixture();
  await assert.rejects(restoreGames(env, { appids: [5], decision: 'flag' }, sql, fetcher), /decision must be/);
  await assert.rejects(restoreGames(env, { appids: Array.from({ length: 26 }, (_, i) => i + 2) }, sql, fetcher), /1-25/);
});

test('restore status reports outcome and where the game is now', async () => {
  const { db, sql, fetcher, env } = fixture();
  await restoreGames(env, { appids: [5, 6], decision: 'pass' }, sql, fetcher);
  db.exec(`UPDATE restore_requests SET status='done', detail='restored to Passed' WHERE appid=5;
    UPDATE restore_requests SET status='failed', detail='already released (2020)' WHERE appid=6;
    INSERT INTO scrape_tracker VALUES (5, 'Triaged')`);
  const out = await restoreStatus(env, { appids: [5, 6, 7] }, sql);
  assert.deepEqual(out.requests.map(r => [r.appid, r.status, r.detail, r.mail_status]),
    [[5, 'done', 'restored to Passed', 'Triaged'], [6, 'failed', 'already released (2020)', null]]);
  assert.deepEqual(out.not_requested, [7]);
});
