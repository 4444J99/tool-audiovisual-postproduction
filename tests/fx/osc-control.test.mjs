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

// --- rule sync: the rack reports changes to the rules that govern a control ---------------------------------------------
import {applyParameterEdit, processorRules, EVENT_MERGE_SECONDS} from '../../web/fx/parameter-rules.mjs';

function ruleRig() {
  const patch = freshPatch('delay');
  const node = patch.nodes.find(n => CATALOG_BY_TYPE[n.type]);
  node.automation = [{parameter: 'wet', interpolation: 'linear', keyframes: [{time: 0, value: 0.2}, {time: 5, value: 1}]}];
  const view = {nodes: [node], selectedId: node.id, duration: 12, time: 0, current: () => 0.5, global: () => false, rules: id => id === node.id ? processorRules(patch, node) : null};
  const ruleMessages = feedback => feedback.collect(view).filter(m => /\/(released|event|lane|macro)\//.test(m.address));
  return {patch, node, view, ruleMessages, feedback: new OscFeedback()};
}

test('rule addresses are percent-encoded like the control addresses and split a parameter path the same way', () => {
  assert.equal(oscControl.ruleAddress('fx 1', 'released', 'params.time'), '/fx/node/fx%201/released/params/time');
  assert.equal(oscControl.ruleAddress('fx-1', 'lane', 'wet'), '/fx/node/fx-1/lane/wet');
  assert.deepEqual([...oscControl.RULE_KINDS], ['released', 'event', 'lane', 'macro']);
});

test('the first report releases every control of a processor, then sends its rules; a second report sends nothing', () => {
  const {node, ruleMessages, feedback} = ruleRig();
  const first = ruleMessages(feedback);
  const released = first.filter(m => m.address.includes('/released/')).map(m => m.address);
  const {ranges, switches} = controller.describeControls(node, {duration: 12});
  assert.equal(released.length, ranges.length + switches.length);
  assert.equal(first.findIndex(m => !m.address.includes('/released/')) , released.length, 'every release comes first');
  const lane = first.find(m => m.address.endsWith('/lane/wet'));
  assert.deepEqual(JSON.parse(lane.args[0]), {interpolation: 'linear', keyframes: [{time: 0, value: 0.2}, {time: 5, value: 1}]});
  assert.ok(first.some(m => m.address.includes('/macro/params/')), 'the preset macro mappings are sent');
  assert.deepEqual(ruleMessages(feedback), []);
});

test('recording sends one event message per new event and a replaced value at the same time, and keeps the lane', () => {
  const {patch, node, ruleMessages, feedback} = ruleRig();
  ruleMessages(feedback);
  applyParameterEdit(patch, node, 'wet', 0.7, {recording: true, time: 1.23456});
  assert.deepEqual(ruleMessages(feedback), [{address: '/fx/node/fx-1/event/wet', args: [1.2346, 0.7]}]);
  applyParameterEdit(patch, node, 'wet', 0.4, {recording: true, time: 1.23456 + EVENT_MERGE_SECONDS / 2});   // merged into the last event by the workbench
  assert.deepEqual(ruleMessages(feedback), [{address: '/fx/node/fx-1/event/wet', args: [1.2346, 0.4]}]);
  applyParameterEdit(patch, node, 'bypass', true, {recording: true, time: 3});
  assert.deepEqual(ruleMessages(feedback), [{address: '/fx/node/fx-1/event/bypass', args: [3, 1]}], 'booleans travel as 0 / 1');
});

test('a hand edit releases the control, and what else the control still has is sent again', () => {
  const {patch, node, ruleMessages, feedback} = ruleRig();
  applyParameterEdit(patch, node, 'wet', 0.9, {recording: true, time: 1});
  applyParameterEdit(patch, node, 'wet', 0.8, {recording: true, time: 2});
  ruleMessages(feedback);
  applyParameterEdit(patch, node, 'wet', 0.3, {});
  assert.deepEqual(ruleMessages(feedback), [{address: '/fx/node/fx-1/released/wet', args: []}]);
  // Clear removes one control's events but not its lane: the release drops both in the mirror, so the lane is sent again
  applyParameterEdit(patch, node, 'wet', 0.9, {recording: true, time: 1});
  node.automation.push({parameter: 'wet', interpolation: 'step', keyframes: [{time: 0, value: 1}]});
  ruleMessages(feedback);
  patch.events = [];
  assert.deepEqual(ruleMessages(feedback).map(m => m.address), ['/fx/node/fx-1/released/wet', '/fx/node/fx-1/lane/wet']);
});

test('a processor parameter edit releases its macro mapping as well', () => {
  const {patch, node, ruleMessages, feedback} = ruleRig();
  const key = node.macro[0].parameter;
  ruleMessages(feedback);
  applyParameterEdit(patch, node, `params.${key}`, CATALOG_BY_TYPE[node.type].params.find(p => p.key === key).min, {});
  assert.deepEqual(ruleMessages(feedback).map(m => m.address), [oscControl.ruleAddress(node.id, 'released', `params.${key}`)]);
});

test('a lane too large for one bridge frame is not sent: the mirror releases the control and keeps it released', () => {
  const {node, ruleMessages, feedback} = ruleRig();
  ruleMessages(feedback);
  node.automation = [{parameter: 'wet', interpolation: 'linear', keyframes: Array.from({length: 2000}, (_, i) => ({time: i / 100, value: 0.123456789}))}];
  assert.deepEqual(ruleMessages(feedback), [{address: '/fx/node/fx-1/released/wet', args: []}]);
  assert.deepEqual(ruleMessages(feedback), [], 'and it stays quiet');
});

test('a view without rules reports no rule messages, and reset() makes the next report a full resync', () => {
  const {view, node, feedback, ruleMessages} = ruleRig();
  const plain = {...view, rules: undefined};
  assert.deepEqual(new OscFeedback().collect(plain).filter(m => /\/(released|event|lane|macro)\//.test(m.address)), []);
  ruleMessages(feedback);
  feedback.reset();
  assert.ok(ruleMessages(feedback).some(m => m.address === oscControl.ruleAddress(node.id, 'released', 'intensity')));
});

test('applyParameterEdit is exactly what the workbench setter does outside recording and while recording', () => {
  const {patch, node} = ruleRig();
  applyParameterEdit(patch, node, 'params.taps', 3, {recording: true, time: 2});
  assert.equal(patch.events.length, 1);
  assert.equal(node.params.taps === 3, false, 'recording does not change the base value');
  applyParameterEdit(patch, node, 'params.taps', 3, {});
  assert.deepEqual(patch.events, []);
  assert.equal(node.params.taps, 3);
  assert.ok(!node.macro.some(m => m.parameter === 'taps'));
});

test('a 50-keyframe lane passes the bridge\'s wire check (it was refused above 1,024 characters)', async () => {
  const {parseWireMessage} = await import('../../tools/fx-bridge/bridge.mjs');
  const {node, ruleMessages, feedback} = ruleRig();
  ruleMessages(feedback);
  node.automation = [{parameter: 'wet', interpolation: 'linear', keyframes: Array.from({length: 50}, (_, i) => ({time: i / 4, value: 0.123456789012}))}];
  const lane = ruleMessages(feedback).find(m => m.address.endsWith('/lane/wet'));
  assert.ok(lane.args[0].length > 1024);
  assert.deepEqual(parseWireMessage(JSON.stringify(lane)), lane);
});

test('the largest lane the rack will send arrives through a real UDP socket, whole', async () => {
  const {encodeMessage, decodePacket} = await import('../../tools/fx-bridge/osc-codec.mjs');
  const dgram = await import('node:dgram');
  const {node, ruleMessages, feedback} = ruleRig();
  ruleMessages(feedback);
  let count = 1;
  const make = n => [{parameter: 'wet', interpolation: 'linear', keyframes: Array.from({length: n}, (_, i) => ({time: i / 13, value: 0.123456789012}))}];
  while (JSON.stringify(make(count + 1)[0]).length <= oscControl.MAX_RULE_CHARS) count++;
  node.automation = make(count);
  const lane = ruleMessages(feedback).find(m => m.address.endsWith('/lane/wet'));
  assert.ok(lane, 'a lane at the limit is still sent');
  // the page sends the message as JSON; the bridge must accept it, whatever its length up to the limit (a 50-keyframe lane is over 1,000 characters)
  const wire = (await import('../../tools/fx-bridge/bridge.mjs')).parseWireMessage(JSON.stringify(lane));
  assert.deepEqual(wire, lane);
  assert.ok(lane.args[0].length <= oscControl.MAX_RULE_CHARS && (await import('../../tools/fx-bridge/bridge.mjs')).MAX_STRING >= oscControl.MAX_RULE_CHARS);
  const packet = encodeMessage(lane.address, lane.args);
  assert.ok(packet.length < 9216, `${packet.length} bytes`);
  const receiver = dgram.createSocket('udp4'), sender = dgram.createSocket('udp4');
  await new Promise(resolve => receiver.bind(0, '127.0.0.1', resolve));
  const arrived = new Promise(resolve => receiver.once('message', resolve));
  await new Promise((resolve, reject) => sender.send(packet, receiver.address().port, '127.0.0.1', error => error ? reject(error) : resolve()));
  const got = decodePacket(new Uint8Array(await arrived));
  receiver.close(); sender.close();
  assert.deepEqual(JSON.parse(got[0].args[0]).keyframes.length, count);
  node.automation = make(count + 40);
  assert.deepEqual(ruleMessages(feedback), [{address: '/fx/node/fx-1/released/wet', args: []}], 'one past the limit is released, never sent');
});
