// Apply MCP recommendations only while a row is still eligible for human review.
export function triageSnapshot(legacy, recommendations, eligible) {
  const ids = new Set(eligible.map(Number));
  const flagged = new Set((legacy || []).map(Number).filter(id => ids.has(id)));
  const reviews = {};
  for (const [id, review] of Object.entries(recommendations || {})) {
    if (!ids.has(Number(id)) || !['flag', 'pass'].includes(review?.decision)) continue;
    reviews[id] = review;
    if (review.decision === 'flag') flagged.add(Number(id));
    else flagged.delete(Number(id));
  }
  return { triage: [...flagged].sort((a, b) => a - b), triageReviews: reviews };
}
