import test from 'node:test';
import assert from 'node:assert/strict';
import { likelyPositions } from '../src/positions.ts';

const catalog = [
  { id: 'cable15', name: 'Кабель ВВГнг 3х1,5' },
  { id: 'cable25', name: 'Кабель ВВГнг 3х2,5' },
  { id: 'pipe', name: 'Труба ПВХ 20 мм' },
  { id: 'clamp', name: 'Хомут для дренажа' },
];

test('ambiguous work offers model candidates without unrelated catalog entries', () => {
  const line = { phrase: 'кабель 15 метров', position_id: null, candidates: ['cable25', 'missing', 'cable15', 'cable25'] };
  assert.deepEqual(likelyPositions(catalog, line).map(p => p.id), ['cable25', 'cable15']);
});

test('text fallback finds relevant work when model supplied no candidates', () => {
  const line = { phrase: 'кабель 15 метров', position_id: null };
  assert.deepEqual(new Set(likelyPositions(catalog, line).map(p => p.id)), new Set(['cable15', 'cable25']));
});

test('unknown work does not fill recommendations with arbitrary positions', () => {
  assert.deepEqual(likelyPositions(catalog, { phrase: 'непонятное сообщение', position_id: null }), []);
});

test('current manual choice remains available before original candidates', () => {
  const line = { phrase: 'кабель', position_id: null, candidates: ['cable25', 'cable15'] };
  assert.deepEqual(likelyPositions(catalog, line, 'pipe').map(p => p.id), ['pipe', 'cable25', 'cable15']);
});
