import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8');
const script = html.slice(html.indexOf('const failed ='), html.indexOf('// ---- state ----'));
function context(storage, fetch) {
  const banner = { hidden: true, textContent: '' };
  return vm.createContext({ localStorage: { getItem: key => storage[key], setItem: (key, value) => { storage[key]=value; } },
    fetch, AbortSignal, console: { error() {} }, $: () => banner, render() {}, load: async () => {}, bulkBusy: false });
}
test('failed writes survive reload, preserve server error, and clear after confirmed retry', async () => {
  const storage = {};
  let ctx = context(storage, async () => ({ ok:false, status:503, json:async()=>({error:'Database temporarily unavailable'}) }));
  vm.runInContext(script, ctx);
  assert.equal(await vm.runInContext("api('keep', {appid:42})",ctx), false);
  assert.match(storage.sre_pending_writes, /Database temporarily unavailable/);
  ctx = context(storage, async()=>({ok:true, json:async()=>({ok:true})}));
  vm.runInContext(script,ctx);
  await vm.runInContext('retryFailed()',ctx);
  assert.equal(storage.sre_pending_writes,'[]');
});
test('workflow dispatch is not replayed after an uncertain response', async () => {
  const storage = {}; let requests = 0;
  const ctx = context(storage, async()=>{requests++; throw new Error('response lost');});
  vm.runInContext(script,ctx);
  await vm.runInContext("api('trigger',{workflow:'send'})",ctx);
  await vm.runInContext('retryFailed()',ctx);
  assert.equal(requests,1);
});
test('the inline dashboard script parses completely', () => {
  new vm.Script(html.slice(html.indexOf('<script>')+8,html.indexOf('</script>')));
});

test('reload removes obsolete socials intents and preserves unrelated retries and dispatch warnings', async () => {
  const retained = [['keep', { appid: 42 }, 'offline'], ['trigger', { workflow: 'send' }, 'response lost']];
  const storage = { sre_pending_writes: JSON.stringify([
    ['require_socials', { appid: 42, required: true }],
    ['require_socials', { appid: 43, required: false }],
    ['trigger', { workflow: 'socials' }, 'response lost'], ...retained
  ]) };
  const sent = [];
  const ctx = context(storage, async (_, options) => {
    sent.push(JSON.parse(options.body));
    return { ok: true, json: async () => ({ ok: true }) };
  });
  vm.runInContext(script, ctx);
  assert.deepEqual(JSON.parse(storage.sre_pending_writes), retained);
  await vm.runInContext('retryFailed()', ctx);
  assert.deepEqual(sent, [{ action: 'keep', appid: 42 }]);
  assert.deepEqual(JSON.parse(storage.sre_pending_writes), [retained[1]]);
});

test('obsolete intents stay out of memory even if storage cleanup fails', async () => {
  const storage = { sre_pending_writes: JSON.stringify([
    ['require_socials', { appid: 42, required: true }], ['trigger', { workflow: 'socials' }]
  ]) };
  let requests = 0;
  const ctx = context(storage, async () => { requests++; });
  ctx.localStorage.setItem = () => { throw new Error('storage unavailable'); };
  vm.runInContext(script, ctx);
  assert.equal(vm.runInContext('failed.length', ctx), 0);
  await vm.runInContext('retryFailed()', ctx);
  assert.equal(requests, 0);
});

test('partial rejection clears saved intent and reports blocked IDs without offering replay', async () => {
  const storage = {};
  let requests = 0;
  const elements = Object.fromEntries(['#activity', '#banner', '#bannerText', '#retryWrites'].map(key => [key, {}]));
  const result = { ok: true, deleted: [1], blocked: [2], cleanup: 'queued' };
  const ctx = context(storage, async () => { requests++; return { ok: true, json: async () => result }; });
  ctx.$ = key => elements[key];
  vm.runInContext(script, ctx);
  assert.deepEqual(await vm.runInContext("api('reject_all',{appids:[1,2]})", ctx), result);
  assert.equal(storage.sre_pending_writes, '[]');
  assert.equal(elements['#banner'].hidden, false);
  assert.equal(elements['#retryWrites'].hidden, true);
  assert.match(elements['#bannerText'].textContent, /currently Sending: 2/);
  await vm.runInContext('retryFailed()', ctx);
  assert.equal(requests, 1);
});
