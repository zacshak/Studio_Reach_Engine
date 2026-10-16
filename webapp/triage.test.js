import test from 'node:test';
import assert from 'node:assert/strict';
import { triageSnapshot } from './triage.js';

test('MCP pass clears legacy flag and flag enters review without affecting other leads', () => {
  const result = triageSnapshot([1, 2], { 1: { decision: 'pass', reason: 'matches criteria' }, 3: { decision: 'flag' } }, [1, 2, 3, 4]);
  assert.deepEqual(result.triage, [2, 3]);
  assert.equal(result.triageReviews[1].reason, 'matches criteria');
});

test('human-kept, deleted and advanced mail rows never reappear from stale recommendations', () => {
  assert.deepEqual(triageSnapshot([1, 2], { 1: { decision: 'flag' }, 2: { decision: 'pass' } }, []), { triage: [], triageReviews: {} });
  assert.deepEqual(triageSnapshot([1], null, [1]).triage, [1]);
});
