import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import worker from './worker.js';
import { TRIAGE_RULES, triageCutoff, triagePrompt } from './triage-rules.js';

test('TRIAGE.md carries the exact rules the MCP prompt uses', () => {
  const doc = readFileSync(new URL('../TRIAGE.md', import.meta.url), 'utf8');
  assert.ok(doc.includes(TRIAGE_RULES));
});

test('rules require the sprite sheet, 3 to 5 more images on doubt, and no description-only decisions', () => {
  assert.match(TRIAGE_RULES, /sprite sheet MUST be looked at/);
  assert.match(TRIAGE_RULES, /No decision is made from the description alone/);
  assert.match(TRIAGE_RULES, /pull 3 to 5 more screenshots/);
  assert.match(TRIAGE_RULES, /Doubt means "pull more images", not "flag"/);
  assert.match(TRIAGE_RULES, /co-op sims, vehicle or delivery games, crafting plus combat, and shooters/);
  assert.match(TRIAGE_RULES, /Genre labels like "simulator"/);
  assert.match(TRIAGE_RULES, /All That Junk/);
  assert.doesNotMatch(TRIAGE_RULES, /in doubt.{0,10}flag/i);
});

test('cutoff is today plus 3 months and the prompt states both dates', () => {
  const today = new Date('2026-11-12T08:00:00Z');
  assert.equal(triageCutoff(today), '2027-02-12');
  const text = triagePrompt(40, today);
  assert.match(text, /Triage 40 Pending games/);
  assert.match(text, /Today is 2026-11-12; the release cutoff is 2027-02-12/);
  assert.match(text, /LIMIT 40/);
  assert.match(text, /every game where you pulled extra screenshots/);
  assert.ok(text.endsWith(TRIAGE_RULES));
});

test('the MCP server lists triage_games and serves the rules', async () => {
  const env = { PUBLIC_ACCESS: 'true', TURSO_URL: 'https://test.invalid', TURSO_READ_TOKEN: 'synthetic' };
  const client = new Client({ name: 'triage-prompt-test', version: '1' });
  await client.connect(new StreamableHTTPClientTransport(new URL('https://mcp.test/mcp'),
    { fetch: (input, init) => worker.fetch(new Request(input, init), env) }));
  try {
    const { prompts } = await client.listPrompts();
    assert.ok(prompts.some(p => p.name === 'triage_games'));
    const prompt = await client.getPrompt({ name: 'triage_games', arguments: { count: '25' } });
    assert.match(prompt.messages[0].content.text, /Triage 25 Pending games/);
    assert.ok(prompt.messages[0].content.text.includes(TRIAGE_RULES));
    assert.match(client.getInstructions(), /triage_games prompt/);
    assert.match(client.getInstructions(), /3 to 5 more images/);
  } finally { await client.close(); }
});
