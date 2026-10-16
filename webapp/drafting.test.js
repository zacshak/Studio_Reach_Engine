import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {getDraftContext,saveDraft,render,draftRequest} from './drafting.js';

function fixture(t) {
  const db=new DatabaseSync(':memory:');t.after(()=>db.close());
  db.exec("CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY,game_name TEXT,short_descript TEXT,emails TEXT,scrape_status TEXT,Mail_status TEXT,mail_template INTEGER); INSERT INTO scrape_tracker VALUES(1,'Game','Description','studio@example.com','seeded','Writing',NULL)");
  let version=0;
  const objects=new Map();
  function put(key,value){objects.set(key,{bytes:typeof value==='string'?new TextEncoder().encode(value):value,etag:String(++version)});}
  put('index.json',JSON.stringify({'1':'Game_1'}));
  put('Game_1/manifest.json',JSON.stringify({name:'Game',desc:'Description',meta:'Studio · Action',images:['SpriteSheet_Screenshots.png']}));
  put('Game_1/SpriteSheet_Screenshots.png',new Uint8Array([137,80,78,71,13,10,26,10,0]));
  const template='Subject: <game>\n\nHi <developer/studio name>,\n<observation>\nC++, C# stay unchanged.';
  put('cold_mails.txt','#Cold Mail - 1\n'+template);
  const env={MEDIA:{head:async key=>objects.get(key)||null,get:async key=>{
    const o=objects.get(key);if(!o)return null;
    return {etag:o.etag,size:o.bytes.length,json:async()=>JSON.parse(new TextDecoder().decode(o.bytes)),
      text:async()=>new TextDecoder().decode(o.bytes),arrayBuffer:async()=>o.bytes.buffer};
  },put:async(key,value,options)=>{
    if(objects.get(key)?.etag!==options.onlyIf.etagMatches)return null;
    put(key,value);return {etag:objects.get(key).etag};
  }}};
  const sql=async(_,queries)=>queries.map(([q,...args])=>db.prepare(q).all(...args).map(r=>Object.values(r)));
  const get=()=>getDraftContext(env,1,sql);
  const args=async()=>({appid:1,revision:JSON.parse((await get()).content[0].text).revision,placeholder_values:{observation_1:'Visible lighting with C plus plus'}});
  return {db,env,sql,put,get,args,template,objects};
}

test('context returns actual image and slots; save preserves fixed text and only advances Writing',async t=>{
  const f=fixture(t), context=await f.get();
  assert.equal(context.content[1].type,'image');
  assert.deepEqual(JSON.parse(context.content[0].text).placeholders,{observation_1:'observation'});
  const args=await f.args();
  assert.equal((await saveDraft(f.env,args,f.sql)).mail_status,'Drafted');
  const value=await (await f.env.MEDIA.get('Game_1/manifest.json')).json();
  assert.equal(value.mail,'Subject: Game\n\nHi Studio,\nVisible lighting with C++\nC++, C# stay unchanged.');
  assert.equal(f.db.prepare('SELECT Mail_status FROM scrape_tracker').get().Mail_status,'Drafted');
  assert.equal((await saveDraft(f.env,args,f.sql)).ok,true);
});

test('template rendering rejects wrong keys and forbidden values',()=>{
  for(const value of [{},{extra:'bad'},{observation_1:''},{observation_1:'bad-value'},{observation_1:'<rewrite>'}])
    assert.throws(()=>render('Fixed <slot>','Game','Studio',value));
  assert.equal(render('Fixed <game>','Game','Studio',{}),'Fixed Game');
});

test('changed source, media or template invalidates a pending submission',async t=>{
  for(const change of [f=>f.db.exec("UPDATE scrape_tracker SET emails='other@example.com'"),
    f=>f.put('cold_mails.txt','#Cold Mail - 1\nChanged <observation>'),
    f=>f.put('Game_1/SpriteSheet_Screenshots.png',new Uint8Array([137,80,78,71,13,10,26,10,1]))]){
    const f=fixture(t),args=await f.args();change(f);
    await assert.rejects(saveDraft(f.env,args,f.sql),/Stale/);
    assert.equal(f.db.prepare('SELECT Mail_status FROM scrape_tracker').get().Mail_status,'Writing');
  }
});

test('partial save recovers identical payload after database failure without replacing text',async t=>{
  const f=fixture(t),args=await f.args();
  const fail=async(env,queries)=>{if(queries[0][0].startsWith('UPDATE'))throw Error('DB unavailable');return f.sql(env,queries);};
  await assert.rejects(saveDraft(f.env,args,fail),/DB unavailable/);
  assert.equal(JSON.parse((await f.get()).content[0].text).recovery,true);
  await assert.rejects(saveDraft(f.env,{...args,placeholder_values:{observation_1:'Different'}},f.sql),/already won/);
  assert.equal((await saveDraft(f.env,args,f.sql)).ok,true);
});

test('competing manifest save wins and cannot be overwritten',async t=>{
  const f=fixture(t),args=await f.args(),write=f.env.MEDIA.put;
  f.env.MEDIA.put=async(...params)=>{
    f.put('Game_1/manifest.json',JSON.stringify({mail:'GHA draft',mail_template:1}));
    return write(...params);
  };
  await assert.rejects(saveDraft(f.env,args,f.sql),/Concurrent/);
  assert.equal((await(await f.env.MEDIA.get('Game_1/manifest.json')).json()).mail,'GHA draft');
});

test('advanced and deleted rows cannot be drafted, missing images fail closed',async t=>{
  const f=fixture(t);
  f.db.exec("UPDATE scrape_tracker SET Mail_status='Scheduled'");
  await assert.rejects(f.get(),/Writing/);
  f.db.exec("UPDATE scrape_tracker SET Mail_status='Writing'");
  f.objects.delete('Game_1/SpriteSheet_Screenshots.png');
  await assert.rejects(f.get(),/missing/);
  f.db.exec('DELETE FROM scrape_tracker');
  await assert.rejects(f.get(),/no longer exists/);
});

test('oversized public requests fail before database access',async()=>{
  await assert.rejects(draftRequest(new Request('https://test',{method:'POST',body:'x'.repeat(65537)}),{},()=>{throw Error('db called');},'save'),/64 KiB/);
});

test('recipient change during publication never advances the changed row',async t=>{
  const f=fixture(t),args=await f.args(),write=f.env.MEDIA.put;
  f.env.MEDIA.put=async(...params)=>{
    const result=await write(...params);
    f.db.exec("UPDATE scrape_tracker SET emails='changed@example.com'");
    return result;
  };
  await assert.rejects(saveDraft(f.env,args,f.sql),/row changed/);
  assert.equal(f.db.prepare('SELECT Mail_status FROM scrape_tracker').get().Mail_status,'Writing');
});

test('template selection and image changes during publication do not advance the row',async t=>{
  for(const change of [f=>f.db.exec('UPDATE scrape_tracker SET mail_template=2'),
    f=>f.put('Game_1/SpriteSheet_Screenshots.png',new Uint8Array([137,80,78,71,13,10,26,10,5]))]) {
    const f=fixture(t),args=await f.args(),write=f.env.MEDIA.put;
    f.env.MEDIA.put=async(...params)=>{const result=await write(...params);change(f);return result;};
    await assert.rejects(saveDraft(f.env,args,f.sql),/changed/);
    assert.equal(f.db.prepare('SELECT Mail_status FROM scrape_tracker').get().Mail_status,'Writing');
  }
});
