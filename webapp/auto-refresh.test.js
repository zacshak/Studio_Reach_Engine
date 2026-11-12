import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8');
const script = html.slice(html.indexOf('// ---- auto refresh ----'), html.lastIndexOf('</script>'));

function setup() {
  const timers = [], listeners = {}, calls = [];
  const ctx = vm.createContext({
    Date, Q: {}, writeBusy: false, bulkBusy: false, pendingCards: new Set(), armed: null,
    document: { visibilityState: 'visible', addEventListener: (type, fn) => { listeners[type] = fn; } },
    $: () => ({ open: false }),
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
    load: async opts => { calls.push(opts); },
  });
  vm.runInContext(script, ctx);
  return { ctx, timers, listeners, calls };
}

test('auto refresh runs a background load every 30 s and reschedules itself', async () => {
  const { timers, calls } = setup();
  assert.equal(timers.length, 1, 'first refresh is scheduled at start');
  await timers[0]();
  assert.equal(JSON.stringify(calls), '[{"background":true}]');
  assert.equal(timers.length, 2, 'next refresh is scheduled after each one');
  await timers[1]();
  assert.equal(calls.length, 2);
});

test('auto refresh is skipped while hidden, saving, armed, dialog open or before first load', async () => {
  for (const block of ["document.visibilityState='hidden'", 'writeBusy=true', 'bulkBusy=true',
                       'pendingCards.add(1)', "armed='reject'", 'Q=null', '$=()=>({open:true})']) {
    const { ctx, calls } = setup();
    vm.runInContext(block, ctx);
    await vm.runInContext('autoRefresh()', ctx);
    assert.equal(calls.length, 0, block);
  }
});

test('returning to the tab refreshes once, not repeatedly within 5 s', async () => {
  const { ctx, listeners, calls } = setup();
  vm.runInContext('lastLoadAt = Date.now() - 60000', ctx);
  listeners.visibilitychange();
  await new Promise(r => setImmediate(r));
  listeners.visibilitychange();
  await new Promise(r => setImmediate(r));
  assert.equal(calls.length, 1);
});
