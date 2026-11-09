import test from 'node:test';
import assert from 'node:assert/strict';
import { triageSnapshot, validateTriage } from './triage.js';

test('flag rows enter Triage, pass rows stay in their queue, both keep their reason', () => {
  const result = triageSnapshot([[1, 'pass', 'matches', 'rubric', 't1'], ['2', 'flag', 'too small', null, null]]);
  assert.deepEqual(result.triage, [2]);
  assert.deepEqual(result.triageReviews[1], { decision: 'pass', reason: 'matches', criteria: 'rubric', reviewed_at: 't1' });
  assert.deepEqual(result.triageReviews[2], { decision: 'flag', reason: 'too small', criteria: '', reviewed_at: null });
});

test('no rows means no triage and no reviews', () => {
  assert.deepEqual(triageSnapshot([]), { triage: [], triageReviews: {} });
  assert.deepEqual(triageSnapshot(undefined), { triage: [], triageReviews: {} });
});

test('validation trims text and refuses kept, duplicates and oversize batches', () => {
  assert.deepEqual(validateTriage({ criteria: ' c ', results: [{ appid: '5', decision: 'pass', reason: ' r ' }] }),
    { criteria: 'c', results: [{ appid: 5, decision: 'pass', reason: 'r' }] });
  assert.throws(() => validateTriage({ criteria: 'c', results: [{ appid: 1, decision: 'kept', reason: 'r' }] }), /flag or pass/);
  assert.throws(() => validateTriage({ criteria: 'x'.repeat(2001), results: [{ appid: 1, decision: 'pass', reason: 'r' }] }), /criteria/);
  assert.throws(() => validateTriage({ criteria: 'c', results: Array.from({ length: 101 }, (_, i) => ({ appid: i + 1, decision: 'pass', reason: 'r' })) }), /1-100/);
});
