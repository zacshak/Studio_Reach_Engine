import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('./public/index.html', import.meta.url), 'utf8');
const source = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('load();'));
const emptyState = { index: {}, counts: {}, pending: [], passed: [], flagged: [], noMail: [], mail: [], triageReviews: {} };
const response = value => ({ ok: true, json: async () => value });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function fixture(fetch) {
  const elements = new Map();
  const ctx = vm.createContext({ fetch, AbortSignal, URL, console, setTimeout, clearTimeout,
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

test('Home plus the four review views render without socials state or controls', async () => {
  const views = ['pending', 'passed', 'flagged', 'mail'];
  assert.deepEqual([...html.matchAll(/data-tab="([^"]+)"/g)].map(match => match[1]), ['home', ...views]);
  assert.doesNotMatch(html, /renderSocials|toggleSocials|reqsocial|social-links|social-empty|Fetch requested socials|social research/);
  const buttons = { pending: ['reject', 'triage_pass'], passed: ['flag', 'accept'], flagged: ['reject', 'pass'], mail: ['reject', 'approve'] };
  for (const view of views) {
    const state = { ...emptyState, index: { 1: 'Game_1' }, [view]: view === 'mail' ? [{ appid: 1 }] : [1] };
    const { run, elements } = fixture(async url => response(url === '/api/state' ? state
      : { name: 'Game', desc: 'Description', images: ['SpriteSheet_Screenshots.png'], mail: 'Draft' }));
    await run('load()');
    assert.equal(run('tab'), 'home', 'the dashboard opens on Home');
    run(`openTab('${view}')`);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(Array.from(run('Object.keys(Q)')), views);
    assert.match(elements.get('#view').innerHTML, /Description/);
    assert.deepEqual([...elements.get('#view').innerHTML.matchAll(/doAct\('([a-z_]+)'\)/g)].map(m => m[1]), buttons[view]);
    assert.doesNotMatch(elements.get('#view').innerHTML, /social/i);
  }
});

test('Home lists every count and links the review views', async () => {
  const counts = { pending: 230, triaged: 1190, passed: 1185, flagged: 5, no_mail: 24, accepted: 3, drafted: 2,
    scheduled: 2, sending: 0, sent: 2341, replied: 224, invalid: 1 };
  const { run, elements } = fixture(async () => response({ ...emptyState, counts, scheduled: true, pendingDrafts: true }));
  await run('load()');
  const page = elements.get('#view').innerHTML;
  for (const [label, n] of [['Pending', 230], ['Triaged', 1190], ['Passed', 1185], ['Flagged', 5], ['No email (not accepted)', 24],
    ['Accepted', 3], ['Drafted', 2], ['Scheduled', 2], ['Sent', 2341], ['Replied', 224], ['Invalid', 1]])
    assert.match(page, new RegExp(`<b>${n}</b><span>${label.replace(/[()]/g, '\\$&')}</span>`), label);
  assert.doesNotMatch(page, /Sending/, 'Sending only shows while non-zero');
  for (const view of ['pending', 'passed', 'flagged', 'mail']) assert.match(page, new RegExp(`openTab\\('${view}'\\)`));
  assert.match(page, /Draft accepted mails/);
  assert.match(page, /Send scheduled mails/);
});

test('Passed Accept on a game without email shows an error without any request', async () => {
  let writes = 0;
  const state = { ...emptyState, index: { 1: 'A_1' }, passed: [1], noMail: [1] };
  const { run, elements } = fixture(async url => { if (url === '/api/act') writes++; return response(url === '/api/state' ? state : { name: 'Alpha', images: [] }); });
  await run('load()');
  run("openTab('passed')");
  await new Promise(resolve => setImmediate(resolve));
  await run("doAct('accept')");
  assert.equal(writes, 0);
  assert.match(elements.get('#toast').textContent, /Can't accept Alpha: it has no email/);
  assert.equal(run('Q.passed.length'), 1);
  assert.match(elements.get('#view').innerHTML, /No email/);
});

test('a refused card action is final: the card returns and nothing is kept for retry', async () => {
  const state = { ...emptyState, index: { 1: 'A_1' }, passed: [1] };
  const { run, elements } = fixture(async url => url === '/api/act'
    ? { ok: false, status: 409, json: async () => ({ error: 'Cannot accept 1: has no email.', refused: true }) }
    : response(url === '/api/state' ? state : { images: [] }));
  await run('load()');
  run("openTab('passed')");
  await run("doAct('accept')");
  assert.equal(run('failed.length'), 0);
  assert.equal(run('Q.passed.length'), 1);
  assert.match(elements.get('#toast').textContent, /has no email/);
});

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
  run("tab='mail'"); await run('load()');
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
  run("tab='mail'"); await run('load()');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements.get('#view').innerHTML, /Media request failed \(503\)/);
  assert.match(elements.get('#view').innerHTML, /Retry images/);
});

test('background refresh preserves loaded media but revalidates newly drafted rows', async () => {
  const next={...emptyState,index:{1:'Game_1'},mail:[{appid:1}]};
  const {run}=fixture(async url=>response(url==='/api/state'?next:{mail:'fresh draft',images:[]}));
  run("S={index:{},mail:[]}; Q={pending:[],passed:[],flagged:[],mail:[]}; manifests.set('Game_1',{mail:null}); manifests.set('Other_2',{name:'cached'})");
  run("tab='mail'"); await run('load({background:true})');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run("manifests.get('Game_1').mail"),'fresh draft');
  assert.equal(run("manifests.get('Other_2').name"),'cached');
});

test('neighbor prefetch does not repaint the current card or fetch neighboring images', async () => {
  let paints=0,images=0;
  const {ctx,run}=fixture(async()=>response({images:['SpriteSheet_Screenshots.png']}));
  ctx.Image=class { constructor(){images++;} };
  ctx.countPaint=()=>paints++;
  run("tab='flagged';Q={flagged:[{folder:'Current_1'},{folder:'Neighbor_2'}]};render=countPaint");
  await run("manifest('Neighbor_2')");
  assert.equal(paints,0);
  assert.equal(images,0);
});

test('neighbor prefetch downloads bounded lookahead hero previews once and uses the rendered URL', async () => {
  const images=[];
  const {ctx,run}=fixture(async()=>response({images:['shot.jpg','SpriteSheet_Screenshots.png'],previews:{'SpriteSheet_Screenshots.png':{src:'preview.webp'}}}));
  ctx.Image=class {set src(value){this.url=value;images.push(this);}};
  run("Q={flagged:[{folder:'Far_0'},{folder:'Previous_1'},{folder:'Current_2'},{folder:'Next_3'},{folder:'Far_4'}]};tab='flagged';idx.flagged=2;render=()=>{};prefetch(Q.flagged,2)");
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(images.map(x=>x.url).sort(),['/media/Far_4/preview.webp','/media/Next_3/preview.webp','/media/Previous_1/preview.webp']);
  assert(images.every(x=>x.fetchPriority==='low'));
  assert.match(run("imageTag('Next_3','SpriteSheet_Screenshots.png','class=\"hero\"')"),/src="\/media\/Next_3\/preview.webp"/);
  run('prefetch(Q.flagged,2)');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(images.length,3);
  images[0].onerror();
  run('prefetch(Q.flagged,2)');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(images.length,4);
});

test('normal background refresh preserves in-flight media and its generation', async () => {
  const pending=deferred();let requests=0;
  const {run}=fixture(async url=>url==='/api/state'?response(emptyState):(requests++,pending.promise));
  run('S={mail:[]};Q={pending:[],passed:[],flagged:[],mail:[]}');
  const loading=run("manifest('Next_2')");
  const generation=run('mediaGeneration');
  await run('load({background:true})');
  assert.equal(run('mediaGeneration'),generation);
  pending.resolve(response({images:['SpriteSheet_Screenshots.png']}));
  await loading;
  assert.equal(run("manifests.get('Next_2').images.length"),1);
  assert.equal(requests,1);
});

test('newly drafted row invalidates only its own in-flight manifest', async () => {
  const old=deferred();let calls=0;
  const {run}=fixture(async url=>url==='/api/state'
    ?response({...emptyState,index:{1:'Game_1'},mail:[{appid:1}]})
    :++calls===1?old.promise:response({mail:'fresh',images:[]}));
  run("S={mail:[]};Q={pending:[],passed:[],flagged:[],mail:[]}");
  const loading=run("manifest('Game_1')");
  run("tab='mail'"); await run('load({background:true})');
  await new Promise(r=>setImmediate(r));
  old.resolve(response({mail:'stale',images:[]}));
  await loading;
  assert.equal(run("manifests.get('Game_1').mail"),'fresh');
});

test('outdated neighboring metadata cannot start a preload after navigating away', async () => {
  const delayed=deferred();let images=0;
  const {ctx,run}=fixture(async()=>delayed.promise);
  ctx.Image=class {constructor(){images++;}};
  run("Q={flagged:[{folder:'Current_1'},{folder:'Next_2'},{folder:'Other_3'},{folder:'Last_4'}]};tab='flagged';render=()=>{};prefetch(Q.flagged,0);Q={flagged:[{folder:'Unrelated_9'}]};idx.flagged=0");
  delayed.resolve(response({images:['SpriteSheet_Screenshots.png']}));
  await new Promise(resolve=>setImmediate(resolve));
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
  run("S={mail:[]};Q={pending:[],passed:[],flagged:[],mail:[]};document.querySelector('#view').innerHTML='Current card'");
  await run('load({background:true})');
  assert.equal(elements.get('#view').innerHTML,'Current card');
  assert.match(run('actionNotice'),/offline/);
});

test('card actions advance before confirmation, serialize writes, and hide pending cards on refresh', async () => {
  const first = deferred(), second = deferred();
  const state = { ...emptyState, index: {1:'A_1',2:'B_2',3:'C_3'}, passed:[1,2,3] };
  let writes = 0;
  const {run} = fixture(async url => url === '/api/act'
    ? (++writes === 1 ? first.promise : second.promise)
    : response(url === '/api/state' ? structuredClone(state) : {images:['SpriteSheet_Screenshots.png']}));
  await run('load()');
  run("tab='passed'");
  const saving1 = run("doAct('accept')");
  assert.deepEqual(Array.from(run('Q.passed.map(c=>c.appid)')), [2,3]);
  const saving2 = run("doAct('reject')");
  assert.deepEqual(Array.from(run('Q.passed.map(c=>c.appid)')), [3]);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes,1);
  await run('load({background:true})');
  assert.deepEqual(Array.from(run('Q.passed.map(c=>c.appid)')), [3]);
  state.passed=[2,3]; first.resolve(response({ok:true}));
  await saving1;
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes,2);
  state.passed=[3]; second.resolve(response({ok:true,deleted:[2]}));
  await saving2;
  assert.equal(run('pendingCards.size'),0);
  assert.equal(run('failed.length'),0);
  assert.deepEqual(Array.from(run('Q.passed.map(c=>c.appid)')), [3]);
});

test('failed card is restored and cannot receive a conflicting action before retry', async () => {
  const write = deferred();
  const state = {...emptyState,index:{1:'A_1',2:'B_2'},passed:[1,2]};
  let writes=0;
  const {run,elements}=fixture(async url=>url==='/api/act' ? (writes++,write.promise)
    : response(url==='/api/state'?state:{images:['SpriteSheet_Screenshots.png']}));
  await run('load()');
  run("tab='passed'");
  const saving=run("doAct('accept')");
  assert.deepEqual(Array.from(run('Q.passed.map(c=>c.appid)')), [2]);
  write.resolve({ok:false,status:503,json:async()=>({error:'offline'})});
  await saving;
  assert.deepEqual(Array.from(run('Q.passed.map(c=>c.appid)')), [1,2]);
  assert.equal(run('failed.length'),1);
  assert.match(elements.get('#bannerText').textContent,/offline/);
  await run("doAct('reject')");
  assert.equal(writes,1);
});

test('storage failure prevents optimistic advance and network write', async () => {
  const state={...emptyState,index:{1:'A_1'},passed:[1]};
  let writes=0;
  const {ctx,run}=fixture(async url=>{if(url==='/api/act') writes++; return response(url==='/api/state'?state:{});});
  await run('load()');
  run("tab='passed'");
  ctx.localStorage.setItem=()=>{throw new Error('quota');};
  await run("doAct('accept')");
  assert.equal(writes,0);
  assert.equal(run('Q.passed.length'),1);
  assert.equal(run('failed.length'),0);
});

test('workflow and bulk actions do not overtake queued card writes', async () => {
  let writes=0;
  const {run}=fixture(async()=>{writes++; return response({ok:true});});
  run('pendingCards.add(1)');
  await run("fireGha('send')");
  await run('approveAll()');
  await run('deleteAll()');
  await run('acceptAll()');
  await run('retryFailed()');
  assert.equal(writes,0);
});

test('cards show the release date next to the appid, or say there is none', async () => {
  for (const [releases, expected] of [[{ 1: 'Mar 26, 2027' }, /appid 1 · 📅 Mar 26, 2027 ·/], [{}, /appid 1 · 📅 No release date ·/]]) {
    const state = { ...emptyState, index: { 1: 'Game_1' }, pending: [1], releases };
    const { run, elements } = fixture(async url => response(url === '/api/state' ? state
      : { name: 'Game', desc: 'Description', images: ['SpriteSheet_Screenshots.png'] }));
    await run('load()');
    run("openTab('pending')");
    await new Promise(resolve => setImmediate(resolve));
    assert.match(elements.get('#view').innerHTML, expected);
  }
});

test('cards show the developer\'s Steam game count right after the developer name', async () => {
  for (const [devGames, meta, expected] of [
    [{ 1: 3 }, 'Red <Corn>  ·  Action, Indie', /Red &lt;Corn&gt;<span class="devcount"[^>]*>🎮 3 games<\/span> · Action, Indie · appid 1/],
    [{ 1: 1 }, 'Solo', /Solo<span class="devcount"[^>]*>🎮 1 game<\/span> · appid 1/],
    [{}, 'Studio  ·  Action', /Studio  ·  Action · appid 1/]]) {
    const state = { ...emptyState, index: { 1: 'Game_1' }, pending: [1], devGames };
    const { run, elements } = fixture(async url => response(url === '/api/state' ? state
      : { name: 'Game', desc: 'Description', meta, images: ['SpriteSheet_Screenshots.png'] }));
    await run('load()');
    run("openTab('pending')");
    await new Promise(resolve => setImmediate(resolve));
    assert.match(elements.get('#view').innerHTML, expected);
  }
});
