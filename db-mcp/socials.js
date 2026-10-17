import {z} from 'zod';
import {SOCIAL_FIELDS} from '../webapp/socials.js';
const display=raw=>{const data=JSON.parse(raw||'{}');return Object.fromEntries(SOCIAL_FIELDS.map(key=>[key,typeof data[key]==='string'?data[key]:null]));};
const reply=value=>({content:[{type:'text',text:JSON.stringify(value)}]});

export function registerSocialTools(server,env,run) {
  const wrap=fn=>async args=>{try{return reply(await fn(args));}catch(error){return {isError:true,content:[{type:'text',text:error.message}]};}};
  const appid=z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
  const read={readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false};
  server.registerTool('get_social_requests',{
    description:'List games queued by a future Send scheduled mails click. Research their official X, LinkedIn, Instagram, Discord, and company Website yourself. No built-in search or LLM call. Use next_after_appid until has_more=false. Sending success does not clear these requests.',
    inputSchema:{after_appid:z.number().int().min(0).default(0),page_size:z.number().int().min(1).max(500).default(100)},annotations:read
  },wrap(async({after_appid,page_size})=>{
    const result=await run(env,"SELECT appid,game_name,short_descript,steam_url,website,developers,publishers,Socials_Request_ID,Socials_Data FROM scrape_tracker WHERE Require_Socials=1 AND Socials_Request_ID IS NOT NULL AND appid>? ORDER BY appid LIMIT ?",[after_appid,page_size+1]);
    const games=result.rows.slice(0,page_size).map(r=>({appid:Number(r[0]),game_name:r[1],description:r[2],steam_url:r[3],website_hint:r[4],developers:r[5],publishers:r[6],request_id:r[7],socials:display(r[8])}));
    return {games,has_more:result.rows.length>page_size,next_after_appid:result.rows.length>page_size?games.at(-1).appid:null};
  }));
  server.registerTool('get_social_data',{
    description:'Read stored social results for any existing game, including completed requests, to present them to the user. Does not queue or change anything.',
    inputSchema:{appid},annotations:read
  },wrap(async({appid})=>{
    const result=await run(env,'SELECT game_name,Require_Socials,Socials_Request_ID,Socials_Data FROM scrape_tracker WHERE appid=?',[appid]);
    if(!result.rows.length)throw new Error('Game no longer exists.');
    const r=result.rows[0];return {appid,game_name:r[0],require_socials:Number(r[1])===1,request_id:r[2],socials:display(r[3])};
  }));
  server.registerTool('save_social_data',{
    description:'Save exactly five researched HTTP(S) links or nulls for an outstanding social request. Clears Require_Socials on success, even if all five are null. Never changes mail state or recipient. Retry identical payload after uncertain responses; competing different results cannot overwrite the winner. Do not invent links. Email is not a field.',
    inputSchema:{appid,request_id:z.string().regex(/^[a-f0-9]{32}$/),socials:z.object(Object.fromEntries(SOCIAL_FIELDS.map(key=>[key,z.string().max(2048).nullable()]))).strict()},
    annotations:{...read,readOnlyHint:false}
  },wrap(async args=>{
    if(!env.REVIEW)throw new Error('Review backend binding missing.');
    const r=await env.REVIEW.fetch(new Request('https://review.internal/api/socials/save',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args),signal:AbortSignal.timeout(30000)}));
    const result=await r.json();if(!r.ok)throw new Error(result.error||`Backend HTTP ${r.status}`);return result;
  }));
}
