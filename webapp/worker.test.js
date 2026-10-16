import assert from "node:assert/strict";
import test from "node:test";

import {
  approveStmt, keepStmt, NOMAIL_SQL, requireSocialsStmt,
  SOCIALS_SQL, TRIAGE_KEPT_SQL, sql, updateJSON,
} from "./worker.js";
import worker from "./worker.js";

test('oversized deletion batches fail before touching storage', async () => {
  const response = await worker.fetch(new Request('https://example/api/act', { method: 'POST', body: JSON.stringify({ action: 'reject_all_irrelevant', appids: Array.from({length:10001}, (_,i)=>i+1) }) }), {});
  assert.equal(response.status, 500);
  assert.match((await response.json()).error, /at most 10000/);
});

test('confirmed rejection schedules cleanup without synchronous media deletion', async () => {
  const original = globalThis.fetch;
  const objects = { 'index.json': { 1: 'One_1', 2: 'Two_2' }, 'irrelevant.json': [1,2], 'mcp-triage.json': { 1: { decision: 'flag' }, 2: { decision: 'flag' } } };
  let deletedRows = false;
  globalThis.fetch = async (_url, options) => {
    const steps = JSON.parse(options.body).requests[0].batch.steps;
    assert.match(steps[1].stmt.sql, /INSERT INTO media_cleanup_jobs/);
    assert.match(steps[3].stmt.sql, /DELETE FROM newly_added/);
    assert.match(steps[4].stmt.sql, /DELETE FROM scrape_tracker/);
    deletedRows = true;
    return Response.json({ results: [{ type: 'ok', response: { result: { step_results: [{}, {}, {}, {}, {}, {rows:[]}, {}, null], step_errors: [] } } }] });
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
    assert.deepEqual(objects['irrelevant.json'], [1,2]);
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

test('dashboard state routes MCP flags and passes and excludes stale human decisions', async () => {
  const originalFetch = globalThis.fetch;
  const sets = [[[1], [2]], [], [[3, 0]], [], [[0]], [[0]], [[1], [2], [3]]];
  globalThis.fetch = async () => Response.json({ results: [...sets.map(rows => ({ type: 'ok', response: { result: {
    rows: rows.map(row => row.map(value => ({ type: 'integer', value: String(value) })))
  } } })), { type: 'ok' }] });
  const objects = {
    'index.json': { 1: 'One_1', 2: 'Two_2', 3: 'Three_3' },
    'irrelevant.json': [1, 4],
    'mcp-triage.json': { 1: { decision: 'pass', reason: 'Fits', criteria: 'Custom' }, 2: { decision: 'flag', reason: 'Does not fit', criteria: 'Custom' }, 4: { decision: 'flag' } }
  };
  try {
    const response = await worker.fetch(new Request('https://review.test/api/state'), {
      TURSO_URL: 'libsql://example', TURSO_TOKEN: 'test',
      MEDIA: { get: async key => ({ json: async () => objects[key] }) }
    });
    assert.equal(response.status, 200);
    const state = await response.json();
    assert.deepEqual(state.triage, [2]);
    assert.deepEqual(state.approval, [1]);
    assert.deepEqual(state.nomail, [3]);
    assert.equal(state.triageReviews[1].reason, 'Fits');
    assert.equal(state.triageReviews[4], undefined);
  } finally { globalThis.fetch = originalFetch; }
});

test("No-Mail includes only pending outreach with unresolved scrape states", () => {
  assert.equal(
    NOMAIL_SQL,
    "SELECT appid, Require_Socials FROM scrape_tracker WHERE scrape_status IN ('pending','no_email','failed') AND Mail_status='Pending' ORDER BY appid",
  );
});

test("ReqSocial stores an explicit boolean without changing queue state", () => {
  assert.deepEqual(requireSocialsStmt(42, true), [
    "UPDATE scrape_tracker SET Require_Socials=? WHERE appid=?", 1, 42,
  ]);
  assert.deepEqual(requireSocialsStmt(42, false), [
    "UPDATE scrape_tracker SET Require_Socials=? WHERE appid=?", 0, 42,
  ]);
});

test("Socials shows completed requests that still await send or rejection", () => {
  assert.equal(
    SOCIALS_SQL,
    "SELECT appid, Socials_Data, Require_Socials FROM scrape_tracker WHERE Require_Socials=1 AND Socials_Data<>'{}' ORDER BY appid",
  );
});

test("mail approval is one guarded database update", () => {
  assert.deepEqual(approveStmt(42), [
    "UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid=? AND Mail_status='Drafted' AND scrape_status IN ('seeded','scraped')",
    42,
  ]);
});

test("triage Keep persists without advancing the mail state", () => {
  assert.deepEqual(keepStmt(42), [
    "UPDATE scrape_tracker SET triage_kept=1 WHERE appid=? AND Mail_status='Pending'",
    42,
  ]);
  assert.equal(TRIAGE_KEPT_SQL, "SELECT appid FROM scrape_tracker WHERE triage_kept=1");
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
