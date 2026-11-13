import {existsSync} from 'node:fs';
import {dirname, join, resolve, sep} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

// Relocatable after copying anywhere inside the repository, or with an explicit module root.
const candidates = [];
if (process.env.FX_MODULE_ROOT) candidates.push(resolve(process.env.FX_MODULE_ROOT));
let ancestor = dirname(fileURLToPath(import.meta.url));
while (true) {
  candidates.push(join(ancestor, 'web', 'fx'));
  const parent = dirname(ancestor); if (parent === ancestor) break; ancestor = parent;
}
const moduleRoot = candidates.find(candidate => existsSync(join(candidate, 'core.mjs')) && existsSync(join(candidate, 'visual-engine.mjs')));
if (!moduleRoot) throw new Error('Set FX_MODULE_ROOT to the directory containing core.mjs, visual-engine.mjs and workbench-state.mjs.');
const rootURL = pathToFileURL(moduleRoot + sep);
export const core = await import(new URL('core.mjs', rootURL));
export const workbench = await import(new URL('workbench-state.mjs', rootURL));
export const visual = await import(new URL('visual-engine.mjs', rootURL));
export const preview = await import(new URL('preview-key.mjs', rootURL));
export const controller = await import(new URL('controller-map.mjs', rootURL));
export const {
  CATALOG, CATALOG_BY_TYPE, PATCH_CATALOG, createModule, createPatch, createSingleModulePatch,
  validatePatch, evaluateAutomation, evaluatedNode, topologicalOrder, buildSerialConnections, clonePatch,
} = core;
export const SOURCE = Object.freeze({id: 'test-excerpt', sha256: 'a'.repeat(64), offset: 95, duration: 12.166666667});
export const CONTEXT = Object.freeze({source: SOURCE, duration: SOURCE.duration});
export const freshPatch = (type = 'delay') => core.createSingleModulePatch(type, {source: SOURCE, demonstration: false});
