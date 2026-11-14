/**
 * Placement geometry for the eight-layout horizontal/vertical comparison.
 *
 * Pure arithmetic, no canvas and no media: tools/render-layout-comparison.mjs draws from these
 * rectangles and tools/analyze-layout-geometry.mjs measures them.  Rectangles are in output pixels
 * of the two fixed comparison canvases.  A silhouette is one of five source regions cut by its
 * workprint matte; the complete reference picture (the anchor) is always drawn last.
 */
export const CANVAS = Object.freeze({landscape: Object.freeze({width: 768, height: 432}), portrait: Object.freeze({width: 432, height: 768})});
export const LAYOUT_NAMES = Object.freeze(['unfolded-room', 'five-territories', 'sender-channel-receiver', 'opposing-angle-duplex', 'face-body-world', 'travelling-attention', 'temporal-accumulation', 'architecture-of-vacancy']);
export const REGION_COUNT = 5;

const dimensions = portrait => CANVAS[portrait ? 'portrait' : 'landscape'];

/** Backdrop bands that show the processed picture over the dimmed room plate: [x, y, w, h]. */
export function bands(portrait) {
  const {width: W, height: H} = dimensions(portrait);
  return portrait ? [[0, 45, W, 228], [0, 510, W, 230]] : [[0, 35, 195, H - 35], [573, 35, 195, H - 35], [205, 325, 358, 107]];
}

/** Unobstructed full-reference window: [x, y, w, h]. */
export function anchor(portrait) { return portrait ? [22, 282, 388, 218] : [205, 108, 358, 201]; }

/** Region of the expressive text layer: [x, y, w, h]. */
export function textRect(portrait) {
  const {width: W} = dimensions(portrait);
  return portrait ? [0, 525, W, 200] : [190, 320, 388, 112];
}

/**
 * Placement of region `r` (0-4) in layout `layout` (0-7) at time `t` seconds of `duration`.
 * Returns {x, y, w, h, alpha, mirror, ghosts, drawn}; ghosts are extra translucent copies drawn first.
 */
export function silhouette(layout, r, portrait, t, duration) {
  if (!Number.isInteger(layout) || layout < 0 || layout >= LAYOUT_NAMES.length) throw RangeError('layout out of range');
  if (!Number.isInteger(r) || r < 0 || r >= REGION_COUNT) throw RangeError('region out of range');
  if (!Number.isFinite(t) || !(duration > 0)) throw RangeError('time and duration must be finite and positive');
  const {width: W} = dimensions(portrait), phase = t / duration;
  let x, y, w, h;
  if (portrait) { x = r % 2 ? W * .51 : 0; y = r < 2 ? 50 : r < 4 ? 520 : 145; w = W * .49; h = 220; }
  else { x = r < 2 ? 0 : r < 4 ? W * .75 : W * .4; y = r % 2 ? 220 : 40; w = W * .25; h = 190; }
  if (layout === 0) { x = portrait ? (r % 2) * W * .52 : r * W / 5; y = portrait ? (r < 2 ? 50 : 530) : r % 2 ? 250 : 35; }
  if (layout === 2) { x += Math.sin(t * 2 + r) * 25; y += Math.cos(t * 2 + r) * 12; }
  const mirror = layout === 3 && r % 2 === 1;
  if (layout === 4) { w *= r === 4 ? 1.4 : .8; h *= r === 4 ? 1.4 : .8; }
  let alpha = 1;
  if (layout === 5) { x += Math.sin(phase * Math.PI * 2 + r) * 30; alpha = r === Math.floor(t) % 5 ? 1 : .35; }
  const ghosts = layout === 6 ? [1, 2, 3].map(j => ({x: x + j * 12, y: y + j * 6, w, h, alpha: .6})) : [];
  const drawn = !(layout === 7 && Math.floor(t * 1.5) % 5 === r);
  return {x, y, w, h, alpha, mirror, ghosts, drawn};
}

const overlapArea = (a, b) => Math.max(0, Math.min(a[0] + a[2], b[0] + b[2]) - Math.max(a[0], b[0])) * Math.max(0, Math.min(a[1] + a[3], b[1] + b[3]) - Math.max(a[1], b[1]));

/** Mean of per-sample metrics for one layout and orientation over `samples` evenly spaced instants. */
export function measureLayout(layout, portrait, {duration = 12.167, samples = 24, cell = 4} = {}) {
  if (!(samples >= 1) || !(cell >= 1)) throw RangeError('samples and cell must be at least 1');
  const {width: W, height: H} = dimensions(portrait), canvas = W * H, ref = anchor(portrait), text = textRect(portrait);
  const cols = Math.ceil(W / cell), rows = Math.ceil(H / cell);
  const series = {silhouetteShare: [], visibleShare: [], hiddenByAnchorFraction: [], offCanvasFraction: [], textCoverage: [], active: []};
  for (let s = 0; s < samples; s++) {
    const t = (s + .5) * duration / samples, grid = new Uint8Array(cols * rows);
    let boxArea = 0, off = 0, active = 0;
    for (let r = 0; r < REGION_COUNT; r++) {
      const p = silhouette(layout, r, portrait, t, duration);
      if (!p.drawn) continue;
      active++;
      for (const box of [...p.ghosts, p]) {
        boxArea += box.w * box.h;
        off += box.w * box.h - overlapArea([box.x, box.y, box.w, box.h], [0, 0, W, H]);
        const x0 = Math.max(0, Math.floor(box.x / cell)), x1 = Math.min(cols, Math.ceil((box.x + box.w) / cell));
        const y0 = Math.max(0, Math.floor(box.y / cell)), y1 = Math.min(rows, Math.ceil((box.y + box.h) / cell));
        for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) grid[y * cols + x] = 1;
      }
    }
    let covered = 0, hidden = 0, textCovered = 0, textCells = 0;
    for (let y = 0; y < rows; y++) for (let x = 0; x < cols; x++) {
      const cx = x * cell + cell / 2, cy = y * cell + cell / 2, inAnchor = cx >= ref[0] && cx < ref[0] + ref[2] && cy >= ref[1] && cy < ref[1] + ref[3];
      const inText = cx >= text[0] && cx < text[0] + text[2] && cy >= text[1] && cy < text[1] + text[3];
      if (inText) textCells++;
      if (grid[y * cols + x]) { covered++; if (inAnchor) hidden++; if (inText && !inAnchor) textCovered++; }
    }
    const cellArea = cell * cell;
    series.silhouetteShare.push(covered * cellArea / canvas);
    series.visibleShare.push((covered - hidden) * cellArea / canvas);
    series.hiddenByAnchorFraction.push(covered ? hidden / covered : 0);
    series.offCanvasFraction.push(boxArea ? off / boxArea : 0);
    series.textCoverage.push(textCells ? textCovered / textCells : 0);
    series.active.push(active);
  }
  const mean = a => a.reduce((x, y) => x + y, 0) / a.length;
  const rounded = v => Math.round(v * 1e4) / 1e4;
  const result = {layout: LAYOUT_NAMES[layout], orientation: portrait ? 'vertical' : 'horizontal', canvas: [W, H],
    anchorShare: rounded(ref[2] * ref[3] / canvas), bandShare: rounded(bands(portrait).reduce((a, b) => a + b[2] * b[3], 0) / canvas), samples};
  for (const [key, values] of Object.entries(series)) {
    result[key] = key === 'active' ? {min: Math.min(...values), mean: rounded(mean(values))} : {mean: rounded(mean(values)), min: rounded(Math.min(...values)), max: rounded(Math.max(...values))};
  }
  return result;
}

/** All layouts in both orientations, plus the vertical/horizontal ratio of the headline shares. */
export function compareOrientations(options = {}) {
  return LAYOUT_NAMES.map((name, layout) => {
    const horizontal = measureLayout(layout, false, options), vertical = measureLayout(layout, true, options);
    const ratio = (a, b) => b === 0 ? null : Math.round(a / b * 1e3) / 1e3;
    return {layout: name, horizontal, vertical, verticalOverHorizontal: {
      anchorShare: ratio(vertical.anchorShare, horizontal.anchorShare),
      visibleShare: ratio(vertical.visibleShare.mean, horizontal.visibleShare.mean),
      hiddenByAnchorFraction: ratio(vertical.hiddenByAnchorFraction.mean, horizontal.hiddenByAnchorFraction.mean),
      textCoverage: ratio(vertical.textCoverage.mean, horizontal.textCoverage.mean)}};
  });
}
