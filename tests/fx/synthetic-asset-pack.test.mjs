import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
import {writePack, buildManifest, buildRegions, PACK} from '../../tools/make-synthetic-asset-pack.mjs';
import {assetPackIdentity} from '../../web/fx/asset-pack.mjs';
import {decodeWav, amplitudeEnvelope} from '../../web/fx/audio-engine.mjs';
import {validateDecodedAudio, normalizeWorkbenchPatch} from '../../web/fx/workbench-state.mjs';
import {VisualEngine} from '../../web/fx/visual-engine.mjs';
import {createSingleModulePatch} from '../../web/fx/core.mjs';
import {GOD_HERE_SOURCE, GOD_HERE_REGIONS} from '../../web/fx/god-here.config.mjs';

const require = createRequire(import.meta.url), {createCanvas, loadImage} = require('@napi-rs/canvas');

test('the synthetic pack satisfies the loader contract and drives the real visual engine', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'synthetic-pack-'));
  try {
    const manifest = await writePack(dir), bytes = name => readFile(path.join(dir, name)), json = async name => JSON.parse(await bytes(name));
    assert.deepEqual(manifest, buildManifest());
    assert.equal(manifest.synthetic, true);
    const identity = await assetPackIdentity(await bytes('manifest.json'), bytes);
    assert.equal(identity.files.length, 2 * manifest.picture.pages.length + 7);
    const audio = validateDecodedAudio(decodeWav(await bytes('reference.wav')), manifest);
    assert.equal(audio.channels.length, 2);
    assert.ok(amplitudeEnvelope(audio.channels, PACK.sampleRate).reduce((a, b) => Math.max(a, b), 0) > .1, 'dialogue stand-in carries energy');
    const regions = await json('regions.json'), cues = await json('cues.json'), provenance = await json('provenance.json');
    assert.deepEqual(regions.items.map(r => r.id), [...GOD_HERE_REGIONS]);
    assert.equal(provenance.source.sha256, GOD_HERE_SOURCE.sha256);
    assert.equal(provenance.synthetic, true);
    const waveforms = await json('waveforms.json');
    assert.ok(waveforms['dialogue-candidate'].length > 100);
    const assets = {picture: await Promise.all(manifest.picture.pages.map(p => loadImage(path.join(dir, p)))), matte: await Promise.all(manifest.matte.pages.map(p => loadImage(path.join(dir, p)))), room: await loadImage(path.join(dir, manifest.room.hybrid)), cues, regions, waveforms};
    const engine = new VisualEngine(manifest, assets, {createCanvas, width: 192, height: 108, cacheFrames: 8, control: () => 0});
    const source = {id: 'god-here-second-draft', sha256: provenance.source.sha256, offset: manifest.sourceIn, duration: manifest.duration};
    const patch = normalizeWorkbenchPatch(createSingleModulePatch('delay', {source, demonstration: false}), {source, regions: regions.items.map(r => r.id), duration: manifest.duration});
    const frame = engine.frame(3), matte = engine.frame(3, true), layers = engine.renderLayers(patch, 3);
    assert.ok(frame && matte && layers.picture);
    const labels = new Set(), pixels = matte.getContext('2d').getImageData(0, 0, 192, 108).data;
    for (let i = 0; i < pixels.length; i += 4) labels.add(pixels[i]);
    for (const region of regions.items) assert.ok([...labels].some(v => Math.abs(v - region.labelValue) < 15), `matte label for ${region.id}`);
  } finally { await rm(dir, {recursive: true, force: true}); }
});

test('manifest and regions are deterministic and the last page holds the remainder', () => {
  assert.deepEqual(buildManifest(), buildManifest());
  assert.deepEqual(buildRegions(), buildRegions());
  const m = buildManifest();
  assert.equal(m.picture.pages.length, Math.ceil(m.frameCount / m.framesPerPage));
  assert.equal(m.frameCount, 146);
});
