/** Measure the eight comparison layouts in horizontal and vertical form; reads no media.
 *  node tools/analyze-layout-geometry.mjs [out.json] [--table]
 */
import fs from 'node:fs/promises';
import {compareOrientations} from '../web/fx/layout-geometry.mjs';

const args = process.argv.slice(2), table = args.includes('--table'), out = args.find(a => !a.startsWith('--'));
const report = {schema: 'amps.layout-geometry.v1', duration: 12.167, samples: 24,
  note: 'Geometry of the placement rectangles only. Shares are fractions of the output canvas; no visual or artistic judgement is implied.',
  layouts: compareOrientations({duration: 12.167, samples: 24})};
if (out) await fs.writeFile(out, JSON.stringify(report, null, 2) + '\n');
if (table) {
  const f = v => (v * 100).toFixed(1).padStart(5);
  console.log('layout                    orient       anchor  silhouettes  visible  hidden-by-anchor  caption-underlaid  active(min)');
  for (const row of report.layouts) for (const o of ['horizontal', 'vertical']) {
    const m = row[o];
    console.log(`${row.layout.padEnd(25)} ${o.padEnd(11)} ${f(m.anchorShare)}%      ${f(m.silhouetteShare.mean)}%    ${f(m.visibleShare.mean)}%         ${f(m.hiddenByAnchorFraction.mean)}%        ${f(m.textCoverage.mean)}%   ${m.active.min}`);
  }
} else if (!out) console.log(JSON.stringify(report, null, 2));
