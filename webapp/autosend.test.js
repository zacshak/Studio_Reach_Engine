import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { autoSend, AUTO_SEND_SQL } from './autosend.js';
import worker from './worker.js';

const ON_TEN = Date.parse('2026-11-14T12:10:00Z');

function fixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT, Require_Socials INTEGER DEFAULT 0, Socials_Request_ID TEXT);
    CREATE TABLE sender_accounts(email TEXT PRIMARY KEY, daily_cap INTEGER, last_status TEXT);
    CREATE TABLE mail_send_log(appid INTEGER, sender TEXT, sent_at TEXT);
    CREATE TABLE mail_send_attempt(appid INTEGER PRIMARY KEY, sender TEXT);
    INSERT INTO sender_accounts VALUES ('a@x', 2, 'ok'), ('paused@x', 0, 'paused (daily cap 0)'), ('broken@x', 5, 'login failed');`);
  const sql = async (_env, stmts) => stmts.map(([q, ...args]) => db.prepare(q).all(...args).map(r => Object.values(r)));
  return { db, sql };
}

function github({ last = null } = {}) {
  const calls = [];
  const fetcher = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET', body: opts.body });
    if (url.endsWith('/runs?per_page=1')) return Response.json({ workflow_runs: last ? [last] : [] });
    if (url.endsWith('/dispatches')) return new Response(null, { status: 204 });
    return new Response('no', { status: 404 });
  };
  return { calls, fetcher };
}

const env = { GH_REPO: 'o/r', GH_PAT: 't' };
const dispatched = calls => calls.filter(c => c.method === 'POST').map(c => c.url);

test('starts send.yml when approved mails wait and an account has room', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO scrape_tracker(appid, Mail_status) VALUES (1,'Scheduled'),(2,'Scheduled')");
  db.exec("INSERT INTO mail_send_log VALUES (9,'a@x',datetime('now','-2 hours'))"); // 1 of 2 used
  const { calls, fetcher } = github({ last: { status: 'completed', created_at: '2026-11-14T10:00:00Z' } });
  assert.equal(await autoSend(env, sql, { now: ON_TEN, fetcher }), 'started');
  assert.deepEqual(dispatched(calls), ['https://api.github.com/repos/o/r/actions/workflows/send.yml/dispatches']);
  // same social-request queueing as the Send button
  assert.equal(db.prepare("SELECT count(*) n FROM scrape_tracker WHERE Require_Socials=1 AND Socials_Request_ID IS NOT NULL").get().n, 2);
});

test('does nothing while every usable account is at its rolling 24h cap', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO scrape_tracker(appid, Mail_status) VALUES (1,'Scheduled'),(3,'Sending')");
  db.exec("INSERT INTO mail_send_log VALUES (9,'a@x',datetime('now','-23 hours'))");
  db.exec("INSERT INTO mail_send_attempt VALUES (3,'a@x')"); // being sent right now counts too
  const { calls, fetcher } = github();
  assert.equal(await autoSend(env, sql, { now: ON_TEN, fetcher }), 'all accounts at their 24h cap');
  assert.equal(calls.length, 0);
  // once the oldest send is more than 24h old, the cap has room again
  db.exec("UPDATE mail_send_log SET sent_at=datetime('now','-25 hours')");
  assert.equal(await autoSend(env, sql, { now: ON_TEN, fetcher }), 'started');
});

test('broken or paused accounts do not count as room', async () => {
  const { db, sql } = fixture();
  db.exec("INSERT INTO scrape_tracker(appid, Mail_status) VALUES (1,'Scheduled')");
  db.exec("UPDATE sender_accounts SET daily_cap=0 WHERE email='a@x'");
  assert.deepEqual((await sql(env, [[AUTO_SEND_SQL]]))[0][0].map(Number), [1, 0]);
});

test('skips when nothing is approved, a run is active, or the last run is recent', async () => {
  const { db, sql } = fixture();
  let g = github();
  assert.equal(await autoSend(env, sql, { now: ON_TEN, fetcher: g.fetcher }), 'nothing approved');
  db.exec("INSERT INTO scrape_tracker(appid, Mail_status) VALUES (1,'Scheduled')");
  for (const [last, why] of [
    [{ status: 'in_progress', created_at: '2026-11-14T08:00:00Z' }, 'send run already active'],
    [{ status: 'queued', created_at: '2026-11-14T12:09:00Z' }, 'send run already active'],
    [{ status: 'completed', created_at: '2026-11-14T11:50:00Z' }, 'last run too recent']]) {
    g = github({ last });
    assert.equal(await autoSend(env, sql, { now: ON_TEN, fetcher: g.fetcher }), why);
    assert.equal(dispatched(g.calls).length, 0);
  }
});

test('checks only every 10 minutes and can be switched off', async () => {
  const { sql } = fixture();
  const { calls, fetcher } = github();
  assert.equal(await autoSend(env, sql, { now: ON_TEN + 60000, fetcher }), 'not this minute');
  assert.equal(await autoSend({ ...env, AUTO_SEND: 'off' }, sql, { now: ON_TEN, fetcher }), 'disabled');
  assert.equal(calls.length, 0);
});

test('the worker cron runs the automatic send check', async () => {
  const waits = [];
  const original = globalThis.fetch;
  globalThis.fetch = async () => Response.json({ results: [] });
  try {
    await worker.scheduled({ scheduledTime: ON_TEN + 60000 }, { TURSO_URL: 'libsql://x', TURSO_TOKEN: 't' },
      { waitUntil: p => waits.push(p.catch(() => {})) });
    assert.equal(waits.length, 2);
    await Promise.all(waits);
  } finally { globalThis.fetch = original; }
});
