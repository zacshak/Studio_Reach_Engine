import { z } from 'zod';

export function registerDraftTools(server,env) {
  const invoke = path => async args => {
    try {
      if (!env.REVIEW) throw new Error('Drafting backend binding is not configured.');
      const response=await env.REVIEW.fetch(new Request(`https://review.internal/api/draft/${path}`,{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args),
        signal:AbortSignal.timeout(30000)}));
      const value=await response.json();
      if(!response.ok) throw new Error(value.error||`Draft backend HTTP ${response.status}`);
      return path==='context' ? value : {content:[{type:'text',text:JSON.stringify(value)}]};
    } catch(error) {return {isError:true,content:[{type:'text',text:error.message}]};}
  };
  const appid=z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
  server.registerTool('get_draft_context',{
    description:'Get an eligible Writing game, exact mail template, observation slots, revision, and sprite image. Use this before drafting. Saved partial submissions return an exact recovery payload. Never invent visual details.',
    inputSchema:{appid},annotations:{readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false}
  },invoke('context'));
  server.registerTool('save_draft',{
    description:'Submit observation slot values from get_draft_context. Server preserves outside text, saves the draft and changes Writing to Drafted. Never sends email. Retry the identical payload after an uncertain response. Changed context or competing drafts are rejected.',
    inputSchema:{appid,revision:z.string().regex(/^[a-f0-9]{64}$/),
      placeholder_values:z.record(z.string().regex(/^observation_[1-9]\d*$/),z.string().min(1).max(4000))},
    annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true,openWorldHint:false}
  },invoke('save'));
}
