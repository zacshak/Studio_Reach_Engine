import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const url = new URL(process.argv[2]);
const secrets = JSON.parse(await readFile(new URL('./credentials.json', import.meta.url), 'utf8'));
assert.equal((await fetch(url)).status, 401);
const client = new Client({ name: 'deployment-smoke', version: '1.0.0' });
try {
  await client.connect(new StreamableHTTPClientTransport(url, {
    requestInit: { headers: { Authorization: `Bearer ${secrets.MCP_AUTH_TOKEN}` } }
  }));
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
  console.log('Live MCP smoke passed: authentication, tool discovery, three schemas, three aggregate queries, and denied table access.');
} finally { await client.close(); }
