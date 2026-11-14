#!/usr/bin/env node
/**
 * Generate the TouchDesigner version of an FX rack patch.
 *
 *   node tools/fx-td-network.mjs web/fx/fixtures/closed-inbox.json --out outputs/td [--source-file media.mp4]
 *        [--osc-in-port 7000] [--osc-out-port 7001] [--host 127.0.0.1] [--root-name fx_rack]
 *
 * Writes <id>.network.json (the spec), <id>.build.py (paste or exec it in TouchDesigner) and modules/.
 */
import {readFileSync} from 'node:fs';
import {writeNetwork} from './td-network/emit.mjs';

const args = process.argv.slice(2);
const option = (name, fallback) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : fallback; };
const patchPath = args.find((arg, i) => !arg.startsWith('--') && (i === 0 || !args[i - 1].startsWith('--')));
const outDir = option('--out');
if (!patchPath || !outDir) { console.error('usage: node tools/fx-td-network.mjs patch.json --out DIR [--source-file PATH] [--osc-in-port N] [--osc-out-port N] [--host H] [--root-name NAME]'); process.exit(2); }
const number = (name, fallback) => { const value = option(name); return value === undefined ? fallback : Number(value); };
try {
  const patch = JSON.parse(readFileSync(patchPath, 'utf8'));
  const {spec, files} = writeNetwork(patch, outDir, {
    sourceFile: option('--source-file'), oscInPort: number('--osc-in-port', 7000), oscOutPort: number('--osc-out-port', 7001),
    host: option('--host'), rootName: option('--root-name'),
  });
  console.log(JSON.stringify({patch: spec.patch.id, operators: spec.operators.length, processors: spec.contract.nodes.length, wires: spec.wires.length, files, warnings: spec.warnings}, null, 2));
} catch (error) { console.error(error.message); process.exit(1); }
