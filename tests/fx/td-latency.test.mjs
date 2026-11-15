import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {interpretOsc, OscFeedback, MAX_LATENCY} from '../../web/fx/osc-control.mjs';
import {oscSession, freshPatch, CATALOG_BY_TYPE} from './runtime.mjs';
import {buildNetworkSpec, validateNetworkSpec} from '../../tools/td-network/network-spec.mjs';

const patch = name => JSON.parse(readFileSync(new URL(`../../web/fx/fixtures/${name}.json`, import.meta.url), 'utf8'));
const view = {nodes: [], selectedId: null, time: 0, current: () => 0, global: () => false, duration: 12};

test('the workbench accepts a latency from the network and refuses anything that is not a duration', () => {
  assert.equal(interpretOsc({address: '/fx/latency', args: [2.5]}, view).latency, 2.5);
  assert.equal(interpretOsc({address: '/fx/latency', args: [0]}, view).latency, 0);
  for (const args of [[], [-1], [MAX_LATENCY + 1], ['2'], [NaN], [Infinity], [1, 2]]) {
    const result = interpretOsc({address: '/fx/latency', args}, view);
    assert.equal(result.ignored, true, JSON.stringify(args));
    assert.equal(result.latency, undefined);
  }
  assert.equal(interpretOsc({address: '/fx/latency/extra', args: [1]}, view).ignored, true);
});

test('the rack reports its playhead ahead by the latency, and a reconnect forgets it', () => {
  const feedback = new OscFeedback();
  const time = () => feedback.collect({...view, time: 3, fx: [], nodes: []}, {}).find(m => m.address === '/fx/time')?.args[0];
  assert.equal(time(), 3);
  feedback.setLatency(2.5);
  assert.equal(time(), 5.5, 'the next report carries the advanced playhead even though the playhead did not move');
  assert.equal(time(), undefined, 'and then it is quiet again');
  feedback.reset();
  assert.equal(feedback.latency, 0);
  assert.equal(time(), 3);
});

test('the latency in a generated contract is the address the workbench interprets, and every value it can take is accepted', () => {
  for (const name of ['demo-reverse', 'demo-stutter', 'closed-inbox']) {
    const spec = buildNetworkSpec(patch(name));
    assert.deepEqual(validateNetworkSpec(spec), [], name);
    const {address, seconds} = spec.contract.latency;
    assert.equal(address, '/fx/latency');
    assert.equal(interpretOsc({address, args: [seconds]}, view).latency, seconds, name);
  }
  assert.equal(buildNetworkSpec(patch('demo-reverse')).contract.latency.seconds, 2.5);
  assert.equal(buildNetworkSpec(patch('demo-stutter')).contract.latency.seconds, 1);
  assert.equal(buildNetworkSpec(patch('closed-inbox')).contract.latency.seconds, 0);
});

test('maxLatency caps the declared latency and the generator refuses one that is not a duration', () => {
  assert.equal(buildNetworkSpec(patch('demo-reverse'), {maxLatency: 1.2}).contract.latency.seconds, 1.2);
  assert.deepEqual(buildNetworkSpec(patch('demo-reverse'), {maxLatency: 1.2}).contract.nodes.find(n => n.id === 'fx-1').resolve.limits, {span: 1.2});
  assert.throws(() => buildNetworkSpec(patch('demo-reverse'), {maxLatency: 0}), /maxLatency/);
  assert.throws(() => buildNetworkSpec(patch('demo-reverse'), {maxLatency: -1}), /maxLatency/);
});

test('a session that is told the latency pushes the advanced playhead to the network at once', () => {
  const sent = [];
  class FakeSocket {
    constructor() {this.readyState = 1;}
    send(text) {sent.push(JSON.parse(text));}
    close() {}
  }
  const patchValue = freshPatch('delay');
  const node = patchValue.nodes.find(n => CATALOG_BY_TYPE[n.type]);
  const state = {time: 4};
  const host = {getView: () => ({nodes: [node], selectedId: node.id, duration: 12, time: state.time, current: (id, path) => path === 'bypass' || path === 'solo' ? false : path.startsWith('params.') ? node.params[path.slice(7)] : node[path], global: () => false}),
    apply() {}, gesture() {}, select() {}, selectNode() {}};
  const timers = {setInterval: () => 1, clearInterval() {}, setTimeout: () => 1, clearTimeout() {}};
  const session = new oscSession.OscSession(host, {WebSocketImpl: FakeSocket, timers, onStatus() {}});
  session.socket = new FakeSocket();
  session.pump({full: true});
  assert.equal(sent.filter(frame => frame.address === '/fx/time').at(-1).args[0], 4);
  sent.length = 0;
  session.handle(JSON.stringify({address: '/fx/latency', args: [1]}));
  assert.equal(sent.filter(frame => frame.address === '/fx/time').at(-1).args[0], 5, JSON.stringify(sent));
  assert.equal(session.received, 1);
});
