import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {queueSocialsStatement,saveSocials,validateSocials} from './socials.js';
import {migrateSocials} from './migrate-socials.mjs';
const result={X:'https://x.com/synthetic',LinkedIn:null,Instagram:null,Discord:null,Website:'https://example.invalid'};
function fixture(t){
  const db=new DatabaseSync(':memory:');t.after(()=>db.close());
  db.exec("CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY,Mail_status TEXT,Require_Socials INTEGER DEFAULT 0,Socials_Data TEXT DEFAULT '{}'); INSERT INTO scrape_tracker(appid,Mail_status) VALUES(1,'Scheduled'),(2,'Sent'),(3,'Drafted'),(4,'Pending')");
  const run=async(_,stmts)=>stmts.map(([q,...args])=>db.prepare(q).all(...args).map(r=>Object.values(r)));
  return {db,run};
}
test('migration preserves links, removes Email and never queues historical sends',async t=>{
  const {db,run}=fixture(t);
  db.prepare('UPDATE scrape_tracker SET Require_Socials=1,Socials_Data=? WHERE appid=2').run(JSON.stringify({...result,Email:'old@example.invalid'}));
  await migrateSocials({},run);
  const old=db.prepare('SELECT * FROM scrape_tracker WHERE appid=2').get();
  assert.equal(old.Require_Socials,0);assert.equal(old.Socials_Request_ID,null);
  assert.deepEqual(JSON.parse(old.Socials_Data),result);
  await run({},[queueSocialsStatement()]);
  const requested=db.prepare('SELECT Socials_Request_ID FROM scrape_tracker WHERE appid=1').get().Socials_Request_ID;
  await migrateSocials({},run);
  assert.equal(db.prepare('SELECT Require_Socials FROM scrape_tracker WHERE appid=1').get().Require_Socials,1);
  assert.equal(db.prepare('SELECT Socials_Request_ID FROM scrape_tracker WHERE appid=1').get().Socials_Request_ID,requested);
});
test('Send click queues Scheduled only and repeated clicks never requeue completed research',async t=>{
  const {db,run}=fixture(t);await migrateSocials({},run);
  assert.deepEqual((await run({},[queueSocialsStatement()]))[0],[[1]]);
  const request_id=db.prepare('SELECT Socials_Request_ID FROM scrape_tracker WHERE appid=1').get().Socials_Request_ID;
  assert.deepEqual((await run({},[queueSocialsStatement()]))[0],[]);
  await saveSocials({}, {appid:1,request_id,socials:result},run);
  assert.deepEqual((await run({},[queueSocialsStatement()]))[0],[]);
  assert.equal(db.prepare('SELECT Mail_status FROM scrape_tracker WHERE appid=1').get().Mail_status,'Scheduled');
  assert.equal((await saveSocials({}, {appid:1,request_id,socials:result},run)).ok,true);
  await assert.rejects(saveSocials({}, {appid:1,request_id,socials:{...result,X:null}},run),/completed/);
});
test('saving after mail delivery works; null results complete; deleted or stale requests cannot write',async t=>{
  const {db,run}=fixture(t);await migrateSocials({},run);await run({},[queueSocialsStatement()]);
  const request_id=db.prepare('SELECT Socials_Request_ID FROM scrape_tracker WHERE appid=1').get().Socials_Request_ID;
  db.exec("UPDATE scrape_tracker SET Mail_status='Sent' WHERE appid=1");
  const allNull=Object.fromEntries(Object.keys(result).map(k=>[k,null]));
  await saveSocials({}, {appid:1,request_id,socials:allNull},run);
  assert.equal(db.prepare('SELECT Require_Socials FROM scrape_tracker WHERE appid=1').get().Require_Socials,0);
  await assert.rejects(saveSocials({}, {appid:1,request_id:'f'.repeat(32),socials:allNull},run),/stale/);
  db.exec('DELETE FROM scrape_tracker WHERE appid=1');
  await assert.rejects(saveSocials({}, {appid:1,request_id,socials:allNull},run),/missing/);
});
test('social input forbids extra fields, fabricated platform domains, credentials and executable URLs',()=>{
  for(const data of [{...result,Email:null},{...result,X:'https://x.com.evil.invalid/user'},
    {...result,Website:'javascript:alert(1)'},{...result,Website:'https://u:p@example.com'},
    {...result,Discord:'not a URL'},{}])assert.throws(()=>validateSocials(data));
});
