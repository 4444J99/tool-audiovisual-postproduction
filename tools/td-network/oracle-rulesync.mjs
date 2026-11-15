#!/usr/bin/env node
/**
 * The reference for the rule sync (docs/FX-TOUCHDESIGNER.md, "Rule sync"): a patch is edited the way the workbench edits it
 * (parameter-rules.mjs `applyParameterEdit`, the automation form, Clear, a preset), and after every step the rack's own
 * feedback (`OscFeedback`) is collected and the workbench's `evaluatedNode` is sampled. Prints
 * {contract, start, steps: [{label, messages, bases, cases}]}. `contract` is the network generated from the patch BEFORE it
 * was given its rules (`start` says which), so the first step also proves that a connect resynchronises a stale snapshot.
 * `tests/test_td_rulesync.py` feeds each step's messages to the generated Python module and compares what its resolver
 * returns with `evaluatedNode`. The patch is the stressed fixture (stress-patch.mjs).
 *
 *   node tools/td-network/oracle-rulesync.mjs web/fx/fixtures/closed-inbox.json
 */
import {readFileSync} from 'node:fs';
import {CATALOG_BY_TYPE, evaluatedNode, clonePatch} from '../../web/fx/core.mjs';
import {OscFeedback} from '../../web/fx/osc-control.mjs';
import {applyParameterEdit, processorRules} from '../../web/fx/parameter-rules.mjs';
import {buildNetworkSpec} from './network-spec.mjs';
import {stressPatch} from './stress-patch.mjs';

const patch = stressPatch(JSON.parse(readFileSync(process.argv[2], 'utf8')));
// The network as generated before the workbench had these rules: the same patch with no automation, macros or events.
// Sidechain modulation and clip scope are not rules a control edit changes, so the snapshot keeps them.
const snapshot = clonePatch(patch);
snapshot.events = [];
for (const node of snapshot.nodes) if (CATALOG_BY_TYPE[node.type]) {node.automation = []; node.macro = [];}
const contract = buildNetworkSpec(snapshot).contract;
const nodes = patch.nodes.filter(node => CATALOG_BY_TYPE[node.type]);
if (nodes.length < 3) throw Error('the rule sync oracle needs a patch with three processors');
const [a, b, c] = nodes;
const second = node => CATALOG_BY_TYPE[node.type].params[1] ?? CATALOG_BY_TYPE[node.type].params[0];
const first = node => CATALOG_BY_TYPE[node.type].params[0];

const view = {nodes, selectedId: a.id, duration: patch.duration, time: 0, current: () => 0, global: () => false, rules: id => {
  const node = nodes.find(item => item.id === id);
  return node ? processorRules(patch, node) : null;
}};
const feedback = new OscFeedback();
const basesOf = () => Object.fromEntries(nodes.map(node => [node.id, {intensity: node.intensity, wet: node.wet, params: {...node.params}, bypass: Boolean(node.bypass), solo: Boolean(node.solo)}]));
const times = () => {
  const set = new Set([0.05, patch.duration - 0.05]);
  for (let step = 1; step < 24; step++) set.add(patch.duration * step / 24 + 0.0123);
  for (const event of patch.events) for (const delta of [-0.2, 0.2]) set.add(Math.max(0, event.time + delta));
  for (const node of nodes) for (const lane of node.automation) for (const frame of lane.keyframes) for (const delta of [-0.2, 0.2]) set.add(Math.max(0, frame.time + delta));
  return [...set].sort((x, y) => x - y);
};
const sample = () => times().flatMap((time, index) => [0, 0.37, 1].map((_, slot) => {
  const controls = Object.fromEntries(nodes.map((node, i) => [node.id, [0, 0.37, 1][(index + slot + i) % 3]]));
  const evaluated = Object.fromEntries(nodes.map(node => {
    const value = evaluatedNode(node, time, patch, controls[node.id]);
    return [node.id, {intensity: value.intensity, wet: value.wet, params: value.params, solo: value.solo, bypass: value.bypass, active: value.active}];
  }));
  return {time, controls, evaluated};
})).filter((item, index) => index % 3 === 0);   // one envelope per time: the resolver's envelope handling is covered elsewhere
const steps = [];
const step = (label, edit) => {
  edit?.();
  const messages = feedback.collect(view).filter(message => /\/(released|event|lane|macro)\//.test(message.address));
  steps.push({label, messages, bases: basesOf(), cases: sample()});
};
const dur = patch.duration;

step('connect: the first report resynchronises the whole rack', null);
step('nothing changed: nothing is sent', null);
step('hand edit of an automated control releases its lane', () => applyParameterEdit(patch, a, 'intensity', 0.42, {}));
step('hand edit of a macro-driven parameter releases its macro', () => applyParameterEdit(patch, a, `params.${first(a).key}`, CATALOG_BY_TYPE[a.type].params[0].min, {}));
step('recording adds events, and a second edit at nearly the same time replaces the first', () => {
  const key = `params.${second(b).key}`, item = second(b);
  applyParameterEdit(patch, b, key, item.min + (item.max - item.min) * 0.8, {recording: true, time: 2});
  applyParameterEdit(patch, b, key, item.min + (item.max - item.min) * 0.3, {recording: true, time: 2.01});
  applyParameterEdit(patch, b, key, item.min + (item.max - item.min) * 0.9, {recording: true, time: 5.5});
  applyParameterEdit(patch, b, 'bypass', true, {recording: true, time: 7.25});
  applyParameterEdit(patch, b, 'bypass', false, {recording: true, time: 9.5});
});
step('recording over an automated control keeps its lane and adds an event', () => applyParameterEdit(patch, c, 'wet', 0.15, {recording: true, time: 3}));
step('the automation form replaces a lane', () => {
  b.automation = b.automation.filter(lane => lane.parameter !== 'intensity');
  b.automation.push({parameter: 'intensity', interpolation: 'step', keyframes: [{time: 0, value: 0.2}, {time: 4, value: 0.8}, {time: 8, value: 0.5}]});
});
step('a hand edit of a recorded control releases its events', () => applyParameterEdit(patch, b, `params.${second(b).key}`, CATALOG_BY_TYPE[b.type].params[1].max, {}));
step('Clear removes every recorded event', () => { patch.events = []; });
step('a preset restores a processor with lanes, a macro and events of its own', () => {
  const restored = clonePatch(c);
  restored.macro = [{parameter: first(c).key, min: CATALOG_BY_TYPE[c.type].params[0].min, max: CATALOG_BY_TYPE[c.type].params[0].max, curve: 'smoothstep'}];
  restored.automation = [{parameter: 'wet', interpolation: 'linear', keyframes: [{time: 0, value: 1}, {time: 6, value: 0.2}]}];
  Object.assign(c, {macro: restored.macro, automation: restored.automation});
  patch.events.push({time: 1.5, nodeId: c.id, parameter: 'wet', value: 0.6});
});
console.log(JSON.stringify({contract, start: 'generated before the patch had its automation, macros and events', steps}));
