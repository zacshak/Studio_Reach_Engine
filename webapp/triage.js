// Triage recommendations live in scrape_tracker (triage_decision / triage_reason /
// triage_criteria_id / triaged_at) plus the shared triage_criteria table.
// 'kept' is the human Keep and is never overwritten by an AI recommendation.

// Rows still eligible for human review, with their current recommendation.
export const TRIAGE_SQL = `SELECT s.appid, s.triage_decision, s.triage_reason, c.text, s.triaged_at
  FROM scrape_tracker s LEFT JOIN triage_criteria c ON c.id = s.triage_criteria_id
  WHERE s.Mail_status='Pending' AND s.triage_kept=0 AND s.triage_decision IN ('pass','flag')
  ORDER BY s.appid`;

// Same condition an AI recommendation must meet to be written.
export const TRIAGE_ELIGIBLE = "Mail_status='Pending' AND triage_kept=0 AND coalesce(triage_decision,'')<>'kept'";

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

// Saves recommendations for eligible rows in one transaction. A row must still be
// Pending, not human-kept, and have indexed media (the dashboard needs it to review).
// An identical resubmission keeps its original timestamp.
export async function saveTriage(env, body, sql, getJSON) {
  const { criteria, results } = validateTriage(body);
  const index = await getJSON(env, 'index.json') || {};
  const skipped = [], candidates = [];
  for (const r of results)
    (index[String(r.appid)] ? candidates : skipped).push(r);
  const statements = [['INSERT INTO triage_criteria(text) VALUES (?) ON CONFLICT(text) DO NOTHING', criteria]];
  for (const r of candidates)
    statements.push([`UPDATE scrape_tracker SET triage_decision=?, triage_reason=?, triage_criteria_id=${CRITERIA_ID},
      triaged_at=CASE WHEN triage_decision IS ? AND triage_reason IS ? AND triage_criteria_id IS ${CRITERIA_ID}
        AND triaged_at IS NOT NULL THEN triaged_at ELSE ${NOW} END
      WHERE appid=? AND ${TRIAGE_ELIGIBLE} RETURNING appid`,
      r.decision, r.reason, criteria, r.decision, r.reason, criteria, r.appid]);
  const out = statements.length > 1 ? await sql(env, statements, true) : [];
  const updated = new Set(out.slice(1).flat().map(row => Number(row[0])));
  const saved = candidates.filter(r => updated.has(r.appid)).map(r => r.appid);
  for (const r of candidates) if (!updated.has(r.appid)) skipped.push(r);
  return { saved, skipped: skipped.map(r => ({ appid: r.appid,
    reason: 'Missing game/media, human-kept, or mail status is no longer Pending.' })) };
}
