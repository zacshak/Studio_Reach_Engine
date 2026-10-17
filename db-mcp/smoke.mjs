import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const url = new URL(process.argv[2]);
assert.equal((await fetch(url)).status, 405);
const client = new Client({ name: 'deployment-smoke', version: '1.0.0' });
try {
  await client.connect(new StreamableHTTPClientTransport(url));
  assert.equal((await client.listTools()).tools.length, 11);
  const list = await client.callTool({ name: 'list_tables', arguments: {} });
  const tables = JSON.parse(list.content[0].text).tables;
  assert.equal(tables.length, 3);
  for (const table of tables) {
    const schema = await client.callTool({ name: 'describe_table', arguments: { table } });
    assert.ok(!schema.isError, 'Live schema inspection failed');
    assert.ok(JSON.parse(schema.content[0].text).rows.length > 0);
    const count = await client.callTool({ name: 'query', arguments: { sql: `SELECT count(*) FROM ${table}` } });
    assert.ok(!count.isError, 'Live query failed');
    assert.equal(JSON.parse(count.content[0].text).rows.length, 1);
  }
  const denied = await client.callTool({ name: 'query', arguments: { sql: 'SELECT * FROM email_verification_cache LIMIT 0' } });
  assert.equal(denied.isError, true);
  const candidates = await client.callTool({ name: 'query', arguments: {
    sql: "SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' AND triage_kept=0 ORDER BY appid DESC LIMIT 10"
  } });
  assert.ok(!candidates.isError);
  let imageChecked = false;
  for (const [appid] of JSON.parse(candidates.content[0].text).rows) {
    const media = await client.callTool({ name: 'get_game_media', arguments: { appid } });
    if (media.isError) continue;
    assert.ok(media.content.some(c => c.type === 'image' && c.data.length > 0));
    const info = JSON.parse(media.content[0].text);
    const extra = info.available_images.find(n => n !== info.image);
    if (extra) {
      const additional = await client.callTool({ name: 'get_game_image', arguments: { appid, image_name: extra } });
      assert.ok(!additional.isError);
      assert.ok(additional.content.some(c => c.type === 'image'));
    }
    imageChecked = true;
    break;
  }
  assert.ok(imageChecked, 'No available pending-game image found for live smoke test.');
  const absent = await client.callTool({ name: 'query', arguments: { sql: 'SELECT count(*) FROM scrape_tracker WHERE appid=?', params: [Number.MAX_SAFE_INTEGER] } });
  assert.equal(JSON.parse(absent.content[0].text).rows[0][0], 0);
  const skip = await client.callTool({ name: 'submit_triage', arguments: { criteria: 'Synthetic no-op deployment check', results: [{ appid: Number.MAX_SAFE_INTEGER, decision: 'pass', reason: 'Nonexistent test ID, must skip' }] } });
  assert.ok(!skip.isError);
  assert.deepEqual(JSON.parse(skip.content[0].text).saved, []);
  assert.equal(JSON.parse(skip.content[0].text).skipped.length, 1);
  console.log('Live media and triage smoke passed: sprite sheet, optional extra image, and nonexistent-row submission skipped. No real game classified.');
  console.log('Live public MCP smoke passed without credentials: tool discovery, three schemas, three aggregate queries, and denied table access.');
} finally { await client.close(); }
