import test from 'node:test';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import {controller} from './runtime.mjs';

const surfaceURL = pathToFileURL(resolve(import.meta.dirname, '../../web/fx/controller-surface.mjs'));
const {connectMidi} = await import(surfaceURL);

function fakeAccess(devices) {
  const inputs = new Map(devices.map(d => [d.id, {state: 'connected', onmidimessage: null, ...d}]));
  return {inputs, onstatechange: null};
}

test('Web MIDI is requested without system-exclusive access and every input is listened to', async () => {
  const access = fakeAccess([{id: 'a', name: 'Pad'}, {id: 'b', name: 'Knobs'}]);
  let options, seen = [], statuses = [];
  const link = await connectMidi({request: async o => {options = o; return access;}, onMessage: (data, input) => seen.push([input.id, [...data]]), onStatus: s => statuses.push(s)});
  assert.deepEqual(options, {sysex: false});
  assert.deepEqual(statuses.at(-1).inputs, ['Pad', 'Knobs']);
  access.inputs.get('a').onmidimessage({data: [0xB0, 20, 100]});
  access.inputs.get('b').onmidimessage({data: new Uint8Array([0x90, 36, 90])});
  assert.deepEqual(seen, [['a', [0xB0, 20, 100]], ['b', [0x90, 36, 90]]]);
  assert.deepEqual(link.inputs(), ['Pad', 'Knobs']);
  link.close();
  assert.equal(access.inputs.get('a').onmidimessage, null);
  assert.equal(access.onstatechange, null);
});

test('a controller plugged in later is picked up and a removed one is dropped', async () => {
  const access = fakeAccess([{id: 'a', name: 'Pad'}]);
  const statuses = [];
  await connectMidi({request: async () => access, onMessage() {}, onStatus: s => statuses.push(s.inputs)});
  access.inputs.set('c', {id: 'c', name: 'Late', state: 'connected', onmidimessage: null});
  access.onstatechange();
  assert.equal(typeof access.inputs.get('c').onmidimessage, 'function');
  assert.deepEqual(statuses.at(-1), ['Pad', 'Late']);
  access.inputs.get('a').state = 'disconnected';
  access.onstatechange();
  assert.deepEqual(statuses.at(-1), ['Late']);
  access.inputs.get('a').state = 'connected';
  access.onstatechange();
  assert.equal(typeof access.inputs.get('a').onmidimessage, 'function', 'a re-plugged device is listened to again');
});

test('missing Web MIDI support fails with a clear, non-fatal message', async () => {
  await assert.rejects(connectMidi({request: undefined, onMessage() {}}), /does not offer Web MIDI/);
  await assert.rejects(connectMidi({request: async () => {throw Error('Permission denied');}, onMessage() {}}), /Permission denied/);
});

test('messages from a live device flow through parsing into rack actions', async () => {
  const access = fakeAccess([{id: 'a', name: 'Pad'}]);
  const map = new controller.ControllerMap(controller.defaultTemplate());
  const view = {nodes: [{id: 'n', type: 'delay'}], selectedId: 'n', duration: 12, current: () => false, global: () => false};
  const actions = [];
  await connectMidi({request: async () => access, onMessage: data => actions.push(...map.process(controller.parseMidi(data), view).actions)});
  access.inputs.get('a').onmidimessage({data: [0x90, 36, 100]});
  access.inputs.get('a').onmidimessage({data: [0xF8]});
  assert.deepEqual(actions.map(a => [a.path, a.value]), [['bypass', true]]);
});
