import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8');
const source = html.slice(html.indexOf('let bulkBusy ='), html.indexOf('// ---- touch swipe'));
const context = extra => vm.createContext({ pendingCards: new Set(), failed: [], writeBusy: false, noMail: new Set(),
  render() {}, toast() {}, load: async () => {}, watchVerification() {}, ...extra });

for (const failAt of [0, 2]) {
  test(`Flagged Delete all confirms each batch and stops safely on failure ${failAt}`, async () => {
    const calls = [];
    let loads = 0;
    const ctx = context({ armed: 'deleteAll', tab: 'flagged', Q: { flagged: Array.from({ length: 12003 }, (_, i) => ({ appid: i + 1 })) },
      load: async () => { loads++; },
      api: async (action, { appids }) => {
        calls.push([...appids]);
        assert.equal(action, 'reject_all');
        assert.ok(ctx.Q.flagged.some(c => c.appid === appids[0]), 'not removed before confirmation');
        return calls.length !== failAt;
      }
    });
    vm.runInContext(source, ctx);
    await vm.runInContext('deleteAll()', ctx);
    assert.deepEqual(calls.map(ids => ids.length), [10000, 2003]);
    assert.equal(ctx.Q.flagged.length, failAt ? 2003 : 0);
    assert.equal(loads, 1);
    assert.equal(vm.runInContext('bulkBusy', ctx), false);
  });
}

for (const result of [{ ok: true, deleted: [1], blocked: [2], cleanup: 'queued' },
  { ok: true, deleted: [], blocked: [1, 2], cleanup: 'queued' }, { ok: true }]) {
  test(`Delete all handles ${result.deleted ? `${result.deleted.length} deleted` : 'legacy'} success and refreshes state`, async () => {
    let loads = 0;
    const ctx = context({ armed: 'deleteAll', tab: 'flagged', Q: { flagged: [{ appid: 1 }, { appid: 2 }], passed: [{ appid: 2 }] },
      api: async () => result,
      load: async () => {
        loads++;
        const deleted = result.deleted ?? [1, 2];
        assert.deepEqual(Array.from(ctx.Q.flagged, c => c.appid), [1, 2].filter(id => !deleted.includes(id)));
      }
    });
    vm.runInContext(source, ctx);
    await vm.runInContext('deleteAll()', ctx);
    assert.equal(loads, 1);
    assert.equal(vm.runInContext('bulkDone', ctx), result.deleted?.length ?? 2);
  });
}

test('Delete all needs a second tap', async () => {
  let calls = 0;
  const ctx = context({ armed: null, tab: 'flagged', Q: { flagged: [{ appid: 1 }] }, api: async () => { calls++; return { ok: true }; } });
  vm.runInContext(source, ctx);
  await vm.runInContext('deleteAll()', ctx);
  assert.equal(calls, 0);
  assert.equal(vm.runInContext('armed', ctx), 'deleteAll');
});

test('Passed Accept all skips games without email, batches by 10 and reports refusals', async () => {
  const calls = [], toasts = [];
  let loads = 0;
  const passed = Array.from({ length: 25 }, (_, i) => ({ appid: i + 1 }));
  const ctx = context({ armed: 'acceptAll', tab: 'passed', Q: { passed }, noMail: new Set([3, 4]),
    toast: (message, kind) => toasts.push([message, kind]), load: async () => { loads++; },
    api: async (action, { appids }) => {
      assert.equal(action, 'accept_many');
      calls.push([...appids]);
      const failed = appids.filter(a => a === 7).map(appid => ({ appid, error: 'sprite sheet is missing or empty' }));
      return { accepted: appids.filter(a => a !== 7), failed };
    }
  });
  vm.runInContext(source, ctx);
  await vm.runInContext('acceptAll()', ctx);
  assert.deepEqual(calls.map(ids => ids.length), [10, 10, 3]);
  assert.ok(!calls.flat().includes(3) && !calls.flat().includes(4), 'games without email are never sent');
  assert.deepEqual(ctx.Q.passed.map(c => c.appid), [3, 4, 7]);
  assert.equal(loads, 1);
  assert.equal(toasts.length, 1);
  assert.match(toasts[0][0], /Checking the email of 22\. Skipped 2 without an email\. 1 refused: sprite sheet is missing or empty\./);
  assert.equal(toasts[0][1], 'error');
});

test('Accept all stops at a failed batch and still reports what was accepted', async () => {
  const toasts = [];
  let calls = 0;
  const ctx = context({ armed: 'acceptAll', tab: 'passed', Q: { passed: Array.from({ length: 30 }, (_, i) => ({ appid: i + 1 })) },
    toast: message => toasts.push(message),
    api: async (_action, { appids }) => (++calls === 2 ? false : { accepted: appids, failed: [] }) });
  vm.runInContext(source, ctx);
  await vm.runInContext('acceptAll()', ctx);
  assert.equal(calls, 2);
  assert.equal(ctx.Q.passed.length, 20);
  assert.match(toasts[0], /^Checking the email of 10\.$/);
});
