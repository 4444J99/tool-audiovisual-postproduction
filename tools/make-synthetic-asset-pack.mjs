/**
 * Write a synthetic stand-in for the private FX media pack so the real workbench page can run
 * (rack, Control tab, TouchDesigner bridge) without the film. Nothing here is derived from the film:
 * moving bars, five labelled rectangles, a tone-burst "dialogue" and the three authored word cues.
 *
 *   node tools/make-synthetic-asset-pack.mjs <out-dir>        # then serve so <out-dir> appears as web/fx/assets
 *
 * The pack carries the source identity the factory patches bind to, because the workbench refuses
 * patches for any other source. That makes it a local test fixture only: the manifest and
 * provenance say `synthetic: true`, and it must never be published or committed.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {encodeWav} from '../web/fx/audio-engine.mjs';
import {GOD_HERE_SOURCE, GOD_HERE_REGIONS} from '../web/fx/god-here.config.mjs';

const require = createRequire(import.meta.url);
const {createCanvas} = require('@napi-rs/canvas');

export const PACK = Object.freeze({fps: 12, columns: 6, framesPerPage: 24, frame: [96, 54], sampleRate: 24000});

export function buildManifest() {
  const duration = GOD_HERE_SOURCE.duration, frameCount = Math.round(duration * PACK.fps), pages = Math.ceil(frameCount / PACK.framesPerPage);
  const names = prefix => Array.from({length: pages}, (_, i) => `${prefix}-${String(i).padStart(2, '0')}.png`);
  return {synthetic: true, originalFilmSha256: GOD_HERE_SOURCE.sha256, sourceIn: GOD_HERE_SOURCE.offset, duration, fps: PACK.fps, frameCount,
    columns: PACK.columns, framesPerPage: PACK.framesPerPage,
    picture: {pages: names('picture'), width: PACK.frame[0], height: PACK.frame[1]}, matte: {pages: names('matte'), width: PACK.frame[0], height: PACK.frame[1]},
    room: {hybrid: 'room.png'}, cues: 'cues.json', regions: 'regions.json', provenance: 'provenance.json', waveforms: 'waveforms.json',
    audio: {sampleRate: PACK.sampleRate, source: 'source.wav', reference: 'reference.wav'}};
}

export function buildRegions() {
  return {items: GOD_HERE_REGIONS.map((id, i) => ({id, labelValue: 40 + i * 40, bounds: [.08 + i * .18, .3, .14, .5]}))};
}

function lcg(seed) { let s = seed >>> 0; return () => (s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296; }

export function synthesizeAudio(duration, sampleRate, words) {
  const frames = Math.round(duration * sampleRate), rand = lcg(7), left = new Float32Array(frames), right = new Float32Array(frames);
  for (let i = 0; i < frames; i++) {
    const t = i / sampleRate; let burst = 0;
    for (const w of words) if (t >= w.in && t <= w.out) burst = Math.sin(Math.PI * (t - w.in) / (w.out - w.in)) * .5 * Math.sin(2 * Math.PI * (180 + 40 * words.indexOf(w)) * t);
    const noise = (rand() - .5) * .02;
    left[i] = burst + noise; right[i] = burst * .9 + noise;
  }
  return [left, right];
}

export function envelopeSummary(channels, points = 240) {
  const n = channels[0].length, size = Math.floor(n / points), out = [];
  for (let p = 0; p < points; p++) { let peak = 0; for (let i = p * size; i < (p + 1) * size; i++) peak = Math.max(peak, Math.abs(channels[0][i])); out.push(Math.round(peak * 1e4) / 1e4); }
  return out;
}

function pageCanvas(manifest, kind, page) {
  const [fw, fh] = PACK.frame, rows = PACK.framesPerPage / PACK.columns, canvas = createCanvas(fw * PACK.columns, fh * rows), ctx = canvas.getContext('2d');
  const regions = buildRegions().items;
  for (let cell = 0; cell < PACK.framesPerPage; cell++) {
    const index = page * PACK.framesPerPage + cell; if (index >= manifest.frameCount) break;
    const x0 = (cell % PACK.columns) * fw, y0 = Math.floor(cell / PACK.columns) * fh, t = index / PACK.fps;
    if (kind === 'matte') {
      ctx.fillStyle = 'rgb(0,0,0)'; ctx.fillRect(x0, y0, fw, fh);
      for (const r of regions) { ctx.fillStyle = `rgb(${r.labelValue},${r.labelValue},${r.labelValue})`; ctx.fillRect(x0 + (r.bounds[0] + Math.sin(t + r.labelValue) * .01) * fw, y0 + r.bounds[1] * fh, r.bounds[2] * fw, r.bounds[3] * fh); }
    } else {
      const g = ctx.createLinearGradient(x0, y0, x0 + fw, y0 + fh); g.addColorStop(0, '#26323c'); g.addColorStop(1, '#4a5a3e');
      ctx.fillStyle = g; ctx.fillRect(x0, y0, fw, fh);
      for (const [i, r] of regions.entries()) { ctx.fillStyle = `hsl(${i * 62 + t * 8},55%,55%)`; ctx.fillRect(x0 + (r.bounds[0] + Math.sin(t + r.labelValue) * .01) * fw, y0 + r.bounds[1] * fh, r.bounds[2] * fw, r.bounds[3] * fh); }
      ctx.fillStyle = '#f4faed'; ctx.fillRect(x0 + ((t / manifest.duration) % 1) * fw, y0, 2, fh);
      ctx.font = '8px sans-serif'; ctx.fillText(`SYNTHETIC ${index}`, x0 + 3, y0 + 9);
    }
  }
  return canvas;
}

export async function writePack(out) {
  const manifest = buildManifest(), words = [{text: 'OUT', in: 2.88, out: 3.12, probability: 1}, {text: 'HOW', in: 6.14, out: 6.52, probability: 1}, {text: 'WHAT', in: 9.94, out: 10.08, probability: 1}];
  await fs.mkdir(out, {recursive: true});
  const write = (name, data) => fs.writeFile(path.join(out, name), data), json = (name, value) => write(name, JSON.stringify(value, null, 2) + '\n');
  await json('manifest.json', manifest); await json('regions.json', buildRegions()); await json('cues.json', {synthetic: true, words});
  await json('provenance.json', {synthetic: true, note: 'Generated test fixture; not derived from the film.', source: {sha256: GOD_HERE_SOURCE.sha256}});
  const channels = synthesizeAudio(manifest.duration, PACK.sampleRate, words), envelope = envelopeSummary(channels);
  await json('waveforms.json', {source: envelope, reference: envelope, 'dialogue-candidate': envelope});
  for (const name of ['source.wav', 'reference.wav']) await write(name, new Uint8Array(encodeWav(channels, PACK.sampleRate)));
  for (let page = 0; page < manifest.picture.pages.length; page++) {
    await write(manifest.picture.pages[page], pageCanvas(manifest, 'picture', page).toBuffer('image/png'));
    await write(manifest.matte.pages[page], pageCanvas(manifest, 'matte', page).toBuffer('image/png'));
  }
  const room = createCanvas(384, 216), rc = room.getContext('2d'), gradient = rc.createLinearGradient(0, 0, 384, 216); gradient.addColorStop(0, '#1b232b'); gradient.addColorStop(1, '#394a33');
  rc.fillStyle = gradient; rc.fillRect(0, 0, 384, 216); await write('room.png', room.toBuffer('image/png'));
  return manifest;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const out = process.argv[2];
  if (!out) { console.error('usage: node tools/make-synthetic-asset-pack.mjs <out-dir>'); process.exit(2); }
  const manifest = await writePack(path.resolve(out));
  console.log(JSON.stringify({status: 'written', out: path.resolve(out), synthetic: true, frames: manifest.frameCount, pages: manifest.picture.pages.length}));
}
