/**
 * OSC address space for the FX rack, in both directions, with no DOM, network or
 * workbench state. It is the TouchDesigner counterpart of controller-map.mjs: the
 * workbench supplies a `view` (see ControllerMap.process) and applies the returned
 * actions through its own parameter setter, so recording, undo and rendering are
 * identical to a mouse, touch or MIDI edit.
 *
 * Inbound (TouchDesigner -> rack). Ranges take a normalised 0..1 value that is scaled
 * onto the control's own range and step; switches take 0/1 or a boolean.
 *   /fx/selected/knob/<n>            n-th range control of the selected processor
 *   /fx/selected/switch/<n>          0 = Bypass, 1 = Solo
 *   /fx/node/<id>/intensity|wet      by processor id
 *   /fx/node/<id>/params/<key>       processor parameter
 *   /fx/node/<id>/bypass|solo        switches
 *   /fx/transport/play|record|loop   switches
 *   /fx/selected  <id>               select a processor by id
 *   /fx/select/next | /fx/select/prev
 *   /fx/sync                         ask for the full state to be sent again
 *   /fx/latency <seconds>            the network's declared latency; /fx/time is then reported that far ahead of the playhead
 * Outbound (rack -> TouchDesigner) uses /fx/node/<id>/..., /fx/transport/..., plus
 * /fx/selected <id>, /fx/selected/type, /fx/time and /fx/count, and the rule messages
 *   /fx/node/<id>/released/<path>    the control's events, automation lane and macro mapping are gone
 *   /fx/node/<id>/event/<path>       time, value: one recorded event (inserted, or its value replaced)
 *   /fx/node/<id>/lane/<path>        one JSON string {interpolation, keyframes}: the control's automation lane
 *   /fx/node/<id>/macro/<path>       one JSON string {min, max, curve}: the macro mapping of a processor parameter
 * where <path> is intensity, wet, bypass, solo or params/<key>. See docs/FX-TOUCHDESIGNER.md.
 *
 * Processor ids can contain characters OSC reserves, so each address segment is
 * percent-encoded for everything outside A-Z a-z 0-9 _ . -.
 */
import {describeControls, findControl, normToValue, valueToNorm, GLOBAL_ACTIONS} from './controller-map.mjs';

export const OSC_ROOT = 'fx';
/** The longest latency the rack accepts from a network (seconds); anything else is ignored. */
export const MAX_LATENCY = 60;
const SAFE = /[A-Za-z0-9_.-]/;
const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder('utf-8', {fatal: true});
/** Reported values carry 4 decimals and travel as float32; anything this close to the current value is an echo. */
export const ECHO_TOLERANCE = 2e-4;
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

/** Encode one address segment so it holds no OSC-reserved character. */
export function encodeSegment(text) {
  let out = '';
  for (const byte of textEncoder.encode(String(text))) {
    const char = String.fromCharCode(byte);
    out += byte < 128 && SAFE.test(char) ? char : `%${byte.toString(16).toUpperCase().padStart(2, '0')}`;
  }
  return out;
}

/** Reverse of encodeSegment. Returns null for malformed escapes or invalid UTF-8. */
export function decodeSegment(text) {
  const bytes = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '%') {
      const hex = text.slice(i + 1, i + 3);
      if (!/^[0-9A-Fa-f]{2}$/.test(hex)) return null;
      bytes.push(parseInt(hex, 16)); i += 2;
    } else if (text.charCodeAt(i) < 128) bytes.push(text.charCodeAt(i));
    else return null;
  }
  try {return textDecoder.decode(Uint8Array.from(bytes));} catch {return null;}
}

/** The rule message kinds the rack reports to a mirroring network, besides control values. */
export const RULE_KINDS = Object.freeze(['released', 'event', 'lane', 'macro']);
/** A lane whose JSON is longer than this is not mirrored: each message leaves the bridge as one UDP datagram, and macOS
 *  refuses datagrams over 9216 bytes (net.inet.udp.maxdgram). The mirror drops its copy of that control's rules instead. */
export const MAX_RULE_CHARS = 8000;
/** Event times travel with this many decimals (float32 on the wire keeps them to about a microsecond). */
export const EVENT_TIME_DECIMALS = 4;

const pathSegments = path => path.startsWith('params.') ? ['params', encodeSegment(path.slice(7))] : [path];
export const nodeAddress = (id, path) => `/${OSC_ROOT}/node/${encodeSegment(id)}/${pathSegments(path).join('/')}`;
/** The address of one rule message (`kind` is one of RULE_KINDS) for a control path. */
export const ruleAddress = (id, kind, path) => `/${OSC_ROOT}/node/${encodeSegment(id)}/${kind}/${pathSegments(path).join('/')}`;

const isNumber = value => typeof value === 'number' && Number.isFinite(value);
/** A switch value: booleans, or numbers at or above one half. No argument means "trigger". */
function truthy(args) {
  if (!args.length) return true;
  const value = args[0];
  if (typeof value === 'boolean') return value;
  return isNumber(value) ? value >= .5 : null;
}
function normalised(args) {
  const value = args[0];
  if (typeof value === 'boolean') return value ? 1 : 0;
  return isNumber(value) ? clamp(value, 0, 1) : null;
}

/**
 * Interpret one OSC message from TouchDesigner.
 * Returns {actions, select, sync, ignored}; `select` is {delta} or {nodeId}. Never throws:
 * a message that is not understood is `ignored`, so a noisy sender cannot disturb the rack.
 */
export function interpretOsc(message, view) {
  const result = {actions: [], select: null, sync: false, ignored: false};
  const ignore = () => {result.ignored = true; return result;};
  if (!message || typeof message.address !== 'string' || !Array.isArray(message.args)) return ignore();
  const parts = message.address.split('/');
  if (parts[0] !== '' || parts[1] !== OSC_ROOT) return ignore();
  const [, , area, ...rest] = parts;
  const args = message.args;

  const drive = (nodeId, control) => {
    if (!control) return ignore();
    const current = view.current(nodeId, control.path);
    if (control.kind === 'toggle') {
      const next = truthy(args);
      if (next === null) return ignore();
      if (next !== Boolean(current)) result.actions.push({nodeId, path: control.path, value: next, binding: 'osc'});
    } else {
      const norm = normalised(args);
      if (norm === null) return ignore();
      // A value that merely echoes what the rack reported (4 decimals, float32 on the wire) is not an edit:
      // off-grid presets and automation values must not be snapped onto the step grid by their own report.
      if (Math.abs(norm - valueToNorm(control, current)) <= ECHO_TOLERANCE) return result;
      const value = normToValue(control, norm);
      if (value !== current) result.actions.push({nodeId, path: control.path, value, binding: 'osc'});
    }
    return result;
  };

  if (area === 'sync' && !rest.length) {result.sync = true; return result;}
  // The network's declared latency: its output at rack time T shows workbench time T - latency, so the rack reports its playhead that far ahead.
  if (area === 'latency' && !rest.length) {
    if (args.length !== 1 || !isNumber(args[0]) || args[0] < 0 || args[0] > MAX_LATENCY) return ignore();
    result.latency = args[0];
    return result;
  }
  if (area === 'select' && rest.length === 1 && (rest[0] === 'next' || rest[0] === 'prev')) {
    if (truthy(args) !== true) return result;
    result.select = {delta: rest[0] === 'next' ? 1 : -1};
    return result;
  }
  if (area === 'selected' && !rest.length) {
    if (typeof args[0] !== 'string' || !view.nodes.some(node => node.id === args[0])) return ignore();
    result.select = {nodeId: args[0]};
    return result;
  }
  if (area === 'transport' && rest.length === 1) {
    if (!GLOBAL_ACTIONS.includes(rest[0])) return ignore();
    const next = truthy(args);
    if (next === null) return ignore();
    if (next !== Boolean(view.global?.(rest[0]))) result.actions.push({global: rest[0], value: next, binding: 'osc'});
    return result;
  }
  if (area === 'selected' && rest.length === 2 && (rest[0] === 'knob' || rest[0] === 'switch') && /^\d{1,2}$/.test(rest[1])) {
    const node = view.nodes.find(item => item.id === view.selectedId);
    return node ? drive(node.id, findControl(node, {slot: `${rest[0]}:${Number(rest[1])}`}, view)) : ignore();
  }
  if (area === 'node' && rest.length >= 2) {
    const nodeId = decodeSegment(rest[0]);
    const node = nodeId === null ? null : view.nodes.find(item => item.id === nodeId);
    if (!node) return ignore();
    let path;
    if (rest.length === 2) path = rest[1];
    else if (rest.length === 3 && rest[1] === 'params') {const key = decodeSegment(rest[2]); path = key === null ? null : `params.${key}`;}
    if (!path) return ignore();
    return drive(node.id, findControl(node, {path}, view));
  }
  return ignore();
}

const round = (value, places) => Math.round(value * 10 ** places) / 10 ** places;

/**
 * Tracks what TouchDesigner was last told and reports only what changed, so the page
 * can poll the rack cheaply. `collect(view, {full})` returns OSC messages
 * `{address, args}`; `full` resends everything (on connect, or when TouchDesigner asks /fx/sync).
 */
export class OscFeedback {
  constructor() {this.last = new Map(); this.time = null; this.rules = new Map(); this.latency = 0;}
  reset() {this.last.clear(); this.time = null; this.rules.clear(); this.latency = 0;}
  /** The network declared its latency: the playhead is reported that many seconds ahead from now on. */
  setLatency(seconds) {this.latency = seconds; this.time = null;}

  /**
   * The rule messages for one processor: what the mirroring network must do to match the workbench's rules now.
   * A processor not seen before (first report, after a connect or /fx/sync) is resynchronised whole: every control's
   * rules are released, then the current ones are sent. Afterwards only differences are sent. A rule that vanished
   * (a hand edit, a preset, Clear) releases its control, and the control's remaining rules are sent again, since a
   * release drops all of them.
   */
  collectRules(node, view) {
    const rules = view.rules(node.id);
    if (!rules) return [];
    const model = new Map();   // `${kind}|${path}[|${time}]` -> {path, kind, args | null}
    const set = (kind, path, time, args) => model.set(time === null ? `${kind}|${path}` : `${kind}|${path}|${time}`, {kind, path, time, args});
    for (const event of rules.events) {
      const value = typeof event.value === 'boolean' ? (event.value ? 1 : 0) : event.value;
      if (isNumber(event.time) && isNumber(value)) set('event', event.parameter, round(event.time, EVENT_TIME_DECIMALS), [round(event.time, EVENT_TIME_DECIMALS), value]);
    }
    for (const lane of rules.automation) {
      const text = JSON.stringify({interpolation: lane.interpolation, keyframes: lane.keyframes.map(frame => ({time: frame.time, value: frame.value}))});
      set('lane', lane.parameter, null, text.length > MAX_RULE_CHARS ? null : [text]);
    }
    for (const mapping of rules.macro) set('macro', `params.${mapping.parameter}`, null, [JSON.stringify({min: mapping.min, max: mapping.max, curve: mapping.curve})]);
    const before = this.rules.get(node.id);
    this.rules.set(node.id, model);
    const released = new Set();
    if (!before) for (const control of [...describeControls(node, view).ranges, ...describeControls(node, view).switches]) released.add(control.path);
    else for (const [key, entry] of before) if (!model.has(key) || (model.get(key).args === null) !== (entry.args === null)) released.add(entry.path);
    const out = [];
    for (const path of [...released].sort()) out.push({address: ruleAddress(node.id, 'released', path), args: []});
    const entries = [...model.values()].sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : (a.time ?? 0) - (b.time ?? 0));
    for (const entry of entries) {
      if (entry.args === null) {   // too large to mirror: the released copy stays released
        if (!released.has(entry.path) && !before.has(`${entry.kind}|${entry.path}`)) out.push({address: ruleAddress(node.id, 'released', entry.path), args: []});
        continue;
      }
      const key = entry.time === null ? `${entry.kind}|${entry.path}` : `${entry.kind}|${entry.path}|${entry.time}`;
      const previous = before?.get(key);
      const unchanged = previous && previous.args !== null && JSON.stringify(previous.args) === JSON.stringify(entry.args);
      if (unchanged && !released.has(entry.path)) continue;
      out.push({address: ruleAddress(node.id, entry.kind, entry.path), args: entry.args});
    }
    return out;
  }

  collect(view, {full = false} = {}) {
    if (full) this.reset();
    const out = [];
    const emit = (address, args) => {
      const key = JSON.stringify(args);
      if (this.last.get(address) === key) return;
      this.last.set(address, key);
      out.push({address, args});
    };
    const fx = view.nodes;
    emit(`/${OSC_ROOT}/count`, [fx.length]);
    for (const node of fx) {
      emit(`/${OSC_ROOT}/node/${encodeSegment(node.id)}/type`, [String(node.type)]);
      const {ranges, switches} = describeControls(node, view);
      for (const control of ranges) emit(nodeAddress(node.id, control.path), [round(valueToNorm(control, view.current(node.id, control.path)), 4)]);
      for (const control of switches) emit(nodeAddress(node.id, control.path), [view.current(node.id, control.path) ? 1 : 0]);
    }
    // Rules go after the values, so a mirroring network that releases a control has already been given its base value.
    if (typeof view.rules === 'function') for (const node of fx) out.push(...this.collectRules(node, view));
    const selected = fx.find(node => node.id === view.selectedId);
    emit(`/${OSC_ROOT}/selected`, [selected ? selected.id : '']);
    emit(`/${OSC_ROOT}/selected/type`, [selected ? String(selected.type) : '']);
    for (const name of GLOBAL_ACTIONS) emit(`/${OSC_ROOT}/transport/${name}`, [view.global?.(name) ? 1 : 0]);
    if (isNumber(view.time) && (this.time === null || Math.abs(view.time + this.latency - this.time) >= .04)) {
      this.time = view.time + this.latency;
      out.push({address: `/${OSC_ROOT}/time`, args: [round(this.time, 3)]});
    }
    return out;
  }
}
