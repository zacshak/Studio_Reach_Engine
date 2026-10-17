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

test('new socials intent supersedes the same appid across reload without losing unrelated actions', async () => {
  const storage = {};
  let ctx = context(storage, async () => { throw new Error('response lost'); });
  vm.runInContext(script, ctx);
  await vm.runInContext("api('require_socials',{appid:42,required:true})", ctx);
  await vm.runInContext("api('require_socials',{appid:43,required:true})", ctx);
  await vm.runInContext("api('keep',{appid:42})", ctx);
  const sent = [];
  ctx = context(storage, async (_, options) => {
    sent.push(JSON.parse(options.body));
    return { ok: true, json: async () => ({ ok: true }) };
  });
  vm.runInContext(script, ctx);
  await vm.runInContext("api('require_socials',{appid:'42',required:false})", ctx);
  assert.deepEqual(JSON.parse(storage.sre_pending_writes).map(item => item.slice(0, 2)), [
    ['require_socials', { appid: 43, required: true }], ['keep', { appid: 42 }]
  ]);
  await vm.runInContext('retryFailed()', ctx);
  assert.equal(sent.filter(item => String(item.appid) === '42' && item.action === 'require_socials').length, 1);
  assert.equal(storage.sre_pending_writes, '[]');
});

test('a failed replacement persists only the latest socials intent', async () => {
  const storage = {};
  const ctx = context(storage, async () => { throw new Error('offline'); });
  vm.runInContext(script, ctx);
  await vm.runInContext("api('require_socials',{appid:42,required:true})", ctx);
  await vm.runInContext("api('require_socials',{appid:42,required:false})", ctx);
  const saved = JSON.parse(storage.sre_pending_writes);
  assert.equal(saved.length, 1);
  assert.deepEqual(saved[0].slice(0, 2), ['require_socials', { appid: 42, required: false }]);
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
