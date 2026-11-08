import test from 'node:test';
import assert from 'node:assert/strict';
import { todaySummary } from '../src/dashboard.ts';

const report = {
  draft: { day: '2026-11-06', before: { cable: '5', clamp: '3' }, lines: [{ position_id: 'cable', quantity: '25' }] },
  catalog: [{ id: 'cable', name: 'Кабель', unit: 'м', unit_pay: '2' }, { id: 'clamp', name: 'Хомут', unit: 'шт', unit_pay: null }],
};

test('daily summary preserves server quantities and units independently of the pending draft', () => {
  const actual = {
    day: '2026-11-06',
    lines: [
      { position_id: 'cable', name: 'Кабель', quantity: '5', unit: 'м', pay: '10' },
      { position_id: 'clamp', name: 'Хомут', quantity: '3', unit: 'шт', pay: null },
    ],
    total_pay: null,
  };
  assert.equal(todaySummary({ ...report, today: actual }), actual);
});

test('missing daily summary is unavailable and never inferred from a draft or catalog', () => {
  assert.equal(todaySummary(report), null);
});

test('a server-confirmed empty day remains a valid empty summary', () => {
  const actual = { day: '2026-11-06', lines: [], total_pay: '0' };
  assert.equal(todaySummary({ ...report, today: actual }), actual);
});
