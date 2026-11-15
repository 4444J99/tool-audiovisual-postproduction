/**
 * Declared latency of the generated network.
 *
 * Reverse and Stutter output the source at times AHEAD of their own playhead (the workbench reads a finite interval
 * of the source at random access). A live network cannot read a signal that has not arrived, so such a processor is
 * given a fixed, declared latency L (a look-ahead buffer): the stream it receives runs L seconds ahead of the stream it
 * emits, and it reads the history it keeps (a Cache TOP) at depths up to L ahead of its output time.
 *
 * Latency is a path sum, as in a digital audio workstation. Every stream has a lag: the number of seconds its newest
 * sample is behind the live edge (the source has lag 0). A processor with no look-ahead keeps the lag of its
 * inputs; a processor with look-ahead L emits at lag (input lag + L). Where wires of different lag meet (a mix, a
 * processor with several inputs) the shorter ones are delayed to the longest, and a processor with look-ahead also
 * delays its own dry branch, so wet/dry and bypass stay aligned. The rack's latency is the lag of the output node.
 * Everything a processor evaluates (automation, events, macros, Reset time, Gate period, Displacement field) reads its
 * own clock, the rack Time minus its output lag.
 *
 * The look-ahead a processor needs is bounded by the catalog's extremes, not by what a patch currently holds, because
 * every parameter can be edited at any time. The bound is capped at `maxLatency` (default 2.5 s): a larger
 * request is not silently read as a shorter one; the resolver limits the parameter to what the declared latency can
 * cover (`limits` in the resolve block) and the generated spec says so in its warnings. tests/test_td_latency.py sweeps the
 * workbench's own source-time functions over the catalog extremes and checks these bounds hold.
 */
import {CATALOG_BY_TYPE} from '../../web/fx/catalog.mjs';

export const DEFAULT_MAX_LATENCY = 2.5;
const STRUCTURAL = new Set(['source', 'mix', 'output']);
const range = (type, key) => CATALOG_BY_TYPE[type].params.find(item => item.key === key);

/**
 * Seconds of source a processor can read ahead of its output time, at the catalog's extremes.
 *  Reverse (playback): the interval end is at most `span` ahead of the output time (the block it is in starts no later).
 *  Stutter: repeat 0 plays source at `rate` times real time from the capture, so after `phase` seconds it is
 *  phase * (rate - 1) ahead; the largest phase is window / rate, so the lead is window * (1 - 1 / rate).
 */
export const LOOKAHEAD = Object.freeze({
  reverse: () => range('reverse', 'span').max,
  stutter: () => range('stutter', 'window').max * (1 - 1 / range('stutter', 'rate').max),
});

/** The parameters whose range the declared latency has to bound, and what bounds them (the look-ahead itself). */
export const LIMITED = Object.freeze({reverse: ['span']});

/** Cache depth, in seconds, a processor needs behind its input's live edge. */
export const HISTORY = Object.freeze({
  reverse: lookahead => lookahead + Math.min(range('reverse', 'span').max, lookahead),   // reads back to the start of its interval
  stutter: () => range('stutter', 'window').max,                                        // the captured window
});

export const hasLookahead = type => type in LOOKAHEAD;

/** The look-ahead a node declares: the catalog bound, capped. 0 for a processor that needs none. */
export function lookaheadOf(type, maxLatency = DEFAULT_MAX_LATENCY) {
  return hasLookahead(type) ? Math.min(LOOKAHEAD[type](), maxLatency) : 0;
}

/**
 * Lags for a whole patch. Returns Map id -> {lagIn, lookahead, lag}: `lagIn` is the lag of the node's aligned inputs,
 * `lookahead` its own, `lag` the lag of what it emits. `wires` lists, per edge, the delay that edge needs to reach
 * its destination's `lagIn`. `seconds` is the rack's latency (the output node's lag).
 */
export function planLatency(patch, maxLatency = DEFAULT_MAX_LATENCY) {
  if (!(Number.isFinite(maxLatency) && maxLatency > 0)) throw Error('maxLatency must be a positive number of seconds.');
  const byId = new Map(patch.nodes.map(node => [node.id, node]));
  const incoming = new Map(patch.nodes.map(node => [node.id, []]));
  for (const edge of patch.connections) incoming.get(edge.to)?.push(edge);
  const lags = new Map();
  const visit = id => {
    if (lags.has(id)) return lags.get(id);
    if (lags.get(id) === null) throw Error('the signal wires form a cycle');
    lags.set(id, null);
    const node = byId.get(id);
    const edges = incoming.get(id);
    const lagIn = Math.max(0, ...edges.map(edge => visit(edge.from).lag));
    const lookahead = STRUCTURAL.has(node.type) ? 0 : lookaheadOf(node.type, maxLatency);
    const entry = {lagIn, lookahead, lag: lagIn + lookahead};
    lags.set(id, entry);
    return entry;
  };
  for (const node of patch.nodes) visit(node.id);
  const wires = patch.connections.map(edge => ({from: edge.from, to: edge.to, port: edge.port, delay: lags.get(edge.to).lagIn - lags.get(edge.from).lag}));
  const output = patch.nodes.find(node => node.type === 'output');
  return {nodes: lags, wires, seconds: output ? lags.get(output.id).lag : 0};
}

/**
 * The frames a Cache TOP needs to hold `seconds`: an expression of the project's cook rate, so the cache always covers the delay
 * the read indices ask for (`round(seconds * project.cookRate)` frames back) however fast the project cooks. Two frames are added
 * for the frame being cached.
 */
export const cacheSizeText = seconds => `max(2, int(math.ceil(${seconds} * project.cookRate)) + 2)`;
/** The read index for a delay of `seconds` into a cache sized by cacheSizeText(cacheSeconds): negative, never past the oldest frame. */
export const cacheIndexText = (seconds, cacheSeconds) => `-min(int(math.ceil(${cacheSeconds} * project.cookRate)) + 1, int(round(${seconds} * project.cookRate)))`;
