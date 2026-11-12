import test from 'node:test';
import assert from 'node:assert/strict';
import worker from './worker.js';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';

test('public MCP drafting forwards only scoped payloads and returns image content',async()=>{
  const calls=[];
  const env={PUBLIC_ACCESS:'true',TURSO_URL:'https://test.invalid',TURSO_READ_TOKEN:'synthetic',REVIEW:{fetch:async request=>{
    calls.push([new URL(request.url).pathname,await request.json()]);
    return Response.json(request.url.endsWith('/context') ? {content:[{type:'text',text:'{"revision":"test"}'},
      {type:'image',data:'iVBORw0KGgo=',mimeType:'image/png'}]} : {ok:true,mail_status:'Drafted'});
  }}};
  const client=new Client({name:'draft-test',version:'1'});
  await client.connect(new StreamableHTTPClientTransport(new URL('https://mcp.test/mcp'),{
    fetch:(input,init)=>worker.fetch(new Request(input,init),env)}));
  try {
    const context=await client.callTool({name:'get_draft_context',arguments:{appid:1}});
    assert.equal(context.content[1].type,'image');
    const args={appid:1,revision:'a'.repeat(64),placeholder_values:{observation_1:'Visible lighting'}};
    const saved=await client.callTool({name:'save_draft',arguments:args});
    assert.equal(JSON.parse(saved.content[0].text).mail_status,'Drafted');
    assert.deepEqual(calls,[['/api/draft/context',{appid:1}],['/api/draft/save',args]]);
    assert.equal((await client.callTool({name:'save_draft',arguments:{...args,revision:'bad'}})).isError,true);
    assert.equal(calls.length,2);
  } finally {await client.close();}
});

test('list_draftable_games forwards count and draft_mails prompt carries the procedure',async()=>{
  const calls=[];
  const env={PUBLIC_ACCESS:'true',TURSO_URL:'https://test.invalid',TURSO_READ_TOKEN:'synthetic',REVIEW:{fetch:async request=>{
    calls.push([new URL(request.url).pathname,await request.json()]);
    return Response.json({games:[{appid:1,name:'Game'}],not_ready:[],accepted_total:1});
  }}};
  const client=new Client({name:'draft-list-test',version:'1'});
  await client.connect(new StreamableHTTPClientTransport(new URL('https://mcp.test/mcp'),{
    fetch:(input,init)=>worker.fetch(new Request(input,init),env)}));
  try {
    const listed=await client.callTool({name:'list_draftable_games',arguments:{count:20}});
    assert.equal(JSON.parse(listed.content[0].text).games[0].appid,1);
    assert.deepEqual(calls,[['/api/draft/list',{count:20}]]);
    assert.equal((await client.callTool({name:'list_draftable_games',arguments:{count:26}})).isError,true);
    const {prompts}=await client.listPrompts();
    assert.deepEqual(prompts.map(p=>p.name),['draft_mails','triage_games']);
    const prompt=await client.getPrompt({name:'draft_mails',arguments:{count:'20'}});
    const text=prompt.messages[0].content.text;
    assert.match(text,/Draft cold mails for 20 Accepted games/);
    assert.match(text,/list_draftable_games with count 20/);
    assert.match(text,/Do not approve or send anything/);
  } finally {await client.close();}
});
