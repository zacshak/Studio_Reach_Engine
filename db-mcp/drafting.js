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
  server.registerTool('list_draftable_games',{
    description:'Next Accepted games ready to draft now (up to count, max 25; call again for more). Games whose media is not ready are listed separately in not_ready. Returns the drafting procedure to follow. Read-only.',
    inputSchema:{count:z.number().int().min(1).max(25)},annotations:{readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false}
  },invoke('list'));
  server.registerTool('get_draft_context',{
    description:'Get an eligible Accepted game, exact mail template, observation slots, revision, and sprite image. Use this before drafting. Saved partial submissions return an exact recovery payload. Never invent visual details.',
    inputSchema:{appid},annotations:{readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false}
  },invoke('context'));
  server.registerTool('save_draft',{
    description:'Submit observation slot values from get_draft_context. Server preserves outside text, saves the draft and changes Accepted to Drafted. Never sends email. Retry the identical payload after an uncertain response. Changed context or competing drafts are rejected.',
    inputSchema:{appid,revision:z.string().regex(/^[a-f0-9]{64}$/),
      placeholder_values:z.record(z.string().regex(/^observation_[1-9]\d*$/),z.string().min(1).max(4000))},
    annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true,openWorldHint:false}
  },invoke('save'));
  server.registerPrompt('draft_mails',{
    title:'Draft cold mails',
    description:'Draft cold mails for N Accepted games with this agent (no pipeline AI key needed). Drafts land in Drafted for review; nothing is sent.',
    argsSchema:{count:z.string().regex(/^\d+$/).describe('How many games to draft, e.g. 20')}
  },({count})=>({messages:[{role:'user',content:{type:'text',text:draftMailsPrompt(Number(count))}}]}));
}

export function draftMailsPrompt(count) {
  return `Draft cold mails for ${count} Accepted games using the sre-database MCP tools.

1. Call list_draftable_games with count ${Math.min(count,25)} (max 25 per call; call again until you have ${count} games or it returns none).
2. For each game, one at a time:
   a. get_draft_context(appid). It returns the mail template, the observation slots to fill, a revision, and the screenshot sprite image.
   b. Look at the image and the description. Fill ONLY the observation keys, from what is actually visible or stated. Never invent mechanics or claim you watched a trailer. No hyphens or angle brackets in values.
   c. save_draft(appid, revision, placeholder_values). This saves the mail and moves the game to Drafted. It never sends mail.
   d. Skip the game (leave it Accepted) when: the images do not show enough to fill a slot honestly; the context or save returns an error that persists after fetching fresh context and retrying once.
   e. If get_draft_context returns recovery: true, call save_draft with exactly those values.
3. Stop the whole run if the mail templates are unavailable.
4. Do not approve or send anything.
5. Finish with a report: how many were drafted, and each skipped game with appid, name and reason (include not_ready games from list_draftable_games).`;
}
