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
