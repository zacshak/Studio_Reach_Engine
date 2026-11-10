import { z } from 'zod';

// Workflow tools. Writes go through the review Worker (REVIEW service binding), which
// applies the same guarded SQL as the dashboard; the MCP's own Turso token stays read-only.
const appid = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const appids = max => z.array(appid).min(1).max(max);
const text = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }] });
const READ = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false };
const WRITE = { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false };
const RUN = { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true };
const DESTROY = { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false };
const SEND = { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true };
const WORKFLOWS = ['discover', 'hermes', 'kimchi', 'draft', 'epic'];

export async function review(env, path, body) {
  if (!env.REVIEW) throw new Error('Review backend binding is not configured.');
  const response = await env.REVIEW.fetch(new Request(`https://review.internal${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}), signal: AbortSignal.timeout(60000) }));
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Review backend HTTP ${response.status}`);
  return value;
}

function splitSubject(mail) {
  const lines = mail.split(/\r?\n/);
  const first = lines.findIndex(line => line.trim());
  if (first >= 0 && /^subject/i.test(lines[first].trim()))
    return { subject: lines[first].split(':').slice(1).join(':').trim() || 'Hello', body: lines.slice(first + 1).join('\n').trim() };
  return { subject: null, body: mail.trim() };
}

export async function getDraft(env, id, run) {
  const { rows } = await run(env, 'SELECT Mail_status, emails, mail_template FROM scrape_tracker WHERE appid=?', [id]);
  if (!rows.length) throw new Error('Game does not exist in scrape_tracker.');
  const [mailStatus, emails, template] = rows[0];
  const index = await (await env.MEDIA.get('index.json'))?.json() || {};
  const folder = index[String(id)];
  if (!folder) throw new Error('Game has no indexed media, so no draft is stored.');
  const manifest = await (await env.MEDIA.get(`${folder}/manifest.json`))?.json();
  if (!manifest) throw new Error('Game media manifest is missing.');
  const mail = typeof manifest.mail === 'string' ? manifest.mail : '';
  return { appid: id, mail_status: mailStatus, recipient: (emails || '').split(',')[0].trim() || null,
    template_id: template ?? manifest.mail_template ?? null, has_draft: Boolean(mail.trim()),
    ...(mail.trim() ? splitSubject(mail) : {}) };
}

export async function r2Folder(env, { appid: id, folder }) {
  if ((id == null) === (folder == null)) throw new Error('Pass exactly one of appid or folder.');
  if (id != null) {
    const index = await (await env.MEDIA.get('index.json'))?.json() || {};
    folder = index[String(id)];
    if (!folder) return { appid: id, indexed: false, folder: null, objects: [], note: 'Not in index.json. Pass the old folder name to check leftover files.' };
  }
  if (/[\\/]/.test(folder) || !folder.trim()) throw new Error('folder must be a single folder name.');
  const objects = []; let cursor; let truncated = false;
  do {
    const page = await env.MEDIA.list({ prefix: `${folder}/`, cursor, limit: 1000 });
    objects.push(...page.objects.map(o => ({ key: o.key.slice(folder.length + 1), size: o.size })));
    cursor = page.truncated ? page.cursor : undefined;
    if (objects.length >= 2000) { truncated = Boolean(cursor); break; }
  } while (cursor);
  return { appid: id ?? null, indexed: id != null, folder, count: objects.length, truncated,
    bytes: objects.reduce((n, o) => n + o.size, 0), objects };
}

export function registerWorkflowTools(server, env, run) {
  const tool = (name, description, inputSchema, annotations, fn) => server.registerTool(name,
    { description, inputSchema, annotations }, async args => {
      try { return text(await fn(args)); }
      catch (error) { return { isError: true, content: [{ type: 'text', text: JSON.stringify({ error: error.message }) }] }; }
    });

  tool('get_queues', 'Every review queue as appid lists with counts: untriaged (Pending), approval (Triaged as pass, has email), nomail (Triaged as pass, no email), triage (Triaged as flag), accepted, mail_approval (Drafted), scheduled and sending.',
    {}, READ, () => review(env, '/api/queues'));
  tool('get_triage', 'Stored triage for up to 100 games: decision (pass/flag or null when untriaged), reason, full criteria text, timestamp and mail status.',
    { appids: appids(100) }, READ, ({ appids }) => review(env, '/api/triage/get', { appids }));
  tool('keep_games', 'Triage Keep for up to 100 flagged games: overrides the flag to pass (the reason records the override), moving them to the dashboard Passed view. Returns kept and not_flagged.',
    { appids: appids(100) }, WRITE, ({ appids }) => review(env, '/api/act', { action: 'keep_many', appids }));
  tool('accept_games', 'Accept up to 100 games for outreach (Triaged -> Accepted). Only games triaged as pass that have an email and a stored sprite sheet; untriaged, flagged and no-email games are refused. Returns accepted and failed with reasons.',
    { appids: appids(100) }, WRITE, ({ appids }) => review(env, '/api/act', { action: 'accept_many', appids }));
  tool('reject_games', 'Permanently delete up to 1000 games: database rows plus queued R2 media cleanup. Cannot be undone. Rows in Sending are protected. Requires confirm="DELETE".',
    { appids: appids(1000), confirm: z.literal('DELETE') }, DESTROY,
    ({ appids }) => review(env, '/api/act', { action: 'reject_all', appids }));
  tool('save_email_result', 'Record an email search for a Pending or Triaged game still awaiting an email. status=scraped needs 1-5 published addresses, best first (moves it from No-Mail to Approval); no_email or failed record the outcome. Never invent addresses.',
    { appid, status: z.enum(['scraped', 'no_email', 'failed']), emails: z.array(z.string().trim().min(3).max(254)).max(5).optional(),
      website: z.string().url().max(2000).optional() }, WRITE, args => review(env, '/api/email/save', args));
  tool('get_draft', 'Read the saved cold-mail draft for a game: subject, body, recipient, template and mail status.',
    { appid }, READ, ({ appid }) => getDraft(env, appid, run));
  tool('approve_mails', 'Approve up to 100 drafted mails (Drafted -> Scheduled). Approved mails are only sent when send_scheduled_mails or the dashboard Send button runs.',
    { appids: appids(100) }, WRITE, ({ appids }) => review(env, '/api/act', { action: 'approve_many', appids }));
  tool('return_to_accepted', 'Discard the saved draft of up to 100 Drafted games and move them back to Accepted so they can be drafted again.',
    { appids: appids(100) }, DESTROY, ({ appids }) => review(env, '/api/act', { action: 'return_to_accepted', appids }));
  tool('run_workflow', 'Start a GitHub Actions workflow on master: discover (Steam discovery), hermes or kimchi (email scraping), draft (AI drafting of Accepted rows), epic (Epic discovery). Not retried automatically; check get_workflow_runs before starting it again.',
    { workflow: z.enum(WORKFLOWS) }, RUN, ({ workflow }) => review(env, '/api/act', { action: 'trigger', workflow }));
  tool('get_workflow_runs', 'Recent GitHub Actions runs of a workflow with status and conclusion.',
    { workflow: z.enum([...WORKFLOWS, 'send']), limit: z.number().int().min(1).max(20).default(5) }, READ,
    args => review(env, '/api/workflows/runs', args));
  tool('send_scheduled_mails', 'SENDS REAL EMAIL. Starts send.yml, which emails every Scheduled game one at a time (2-4 minutes apart) and queues their social research. Requires confirm="SEND_REAL_EMAILS". Check get_workflow_runs first so a run is not started twice.',
    { confirm: z.literal('SEND_REAL_EMAILS') }, SEND, () => review(env, '/api/act', { action: 'trigger', workflow: 'send' }));
  tool('get_r2_folder', 'List the stored R2 objects of a game, by appid (current index) or by folder name (to check leftovers after deletion).',
    { appid: appid.optional(), folder: z.string().min(1).max(300).optional() }, READ, args => r2Folder(env, args));
}
