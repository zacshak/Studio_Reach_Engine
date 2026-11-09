// Backend for the MCP workflow tools. Every write uses the same guarded SQL as the
// dashboard so an agent cannot move a row from a state the dashboard would refuse.

const BATCH = 100;
export const WORKFLOWS = {
  send: 'send.yml', draft: 'draft.yml', discover: 'discover.yml',
  hermes: 'hermes.yml', kimchi: 'kimchi.yml', epic: 'epic-discovery.yml',
};

export function batchIds(body, max = BATCH) {
  if (!Array.isArray(body?.appids) || !body.appids.length || body.appids.length > max)
    throw new Error(`appids must be an array of 1-${max} values`);
  return [...new Set(body.appids.map(value => {
    value = Number(value);
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error('invalid appid');
    return value;
  }))];
}

// Every review queue as appid lists, without media indexes or recommendation text.
export async function queues(env, sql) {
  const [approval, nomail, triage, writing, drafted, scheduled, sending] = await sql(env, [
    ["SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' AND scrape_status IN ('seeded','scraped') AND coalesce(triage_decision,'')<>'flag' ORDER BY appid"],
    ["SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' AND scrape_status IN ('pending','no_email','failed') AND coalesce(triage_decision,'')<>'flag' ORDER BY appid"],
    ["SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' AND triage_kept=0 AND triage_decision='flag' ORDER BY appid"],
    ["SELECT appid FROM scrape_tracker WHERE Mail_status='Writing' ORDER BY appid"],
    ["SELECT appid FROM scrape_tracker WHERE Mail_status='Drafted' ORDER BY appid"],
    ["SELECT appid FROM scrape_tracker WHERE Mail_status='Scheduled' ORDER BY appid"],
    ["SELECT appid FROM scrape_tracker WHERE Mail_status='Sending' ORDER BY appid"],
  ]);
  const ids = rows => rows.map(r => Number(r[0]));
  const out = { approval: ids(approval), nomail: ids(nomail), triage: ids(triage), writing: ids(writing),
    mail_approval: ids(drafted), scheduled: ids(scheduled), sending: ids(sending) };
  const [untriaged] = await sql(env, [["SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' AND triage_decision IS NULL ORDER BY appid"]]);
  out.untriaged = ids(untriaged);
  out.counts = Object.fromEntries(Object.entries(out).map(([k, v]) => [k, v.length]));
  return out;
}

// Pending -> Writing for a batch. Same media checks as the dashboard Accept, and a
// flagged game must be kept (or re-triaged as pass) before it can be accepted.
export async function acceptMany(env, body, sql, getJSON) {
  const appids = batchIds(body);
  const index = await getJSON(env, 'index.json') || {};
  const results = [];
  for (const appid of appids) {
    try {
      const folder = index[String(appid)];
      const manifest = folder ? await getJSON(env, `${folder}/manifest.json`) : null;
      if (!Array.isArray(manifest?.images) || !manifest.images.includes('SpriteSheet_Screenshots.png'))
        throw new Error('screenshot export is incomplete');
      const sprite = await env.MEDIA.head(`${folder}/SpriteSheet_Screenshots.png`);
      if (!sprite || sprite.size <= 0) throw new Error('sprite sheet is missing or empty');
      const [changed] = await sql(env, [["UPDATE scrape_tracker SET Mail_status='Writing' WHERE appid=? AND Mail_status='Pending' AND scrape_status IN ('seeded','scraped') AND coalesce(triage_decision,'')<>'flag' RETURNING appid", appid]]);
      results.push(changed.length ? { appid, ok: true }
        : { appid, ok: false, error: 'not Pending with an email, or flagged in triage' });
    } catch (error) { results.push({ appid, ok: false, error: error.message }); }
  }
  return { accepted: results.filter(r => r.ok).map(r => r.appid), failed: results.filter(r => !r.ok) };
}

// Drafted -> Scheduled; returns the rows that actually moved.
export async function approveMany(env, body, sql) {
  const appids = batchIds(body);
  const [rows] = await sql(env, [["UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid IN (SELECT value FROM json_each(?)) AND Mail_status='Drafted' AND scrape_status IN ('seeded','scraped') RETURNING appid", JSON.stringify(appids)]]);
  const moved = new Set(rows.map(r => Number(r[0])));
  return { approved: appids.filter(a => moved.has(a)), not_drafted: appids.filter(a => !moved.has(a)) };
}

// Drafted -> Writing: discard the saved draft so it can be drafted again.
// The manifest draft is cleared first; if the row then fails to move, the sender would
// find no draft and requeue it to Writing anyway, so no mail can go out unreviewed.
export async function returnToWriting(env, body, sql, getJSON) {
  const appids = batchIds(body);
  const index = await getJSON(env, 'index.json') || {};
  const results = [];
  for (const appid of appids) {
    try {
      const [status] = await sql(env, [["SELECT Mail_status FROM scrape_tracker WHERE appid=?", appid]]);
      if (status[0]?.[0] !== 'Drafted') throw new Error('only Drafted rows can be returned to Writing');
      const folder = index[String(appid)];
      if (!folder) throw new Error('game has no indexed media');
      const key = `${folder}/manifest.json`;
      const object = await env.MEDIA.get(key);
      if (!object) throw new Error('manifest is missing');
      const manifest = await object.json();
      const { mail: _mail, draft_submission: _draft, ...rest } = manifest;
      const written = await env.MEDIA.put(key, JSON.stringify(rest), {
        onlyIf: { etagMatches: object.etag }, httpMetadata: { contentType: 'application/json' } });
      if (!written) throw new Error('manifest changed concurrently; retry');
      const [changed] = await sql(env, [["UPDATE scrape_tracker SET Mail_status='Writing' WHERE appid=? AND Mail_status='Drafted' RETURNING appid", appid]]);
      results.push(changed.length ? { appid, ok: true } : { appid, ok: false, error: 'row changed before it could move' });
    } catch (error) { results.push({ appid, ok: false, error: error.message }); }
  }
  return { returned: results.filter(r => r.ok).map(r => r.appid), failed: results.filter(r => !r.ok) };
}

const EMAIL = /^[^\s@,<>"()[\]\\;:]+@[^\s@,<>"()[\]\\;:]+\.[^\s@,<>"()[\]\\;:.]+$/;
// Same contract as pipeline.write_result: only Pending rows still awaiting an email.
export async function saveEmail(env, body, sql) {
  const appid = Number(body?.appid);
  if (!Number.isSafeInteger(appid) || appid <= 0) throw new Error('invalid appid');
  const status = body?.status;
  if (!['scraped', 'no_email', 'failed'].includes(status)) throw new Error('status must be scraped, no_email or failed');
  let emails = null;
  if (status === 'scraped') {
    const list = Array.isArray(body.emails) ? body.emails.map(e => String(e).trim()).filter(Boolean) : [];
    if (!list.length || list.length > 5) throw new Error('scraped needs 1-5 emails, best first');
    for (const email of list) if (email.length > 254 || !EMAIL.test(email)) throw new Error(`invalid email: ${email}`);
    emails = list.join(', ');
  } else if (body.emails?.length) throw new Error('emails are only accepted with status scraped');
  let website = null;
  if (body.website != null && body.website !== '') {
    const url = new URL(String(body.website));
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('website must be http(s)');
    website = url.href;
  }
  // The HTTP SQL client sends every non-number as text, so optional values are
  // added as SET clauses only when present rather than passed as NULL.
  const sets = ['scrape_status=?'], args = [status];
  if (emails) { sets.push('emails=?'); args.push(emails); }
  if (website) { sets.push('website=?'); args.push(website); }
  const [rows] = await sql(env, [[`UPDATE scrape_tracker SET ${sets.join(', ')}
    WHERE appid=? AND Mail_status='Pending' AND scrape_status IN ('pending','failed','no_email')
    RETURNING appid, scrape_status, emails`, ...args, appid]]);
  if (!rows.length) throw new Error('lead is missing, not Pending, or already has an email');
  return { appid, scrape_status: rows[0][1], emails: rows[0][2] ?? null };
}

export async function triageDetails(env, body, sql) {
  const appids = batchIds(body);
  const [rows] = await sql(env, [[`SELECT s.appid, s.Mail_status, s.triage_decision, s.triage_reason, c.text, s.triaged_at
    FROM scrape_tracker s LEFT JOIN triage_criteria c ON c.id=s.triage_criteria_id
    WHERE s.appid IN (SELECT value FROM json_each(?)) ORDER BY s.appid`, JSON.stringify(appids)]]);
  const found = new Set(rows.map(r => Number(r[0])));
  // The HTTP SQL client yields undefined for NULL cells; report them as null.
  return { games: rows.map(([appid, mail_status, decision, reason, criteria, triaged_at]) =>
    ({ appid: Number(appid), mail_status: mail_status ?? null, decision: decision ?? null, reason: reason ?? null,
      criteria: criteria ?? null, triaged_at: triaged_at ?? null })),
    missing: appids.filter(a => !found.has(a)) };
}

export async function workflowRuns(env, body) {
  const file = WORKFLOWS[body?.workflow];
  if (!file) throw new Error(`workflow must be one of ${Object.keys(WORKFLOWS).join(', ')}`);
  if (!env.GH_REPO || !env.GH_PAT) throw new Error('GitHub access is not configured');
  const limit = Math.min(Math.max(Number(body.limit) || 5, 1), 20);
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${file}/runs?per_page=${limit}`, {
    headers: { Authorization: `Bearer ${env.GH_PAT}`, Accept: 'application/vnd.github+json', 'User-Agent': 'sre-review-worker' },
    signal: AbortSignal.timeout(20000) });
  if (!r.ok) throw new Error(`github ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const { workflow_runs = [] } = await r.json();
  return { workflow: body.workflow, runs: workflow_runs.map(run => ({ id: run.id, status: run.status,
    conclusion: run.conclusion, event: run.event, branch: run.head_branch, created_at: run.created_at,
    updated_at: run.updated_at, url: run.html_url })) };
}

// Triage Keep for a batch (same SQL as the dashboard Keep); reports rows that changed.
export async function keepMany(env, body, sql) {
  const appids = batchIds(body);
  const [rows] = await sql(env, [["UPDATE scrape_tracker SET triage_decision='kept', triage_kept=1, triaged_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE appid IN (SELECT value FROM json_each(?)) AND Mail_status='Pending' RETURNING appid", JSON.stringify(appids)]]);
  const kept = new Set(rows.map(r => Number(r[0])));
  return { kept: appids.filter(a => kept.has(a)), not_pending: appids.filter(a => !kept.has(a)) };
}
