// Automatic send: every AUTO_SEND_EVERY_MIN minutes the worker's cron checks whether
// approved mails (Mail_status='Scheduled') are waiting and a sending account has room left
// in its rolling 24h cap. If so, and no send run is active or started recently, it starts
// send.yml exactly like the dashboard's Send button. Only approved mails are ever sent.
// Set the AUTO_SEND variable to 'off' to disable.
import { dispatchWorkflow } from './agent-actions.js';
import { queueSocialsStatement } from './socials.js';

export const AUTO_SEND_EVERY_MIN = 10;
export const MIN_GAP_MINUTES = 30; // never start a run within 30 min of the previous one

// [scheduled mails, accounts with room]. "Room" mirrors mailer.py: sent in the last 24h
// plus mails it is sending right now, against its daily_cap. Only accounts whose last
// login check was ok count, so a broken account cannot keep starting empty runs.
export const AUTO_SEND_SQL = `SELECT
  (SELECT count(*) FROM scrape_tracker WHERE Mail_status='Scheduled'),
  (SELECT count(*) FROM sender_accounts a WHERE a.daily_cap > 0 AND a.last_status LIKE 'ok%'
     AND a.daily_cap > (SELECT count(*) FROM mail_send_log l WHERE l.sender=a.email AND l.sent_at >= datetime('now','-1 day'))
       + (SELECT count(*) FROM mail_send_attempt t JOIN scrape_tracker s ON s.appid=t.appid
          WHERE s.Mail_status='Sending' AND t.sender=a.email))`;

// The send button and the automatic send both go through here.
export async function startSend(env, sql, fetcher = fetch) {
  const [queued] = await sql(env, [queueSocialsStatement()]);
  console.log(JSON.stringify({ event: 'social_requests_queued', count: queued.length }));
  await dispatchWorkflow(env, 'send', fetcher);
}

async function latestSendRun(env, fetcher) {
  const r = await fetcher(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/send.yml/runs?per_page=1`, {
    headers: { Authorization: `Bearer ${env.GH_PAT}`, Accept: 'application/vnd.github+json', 'User-Agent': 'sre-review-worker' },
    signal: AbortSignal.timeout(20000) });
  if (!r.ok) throw new Error(`github ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()).workflow_runs?.[0] || null;
}

// Returns what it decided, for logs and tests.
export async function autoSend(env, sql, { now = Date.now(), fetcher = fetch } = {}) {
  if (env.AUTO_SEND === 'off') return 'disabled';
  if (new Date(now).getUTCMinutes() % AUTO_SEND_EVERY_MIN) return 'not this minute';
  const [[[scheduled, room]]] = await sql(env, [[AUTO_SEND_SQL]]);
  if (!Number(scheduled)) return 'nothing approved';
  if (!Number(room)) return 'all accounts at their 24h cap';
  const last = await latestSendRun(env, fetcher);
  if (last && last.status !== 'completed') return 'send run already active';
  if (last && now - Date.parse(last.created_at) < MIN_GAP_MINUTES * 60000) return 'last run too recent';
  await startSend(env, sql, fetcher);
  return 'started';
}
