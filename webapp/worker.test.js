import assert from "node:assert/strict";
import test from "node:test";

import {
  approveStmt, VIEW_SQL, homeCounts, sql, updateJSON,
} from "./worker.js";
import worker from "./worker.js";

test('social save HTTP route reaches guarded completion',async()=>{
  const original=globalThis.fetch;
  try {
    globalThis.fetch=async(_url,options)=>{
      assert.match(JSON.parse(options.body).requests[0].stmt.sql,/Socials_Data=\?,Require_Socials=0/);
      return Response.json({results:[{type:'ok',response:{result:{rows:[[{type:'integer',value:'1'}]]}}},{type:'ok'}]});
    };
    const socials={X:null,LinkedIn:null,Instagram:null,Discord:null,Website:'https://example.invalid'};
    const r=await worker.fetch(new Request('https://review.test/api/socials/save',{method:'POST',body:JSON.stringify({appid:1,request_id:'a'.repeat(32),socials})}),{TURSO_URL:'https://db.invalid'});
    assert.equal(r.status,200);assert.deepEqual((await r.json()).socials,socials);
  }finally{globalThis.fetch=original;}
});

test('accept never advances a lead with an incomplete or absent sprite', async () => {
  const original = globalThis.fetch;
  // Only the state read is allowed; any UPDATE would mean the lead advanced.
  globalThis.fetch = async (_url, options) => {
    const query = JSON.parse(options.body).requests[0].stmt.sql;
    assert.match(query, /^SELECT Mail_status, triage_decision, scrape_status/);
    return Response.json({ results: [{ type: 'ok', response: { result: { rows: [[
      { type: 'text', value: 'Triaged' }, { type: 'text', value: 'pass' }, { type: 'text', value: 'seeded' }]] } } }, { type: 'ok' }] });
  };
  try {
    for (const [images, message] of [[[], /screenshot export is incomplete/], [['SpriteSheet_Screenshots.png'], /sprite sheet is missing/]]) {
      const response = await worker.fetch(new Request('https://example/api/act', {method:'POST',body:JSON.stringify({action:'accept',appid:1})}), {
        TURSO_URL: 'libsql://example', TURSO_TOKEN: 'test',
        MEDIA: { get: async key => ({json: async () => key==='index.json' ? {1:'Game_1'} : {images}}), head:async()=>null }
      });
      assert.equal(response.status,409);
      assert.match((await response.json()).error, message);
    }
  } finally { globalThis.fetch=original; }
});

test('oversized deletion batches fail before touching storage', async () => {
  const response = await worker.fetch(new Request('https://example/api/act', { method: 'POST', body: JSON.stringify({ action: 'reject_all_irrelevant', appids: Array.from({length:10001}, (_,i)=>i+1) }) }), {});
  assert.equal(response.status, 500);
  assert.match((await response.json()).error, /at most 10000/);
});

test('confirmed rejection schedules cleanup without synchronous media deletion', async () => {
  const original = globalThis.fetch;
  const objects = { 'index.json': { 1: 'One_1', 2: 'Two_2' } };
  let deletedRows = false;
  globalThis.fetch = async (_url, options) => {
    const steps = JSON.parse(options.body).requests[0].batch.steps;
    assert.match(steps[1].stmt.sql, /INSERT INTO media_cleanup_jobs/);
    assert.match(steps[3].stmt.sql, /DELETE FROM newly_added/);
    assert.match(steps[4].stmt.sql, /DELETE FROM scrape_tracker/);
    deletedRows = true;
    return Response.json({ results: [{ type: 'ok', response: { result: { step_results: [{}, {}, {}, {}, {}, {}, {rows:[]}, {}, null], step_errors: [] } } }] });
  };
  try {
    const response = await worker.fetch(new Request('https://example/api/act', { method: 'POST', body: JSON.stringify({ action: 'reject_all_irrelevant', appids: [1] }) }), {
      TURSO_URL: 'libsql://example', TURSO_TOKEN: 'test', MEDIA: {
        get: async key => ({ etag: 'v1', json: async () => objects[key] }),
        list: async () => ({ objects: [{ key: 'One_1/img.png' }], truncated: false }),
        delete: async () => { throw new Error('must not delete media synchronously'); },
        put: async (key, value) => { objects[key] = JSON.parse(value); return { etag: 'v2' }; }
      }
    });
    assert.equal(response.status, 200); assert.equal(deletedRows, true);
    assert.deepEqual(objects['index.json'], { 1: 'One_1', 2: 'Two_2' });
  } finally { globalThis.fetch = original; }
});

test('transaction begin and commit errors are never reported as success', async () => {
  const original = globalThis.fetch;
  try {
    for (const failedStep of [0,3]) {
      globalThis.fetch = async () => {
        const errors = [null,null,null,null,null]; errors[failedStep] = { message: 'transaction failed' };
        return Response.json({ results: [{ type: 'ok', response: { result: { step_errors: errors, step_results: [{},{},{},{},null] } } }] });
      };
      await assert.rejects(sql({ TURSO_URL: 'libsql://example' }, [['DELETE FROM a'], ['DELETE FROM b']], true), /transaction failed/);
    }
  } finally { globalThis.fetch = original; }
});

test('dashboard state returns Home counts and the Pending, Passed, Flagged and Mail views', async () => {
  const originalFetch = globalThis.fetch;
  // pending, passed, flagged, noMail, drafted, counts, reviews
  const sets = [[[4]], [[1], [3]], [[2]], [[3], [4]], [[6, 'a@b.co']],
    [['Pending', '', 0, 1], ['Triaged', 'pass', 1, 1], ['Triaged', 'pass', 0, 1], ['Triaged', 'flag', 1, 1],
     ['Accepted', '', 1, 2], ['Drafted', '', 1, 1], ['Sent', '', 1, 7], ['Invalid', '', 0, 1]],
    [[1, 'pass', 'Fits', 'Custom', '2026-11-09T00:00:00.000Z'], [2, 'flag', 'Does not fit', 'Custom', null]]];
  const cell = value => value === null ? { type: 'null' } : typeof value === 'number'
    ? { type: 'integer', value: String(value) } : { type: 'text', value };
  let statements;
  globalThis.fetch = async (_url, options) => {
    statements = JSON.parse(options.body).requests.filter(r => r.stmt).map(r => r.stmt.sql);
    return Response.json({ results: [...sets.map(rows => ({ type: 'ok', response: { result: {
      rows: rows.map(row => row.map(cell)) } } })), { type: 'ok' }] });
  };
  const objects = { 'index.json': { 1: 'One_1', 2: 'Two_2', 3: 'Three_3' } };
  const reads = [];
  try {
    const response = await worker.fetch(new Request('https://review.test/api/state'), {
      TURSO_URL: 'libsql://example', TURSO_TOKEN: 'test',
      MEDIA: { get: async key => { reads.push(key); return { json: async () => objects[key] }; } }
    });
    assert.equal(response.status, 200);
    const state = await response.json();
    assert.deepEqual(reads, ['index.json']);
    assert.deepEqual(statements.slice(0, 4), [VIEW_SQL.pending, VIEW_SQL.passed, VIEW_SQL.flagged, VIEW_SQL.noMail]);
    assert.deepEqual([state.pending, state.passed, state.flagged, state.noMail], [[4], [1, 3], [2], [3, 4]]);
    assert.deepEqual(state.mail, [{ appid: 6, emails: 'a@b.co' }]);
    assert.deepEqual(state.counts, { pending: 1, triaged: 3, passed: 2, flagged: 1, no_mail: 2, accepted: 2, drafted: 1,
      scheduled: 0, sending: 0, sent: 7, replied: 0, invalid: 1 });
    assert.equal(state.pendingDrafts, true);
    assert.equal(state.scheduled, false);
    assert.deepEqual(state.triageReviews[1], { decision: 'pass', reason: 'Fits', criteria: 'Custom', reviewed_at: '2026-11-09T00:00:00.000Z' });
    for (const gone of ['triage', 'approval', 'nomail', 'untriaged', 'socials', 'requireSocials']) assert.equal(gone in state, false, gone);
  } finally { globalThis.fetch = originalFetch; }
});

test('view queries split Pending, Passed and Flagged, and badge not-yet-accepted games without email', () => {
  assert.equal(VIEW_SQL.pending, "SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' ORDER BY appid");
  assert.equal(VIEW_SQL.passed, "SELECT appid FROM scrape_tracker WHERE Mail_status='Triaged' AND triage_decision='pass' ORDER BY appid");
  assert.equal(VIEW_SQL.flagged, "SELECT appid FROM scrape_tracker WHERE Mail_status='Triaged' AND triage_decision='flag' ORDER BY appid");
  assert.equal(VIEW_SQL.noMail, "SELECT appid FROM scrape_tracker WHERE Mail_status IN ('Pending','Triaged') AND NOT (scrape_status IN ('seeded','scraped')) ORDER BY appid");
  assert.deepEqual(homeCounts([['Sending', '', 1, 2], ['Replied', '', 1, 3], ['Scheduled', '', 1, 4]]),
    { pending: 0, triaged: 0, passed: 0, flagged: 0, no_mail: 0, accepted: 0, drafted: 0, scheduled: 4, sending: 2, sent: 0, replied: 3, invalid: 0 });
});

test('dashboard actions refuse with 409 and a reason instead of silently succeeding', async () => {
  const original = globalThis.fetch;
  const media = { get: async key => key === 'index.json' ? { json: async () => ({ 7: 'G_7' }) }
      : { json: async () => ({ images: ['SpriteSheet_Screenshots.png'] }) }, head: async () => ({ size: 10 }) };
  const cases = [
    ['accept', [['Triaged', 'pass', 'no_email']], /Cannot accept 7: has no email\./],
    ['accept', [['Triaged', 'flag', 'seeded']], /Cannot accept 7: flagged in triage; Pass it first\./],
    ['accept', [['Pending', null, 'seeded']], /Cannot accept 7: not triaged yet\./],
    ['accept', [], /Cannot accept 7: game no longer exists\./],
    ['triage_pass', [], /Cannot pass 7: it is no longer Pending\./],
    ['pass', [], /Cannot pass 7: it is not a flagged Triaged game\./],
    ['flag', [], /Cannot flag 7: it is not a passed Triaged game\./],
  ];
  try {
    for (const [action, rows, message] of cases) {
      globalThis.fetch = async () => Response.json({ results: [{ type: 'ok', response: { result: {
        rows: rows.map(row => row.map(v => v === null ? { type: 'null' } : { type: 'text', value: v })) } } }, { type: 'ok' }] });
      const r = await worker.fetch(new Request('https://test/api/act', { method: 'POST', body: JSON.stringify({ action, appid: 7 }) }),
        { TURSO_URL: 'libsql://example', TURSO_TOKEN: 'test', MEDIA: media });
      assert.equal(r.status, 409, action);
      const body = await r.json();
      assert.equal(body.refused, true);
      assert.match(body.error, message);
    }
  } finally { globalThis.fetch = original; }
});

test('obsolete dashboard socials actions are rejected without external requests',async()=>{
  for(const body of [{action:'require_socials',appid:1,required:true},{action:'trigger',workflow:'socials'}]){
    const r=await worker.fetch(new Request('https://test/api/act',{method:'POST',body:JSON.stringify(body)}),{});
    assert.equal(r.status,500);
  }
});

test('Send click queues socials before dispatch, but Draft click never does',async()=>{
  const original=globalThis.fetch;
  try{
    for(const workflow of ['send','draft']){
      const calls=[];
      globalThis.fetch=async(url,options)=>{
        calls.push(String(url));
        if(String(url).includes('api.github.com'))return new Response(null,{status:204});
        const statement=JSON.parse(options.body).requests[0].stmt.sql;
        assert.match(statement,/Socials_Request_ID IS NULL/);
        assert.match(statement,/Mail_status='Scheduled'/);
        return Response.json({results:[{type:'ok',response:{result:{rows:[]}}},{type:'ok'}]});
      };
      const r=await worker.fetch(new Request('https://test/api/act',{method:'POST',body:JSON.stringify({action:'trigger',workflow})}),{TURSO_URL:'https://db.invalid',GH_REPO:'example/repo',GH_PAT:'synthetic'});
      assert.equal(r.status,200);
      assert.equal(calls.length,workflow==='send'?2:1);
      assert.ok(calls.at(-1).includes('api.github.com'));
    }
  }finally{globalThis.fetch=original;}
});

test("mail approval is one guarded database update", () => {
  assert.deepEqual(approveStmt(42), [
    "UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid=? AND Mail_status='Drafted' AND scrape_status IN ('seeded','scraped')",
    42,
  ]);
});

test('triage save route writes eligible rows in one transaction and reports skips', async () => {
  const original = globalThis.fetch;
  let steps;
  globalThis.fetch = async (_url, options) => {
    steps = JSON.parse(options.body).requests[0].batch.steps;
    // BEGIN, criteria insert, update 1 (saved), update 2 (now ineligible), COMMIT, ROLLBACK
    return Response.json({ results: [{ type: 'ok', response: { result: {
      step_results: [{}, { rows: [] }, { rows: [[{ type: 'integer', value: '1' }]] }, { rows: [] }, {}, null], step_errors: [] } } }] });
  };
  try {
    const body = { criteria: ' Rubric ', results: [
      { appid: 1, decision: 'flag', reason: 'Too small' },
      { appid: 2, decision: 'pass', reason: 'Fits' },
      { appid: 3, decision: 'pass', reason: 'No media' }] };
    const r = await worker.fetch(new Request('https://review.internal/api/triage/save', { method: 'POST', body: JSON.stringify(body) }), {
      TURSO_URL: 'libsql://example', TURSO_TOKEN: 'test',
      MEDIA: { get: async key => key === 'index.json' ? { json: async () => ({ 1: 'One_1', 2: 'Two_2' }) } : null }
    });
    assert.equal(r.status, 200);
    const result = await r.json();
    assert.deepEqual(result.saved, [1]);
    assert.deepEqual(result.skipped.map(s => s.appid), [3, 2]);
    assert.equal(steps.length, 6);
    assert.match(steps[1].stmt.sql, /INSERT INTO triage_criteria/);
    assert.equal(steps[1].stmt.args[0].value, 'Rubric');
    assert.match(steps[2].stmt.sql, /SET Mail_status='Triaged'/);
    assert.match(steps[2].stmt.sql, /Mail_status='Pending' OR \(Mail_status='Triaged' AND triage_decision IS \?/);
    assert.deepEqual(steps[2].stmt.args.map(a => a.value), ['flag', 'Too small', 'Rubric', '1', 'flag', 'Too small', 'Rubric']);
  } finally { globalThis.fetch = original; }
});

test('triage save rejects malformed batches before touching the database', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error('database must not be called'); };
  try {
    const bad = [
      { criteria: '', results: [{ appid: 1, decision: 'flag', reason: 'x' }] },
      { criteria: 'c', results: [] },
      { criteria: 'c', results: [{ appid: 1, decision: 'kept', reason: 'x' }] },
      { criteria: 'c', results: [{ appid: 1, decision: 'flag', reason: 'x' }, { appid: 1, decision: 'pass', reason: 'y' }] },
      { criteria: 'c', results: [{ appid: -1, decision: 'flag', reason: 'x' }] },
    ];
    for (const body of bad) {
      const r = await worker.fetch(new Request('https://review.internal/api/triage/save', { method: 'POST', body: JSON.stringify(body) }), { MEDIA: { get: async () => null } });
      assert.equal(r.status, 500);
    }
  } finally { globalThis.fetch = original; }
});

test("sql uses a guarded transaction for multi-statement mutations", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_url, options) => {
    const body = JSON.parse(options.body);
    assert.equal(body.requests[0].type, "batch");
    const steps = body.requests[0].batch.steps;
    assert.equal(steps[0].stmt.sql, "BEGIN IMMEDIATE");
    assert.equal(steps.at(-2).stmt.sql, "COMMIT");
    assert.equal(steps.at(-1).stmt.sql, "ROLLBACK");
    return new Response(JSON.stringify({
      results: [{
        type: "ok",
        response: {
          result: {
            step_results: [{ rows: [] }, { rows: [[{ value: "1" }]] }, { rows: [] }, { rows: [] }, null],
            step_errors: [null, null, null, null, null],
          },
        },
      }, { type: "ok" }],
    }));
  };
  try {
    const rows = await sql(
      { TURSO_URL: "libsql://example", TURSO_TOKEN: "token" },
      [["DELETE FROM a WHERE id=?", 1], ["DELETE FROM b WHERE id=?", 1]],
      true,
    );
    assert.deepEqual(rows, [[['1']], []]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("updateJSON retries a failed conditional write", async () => {
  let puts = 0;
  const media = {
    get: async () => ({ etag: `v${puts}`, json: async () => ({ keep: true }) }),
    put: async (_key, value) => {
      puts += 1;
      if (puts === 1) return null;
      assert.deepEqual(JSON.parse(value), { keep: true, added: true });
      return { etag: "done" };
    },
  };
  await updateJSON({ MEDIA: media }, "index.json", {}, (value) => ({ ...value, added: true }));
  assert.equal(puts, 2);
});

test("media is available without authentication", async () => {
  const env = {
    MEDIA: { get: async () => ({ body: "ok", httpMetadata: { contentType: "text/plain" } }) },
  };
  const response = await worker.fetch(new Request("https://example/media/a"), env);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "ok");
});

test("public API does not require or set an authentication cookie", async () => {
  const response = await worker.fetch(new Request("https://example/api/not-found"), {});
  assert.equal(response.status, 404);
  assert.equal(response.headers.get("set-cookie"), null);
});
