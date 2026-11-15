import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, readdirSync, mkdtempSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {oscControl, controller, createPatch, PATCH_CATALOG, CATALOG, freshPatch, validatePatch} from './runtime.mjs';
import {buildNetworkSpec, validateNetworkSpec, operatorName, customParName} from '../../tools/td-network/network-spec.mjs';
import {serialise, buildScript} from '../../tools/td-network/emit.mjs';
import {checkVocabulary} from '../../tools/td-network/check-vocabulary.mjs';
import {VOCABULARY} from '../../tools/td-network/vocabulary.mjs';

const {interpretOsc, OscFeedback, encodeSegment} = oscControl;
const here = dirname(fileURLToPath(import.meta.url));
const fixtureDir = join(here, '../../web/fx/fixtures');
const fixtures = readdirSync(fixtureDir).filter(name => /^(patch-demo-.*|closed-inbox|correction-machine|fractured-utterance|message-impact|out-but-still-here|room-takeover)\.json$/.test(name)).sort();
const patches = [
  ...fixtures.map(name => ({label: name, patch: JSON.parse(readFileSync(join(fixtureDir, name), 'utf8'))})),
  ...PATCH_CATALOG.map(entry => ({label: `factory ${entry.id}`, patch: createPatch(entry.id)})),
  ...CATALOG.map(definition => ({label: `single ${definition.type}`, patch: freshPatch(definition.type)})),
];

/** The workbench's own view of a patch, as the page would give it to the OSC layer. */
function viewOf(patch, selectedId) {
  const nodes = patch.nodes.filter(node => oscControl && !['source', 'mix', 'output'].includes(node.type));
  const values = new Map();
  return {
    values, nodes, selectedId: selectedId ?? nodes[0]?.id, duration: patch.duration, time: 0,
    current(id, path) {
      const key = `${id}|${path}`;
      if (values.has(key)) return values.get(key);
      const node = nodes.find(item => item.id === id);
      return path === 'bypass' || path === 'solo' ? Boolean(node[path]) : path.startsWith('params.') ? node.params[path.slice(7)] : node[path];
    },
    globals: new Map([['loop', true]]),
    global(name) { return this.globals.get(name) ?? false; },
  };
}
const probe = (request) => {
  const result = spawnSync('python3', ['-I', join(here, 'td_probe.py')], {input: JSON.stringify(request), encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
};
const moduleSource = join(here, '../../tools/td-network/python/fx_td_osc.py');

test('every factory, demo and single-processor patch yields a sound spec', () => {
  assert.ok(patches.length >= 30);
  for (const {label, patch} of patches) {
    const spec = buildNetworkSpec(patch);
    assert.deepEqual(validateNetworkSpec(spec), [], label);
    assert.equal(spec.contract.nodes.length, patch.nodes.filter(n => !['source', 'mix', 'output'].includes(n.type)).length, label);
  }
});

test('the spec and the build script are byte-identical across runs', () => {
  for (const {label, patch} of patches.slice(0, 8)) {
    const first = buildNetworkSpec(structuredClone(patch)), second = buildNetworkSpec(structuredClone(patch));
    assert.equal(serialise(first), serialise(second), label);
    assert.equal(buildScript(first), buildScript(second), label);
  }
});

test('every connection becomes exactly one select wired to the upstream port, in connection order', () => {
  for (const {label, patch} of patches) {
    const spec = buildNetworkSpec(patch), byPath = new Map(spec.operators.map(o => [o.path, o]));
    assert.equal(spec.wires.length, patch.connections.length, label);
    for (const edge of patch.connections) {
      const wire = spec.wires.find(w => w.from === edge.from && w.to === edge.to && w.port === edge.port);
      assert.ok(wire, `${label}: ${edge.from}->${edge.to} ${edge.port}`);
      assert.equal(wire.role, edge.role); assert.equal(wire.gain, edge.gain);
      const select = byPath.get(wire.select);
      assert.match(select.type, /^select(TOP|CHOP|DAT)$/);
      assert.equal(Object.values(select.pars)[0].ref, wire.source);
      assert.match(wire.source, new RegExp(`/out_${edge.port}$`));
      if (edge.gain !== 1 && edge.port !== 'text') {
        const gain = spec.operators.find(o => o.parent === select.parent && o.inputs.includes(select.name) && /^gain_/.test(o.name));
        assert.ok(gain, `${label}: gain operator for ${edge.from}->${edge.to}`);
        assert.equal(Object.values(gain.pars)[0], edge.gain);
      }
    }
  }
});

test('every catalog parameter has a custom parameter with the catalog range, step and default', () => {
  for (const definition of CATALOG) {
    const spec = buildNetworkSpec(freshPatch(definition.type));
    const comp = spec.operators.find(o => o.node?.type === definition.type);
    const node = spec.contract.nodes[0];
    assert.equal(node.comp, comp.name);
    for (const param of definition.params) {
      const control = node.controls.find(c => c.path === `params.${param.key}`);
      const par = comp.customPars.find(p => p.name === customParName(param.key));
      assert.ok(control && par, `${definition.type}.${param.key}`);
      assert.equal(par.min, control.min); assert.equal(par.max, control.max);
      assert.equal(control.step, param.step); assert.equal(control.default, param.default);
      assert.equal(par.style, param.step === 1 ? 'Int' : 'Float');
    }
    for (const name of ['Intensity', 'Wet', 'Bypass', 'Solo']) assert.ok(comp.customPars.some(p => p.name === name), `${definition.type} ${name}`);
  }
});

/** A valid patch whose processors are the given ids, wired in a chain from source to output. */
function chainPatch(ids) {
  const base = freshPatch('delay');
  const template = base.nodes.find(n => n.type === 'delay');
  const copy = structuredClone(base);
  copy.nodes = copy.nodes.filter(n => n.type !== 'delay');
  ids.forEach((id, i) => { const n = structuredClone(template); n.id = id; copy.nodes.splice(1 + i, 0, n); });
  const output = copy.nodes.find(n => n.type === 'output'), source = copy.nodes.find(n => n.type === 'source');
  copy.connections = [];
  let previous = source.id;
  for (const id of [...ids, output.id]) { for (const port of ['audio', 'picture', 'text', 'matte']) copy.connections.push({from: previous, to: id, port, gain: 1, role: 'insert'}); previous = id; }
  return copy;
}

test('operator names are legal and unique, and the original id survives in a parameter and the address', () => {
  const taken = new Set();
  assert.equal(operatorName('prior-returns', taken), 'fx_prior_returns');
  assert.equal(operatorName('prior_returns', taken), 'fx_prior_returns_2');
  assert.equal(operatorName('9 lives', new Set()), 'fx_9_lives');
  const patch = chainPatch(['a-b', 'a_b', 'a b']);
  assert.equal(validatePatch(patch).valid, true, validatePatch(patch).errors.join(' '));
  const spec = buildNetworkSpec(patch);
  assert.deepEqual(validateNetworkSpec(spec), []);
  assert.deepEqual(spec.contract.nodes.map(n => n.comp), ['fx_a_b', 'fx_a_b_2', 'fx_a_b_3']);
  for (const node of spec.contract.nodes) {
    const comp = spec.operators.find(o => o.name === node.comp);
    assert.equal(comp.customPars.find(p => p.name === 'Nodeid').value, node.id);
    assert.equal(node.controls[0].address, `/fx/node/${encodeSegment(node.id)}/intensity`);
  }
  assert.equal(new Set(spec.contract.nodes.flatMap(n => n.controls.map(c => c.address))).size, spec.contract.nodes.length * spec.contract.nodes[0].controls.length);
});

test('addresses in the contract are the ones the bridge uses, for ids OSC reserves characters in', () => {
  const ids = ['fx 1', 'a/b', 'ünï', 'a%b', 'wild*card?', 'brace{a,b}[x]#'];
  const copy = chainPatch(ids);
  assert.equal(validatePatch(copy).valid, true, validatePatch(copy).errors.join(' '));
  const spec = buildNetworkSpec(copy);
  assert.deepEqual(validateNetworkSpec(spec), []);
  const comps = spec.contract.nodes.map(n => n.comp);
  assert.equal(new Set(comps).size, comps.length);
  for (const id of ids) {
    const node = spec.contract.nodes.find(n => n.id === id);
    assert.equal(node.typeAddress, `/fx/node/${encodeSegment(id)}/type`);
    assert.doesNotMatch(node.controls[0].address, /[\s*?,[\]{}#]|\/\/|[^\x00-\x7f]/);
    assert.equal(node.controls.find(c => c.path === 'wet').address, `/fx/node/${encodeSegment(id)}/wet`);
  }
  assert.equal(spec.contract.nodes.find(n => n.id === 'a/b').controls[0].address, '/fx/node/a%2Fb/intensity');
  assert.equal(spec.contract.nodes.find(n => n.id === 'ünï').controls[0].address, '/fx/node/%C3%BCn%C3%AF/intensity');
});

test('contract: every address the workbench reports has a receive entry, and every contract address is one the workbench reports', () => {
  for (const {label, patch} of patches) {
    const spec = buildNetworkSpec(patch), view = viewOf(patch);
    const reported = new Set(new OscFeedback().collect(view, {full: true}).map(m => m.address).filter(a => a !== '/fx/time'));
    const known = new Set([
      ...spec.contract.nodes.flatMap(n => [n.typeAddress, ...n.controls.map(c => c.address)]),
      spec.contract.rack.count.address, spec.contract.rack.selected.address, spec.contract.rack.selectedType.address,
      ...spec.contract.rack.transport.map(t => t.address),
    ]);
    assert.deepEqual([...reported].sort(), [...known].sort(), label);
  }
});

test('contract: every address the network can send is accepted by the workbench, and the slot aliases mean the same control', () => {
  for (const {label, patch} of patches.slice(0, 20)) {
    const spec = buildNetworkSpec(patch);
    for (const node of spec.contract.nodes) {
      const view = viewOf(patch, node.id);
      for (const control of node.controls) {
        const message = {address: control.address, args: [control.kind === 'toggle' ? (control.value ? 0 : 1) : 0.3731]};
        const direct = interpretOsc(message, view);
        assert.equal(direct.ignored, false, `${label} ${control.address}`);
        const alias = spec.contract.selectedSlots.find(s => s.id === node.id && s.path === control.path);
        const viaSlot = interpretOsc({address: alias.address, args: message.args}, view);
        assert.deepEqual(viaSlot.actions, direct.actions, `${label} ${alias.address}`);
      }
    }
    const view = viewOf(patch);
    for (const item of [...spec.contract.rack.transport]) assert.equal(interpretOsc({address: item.address, args: [1]}, view).ignored, false, item.address);
    for (const item of spec.contract.rack.pulses) assert.equal(interpretOsc({address: item.address, args: []}, view).ignored, false, item.address);
    assert.equal(interpretOsc({address: spec.contract.rack.selected.address, args: [spec.contract.nodes[0].id]}, view).ignored, false);
  }
});

test('round trip: the rack reports its state, the Python module sets the parameters, and the values agree', () => {
  for (const {label, patch} of patches.slice(0, 18)) {
    const spec = buildNetworkSpec(patch), view = viewOf(patch);
    // move a few values off their defaults so the check is not satisfied by defaults alone
    for (const node of view.nodes) {
      const {ranges} = controller.describeControls(node, view);
      ranges.forEach((range, i) => view.values.set(`${node.id}|${range.path}`, controller.normToValue(range, (i * 0.137 + 0.21) % 1)));
      view.values.set(`${node.id}|bypass`, node.id.length % 2 === 0);
    }
    const reports = new OscFeedback().collect(view, {full: true});
    const out = probe({module: moduleSource, contract: spec.contract, reports, edits: []});
    assert.ok(out.understood.every(Boolean), `${label}: some rack report was not understood`);
    assert.deepEqual(out.echoed, [], `${label}: applying the rack's own report must not send anything back`);
    for (const node of spec.contract.nodes) for (const control of node.controls) {
      const got = out.params[`${node.comp}|${control.par}`], want = view.current(node.id, control.path);
      if (control.kind === 'toggle') assert.equal(Boolean(got), Boolean(want), `${label} ${node.id}.${control.path}`);
      else assert.ok(Math.abs(got - want) <= (control.max - control.min) * 1e-4 + 1e-9, `${label} ${node.id}.${control.path}: ${got} vs ${want}`);
    }
    assert.equal(out.params[`|${spec.contract.rack.selected.par}`], view.selectedId);
    assert.equal(out.params[`|${spec.contract.rack.count.par}`], view.nodes.length);
    assert.match(out.status, /^linked: \d+ processors$/);
  }
});

test('round trip: an edit made in TouchDesigner becomes the control message and the workbench applies the same value', () => {
  for (const {label, patch} of patches.slice(0, 18)) {
    const spec = buildNetworkSpec(patch);
    const edits = [], expected = [];
    for (const node of spec.contract.nodes) for (const control of node.controls) {
      const value = control.kind === 'toggle' ? !control.value : control.min + (control.max - control.min) * 0.6183;
      edits.push({comp: node.comp, par: control.par, value: control.kind === 'toggle' ? (value ? 1 : 0) : value});
      expected.push({node, control, value});
    }
    const out = probe({module: moduleSource, contract: spec.contract, reports: [], edits});
    out.edits.forEach((sent, i) => {
      const {node, control, value} = expected[i], view = viewOf(patch, node.id);
      if (sent === null) { assert.equal(control.kind === 'range' && Math.abs(0.6183 - (control.value - control.min) / (control.max - control.min)) <= 2e-4, true, `${label} ${control.address} was suppressed as an echo`); return; }
      assert.equal(sent.address, control.address);
      const result = interpretOsc({address: sent.address, args: sent.args}, view);
      assert.equal(result.ignored, false);
      if (control.kind === 'toggle') assert.deepEqual(result.actions.map(a => a.value), [Boolean(value)], `${label} ${control.address}`);
      else {
        // The workbench quantises onto the control's step; so must the TD side's edit have, to within one step.
        const applied = result.actions.length ? result.actions[0].value : view.current(node.id, control.path);
        assert.ok(Math.abs(applied - value) <= control.step / 2 + (control.max - control.min) * 1e-4, `${label} ${control.address}: ${applied} vs ${value}`);
      }
    });
  }
});

test('echo: a value TouchDesigner just received is not sent back, and a real edit is', () => {
  const patch = freshPatch('delay'), spec = buildNetworkSpec(patch), node = spec.contract.nodes[0];
  const control = node.controls.find(c => c.path === 'params.time');
  const norm = 0.4;
  const out = probe({module: moduleSource, contract: spec.contract,
    reports: [{address: control.address, args: [norm]}],
    edits: [{comp: node.comp, par: control.par, value: control.min + norm * (control.max - control.min)}, {comp: node.comp, par: control.par, value: control.max}]});
  assert.equal(out.edits[0], null);
  assert.deepEqual(out.edits[1], {address: control.address, args: [1]});
});

test('validator catches broken specs', () => {
  const base = () => structuredClone(buildNetworkSpec(createPatch('message-impact')));
  const cases = {
    'unknown operator type': spec => { spec.operators[3].type = 'bogusTOP'; },
    'parameter outside the vocabulary': spec => { spec.operators.find(o => o.type === 'selectTOP').pars.bogus = 1; },
    'illegal operator name': spec => { spec.operators[2].name = 'bad-name'; },
    'duplicate path': spec => { spec.operators.push(structuredClone(spec.operators[2])); },
    'child before parent': spec => { const i = spec.operators.findIndex(o => o.parent); spec.operators.unshift(spec.operators.splice(i, 1)[0]); },
    'input that is not a sibling': spec => { spec.operators.find(o => o.inputs.length).inputs[0] = 'nowhere'; },
    'family mismatch on a wire': spec => { const o = spec.operators.find(op => op.name === 'in_audio' && op.inputs.length); o.inputs = [spec.operators.find(op => op.parent === o.parent && op.family === 'TOP').name]; },
    'select pointing at the wrong family': spec => { const o = spec.operators.find(op => op.type === 'selectTOP'); Object.keys(o.pars).forEach(k => { o.pars[k] = {ref: spec.operators.find(x => x.family === 'CHOP').path}; }); },
    'select with a dangling reference': spec => { const o = spec.operators.find(op => op.type === 'selectTOP'); Object.keys(o.pars).forEach(k => { o.pars[k] = {ref: 'fx_ghost/out_picture'}; }); },
    'expression naming an undefined parameter': spec => { const o = spec.operators.find(op => op.pars.cross); o.pars.cross = {expr: 'parent().par.Nope', par: 'Nope'}; },
    'custom parameter with a bad name': spec => { spec.operators.find(o => o.node?.type === 'delay').customPars[2].name = 'bad_name'; },
    'a control without its rule addresses': spec => { delete spec.contract.nodes[0].controls[2].rules.event; spec.operators.find(o => o.text && o.name === 'td_contract').text = JSON.stringify(spec.contract); },
    'a rule address that is not the workbench\'s': spec => { spec.contract.nodes[0].controls[2].rules.lane += 'x'; spec.operators.find(o => o.text && o.name === 'td_contract').text = JSON.stringify(spec.contract); },
    'contract range differing from the parameter': spec => { spec.contract.nodes[0].controls[2].max += 1; spec.operators.find(o => o.text && o.name === 'td_contract').text = JSON.stringify(spec.contract); },
    'duplicate contract address': spec => { spec.contract.nodes[0].controls[1].address = spec.contract.nodes[0].controls[0].address; spec.operators.find(o => o.name === 'td_contract').text = JSON.stringify(spec.contract); },
    'td_contract text out of step with the contract': spec => { spec.contract.nodes[0].controls[0].default = 0.123; },
    'wire pointing somewhere else': spec => { const o = spec.operators.find(op => op.path === spec.wires[0].select); Object.keys(o.pars).forEach(k => { o.pars[k] = {ref: spec.wires[1].source}; }); },
    'OSC port out of step': spec => { spec.osc.inPort = 9; },
    'expression reading a row the resolved table does not hold': spec => { const o = spec.operators.find(op => op.pars.cross); o.pars.cross = {expr: "float(parent().op('resolved')['ghost',1].val)", resolved: ['param.ghost'], rack: [], siblings: []}; },
    'expression that does not read its declared row': spec => { const o = spec.operators.find(op => op.pars.cross); o.pars.cross = {expr: '0.5', resolved: ['wet'], rack: [], siblings: []}; },
    'a processor without its resolve block': spec => { delete spec.contract.nodes[0].resolve; spec.operators.find(o => o.name === 'td_contract').text = JSON.stringify(spec.contract); },
    'resolve range differing from the catalog': spec => { const block = spec.contract.nodes[0].resolve; block.params[block.order[0]].max += 1; spec.operators.find(o => o.name === 'td_contract').text = JSON.stringify(spec.contract); },
    'resolved table missing a row': spec => { const o = spec.operators.find(op => op.name === 'resolved'); o.table = o.table.slice(1); },
    'a contract without a picture rate': spec => { spec.contract.pictureFps = 0; spec.operators.find(o => o.name === 'td_contract').text = JSON.stringify(spec.contract); },
    'select given an input': spec => { const o = spec.operators.find(op => op.type === 'selectCHOP'); o.inputs = ['in_audio']; },
  };
  assert.deepEqual(validateNetworkSpec(base()), []);
  for (const [label, mutate] of Object.entries(cases)) {
    const spec = base();
    mutate(spec);
    assert.ok(validateNetworkSpec(spec).length > 0, label);
  }
  // effects that message-impact does not contain (gate and feedback), checked on the closed inbox
  const effectCases = {
    'effect expression reading a sibling that is not in its processor': spec => { const o = spec.operators.find(op => op.name === 'gate_picture'); o.pars.opacity = {expr: "float(parent().op('nowhere')[0].eval())", resolved: [], rack: [], siblings: ['nowhere']}; },
    'effect expression reading a rack parameter the rack lacks': spec => { const o = spec.operators.find(op => op.name === 'gate_raw'); o.pars.const0value = {expr: 'parent().parent().par.Nope', resolved: [], rack: ['Nope'], siblings: []}; },
    'reference to a missing operator inside a processor': spec => { const o = spec.operators.find(op => op.name === 'fb_frame_1'); o.pars.cachetop = {ref: o.parent + '/ghost'}; },
  };
  for (const [label, mutate] of Object.entries(effectCases)) {
    const spec = structuredClone(buildNetworkSpec(createPatch('closed-inbox')));
    assert.deepEqual(validateNetworkSpec(spec), [], label);
    mutate(spec);
    assert.ok(validateNetworkSpec(spec).length > 0, label);
  }
});

test('the generator refuses an invalid patch and the CLI writes the files', () => {
  assert.throws(() => buildNetworkSpec({...freshPatch('delay'), nodes: []}), /not valid/);
  const dir = mkdtempSync(join(tmpdir(), 'td-net-'));
  const patchFile = join(dir, 'p.json');
  writeFileSync(patchFile, JSON.stringify(createPatch('closed-inbox')));
  const run = spawnSync(process.execPath, [join(here, '../../tools/fx-td-network.mjs'), patchFile, '--out', join(dir, 'out'), '--osc-in-port', '7100', '--osc-out-port', '7101', '--root-name', 'rack_a'], {encoding: 'utf8'});
  assert.equal(run.status, 0, run.stderr);
  const summary = JSON.parse(run.stdout);
  assert.ok(summary.files.includes('closed-inbox.build.py'));
  const spec = JSON.parse(readFileSync(join(dir, 'out/closed-inbox.network.json'), 'utf8'));
  assert.equal(spec.osc.inPort, 7100); assert.equal(spec.root.name, 'rack_a');
  assert.deepEqual(validateNetworkSpec(spec), []);
  const bad = spawnSync(process.execPath, [join(here, '../../tools/fx-td-network.mjs')], {encoding: 'utf8'});
  assert.equal(bad.status, 2);
});

// ---- the generated module against the real bridge, over real sockets --------------------------------------------
import {createSocket} from 'node:dgram';
import {spawn} from 'node:child_process';
import {startBridge} from '../../tools/fx-bridge/bridge.mjs';
import {encodeMessage as encodeOsc, decodePacket} from '../../tools/fx-bridge/osc-codec.mjs';
import {oscSession} from './runtime.mjs';

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, ms = 4000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await predicate()) return; await wait(15); }
  assert.fail('timed out waiting for the condition');
}

/** Headless TouchDesigner: the generated module, behind a UDP socket and JSON-lines control. */
async function headlessTd(specPath, tdPort, oscPort) {
  const child = spawn('python3', ['-I', join(here, '../../tools/td-network/headless_td.py'), specPath, '--td-port', String(tdPort), '--osc-port', String(oscPort)], {stdio: ['pipe', 'pipe', 'pipe']});
  let buffer = '', stderr = '';
  const lines = [], waiting = [];
  child.stdout.on('data', chunk => { buffer += chunk; let at; while ((at = buffer.indexOf('\n')) >= 0) { const line = buffer.slice(0, at); buffer = buffer.slice(at + 1); const next = waiting.shift(); if (next) next(JSON.parse(line)); else lines.push(JSON.parse(line)); } });
  child.stderr.on('data', chunk => { stderr += chunk; });
  const read = () => lines.length ? Promise.resolve(lines.shift()) : Promise.race([new Promise(resolve => waiting.push(resolve)), wait(5000).then(() => { throw Error(`headless TD gave no answer: ${stderr}`); })]);
  const ready = await Promise.race([read(), wait(8000).then(() => { throw Error(`headless TD did not start: ${stderr}`); })]);
  assert.equal(ready.ready, true);
  const command = async body => { child.stdin.write(`${JSON.stringify(body)}\n`); return read(); };
  return {command, params: async () => (await command({cmd: 'params'})), async close() { child.stdin.write('{"cmd":"quit"}\n'); await new Promise(resolve => child.once('close', resolve)); }, stderr: () => stderr};
}

test('end to end: workbench session <-> bridge <-> the generated TouchDesigner module over UDP', async () => {
  const patch = createPatch('message-impact');
  const spec = buildNetworkSpec(patch);
  const dir = mkdtempSync(join(tmpdir(), 'td-e2e-'));
  const specPath = join(dir, 'network.json');
  writeFileSync(specPath, serialise(spec));
  // Reserve two UDP ports: the headless TD listens on one, the bridge on the other.
  const probeSocket = async () => { const s = createSocket('udp4'); await new Promise(r => s.bind(0, '127.0.0.1', r)); const port = s.address().port; await new Promise(r => s.close(r)); return port; };
  const tdPort = await probeSocket();
  const view = viewOf(patch), applied = [];
  const node = view.nodes[0];
  const host = {getView: () => view, apply(action) { applied.push(action); view.values.set(`${action.nodeId}|${action.path}`, action.value); }, gesture() {}, select() {}, selectNode() {}};
  const bridge = await startBridge({wsPort: 0, oscPort: 0, tdPort, log: () => {}});
  const td = await headlessTd(specPath, tdPort, bridge.oscPort);
  const session = new oscSession.OscSession(host, {});
  try {
    session.connect(`ws://127.0.0.1:${bridge.wsPort}`);
    // The session opens by sending the whole rack state; TouchDesigner's parameters take those values.
    await until(async () => (await td.params()).status === `linked: ${view.nodes.length} processors`);
    await wait(300);
    assert.deepEqual(applied, [], 'the rack\'s own report applied to TouchDesigner must not come back as an edit');
    const comp = spec.contract.nodes.find(n => n.id === node.id);
    const intensity = comp.controls.find(c => c.path === 'intensity'), bypass = comp.controls.find(c => c.path === 'bypass');
    await until(async () => { const {params} = await td.params(); return params[`${comp.comp}|Bypass`] === 0 && params['|Selected'] === node.id && params['|Loop'] === 1; });
    // An edit in TouchDesigner reaches the rack through real OSC and a real WebSocket, as an osc-bound action.
    const sent = await td.command({cmd: 'edit', comp: comp.comp, par: intensity.par, value: 0.37});
    assert.deepEqual(sent.sent, [intensity.address, [0.37]]);
    await until(() => applied.some(a => a.path === 'intensity' && a.nodeId === node.id));
    assert.deepEqual(applied.find(a => a.path === 'intensity'), {nodeId: node.id, path: 'intensity', value: 0.37, binding: 'osc'});
    assert.equal((await td.command({cmd: 'edit', comp: comp.comp, par: bypass.par, value: 1})).sent[1][0], 1);
    await until(() => applied.some(a => a.path === 'bypass' && a.value === true));
    // The rack's own report of that edit is an echo: TouchDesigner does not answer it with another edit.
    await wait(300);
    assert.equal(applied.filter(a => a.path === 'intensity').length, 1);
    // A change made in the workbench shows up in TouchDesigner's parameters.
    const wet = comp.controls.find(c => c.path === 'wet');
    view.values.set(`${node.id}|wet`, 0.2);
    await td.command({cmd: 'pulse', comp: '', par: 'Sync'});
    await until(async () => Math.abs(((await td.params()).params[`${comp.comp}|${wet.par}`] ?? 9) - 0.2) < 1e-6);
    // Transport and selection pulses are understood by the rack.
    assert.deepEqual((await td.command({cmd: 'pulse', comp: '', par: 'Next'})).sent, ['/fx/select/next', []]);
    assert.deepEqual((await td.command({cmd: 'edit', comp: '', par: 'Loop', value: 0})).sent, ['/fx/transport/loop', [0]]);
    assert.equal(td.stderr(), '');
  } finally {
    session.disconnect();
    await td.close();
    await bridge.close();
  }
});

test('the headless OSC codec agrees with the Node codec in both directions', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'td-codec-'));
  const specPath = join(dir, 'network.json');
  writeFileSync(specPath, serialise(buildNetworkSpec(freshPatch('delay'))));
  const received = [];
  const socket = createSocket('udp4');
  await new Promise(r => socket.bind(0, '127.0.0.1', r));
  socket.on('message', packet => received.push(...decodePacket(new Uint8Array(packet))));
  const tdPort = await new Promise(resolve => { const s = createSocket('udp4'); s.bind(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
  const td = await headlessTd(specPath, tdPort, socket.address().port);
  try {
    // Node encodes a report; the Python codec must decode it exactly (the intensity reaches the parameter).
    const spec = buildNetworkSpec(freshPatch('delay')), node = spec.contract.nodes[0];
    const intensity = node.controls.find(c => c.path === 'intensity');
    socket.send(Buffer.from(encodeOsc(intensity.address, [0.25])), tdPort, '127.0.0.1');
    await until(async () => ((await td.params()).params[`${node.comp}|Intensity`]) === 0.25);
    socket.send(Buffer.from(encodeOsc('/fx/selected', ['ünï'])), tdPort, '127.0.0.1');
    await until(async () => ((await td.params()).params['|Selected']) === 'ünï');
    // Python encodes an edit; the Node codec must decode it exactly.
    await td.command({cmd: 'edit', comp: node.comp, par: 'Intensity', value: 0.75});
    await until(() => received.some(m => m.address === intensity.address && Math.abs(m.args[0] - 0.75) < 1e-6));
    await td.command({cmd: 'edit', comp: '', par: 'Selected', value: 'a/b ünï'});
    await until(() => received.some(m => m.address === '/fx/selected' && m.args[0] === 'a/b ünï'));
  } finally { await td.close(); await new Promise(r => socket.close(r)); }
});

async function runSimulatorAgainst(patch) {
  const spec = buildNetworkSpec(patch);
  const dir = mkdtempSync(join(tmpdir(), 'td-sim-'));
  const specPath = join(dir, 'network.json'), resultPath = join(dir, 'result.json');
  writeFileSync(specPath, serialise(spec));
  const freePort = () => new Promise(resolve => { const s = createSocket('udp4'); s.bind(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
  const tdPort = await freePort();
  const view = viewOf(patch);
  const host = {getView: () => view, apply(action) { if (action.global) { view.globals.set(action.global, action.value); return; } view.values.set(`${action.nodeId}|${action.path}`, action.value); }, gesture() {}, select() {}, selectNode() {}};
  const bridge = await startBridge({wsPort: 0, oscPort: 0, tdPort, log: () => {}});
  const session = new oscSession.OscSession(host, {});
  try {
    session.connect(`ws://127.0.0.1:${bridge.wsPort}`);
    await until(() => bridge.stats.clients === 1);
    const run = spawn(process.execPath, [join(here, '../../tools/fx-td-simulator.mjs'), resultPath, '--spec', specPath, '--td-port', String(tdPort), '--osc-port', String(bridge.oscPort), '--wait-seconds', '20'], {stdio: ['ignore', 'pipe', 'pipe']});
    let output = '';
    run.stdout.on('data', chunk => { output += chunk; }); run.stderr.on('data', chunk => { output += chunk; });
    const code = await new Promise(resolve => run.once('close', resolve));
    return {spec, code, output, result: JSON.parse(readFileSync(resultPath, 'utf8'))};
  } finally { session.disconnect(); await bridge.close(); }
}

test('the simulator, driven by the generated spec, passes against a rack that follows the contract', async () => {
  const {spec, code, output, result} = await runSimulatorAgainst(createPatch('closed-inbox'));
  assert.equal(code, 0, output);
  assert.equal(result.passed, true);
  assert.ok(result.checks.length >= 3 * spec.contract.nodes.length, `checks: ${result.checks.length}`);
  for (const node of spec.contract.nodes) assert.ok(result.checks.some(c => c.name.startsWith(`${node.id}: ${node.controls.find(x => x.path === 'intensity').address}`)), node.id);
});

test('the simulator copes with a generated spec that has no processors', async () => {
  const patch = chainPatch([]);
  assert.equal(validatePatch(patch).valid, true, validatePatch(patch).errors.join(' '));
  const {spec, code, output, result} = await runSimulatorAgainst(patch);
  assert.equal(spec.contract.nodes.length, 0);
  assert.equal(code, 0, output);
  assert.equal(result.passed, true);
  assert.ok(result.checks.some(c => /transport loop/.test(c.name)));
});

test('the vocabulary checker flags names the TouchDesigner reference does not know, and accepts colour tuplets', () => {
  const help = {product: 'TouchDesigner', help: {CHOPs: {selectCHOP: {parameters: {chop: {parType: 'CHOP'}}}, constantCHOP: {parameters: {const0name: {}, const0value: {}}}}, TOPs: {constantTOP: {parameters: {color: {parType: 'RGB'}, alpha: {}}}}}};
  const vocabulary = {
    selectCHOP: {family: 'CHOP', pars: ['chop']}, constantCHOP: {family: 'CHOP', pars: ['const0name', 'const0value']},
    constantTOP: {family: 'TOP', pars: ['colorr', 'colorg', 'colorb', 'alpha']},
  };
  assert.deepEqual(checkVocabulary(help, vocabulary), []);
  assert.deepEqual(checkVocabulary(help, {...vocabulary, selectCHOP: {family: 'CHOP', pars: ['chops']}}), ['selectCHOP.chops: not a parameter in the reference']);
  assert.deepEqual(checkVocabulary(help, {...vocabulary, constantCHOP: {family: 'CHOP', pars: ['name0']}}), ['constantCHOP.name0: not a parameter in the reference']);
  assert.deepEqual(checkVocabulary(help, {noiseTOP: {family: 'TOP', pars: []}}), ['noiseTOP: not an operator type in the reference']);
  // the vocabulary the generator uses never names the parameters the review found wrong
  assert.deepEqual(VOCABULARY.selectCHOP.pars, ['chop']);
  assert.deepEqual(VOCABULARY.constantCHOP.pars, ['const0name', 'const0value']);
});

test('an off-grid value on the rack (a preset, or an automation sample) is not snapped by the echo of its own report', () => {
  const patch = freshPatch('delay'), spec = buildNetworkSpec(patch), node = spec.contract.nodes[0];
  const view = viewOf(patch);
  const time = node.controls.find(c => c.path === 'params.time');
  view.values.set(`${node.id}|params.time`, 0.483);      // step is 0.01
  const reports = new OscFeedback().collect(view, {full: true});
  const out = probe({module: moduleSource, contract: spec.contract, reports, edits: []});
  assert.deepEqual(out.echoed, []);
  const written = out.params[`${node.comp}|${time.par}`];
  assert.ok(Math.abs(written - 0.483) < 1e-3, `written ${written}`);
  // Feeding TouchDesigner's own value back as an edit is a no-op, so the rack's 0.483 is never replaced by 0.48.
  const again = probe({module: moduleSource, contract: spec.contract, reports, edits: [{comp: node.comp, par: time.par, value: written}]});
  assert.equal(again.edits[0], null);
});

test('the processors read their wet, bypass and parameters from the resolved table, not from the raw custom parameters', () => {
  const spec = buildNetworkSpec(createPatch('message-impact'));
  const processor = spec.contract.nodes[0].comp;
  const inside = spec.operators.filter(o => o.parent === processor);
  const blends = inside.filter(o => /^(blend|wet|dry|bypass)_/.test(o.name));
  assert.ok(blends.length >= 6);
  for (const operator of blends) for (const value of Object.values(operator.pars)) if (value?.expr) assert.ok('resolved' in value && !value.expr.includes('par.Wet') && !value.expr.includes('par.Bypass'), operator.path);
  const table = inside.find(o => o.name === 'resolved');
  assert.deepEqual(table.table.slice(0, 5).map(row => row[0]), ['intensity', 'wet', 'bypass', 'solo', 'active']);
  assert.ok(spec.operators.find(o => o.name === 'frame_exec' && o.type === 'executeDAT' && o.pars.framestart === 1));
});

test('the picture rate the feedback epoch is latched on defaults to the workbench\'s 12 and can be set', () => {
  const patch = createPatch('closed-inbox');
  assert.equal(buildNetworkSpec(patch).contract.pictureFps, 12);
  assert.equal(buildNetworkSpec(patch, {pictureFps: 24}).contract.pictureFps, 24);
  for (const bad of [0, -1, Number.NaN, Infinity]) assert.throws(() => buildNetworkSpec(patch, {pictureFps: bad}), /pictureFps/, String(bad));
});
