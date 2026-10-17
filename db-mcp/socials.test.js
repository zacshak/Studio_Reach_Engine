import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {registerSocialTools} from './socials.js';
import {saveSocials} from '../webapp/socials.js';

test('MCP social tools paginate requests, save, clear the flag, and read completed JSON',async t=>{
  const db=new DatabaseSync(':memory:');t.after(()=>db.close());
  db.exec("CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY,game_name TEXT,short_descript TEXT,steam_url TEXT,website TEXT,developers TEXT,publishers TEXT,Require_Socials INTEGER,Socials_Request_ID TEXT,Socials_Data TEXT,Mail_status TEXT)");
  for(let id=1;id<=3;id++)db.prepare('INSERT INTO scrape_tracker VALUES(?,?,?,?,?,?,?,?,?,?,?)').run(id,'Synthetic','desc','https://example.invalid','https://example.invalid','Studio','Studio',1,String(id).repeat(32),'{}','Sent');
  const read=async(_,query,params=[])=>{const rows=db.prepare(query).all(...params);return {columns:rows.length?Object.keys(rows[0]):[],rows:rows.map(Object.values)};};
  const write=async(env,stmts)=>Promise.all(stmts.map(async([q,...args])=>(await read(env,q,args)).rows));
  const handlers={};
  const fakeServer={registerTool:(name,definition,handler)=>{handlers[name]=handler;}};
  registerSocialTools(fakeServer,{REVIEW:{fetch:async request=>{
    try{return Response.json(await saveSocials({},await request.json(),write));}
    catch(e){return Response.json({error:e.message},{status:409});}
  }}},read);
  const parse=r=>{assert.ok(!r.isError,JSON.stringify(r));return JSON.parse(r.content[0].text);};
  const first=parse(await handlers.get_social_requests({after_appid:0,page_size:2}));
  assert.deepEqual(first.games.map(r=>r.appid),[1,2]);assert.equal(first.has_more,true);
  const second=parse(await handlers.get_social_requests({after_appid:first.next_after_appid,page_size:2}));
  assert.deepEqual(second.games.map(r=>r.appid),[3]);assert.equal(second.has_more,false);
  const socials={X:null,LinkedIn:null,Instagram:null,Discord:null,Website:'https://example.invalid'};
  const payload={appid:1,request_id:'1'.repeat(32),socials};
  parse(await handlers.save_social_data(payload));
  assert.deepEqual(parse(await handlers.get_social_data({appid:1})).socials,socials);
  assert.equal(parse(await handlers.get_social_data({appid:1})).require_socials,false);
  assert.equal(db.prepare('SELECT Mail_status FROM scrape_tracker WHERE appid=1').get().Mail_status,'Sent');
  assert.equal(parse(await handlers.get_social_requests({after_appid:0,page_size:10})).games.length,2);
  parse(await handlers.save_social_data(payload));
  assert.equal((await handlers.save_social_data({...payload,socials:{...socials,Website:null}})).isError,true);
});
