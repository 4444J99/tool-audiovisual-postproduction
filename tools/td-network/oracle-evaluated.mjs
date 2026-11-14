#!/usr/bin/env node
/**
 * The reference for the network's parameter resolver: the workbench's own `evaluatedNode`, run over a patch at
 * sampled times. Prints {contract, cases}; `tests/test_td_resolve.py` feeds the same bases and times to the Python
 * resolver and compares.
 *
 *   node tools/td-network/oracle-evaluated.mjs web/fx/fixtures/closed-inbox.json [--stress]
 *
 * --stress rewrites the patch first so that every macro curve, step and linear automation, multiply and add
 * modulation, a clip scope and a Solo event are exercised even where the shipped fixtures use only some of them.
 */
import {readFileSync} from 'node:fs';
import {evaluatedNode} from '../../web/fx/core.mjs';
import {buildNetworkSpec} from './network-spec.mjs';

import {CATALOG_BY_TYPE, MACRO_CURVES} from '../../web/fx/core.mjs';

const patch = JSON.parse(readFileSync(process.argv[2], 'utf8'));
if (process.argv.includes('--stress')) {
  patch.nodes.filter(node => CATALOG_BY_TYPE[node.type]).forEach((node, index) => {
    const definition = CATALOG_BY_TYPE[node.type], first = definition.params[0], second = definition.params[1] ?? first;
    const at = (item, ratio) => { const value = item.min + (item.max - item.min) * ratio; return item.step === 1 ? Math.round(value) : value; };
    node.macro = definition.params.slice(0, 4).map((item, slot) => ({parameter: item.key, min: at(item, 0.2), max: at(item, 0.9), curve: MACRO_CURVES[(index + slot) % MACRO_CURVES.length]}));
    node.automation = [
      {parameter: 'intensity', interpolation: index % 2 ? 'step' : 'linear', keyframes: [{time: 0, value: 0.1}, {time: patch.duration * 0.4, value: 0.9}, {time: patch.duration * 0.8, value: 0.3}]},
      {parameter: `params.${second.key}`, interpolation: index % 2 ? 'linear' : 'step', keyframes: [{time: 0, value: second.min}, {time: patch.duration * 0.5, value: second.max}]},
    ];
    node.modulation = {parameter: index % 2 ? 'intensity' : `params.${first.key}`, mode: index % 3 ? 'multiply' : 'add', amount: index % 2 ? 0.4 : (first.max - first.min) * 0.25};
    if (index === 1) node.scope = {type: 'clip', start: patch.duration * 0.25, end: patch.duration * 0.75};
    patch.events = [...(patch.events ?? []), ...(index === 2 ? [{time: patch.duration * 0.3, nodeId: node.id, parameter: 'solo', value: true}, {time: patch.duration * 0.6, nodeId: node.id, parameter: 'solo', value: false}, {time: patch.duration * 0.5, nodeId: node.id, parameter: 'wet', value: 0.25}] : [])];
  });
}
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
