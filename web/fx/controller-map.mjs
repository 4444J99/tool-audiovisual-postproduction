/**
 * Hardware-style control layer for the FX rack: MIDI parsing, learnable bindings,
 * range scaling against the catalog, soft takeover and gesture grouping.
 *
 * This module is deliberately free of DOM, Web MIDI and workbench state. The
 * workbench supplies a `view` (nodes, selected processor, current values) and
 * applies the returned actions through its own parameter setter, so recording,
 * undo, custom-state memory and rendering stay owned by the rack. Mappings are a
 * per-user device convenience and are never written into a portable patch.
 */
import {CATALOG_BY_TYPE} from './catalog.mjs';

export const CONTROLLER_MAP_VERSION = 'fx.controller-map.v1';
export const MAX_BINDINGS = 128;
export const GLOBAL_ACTIONS = Object.freeze(['play', 'record', 'loop']);
export const BINDING_MODES = Object.freeze(['absolute', 'relative', 'toggle', 'momentary']);
export const SELECTED = '@selected';

const SOURCE_KINDS = Object.freeze(['cc', 'note', 'pitch-bend']);
const TOGGLE_PATHS = Object.freeze(['bypass', 'solo']);
const PATH_PATTERN = /^(intensity|wet|bypass|solo|params\.[A-Za-z0-9_-]{1,64})$/;
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const isFiniteNumber = value => typeof value === 'number' && Number.isFinite(value);

/** Decode one raw MIDI message. Returns null for anything the rack does not use or that is malformed. */
export function parseMidi(data) {
  if (!data || typeof data.length !== 'number' || data.length < 1) return null;
  const status = data[0];
  if (!Number.isInteger(status) || status < 0x80 || status >= 0xF0) return null;
  const type = status & 0xF0, channel = (status & 0x0F) + 1;
  // Only control change, notes and pitch bend are used; program change and both aftertouch kinds are ignored.
  if (type !== 0x80 && type !== 0x90 && type !== 0xB0 && type !== 0xE0) return null;
  const first = data[1], second = data[2];
  const seven = value => Number.isInteger(value) && value >= 0 && value <= 127;
  if (!seven(first) || !seven(second)) return null;
  if (type === 0xB0) return {kind: 'cc', channel, number: first, value: second, max: 127, norm: second / 127};
  if (type === 0xE0) {
    const value = first | (second << 7);
    return {kind: 'pitch-bend', channel, number: 0, value, max: 16383, norm: value / 16383};
  }
  if (type === 0x90 && second > 0) return {kind: 'note-on', channel, number: first, value: second, max: 127, norm: second / 127};
  return {kind: 'note-off', channel, number: first, value: second, max: 127, norm: 0};
}

/** The binding-facing identity of a parsed message (note-on and note-off share one source). */
export function sourceOf(message) {
  return {kind: message.kind.startsWith('note') ? 'note' : message.kind, channel: message.channel, number: message.number};
}
export const sourceKey = source => `${source.kind}:${source.channel}:${source.number}`;
export function describeSource(source) {
  const channel = source.channel ? ` ch${source.channel}` : ' any ch';
  if (source.kind === 'pitch-bend') return `Pitch bend${channel}`;
  return `${source.kind === 'cc' ? 'CC' : 'Note'} ${source.number}${channel}`;
}

/** Quantise a normalised position onto a control's own range and step. */
export function normToValue(control, norm) {
  const position = clamp(Number.isFinite(norm) ? norm : 0, 0, 1);
  let value = control.min + position * (control.max - control.min);
  if (control.step > 0) {
    const decimals = (String(control.step).split('.')[1] || '').length;
    value = control.min + Math.round((value - control.min) / control.step) * control.step;
    value = Number(value.toFixed(Math.min(10, decimals + 2)));
  }
  return clamp(value, control.min, control.max);
}
export function valueToNorm(control, value) {
  const span = control.max - control.min;
  return span > 0 ? clamp((value - control.min) / span, 0, 1) : 0;
}

/**
 * Addressable controls of one processor, in the order a hardware surface lays
 * them out: ranges first (Intensity, Wet/dry, then processor parameters), then switches.
 */
export function describeControls(node, context = {}) {
  const definition = CATALOG_BY_TYPE[node?.type];
  if (!definition) return {ranges: [], switches: []};
  const limit = isFiniteNumber(context.duration) ? context.duration : Infinity;
  const ranges = [
    {path: 'intensity', label: 'Intensity', kind: 'range', min: 0, max: 1, step: .01, default: 1, unit: '%'},
    {path: 'wet', label: 'Wet / dry', kind: 'range', min: 0, max: 1, step: .01, default: 1, unit: '%'},
    ...definition.params.map(item => ({
      path: `params.${item.key}`, label: item.label, kind: 'range', min: item.min,
      max: item.sourceTime || item.outputTime ? Math.min(item.max, limit) : item.max,
      step: item.step, default: item.default, unit: item.unit || '', options: item.options,
    })),
  ].map(control => ({...control, max: Math.max(control.min, control.max)}));
  const switches = TOGGLE_PATHS.map(path => ({path, label: path === 'bypass' ? 'Bypass' : 'Solo', kind: 'toggle'}));
  return {ranges, switches};
}

/** Look a control up by catalog path or by hardware slot (`knob:N`, `switch:N`). */
export function findControl(node, target, context = {}) {
  const {ranges, switches} = describeControls(node, context);
  if (typeof target.slot === 'string') {
    const [group, index] = target.slot.split(':');
    const list = group === 'knob' ? ranges : group === 'switch' ? switches : [];
    return list[Number(index)] || null;
  }
  return [...ranges, ...switches].find(control => control.path === target.path) || null;
}

function targetKey(target) {
  if (target.global) return `global:${target.global}`;
  return `${target.nodeId}|${target.slot ?? target.path}`;
}

function validateSource(source) {
  if (!source || typeof source !== 'object' || !SOURCE_KINDS.includes(source.kind)) throw Error('Controller binding has an unknown source type.');
  if (!Number.isInteger(source.channel) || source.channel < 0 || source.channel > 16) throw Error('Controller channel must be 0 (any) to 16.');
  if (!Number.isInteger(source.number) || source.number < 0 || source.number > 127) throw Error('Controller number must be 0 to 127.');
  return {kind: source.kind, channel: source.channel, number: source.kind === 'pitch-bend' ? 0 : source.number};
}

function validateTarget(target) {
  if (!target || typeof target !== 'object' || Array.isArray(target)) throw Error('Controller binding needs a target.');
  if (target.global !== undefined) {
    if (!GLOBAL_ACTIONS.includes(target.global)) throw Error('Unknown transport target.');
    return {global: target.global};
  }
  if (typeof target.nodeId !== 'string' || !target.nodeId || target.nodeId.length > 128) throw Error('Controller target needs a processor.');
  if (target.slot !== undefined) {
    if (typeof target.slot !== 'string' || !/^(knob|switch):\d{1,2}$/.test(target.slot)) throw Error('Controller slot must look like knob:0 or switch:1.');
    return {nodeId: target.nodeId, slot: target.slot};
  }
  if (typeof target.path !== 'string' || !PATH_PATTERN.test(target.path)) throw Error('Controller target parameter is not recognised.');
  return {nodeId: target.nodeId, path: target.path};
}

function validateBinding(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw Error('Controller binding must be an object.');
  const source = validateSource(raw.source), target = validateTarget(raw.target);
  const toggleTarget = Boolean(target.global) || TOGGLE_PATHS.includes(target.path) || (target.slot ?? '').startsWith('switch:');
  const mode = raw.mode ?? (toggleTarget ? 'toggle' : 'absolute');
  if (!BINDING_MODES.includes(mode)) throw Error('Unknown controller mode.');
  if (toggleTarget !== (mode === 'toggle' || mode === 'momentary')) throw Error('Switches take toggle or momentary mode; ranges take absolute or relative.');
  const rate = raw.rate ?? 1;
  if (!isFiniteNumber(rate) || rate < .01 || rate > 10) throw Error('Controller rate must be between 0.01 and 10.');
  if (raw.pickup !== undefined && typeof raw.pickup !== 'boolean') throw Error('Controller pickup must be true or false.');
  return {source, target, mode, rate, pickup: raw.pickup ?? (mode === 'absolute')};
}

/**
 * Mutable controller state: bindings, MIDI-learn arming and soft-takeover memory.
 * One binding per source and one per target; binding again replaces the earlier one.
 */
export class ControllerMap {
  constructor(bindings = []) {
    this.bindings = [];
    this.learning = null;
    this.pickup = new Map();
    this.pending = new Map();
    for (const binding of bindings) this.bind(binding);
  }

  bind(raw) {
    const binding = validateBinding(raw);
    const target = targetKey(binding.target);
    // Any-channel (0) sources overlap every channel of the same control, so they replace each other.
    const overlaps = item => item.source.kind === binding.source.kind && item.source.number === binding.source.number
      && (item.source.channel === 0 || binding.source.channel === 0 || item.source.channel === binding.source.channel);
    this.bindings = this.bindings.filter(item => !overlaps(item) && targetKey(item.target) !== target);
    if (this.bindings.length >= MAX_BINDINGS) throw Error(`A controller map holds at most ${MAX_BINDINGS} bindings.`);
    this.bindings.push(binding);
    this.pickup.clear();
    this.pending.clear();
    return binding;
  }

  unbindTarget(target) {
    const key = targetKey(validateTarget(target));
    const before = this.bindings.length;
    this.bindings = this.bindings.filter(item => targetKey(item.target) !== key);
    return before - this.bindings.length;
  }

  bindingForTarget(target) {
    const key = targetKey(target);
    return this.bindings.find(item => targetKey(item.target) === key) || null;
  }

  clear() {this.bindings = []; this.pickup.clear(); this.pending.clear(); this.learning = null;}

  /** Arm MIDI learn: the next usable message binds to this target. */
  armLearn(target) {this.learning = validateTarget(target);}
  cancelLearn() {this.learning = null;}

  /** Forget takeover state, e.g. when the selected processor or the patch changes. */
  resetPickup() {this.pickup.clear(); this.pending.clear();}

  /**
   * Interpret one parsed MIDI message.
   * view: {nodes: [{id,type}], selectedId, duration?, current(nodeId, path) -> number|boolean, global(name) -> boolean}
   * Returns {learned, actions, waiting}. Actions are {nodeId, path, value, binding} or {global, value, binding}.
   */
  process(message, view) {
    const result = {learned: null, actions: [], waiting: []};
    if (!message) return result;
    if (this.learning) {
      if (message.kind === 'note-off') return result;
      const target = this.learning;
      const toggleTarget = Boolean(target.global) || TOGGLE_PATHS.includes(target.path) || (target.slot ?? '').startsWith('switch:');
      const source = sourceOf(message);
      const mode = toggleTarget ? (source.kind === 'note' ? 'toggle' : 'momentary') : 'absolute';
      result.learned = this.bind({source, target, mode});
      this.learning = null;
      return result;
    }
    const key = sourceKey(sourceOf(message));
    const anyChannel = sourceKey({...sourceOf(message), channel: 0});
    for (const binding of this.bindings) {
      const bound = sourceKey(binding.source);
      if (bound !== key && bound !== anyChannel) continue;
      const action = this.#act(binding, bound, message, view, result);
      if (action) result.actions.push(action);
    }
    return result;
  }

  #act(binding, key, message, view, result) {
    const {target, mode} = binding;
    if (target.global) {
      const current = Boolean(view.global?.(target.global));
      const next = this.#switchValue(mode, message, current);
      return next === null || next === current ? null : {global: target.global, value: next, binding: key};
    }
    const nodeId = target.nodeId === SELECTED ? view.selectedId : target.nodeId;
    const node = view.nodes.find(item => item.id === nodeId);
    const control = node && findControl(node, target, view);
    if (!control) return null;
    const current = view.current(nodeId, control.path);
    if (control.kind === 'toggle') {
      const next = this.#switchValue(mode, message, Boolean(current));
      return next === null || next === Boolean(current) ? null : {nodeId, path: control.path, value: next, binding: key};
    }
    if (message.kind === 'note-off') return null;
    let value;
    if (mode === 'relative') {
      if (message.kind !== 'cc' || message.value === 0 || message.value === 64) return null;
      const delta = message.value < 64 ? message.value : message.value - 128;
      // Fractional movement accumulates, so one-tick turns still move coarse stepped parameters.
      const slot = `${key}>${nodeId}|${control.path}`;
      let pending = (this.pending.get(slot) ?? 0) + delta * .01 * binding.rate;
      if (Math.sign(pending) !== Math.sign(delta)) pending = delta * .01 * binding.rate;
      value = normToValue(control, valueToNorm(control, current) + pending);
      if (value === current && ((pending > 0 && value < control.max) || (pending < 0 && value > control.min))) this.pending.set(slot, pending);
      else this.pending.delete(slot);
    } else {
      const slot = `${key}>${nodeId}|${control.path}`;
      const incoming = message.norm, now = valueToNorm(control, current);
      if (binding.pickup) {
        const state = this.pickup.get(slot) ?? {caught: false, last: null, sent: null};
        // A value changed from elsewhere (slider, preset, undo, automation) since this knob last wrote it: take over again.
        if (state.caught && state.sent !== null && current !== state.sent) {state.caught = false; state.last = null;}
        const crossed = state.last !== null && (state.last - now) * (incoming - now) <= 0;
        if (!state.caught && (Math.abs(incoming - now) <= .02 || crossed)) state.caught = true;
        state.last = incoming;
        this.pickup.set(slot, state);
        if (!state.caught) {result.waiting.push({nodeId, path: control.path, hardware: incoming, software: now}); return null;}
      }
      value = normToValue(control, incoming);
      if (binding.pickup) this.pickup.get(slot).sent = value;
    }
    return value === current ? null : {nodeId, path: control.path, value, binding: key};
  }

  #switchValue(mode, message, current) {
    if (mode === 'toggle') return message.kind === 'note-off' || message.norm === 0 ? null : (message.kind === 'cc' && message.value < 64 ? null : !current);
    if (message.kind === 'note-off') return false;
    return message.kind === 'note-on' ? true : message.norm >= .5;
  }

  toJSON() {
    return {version: CONTROLLER_MAP_VERSION, bindings: this.bindings.map(item => ({source: {...item.source}, target: {...item.target}, mode: item.mode, rate: item.rate, pickup: item.pickup}))};
  }

  /** Strictly validate saved or imported JSON; throws before any state is created. */
  static fromJSON(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw Error('Controller map must be an object.');
    if (input.version !== CONTROLLER_MAP_VERSION) throw Error(`Controller map version must be ${CONTROLLER_MAP_VERSION}.`);
    if (!Array.isArray(input.bindings) || input.bindings.length > MAX_BINDINGS) throw Error(`Controller map needs a list of at most ${MAX_BINDINGS} bindings.`);
    return new ControllerMap(input.bindings);
  }
}

/**
 * A ready-made template, like a DAW's "assign to selected track": eight knobs on
 * consecutive CCs, bypass/solo on two notes, transport on the next three.
 * Every binding follows the selected processor, so one hardware strip drives the whole rack.
 */
export function defaultTemplate({channel = 0, firstCC = 20, firstNote = 36, knobs = 8} = {}) {
  const bindings = [];
  for (let i = 0; i < knobs; i++) bindings.push({source: {kind: 'cc', channel, number: firstCC + i}, target: {nodeId: SELECTED, slot: `knob:${i}`}, mode: 'absolute', pickup: true});
  for (let i = 0; i < 2; i++) bindings.push({source: {kind: 'note', channel, number: firstNote + i}, target: {nodeId: SELECTED, slot: `switch:${i}`}, mode: 'toggle'});
  GLOBAL_ACTIONS.forEach((name, i) => bindings.push({source: {kind: 'note', channel, number: firstNote + 2 + i}, target: {global: name}, mode: 'toggle'}));
  return bindings;
}

/**
 * One undo snapshot per physical gesture. A control that has been quiet for
 * `gapMs` starts a new gesture; a stream of controller messages inside the gap does not.
 */
export class GestureTracker {
  constructor(gapMs = 600) {this.gapMs = gapMs; this.last = new Map();}
  touch(key, now) {
    const previous = this.last.get(key);
    this.last.set(key, now);
    if (this.last.size > 512) for (const [name, at] of this.last) if (now - at > this.gapMs) this.last.delete(name);
    return previous === undefined || now - previous > this.gapMs;
  }
  end(key) {this.last.delete(key);}
}

/** Human text for a value on a control: option names, percentages or the unit. */
export function formatValue(control, value) {
  if (control.kind === 'toggle') return value ? 'On' : 'Off';
  if (control.options) return String(control.options[Math.round(value)] ?? value);
  if (control.unit === '%') return `${Math.round(value * 100)}%`;
  const text = Number.isInteger(value) ? String(value) : Number(value).toFixed(Math.abs(value) < .1 ? 3 : 2).replace(/0+$/, '').replace(/\.$/, '');
  return control.unit ? `${text} ${control.unit}` : text;
}
