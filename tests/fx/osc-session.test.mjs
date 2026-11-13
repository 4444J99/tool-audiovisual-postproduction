import test from 'node:test';
import assert from 'node:assert/strict';
import {oscSession, freshPatch, CATALOG_BY_TYPE} from './runtime.mjs';

const {OscSession, checkBridgeUrl, DEFAULT_BRIDGE_URL, updateOscPrefs} = oscSession;

class FakeSocket {
  static instances = [];
  constructor(url) {this.url = url; this.readyState = 0; this.sent = []; FakeSocket.instances.push(this);}
  send(text) {this.sent.push(JSON.parse(text));}
  close() {this.readyState = 3; this.closedByClient = true;}
  open() {this.readyState = 1; this.onopen?.();}
  receive(data) {this.onmessage?.({data: typeof data === 'string' ? data : JSON.stringify(data)});}
  drop() {this.readyState = 3; this.onclose?.();}
}

function fakeTimers() {
  const pending = new Map(); let next = 1;
  return {
    pending,
    setTimeout: (fn, ms) => {pending.set(next, {fn, ms, repeat: false}); return next++;},
    setInterval: (fn, ms) => {pending.set(next, {fn, ms, repeat: true}); return next++;},
    clearTimeout: id => pending.delete(id), clearInterval: id => pending.delete(id),
    fire(kind) {for (const [id, t] of [...pending]) if (t.repeat === (kind === 'interval')) {if (!t.repeat) pending.delete(id); t.fn();}},
    delays: () => [...pending.values()].filter(t => !t.repeat).map(t => t.ms),
  };
}

function rig() {
  FakeSocket.instances.length = 0;
  const patch = freshPatch('delay');
  const node = patch.nodes.find(n => CATALOG_BY_TYPE[n.type]);
  node.solo = false; node.bypass = false;
  const values = new Map(), applied = [], gestures = [], selected = [];
  const view = {
    nodes: [node], selectedId: node.id, duration: 12, time: 0,
    current: (id, path) => values.has(`${id}|${path}`) ? values.get(`${id}|${path}`) : path === 'bypass' || path === 'solo' ? Boolean(node[path]) : path.startsWith('params.') ? node.params[path.slice(7)] : node[path],
    global: () => false,
  };
  const host = {
    getView: () => view,
    apply(action) {applied.push(action); if (!action.global) values.set(`${action.nodeId}|${action.path}`, action.value);},
    gesture: key => gestures.push(key), select: delta => selected.push({delta}), selectNode: id => selected.push({id}),
  };
  const timers = fakeTimers(), statuses = [];
  const session = new OscSession(host, {WebSocketImpl: FakeSocket, timers, onStatus: s => statuses.push(s)});
  return {session, host, view, node, values, applied, gestures, selected, timers, statuses, socket: () => FakeSocket.instances.at(-1)};
}

test('bridge addresses must be plain WebSocket URLs', () => {
  assert.equal(checkBridgeUrl(DEFAULT_BRIDGE_URL), 'ws://127.0.0.1:9120/');
  assert.equal(checkBridgeUrl(' wss://studio.example/fx '), 'wss://studio.example/fx');
  for (const bad of ['', 'localhost:9120', 'http://127.0.0.1:9120', 'javascript:alert(1)', 'ws://user:pw@host/', 'ws://host/#frag']) assert.throws(() => checkBridgeUrl(bad), Error, bad);
});

test('opening the link sends the full rack state, then a timer pushes changes', () => {
  const {session, socket, timers, node, values} = rig();
  assert.equal(session.connect('ws://127.0.0.1:9120'), true);
  assert.equal(socket().url, 'ws://127.0.0.1:9120/');
  socket().open();
  const first = socket().sent;
  assert.ok(first.some(m => m.address === '/fx/count'));
  assert.ok(first.some(m => m.address === '/fx/selected' && m.args[0] === node.id));
  const before = socket().sent.length;
  timers.fire('interval');
  assert.equal(socket().sent.length, before, 'idle polling sends nothing');
  values.set(`${node.id}|intensity`, 0.4);
  timers.fire('interval');
  assert.deepEqual(socket().sent.slice(before), [{address: `/fx/node/${node.id}/intensity`, args: [0.4]}]);
});

test('inbound OSC drives the rack through host.apply with a gesture key', () => {
  const {session, socket, applied, gestures, node} = rig();
  session.connect('ws://127.0.0.1:9120'); socket().open();
  socket().receive({address: '/fx/selected/knob/0', args: [0.5]});
  assert.deepEqual(applied, [{nodeId: node.id, path: 'intensity', value: 0.5, binding: 'osc'}]);
  assert.deepEqual(gestures, [`osc:${node.id}:intensity`]);
  socket().receive({address: '/fx/transport/play', args: [1]});
  assert.deepEqual(applied.at(-1), {global: 'play', value: true, binding: 'osc'});
  assert.equal(gestures.at(-1), 'global:play');
  assert.equal(session.received, 2);
});

test('selection, sync and the bridge greeting', () => {
  const {session, socket, selected, statuses, node} = rig();
  session.connect('ws://127.0.0.1:9120'); socket().open();
  socket().receive({address: '/fx/select/next', args: []});
  socket().receive({address: '/fx/selected', args: [node.id]});
  assert.deepEqual(selected, [{delta: 1}, {id: node.id}]);
  const before = socket().sent.length;
  socket().receive({address: '/fx/sync', args: []});
  assert.ok(socket().sent.length > before + 5, 'sync resends everything');
  socket().receive({bridge: 'fx-td', version: 1, touchdesigner: {host: '127.0.0.1', port: 7000}});
  assert.equal(statuses.at(-1).bridge.version, 1);
});

test('malformed frames are ignored and never reach the rack', () => {
  const {session, socket, applied, selected} = rig();
  session.connect('ws://127.0.0.1:9120'); socket().open();
  for (const data of ['not json', '[]', '{}', '{"address":5,"args":[]}', '{"address":"/fx/selected/knob/0"}', '{"address":"/fx/selected/knob/0","args":[{"a":1}]}',
    JSON.stringify({address: '/fx/selected/knob/0', args: new Array(65).fill(1)}), 'x'.repeat(70000), 42, null]) {
    socket().receive(data);
  }
  assert.deepEqual(applied, []);
  assert.deepEqual(selected, []);
  assert.equal(session.received, 0);
});

test('feedback can be turned off and back on', () => {
  const {session, socket, timers, node, values} = rig();
  session.connect('ws://127.0.0.1:9120'); socket().open();
  session.setFeedback(false);
  const before = socket().sent.length;
  values.set(`${node.id}|wet`, 0.1);
  timers.fire('interval');
  assert.equal(socket().sent.length, before);
  session.setFeedback(true);
  timers.fire('interval');
  assert.ok(socket().sent.length > before, 're-enabling resends the state');
});

test('a dropped link retries with a growing, capped delay and resumes with a full resend', () => {
  const {session, socket, timers} = rig();
  session.connect('ws://127.0.0.1:9120');
  const delays = [];
  for (let i = 0; i < 7; i++) {
    socket().drop();
    delays.push(timers.delays()[0]);
    timers.fire('timeout');
  }
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000, 8000, 8000]);
  socket().open();
  assert.ok(socket().sent.some(m => m.address === '/fx/count'));
  socket().drop();
  assert.equal(timers.delays()[0], 500, 'a successful connection resets the back-off');
});

test('disconnect stops retries, polling and listeners', () => {
  const {session, socket, timers, applied, statuses} = rig();
  session.connect('ws://127.0.0.1:9120'); socket().open();
  const first = socket();
  session.disconnect();
  assert.equal(first.closedByClient, true);
  assert.equal(timers.pending.size, 0);
  first.receive({address: '/fx/selected/knob/0', args: [1]});
  first.drop();
  assert.deepEqual(applied, []);
  assert.equal(timers.pending.size, 0);
  assert.equal(statuses.at(-1).state, 'off');
  assert.equal(session.wanted, false);
});

test('reconnecting to a new address replaces the old socket', () => {
  const {session, socket} = rig();
  session.connect('ws://127.0.0.1:9120'); socket().open();
  const first = socket();
  session.connect('ws://127.0.0.1:9999');
  assert.equal(first.closedByClient, true);
  assert.equal(socket().url, 'ws://127.0.0.1:9999/');
  first.drop();
  assert.equal(session.state, 'connecting', 'the replaced socket cannot disturb the new link');
});

test('without WebSocket support the session reports it and stays off', () => {
  const statuses = [];
  const session = new OscSession({getView() {}}, {WebSocketImpl: null, onStatus: s => statuses.push(s)});
  assert.equal(session.connect('ws://127.0.0.1:9120'), false);
  assert.equal(statuses.at(-1).state, 'unsupported');
});

test('a constructor that throws is retried rather than crashing the page', () => {
  const timers = fakeTimers(), statuses = [];
  const session = new OscSession({getView() {}}, {WebSocketImpl: class {constructor() {throw Error('blocked by policy');}}, timers, onStatus: s => statuses.push(s)});
  session.connect('ws://127.0.0.1:9120');
  assert.equal(statuses.at(-1).state, 'retrying');
  assert.match(statuses.at(-1).detail, /blocked by policy/);
  assert.equal(timers.delays().length, 1);
});

test('saved OSC preferences merge: a feedback change keeps the url and connect choice and other preferences', () => {
  let saved = {connect: true, osc: {url: 'ws://127.0.0.1:9120/', connect: true, feedback: true}};
  const storage = {get: () => structuredClone(saved), set: value => {saved = structuredClone(value);}};
  updateOscPrefs(storage, {feedback: false});
  assert.deepEqual(saved, {connect: true, osc: {url: 'ws://127.0.0.1:9120/', connect: true, feedback: false}});
  updateOscPrefs(storage, {connect: false});
  assert.deepEqual(saved.osc, {url: 'ws://127.0.0.1:9120/', connect: false, feedback: false});
  saved = {};
  updateOscPrefs(storage, {url: 'ws://x/', connect: true, feedback: true});
  assert.deepEqual(saved, {osc: {url: 'ws://x/', connect: true, feedback: true}});
  assert.doesNotThrow(() => updateOscPrefs(undefined, {feedback: false}));
  assert.doesNotThrow(() => updateOscPrefs({get() {throw Error('blocked');}, set() {}}, {feedback: false}));
});
