import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import worker, { selectSQL, query, boundedJSON } from './worker.js';

test('SQL gate rejects statements and permits quoted punctuation', () => {
  for (const sql of ['DELETE FROM scrape_tracker', 'PRAGMA query_only=0', 'SELECT 1; DELETE FROM scrape_tracker', 'WITH x AS (SELECT 1) SELECT * FROM x;']) {
    assert.throws(() => selectSQL(sql));
  }
  assert.equal(selectSQL("SELECT ';' AS value -- hi\n"), "SELECT ';' AS value -- hi\n");
});

test('reads every synthetic row across pages and supports filters, joins, batch IDs and dates', async () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, added_at TEXT, Mail_status TEXT); CREATE TABLE newly_added(appid INTEGER PRIMARY KEY)');
  const insert = db.prepare('INSERT INTO scrape_tracker VALUES (?, ?, ?)');
  for (let n = 1; n <= 253; n++) insert.run(n, '2026-10-15', n % 2 ? 'Pending' : 'Sent');
  db.exec('INSERT INTO newly_added VALUES (2), (4)');
  const run = async (_, sql, params) => {
    const stmt = db.prepare(sql);
    const records = stmt.all(...params);
    const columns = records.length ? Object.keys(records[0]) : [];
    return { columns, rows: records.map(r => columns.map(c => r[c])) };
  };
  const all = [];
  let offset = 0;
  do {
    const page = await query({}, { sql: 'SELECT appid FROM scrape_tracker ORDER BY appid', page_size: 17, offset }, run);
    all.push(...page.rows.flat()); offset = page.next_offset;
  } while (offset !== null);
  assert.deepEqual(all, Array.from({ length: 253 }, (_, i) => i + 1));
  const join = await query({}, { sql: 'SELECT s.appid FROM scrape_tracker s JOIN newly_added n USING(appid) WHERE s.added_at >= ? AND s.appid IN (?, ?) ORDER BY s.appid DESC', params: ['2026-10-01', 2, 4] }, run);
  assert.deepEqual(join.rows, [[4], [2]]);
  const count = await query({}, { sql: 'WITH x AS (SELECT * FROM scrape_tracker WHERE Mail_status = ?) SELECT count(*) AS n FROM x', params: ['Sent'] }, run);
  assert.deepEqual(count.rows, [[126]]);
  db.close();
});

test('response bytes stop at complete rows with accurate continuation', async () => {
  const result = await query({}, { sql: 'SELECT value FROM scrape_tracker', offset: 5 }, async () => ({ columns: ['value'], rows: [['x'.repeat(700000)], ['x'.repeat(700000)]] }));
  assert.equal(result.row_count, 1); assert.equal(result.next_offset, 6); assert.equal(result.has_more, true);
  await assert.rejects(query({}, { sql: 'SELECT value FROM scrape_tracker' }, async () => ({ columns: ['value'], rows: [['x'.repeat(1100000)]] })), /substr/);
  await assert.rejects(boundedJSON(new Response('x'.repeat(50)), 20), /too large/);
});

const env = { MCP_AUTH_TOKEN: 'synthetic-test-key', TURSO_READ_TOKEN: 'synthetic-db-token', TURSO_URL: 'https://example.invalid' };
test('public mode allows an official client without any credentials', async () => {
  const publicEnv = { TURSO_READ_TOKEN: 'synthetic-db-token', TURSO_URL: 'https://example.invalid', PUBLIC_ACCESS: 'true' };
  const client = new Client({ name: 'public-test', version: '1.0.0' });
  await client.connect(new StreamableHTTPClientTransport(new URL('https://mcp.test/mcp'), {
    fetch: (input, init) => worker.fetch(new Request(input, init), publicEnv)
  }));
  assert.equal((await client.listTools()).tools.length, 8);
  const result = await client.callTool({ name: 'list_tables', arguments: {} });
  assert.equal(JSON.parse(result.content[0].text).tables.length, 3);
  assert.equal((await client.callTool({ name: 'query', arguments: { sql: 'DELETE FROM scrape_tracker' } })).isError, true);
  assert.equal((await worker.fetch(new Request('https://mcp.test/.well-known/oauth-protected-resource'), publicEnv)).status, 404);
  await client.close();
});
test('rejects missing/wrong auth and foreign origins before database access', async () => {
  assert.equal((await worker.fetch(new Request('https://mcp.test/mcp'), env)).status, 401);
  assert.equal((await worker.fetch(new Request('https://mcp.test/mcp', { headers: { Authorization: 'Bearer wrong' } }), env)).status, 401);
  assert.equal((await worker.fetch(new Request('https://mcp.test/mcp', { headers: { Authorization: 'Bearer synthetic-test-key', Origin: 'https://evil.test' } }), env)).status, 403);
});

test('official MCP client initializes, lists tools and gets validation errors', async () => {
  const client = new Client({ name: 'smoke-test', version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(new URL('https://mcp.test/mcp'), {
    requestInit: { headers: { Authorization: 'Bearer synthetic-test-key' } },
    fetch: (input, init) => worker.fetch(new Request(input, init), env)
  });
  await client.connect(transport);
  const tools = await client.listTools();
  assert.deepEqual(tools.tools.map(t => t.name), ['list_tables', 'describe_table', 'query', 'get_game_media', 'get_game_image', 'submit_triage', 'get_draft_context', 'save_draft']);
  const result = await client.callTool({ name: 'list_tables', arguments: {} });
  assert.deepEqual(JSON.parse(result.content[0].text).tables, ['scrape_tracker', 'newly_added', 'known_comingsoon']);
  assert.equal((await client.callTool({ name: 'query', arguments: { sql: 'DELETE FROM scrape_tracker' } })).isError, true);
  assert.equal((await client.callTool({ name: 'query', arguments: { sql: 'SELECT 1', page_size: -1 } })).isError, true);
  await client.close();
});
