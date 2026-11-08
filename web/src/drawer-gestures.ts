export type DrawerGesture = 'expand' | 'collapse' | 'close' | 'stay';

export function resolveDrawerGesture(distance: number, elapsed: number, expanded: boolean): DrawerGesture {
  const velocity = distance / Math.max(elapsed, 1);
  const deliberate = Math.abs(distance) >= 64 || (Math.abs(distance) >= 16 && Math.abs(velocity) >= .5);
  if (!deliberate) return 'stay';
  if (distance < 0) return 'expand';
  return expanded ? 'collapse' : 'close';
}
