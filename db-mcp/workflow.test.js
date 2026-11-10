import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import worker from './worker.js';
import { getDraft, r2Folder } from './workflow.js';

// Public-mode MCP with a recording REVIEW binding; no database or GitHub is reached.
function setup() {
  const calls = [];
  const env = { PUBLIC_ACCESS: 'true', TURSO_URL: 'https://db.invalid', TURSO_READ_TOKEN: 'x', MEDIA: {},
    REVIEW: { fetch: async request => {
      const body = await request.json();
      calls.push({ path: new URL(request.url).pathname, body });
      return Response.json({ ok: true, echo: body });
    } } };
  return { env, calls };
}
async function connect(env) {
  const client = new Client({ name: 'workflow-test', version: '1.0.0' });
  await client.connect(new StreamableHTTPClientTransport(new URL('https://mcp.test/mcp'), {
    fetch: (input, init) => worker.fetch(new Request(input, init), env) }));
  return client;
}

test('workflow tools forward to the guarded review backend', async () => {
  const { env, calls } = setup();
  const client = await connect(env);
  const cases = [
    ['get_queues', {}, '/api/queues', {}],
    ['get_triage', { appids: [1] }, '/api/triage/get', { appids: [1] }],
    ['keep_games', { appids: [1] }, '/api/act', { action: 'keep_many', appids: [1] }],
    ['accept_games', { appids: [1, 2] }, '/api/act', { action: 'accept_many', appids: [1, 2] }],
    ['reject_games', { appids: [3], confirm: 'DELETE' }, '/api/act', { action: 'reject_all', appids: [3] }],
    ['save_email_result', { appid: 4, status: 'no_email' }, '/api/email/save', { appid: 4, status: 'no_email' }],
    ['approve_mails', { appids: [5] }, '/api/act', { action: 'approve_many', appids: [5] }],
    ['return_to_accepted', { appids: [5] }, '/api/act', { action: 'return_to_accepted', appids: [5] }],
    ['run_workflow', { workflow: 'hermes' }, '/api/act', { action: 'trigger', workflow: 'hermes' }],
    ['get_workflow_runs', { workflow: 'send' }, '/api/workflows/runs', { workflow: 'send', limit: 5 }],
    ['send_scheduled_mails', { confirm: 'SEND_REAL_EMAILS' }, '/api/act', { action: 'trigger', workflow: 'send' }],
  ];
  for (const [name, args, path, body] of cases) {
    const result = await client.callTool({ name, arguments: args });
    assert.notEqual(result.isError, true, name);
    assert.deepEqual(calls.at(-1), { path, body }, name);
  }
  await client.close();
});

test('destructive and outbound tools refuse without their exact confirmation', async () => {
  const { env, calls } = setup();
  const client = await connect(env);
  for (const [name, args] of [
    ['reject_games', { appids: [3] }], ['reject_games', { appids: [3], confirm: 'yes' }],
    ['send_scheduled_mails', {}], ['send_scheduled_mails', { confirm: 'SEND' }],
    ['run_workflow', { workflow: 'send' }], ['accept_games', { appids: Array.from({ length: 101 }, (_, i) => i + 1) }],
  ]) assert.equal((await client.callTool({ name, arguments: args })).isError, true, `${name} ${JSON.stringify(args)}`);
  assert.equal(calls.length, 0);
  await client.close();
});

test('backend errors are returned as tool errors', async () => {
  const { env } = setup();
  env.REVIEW = { fetch: async () => Response.json({ error: 'not Pending' }, { status: 500 }) };
  const client = await connect(env);
  const result = await client.callTool({ name: 'keep_games', arguments: { appids: [1] } });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /not Pending/);
  await client.close();
});

const bucket = objects => ({
  get: async key => objects[key] ? { json: async () => objects[key] } : null,
  list: async ({ prefix, cursor }) => {
    const keys = Object.keys(objects).filter(k => k.startsWith(prefix)).sort();
    const start = cursor ? Number(cursor) : 0;
    return { objects: keys.slice(start, start + 2).map(key => ({ key, size: 10 })),
      truncated: start + 2 < keys.length, cursor: String(start + 2) };
  },
});

test('get_draft splits subject and body and reports a missing draft', async () => {
  const MEDIA = bucket({ 'index.json': { 5: 'G_5', 6: 'G_6' },
    'G_5/manifest.json': { mail: 'Subject: Hello Studio\n\nBody text.', mail_template: 2 }, 'G_6/manifest.json': {} });
  const run = async (_env, _sql, [id]) => ({ rows: id === 9 ? [] : [[id === 5 ? 'Drafted' : 'Accepted', 'jobs@studio.com, hi@studio.com', 2]] });
  assert.deepEqual(await getDraft({ MEDIA }, 5, run), { appid: 5, mail_status: 'Drafted', recipient: 'jobs@studio.com',
    template_id: 2, has_draft: true, subject: 'Hello Studio', body: 'Body text.' });
  assert.equal((await getDraft({ MEDIA }, 6, run)).has_draft, false);
  await assert.rejects(getDraft({ MEDIA }, 9, run), /does not exist/);
});

test('get_r2_folder lists every page and checks leftover folders by name', async () => {
  const MEDIA = bucket({ 'index.json': { 1: 'G_1' }, 'G_1/a.png': 1, 'G_1/b.png': 1, 'G_1/manifest.json': 1, 'G_10/x': 1 });
  const listed = await r2Folder({ MEDIA }, { appid: 1 });
  assert.equal(listed.count, 3); assert.equal(listed.bytes, 30);
  assert.deepEqual(listed.objects.map(o => o.key), ['a.png', 'b.png', 'manifest.json']);
  assert.equal((await r2Folder({ MEDIA }, { folder: 'Old_7' })).count, 0);
  assert.equal((await r2Folder({ MEDIA }, { appid: 2 })).indexed, false);
  await assert.rejects(r2Folder({ MEDIA }, {}), /exactly one/);
  await assert.rejects(r2Folder({ MEDIA }, { folder: '../x' }), /single folder/);
});
