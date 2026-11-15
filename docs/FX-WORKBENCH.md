# god-here audiovisual FX workbench

Implemented October 8, 2026. This is a functioning source-bound rack and demonstration edition of the 95.000–107.167 second passage. It is not a completed re-edit of the entire film.

## Open and operate

The private production preview is at <https://god-here-five-realities.ajpadavano.chatgpt.site/fx/>. The earlier five-realities player and audio review remain available at the same site.

For immediate playback, open <https://god-here-five-realities.ajpadavano.chatgpt.site/fx/watch.html>. Its native video player contains picture and sound together, loads no frame atlases or audio worker, and offers all six patches and thirteen processor demonstrations. Rotate the device for the corresponding composition. **Edit this patch** opens the matching rack through `?patch=` or `?demo=`. Direct movie links remain available if scripting cannot run.

1. Choose one of the six named patches and press **Play**.
2. Select any of the thirteen processors, then **Demonstrate selected**. This opens a full-canvas comparison: approximately 0–2 s bypass, 2–6 s extreme preset, 6–10 s Intensity sweep, and 10–12.167 s bypass. **Demonstrate patch** applies that sequence to the current rack.
3. Exit full screen to edit the rack. The up/down buttons exchange processor positions while retaining branch connections. **Make serial chain** deliberately replaces the current routing with a serial chain.
4. Use the Monitor selector for the original recording, the dialogue enhancement candidate, or expressive output. **Bypass rack** directly selects the dialogue reference and original picture; it does not sum several bypassed parallel branches.
5. Export the patch to retain the source identity, ordered nodes, typed cables, presets, macros, seed, curves and recorded changes. Import validates those fields before replacing the working patch. The current patch is also saved in browser storage when available.

### Playback recovery

Pending Play changes to **Cancel**. Canceling, pausing, or selecting another patch invalidates that playback request; later audio completion cannot restart it. Sound startup and rendering have bounded waits, explicit error state, and **Retry sound**. A failed worker clears the busy state and is replaced on retry. The original recording and dialogue candidate remain independently playable while expressive sound is unavailable.

Rendered previews request playback before requiring decoded frame readiness, including when a phone honors `preload="metadata"` without preloading video frames. Intentional pause/source-change `AbortError` results do not permanently disable the preset preview. Coarse-pointer devices use a smaller temporal frame-cache budget.

The focused recovery harness is `tools/check-fx-playback.mjs`. It requires Playwright WebKit and the private media pack in `web/fx/assets/`; set `PLAYWRIGHT_WEBKIT_EXECUTABLE` only when using a non-default installed browser. Its browser-level fault injections cover pending-play cancellation, worker failure/retry with reference audition, metadata-only video readiness, and invalid persisted patches. These checks do not establish physical iPhone behavior.

## Processor behavior

| Processor | Extreme picture/text operation | Voice operation |
|---|---|---|
| Routing | Travelling frame/word copies cross the canvas and mark arrival | Moving stereo route with distinct routed copies |
| Delay / Echo | Several scaled, displaced source-time returns coexist | Scheduled delayed taps; 100% wet has no hidden direct tap |
| Stutter | A captured gesture and word hammer, then release | Explicit captured interval, repeated windows, gaps and release |
| Granular | Temporally sampled tiles scatter and reorder | Seeded, enveloped grains with density, scatter and pitch/speed |
| Reverse | A selected body rewinds against continuing surrounding action | Reversed interval or route mode |
| Freeze / Hold | The selected body stays on a captured frame while others continue | Sustained granular fragment with duration and decay |
| Feedback | Successive scaled/rotated returns form a tunnel or invasion | Buffered, bounded returning signal, explicit reset and recurrence cap |
| Queue / Overflow | Distinct source moments accumulate and burst into space | FIFO/LIFO arrivals from distinct source windows, release and overflow |
| Gate / Erase | Selected picture, body, text or room disappears and returns | Periodic or amplitude-threshold gain gate, independent retained tail |
| Displacement | Strips stretch, fold, shear or flow, with containment/spill | Time displacement driven by the corresponding procedural field |
| Exchange | Region strips and their retained histories trade destinations | Stereo destination/temporal reassignment of the shared recording |
| Typography | Giant words, repeating walls or picture apertures | Transparent audio pass-through; typography is a visual processor |
| Signal Damage | Coarse blocks and aggressively reduced tonal levels | Quantization, sample-rate reduction, drive and filtering |

Body and room operations use the moving matte pack. Objects are explicit, editable rectangles. Selecting multiple supported targets combines those targets. A full-picture selection naturally contains the body and room. Text is a separate compositing plane. Its expressive words are not certified captions.

Each module exposes bypass, solo, wet/dry, supported targets, target address, clip/track scope, processor controls, a named preset, automation and an editable Intensity macro. Macro endpoint inputs define the values at Intensity 0 and 1. Editing a processor parameter directly removes that parameter's macro assignment; **Restore preset macro** restores the mapping. Auditioning presets preserves a separate saved custom setting, including its recorded changes.

Intensity is a parameter macro, not an additional wet/dry control. A fully wet low-Intensity patch can remain effectful because its macro minimums are authored settings. Audio output management only attenuates excess peak level; it does not normalize quiet effects upward or add a dry voice underneath.

## Six artwork patches

| Patch | Implemented graph | What to assess |
|---|---|---|
| A · Message impact | Routing → Delay → Displacement; parallel text-only Typography return | A moving trace, delayed copies and an affected receiving region |
| B · Closed inbox | Queue → Feedback → Gate; prior-return send around the gate | Arrivals accumulating, reception closing and earlier returns persisting |
| C · Out, but still here | Body gate, independent voice/word delay, held body/text return | Removal and persistence can coexist without an unprocessed voice mixed in |
| D · Correction machine | Stutter → Typography → Exchange | HOW / WHY / WHAT become large matter with changing destinations |
| E · Fractured utterance | Stutter → Granular → Reverse → Freeze | Atomization, reversed movement and held residue |
| F · Room takeover | Room routing → Displacement → Feedback; original body anchor | Room transformation around bodies retained as visible anchors |

WHY is authored screenplay material, not a claim that it was spoken in this excerpt. The browser exposes the expressive word source and editable authored words. OUT/HOW/WHAT timings are provisional machine-crosschecked hypotheses; listening acceptance remains open.

## Routing and timing

Connections explicitly carry **audio, picture, text, matte or control**. A cable also retains its role: insert, send, return or sidechain. Parallel signals meet at explicit mix nodes. Clip inserts are active on a half-open interval `[start, end)`; track inserts cover the passage. Bypass removes processing without cancelling an intentional delay. Solo leaves the selected processor(s) active and bypasses other processors in their existing graph positions.

Audio is computed against output sample time in a worker. Picture sampling is quantized to the 12 fps addressable frame bank and follows the audio playback clock. Temporal operators request the upstream graph at the required source time, so reordering modules changes the composition. Internal feedback has finite recurrence and a nonzero delay; instantaneous graph cycles are rejected.

Control cables can be added independently of audio cables. The source amplitude detector and short per-node control lanes are shared from the audio render to the picture engine. The checkbox in each module creates a source-envelope-to-Intensity sidechain; advanced cables can use another node's control output. Parameter automation curves supply authored modulation independently of audio analysis.

**Record controls** captures Intensity, wet/dry, processor parameters, bypass and solo at the output clock. **Replay** restarts the stored event score. Structural edits such as rewiring, adding nodes, object rectangles and target selection are saved as the resulting graph, rather than a time-varying graph-edit log. The automation editor accepts comma-separated `time:value` pairs with linear or step interpolation. An edited parameter curve supersedes recorded events for the same node/parameter.

Matte values are categorical identities, not brightness. Their mix uses ordered nonzero-label compositing. Matte wet selection switches at 0.5 instead of blending identity numbers; picture and sound retain continuous wet/dry blending. This is an explicit preview convention, not physically accurate opacity tracking of every overlapping layer.

## Preview and offline render

The editable picture engine uses 384 × 216 processing canvases and the 640 × 360 source frame atlas. It fills the monitor and full screen. A narrow portrait canvas uses three independently cropped picture bands under one full-height text plane. The native full-screen API is used where supported, with a fixed-viewport fallback for phone browsers.

Unchanged factory patches and demonstrations use their corresponding encoded pictures. Both 480 × 270 horizontal and 270 × 480 portrait renders are generated from the same graph evaluation, then fitted to the display. The signal signature includes all output-affecting node fields, cable order, source identity, seed, automation, words and phase score. Cosmetic labels and saved UI snapshots do not affect the signature. Any signal edit switches to the editable renderer; it never silently plays the old factory movie as if it incorporated the edit. The displayed **Rendered preset / Editable rack** indicator makes the mode visible.

Dense edited feedback graphs can run below 12 fps. The audio clock continues independently and pictures skip forward to its current time. The offline renderer evaluates every frame in short child-process chunks to bound native canvas allocations. Its receipt records source, patch, code and output hashes; raster, frame count, audio sample count, font substitute and encoding details. H.264/AAC outputs are lossy previews, so encoded pixels/audio are not claimed to be bit-identical to browser output.

To serve a local checkout, provide the private media pack under `web/fx/assets/`, then run an HTTP server from `web/`. Modules and tests require no build step. Media are deliberately excluded from Git.

```bash
python -m http.server 8000 --directory web
npm test
python -m unittest discover -s tests -v
```

For offline rendering, install Node 20+, FFmpeg/ffprobe and the pinned `@napi-rs/canvas` development dependency, then:

```bash
node tools/render-fx.mjs --engine-dir web/fx --assets-dir web/fx/assets \
  --patch web/fx/fixtures/demo-delay.json --output outputs/delay.mp4 \
  --portrait-output outputs/delay-portrait.mp4 --keep-wav

node tools/render-factory.mjs --engine-dir web/fx --assets-dir web/fx/assets \
  --output-dir outputs/fx-factory
```

The asset pack needs `manifest.json`, the picture/matte atlas pages it addresses, source/reference WAVs, room plate, cues, regions, provenance and waveforms. The source film hash is bound in `god-here.config.mjs`; the pack's provenance records its derivation. The rendition receipt declares the font used. Supplying `--font` makes that choice explicit on another host.

## Verification and remaining production limits

Automated checks cover graph semantics, all presets, common controls, bounded feedback, clip edges, seeded replay, parameter/event precedence, audio sample behavior, module order, malformed imports, saved custom settings, matte-return isolation, source gains, typography persistence and preview identity. Source-derived pixel checks additionally compare bypass/wet-zero/repeated renders, reordered chains, body-return containment and portrait coverage. Browser interaction checks exercise real Web Audio playback, rack editing, automation, import/export, full screen and a phone-size viewport.

Those checks establish engineering behavior. They do not establish accepted dialogue restoration, certified performed wording, clean production mattes, accurate unseen room reconstruction, isolated performer audio, a full-film FX edition, physical iPhone/Safari performance or audience/installation acceptance. The current body/room pack is explicitly a workprint; target addresses use clothing descriptors rather than asserted performer names.

## Silent browsers

No check may produce audible sound. Every Playwright launch (`tools/site_audit_snapshot.py`, `tests/studio/browser_acceptance.py`, `tools/check-fx-playback.mjs`) goes through `tools/silent_browser.py` or `tools/silent-browser.mjs`: Chromium gets `--mute-audio` merged into the caller's arguments, and every context gets `tools/silent-init.js`, which pins audio and video elements to native `muted = true` and `volume = 0` (the page still reads back what it set) and routes every realtime `AudioContext` destination connection through a gain-0 sink. `tests/test_silent_browser.py` proves this with silent fixtures and `tests/test_silent_browser_lint.py` fails on any launch that bypasses the helper.
