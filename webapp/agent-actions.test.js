import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { queues, keepMany, acceptMany, approveMany, returnToWriting, saveEmail, triageDetails, workflowRuns, batchIds } from './agent-actions.js';
import { migrateTriage } from './migrate-triage.mjs';

// appid, Mail_status, scrape_status, emails
const ROWS = [
  [1, 'Pending', 'seeded', 'a@studio.com'], [2, 'Pending', 'pending', null], [3, 'Pending', 'scraped', 'b@studio.com'],
  [4, 'Writing', 'seeded', 'c@studio.com'], [5, 'Drafted', 'seeded', 'd@studio.com'], [6, 'Scheduled', 'seeded', 'e@studio.com'],
  [7, 'Pending', 'no_email', null], [8, 'Sent', 'seeded', 'f@studio.com'],
];
async function fixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT, scrape_status TEXT, emails TEXT,
    website TEXT, triage_kept INTEGER NOT NULL DEFAULT 0)`);
  const add = db.prepare('INSERT INTO scrape_tracker(appid, Mail_status, scrape_status, emails) VALUES (?,?,?,?)');
  for (const row of ROWS) add.run(...row);
  const sql = async (_env, stmts) => stmts.map(([query, ...args]) => db.prepare(query).all(...args).map(r => Object.values(r)));
  await migrateTriage({}, {}, sql);
  return { db, sql };
}
const status = (db, id) => db.prepare('SELECT Mail_status s FROM scrape_tracker WHERE appid=?').get(id)?.s;

function media(objects) {
  return {
    objects,
    get: async key => objects[key] ? { etag: 'v1', json: async () => structuredClone(objects[key]) } : null,
    head: async key => objects[key] ? { size: 10 } : null,
    put: async (key, value) => { objects[key] = JSON.parse(value); return { etag: 'v2' }; },
  };
}
const getJSON = env => async (_e, key) => env.MEDIA.objects[key] ?? null;

test('queues split Pending rows by email and triage, and list later stages', async () => {
  const { db, sql } = await fixture();
  db.exec("UPDATE scrape_tracker SET triage_decision='flag' WHERE appid=3; UPDATE scrape_tracker SET triage_decision='pass' WHERE appid=1");
  const q = await queues({}, sql);
  assert.deepEqual(q.approval, [1]);
  assert.deepEqual(q.nomail, [2, 7]);
  assert.deepEqual(q.triage, [3]);
  assert.deepEqual(q.untriaged, [2, 7]);
  assert.deepEqual([q.writing, q.mail_approval, q.scheduled, q.sending], [[4], [5], [6], []]);
  assert.equal(q.counts.approval, 1);
});

test('keep marks only Pending rows and removes them from Triage', async () => {
  const { db, sql } = await fixture();
  db.exec("UPDATE scrape_tracker SET triage_decision='flag' WHERE appid=3");
  assert.deepEqual(await keepMany({}, { appids: [3, 4] }, sql), { kept: [3], not_pending: [4] });
  assert.deepEqual((await queues({}, sql)).approval, [1, 3]);
});

test('accept needs an email, sprite sheet and no flag', async () => {
  const { db, sql } = await fixture();
  db.exec("UPDATE scrape_tracker SET triage_decision='flag' WHERE appid=3");
  const env = { MEDIA: media({ 'index.json': { 1: 'G_1', 2: 'G_2', 3: 'G_3' },
    'G_1/manifest.json': { images: ['SpriteSheet_Screenshots.png'] }, 'G_1/SpriteSheet_Screenshots.png': {},
    'G_2/manifest.json': { images: ['SpriteSheet_Screenshots.png'] }, 'G_2/SpriteSheet_Screenshots.png': {},
    'G_3/manifest.json': { images: ['SpriteSheet_Screenshots.png'] }, 'G_3/SpriteSheet_Screenshots.png': {} }) };
  const result = await acceptMany(env, { appids: [1, 2, 3, 99] }, sql, getJSON(env));
  assert.deepEqual(result.accepted, [1]);
  assert.deepEqual(result.failed.map(f => f.appid), [2, 3, 99]);
  assert.match(result.failed.find(f => f.appid === 99).error, /screenshot/);
  assert.equal(status(db, 1), 'Writing'); assert.equal(status(db, 2), 'Pending'); assert.equal(status(db, 3), 'Pending');
});

test('approve moves only Drafted rows', async () => {
  const { db, sql } = await fixture();
  assert.deepEqual(await approveMany({}, { appids: [5, 4] }, sql), { approved: [5], not_drafted: [4] });
  assert.equal(status(db, 5), 'Scheduled');
});

test('return to writing clears the saved draft first and only for Drafted rows', async () => {
  const { db, sql } = await fixture();
  const env = { MEDIA: media({ 'index.json': { 5: 'G_5', 6: 'G_6' },
    'G_5/manifest.json': { name: 'G', mail: 'Subject: hi\nbody', draft_submission: { revision: 'x' }, images: [] } }) };
  const result = await returnToWriting(env, { appids: [5, 6] }, sql, getJSON(env));
  assert.deepEqual(result.returned, [5]);
  assert.match(result.failed[0].error, /only Drafted/);
  assert.deepEqual(env.MEDIA.objects['G_5/manifest.json'], { name: 'G', images: [] });
  assert.equal(status(db, 5), 'Writing'); assert.equal(status(db, 6), 'Scheduled');
});

test('save email follows the scraper contract', async () => {
  const { db, sql } = await fixture();
  assert.deepEqual(await saveEmail({}, { appid: 2, status: 'scraped', emails: ['jobs@studio.com', 'hi@studio.com'], website: 'https://studio.com' }, sql),
    { appid: 2, scrape_status: 'scraped', emails: 'jobs@studio.com, hi@studio.com' });
  assert.equal(db.prepare('SELECT website w FROM scrape_tracker WHERE appid=2').get().w, 'https://studio.com/');
  assert.deepEqual((await queues({}, sql)).approval, [1, 2, 3]);
  const failed = await saveEmail({}, { appid: 7, status: 'failed' }, sql);
  assert.equal(failed.scrape_status, 'failed'); assert.equal(failed.emails, null);
  await assert.rejects(saveEmail({}, { appid: 1, status: 'scraped', emails: ['x@y.com'] }, sql), /already has an email/);
  await assert.rejects(saveEmail({}, { appid: 7, status: 'scraped', emails: ['not-an-email'] }, sql), /invalid email/);
  await assert.rejects(saveEmail({}, { appid: 7, status: 'scraped' }, sql), /1-5 emails/);
  await assert.rejects(saveEmail({}, { appid: 7, status: 'no_email', emails: ['a@b.co'] }, sql), /only accepted/);
  await assert.rejects(saveEmail({}, { appid: 7, status: 'scraped', emails: ['a@b.co'], website: 'ftp://x' }, sql), /http/);
});

test('triage details include criteria text and report missing games', async () => {
  const { db, sql } = await fixture();
  db.exec("INSERT INTO triage_criteria(id, text) VALUES (1, 'Rubric'); UPDATE scrape_tracker SET triage_decision='pass', triage_reason='fits', triage_criteria_id=1, triaged_at='t' WHERE appid=1");
  const result = await triageDetails({}, { appids: [1, 2, 99] }, sql);
  assert.deepEqual(result.games[0], { appid: 1, mail_status: 'Pending', decision: 'pass', reason: 'fits', criteria: 'Rubric', triaged_at: 't' });
  assert.equal(result.games[1].decision, null);
  assert.deepEqual(result.missing, [99]);
});

test('workflow runs are read from GitHub and unknown workflows are refused', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async url => {
    assert.match(String(url), /repos\/o\/r\/actions\/workflows\/hermes\.yml\/runs\?per_page=2/);
    return Response.json({ workflow_runs: [{ id: 9, status: 'completed', conclusion: 'success', event: 'workflow_dispatch', head_branch: 'master', html_url: 'u' }] });
  };
  try {
    const result = await workflowRuns({ GH_REPO: 'o/r', GH_PAT: 'p' }, { workflow: 'hermes', limit: 2 });
    assert.equal(result.runs[0].conclusion, 'success');
    await assert.rejects(workflowRuns({ GH_REPO: 'o/r', GH_PAT: 'p' }, { workflow: 'deploy' }), /must be one of/);
  } finally { globalThis.fetch = original; }
});

test('batch IDs are bounded and deduplicated', () => {
  assert.deepEqual(batchIds({ appids: [1, '1', 2] }), [1, 2]);
  assert.throws(() => batchIds({ appids: [] }), /1-100/);
  assert.throws(() => batchIds({ appids: Array.from({ length: 101 }, (_, i) => i + 1) }), /1-100/);
  assert.throws(() => batchIds({ appids: [0] }), /invalid/);
});
