// The Node silent launch helper: the same contract as tools/silent_browser.py, proven with a fake engine, so it
// needs no browser and makes no sound. The page behaviour of the shared init script is proven by
// tests/test_silent_browser.py against a real browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import {silentLaunch, silentLaunchPersistentContext, silentArgs, INIT_SCRIPT_PATH, initScript} from '../tools/silent-browser.mjs';

function fakeEngine(name) {
  const scripts = [];
  const contexts = [];
  const context = () => { const c = {addInitScript: async options => { scripts.push(options.path); }, closed: false, close: async () => { c.closed = true; }, newPage: async () => ({close: async () => {}})}; contexts.push(c); return c; };
  return {scripts, contexts, launched: null, name: () => name,
    async launch(options) { this.launched = options; return {newContext: async () => context(), newPage: async () => { throw new Error('original newPage must not run'); }}; },
    async launchPersistentContext(dir, options) { this.launched = {dir, ...options}; return context(); }};
}

test('chromium gets --mute-audio merged with the caller arguments, once', async () => {
  const engine = fakeEngine('chromium');
  await silentLaunch(engine, {headless: true, args: ['--no-sandbox', '--no-zygote']});
  assert.deepEqual(engine.launched, {headless: true, args: ['--no-sandbox', '--no-zygote', '--mute-audio']});
  assert.deepEqual(silentArgs('chromium', ['--mute-audio']), ['--mute-audio']);
  assert.deepEqual(silentArgs('chromium'), ['--mute-audio']);
});

test('webkit and firefox have no flag and rely on the init script', async () => {
  assert.deepEqual(silentArgs('webkit', ['--x']), ['--x']);
  assert.deepEqual(silentArgs('firefox'), []);
});

test('every context, page and persistent context gets the init script', async () => {
  const engine = fakeEngine('webkit');
  const browser = await silentLaunch(engine, {});
  await browser.newContext();
  const page = await browser.newPage();
  await silentLaunchPersistentContext(engine, '/profile', {});
  assert.deepEqual(engine.scripts, [INIT_SCRIPT_PATH, INIT_SCRIPT_PATH, INIT_SCRIPT_PATH]);
  await page.close();
  assert.equal(engine.contexts[1].closed, true, 'the page owns its silenced context and closes it');
  assert.ok(initScript().includes('__SILENT_BROWSER__'));
});

