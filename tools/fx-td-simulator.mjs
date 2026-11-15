/**
 * Stand-in for TouchDesigner when exercising the FX bridge end to end.
 *
 *   node tools/fx-td-simulator.mjs result.json [--td-port 7000] [--osc-port 7001] [--wait-seconds 120] [--spec network.json]
 *
 * With --spec (a network spec from tools/fx-td-network.mjs) the checks come from the generated network's
 * own OSC contract instead of the fixed sequence: every processor in the spec has its first range control and
 * its Bypass switch driven by the address the network would send, and each result is checked against what the
 * rack reports back. The addresses, ranges and ids are the ones the TouchDesigner network was built from.
 *
 * Listens where TouchDesigner would (the bridge's OSC out), waits for the workbench page to open
 * the link (the rack reports `/fx/count`), then sends a fixed control sequence into the bridge's
 * OSC in and records everything the rack reports back. Ill-formed messages are included on purpose:
 * the link must survive them. Exit status 1 when the link never opened or a check fails.
 */
import dgram from 'node:dgram';
import fs from 'node:fs';
import path from 'node:path';
import {encodeMessage, decodePacket} from './fx-bridge/osc-codec.mjs';
import {nodeAddress} from '../web/fx/osc-control.mjs';

const option = (name, fallback) => { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : fallback; };
const resultPath = process.argv[2];
if (!resultPath || resultPath.startsWith('--')) { console.error('usage: node tools/fx-td-simulator.mjs result.json'); process.exit(2); }
const specPath = option('--spec');
const spec = specPath ? JSON.parse(fs.readFileSync(specPath, 'utf8')) : null;
const tdPort = Number(option('--td-port', 7000)), oscPort = Number(option('--osc-port', 7001)), waitSeconds = Number(option('--wait-seconds', 120));

const socket = dgram.createSocket('udp4'), log = [], t0 = Date.now();
socket.on('message', buffer => { try { for (const m of decodePacket(new Uint8Array(buffer))) log.push({t: Date.now() - t0, address: m.address, args: m.args}); } catch (error) { log.push({error: String(error)}); } });
await new Promise((resolve, reject) => { socket.once('error', reject); socket.bind(tdPort, '127.0.0.1', resolve); });
const send = (address, args) => new Promise(resolve => socket.send(Buffer.from(encodeMessage(address, args)), oscPort, '127.0.0.1', resolve));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const until = async (predicate, ms) => { const end = Date.now() + ms; while (Date.now() < end) { if (predicate()) return true; await wait(100); } return false; };
const reportedSince = (from, address, check = () => true) => log.slice(from).some(m => m.address === address && check(m.args));
const reported = (from, address, check = () => true) => log.slice(from).some(m => m.address === address && check(m.args));

const checks = [], note = (name, pass) => checks.push({name, pass: !!pass});
// The page may have linked before this process bound its port, so ask for the state again until it answers.
const ticker = setInterval(() => send('/fx/sync', []), 2000);
const linked = await until(() => log.some(m => m.address === '/fx/count'), waitSeconds * 1000);
clearInterval(ticker);
note('link opened and rack reported its state', linked);
if (linked && spec) {
  await wait(500);
  const last = address => log.filter(m => m.address === address).at(-1)?.args?.[0];
  const nodes = spec.contract.nodes;
  note(`rack reports ${nodes.length} processors, as the network was generated for`, last(spec.contract.rack.count.address) === nodes.length);
  for (const node of nodes) {
    note(`${node.id}: the rack reports its type ${node.type}`, last(node.typeAddress) === node.type);
    const range = node.controls.find(c => c.kind === 'range' && c.path === 'intensity');
    const bypass = node.controls.find(c => c.path === 'bypass');
    const reported = last(range.address) ?? 1, target = reported < 0.5 ? 0.75 : 0.25;
    let mark = log.length; await send(range.address, [target]); await wait(700);
    note(`${node.id}: ${range.address} = ${target} is reported back`, reportedSince(mark, range.address, a => Math.abs(a[0] - target) < 2e-4));
    const flip = last(bypass.address) ? 0 : 1;
    mark = log.length; await send(bypass.address, [flip]); await wait(700);
    note(`${node.id}: ${bypass.address} = ${flip} is reported back`, reportedSince(mark, bypass.address, a => a[0] === flip));
  }
  const loop = spec.contract.rack.transport.find(t => t.name === 'loop'), next = last(loop.address) ? 0 : 1;
  let mark = log.length; await send(loop.address, [next]); await wait(700);
  note(`transport loop becomes ${next}`, reportedSince(mark, loop.address, a => a[0] === next));
  await send('/fx/bogus/address', [0.5]); if (nodes.length) await send(nodes[0].controls[0].address, ['wrong-type']); await send('/fx/node/no-such-node/intensity', [0.5]); await wait(800);
  mark = log.length; await send(spec.contract.rack.sync.address, []); await wait(1200);
  note('link survives unknown address, wrong type and unknown id (rack still answers sync)', reportedSince(mark, spec.contract.rack.count.address));
} else if (linked) {
  await wait(500);
  const last = address => log.filter(m => m.address === address).at(-1)?.args?.[0];
  const selected = last('/fx/selected');
  // Targets differ from the reported state, because a value that repeats the rack's own report is an echo, not an edit.
  const intensity = (last(nodeAddress(selected, 'intensity')) ?? 1) < 0.5 ? 0.75 : 0.25, bypass = last(nodeAddress(selected, 'bypass')) ? 0 : 1, loop = last('/fx/transport/loop') ? 0 : 1;
  let mark = log.length;
  await send('/fx/selected/knob/0', [intensity]); await wait(1500);
  note(`knob 0 sets Intensity of the selected processor and the rack reports ${intensity}`, reported(mark, nodeAddress(selected, 'intensity'), a => Math.abs(a[0] - intensity) < 2e-4));
  mark = log.length; await send('/fx/selected/switch/0', [bypass]); await wait(1500);
  note(`switch 0 sets Bypass to ${bypass}`, reported(mark, nodeAddress(selected, 'bypass'), a => a[0] === bypass));
  mark = log.length; await send('/fx/transport/loop', [loop]); await wait(1500);
  note(`transport loop becomes ${loop}`, reported(mark, '/fx/transport/loop', a => a[0] === loop));
  await send('/fx/bogus/address', [0.5]); await send('/fx/selected/knob/0', ['wrong-type']); await send('/fx/node/no-such-node/intensity', [0.5]); await wait(1000);
  mark = log.length; await send('/fx/sync', []); await wait(1500);
  note('link survives unknown address, wrong type and unknown id (rack still answers /fx/sync)', reported(mark, '/fx/count'));
  note('ignored messages did not change Intensity', last(nodeAddress(selected, 'intensity')) === undefined || Math.abs(last(nodeAddress(selected, 'intensity')) - intensity) < 2e-4);
}
const result = {schema: 'amps.fx-bridge-e2e.v1', linked, checks, passed: checks.length > 0 && checks.every(c => c.pass), reported: log.length, sample: log.slice(-10)};
fs.mkdirSync(path.dirname(path.resolve(resultPath)), {recursive: true});
fs.writeFileSync(resultPath, JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify({passed: result.passed, checks}, null, 2));
socket.close();
process.exit(result.passed ? 0 : 1);
