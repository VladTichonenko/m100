import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveDrawerGesture } from '../src/drawer-gestures.ts';

test('a tap or small movement leaves the drawer at its current size', () => {
  assert.equal(resolveDrawerGesture(0, 100, false), 'stay');
  assert.equal(resolveDrawerGesture(8, 10, false), 'stay');
  assert.equal(resolveDrawerGesture(-25, 500, true), 'stay');
});

test('upward drags and intentional flicks expand the drawer', () => {
  assert.equal(resolveDrawerGesture(-90, 600, false), 'expand');
  assert.equal(resolveDrawerGesture(-30, 40, false), 'expand');
});

test('downward drag first collapses full-screen and then closes the smaller drawer', () => {
  assert.equal(resolveDrawerGesture(90, 600, true), 'collapse');
  assert.equal(resolveDrawerGesture(90, 600, false), 'close');
  assert.equal(resolveDrawerGesture(30, 40, false), 'close');
});
