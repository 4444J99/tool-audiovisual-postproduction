import test from 'node:test';
import assert from 'node:assert/strict';
import {CANVAS, LAYOUT_NAMES, bands, anchor, textRect, silhouette, measureLayout, compareOrientations} from '../../web/fx/layout-geometry.mjs';

// Frozen copy of the arithmetic that lived inline in tools/render-layout-comparison.mjs before extraction.
function legacy(layout, r, portrait, t, duration) {
  const W = portrait ? 432 : 768, phase = t / duration;
  let x, y, w, h, alpha = 1, mirror = false, ghosts = [];
  if (portrait) { x = r % 2 ? W * .51 : 0; y = r < 2 ? 50 : r < 4 ? 520 : 145; w = W * .49; h = 220; }
  else { x = r < 2 ? 0 : r < 4 ? W * .75 : W * .4; y = r % 2 ? 220 : 40; w = W * .25; h = 190; }
  if (layout === 0) { x = portrait ? (r % 2) * W * .52 : r * W / 5; y = portrait ? (r < 2 ? 50 : 530) : r % 2 ? 250 : 35; }
  if (layout === 2) { x += Math.sin(t * 2 + r) * 25; y += Math.cos(t * 2 + r) * 12; }
  if (layout === 3) { if (r % 2) { mirror = true; } }
  if (layout === 4) { w *= r === 4 ? 1.4 : .8; h *= r === 4 ? 1.4 : .8; }
  if (layout === 5) { x += Math.sin(phase * Math.PI * 2 + r) * 30; alpha = r === Math.floor(t) % 5 ? 1 : .35; }
  if (layout === 6) for (let j = 1; j < 4; j++) ghosts.push({x: x + j * 12, y: y + j * 6, w, h, alpha: .6});
  const drawn = layout !== 7 || Math.floor(t * 1.5) % 5 !== r;
  return {x, y, w, h, alpha, mirror, ghosts, drawn};
}

test('extracted placement equals the former inline arithmetic for every layout, region, orientation and time', () => {
  let checked = 0;
  for (let layout = 0; layout < 8; layout++) for (let r = 0; r < 5; r++) for (const portrait of [false, true]) for (let i = 0; i < 40; i++) {
    const t = i * 12.167 / 40;
    assert.deepEqual(silhouette(layout, r, portrait, t, 12.167), legacy(layout, r, portrait, t, 12.167), `${layout}/${r}/${portrait}/${t}`);
    checked++;
  }
  assert.equal(checked, 3200);
});

test('fixed rectangles keep their original values', () => {
  assert.deepEqual(anchor(false), [205, 108, 358, 201]);
  assert.deepEqual(anchor(true), [22, 282, 388, 218]);
  assert.deepEqual(bands(false), [[0, 35, 195, 397], [573, 35, 195, 397], [205, 325, 358, 107]]);
  assert.deepEqual(bands(true), [[0, 45, 432, 228], [0, 510, 432, 230]]);
  assert.deepEqual(textRect(false), [190, 320, 388, 112]);
  assert.deepEqual(textRect(true), [0, 525, 432, 200]);
  assert.equal(LAYOUT_NAMES.length, 8);
});

test('invalid requests are rejected', () => {
  assert.throws(() => silhouette(8, 0, false, 0, 12), RangeError);
  assert.throws(() => silhouette(0, 5, false, 0, 12), RangeError);
  assert.throws(() => silhouette(0, 0, false, NaN, 12), RangeError);
  assert.throws(() => silhouette(0, 0, false, 0, 0), RangeError);
  assert.throws(() => measureLayout(0, false, {samples: 0}), RangeError);
});

test('measurements are deterministic, bounded and reflect the vacancy layout', () => {
  const a = compareOrientations({samples: 12}), b = compareOrientations({samples: 12});
  assert.deepEqual(a, b);
  assert.equal(a.length, 8);
  for (const row of a) for (const o of ['horizontal', 'vertical']) {
    const m = row[o];
    assert.ok(m.anchorShare > 0 && m.anchorShare < 1);
    assert.ok(m.silhouetteShare.mean >= m.visibleShare.mean);
    assert.ok(m.hiddenByAnchorFraction.mean >= 0 && m.hiddenByAnchorFraction.mean <= 1);
    assert.ok(m.offCanvasFraction.mean >= 0 && m.offCanvasFraction.mean <= 1);
  }
  const vacancy = a[7].horizontal;
  assert.equal(vacancy.active.min, 4, 'one region is vacated at every instant');
  assert.equal(a[0].horizontal.active.min, 5);
  assert.equal(a[0].horizontal.canvas[0], CANVAS.landscape.width);
});

test('grid resolution does not change headline shares beyond rounding of cell edges', () => {
  const coarse = measureLayout(1, false, {cell: 4, samples: 6}), fine = measureLayout(1, false, {cell: 1, samples: 6});
  assert.ok(Math.abs(coarse.silhouetteShare.mean - fine.silhouetteShare.mean) < 0.02);
});
