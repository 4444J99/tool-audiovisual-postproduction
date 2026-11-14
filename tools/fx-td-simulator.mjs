/**
 * Stand-in for TouchDesigner when exercising the FX bridge end to end.
 *
 *   node tools/fx-td-simulator.mjs result.json [--td-port 7000] [--osc-port 7001] [--wait-seconds 120]
 *
 * Listens where TouchDesigner would (the bridge's OSC out), waits for the workbench page to open
 * the link (the rack reports `/fx/count`), then sends a fixed control sequence into the bridge's
 * OSC in and records everything the rack reports back. Ill-formed messages are included on purpose:
 * the link must survive them. Exit status 1 when the link never opened or a check fails.
 */
import dgram from 'node:dgram';
import fs from 'node:fs';
import {encodeMessage, decodePacket} from './fx-bridge/osc-codec.mjs';

const option = (name, fallback) => { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : fallback; };
const resultPath = process.argv[2];
if (!resultPath || resultPath.startsWith('--')) { console.error('usage: node tools/fx-td-simulator.mjs result.json'); process.exit(2); }
const tdPort = Number(option('--td-port', 7000)), oscPort = Number(option('--osc-port', 7001)), waitSeconds = Number(option('--wait-seconds', 120));

const socket = dgram.createSocket('udp4'), log = [], t0 = Date.now();
socket.on('message', buffer => { try { for (const m of decodePacket(new Uint8Array(buffer))) log.push({t: Date.now() - t0, address: m.address, args: m.args}); } catch (error) { log.push({error: String(error)}); } });
await new Promise((resolve, reject) => { socket.once('error', reject); socket.bind(tdPort, '127.0.0.1', resolve); });
const send = (address, args) => new Promise(resolve => socket.send(Buffer.from(encodeMessage(address, args)), oscPort, '127.0.0.1', resolve));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const until = async (predicate, ms) => { const end = Date.now() + ms; while (Date.now() < end) { if (predicate()) return true; await wait(100); } return false; };
const reported = (from, address, check = () => true) => log.slice(from).some(m => m.address === address && check(m.args));

const checks = [], note = (name, pass) => checks.push({name, pass: !!pass});
// The page may have linked before this process bound its port, so ask for the state again until it answers.
const ticker = setInterval(() => send('/fx/sync', []), 2000);
const linked = await until(() => log.some(m => m.address === '/fx/count'), waitSeconds * 1000);
clearInterval(ticker);
note('link opened and rack reported its state', linked);
if (linked) {
  await wait(500);
  const last = address => log.filter(m => m.address === address).at(-1)?.args?.[0];
  const selected = last('/fx/selected');
  // Targets differ from the reported state, because a value that repeats the rack's own report is an echo, not an edit.
  const intensity = (last(`/fx/node/${selected}/intensity`) ?? 1) < 0.5 ? 0.75 : 0.25, bypass = last(`/fx/node/${selected}/bypass`) ? 0 : 1, loop = last('/fx/transport/loop') ? 0 : 1;
  let mark = log.length;
  await send('/fx/selected/knob/0', [intensity]); await wait(1500);
  note(`knob 0 sets Intensity of the selected processor and the rack reports ${intensity}`, reported(mark, `/fx/node/${selected}/intensity`, a => Math.abs(a[0] - intensity) < 2e-4));
  mark = log.length; await send('/fx/selected/switch/0', [bypass]); await wait(1500);
  note(`switch 0 sets Bypass to ${bypass}`, reported(mark, `/fx/node/${selected}/bypass`, a => a[0] === bypass));
  mark = log.length; await send('/fx/transport/loop', [loop]); await wait(1500);
  note(`transport loop becomes ${loop}`, reported(mark, '/fx/transport/loop', a => a[0] === loop));
  await send('/fx/bogus/address', [0.5]); await send('/fx/selected/knob/0', ['wrong-type']); await send('/fx/node/no-such-node/intensity', [0.5]); await wait(1000);
  mark = log.length; await send('/fx/sync', []); await wait(1500);
  note('link survives unknown address, wrong type and unknown id (rack still answers /fx/sync)', reported(mark, '/fx/count'));
  note('ignored messages did not change Intensity', last(`/fx/node/${selected}/intensity`) === undefined || Math.abs(last(`/fx/node/${selected}/intensity`) - intensity) < 2e-4);
}
const result = {schema: 'amps.fx-bridge-e2e.v1', linked, checks, passed: checks.length > 0 && checks.every(c => c.pass), reported: log.length, sample: log.slice(-10)};
fs.writeFileSync(resultPath, JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify({passed: result.passed, checks}, null, 2));
socket.close();
process.exit(result.passed ? 0 : 1);
