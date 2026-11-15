/* Launch every Playwright browser through here so no run can make a sound. Chromium gets --mute-audio merged into
 * the caller's arguments, and every context (newContext, and newPage, which makes its own) gets silent-init.js:
 * media elements pinned muted at volume 0 and a gain-0 sink in front of every realtime AudioContext destination.
 * The init script is engine-agnostic, so it is the mute for WebKit and Firefox, which have no flag. */
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const INIT_SCRIPT_PATH = path.join(path.dirname(fileURLToPath(import.meta.url)), 'silent-init.js');
const MUTE_ARGS = ['--mute-audio'];
const initScript = () => fs.readFileSync(INIT_SCRIPT_PATH, 'utf8');

const engineName = engine => (typeof engine.name === 'function' ? engine.name() : engine.name);

function silentArgs(name, args = []) {
  const merged = [...args];
  if (name === 'chromium') for (const flag of MUTE_ARGS) if (!merged.includes(flag)) merged.push(flag);
  return merged;
}

async function silenceContext(context) {
  await context.addInitScript({path: INIT_SCRIPT_PATH});
  return context;
}

function silenceBrowser(browser) {
  if (browser.__silentBrowser) return browser;
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async (...args) => silenceContext(await newContext(...args));
  // Playwright's own newPage makes a context after the fact, so a script added to it would miss the blank document
  // that setContent and addScriptTag write into. Make the silenced context first, then the page, and close the
  // context with the page the way Playwright's owned context does.
  browser.newPage = async (...args) => {
    const context = await browser.newContext(...args), page = await context.newPage(), pageClose = page.close.bind(page);
    page.close = async (...closeArgs) => { try { await pageClose(...closeArgs); } finally { await context.close(); } };
    return page;
  };
  browser.__silentBrowser = true;
  return browser;
}

async function silentLaunch(engine, options = {}) {
  return silenceBrowser(await engine.launch({...options, args: silentArgs(engineName(engine), options.args)}));
}

async function silentLaunchPersistentContext(engine, userDataDir, options = {}) {
  return silenceContext(await engine.launchPersistentContext(userDataDir, {...options, args: silentArgs(engineName(engine), options.args)}));
}

export {INIT_SCRIPT_PATH, MUTE_ARGS, initScript, silentArgs, silenceContext, silenceBrowser, silentLaunch, silentLaunchPersistentContext};
