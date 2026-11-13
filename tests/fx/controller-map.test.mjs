import test from 'node:test';
import assert from 'node:assert/strict';
import {controller, freshPatch, CATALOG_BY_TYPE} from './runtime.mjs';

const {
  parseMidi, sourceOf, describeControls, findControl, normToValue, valueToNorm, ControllerMap, defaultTemplate,
  GestureTracker, formatValue, CONTROLLER_MAP_VERSION, MAX_BINDINGS, SELECTED, describeSource,
} = controller;

function rig(type = 'delay') {
  const patch = freshPatch(type);
  const node = patch.nodes.find(n => CATALOG_BY_TYPE[n.type]);
  const values = new Map();
  const globals = new Map([['play', false], ['record', false], ['loop', true]]);
  const view = {
    nodes: patch.nodes, selectedId: node.id, duration: 12,
    current: (id, path) => values.has(`${id}|${path}`) ? values.get(`${id}|${path}`)
      : path === 'bypass' || path === 'solo' ? Boolean(node[path])
      : path.startsWith('params.') ? node.params[path.slice(7)] : node[path],
    global: name => globals.get(name),
  };
  const apply = actions => { for (const a of actions) a.global ? globals.set(a.global, a.value) : values.set(`${a.nodeId}|${a.path}`, a.value); };
  return {patch, node, view, values, globals, apply};
}
const cc = (number, value, channel = 1) => parseMidi([0xB0 | (channel - 1), number, value]);
const note = (number, velocity, channel = 1, on = true) => parseMidi([(on ? 0x90 : 0x80) | (channel - 1), number, velocity]);

test('MIDI messages decode and malformed or unused ones are ignored', () => {
  assert.deepEqual(cc(21, 127, 3), {kind: 'cc', channel: 3, number: 21, value: 127, max: 127, norm: 1});
  assert.equal(note(36, 100).kind, 'note-on');
  assert.equal(note(36, 0).kind, 'note-off', 'note-on with velocity 0 is a note-off');
  assert.equal(note(36, 64, 1, false).kind, 'note-off');
  const bend = parseMidi([0xE2, 0x7F, 0x7F]);
  assert.equal(bend.kind, 'pitch-bend'); assert.equal(bend.value, 16383); assert.equal(bend.norm, 1); assert.equal(bend.channel, 3);
  assert.equal(parseMidi([0xE0, 0, 0]).norm, 0);
  for (const bad of [null, undefined, [], [0xF8], [0xF0, 1, 2], [0x20, 1, 2], [0xB0, 1], [0xB0, 128, 1], [0xB0, 1, 200], [0xB0, 1.5, 1], [0xC0, 5, 0], [0xD0, 5, 0], 'x']) {
    assert.equal(parseMidi(bad), null, JSON.stringify(bad));
  }
  assert.equal(parseMidi(new Uint8Array([0xB0, 7, 64])).value, 64);
  assert.deepEqual(sourceOf(note(40, 90, 5)), {kind: 'note', channel: 5, number: 40});
  assert.equal(describeSource({kind: 'cc', channel: 0, number: 21}), 'CC 21 any ch');
});

test('controls are derived from the catalog and respect the clip duration', () => {
  const {node} = rig('delay');
  const {ranges, switches} = describeControls(node, {duration: 12});
  const definition = CATALOG_BY_TYPE.delay;
  assert.deepEqual(ranges.slice(0, 2).map(c => c.path), ['intensity', 'wet']);
  assert.deepEqual(ranges.slice(2).map(c => c.path), definition.params.map(p => `params.${p.key}`));
  assert.deepEqual(switches.map(c => c.path), ['bypass', 'solo']);
  for (const control of ranges) assert.ok(control.max >= control.min && control.step > 0);
  for (const type of Object.keys(CATALOG_BY_TYPE)) {
    const limited = describeControls({type}, {duration: .5}).ranges.filter(c => c.max < CATALOG_BY_TYPE[type].params.find(p => `params.${p.key}` === c.path)?.max);
    for (const control of limited) assert.equal(control.max, Math.max(control.min, .5));
  }
  assert.deepEqual(describeControls({type: 'nope'}), {ranges: [], switches: []});
});

test('range scaling snaps to the control step and stays inside the range', () => {
  const control = {min: .04, max: 2, step: .01};
  assert.equal(normToValue(control, 0), .04);
  assert.equal(normToValue(control, 1), 2);
  assert.equal(normToValue(control, .5), 1.02);
  assert.equal(normToValue(control, -3), .04);
  assert.equal(normToValue(control, 9), 2);
  assert.equal(normToValue(control, NaN), .04);
  const taps = {min: 1, max: 8, step: 1};
  assert.deepEqual([0, .3, .5, 1].map(n => normToValue(taps, n)), [1, 3, 5, 8]);
  assert.equal(valueToNorm(taps, 8), 1);
  assert.equal(valueToNorm({min: 3, max: 3, step: 1}, 3), 0);
  for (let i = 0; i <= 200; i++) {
    const value = normToValue(control, i / 200);
    assert.ok(Math.abs(Math.round(value * 100) - value * 100) < 1e-6, `${value} is on the .01 grid`);
  }
});

test('an absolute knob drives the parameter it was bound to and no other', () => {
  const {node, view, apply} = rig('delay');
  const map = new ControllerMap([{source: {kind: 'cc', channel: 1, number: 21}, target: {nodeId: node.id, path: 'wet'}, pickup: false}]);
  assert.deepEqual(map.process(cc(21, 127), view).actions, [{nodeId: node.id, path: 'wet', value: 1, binding: 'cc:1:21'}].filter(a => a.value !== view.current(node.id, 'wet')));
  const result = map.process(cc(21, 0), view);
  assert.equal(result.actions[0].value, 0);
  apply(result.actions);
  assert.deepEqual(map.process(cc(21, 0), view).actions, [], 'an unchanged value emits nothing');
  assert.deepEqual(map.process(cc(22, 50), view).actions, [], 'unbound controllers are ignored');
  assert.deepEqual(map.process(cc(21, 50, 2), view).actions, [], 'a different channel is ignored');
});

test('channel 0 matches any channel and notes drive ranges by velocity', () => {
  const {node, view} = rig('delay');
  const map = new ControllerMap([
    {source: {kind: 'cc', channel: 0, number: 1}, target: {nodeId: node.id, path: 'intensity'}, pickup: false},
    {source: {kind: 'note', channel: 0, number: 50}, target: {nodeId: node.id, path: 'wet'}, pickup: false},
  ]);
  assert.equal(map.process(cc(1, 0, 9), view).actions[0].value, 0);
  assert.equal(map.process(note(50, 127, 4), view).actions.length, 0, 'wet is already 1');
  assert.equal(map.process(note(50, 64, 4), view).actions[0].value, .5);
  assert.deepEqual(map.process(note(50, 64, 4, false), view).actions, []);
});

test('soft takeover ignores the knob until it reaches or crosses the on-screen value', () => {
  const {node, view, apply, values} = rig('delay');
  values.set(`${node.id}|wet`, .5);
  const map = new ControllerMap([{source: {kind: 'cc', channel: 1, number: 21}, target: {nodeId: node.id, path: 'wet'}}]);
  const first = map.process(cc(21, 0), view);
  assert.deepEqual(first.actions, []); assert.equal(first.waiting.length, 1);
  assert.deepEqual(map.process(cc(21, 20), view).actions, []);
  const caught = map.process(cc(21, 90), view);
  assert.equal(caught.actions.length, 1, 'crossing .5 catches the value');
  apply(caught.actions);
  assert.equal(map.process(cc(21, 10), view).actions[0].value, normToValue({min: 0, max: 1, step: .01}, 10 / 127), 'once caught it follows freely');
  map.resetPickup();
  values.set(`${node.id}|wet`, .9);
  assert.deepEqual(map.process(cc(21, 10), view).actions, [], 'reset requires a new catch');
  assert.equal(map.process(cc(21, 114), view).actions.length, 0, 'landing exactly on the value changes nothing but catches it');
  assert.equal(map.process(cc(21, 100), view).actions.length, 1);
});

test('relative encoders step by signed ticks and clamp at the ends', () => {
  const {node, view, apply, values} = rig('delay');
  values.set(`${node.id}|intensity`, .5);
  const map = new ControllerMap([{source: {kind: 'cc', channel: 1, number: 10}, target: {nodeId: node.id, path: 'intensity'}, mode: 'relative'}]);
  let result = map.process(cc(10, 1), view); apply(result.actions);
  assert.equal(values.get(`${node.id}|intensity`), .51);
  result = map.process(cc(10, 127), view); apply(result.actions);
  assert.equal(values.get(`${node.id}|intensity`), .5);
  result = map.process(cc(10, 120), view); apply(result.actions);
  assert.equal(values.get(`${node.id}|intensity`), .42);
  assert.deepEqual(map.process(cc(10, 64), view).actions, []);
  assert.deepEqual(map.process(cc(10, 0), view).actions, []);
  for (let i = 0; i < 30; i++) apply(map.process(cc(10, 63), view).actions);
  assert.equal(values.get(`${node.id}|intensity`), 1);
  assert.deepEqual(map.process(cc(10, 5), view).actions, [], 'already at the maximum');
});

test('switches toggle on note-on, follow a momentary button, and drive transport', () => {
  const {node, view, apply, globals} = rig('delay');
  const map = new ControllerMap([
    {source: {kind: 'note', channel: 1, number: 36}, target: {nodeId: node.id, path: 'bypass'}},
    {source: {kind: 'cc', channel: 1, number: 64}, target: {nodeId: node.id, path: 'solo'}, mode: 'momentary'},
    {source: {kind: 'note', channel: 1, number: 38}, target: {global: 'play'}},
    {source: {kind: 'note', channel: 1, number: 39}, target: {global: 'loop'}},
  ]);
  let result = map.process(note(36, 100), view); assert.deepEqual(result.actions.map(a => [a.path, a.value]), [['bypass', true]]); apply(result.actions);
  assert.deepEqual(map.process(note(36, 0), view).actions, [], 'release does nothing');
  result = map.process(note(36, 100), view); assert.equal(result.actions[0].value, false); apply(result.actions);
  assert.equal(view.current(node.id, 'solo'), true, 'single-processor patches start soloed');
  result = map.process(cc(64, 0), view); assert.equal(result.actions[0].value, false); apply(result.actions);
  result = map.process(cc(64, 127), view); assert.equal(result.actions[0].value, true); apply(result.actions);
  result = map.process(note(38, 90), view); assert.deepEqual(result.actions.map(a => [a.global, a.value]), [['play', true]]); apply(result.actions);
  assert.equal(globals.get('play'), true);
  result = map.process(note(39, 90), view); assert.equal(result.actions[0].value, false);
  const toggleOnCC = new ControllerMap([{source: {kind: 'cc', channel: 1, number: 70}, target: {nodeId: node.id, path: 'bypass'}}]);
  assert.equal(toggleOnCC.process(cc(70, 10), view).actions.length, 0, 'a low CC value never flips a toggle');
  assert.equal(toggleOnCC.process(cc(70, 100), view).actions[0].value, true);
});

test('selected-processor bindings follow the selection and unknown slots stay dormant', () => {
  const patch = freshPatch('delay');
  const second = {...patch.nodes.find(n => CATALOG_BY_TYPE[n.type]), id: 'second', type: 'routing', params: {}};
  const nodes = [...patch.nodes, second];
  const base = {nodes, duration: 12, current: () => -1, global: () => false};
  const map = new ControllerMap([{source: {kind: 'cc', channel: 1, number: 30}, target: {nodeId: SELECTED, slot: 'knob:2'}, pickup: false}]);
  const delayId = patch.nodes.find(n => n.type === 'delay').id;
  const a = map.process(cc(30, 127), {...base, selectedId: delayId}).actions[0];
  assert.equal(a.nodeId, delayId); assert.equal(a.path, `params.${CATALOG_BY_TYPE.delay.params[0].key}`);
  const b = map.process(cc(30, 127), {...base, selectedId: 'second'}).actions[0];
  assert.equal(b.nodeId, 'second'); assert.equal(b.path, `params.${CATALOG_BY_TYPE.routing.params[0].key}`);
  assert.deepEqual(map.process(cc(30, 127), {...base, selectedId: 'missing'}).actions, []);
  assert.deepEqual(map.process(cc(30, 127), {...base, selectedId: patch.nodes.find(n => n.type === 'source').id}).actions, []);
  assert.equal(findControl({type: 'delay'}, {slot: 'switch:1'}).path, 'solo');
  assert.equal(findControl({type: 'delay'}, {slot: 'knob:99'}), null);
  assert.equal(findControl({type: 'delay'}, {slot: 'dial:0'}), null);
});

test('MIDI learn binds the next message, picks a sensible mode and replaces older bindings', () => {
  const {node, view} = rig('delay');
  const map = new ControllerMap();
  map.armLearn({nodeId: node.id, path: 'wet'});
  assert.deepEqual(map.process(note(40, 0, 1, false), view), {learned: null, actions: [], waiting: []}, 'a release does not bind');
  const learned = map.process(cc(7, 80), view).learned;
  assert.deepEqual(learned.source, {kind: 'cc', channel: 1, number: 7}); assert.equal(learned.mode, 'absolute'); assert.equal(map.learning, null);
  map.armLearn({nodeId: node.id, path: 'bypass'});
  assert.equal(map.process(note(36, 100), view).learned.mode, 'toggle');
  map.armLearn({nodeId: node.id, path: 'solo'});
  assert.equal(map.process(cc(80, 127), view).learned.mode, 'momentary');
  map.armLearn({global: 'record'}); map.process(note(37, 100), view);
  assert.equal(map.bindings.length, 4);
  map.armLearn({nodeId: node.id, path: 'wet'}); map.process(cc(8, 1), view);
  assert.equal(map.bindings.filter(b => b.target.path === 'wet').length, 1, 'one binding per target');
  assert.equal(map.bindings.find(b => b.target.path === 'wet').source.number, 8);
  map.armLearn({nodeId: node.id, path: 'intensity'}); map.process(cc(8, 1), view);
  assert.equal(map.bindings.filter(b => b.source.number === 8).length, 1, 'one binding per source');
  assert.equal(map.bindings.some(b => b.target.path === 'wet'), false);
  map.armLearn({nodeId: node.id, path: 'wet'}); map.cancelLearn();
  assert.equal(map.process(cc(9, 1), view).learned, null);
  assert.equal(map.unbindTarget({nodeId: node.id, path: 'intensity'}), 1);
  assert.equal(map.bindingForTarget({nodeId: node.id, path: 'intensity'}), null);
});

test('the default template maps eight knobs, two switches and transport for the selected processor', () => {
  const template = defaultTemplate({channel: 1, firstCC: 20, firstNote: 36});
  assert.equal(template.length, 13);
  const map = new ControllerMap(template);
  const {node, view, apply, values} = rig('feedback');
  const {ranges} = describeControls(node, view);
  assert.ok(ranges.length >= 8, 'every processor has intensity, wet and at least six parameters');
  const touched = [], initial = new Map(ranges.map(c => [c.path, view.current(node.id, c.path)]));
  for (let i = 0; i < 8; i++) {
    map.process(cc(20 + i, 0), view);               // sweep from the bottom so each knob picks up
    const toward = map.process(cc(20 + i, 127), view);
    for (const a of toward.actions) {touched.push(a.path); apply([a]);}
  }
  const expected = ranges.slice(0, 8).filter(c => initial.get(c.path) !== c.max).map(c => c.path);
  assert.deepEqual(touched.sort(), expected.sort());
  assert.ok(expected.length >= 6, 'most knobs moved');
  for (const control of ranges.slice(0, 8)) assert.equal(view.current(node.id, control.path), control.max);
  assert.ok(map.process(note(36, 100), view).actions.some(a => a.path === 'bypass'));
  assert.ok(map.process(note(37, 100), view).actions.some(a => a.path === 'solo'));
  assert.deepEqual(['play', 'record', 'loop'].map((name, i) => map.process(note(38 + i, 100), view).actions[0].global), ['play', 'record', 'loop']);
});

test('maps round-trip through JSON and imports are strictly validated', () => {
  const map = new ControllerMap(defaultTemplate());
  const json = JSON.parse(JSON.stringify(map));
  assert.equal(json.version, CONTROLLER_MAP_VERSION);
  assert.deepEqual(ControllerMap.fromJSON(json).toJSON(), json);
  const valid = json.bindings[0];
  const withBinding = changes => ({version: CONTROLLER_MAP_VERSION, bindings: [{...valid, ...changes}]});
  const rejected = [
    [null, /object/], [[], /object/], [{version: 'x', bindings: []}, /version/], [{version: CONTROLLER_MAP_VERSION, bindings: 'x'}, /list/],
    [{version: CONTROLLER_MAP_VERSION, bindings: Array.from({length: MAX_BINDINGS + 1}, (_, i) => ({...valid, source: {kind: 'cc', channel: 1, number: i % 128}}))}, /at most/],
    [withBinding({source: {kind: 'sysex', channel: 1, number: 1}}), /source/], [withBinding({source: {kind: 'cc', channel: 17, number: 1}}), /channel/],
    [withBinding({source: {kind: 'cc', channel: 1, number: 128}}), /number/], [withBinding({target: null}), /target/],
    [withBinding({target: {nodeId: '', path: 'wet'}}), /processor/], [withBinding({target: {nodeId: 'a', path: '__proto__'}}), /parameter/],
    [withBinding({target: {nodeId: 'a', path: 'params.x y'}}), /parameter/], [withBinding({target: {nodeId: 'a', slot: 'knob:abc'}}), /slot/],
    [withBinding({target: {global: 'format'}}), /transport/], [withBinding({mode: 'turbo'}), /mode/],
    [withBinding({mode: 'toggle'}), /Switches/], [withBinding({rate: 99}), /rate/], [withBinding({pickup: 'yes'}), /pickup/],
    [withBinding({target: {nodeId: 'x'.repeat(200), path: 'wet'}}), /processor/],
  ];
  for (const [input, pattern] of rejected) assert.throws(() => ControllerMap.fromJSON(input), pattern, JSON.stringify(input).slice(0, 80));
  assert.equal(new ControllerMap().process(null, {}).actions.length, 0);
});

test('one undo gesture covers a stream of controller messages', () => {
  const tracker = new GestureTracker(500);
  assert.equal(tracker.touch('k', 0), true);
  for (let t = 20; t <= 400; t += 20) assert.equal(tracker.touch('k', t), false);
  assert.equal(tracker.touch('other', 410), true);
  assert.equal(tracker.touch('k', 1000), true, 'a pause longer than the gap starts a new gesture');
  tracker.end('k');
  assert.equal(tracker.touch('k', 1010), true);
  for (let i = 0; i < 600; i++) tracker.touch(`key-${i}`, 5000 + i * 1000);
  assert.ok(tracker.last.size < 600, 'idle keys are pruned');
});

test('values are formatted for display', () => {
  assert.equal(formatValue({kind: 'range', unit: '%'}, .456), '46%');
  assert.equal(formatValue({kind: 'range', unit: 's'}, .48), '0.48 s');
  assert.equal(formatValue({kind: 'range', unit: ''}, 4), '4');
  assert.equal(formatValue({kind: 'range', unit: '', options: ['a', 'b']}, 1), 'b');
  assert.equal(formatValue({kind: 'toggle'}, true), 'On');
});

test('polyphonic aftertouch is ignored and never reads as a note release', () => {
  assert.equal(parseMidi([0xA0, 36, 100]), null);
  const {node, view} = rig('delay');
  const map = new ControllerMap([{source: {kind: 'note', channel: 1, number: 36}, target: {nodeId: node.id, path: 'bypass'}, mode: 'momentary'}]);
  assert.deepEqual(map.process(parseMidi([0xA0, 36, 100]), view).actions, []);
  assert.equal(map.process(note(36, 100), view).actions[0].value, true);
});

test('one-tick relative turns accumulate until they move a stepped parameter', () => {
  const {node, view, apply, values} = rig('delay');
  const taps = CATALOG_BY_TYPE.delay.params.find(p => p.key === 'taps');
  const path = 'params.taps';
  values.set(`${node.id}|${path}`, 3);
  const map = new ControllerMap([{source: {kind: 'cc', channel: 1, number: 11}, target: {nodeId: node.id, path}, mode: 'relative'}]);
  const moves = [];
  for (let i = 0; i < 40; i++) {const r = map.process(cc(11, 1), view); apply(r.actions); if (r.actions.length) moves.push(i);}
  assert.ok(moves.length >= 2, 'the encoder eventually moves the parameter');
  assert.ok(values.get(`${node.id}|${path}`) > 3 && values.get(`${node.id}|${path}`) <= taps.max);
  const up = values.get(`${node.id}|${path}`);
  for (let i = 0; i < 40; i++) apply(map.process(cc(11, 127), view).actions);
  assert.ok(values.get(`${node.id}|${path}`) < up, 'turning back moves it down');
  for (let i = 0; i < 400; i++) apply(map.process(cc(11, 127), view).actions);
  assert.equal(values.get(`${node.id}|${path}`), taps.min);
  assert.equal(map.pending.size, 0, 'no leftover movement at the end of the range');
});

test('soft takeover re-arms when the value is changed from elsewhere after the knob caught it', () => {
  const {node, view, apply, values} = rig('delay');
  const map = new ControllerMap([{source: {kind: 'cc', channel: 1, number: 21}, target: {nodeId: node.id, path: 'wet'}}]);
  values.set(`${node.id}|wet`, .5);
  map.process(cc(21, 60), view); apply(map.process(cc(21, 64), view).actions);
  const caughtAt = values.get(`${node.id}|wet`);
  assert.ok(caughtAt !== undefined);
  apply(map.process(cc(21, 80), view).actions);
  values.set(`${node.id}|wet`, .9);                         // a slider, preset, undo or automation moves it
  assert.deepEqual(map.process(cc(21, 82), view).actions, [], 'the knob no longer overwrites the restored value');
  assert.equal(map.process(cc(21, 114), view).actions.length, 0, 'reaching the new value catches it again');
  assert.equal(map.process(cc(21, 100), view).actions.length, 1);
});

test('learning a source replaces overlapping any-channel bindings', () => {
  const {node, view} = rig('delay');
  const map = new ControllerMap(defaultTemplate());
  map.armLearn({nodeId: node.id, path: 'wet'});
  map.process(cc(20, 50, 1), view);
  assert.equal(map.bindings.filter(b => b.source.kind === 'cc' && b.source.number === 20).length, 1);
  assert.equal(map.bindings.find(b => b.source.number === 20).target.path, 'wet');
  assert.equal(map.bindings.some(b => b.target.slot === 'knob:0'), false, 'the template binding for that CC is gone');
  const wildcard = new ControllerMap([{source: {kind: 'cc', channel: 3, number: 5}, target: {nodeId: node.id, path: 'wet'}}]);
  wildcard.bind({source: {kind: 'cc', channel: 0, number: 5}, target: {nodeId: node.id, path: 'intensity'}});
  assert.equal(wildcard.bindings.length, 1);
});
