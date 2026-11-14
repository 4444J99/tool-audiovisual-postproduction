#!/usr/bin/env node
/**
 * Check the operator and parameter names the generated network uses against TouchDesigner's own parameter
 * reference, which ships inside the application:
 *
 *   node tools/td-network/check-vocabulary.mjs [/Applications/TouchDesigner.app/Contents/Resources/tfs/Config/TDParameterHelp.json]
 *
 * Read-only. Exit 1 lists every operator type or parameter the reference does not know. Colour parameters are
 * listed once in the reference by their tuplet name (`color`), so `colorr`, `colorg` and `colorb` are checked
 * against `color` plus the channel suffix.
 */
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {VOCABULARY} from './vocabulary.mjs';

const TUPLET_SUFFIXES = ['r', 'g', 'b', 'a', 'x', 'y', 'z', 'w'];

export function checkVocabulary(help, vocabulary = VOCABULARY) {
  const types = Object.assign({}, ...Object.values(help.help));
  const problems = [];
  for (const [type, item] of Object.entries(vocabulary)) {
    const reference = types[type];
    if (!reference) { problems.push(`${type}: not an operator type in the reference`); continue; }
    for (const name of item.pars) {
      const known = name in reference.parameters
        || TUPLET_SUFFIXES.some(suffix => name.endsWith(suffix) && name.slice(0, -1) in reference.parameters && ['RGB', 'RGBA', 'XY', 'XYZ', 'XYZW', 'UV', 'UVW', 'WH'].includes(reference.parameters[name.slice(0, -1)].parType));
      if (!known) problems.push(`${type}.${name}: not a parameter in the reference`);
    }
  }
  return problems;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const path = process.argv[2] ?? '/Applications/TouchDesigner.app/Contents/Resources/tfs/Config/TDParameterHelp.json';
  let help;
  try { help = JSON.parse(readFileSync(path, 'utf8')); } catch (error) { console.error(`cannot read ${path}: ${error.message}`); process.exit(2); }
  const problems = checkVocabulary(help);
  console.log(JSON.stringify({reference: path, product: help.product, operators: Object.keys(VOCABULARY).length, problems}, null, 2));
  process.exit(problems.length ? 1 : 0);
}
