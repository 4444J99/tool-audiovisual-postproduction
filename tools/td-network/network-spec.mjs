/**
 * Composition patch -> TouchDesigner network spec (amps.td-network.v1).
 *
 * The spec is plain data: operators, their parameters, wiring, custom parameters and the OSC address
 * contract. `python/fx_td_build.py` turns it into a live network; `validateNetworkSpec` checks its
 * structure without TouchDesigner. Processors, parameter ranges and addresses are not restated here:
 * they come from the catalog, `describeControls` (the same list the hardware surface and the OSC bridge
 * use) and `nodeAddress`, so the generated network cannot drift from the workbench.
 *
 * What the network is: the rack's control and routing layer. Every processor becomes a component with the
 * workbench's own controls as custom parameters (Intensity, Wet, Bypass, Solo and the processor's
 * parameters, with their real ranges), typed signal ports wired as the patch wires them, a real wet/dry
 * blend and bypass for picture and audio, and an empty `proc_*` slot where the effect itself belongs. The
 * effects are not ported by this step, so a freshly built rack passes signal through unchanged.
 */
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';
import {CATALOG_BY_TYPE, PORTS} from '../../web/fx/catalog.mjs';
import {describeControls} from '../../web/fx/controller-map.mjs';
import {validatePatch} from '../../web/fx/core.mjs';
import {OSC_ROOT, ECHO_TOLERANCE, encodeSegment, nodeAddress} from '../../web/fx/osc-control.mjs';
import {VOCABULARY, PORT_FAMILY, SELECT, NULL, CUSTOM_PAR_NAME, OPERATOR_NAME} from './vocabulary.mjs';

export const SPEC_SCHEMA = 'amps.td-network.v1';
export const CONTRACT_SCHEMA = 'amps.td-osc-contract.v1';
const here = dirname(fileURLToPath(import.meta.url));
export const pythonText = name => readFileSync(join(here, 'python', name), 'utf8');

const STRUCTURAL = ['source', 'mix', 'output'];
const RACK_PARS = Object.freeze({selected: 'Selected', time: 'Time', count: 'Count', status: 'Status', play: 'Play', record: 'Record', loop: 'Loop', next: 'Next', prev: 'Prev', sync: 'Sync'});

/** TouchDesigner operator names allow letters, digits and underscore only; collisions get a numeric suffix. */
export function operatorName(id, taken) {
  let base = `fx_${String(id).replace(/[^A-Za-z0-9_]/g, '_')}`;
  let name = base, n = 2;
  while (taken.has(name)) name = `${base}_${n++}`;
  taken.add(name);
  return name;
}

/** Custom parameter names are one capital followed by lowercase letters and digits. */
export const customParName = key => `${key[0].toUpperCase()}${key.slice(1).toLowerCase()}`;

const customPar = (page, name, label, style, extra = {}) => ({page, name, label, style, ...extra});
const fixed = value => Number(Number(value).toPrecision(12));

function controlPars(node, patch) {
  const {ranges, switches} = describeControls(node, {duration: patch.duration});
  const used = new Set(['Intensity', 'Wet', 'Bypass', 'Solo']);
  const controls = [];
  const slot = {knob: 0, switch: 0};
  const current = path => path === 'bypass' || path === 'solo' ? Boolean(node[path]) : path.startsWith('params.') ? node.params[path.slice(7)] : node[path];
  for (const control of [...ranges, ...switches]) {
    let par;
    if (control.kind === 'toggle') par = customParName(control.path);
    else if (control.path === 'intensity' || control.path === 'wet') par = customParName(control.path);
    else {
      par = customParName(control.path.slice(7));
      if (used.has(par)) throw Error(`${node.id}: custom parameter ${par} collides inside ${node.type}.`);
      used.add(par);
    }
    const group = control.kind === 'toggle' ? 'switch' : 'knob';
    const value = current(control.path);
    const entry = {
      path: control.path, par, label: control.label, kind: control.kind, slot: `${group}:${slot[group]++}`,
      address: nodeAddress(node.id, control.path), default: control.kind === 'toggle' ? false : control.default,
      value: control.kind === 'toggle' ? Boolean(value) : value,
    };
    if (control.kind === 'range') Object.assign(entry, {min: control.min, max: control.max, step: control.step, integer: control.step === 1 && Number.isInteger(control.min) && Number.isInteger(control.max)});
    if (control.options) entry.options = [...control.options];
    controls.push(entry);
  }
  return controls;
}

const parDefinition = (control, page) => control.kind === 'toggle'
  ? customPar(page, control.par, control.label, 'Toggle', {default: control.default ? 1 : 0, value: control.value ? 1 : 0})
  : customPar(page, control.par, control.label, control.integer ? 'Int' : 'Float', {
    min: control.min, max: control.max, normMin: control.min, normMax: control.max, clampMin: true, clampMax: true,
    default: control.default, value: control.value,
    ...(control.options ? {help: control.options.join(' / ')} : {}),
  });

export function buildNetworkSpec(patch, options = {}) {
  const verdict = validatePatch(patch);
  if (!verdict.valid) throw Error(`Patch ${patch?.id} is not valid: ${verdict.errors.slice(0, 3).join(' ')}`);
  const oscIn = options.oscInPort ?? 7000, oscOut = options.oscOutPort ?? 7001, host = options.host ?? '127.0.0.1';
  const rootName = options.rootName ?? 'fx_rack';
  const taken = new Set(['osc_in', 'osc_in_callbacks', 'osc_out', 'td_osc', 'td_contract', 'par_exec_nodes', 'par_exec_rack', 'video_out', 'audio_out']);
  const names = new Map(patch.nodes.map(node => [node.id, operatorName(node.id, taken)]));
  const warnings = [];
  const operators = [];
  const add = (parent, name, type, extra = {}) => {
    const entry = {path: parent ? `${parent}/${name}` : name, parent, name, type, pars: {}, inputs: [], customPars: [], ...extra};
    operators.push(entry);
    return entry;
  };
  const incoming = new Map(patch.nodes.map(node => [node.id, Object.fromEntries(PORTS.map(port => [port, []]))]));
  for (const edge of patch.connections) incoming.get(edge.to)[edge.port].push(edge);
  const wires = [];

  const nodes = [];
  for (const node of patch.nodes) {
    const comp = names.get(node.id);
    const isFx = !STRUCTURAL.includes(node.type);
    const controls = isFx ? controlPars(node, patch) : [];
    const customPars = [
      customPar('Fx', 'Nodeid', 'Processor id', 'Str', {value: node.id, readOnly: true}),
      customPar('Fx', 'Nodetype', 'Type', 'Str', {value: node.type, readOnly: true}),
      ...(isFx ? controls.map(control => parDefinition(control, 'Fx')) : []),
      ...(!isFx ? [customPar('Fx', 'Gain', 'Gain', 'Float', {min: 0, max: 4, normMin: 0, normMax: 4, clampMin: true, clampMax: true, default: 1, value: node.gain ?? 1})] : []),
      ...(node.type === 'source' ? [
        customPar('Fx', 'Sourcefile', 'Source media', 'File', {value: options.sourceFile ?? ''}),
        customPar('Fx', 'Offset', 'Excerpt offset (s)', 'Float', {value: patch.source.offset, readOnly: true}),
      ] : []),
    ];
    add('', comp, 'baseCOMP', {customPars, node: {id: node.id, type: node.type}});

    // Input side: one select per incoming wire, a gain where the wire has one, a combiner where several meet.
    if (node.type !== 'source') {
      for (const port of PORTS) {
        const family = PORT_FAMILY[port], [selectType, selectPar] = SELECT[family];
        const edges = incoming.get(node.id)[port];
        const tails = [];
        edges.forEach((edge, index) => {
          const upstream = `${names.get(edge.from)}/out_${port}`;
          const select = add(comp, `sel_${port}_${index}`, selectType, {pars: {[selectPar]: {ref: upstream}}, wire: {from: edge.from, port, role: edge.role, gain: edge.gain}});
          wires.push({from: edge.from, to: edge.to, port, role: edge.role, gain: edge.gain, select: select.path, source: upstream});
          let tail = select.name;
          if (edge.gain !== 1 && family !== 'DAT') {
            const gain = family === 'TOP'
              ? add(comp, `gain_${port}_${index}`, 'levelTOP', {inputs: [tail], pars: {opacity: edge.gain}})
              : add(comp, `gain_${port}_${index}`, 'mathCHOP', {inputs: [tail], pars: {gain: edge.gain}});
            tail = gain.name;
          } else if (edge.gain !== 1) warnings.push(`${edge.from} -> ${edge.to} (${port}): text wires carry no gain.`);
          tails.push(tail);
        });
        let source = tails[0];
        if (tails.length > 1) {
          if (family === 'TOP') source = add(comp, `mixin_${port}`, 'addTOP', {inputs: tails}).name;
          else if (family === 'CHOP') source = add(comp, `mixin_${port}`, 'mathCHOP', {inputs: tails, pars: {chopop: 'add'}}).name;
          else warnings.push(`${node.id}: several text wires meet at ${port}; only the first is read.`);
        }
        add(comp, `in_${port}`, NULL[family], {inputs: source ? [source] : []});
      }
    }

    const gainExpr = () => ({expr: 'parent().par.Gain', par: 'Gain'});
    if (node.type === 'source') {
      add(comp, 'video', 'moviefileinTOP', {pars: {file: {expr: 'parent().par.Sourcefile', par: 'Sourcefile'}}});
      add(comp, 'gain_picture', 'levelTOP', {inputs: ['video'], pars: {opacity: gainExpr()}});
      add(comp, 'out_picture', 'nullTOP', {inputs: ['gain_picture']});
      add(comp, 'audio', 'audiofileinCHOP', {pars: {file: {expr: 'parent().par.Sourcefile', par: 'Sourcefile'}}});
      add(comp, 'gain_audio', 'mathCHOP', {inputs: ['audio'], pars: {gain: gainExpr()}});
      add(comp, 'out_audio', 'nullCHOP', {inputs: ['gain_audio']});
      add(comp, 'matte', 'constantTOP', {pars: {colorr: 1, colorg: 1, colorb: 1, alpha: 1}});
      add(comp, 'out_matte', 'nullTOP', {inputs: ['matte']});
      add(comp, 'text', 'textDAT', {text: ''});
      add(comp, 'out_text', 'nullDAT', {inputs: ['text']});
      add(comp, 'control', 'constantCHOP', {pars: {name0: 'control', value0: 0}});
      add(comp, 'out_control', 'nullCHOP', {inputs: ['control']});
    } else if (!isFx) {
      add(comp, 'gain_picture', 'levelTOP', {inputs: ['in_picture'], pars: {opacity: gainExpr()}});
      add(comp, 'out_picture', 'nullTOP', {inputs: ['gain_picture']});
      add(comp, 'gain_audio', 'mathCHOP', {inputs: ['in_audio'], pars: {gain: gainExpr()}});
      add(comp, 'out_audio', 'nullCHOP', {inputs: ['gain_audio']});
      for (const port of ['matte', 'control', 'text']) add(comp, `out_${port}`, NULL[PORT_FAMILY[port]], {inputs: [`in_${port}`]});
    } else {
      const wet = {expr: 'parent().par.Wet', par: 'Wet'}, bypass = {expr: 'int(parent().par.Bypass)', par: 'Bypass'};
      add(comp, 'proc_picture', 'nullTOP', {inputs: ['in_picture'], slot: 'picture'});
      add(comp, 'blend_picture', 'crossTOP', {inputs: ['in_picture', 'proc_picture'], pars: {cross: wet}});
      add(comp, 'bypass_picture', 'switchTOP', {inputs: ['blend_picture', 'in_picture'], pars: {index: bypass}});
      add(comp, 'out_picture', 'nullTOP', {inputs: ['bypass_picture']});
      add(comp, 'proc_audio', 'nullCHOP', {inputs: ['in_audio'], slot: 'audio'});
      add(comp, 'wet_audio', 'mathCHOP', {inputs: ['proc_audio'], pars: {gain: wet}});
      add(comp, 'dry_audio', 'mathCHOP', {inputs: ['in_audio'], pars: {gain: {expr: '1 - parent().par.Wet', par: 'Wet'}}});
      add(comp, 'blend_audio', 'mathCHOP', {inputs: ['wet_audio', 'dry_audio'], pars: {chopop: 'add'}});
      add(comp, 'bypass_audio', 'switchCHOP', {inputs: ['blend_audio', 'in_audio'], pars: {index: bypass}});
      add(comp, 'out_audio', 'nullCHOP', {inputs: ['bypass_audio']});
      for (const port of ['matte', 'control', 'text']) add(comp, `out_${port}`, NULL[PORT_FAMILY[port]], {inputs: [`in_${port}`]});
      if (node.modulation) warnings.push(`${node.id}: sidechain modulation (${node.modulation.parameter}, ${node.modulation.mode}) is carried in the contract but not applied by this network.`);
    }
    nodes.push({id: node.id, comp, type: node.type, structural: !isFx, typeAddress: `/${OSC_ROOT}/node/${encodeSegment(node.id)}/type`, controls, ...(node.modulation ? {modulation: node.modulation} : {})});
  }

  const output = patch.nodes.find(node => node.type === 'output');
  const rackContract = {
    selected: {par: RACK_PARS.selected, address: `/${OSC_ROOT}/selected`}, selectedType: {address: `/${OSC_ROOT}/selected/type`},
    count: {par: RACK_PARS.count, address: `/${OSC_ROOT}/count`}, time: {par: RACK_PARS.time, address: `/${OSC_ROOT}/time`},
    status: {par: RACK_PARS.status}, sync: {par: RACK_PARS.sync, address: `/${OSC_ROOT}/sync`},
    transport: ['play', 'record', 'loop'].map(name => ({name, par: RACK_PARS[name], address: `/${OSC_ROOT}/transport/${name}`})),
    pulses: [
      {par: RACK_PARS.next, address: `/${OSC_ROOT}/select/next`}, {par: RACK_PARS.prev, address: `/${OSC_ROOT}/select/prev`},
      {par: RACK_PARS.sync, address: `/${OSC_ROOT}/sync`},
    ],
  };
  const processors = nodes.filter(node => !node.structural);
  const contract = {
    schema: CONTRACT_SCHEMA, root: `/${OSC_ROOT}`, echoTolerance: ECHO_TOLERANCE, rack: rackContract,
    nodes: processors, // structural nodes carry no OSC address
    selectedSlots: processors.flatMap(node => node.controls.map(control => ({id: node.id, path: control.path, address: `/${OSC_ROOT}/selected/${control.slot.replace(':', '/')}`, slot: control.slot}))),
  };
  const rackPars = [
    customPar('Rack', RACK_PARS.selected, 'Selected processor', 'Str', {value: processors[0]?.id ?? ''}),
    customPar('Rack', RACK_PARS.time, 'Playhead (s)', 'Float', {readOnly: true, value: 0}),
    customPar('Rack', RACK_PARS.count, 'Processors in the rack', 'Int', {readOnly: true, value: 0}),
    customPar('Rack', RACK_PARS.status, 'Link status', 'Str', {readOnly: true, value: 'waiting for the rack'}),
    ...rackContract.transport.map(item => customPar('Rack', item.par, item.name[0].toUpperCase() + item.name.slice(1), 'Toggle', {value: item.name === 'loop' ? 1 : 0, default: item.name === 'loop' ? 1 : 0})),
    customPar('Rack', RACK_PARS.next, 'Select next', 'Pulse'), customPar('Rack', RACK_PARS.prev, 'Select previous', 'Pulse'),
    customPar('Rack', RACK_PARS.sync, 'Ask the rack to resend', 'Pulse'),
  ];

  // Rack-level plumbing: OSC, the controlling module, and stable taps on the output.
  const rackOps = [
    ['osc_in', 'oscinDAT', {pars: {port: oscIn, callbacks: {ref: 'osc_in_callbacks'}}}],
    ['osc_in_callbacks', 'textDAT', {text: pythonText('fx_td_osc_in_callbacks.py')}],
    ['osc_out', 'oscoutDAT', {pars: {address: host, port: oscOut}}],
    ['td_osc', 'textDAT', {text: pythonText('fx_td_osc.py')}],
    ['td_contract', 'textDAT', {text: JSON.stringify(contract)}],
    ['par_exec_nodes', 'parameterexecuteDAT', {pars: {op: 'fx_*', pars: '*', custom: 1, builtin: 0, valuechange: 1, onpulse: 1}, text: pythonText('fx_td_par_exec_nodes.py')}],
    ['par_exec_rack', 'parameterexecuteDAT', {pars: {op: '..', pars: '*', custom: 1, builtin: 0, valuechange: 1, onpulse: 1}, text: pythonText('fx_td_par_exec_rack.py')}],
    ['video_out', 'selectTOP', {pars: {top: {ref: `${names.get(output.id)}/out_picture`}}}],
    ['audio_out', 'selectCHOP', {pars: {chops: {ref: `${names.get(output.id)}/out_audio`}}}],
  ];
  for (const [name, type, extra] of rackOps) add('', name, type, extra);
  // Components first is not required by the builder, but a stable, readable order is: plumbing after the nodes.

  return {
    schema: SPEC_SCHEMA,
    generator: {name: 'tools/td-network', contract: CONTRACT_SCHEMA},
    patch: {id: patch.id, name: patch.name, duration: patch.duration, seed: patch.seed, source: {id: patch.source.id, offset: patch.source.offset, duration: patch.source.duration}},
    osc: {host, inPort: oscIn, outPort: oscOut},
    vocabulary: Object.fromEntries(Object.entries(VOCABULARY).map(([type, item]) => [type, {family: item.family, pars: [...item.pars]}])),
    root: {name: rootName, type: 'baseCOMP', customPars: rackPars},
    operators: finish(operators),
    wires,
    contract,
    warnings: [...new Set(warnings)],
  };
}

/** Give every operator the keys the builder reads, and a family from the vocabulary. */
function finish(operators) {
  return operators.map(entry => ({
    path: entry.path, parent: entry.parent, name: entry.name, type: entry.type, family: VOCABULARY[entry.type].family,
    pars: entry.pars, inputs: entry.inputs, customPars: entry.customPars,
    ...(entry.text !== undefined ? {text: entry.text} : {}), ...(entry.table ? {table: entry.table} : {}),
    ...(entry.slot ? {slot: entry.slot} : {}), ...(entry.node ? {node: entry.node} : {}), ...(entry.wire ? {wire: entry.wire} : {}),
  }));
}

/** Structural checks that need no TouchDesigner. Returns a list of problems; empty means the spec is sound. */
export function validateNetworkSpec(spec) {
  const errors = [], fail = message => errors.push(message);
  if (spec?.schema !== SPEC_SCHEMA) return [`schema must be ${SPEC_SCHEMA}`];
  const byPath = new Map(), customOf = new Map([['', new Set(spec.root.customPars.map(item => item.name))]]);
  for (const [type, item] of Object.entries(spec.vocabulary)) if (!VOCABULARY[type] || VOCABULARY[type].family !== item.family) fail(`vocabulary: ${type} is not a supported operator type`);
  for (const operator of spec.operators) {
    const where = operator.path;
    if (!OPERATOR_NAME.test(operator.name)) fail(`${where}: ${operator.name} is not a legal operator name`);
    if (byPath.has(where)) fail(`${where}: duplicate path`);
    byPath.set(where, operator);
    if (operator.parent !== '' && !byPath.has(operator.parent)) fail(`${where}: parent ${operator.parent} is missing or listed later`);
    if (operator.path !== (operator.parent ? `${operator.parent}/${operator.name}` : operator.name)) fail(`${where}: path does not match parent and name`);
    const kind = spec.vocabulary[operator.type];
    if (!kind) {fail(`${where}: unknown operator type ${operator.type}`); continue;}
    if (kind.family !== operator.family) fail(`${where}: family ${operator.family} does not match ${operator.type}`);
    if (operator.type !== 'baseCOMP' && operator.customPars.length) fail(`${where}: only components carry custom parameters`);
    if (operator.type === 'baseCOMP') {
      const names = operator.customPars.map(item => item.name);
      if (new Set(names).size !== names.length) fail(`${where}: duplicate custom parameter names`);
      for (const item of operator.customPars) if (!CUSTOM_PAR_NAME.test(item.name)) fail(`${where}: ${item.name} is not a legal custom parameter name`);
      customOf.set(where, new Set(names));
    }
    for (const name of Object.keys(operator.pars)) if (!kind.pars.includes(name)) fail(`${where}: ${operator.type} has no parameter ${name} in the vocabulary`);
  }
  // inputs: siblings that exist earlier in the list, same family (a DAT input to a TOP is a wiring error)
  for (const operator of spec.operators) {
    const seen = new Set();
    for (const name of operator.inputs) {
      const sibling = byPath.get(operator.parent ? `${operator.parent}/${name}` : name);
      if (!sibling) {fail(`${operator.path}: input ${name} is not a sibling`); continue;}
      if (sibling.family !== operator.family) fail(`${operator.path}: ${operator.family} operator wired to ${sibling.family} operator ${name}`);
      if (seen.has(name)) fail(`${operator.path}: input ${name} listed twice`);
      seen.add(name);
    }
    if (operator.type.startsWith('select') && operator.inputs.length) fail(`${operator.path}: a select operator takes no input`);
    for (const [name, value] of Object.entries(operator.pars)) {
      if (value && typeof value === 'object') {
        if ('ref' in value) {
          const target = byPath.get(value.ref);
          if (!target) fail(`${operator.path}.${name}: references missing ${value.ref}`);
          else if (name !== 'callbacks' && target.family !== operator.family) fail(`${operator.path}.${name}: ${operator.family} select points at a ${target.family}`);
          else if (name === 'callbacks' && target.type !== 'textDAT') fail(`${operator.path}.callbacks must point at a text DAT`);
        } else if ('expr' in value) {
          const owner = operator.parent;
          if (!customOf.get(owner)?.has(value.par)) fail(`${operator.path}.${name}: expression uses ${value.par}, which ${owner || 'the rack'} does not define`);
          if (!String(value.expr).includes(`parent().par.${value.par}`)) fail(`${operator.path}.${name}: expression does not use ${value.par}`);
        } else fail(`${operator.path}.${name}: unrecognised value`);
      }
    }
  }
  // wires: every patch connection has exactly one select pointing at the upstream out_<port>, and no cycle
  const edges = new Map();
  for (const wire of spec.wires) {
    const select = byPath.get(wire.select);
    if (!select) {fail(`wire ${wire.from} -> ${wire.to}: select ${wire.select} is missing`); continue;}
    const reference = Object.values(select.pars).find(value => value?.ref)?.ref;
    if (reference !== wire.source) fail(`wire ${wire.from} -> ${wire.to}: select points at ${reference}, not ${wire.source}`);
    if (!byPath.has(wire.source)) fail(`wire ${wire.from} -> ${wire.to}: source ${wire.source} is missing`);
    const key = wire.to;
    edges.set(key, [...(edges.get(key) ?? []), wire.from]);
  }
  const state = new Map();
  const visit = id => {
    if (state.get(id) === 1) return true;
    if (state.get(id) === 2) return false;
    state.set(id, 1);
    for (const from of edges.get(id) ?? []) if (visit(from)) return true;
    state.set(id, 2);
    return false;
  };
  for (const id of edges.keys()) if (visit(id)) {fail('the signal wires form a cycle'); break;}
  // contract: components exist, parameters exist with the catalog ranges, addresses are unique
  const addresses = new Set();
  const claim = (address, label) => {if (addresses.has(address)) fail(`${label}: duplicate address ${address}`); addresses.add(address);};
  for (const node of spec.contract.nodes) {
    const comp = byPath.get(node.comp);
    if (!comp || comp.type !== 'baseCOMP') {fail(`contract: ${node.id} has no component ${node.comp}`); continue;}
    claim(node.typeAddress, node.id);
    const definition = CATALOG_BY_TYPE[node.type];
    if (!definition) fail(`contract: ${node.id} has unknown type ${node.type}`);
    for (const control of node.controls) {
      claim(control.address, `${node.id}.${control.path}`);
      const par = comp.customPars.find(item => item.name === control.par);
      if (!par) {fail(`contract: ${node.id} lacks custom parameter ${control.par}`); continue;}
      if (control.kind === 'range') {
        if (par.min !== control.min || par.max !== control.max) fail(`contract: ${node.id}.${control.par} range differs from the contract`);
        if (par.style !== (control.integer ? 'Int' : 'Float')) fail(`contract: ${node.id}.${control.par} has style ${par.style}`);
        const param = control.path.startsWith('params.') ? definition?.params.find(item => item.key === control.path.slice(7)) : null;
        if (param && (control.step !== param.step || control.default !== param.default)) fail(`contract: ${node.id}.${control.par} disagrees with the catalog`);
      } else if (par.style !== 'Toggle') fail(`contract: ${node.id}.${control.par} must be a toggle`);
    }
  }
  const rack = spec.contract.rack;
  for (const item of [rack.selected, rack.count, rack.time, rack.status, ...rack.transport, ...rack.pulses]) if (!customOf.get('').has(item.par)) fail(`contract: rack lacks ${item.par}`);
  for (const item of [rack.selected, rack.selectedType, rack.count, rack.time, ...rack.transport, ...rack.pulses.filter(p => p.par !== rack.sync.par)]) claim(item.address, 'rack');
  const dat = path => byPath.get(path);
  if (JSON.stringify(JSON.parse(dat('td_contract')?.text ?? 'null')) !== JSON.stringify(spec.contract)) fail('td_contract text is not the contract');
  for (const required of ['osc_in', 'osc_out', 'td_osc', 'td_contract', 'par_exec_nodes', 'par_exec_rack', 'video_out', 'audio_out']) if (!dat(required)) fail(`rack plumbing ${required} is missing`);
  if (dat('osc_in')?.pars.port !== spec.osc.inPort || dat('osc_out')?.pars.port !== spec.osc.outPort) fail('OSC ports in the operators differ from spec.osc');
  return errors;
}
