import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8');
const source = html.slice(html.indexOf('let bulkBusy ='), html.indexOf('// ---- touch swipe'));
for (const failAt of [0, 2]) {
  test(`bulk deletion confirms each batch and stops safely on failure ${failAt}`, async () => {
    const calls = [];
    let loads = 0;
    const ctx = vm.createContext({ writeBusy: false, armed: 'bulkR', tab: 'triage', Q: { triage: Array.from({ length: 12003 }, (_, i) => ({ appid: i + 1 })) },
      render() {}, load: async () => { loads++; },
      api: async (action, { appids }) => {
        calls.push([...appids]);
        assert.equal(action, 'reject_all_irrelevant');
        assert.ok(ctx.Q.triage.some(c => c.appid === appids[0]), 'not removed before confirmation');
        return calls.length !== failAt;
      }
    });
    vm.runInContext(source, ctx);
    await vm.runInContext('bulkReject()', ctx);
    assert.deepEqual(calls.map(ids => ids.length), [10000, 2003]);
    assert.equal(ctx.Q.triage.length, failAt ? 2003 : 0);
    assert.equal(loads, 1);
    assert.equal(vm.runInContext('bulkBusy', ctx), false);
  });
}

for (const result of [{ ok: true, deleted: [1], blocked: [2], cleanup: 'queued' },
  { ok: true, deleted: [], blocked: [1, 2], cleanup: 'queued' }, { ok: true }]) {
  test(`bulk rejection handles ${result.deleted ? `${result.deleted.length} deleted` : 'legacy'} success and refreshes state`, async () => {
    let loads = 0;
    const ctx = vm.createContext({ writeBusy: false, armed: 'bulkR', tab: 'nomail',
      Q: { nomail: [{ appid: 1 }, { appid: 2 }], socials: [{ appid: 2 }] },
      render() {}, api: async () => result,
      load: async () => {
        loads++;
        const deleted = result.deleted ?? [1, 2];
        assert.deepEqual(Array.from(ctx.Q.nomail, c => c.appid), [1, 2].filter(id => !deleted.includes(id)));
        assert.deepEqual(Array.from(ctx.Q.socials, c => c.appid), [2].filter(id => !deleted.includes(id)));
      }
    });
    vm.runInContext(source, ctx);
    await vm.runInContext('bulkReject()', ctx);
    assert.equal(loads, 1);
    assert.equal(vm.runInContext('bulkDone', ctx), result.deleted?.length ?? 2);
  });
}
