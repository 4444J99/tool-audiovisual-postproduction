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
const COMMON = new Set(['intensity', 'wet', 'bypass', 'solo', 'active']);
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
    add('dmg_hold', 'expressionCHOP', {inputs: ['dmg_drive'], pars: {expr0expr: expr(`{op:dmg_drive}[0][max(0, int(math.ceil(int((me.inputIndex + 1) / (${step})) * (${step}) - 1)))]`)}});
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
function gate(add) {
  const open = '(1 if (({rack.Time} / max(0.1, {period}) + {phase}) % 1) < {duty} else 0) if {detector} < 0.5 else (1 if (float({op:in_control}[0].eval()) if {op:in_control}.numChans else 0) > {threshold} else 0)';
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
 * below Tail threshold, and epoch is Reset time once the playhead has reached it (0 before). Each recurrence reads the
 * history, not its own output, so it is a finite set of Cache Select taps, not a Feedback TOP loop. A tap that would reach
 * further back than the cache holds is switched off (as in Delay / Echo), never collapsed onto the oldest frame.
 */
function feedback(add) {
  const frames = k => `int(round({time} * ${k} * project.cookRate))`;
  const epoch = '({reset} if {rack.Time} >= {reset} else 0)';
  const passes = `min(int(round({repeats})), int(({rack.Time} - ${epoch}) / max(0.001, {time})))`;
  const picture = () => {
    add('fb_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: CACHE_FRAMES, active: 1}});
    let below = 'in_picture';
    for (let k = 1; k <= MAX_RECURRENCES; k++) {
      add(`fb_frame_${k}`, 'cacheselectTOP', {pars: {cachetop: {ref: 'fb_cache'}, index: expr(`-min(${CACHE_FRAMES - 1}, ${frames(k)})`)}});
      add(`fb_move_${k}`, 'transformTOP', {inputs: [`fb_frame_${k}`], pars: {sx: expr(`{scale} ** ${k}`), sy: expr(`{scale} ** ${k}`), rotate: expr(`{rotation} * 360 * ${k}`)}});
      // source-over of an opaque frame at alpha g is the cross-fade below -> frame by g; a return that is past the pass count,
      // under the tail threshold or beyond the cache contributes nothing
      add(`fb_mix_${k}`, 'crossTOP', {inputs: [below, `fb_move_${k}`], pars: {cross: expr(`({gain} ** ${k}) if (${k} <= ${passes} and ({gain} ** ${k}) >= {threshold} and ${frames(k)} < ${CACHE_FRAMES}) else 0`)}});
      below = `fb_mix_${k}`;
    }
    return below;
  };
  return {picture};
}

/** Displacement: a moving noise field (Field scale, Field movement) pulls the picture by Visual displacement along Direction. */
function displacement(add) {
  const picture = () => {
    add('dsp_field', 'noiseTOP', {pars: {period: expr('1 / max(0.05, {scale})'), tz: expr('{rack.Time} * {speed}'), mono: 1, seed: 1, amp: 1}});
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
function freeze(add) {
  const held = '({rack.Time} >= {capture} and {rack.Time} < {capture} + {duration})';
  const elapsed = '({rack.Time} - {capture})';
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
    add('fz_shake', 'transformTOP', {inputs: ['fz_pick'], pars: {tx: expr(`(math.sin({rack.Time} * 7) * {motion}) if ${held} else 0`)}});
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

/** The processors implemented by this version. A builder returns `shared` (optional, called first), `picture` and `audio`. */
export const EFFECTS = Object.freeze({damage, delay, feedback, freeze, gate, displacement});
