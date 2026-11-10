// Triage recommendations live in scrape_tracker (triage_decision / triage_reason /
// triage_criteria_id / triaged_at) plus the shared triage_criteria table.
// Mail_status: Pending = not triaged yet; Triaged = has a pass/flag decision.
// A human Pass/Flag changes the decision and records the override in triage_reason.

// Rows still eligible for human review, with their current recommendation.
export const TRIAGE_SQL = `SELECT s.appid, s.triage_decision, s.triage_reason, c.text, s.triaged_at
  FROM scrape_tracker s LEFT JOIN triage_criteria c ON c.id = s.triage_criteria_id
  WHERE s.Mail_status='Triaged' AND s.triage_decision IN ('pass','flag')
  ORDER BY s.appid`;

const NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ','now')";

// Manual triage from the dashboard Pending view: Pending -> Triaged as pass.
export const USER_PASS_REASON = 'Passed by user';
export const TRIAGE_PASS_SQL = `UPDATE scrape_tracker SET Mail_status='Triaged', triage_decision='pass',
  triage_reason='${USER_PASS_REASON}', triage_criteria_id=NULL, triaged_at=${NOW_SQL}
  WHERE appid=? AND Mail_status='Pending' RETURNING appid`;

// The reason after a human changes a decision. The AI's original decision and reason
// are kept inside it; switching back to the AI's own decision restores its reason.
export function overrideReason(target, current, reason) {
  const word = target === 'pass' ? 'Passed' : 'Flagged';
  const text = reason || '';
  const wrapped = /^(?:Passed|Flagged|Kept) by user \(AI (passed|flagged): ([\s\S]*)\)$/.exec(text);
  const ai = wrapped ? { decision: wrapped[1] === 'passed' ? 'pass' : 'flag', reason: wrapped[2] }
    : /^(?:Passed|Flagged) by user$/.test(text) ? null : { decision: current, reason: text };
  if (!ai) return `${word} by user`;
  if (ai.decision === target) return ai.reason;
  return `${word} by user (AI ${ai.decision === 'pass' ? 'passed' : 'flagged'}: ${ai.reason})`;
}

// Human Pass (flag -> pass) or Flag (pass -> flag) for Triaged games. Each update is
// guarded on the decision and reason it was computed from, so a racing change wins.
export async function setDecision(env, appids, target, sql) {
  if (!['pass', 'flag'].includes(target)) throw new Error('decision must be pass or flag');
  const from = target === 'pass' ? 'flag' : 'pass';
  const [rows] = await sql(env, [[`SELECT appid, triage_reason FROM scrape_tracker
    WHERE Mail_status='Triaged' AND triage_decision=? AND appid IN (SELECT value FROM json_each(?))`, from, JSON.stringify(appids)]]);
  const statements = rows.map(([appid, reason]) => [`UPDATE scrape_tracker SET triage_decision=?, triage_reason=?,
    triaged_at=${NOW_SQL} WHERE appid=? AND Mail_status='Triaged' AND triage_decision=? AND coalesce(triage_reason,'')=?
    RETURNING appid`, target, overrideReason(target, from, reason ?? ''), Number(appid), from, reason ?? '']);
  const out = statements.length ? await sql(env, statements, true) : [];
  const changed = new Set(out.flat().map(row => Number(row[0])));
  return { changed: appids.filter(a => changed.has(a)), unchanged: appids.filter(a => !changed.has(a)) };
}

export function triageSnapshot(rows) {
  const triage = [], triageReviews = {};
  for (const [appid, decision, reason, criteria, reviewedAt] of rows || []) {
    const id = Number(appid);
    triageReviews[id] = { decision, reason: reason || '', criteria: criteria || '', reviewed_at: reviewedAt || null };
    if (decision === 'flag') triage.push(id);
  }
  return { triage, triageReviews };
}

export function validateTriage(body) {
  const criteria = typeof body?.criteria === 'string' ? body.criteria.trim() : '';
  if (!criteria || criteria.length > 2000) throw new Error('criteria must be 1-2000 characters');
  const results = body?.results;
  if (!Array.isArray(results) || !results.length || results.length > 100)
    throw new Error('results must contain 1-100 items');
  const seen = new Set();
  const clean = results.map(r => {
    const appid = Number(r?.appid);
    if (!Number.isSafeInteger(appid) || appid <= 0) throw new Error('invalid appid');
    if (seen.has(appid)) throw new Error('Each appid must appear only once per batch.');
    seen.add(appid);
    if (!['flag', 'pass'].includes(r.decision)) throw new Error('decision must be flag or pass');
    const reason = typeof r.reason === 'string' ? r.reason.trim() : '';
    if (!reason || reason.length > 1000) throw new Error('reason must be 1-1000 characters');
    return { appid, decision: r.decision, reason };
  });
  return { criteria, results: clean };
}

const NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')";
const CRITERIA_ID = '(SELECT id FROM triage_criteria WHERE text=?)';

// Saves recommendations in one transaction and moves each row Pending -> Triaged.
// A row must still be Pending (untriaged) and have indexed media (the dashboard needs
// it to review). An identical resubmission after an uncertain response is reported as
// saved and keeps its original timestamp; a Triaged row is never re-triaged.
export async function saveTriage(env, body, sql, getJSON) {
  const { criteria, results } = validateTriage(body);
  const index = await getJSON(env, 'index.json') || {};
  const skipped = [], candidates = [];
  for (const r of results)
    (index[String(r.appid)] ? candidates : skipped).push(r);
  const statements = [['INSERT INTO triage_criteria(text) VALUES (?) ON CONFLICT(text) DO NOTHING', criteria]];
  for (const r of candidates)
    statements.push([`UPDATE scrape_tracker SET Mail_status='Triaged', triage_decision=?, triage_reason=?,
      triage_criteria_id=${CRITERIA_ID}, triaged_at=CASE WHEN Mail_status='Triaged' THEN triaged_at ELSE ${NOW} END
      WHERE appid=? AND (Mail_status='Pending' OR (Mail_status='Triaged' AND triage_decision IS ?
        AND triage_reason IS ? AND triage_criteria_id IS ${CRITERIA_ID})) RETURNING appid`,
      r.decision, r.reason, criteria, r.appid, r.decision, r.reason, criteria]);
  const out = statements.length > 1 ? await sql(env, statements, true) : [];
  const updated = new Set(out.slice(1).flat().map(row => Number(row[0])));
  const saved = candidates.filter(r => updated.has(r.appid)).map(r => r.appid);
  for (const r of candidates) if (!updated.has(r.appid)) skipped.push(r);
  return { saved, skipped: skipped.map(r => ({ appid: r.appid,
    reason: 'Missing game/media, or already triaged (only Pending games can be triaged).' })) };
}
