import test from 'node:test';
import assert from 'node:assert/strict';
import { gameMedia, submitTriage } from './media-triage.js';

function bucket(seed = {}) {
  const items = new Map(Object.entries(seed).map(([k, v]) => [k, { data: typeof v === 'object' && !Buffer.isBuffer(v) ? JSON.stringify(v) : v, version: 1 }]));
  return {
    items, conflicts: 0,
    async get(key) {
      const item = items.get(key);
      if (!item) return null;
      const bytes = Buffer.from(item.data);
      return { etag: String(item.version), size: bytes.length, json: async () => JSON.parse(bytes),
        arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length) };
    },
    async put(key, data, options) {
      if (this.conflicts-- > 0) return null;
      const old = items.get(key);
      if (options.onlyIf.etagMatches && String(old?.version) !== options.onlyIf.etagMatches) return null;
      if (options.onlyIf.etagDoesNotMatch && old) return null;
      items.set(key, { data, version: (old?.version || 0) + 1 });
      return { etag: String(items.get(key).version) };
    }
  };
}
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOZ0AAAAASUVORK5CYII=', 'base64');
const media = () => bucket({ 'index.json': { 1: 'Game_1', 2: 'Game_2' },
  'Game_1/manifest.json': { name: 'Synthetic', desc: 'Only synthetic data.', images: ['shot.png', 'SpriteSheet_Screenshots.png', '../secret.png'] },
  'Game_1/SpriteSheet_Screenshots.png': png, 'Game_1/shot.png': png });
const read = async () => ({ rows: [[1], [2]] });

test('default media is the actual sprite sheet and additional images are explicitly selectable', async () => {
  const env = { MEDIA: media() };
  const result = await gameMedia(env, 1, undefined, read);
  const info = JSON.parse(result.content[0].text);
  assert.equal(info.image, 'SpriteSheet_Screenshots.png');
  assert.deepEqual(info.available_images, ['shot.png', 'SpriteSheet_Screenshots.png']);
  assert.equal(result.content[1].type, 'image');
  assert.equal(result.content[1].data, png.toString('base64'));
  assert.equal(JSON.parse((await gameMedia(env, 1, 'shot.png', read)).content[0].text).image, 'shot.png');
  await assert.rejects(gameMedia(env, 1, '../secret.png', read), /listed/);
  await assert.rejects(gameMedia(env, 1, undefined, async () => ({ rows: [] })), /does not exist/);
  await assert.rejects(gameMedia(env, 2, undefined, read), /manifest/);
});

test('submit_triage forwards the batch to the review backend and returns its result', async () => {
  const calls = [];
  const REVIEW = { fetch: async request => {
    calls.push({ url: request.url, method: request.method, body: await request.json() });
    return Response.json({ saved: [1], skipped: [{ appid: 3, reason: 'Missing game/media, or already triaged (only Pending games can be triaged).' }] });
  } };
  const input = { criteria: 'Only games with vehicles', results: [
    { appid: 1, decision: 'flag', reason: 'No vehicles visible' },
    { appid: 3, decision: 'pass', reason: 'Already advanced' }] };
  const result = await submitTriage({ REVIEW }, input);
  assert.deepEqual(result.saved, [1]);
  assert.equal(result.skipped[0].appid, 3);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://review.internal/api/triage/save');
  assert.equal(calls[0].method, 'POST');
  assert.deepEqual(calls[0].body, input);
});

test('backend errors, a missing binding and duplicate IDs fail explicitly', async () => {
  const result = { appid: 1, decision: 'flag', reason: 'custom' };
  const REVIEW = { fetch: async () => Response.json({ error: 'database unavailable' }, { status: 500 }) };
  await assert.rejects(submitTriage({ REVIEW }, { criteria: 'custom', results: [result] }), /database unavailable/);
  await assert.rejects(submitTriage({}, { criteria: 'custom', results: [result] }), /binding/);
  await assert.rejects(submitTriage({ REVIEW }, { criteria: 'custom', results: [result, result] }), /only once/);
});
