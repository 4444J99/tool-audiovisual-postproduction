#!/usr/bin/env node
/**
 * The reference for the network's parameter resolver: the workbench's own `evaluatedNode`, run over a patch at
 * sampled times. Prints {contract, cases}; `tests/test_td_resolve.py` feeds the same bases and times to the Python
 * resolver and compares.
 *
 *   node tools/td-network/oracle-evaluated.mjs web/fx/fixtures/closed-inbox.json [--stress]
 *
 * --stress rewrites the patch first (stress-patch.mjs) so that every macro curve, step and linear automation, multiply and add
 * modulation, a clip scope and a Solo event are exercised even where the shipped fixtures use only some of them.
 */
import {readFileSync} from 'node:fs';
import {evaluatedNode} from '../../web/fx/core.mjs';
import {buildNetworkSpec} from './network-spec.mjs';

import {stressPatch} from './stress-patch.mjs';

const patch = JSON.parse(readFileSync(process.argv[2], 'utf8'));
if (process.argv.includes('--stress')) stressPatch(patch);
const spec = buildNetworkSpec(patch);
const processors = patch.nodes.filter(node => spec.contract.nodes.some(item => item.id === node.id));
const times = new Set([0, patch.duration]);
for (let step = 1; step < 16; step++) times.add(patch.duration * step / 16);
for (const event of patch.events ?? []) for (const delta of [-0.01, 0, 0.01]) times.add(Math.max(0, event.time + delta));
for (const node of processors) {
  for (const lane of node.automation ?? []) for (const frame of lane.keyframes) for (const delta of [-0.01, 0, 0.01]) times.add(Math.max(0, frame.time + delta));
  if (node.scope?.type === 'clip') for (const edge of [node.scope.start, node.scope.end]) for (const delta of [-0.01, 0, 0.01]) times.add(Math.max(0, edge + delta));
}
const cases = [...times].sort((a, b) => a - b).flatMap((time, index) => [0, 0.37, 1].map((envelope, slot) => {
  const controls = Object.fromEntries(processors.map((node, i) => [node.id, [0, 0.37, 1][(index + slot + i) % 3]]));
  const evaluated = Object.fromEntries(processors.map(node => {
    const value = evaluatedNode(node, time, patch, controls[node.id]);
    return [node.id, {intensity: value.intensity, wet: value.wet, params: value.params, solo: value.solo, bypass: value.bypass, active: value.active}];
  }));
  return {time, controls, evaluated};
}));
console.log(JSON.stringify({contract: spec.contract, cases}));
