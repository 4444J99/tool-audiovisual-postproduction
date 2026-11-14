/**
 * The TouchDesigner operator types and parameter names the generator may use. This table is the whole
 * surface the generated network depends on: the spec carries a copy, the validator rejects anything
 * outside it, and the builder refuses it before creating a single operator.
 *
 * The names are checked against TouchDesigner's own parameter reference (`Config/TDParameterHelp.json` in the
 * application bundle) by `check-vocabulary.mjs`. The network has not been built in TouchDesigner yet
 * (see docs/FX-TOUCHDESIGNER.md); when a name is wrong in a given build, it is corrected here, once.
 */
export const VOCABULARY = Object.freeze({
  baseCOMP: {family: 'COMP', pars: []},
  selectTOP: {family: 'TOP', pars: ['top']},
  selectCHOP: {family: 'CHOP', pars: ['chop']},
  selectDAT: {family: 'DAT', pars: ['dat']},
  nullTOP: {family: 'TOP', pars: []},
  nullCHOP: {family: 'CHOP', pars: []},
  nullDAT: {family: 'DAT', pars: []},
  levelTOP: {family: 'TOP', pars: ['opacity', 'stepping', 'stepsize']},
  addTOP: {family: 'TOP', pars: []},
  crossTOP: {family: 'TOP', pars: ['cross']},
  switchTOP: {family: 'TOP', pars: ['index']},
  fitTOP: {family: 'TOP', pars: ['outputresolution', 'resolutionw', 'resolutionh', 'inputfiltertype']},
  transformTOP: {family: 'TOP', pars: ['sx', 'sy', 'rotate']},
  noiseTOP: {family: 'TOP', pars: ['period', 'tz', 'mono', 'seed', 'amp']},
  displaceTOP: {family: 'TOP', pars: ['displaceweightx', 'displaceweighty']},
  feedbackTOP: {family: 'TOP', pars: ['top']},
  cacheTOP: {family: 'TOP', pars: ['cachesize', 'active']},
  cacheselectTOP: {family: 'TOP', pars: ['cachetop', 'index']},
  constantTOP: {family: 'TOP', pars: ['colorr', 'colorg', 'colorb', 'alpha']},
  moviefileinTOP: {family: 'TOP', pars: ['file']},
  expressionCHOP: {family: 'CHOP', pars: ['expr0expr']},
  lagCHOP: {family: 'CHOP', pars: ['lag']},
  delayCHOP: {family: 'CHOP', pars: ['delay']},
  mathCHOP: {family: 'CHOP', pars: ['gain', 'chopop']},
  switchCHOP: {family: 'CHOP', pars: ['index']},
  constantCHOP: {family: 'CHOP', pars: ['const0name', 'const0value']},
  audiofileinCHOP: {family: 'CHOP', pars: ['file']},
  textDAT: {family: 'DAT', pars: []},
  tableDAT: {family: 'DAT', pars: []},
  executeDAT: {family: 'DAT', pars: ['active', 'framestart']},
  oscinDAT: {family: 'DAT', pars: ['port', 'callbacks']},
  oscoutDAT: {family: 'DAT', pars: ['address', 'port']},
  parameterexecuteDAT: {family: 'DAT', pars: ['op', 'pars', 'custom', 'builtin', 'valuechange', 'onpulse']},
});

/** The signal port families: picture and matte are image streams, audio and control are channels, text is a table. */
export const PORT_FAMILY = Object.freeze({picture: 'TOP', matte: 'TOP', audio: 'CHOP', control: 'CHOP', text: 'DAT'});
export const SELECT = Object.freeze({TOP: ['selectTOP', 'top'], CHOP: ['selectCHOP', 'chop'], DAT: ['selectDAT', 'dat']});
export const NULL = Object.freeze({TOP: 'nullTOP', CHOP: 'nullCHOP', DAT: 'nullDAT'});

export const CUSTOM_PAR_NAME = /^[A-Z][a-z0-9]*$/;
export const OPERATOR_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
