import test from 'node:test';
import assert from 'node:assert/strict';
import { cardFigures } from '../src/figures.ts';

const position = {
  id: 'cable', planned: '100', completed: '20', unit_pay: '2',
  planned_pay: '200', completed_pay: '40',
};
const report = { draft: { status: 'draft', mode: 'add', before: { cable: '5' } }, operation: null };
const lines = [{ position_id: 'cable', quantity: '10' }];

test('new report previews remaining volume and pay against cumulative completed volume', () => {
  assert.deepEqual(cardFigures(position, report, lines), {
    completed: 30, remaining: 70, plannedPay: 200, completedPay: 60,
  });
});

test('replacing the daily total subtracts the old daily value before adding the new one', () => {
  const replacement = { ...report, draft: { ...report.draft, mode: 'replace' } };
  assert.equal(cardFigures(position, replacement, [{ position_id: 'cable', quantity: '2,5' }]).completed, 17.5);
});

test('two cards for the same position share the correct cumulative remaining volume', () => {
  const duplicate = [...lines, { position_id: 'cable', quantity: '3' }];
  assert.equal(cardFigures(position, report, duplicate).remaining, 67);
});

test('account edits subtract only the account contribution from shared totals', () => {
  const account = { ...report, draft: { ...report.draft, mode: 'replace', before: { cable: '18' }, account_before: { cable: '10' } } };
  assert.equal(cardFigures(position, account, [{ position_id: 'cable', quantity: '7' }]).completed, 17);
  assert.equal(cardFigures(position, account, [{ position_id: 'cable', quantity: '12' }]).completed, 22);
});

test('saved and partially saved quantities are not added a second time', () => {
  for (const current of [
    { ...report, draft: { ...report.draft, status: 'done' } },
    { ...report, operation: { sheets_done: true } },
  ]) {
    assert.equal(cardFigures({ ...position, completed: '30' }, current, lines).completed, 30);
  }
});

test('a missing wage stays unknown and a zero wage remains a valid zero', () => {
  const missing = { ...position, unit_pay: null, planned_pay: null, completed_pay: null };
  assert.equal(cardFigures(missing, report, lines).completedPay, null);
  assert.equal(cardFigures(missing, report, lines).plannedPay, null);
  const zero = { ...missing, unit_pay: '0' };
  assert.equal(cardFigures(zero, report, lines).completedPay, 0);
});
