import test from 'node:test';
import assert from 'node:assert/strict';
import {oscControl, controller, freshPatch, CATALOG_BY_TYPE} from './runtime.mjs';

const {encodeSegment, decodeSegment, nodeAddress, interpretOsc, OscFeedback} = oscControl;

function rig(type = 'delay') {
  const patch = freshPatch(type);
  const node = patch.nodes.find(n => CATALOG_BY_TYPE[n.type]);
  node.solo = false; node.bypass = false;
  const values = new Map();
  const globals = new Map([['play', false], ['record', false], ['loop', true]]);
  const view = {
    nodes: patch.nodes.filter(n => CATALOG_BY_TYPE[n.type]), selectedId: node.id, duration: 12, time: 0,
    current: (id, path) => values.has(`${id}|${path}`) ? values.get(`${id}|${path}`)
      : path === 'bypass' || path === 'solo' ? Boolean(node[path])
      : path.startsWith('params.') ? node.params[path.slice(7)] : node[path],
    global: name => globals.get(name),
  };
  return {patch, node, view, values, globals};
}
const osc = (address, ...args) => ({address, args});

test('address segments round-trip and never contain an OSC-reserved character', () => {
  for (const id of ['fx-1', 'fx 1', 'a/b', 'wild*card?', 'brace{a,b}[x]#', 'ünï', 'a%b', '']) {
    const encoded = encodeSegment(id);
    assert.doesNotMatch(encoded, /[\s/*?,[\]{}#]/, id);
    assert.equal(decodeSegment(encoded), id);
  }
  assert.equal(decodeSegment('%zz'), null);
  assert.equal(decodeSegment('%C3'), null, 'invalid UTF-8');
  assert.equal(decodeSegment('é'), null, 'raw non-ASCII is not a valid segment');
  assert.equal(nodeAddress('fx 1', 'params.time'), '/fx/node/fx%201/params/time');
  assert.equal(nodeAddress('fx-1', 'intensity'), '/fx/node/fx-1/intensity');
});

test('selected-processor knobs scale a normalised value onto the control range and step', () => {
  const {view, node} = rig('delay');
  const {ranges} = controller.describeControls(node, view);
  const half = interpretOsc(osc('/fx/selected/knob/0', 0.5), view);
  assert.deepEqual(half.actions, [{nodeId: node.id, path: 'intensity', value: 0.5, binding: 'osc'}]);
  const third = ranges[2];
  const result = interpretOsc(osc('/fx/selected/knob/2', 1), view);
  assert.deepEqual(result.actions.map(a => [a.path, a.value]), [[third.path, third.max]]);
  const low = interpretOsc(osc('/fx/selected/knob/2', -5), view);
  assert.equal(low.actions[0].value, third.min, 'out-of-range input is clamped');
  const high = interpretOsc(osc('/fx/selected/knob/2', 9), view);
  assert.equal(high.actions[0].value, third.max);
});

test('a value equal to the current one produces no action', () => {
  const {view, node, values} = rig('delay');
  values.set(`${node.id}|intensity`, 0.5);
  assert.deepEqual(interpretOsc(osc('/fx/selected/knob/0', 0.5), view).actions, []);
  values.set(`${node.id}|bypass`, true);
  assert.deepEqual(interpretOsc(osc('/fx/selected/switch/0', 1), view).actions, []);
});

test('switches accept numbers, booleans and bare triggers', () => {
  const {view, node, values} = rig('delay');
  assert.deepEqual(interpretOsc(osc('/fx/selected/switch/0', 1), view).actions.map(a => [a.path, a.value]), [['bypass', true]]);
  assert.deepEqual(interpretOsc(osc('/fx/selected/switch/1', true), view).actions.map(a => [a.path, a.value]), [['solo', true]]);
  assert.deepEqual(interpretOsc(osc('/fx/selected/switch/0'), view).actions.map(a => [a.path, a.value]), [['bypass', true]], 'no argument means on');
  values.set(`${node.id}|bypass`, true);
  assert.deepEqual(interpretOsc(osc('/fx/selected/switch/0', 0.2), view).actions.map(a => [a.path, a.value]), [['bypass', false]]);
  assert.deepEqual(interpretOsc(osc('/fx/selected/switch/0', false), view).actions.map(a => [a.path, a.value]), [['bypass', false]]);
});

test('processors are addressed by id, with reserved characters escaped', () => {
  const {view, node} = rig('delay');
  const odd = {...node, id: 'fx one/#2'};
  view.nodes = [odd]; view.selectedId = odd.id;
  const key = controller.describeControls(odd, view).ranges[2].path.slice(7);
  const byId = interpretOsc(osc(`/fx/node/${encodeSegment(odd.id)}/intensity`, 0.25), view);
  assert.deepEqual(byId.actions.map(a => [a.nodeId, a.path, a.value]), [[odd.id, 'intensity', 0.25]]);
  const paramPath = `/fx/node/${encodeSegment(odd.id)}/params/${encodeSegment(key)}`;
  const moved = [0, 1].flatMap(v => interpretOsc(osc(paramPath, v), view).actions);
  assert.ok(moved.length >= 1 && moved.every(a => a.nodeId === odd.id && a.path === `params.${key}`));
  assert.equal(interpretOsc(osc(`/fx/node/${encodeSegment('missing')}/wet`, 1), view).ignored, true);
  assert.equal(interpretOsc(osc(`/fx/node/${encodeSegment(odd.id)}/params/doesNotExist`, 1), view).ignored, true);
  assert.equal(interpretOsc(osc(`/fx/node/${encodeSegment(odd.id)}/nonsense`, 1), view).ignored, true);
  assert.equal(interpretOsc(osc('/fx/node/%zz/wet', 1), view).ignored, true);
});

test('transport, selection and sync messages', () => {
  const {view, globals} = rig('delay');
  assert.deepEqual(interpretOsc(osc('/fx/transport/play', 1), view).actions, [{global: 'play', value: true, binding: 'osc'}]);
  assert.deepEqual(interpretOsc(osc('/fx/transport/loop', 1), view).actions, [], 'loop is already on');
  assert.deepEqual(interpretOsc(osc('/fx/transport/loop', 0), view).actions, [{global: 'loop', value: false, binding: 'osc'}]);
  globals.set('record', true);
  assert.deepEqual(interpretOsc(osc('/fx/transport/record', false), view).actions, [{global: 'record', value: false, binding: 'osc'}]);
  assert.equal(interpretOsc(osc('/fx/transport/rewind', 1), view).ignored, true);
  assert.deepEqual(interpretOsc(osc('/fx/select/next'), view).select, {delta: 1});
  assert.deepEqual(interpretOsc(osc('/fx/select/prev', 1), view).select, {delta: -1});
  assert.equal(interpretOsc(osc('/fx/select/next', 0), view).select, null, 'a released button does nothing');
  assert.deepEqual(interpretOsc(osc('/fx/selected', view.nodes[0].id), view).select, {nodeId: view.nodes[0].id});
  assert.equal(interpretOsc(osc('/fx/selected', 'nope'), view).ignored, true);
  assert.equal(interpretOsc(osc('/fx/sync'), view).sync, true);
});

test('anything else is ignored without throwing', () => {
  const {view} = rig('delay');
  const junk = [
    null, undefined, {}, {address: 5, args: []}, {address: '/fx/selected/knob/0'}, osc('/other/knob/0', 1), osc('fx/selected/knob/0', 1),
    osc('/fx'), osc('/fx/'), osc('/fx/selected/knob/99', 1), osc('/fx/selected/knob/x', 1), osc('/fx/selected/knob/0', 'loud'),
    osc('/fx/selected/knob/0', NaN), osc('/fx/selected/knob/0', null), osc('/fx/selected/switch/7', 1), osc('/fx/selected/switch/0', 'on'),
    osc('/fx/node', 1), osc('/fx/node/x', 1), osc('/fx/transport', 1), osc('/fx/' + 'a/'.repeat(500), 1),
  ];
  for (const message of junk) {
    const result = interpretOsc(message, view);
    assert.deepEqual(result.actions, [], JSON.stringify(message));
    assert.equal(result.select, null);
    assert.equal(result.ignored, true, JSON.stringify(message));
  }
});

test('with nothing selected, selected-processor messages are ignored', () => {
  const {view} = rig('delay');
  view.selectedId = 'gone';
  assert.equal(interpretOsc(osc('/fx/selected/knob/0', 1), view).ignored, true);
});

test('feedback sends the whole state once, then only changes', () => {
  const {view, node, values} = rig('delay');
  const feedback = new OscFeedback();
  const first = feedback.collect(view);
  const byAddress = Object.fromEntries(first.map(m => [m.address, m.args]));
  assert.deepEqual(byAddress['/fx/count'], [1]);
  assert.deepEqual(byAddress[`/fx/node/${encodeSegment(node.id)}/type`], [node.type]);
  assert.deepEqual(byAddress['/fx/selected'], [node.id]);
  assert.deepEqual(byAddress['/fx/selected/type'], [node.type]);
  assert.deepEqual(byAddress['/fx/transport/loop'], [1]);
  assert.deepEqual(byAddress['/fx/transport/play'], [0]);
  assert.deepEqual(byAddress[`/fx/node/${encodeSegment(node.id)}/bypass`], [0]);
  const {ranges} = controller.describeControls(node, view);
  for (const control of ranges) {
    const sent = byAddress[nodeAddress(node.id, control.path)];
    assert.ok(sent && sent[0] >= 0 && sent[0] <= 1, control.path);
  }
  assert.equal(first.length, new Set(first.map(m => m.address)).size, 'one message per address');

  assert.deepEqual(feedback.collect(view).map(m => m.address), [], 'nothing changed');
  values.set(`${node.id}|intensity`, 0.25);
  values.set(`${node.id}|solo`, true);
  assert.deepEqual(feedback.collect(view), [
    {address: nodeAddress(node.id, 'intensity'), args: [0.25]},
    {address: nodeAddress(node.id, 'solo'), args: [1]},
  ]);
  assert.equal(feedback.collect(view, {full: true}).length, first.length, 'sync resends everything');
});

test('feedback time is rate-limited and a changed selection is reported', () => {
  const {view} = rig('delay');
  const feedback = new OscFeedback();
  feedback.collect(view);
  view.time = 0.01;
  assert.deepEqual(feedback.collect(view), []);
  view.time = 0.05;
  assert.deepEqual(feedback.collect(view), [{address: '/fx/time', args: [0.05]}]);
  view.time = 0.06;
  assert.deepEqual(feedback.collect(view), []);
  view.selectedId = '';
  assert.deepEqual(feedback.collect(view).map(m => [m.address, m.args]), [['/fx/selected', ['']], ['/fx/selected/type', ['']]]);
});

test('a moved processor parameter is reported on the same normalised scale TouchDesigner sends', () => {
  const {view, node, values} = rig('delay');
  const feedback = new OscFeedback();
  feedback.collect(view);
  const {ranges} = controller.describeControls(node, view);
  const control = ranges[2];
  const sent = interpretOsc(osc('/fx/selected/knob/2', 0.5), view).actions[0];
  values.set(`${node.id}|${sent.path}`, sent.value);
  const [message] = feedback.collect(view);
  assert.equal(message.address, nodeAddress(node.id, control.path));
  assert.ok(Math.abs(message.args[0] - controller.valueToNorm(control, sent.value)) < 1e-4);
  // Feeding the echo back changes nothing, so a wired-up loop settles.
  assert.deepEqual(interpretOsc(osc(message.address, message.args[0]), view).actions, []);
});

test('echoing a reported value back never edits the rack, even for off-grid values', () => {
  const {view, node, values} = rig('delay');
  const feedback = new OscFeedback();
  feedback.collect(view);
  const {ranges} = controller.describeControls(node, view);
  ranges.forEach((control, index) => {
    // Presets and automation need not lie on the step grid, and float32 transport adds its own error.
    for (const fraction of [0.37, 0.5, 0.9]) {
      const offGrid = control.min + (control.max - control.min) * fraction + (control.step || 0) * 0.31;
      const value = Math.min(control.max, offGrid);
      values.set(`${node.id}|${control.path}`, value);
      const [reported] = feedback.collect(view);
      const wire = Math.fround(reported.args[0]);
      assert.deepEqual(interpretOsc(osc(`/fx/selected/knob/${index}`, wire), view).actions, [], `${control.path} at ${value}`);
    }
  });
});

test('a real move goes through, while one inside the echo tolerance does not', () => {
  const {view, node} = rig('delay');
  const intensity = controller.describeControls(node, view).ranges[0];
  const norm = controller.valueToNorm(intensity, view.current(node.id, 'intensity'));
  assert.equal(interpretOsc(osc('/fx/selected/knob/0', norm + oscControl.ECHO_TOLERANCE / 2), view).actions.length, 0);
  assert.equal(interpretOsc(osc('/fx/selected/knob/0', Math.min(1, norm + 0.02)), view).actions.length, norm + 0.02 <= 1 ? 1 : 0);
});
