/**
 * The effects of the FX rack as TouchDesigner operator chains.
 *
 * A processor component holds `in_<port>` -> (chain built here) -> `proc_<port>` -> wet/dry blend -> bypass -> `out_<port>`.
 * Each builder below adds its operators through `add(name, type, {inputs, pars})` and returns the name of the chain's
 * last operator; `proc_<port>` is a Null after it. The network is static, so a count parameter (Taps) is a fixed
 * maximum number of stages whose gain is switched by the resolved value; nothing is created at run time.
 *
 * Every parameter value that follows the workbench is an expression over the processor's `resolved` table
 * (automation, events, macro, sidechain and Solo already applied, see td_resolve) written as a template:
 *
 *   {taps}        the resolved parameter `taps`        {intensity} {wet}   the resolved common values
 *   {rack.Time}   the rack's playhead (seconds)        {op:name}           a sibling operator inside the processor
 *
 * `expr(template)` turns a template into {expr, resolved, rack, siblings} so the validator can check that every row,
 * rack parameter and sibling the expression reads exists.
 */
import {HISTORY, cacheSizeText, cacheIndexText} from './latency.mjs';

/** Rows every processor's `resolved` table can carry besides `param.<key>`: `epoch` (Feedback), and for a processor behind a
 *  look-ahead buffer `clock` (its own time) plus the derived reads of Reverse (`depth`, `shift`) and Stutter (`pick`, `depth`, `record`). */
const COMMON = new Set(['intensity', 'wet', 'bypass', 'solo', 'active', 'epoch', 'clock', 'depth', 'shift', 'pick', 'record']);
export const rowName = key => (COMMON.has(key) ? key : `param.${key}`);
const RESOLVED = key => `float(parent().op('resolved')['${rowName(key)}',1].val)`;

export function expr(template) {
  const resolved = [], rack = [], siblings = [];
  const text = template.replace(/\{(rack\.([A-Za-z]+)|op:([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z][A-Za-z0-9]*))\}/g, (_, whole, rackName, sibling, key) => {
    if (rackName) { if (!rack.includes(rackName)) rack.push(rackName); return `parent().parent().par.${rackName}`; }
    if (sibling) { if (!siblings.includes(sibling)) siblings.push(sibling); return `parent().op('${sibling}')`; }
    if (!resolved.includes(rowName(key))) resolved.push(rowName(key));
    return RESOLVED(key);
  });
  return {expr: text, resolved, rack, siblings};
}

const MAX_TAPS = 8;
/** Frames of picture history the Delay keeps (4 s at 60 fps); a longer delay times a tap number is switched off. */
const CACHE_FRAMES = 240;
const indices = count => Array.from({length: count}, (_, i) => i);

/** Signal Damage: tone-level posterize and nearest-neighbour mosaic; drive, sample-rate hold, bit-depth quantize, bandwidth limit and output level. */
function damage(add) {
  const picture = () => {
    add('dmg_levels', 'levelTOP', {inputs: ['in_picture'], pars: {stepping: 1, stepsize: expr('1 / max(2, {levels})')}});
    add('dmg_small', 'fitTOP', {inputs: ['dmg_levels'], pars: {
      outputresolution: 'custom', inputfiltertype: 'nearest',
      resolutionw: expr('max(2, int(round(1 / {block})))'),
      resolutionh: expr("max(2, int(round(1 / {block} * {op:in_picture}.height / {op:in_picture}.width)))"),
    }});
    add('dmg_full', 'fitTOP', {inputs: ['dmg_small'], pars: {
      outputresolution: 'custom', inputfiltertype: 'nearest',
      resolutionw: expr('{op:in_picture}.width'),
      resolutionh: expr('{op:in_picture}.height'),
    }});
    return 'dmg_full';
  };
  const audio = () => {
    add('dmg_drive', 'mathCHOP', {inputs: ['in_audio'], pars: {gain: expr('{drive}')}});
    // sample-rate reduction: a new sample is taken once the hold phase (rate / sample rate per sample, from 1) reaches 1, i.e. at
    // sample ceil(k * step - 1) for the k-th hold of `step` samples; the workbench holds before it quantises
    const step = 'max(1.0, {op:dmg_drive}.rate / min({op:dmg_drive}.rate, max(100, {rate})))';
    add('dmg_hold', 'expressionCHOP', {inputs: ['dmg_drive'], pars: {expr0expr: expr(`{op:dmg_drive}[me.chanIndex][max(0, int(math.ceil(int((me.sampleIndex + 1) / (${step})) * (${step}) - 1)))]`)}});
    add('dmg_bits', 'expressionCHOP', {inputs: ['dmg_hold'], pars: {expr0expr: expr('max(-1, min(1, round(me.inputVal * (2 ** ({bits} - 1))) / (2 ** ({bits} - 1))))')}});
    add('dmg_band', 'lagCHOP', {inputs: ['dmg_bits'], pars: {lag1: expr('1 / (6.283185307 * max(1, {band}))'), lag2: expr('1 / (6.283185307 * max(1, {band}))')}});
    add('dmg_out', 'mathCHOP', {inputs: ['dmg_band'], pars: {gain: expr('{output}')}});
    return 'dmg_out';
  };
  return {picture, audio};
}

/** Delay / Echo: up to eight taps; tap n sits at n x Tap delay with gain Tap decay^n while n <= Echo taps. */
function delay(add) {
  const tapGain = i => `({decay} ** ${i + 1}) if ${i + 1} <= {taps} else 0`;
  const audio = () => {
    const names = indices(MAX_TAPS).map(i => {
      add(`dly_tap_${i}`, 'delayCHOP', {inputs: ['in_audio'], pars: {delay: expr(`{time} * ${i + 1}`)}});
      add(`dly_gain_${i}`, 'mathCHOP', {inputs: [`dly_tap_${i}`], pars: {gain: expr(tapGain(i))}});
      return `dly_gain_${i}`;
    });
    add('dly_sum', 'mathCHOP', {inputs: names, pars: {chopop: 'add'}});   // delayed returns only: the processor's dry branch carries the direct signal
    return 'dly_sum';
  };
  const picture = () => {
    add('dly_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: CACHE_FRAMES, active: 1}});
    const names = indices(MAX_TAPS).map(i => {
      add(`dly_frame_${i}`, 'cacheselectTOP', {pars: {cachetop: {ref: 'dly_cache'}, index: expr(`-min(${CACHE_FRAMES - 1}, int(round({time} * ${i + 1} * project.cookRate)))`)}});
      // a tap that would reach further back than the cache holds is switched off, never collapsed onto the oldest frame
      add(`dly_copy_${i}`, 'levelTOP', {inputs: [`dly_frame_${i}`], pars: {opacity: expr(`(${tapGain(i)}) if int(round({time} * ${i + 1} * project.cookRate)) < ${CACHE_FRAMES} else 0`)}});
      return `dly_copy_${i}`;
    });
    add('dly_mix', 'addTOP', {inputs: ['in_picture', ...names]});
    return 'dly_mix';
  };
  return {picture, audio};
}

/** Gate / Erase: a periodic (or sidechain-threshold) opening, edge-smoothed, closed state held at Closed-state residue. */
function gate(add, ctx = {}) {
  const T = ctx.clock ?? '{rack.Time}';
  const open = `(1 if ((${T} / max(0.1, {period}) + {phase}) % 1) < {duty} else 0) if {detector} < 0.5 else (1 if (float({op:in_control}[0].eval()) if {op:in_control}.numChans else 0) > {threshold} else 0)`;
  const shared = () => {
    add('gate_raw', 'constantCHOP', {pars: {const0name: 'open', const0value: expr(`{retain} + (1 - {retain}) * (${open})`)}});
    add('gate_ctrl', 'lagCHOP', {inputs: ['gate_raw'], pars: {lag1: expr('max(0.002, {edge})'), lag2: expr('max(0.002, {edge})')}});
  };
  const audio = () => { add('gate_audio', 'mathCHOP', {inputs: ['in_audio'], pars: {gain: expr('float({op:gate_ctrl}[0].eval())')}}); return 'gate_audio'; };
  const picture = () => { add('gate_picture', 'levelTOP', {inputs: ['in_picture'], pars: {opacity: expr('float({op:gate_ctrl}[0].eval())')}}); return 'gate_picture'; };
  return {shared, picture, audio};
}

/** The largest Maximum recurrences the catalog allows; the network is static, so that many return stages exist. */
const MAX_RECURRENCES = 16;

/**
 * Feedback: the workbench draws the input, then for each recurrence k = 1 .. passes the picture from k x Feedback delay
 * earlier, scaled by Scale per recurrence^k, rotated by Rotation per recurrence x k and blended at Return gain^k, where
 * passes = min(Maximum recurrences, floor((time - epoch) / Feedback delay)), the returns stop at the first one whose gain is
 * below Tail threshold, and epoch is the time the playhead last reached Reset time (0 before; it is latched, see EpochLatch in
 * td_resolve, because an automated Reset time can track the playhead without ever crossing it). Each recurrence reads the
 * history, not its own output, so it is a finite set of Cache Select taps, not a Feedback TOP loop. A tap that would reach
 * further back than the cache holds is switched off (as in Delay / Echo), never collapsed onto the oldest frame.
 */
function feedback(add, ctx = {}) {
  const T = ctx.clock ?? '{rack.Time}';
  const frames = k => `int(round({time} * ${k} * project.cookRate))`;
  // `epoch` is the latched reset crossing the resolver keeps (td_resolve EpochLatch): 0 until Reset time is reached, then that time
  const passes = `min(int(round({repeats})), int((${T} - {epoch}) / max(0.001, {time})))`;
  const picture = () => {
    add('fb_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: CACHE_FRAMES, active: 1}});
    let below = 'in_picture';
    for (let k = 1; k <= MAX_RECURRENCES; k++) {
      add(`fb_frame_${k}`, 'cacheselectTOP', {pars: {cachetop: {ref: 'fb_cache'}, index: expr(`-min(${CACHE_FRAMES - 1}, ${frames(k)})`)}});
      // outside the scaled and rotated picture the return is transparent (extend: zero), so it leaves what is below untouched
      add(`fb_move_${k}`, 'transformTOP', {inputs: [`fb_frame_${k}`], pars: {sx: expr(`{scale} ** ${k}`), sy: expr(`{scale} ** ${k}`), rotate: expr(`{rotation} * 360 * ${k}`), extend: 'zero'}});
      // the return's own alpha is Return gain^k; a return that is past the pass count, under the tail threshold or beyond the cache has none
      add(`fb_gain_${k}`, 'levelTOP', {inputs: [`fb_move_${k}`], pars: {opacity: expr(`({gain} ** ${k}) if (${k} <= ${passes} and ({gain} ** ${k}) >= {threshold} and ${frames(k)} < ${CACHE_FRAMES}) else 0`)}});
      // source-over, as the workbench draws each return onto the picture so far (Over TOP: input 1 over input 2)
      add(`fb_over_${k}`, 'overTOP', {inputs: [`fb_gain_${k}`, below]});
      below = `fb_over_${k}`;
    }
    return below;
  };
  return {picture};
}

/** Displacement: a moving noise field (Field scale, Field movement) pulls the picture by Visual displacement along Direction. */
function displacement(add, ctx = {}) {
  const T = ctx.clock ?? '{rack.Time}';
  const picture = () => {
    add('dsp_field', 'noiseTOP', {pars: {period: expr('1 / max(0.05, {scale})'), tz: expr(`${T} * {speed}`), mono: 1, seed: 1, amp: 1}});
    add('dsp_pull', 'displaceTOP', {inputs: ['in_picture', 'dsp_field'], pars: {
      displaceweightx: expr('{amount} * 0.2 * (0 if {direction} == 1 else 1)'),
      displaceweighty: expr('{amount} * 0.2 * (0 if {direction} == 0 else 1)'),
    }});
    return 'dsp_pull';
  };
  return {picture};
}

/**
 * Freeze / Hold: for Hold duration seconds from Capture time the picture is the frame held at the capture, faded toward
 * 0.2 by Hold decay across the hold, shaken sideways by Micro motion; the voice is two overlapped Hann-windowed grains of
 * Voice hold grain seconds read from the capture, decaying at Hold decay per second, with a 6 ms onset and a Release edge
 * tail. Outside the hold both pass through. The picture holds by switching the cache off at the capture; the voice
 * reads the history through two Delay CHOPs whose delay is the age of the grain being repeated. A grain that would start
 * after the live signal (the second grain, in the first half grain of the hold) reads the live signal instead.
 */
function freeze(add, ctx = {}) {
  const T = ctx.clock ?? '{rack.Time}';
  const held = `(${T} >= {capture} and ${T} < {capture} + {duration})`;
  const elapsed = `(${T} - {capture})`;
  const size = 'max(0.001, {grain})';
  const phase = `(${elapsed} % ${size})`;
  const phase2 = `((${phase} + ${size} / 2) % ${size})`;
  const w1 = `(0.5 - 0.5 * math.cos(6.283185307179586 * ${phase} / ${size}))`;
  const gain = `(math.exp(-max(0, {decay}) * ${elapsed}) * min(1, max(0, ${elapsed} / 0.006)) * min(1, max(0, ({duration} - ${elapsed}) / max(0.001, {release}))))`;
  const picture = () => {
    // the cache stops taking frames while the hold lasts; its newest frame is then the one held at the capture
    add('fz_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: 2, active: expr(`0 if ${held} else 1`)}});
    add('fz_frame', 'cacheselectTOP', {pars: {cachetop: {ref: 'fz_cache'}, index: 0}});
    add('fz_pick', 'switchTOP', {inputs: ['in_picture', 'fz_frame'], pars: {index: expr(`1 if ${held} else 0`)}});
    add('fz_shake', 'transformTOP', {inputs: ['fz_pick'], pars: {tx: expr(`(math.sin(${T} * 7) * {motion}) if ${held} else 0`)}});
    add('fz_fade', 'levelTOP', {inputs: ['fz_shake'], pars: {opacity: expr(`max(0.2, 1 - {decay} * ${elapsed} / max(0.001, {duration})) if ${held} else 1`)}});
    return 'fz_fade';
  };
  const audio = () => {
    add('fz_tap_1', 'delayCHOP', {inputs: ['in_audio'], pars: {delay: expr(`max(0, ${elapsed} - ${phase}) if ${held} else 0`)}});
    add('fz_tap_2', 'delayCHOP', {inputs: ['in_audio'], pars: {delay: expr(`max(0, ${elapsed} - ${phase2}) if ${held} else 0`)}});
    add('fz_grain_1', 'mathCHOP', {inputs: ['fz_tap_1'], pars: {gain: expr(`(${w1} * ${gain}) if ${held} else 0`)}});
    add('fz_grain_2', 'mathCHOP', {inputs: ['fz_tap_2'], pars: {gain: expr(`((1 - ${w1}) * ${gain}) if ${held} else 0`)}});
    add('fz_through', 'mathCHOP', {inputs: ['in_audio'], pars: {gain: expr(`0 if ${held} else 1`)}});
    add('fz_sum', 'mathCHOP', {inputs: ['fz_through', 'fz_grain_1', 'fz_grain_2'], pars: {chopop: 'add'}});
    return 'fz_sum';
  };
  return {picture, audio};
}

/**
 * Reverse (playback mode): the interval is read backward from a cache of the input, at a depth the resolver computes
 * (`depth`: seconds behind the input's live edge, from the workbench's own reverseSourceTime at the processor's output
 * time; see td_resolve). The input runs `lookahead` seconds ahead of the output, so the interval end, which is up to
 * `span` ahead, is already in the cache. Route mode reads the picture at its own time and slides it by `shift`
 * (a fraction of the width). The voice is only delayed by the same latency (kept in step with the picture): reversing
 * it needs a per-sample read of a history buffer that this version does not build.
 */
function reverse(add, ctx) {
  const history = HISTORY.reverse(ctx.lookahead);
  const picture = () => {
    add('rv_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: expr(cacheSizeText(history)), active: 1}});
    add('rv_frame', 'cacheselectTOP', {pars: {cachetop: {ref: 'rv_cache'}, index: expr(cacheIndexText('{depth}', history))}});
    add('rv_move', 'transformTOP', {inputs: ['rv_frame'], pars: {tx: expr('{shift}'), extend: 'zero'}});
    return 'rv_move';
  };
  const audio = () => {
    add('rv_audio', 'delayCHOP', {inputs: ['in_audio'], pars: {delay: ctx.lookahead}});
    return 'rv_audio';
  };
  return {picture, audio, audioAlignedOnly: true};
}

/**
 * Stutter: three picture sources, chosen by the resolver's `pick` (0 the running picture, 1 a repeat, 2 the gap
 * between repeats, which the workbench draws as nothing): the input delayed by the latency; a cache that stops taking
 * frames (`record` 0) once the captured window has arrived, read at the resolver's `depth`; and a transparent frame.
 * The voice is delayed only (as in Reverse).
 */
function stutter(add, ctx) {
  const L = ctx.lookahead, held = HISTORY.stutter();
  const picture = () => {
    add('st_live_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: expr(cacheSizeText(L)), active: 1}});
    add('st_live', 'cacheselectTOP', {pars: {cachetop: {ref: 'st_live_cache'}, index: expr(cacheIndexText(L, L))}});
    add('st_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: expr(cacheSizeText(held)), active: expr('int({record})')}});
    add('st_frame', 'cacheselectTOP', {pars: {cachetop: {ref: 'st_cache'}, index: expr(cacheIndexText('{depth}', held))}});
    add('st_blank', 'constantTOP', {pars: {colorr: 0, colorg: 0, colorb: 0, alpha: 0}});
    add('st_pick', 'switchTOP', {inputs: ['st_live', 'st_frame', 'st_blank'], pars: {index: expr('int({pick})')}});
    return 'st_pick';
  };
  const audio = () => {
    add('st_audio', 'delayCHOP', {inputs: ['in_audio'], pars: {delay: ctx.lookahead}});
    return 'st_audio';
  };
  return {picture, audio, audioAlignedOnly: true};
}

/** The most trace copies Routing draws (the catalog's maximum), and how far apart in source time they read (visual-engine.mjs: t - k x 0.085). */
const MAX_COPIES = 8;
const COPY_LAG = 0.085;

/**
 * Routing (picture): a travelling address. The picture moves between Origin and Destination along a triangle wave of period
 * 2 x Travel (out and back), and `Trace copies` earlier positions of it trail behind: copy k sits at the path position
 * `q = clamp(progress - k x 0.12 x Trace spread)`, scaled to 0.62 of the frame, lifted by 0.3 sin(q pi) of the frame height,
 * read k x 0.085 s earlier from a cache, at opacity `(1 - k / (copies + 1)) x 0.95`, and laid over the copies behind it (copy 0 on
 * top), as the workbench draws them. The voice's pan travel is a per-sample gain curve and is not built (the voice passes).
 * Impact scatter (the text-port burst) belongs to the text stream and is not built.
 */
function routing(add, ctx = {}) {
  const T = ctx.clock ?? '{rack.Time}';
  const history = (MAX_COPIES - 1) * COPY_LAG;   // the oldest copy; the cache is sized from the project's cook rate, the rate its read indices use
  const progress = `(lambda phase: phase if phase < 1 else 2 - phase)((${T} / max(0.08, {travel})) % 2)`;
  const q = k => `min(1, max(0, ${progress} - ${k} * 0.12 * {spread}))`;
  const picture = () => {
    add('rt_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: expr(cacheSizeText(history)), active: 1}});
    add('rt_base', 'constantTOP', {pars: {colorr: 0, colorg: 0, colorb: 0, alpha: 0}});
    let below = 'rt_base';
    for (let k = MAX_COPIES - 1; k >= 0; k--) {
      add(`rt_frame_${k}`, 'cacheselectTOP', {pars: {cachetop: {ref: 'rt_cache'}, index: expr(cacheIndexText(k * COPY_LAG, history))}});
      add(`rt_move_${k}`, 'transformTOP', {inputs: [`rt_frame_${k}`], pars: {
        sx: 0.62, sy: 0.62, extend: 'zero',
        tx: expr(`{origin} + ({destination} - {origin}) * ${q(k)} - 0.5`),
        ty: expr(`0.3 * math.sin(${q(k)} * math.pi)`),
      }});
      add(`rt_gain_${k}`, 'levelTOP', {inputs: [`rt_move_${k}`], pars: {opacity: expr(`((1 - ${k} / (int(round({copies})) + 1)) * 0.95) if ${k} < int(round({copies})) else 0`)}});
      add(`rt_over_${k}`, 'overTOP', {inputs: [`rt_gain_${k}`, below]});
      below = `rt_over_${k}`;
    }
    return below;
  };
  return {picture};
}

/** The strips Exchange cuts the picture into when the media pack names no labelled people (the workbench's own fallback, visual-engine.mjs `people.length||5`). */
export const EXCHANGE_REGIONS = 5;

/**
 * Exchange (picture): the frame is cut into strips and the strips are dealt out again by a cyclic permutation. Every
 * `Exchange interval` seconds the address advances by `Address offset`: with `n = floor(time / interval)` the strip
 * `k` lands in slot `(k + (n x offset + 1) mod count) mod count`, so slot `d` shows strip `(d - shift) mod count`. The strips are
 * read from `time - History offset` (a cache read back; before enough history exists the cache's oldest frame is shown, as the
 * workbench clamps to the start of the media). Mode 1 (voice addresses) leaves the picture alone. A Crop TOP cuts each strip out,
 * a Transform TOP on a full-size canvas (which stretches the strip across the canvas, so the scale returns it to a fifth of the width) puts it in its slot and an Over TOP lays the slots side by side. `Exchange transition`
 * only shapes the voice in the workbench, so the picture changes at a hard cut. The voice is not built.
 */
function exchange(add, ctx = {}) {
  const T = ctx.clock ?? '{rack.Time}';
  const history = 3;   // the catalog's largest History offset
  const shift = `((int(math.floor(${T} / max(0.001, {interval}))) * int(round({offset})) + 1) % ${EXCHANGE_REGIONS})`;
  const strip = d => `((${d} - ${shift}) % ${EXCHANGE_REGIONS})`;
  const picture = () => {
    add('xc_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: expr(cacheSizeText(history)), active: 1}});
    add('xc_frame', 'cacheselectTOP', {pars: {cachetop: {ref: 'xc_cache'}, index: expr(cacheIndexText('{timeOffset}', history))}});
    let below = null;
    for (let d = EXCHANGE_REGIONS - 1; d >= 0; d--) {
      add(`xc_crop_${d}`, 'cropTOP', {inputs: ['xc_frame'], pars: {
        cropleft: expr(`${strip(d)} / ${EXCHANGE_REGIONS}`), cropright: expr(`(${strip(d)} + 1) / ${EXCHANGE_REGIONS}`), cropbottom: 0, croptop: 1,
      }});
      add(`xc_slot_${d}`, 'transformTOP', {inputs: [`xc_crop_${d}`], pars: {
        outputresolution: 'custom', resolutionw: expr('{op:in_picture}.width'), resolutionh: expr('{op:in_picture}.height'),
        // a custom output resolution resamples the strip across the whole canvas (it does not pad it), so the scale brings it back to one strip's width
        sx: 1 / EXCHANGE_REGIONS, sy: 1,
        tx: (d + 0.5) / EXCHANGE_REGIONS - 0.5, extend: 'zero',
      }});
      if (below === null) below = `xc_slot_${d}`;
      else { add(`xc_over_${d}`, 'overTOP', {inputs: [`xc_slot_${d}`, below]}); below = `xc_over_${d}`; }
    }
    // mode 1 (voice addresses) leaves the picture as it is
    add('xc_pick', 'switchTOP', {inputs: ['in_picture', below], pars: {index: expr('0 if int(round({mode})) == 1 else 1')}});
    return 'xc_pick';
  };
  return {picture, notes: [`exchange cuts the picture into ${EXCHANGE_REGIONS} strips (the workbench's fallback when the media pack names no people; the network cannot read the pack's regions); the Crop TOP's units and output-resolution behaviour are the reference defaults (the parameter reference lists no menu tokens), and the voice addresses are not built.`]};
}

/** The workbench's `noise(seed, i, j)` (visual-engine.mjs) as a Python expression over two expression texts: a 32-bit integer hash scaled to [0, 1). */
export const noiseText = (seed, i, j = '0') => {
  const start = `((${(seed | 0) >>> 0} ^ (((${i}) + 1) * 374761393 & 0xFFFFFFFF) ^ (((${j}) + 1) * 668265263 & 0xFFFFFFFF)) & 0xFFFFFFFF)`;
  return `(lambda a: (lambda b: ((b ^ (b >> 16)) & 0xFFFFFFFF) / 4294967296)(((a ^ (a >> 13)) * 1274126177) & 0xFFFFFFFF))(${start})`;
};

/** The most entries Queue / Overflow keeps (the catalog's largest Queue capacity) and the longest burst interval (how far back an entry can sit). */
const MAX_QUEUE = 24;
const MAX_BURST = 6;

/**
 * Queue / Overflow (picture): arrivals enter at `Arrival rate` per second inside each `Burst interval`, leave at a fifth of
 * `Release rate`, and what is waiting (at most `Queue capacity` entries, first in first out or last in first out) is drawn as
 * a stack of small copies of the source from the instants each entry arrived, from the top left, falling down and across,
 * scattered by `Overflow spread` over the burst once more is waiting than the capacity (all from the workbench's own formulas). Entry
 * `j` is a cache read back `age` seconds, scaled to 0.48, rotated about its top-left corner and placed there; with nothing waiting the
 * frame itself is drawn. The scatter uses the workbench's noise hash with the processor's seed. The text port's smaller 0.65 copies are not built.
 */
function queue(add, ctx = {}) {
  const T = ctx.clock ?? '{rack.Time}';
  const seed = ctx.seed ?? 1;
  const period = 'max(0.4, {burst})';
  const phase = `((${T}) % ${period})`;
  const arrivals = `int(math.floor(${phase} * {rate}))`;
  const released = `int(math.floor(${phase} * {release} * 0.2))`;
  const backlog = `max(0, ${arrivals} - ${released})`;
  const count = `min(int(math.floor({capacity} + 0.5)), ${backlog})`;
  const overflow = `(${backlog} > {capacity})`;
  const burst = `((${phase} / ${period}) * {spread} if ${overflow} else 0)`;
  const slot = j => `(${j} if int(round({order})) == 0 else ${count} - 1 - ${j})`;
  const age = j => `max(0, ${phase} - (${released} + ${slot(j)}) / max(0.25, {rate}))`;
  const W = '{op:in_picture}.width', H = '{op:in_picture}.height';
  const x = j => `(${W} * 0.08 + ${j} * ${W} * 0.025 + ${burst} * (${noiseText(seed, j, `int(math.floor(${T} / ${period}))`)} - 0.5) * ${W})`;
  const y = j => `(${H} * 0.15 + ${j} * ${H} * 0.055 + ${burst} * (${noiseText(seed, j, '5')} - 0.5) * ${H})`;
  const angle = j => `((${noiseText(seed, j)} - 0.5) * 0.8 if ${overflow} else 0)`;
  // the copy's centre: its top-left corner (x, y) plus the half-size diagonal turned by the angle (canvas y runs down)
  const centreX = j => `(${x(j)} + 0.48 * ${W} / 2 * math.cos(${angle(j)}) - 0.48 * ${H} / 2 * math.sin(${angle(j)}))`;
  const centreY = j => `(${y(j)} + 0.48 * ${W} / 2 * math.sin(${angle(j)}) + 0.48 * ${H} / 2 * math.cos(${angle(j)}))`;
  const picture = () => {
    add('qu_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: expr(cacheSizeText(MAX_BURST)), active: 1}});
    // nothing waiting: the frame itself, as the workbench draws it; otherwise transparent
    add('qu_idle', 'levelTOP', {inputs: ['in_picture'], pars: {opacity: expr(`1 if ${count} == 0 else 0`)}});
    let below = 'qu_idle';
    for (let j = 0; j < MAX_QUEUE; j++) {
      add(`qu_frame_${j}`, 'cacheselectTOP', {pars: {cachetop: {ref: 'qu_cache'}, index: expr(cacheIndexText(age(j), MAX_BURST))}});
      add(`qu_move_${j}`, 'transformTOP', {inputs: [`qu_frame_${j}`], pars: {
        sx: 0.48, sy: 0.48, extend: 'zero',
        tx: expr(`(${centreX(j)} - ${W} / 2) / ${W}`),
        ty: expr(`-(${centreY(j)} - ${H} / 2) / ${H}`),
        rotate: expr(`-${angle(j)} * 180 / math.pi`),
      }});
      add(`qu_gain_${j}`, 'levelTOP', {inputs: [`qu_move_${j}`], pars: {opacity: expr(`1 if ${j} < ${count} else 0`)}});
      add(`qu_over_${j}`, 'overTOP', {inputs: [`qu_gain_${j}`, below]});
      below = `qu_over_${j}`;
    }
    return below;
  };
  return {picture, notes: ['queue: the text port\'s smaller 0.65 copies (the written source) are not built, only the picture; the Transform TOP\'s Rotate is assumed counter-clockwise about the image centre (the parameter reference lists no menu tokens or pivot default), as Feedback already assumes the centre, and its translate unit is taken as the reference default.']};
}

/** The processors implemented by this version. A builder returns `shared` (optional, called first), `picture` and `audio`. */
export const EFFECTS = Object.freeze({damage, delay, feedback, freeze, gate, displacement, reverse, stutter, routing, exchange, queue});
