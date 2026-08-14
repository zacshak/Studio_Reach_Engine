import assert from "node:assert/strict";
import test from "node:test";

import { approveStmt, NOMAIL_SQL, sql, updateJSON } from "./worker.js";

test("No-Mail includes only pending outreach with unresolved scrape states", () => {
  assert.equal(
    NOMAIL_SQL,
    "SELECT appid FROM scrape_tracker WHERE scrape_status IN ('pending','no_email','failed') AND Mail_status='Pending' ORDER BY appid",
  );
});

test("mail approval is one guarded database update", () => {
  assert.deepEqual(approveStmt(42), [
    "UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid=? AND Mail_status='Drafted' AND scrape_status IN ('seeded','scraped')",
    42,
  ]);
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
            step_results: [null, { rows: [[{ value: "1" }]] }, { rows: [] }, null, null],
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
