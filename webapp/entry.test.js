// entry.js imports the page as a Text module (a wrangler rule). Node gets the same
// behaviour from a tiny loader hook, so the deployed entry point is tested as written.
import test from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
import { readFileSync } from 'node:fs';

register('data:text/javascript,' + encodeURIComponent(`
  export async function load(url, context, next) {
    if (url.endsWith('.html')) {
      const { readFile } = await import('node:fs/promises');
      const text = await readFile(new URL(url), 'utf8');
      return { format: 'module', source: 'export default ' + JSON.stringify(text), shortCircuit: true };
    }
    return next(url, context);
  }`));
const { default: entry, servePage } = await import('./entry.js');
const page = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8');

test('the page is served from the Worker bundle at / and /index.html, never cached', async () => {
  for (const path of ['/', '/index.html']) {
    const response = await entry.fetch(new Request('https://sre-review.test' + path), {}, {});
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('Content-Type'), 'text/html; charset=utf-8');
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.equal(await response.text(), page);
  }
  const head = servePage(new Request('https://sre-review.test/', { method: 'HEAD' }));
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
});

test('everything else goes to the review worker', async () => {
  assert.equal(servePage(new Request('https://sre-review.test/', { method: 'POST' })), null);
  assert.equal(servePage(new Request('https://sre-review.test/api/state')), null);
  const response = await entry.fetch(new Request('https://sre-review.test/nope'), {}, {});
  assert.equal(response.status, 404);
  const media = await entry.fetch(new Request('https://sre-review.test/media/x.json'),
    { MEDIA: { get: async () => ({ body: '{"ok":1}', httpMetadata: { contentType: 'application/json' } }) } }, {});
  assert.equal(await media.text(), '{"ok":1}');
});

test('wrangler bundles the page as text instead of uploading assets', () => {
  const config = readFileSync(new URL('./wrangler.jsonc', import.meta.url), 'utf8');
  assert.match(config, /"main": "entry.js"/);
  assert.match(config, /"type": "Text", "globs": \["\*\*\/\*\.html"\]/);
  assert.doesNotMatch(config, /"assets"/);
});
