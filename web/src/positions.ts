import type { Line, Position } from './types';

const normalize = (text: string) => text.toLocaleLowerCase('ru-RU').replace(/ё/g, 'е').replace(/,/g, '.');
const ignored = new Set(['монтаж', 'устано', 'прокла', 'работ', 'метр', 'сегодн', 'запиши', 'исправ', 'нужно']);

function terms(text: string): string[] {
  return [...new Set((normalize(text).match(/[а-яa-z]{3,}|\d+(?:\.\d+)?/g) || [])
    .map(word => /\d/.test(word) ? word : word.slice(0, 6))
    .filter(word => !ignored.has(word)))];
}

/** Prefer the model's actual matches; use text similarity only when it supplied none. */
export function likelyPositions(catalog: Position[], line: Line | null, currentId?: string | null): Position[] {
  if (!line) return [];
  const byId = new Map(catalog.map(position => [position.id, position]));
  const selected = currentId === undefined ? line.position_id : currentId;
  const ids = [...new Set([selected, ...(line.candidates || [])].filter((id): id is string => Boolean(id)))];
  const known = ids.map(id => byId.get(id)).filter((position): position is Position => Boolean(position));
  if ((line.candidates || []).some(id => byId.has(id))) return known.slice(0, 5);

  const current = byId.get(selected || '');
  const words = terms(`${line.phrase} ${current?.name || ''}`).filter(word => !/^\d/.test(word));
  const ranked = catalog.map(position => {
    const nameTerms = new Set(terms(position.name));
    const matches = words.filter(word => nameTerms.has(word)).length;
    return { position, score: matches };
  }).filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score || a.position.id.localeCompare(b.position.id));
  const best = ranked[0]?.score || 0;
  const similar = ranked.filter(item => item.score >= Math.max(1, best * .6)).map(item => item.position);
  return [...new Map([...known, ...similar].map(position => [position.id, position])).values()].slice(0, 5);
}
