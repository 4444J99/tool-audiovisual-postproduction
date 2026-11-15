/**
 * Rewrite a patch so that every macro curve, step and linear automation, multiply and add modulation, a clip scope and a
 * Solo event are exercised even where the shipped fixtures use only some of them. Used by the parameter-resolver oracle
 * (oracle-evaluated.mjs) and the rule-sync oracle (oracle-rulesync.mjs).
 */
import {CATALOG_BY_TYPE, MACRO_CURVES} from '../../web/fx/core.mjs';

export function stressPatch(patch) {
  patch.nodes.filter(node => CATALOG_BY_TYPE[node.type]).forEach((node, index) => {
    const definition = CATALOG_BY_TYPE[node.type], first = definition.params[0], second = definition.params[1] ?? first;
    const at = (item, ratio) => { const value = item.min + (item.max - item.min) * ratio; return item.step === 1 ? Math.round(value) : value; };
    node.macro = definition.params.slice(0, 4).map((item, slot) => ({parameter: item.key, min: at(item, 0.2), max: at(item, 0.9), curve: MACRO_CURVES[(index + slot) % MACRO_CURVES.length]}));
    node.automation = [
      {parameter: 'intensity', interpolation: index % 2 ? 'step' : 'linear', keyframes: [{time: 0, value: 0.1}, {time: patch.duration * 0.4, value: 0.9}, {time: patch.duration * 0.8, value: 0.3}]},
      {parameter: `params.${second.key}`, interpolation: index % 2 ? 'linear' : 'step', keyframes: [{time: 0, value: second.min}, {time: patch.duration * 0.5, value: second.max}]},
    ];
    node.modulation = {parameter: index % 2 ? 'intensity' : `params.${first.key}`, mode: index % 3 ? 'multiply' : 'add', amount: index % 2 ? 0.4 : (first.max - first.min) * 0.25};
    if (index === 1) node.scope = {type: 'clip', start: patch.duration * 0.25, end: patch.duration * 0.75};
    patch.events = [...(patch.events ?? []), ...(index === 2 ? [{time: patch.duration * 0.3, nodeId: node.id, parameter: 'solo', value: true}, {time: patch.duration * 0.6, nodeId: node.id, parameter: 'solo', value: false}, {time: patch.duration * 0.5, nodeId: node.id, parameter: 'wet', value: 0.25}] : [])];
  });
  return patch;
}
