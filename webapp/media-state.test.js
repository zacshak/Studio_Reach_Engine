import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8');
const source = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('load();'));
const emptyState = { index: {}, triage: [], approval: [], mail: [], nomail: [], socials: [] };
const response = value => ({ ok: true, json: async () => value });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function fixture(fetch) {
  const elements = new Map();
  const ctx = vm.createContext({ fetch, AbortSignal, URL, console,
    localStorage: { getItem: () => null, setItem() {} },
    Image: class {},
    document: {
      querySelector: key => {
        if (!elements.has(key)) elements.set(key, { innerHTML: '', querySelectorAll: () => [] });
        return elements.get(key);
      },
      querySelectorAll: () => [], addEventListener() {}
    }
  });
  vm.runInContext(source, ctx);
  return { ctx, run: code => vm.runInContext(code, ctx), elements };
}

test('refresh refetches cached drafts and ignores an earlier manifest completion', async () => {
  const old = deferred();
  let calls = 0;
  const { run } = fixture(async url => {
    if (url === '/api/state') return response(emptyState);
    return ++calls === 1 ? old.promise : response({ mail: 'new draft', images: [] });
  });
  const pending = run("manifest('Game_1')");
  await run('load()');
  assert.equal((await run("manifest('Game_1')")).mail, 'new draft');
  old.resolve(response({ mail: 'old draft', images: [] }));
  assert.equal(await pending, null);
  assert.equal(run("manifests.get('Game_1').mail"), 'new draft');
  await run('load()');
  await run("manifest('Game_1')");
  assert.equal(calls, 3);
});

test('stale manifest failures cannot overwrite a fresh generation', async () => {
  const old = deferred();
  let calls = 0;
  const { run } = fixture(async url => url === '/api/state' ? response(emptyState)
    : ++calls === 1 ? old.promise : response({ mail: 'fresh', images: [] }));
  const pending = run("manifest('Game_1')");
  await run('load()');
  await run("manifest('Game_1')");
  old.reject(new Error('old timeout'));
  assert.equal(await pending, null);
  assert.equal(run("manifests.get('Game_1').mail"), 'fresh');
});

test('an older state response cannot invalidate newer manifests', async () => {
  const old = deferred();
  let calls = 0;
  const { run } = fixture(async () => ++calls === 1 ? old.promise : response(emptyState));
  const first = run('load()');
  await run('load()');
  run("manifests.set('Game_1',{mail:'current'})");
  const generation = run('mediaGeneration');
  old.resolve(response({ ...emptyState, index: { 1: 'Old_1' } }));
  await first;
  assert.equal(run('mediaGeneration'), generation);
  assert.equal(run("manifests.get('Game_1').mail"), 'current');
  assert.equal(run('S.index[1]'), undefined);
});

test('hero, thumbnails and expanded images expose failure and cache-bypassing retry', async () => {
  const media = { images: ['SpriteSheet_Screenshots.png', 'shot.png'], mail: 'draft' };
  const state = { ...emptyState, index: { 1: 'Game_1' }, mail: [{ appid: 1 }] };
  const { ctx, run, elements } = fixture(async url => response(url === '/api/state' ? state : media));
  await run('load()');
  // Flush the manifest fetch started by render().
  await new Promise(resolve => setImmediate(resolve));
  run('showAll=true;render()');
  const view = elements.get('#view');
  assert.equal((view.innerHTML.match(/onerror="imageFailed\(this\)"/g) || []).length, 5);
  const key = '/media/Game_1/shot.png';
  ctx.failedImage = { isConnected: true, dataset: { mediaKey: key, generation: String(run('mediaGeneration')) } };
  run('imageFailed(failedImage)');
  assert.match(view.innerHTML, /Could not load image: shot.png/);
  assert.match(view.innerHTML, /onclick="retryMedia\(\)"/);
  assert.doesNotMatch(view.innerHTML, /src="\/media\/Game_1\/shot.png"/);
  run('retryMedia()');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(view.innerHTML, /src="\/media\/Game_1\/shot.png\?retry=1"/);
  assert.doesNotMatch(view.innerHTML, /Could not load image/);
  // A late event from the replaced DOM must not reintroduce the error.
  run('imageFailed(failedImage)');
  assert.equal(run('imageFailures.size'), 0);
  ctx.failedImage = { isConnected: false, dataset: { mediaKey: key, generation: String(run('mediaGeneration')) } };
  run('imageFailed(failedImage)');
  assert.equal(run('imageFailures.size'), 0);
});

test('manifest failure retains the existing explicit retry path', async () => {
  const { run, elements } = fixture(async url => url === '/api/state'
    ? response({ ...emptyState, index: { 1: 'Game_1' }, mail: [{ appid: 1 }] })
    : { ok: false, status: 503 });
  await run('load()');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements.get('#view').innerHTML, /Media request failed \(503\)/);
  assert.match(elements.get('#view').innerHTML, /Retry images/);
});

test('background refresh preserves loaded media but revalidates newly drafted rows', async () => {
  const next={...emptyState,index:{1:'Game_1'},mail:[{appid:1}]};
  const {run}=fixture(async url=>response(url==='/api/state'?next:{mail:'fresh draft',images:[]}));
  run("S={index:{},mail:[]}; Q={triage:[],approval:[],mail:[],nomail:[],socials:[]}; manifests.set('Game_1',{mail:null}); manifests.set('Other_2',{name:'cached'})");
  await run('load({background:true})');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run("manifests.get('Game_1').mail"),'fresh draft');
  assert.equal(run("manifests.get('Other_2').name"),'cached');
});

test('neighbor prefetch does not repaint the current card or fetch neighboring images', async () => {
  let paints=0,images=0;
  const {ctx,run}=fixture(async()=>response({images:['SpriteSheet_Screenshots.png']}));
  ctx.Image=class { constructor(){images++;} };
  ctx.countPaint=()=>paints++;
  run("Q={triage:[{folder:'Current_1'},{folder:'Neighbor_2'}]};render=countPaint");
  await run("manifest('Neighbor_2')");
  assert.equal(paints,0);
  assert.equal(images,0);
});

test('preview assets and lazy thumbnails replace full PNG downloads only for display', () => {
  const {run}=fixture(async()=>response({}));
  run("manifests.set('Game_1',{images:['SpriteSheet_Screenshots.png'],previews:{'SpriteSheet_Screenshots.png':{src:'preview-abc.webp',thumb:'thumb-abc.webp',width:860,height:484,thumb_width:160,thumb_height:90}}})");
  assert.match(run("imageTag('Game_1','SpriteSheet_Screenshots.png','class=\"hero\"')"),/preview-abc.webp/);
  assert.match(run("imageTag('Game_1','SpriteSheet_Screenshots.png','data-thumb loading=\"lazy\"')"),/thumb-abc.webp/);
  assert.equal(run("manifests.get('Game_1').images[0]"),'SpriteSheet_Screenshots.png');
});

test('background refresh failure keeps the current card visible', async () => {
  const {run,elements}=fixture(async()=>{throw new Error('offline');});
  run("S={mail:[]};Q={triage:[],approval:[],mail:[],nomail:[],socials:[]};document.querySelector('#view').innerHTML='Current card'");
  await run('load({background:true})');
  assert.equal(elements.get('#view').innerHTML,'Current card');
  assert.match(run('actionNotice'),/offline/);
});
