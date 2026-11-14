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
const indices = count => Array.from({length: count}, (_, i) => i);

/** Signal Damage: tone-level posterize and nearest-neighbour mosaic; drive, bit-depth quantize, bandwidth limit and output level. */
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
    add('dmg_bits', 'expressionCHOP', {inputs: ['dmg_drive'], pars: {expr0expr: expr('max(-1, min(1, round(me.inputVal * (2 ** ({bits} - 1))) / (2 ** ({bits} - 1))))')}});
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
    add('dly_sum', 'mathCHOP', {inputs: ['in_audio', ...names], pars: {chopop: 'add'}});
    return 'dly_sum';
  };
  const picture = () => {
    add('dly_cache', 'cacheTOP', {inputs: ['in_picture'], pars: {cachesize: 240, active: 1}});
    const names = indices(MAX_TAPS).map(i => {
      add(`dly_frame_${i}`, 'cacheselectTOP', {pars: {cachetop: {ref: 'dly_cache'}, index: expr(`-min(239, int(round({time} * ${i + 1} * project.cookRate)))`)}});
      add(`dly_copy_${i}`, 'levelTOP', {inputs: [`dly_frame_${i}`], pars: {opacity: expr(tapGain(i))}});
      return `dly_copy_${i}`;
    });
    add('dly_mix', 'addTOP', {inputs: ['in_picture', ...names]});
    return 'dly_mix';
  };
  return {picture, audio};
}

/** Gate / Erase: a periodic (or sidechain-threshold) opening, edge-smoothed, closed state held at Closed-state residue. */
function gate(add) {
  const open = '(1 if (({rack.Time} / max(0.1, {period}) + {phase}) % 1) < {duty} else 0) if {detector} < 0.5 else (1 if float({op:in_control}[0].eval()) > {threshold} else 0)';
  const shared = () => {
    add('gate_raw', 'constantCHOP', {pars: {const0name: 'open', const0value: expr(`{retain} + (1 - {retain}) * (${open})`)}});
    add('gate_ctrl', 'lagCHOP', {inputs: ['gate_raw'], pars: {lag1: expr('max(0.002, {edge})'), lag2: expr('max(0.002, {edge})')}});
  };
  const audio = () => { add('gate_audio', 'mathCHOP', {inputs: ['in_audio'], pars: {gain: expr('float({op:gate_ctrl}[0].eval())')}}); return 'gate_audio'; };
  const picture = () => { add('gate_picture', 'levelTOP', {inputs: ['in_picture'], pars: {opacity: expr('float({op:gate_ctrl}[0].eval())')}}); return 'gate_picture'; };
  return {shared, picture, audio};
}

/** Feedback: the image returns through a transform (Scale per recurrence, Rotation per recurrence) and Return gain. */
function feedback(add) {
  const picture = () => {
    add('fb_state', 'feedbackTOP', {inputs: ['in_picture'], pars: {top: {ref: 'fb_sum'}}});
    add('fb_move', 'transformTOP', {inputs: ['fb_state'], pars: {sx: expr('{scale}'), sy: expr('{scale}'), rotate: expr('{rotation} * 360')}});
    add('fb_gain', 'levelTOP', {inputs: ['fb_move'], pars: {opacity: expr('{gain}')}});
    add('fb_sum', 'addTOP', {inputs: ['in_picture', 'fb_gain']});
    return 'fb_sum';
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

/** The processors implemented by this version. A builder returns `shared` (optional, called first), `picture` and `audio`. */
export const EFFECTS = Object.freeze({damage, delay, gate, feedback, displacement});
