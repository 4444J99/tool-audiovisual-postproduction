# audiovisual-media-postproduction-system

A directable audiovisual postproduction system that translates creative notes into reproducible edits: audio restoration and design, dialogue and performance revision, animated typography, tracked reframing, detail reconstruction, compositing, and new editions from existing media.

The [ACE implementation contract](ace-manifest.json) separates functional software, capabilities, modules, source media, compositions, artworks, editions and archives. Composition configuration now lives in `compositions/comp-001`; the original `projects/god-here` path remains a compatibility link. The `amp` package and commands keep their identities. Use `amp validate --composition comp-001`, or supply another composition directory. See the [naming boundary and reversible migration](docs/architecture/functional-identities.md).

## Current verification and finish line · 12 October 2026

Start with the [current finish-line plan](docs/GOD-HERE-FINISH-LINE.md), [release verification](manifests/acceptance/2026-10-12/summary.json), and [canonical site](https://god-here-five-realities.ajpadavano.chatgpt.site/). The published Studio brings source review, comparisons, composition tools, catalogue and project records into one entry point. Its current checks pass **318 Python tests and 241 Node tests**; six additional editor cases run inside the Python suite.

Fresh media verification completed **241 full-decode file-path checks**: [224 prepared paths](manifests/acceptance/2026-10-12/prepared-media-decode.json) plus [17 recovered directed-media paths](manifests/acceptance/2026-10-12/directed-media-recovery.json). These are scoped checks, not a claim of 241 distinct historical works. The known damaged extra `proof-D.mp4` remains recorded and excluded from delivery.

The published Studio routes require owner sign-in. Small protected assets are served from server-only storage; large protected assets use a fixed R2 asset map behind the access check. Protected applications, source review and documents are absent from the public static build. Existing public media routes and their 52 fixed registrations are retained. **Version 18 is live**, from Site source `4d70e621e754f27d57359adabc93c0cfa5e91ab2`. Both suites passed on code `901552a7af7944498a49c5b773b6e25d656857a2`. The [current publication receipt](manifests/acceptance/god-here-studio-publication.json) records production access checks, full-download verification of 52 public and 41 protected registrations, and explicit shutdown of temporary upload administration. Owner-session browser controls and physical-device acceptance remain open.

The older checkpoints below describe their dated evidence. Current review repairs and release verification supersede their test counts; they do not silently certify listening, final mattes, reconstructed shots, browser-local data migration or physical devices.

## Consolidation checkpoint · 11 October 2026

**Priority at this checkpoint: consolidate and verify the existing project before further creative production.** Start with the [cross-session recovery register](docs/recovery/god-here-cross-session-register-2026-10-11.md), [content-addressed artifact catalog](manifests/artifacts/god-here-recovery-catalog.json), [historical identity expectations](manifests/artifacts/god-here-recovery-expectations.json), and [integration map](docs/architecture/god-here-consolidation-map.md). Preservation, successful decoding, reproducible rendering, publication and artistic acceptance have separate evidence. Historical recovery is not complete.

The [engineering integration record](docs/ENGINEERING-INTEGRATION.md) identifies the retained directed-recipe, directed-revision, repaired FX, and room-study heads combined in this tree. The existing `amp` entry point and each native recipe format remain available. Source identity is shared; each renderer's timing and image-processing semantics stay explicit. The [shared execution contract](docs/architecture/shared-execution-contract.md) adds `amp execute`, with five exact-input bindings across the four retained native families. It verifies content hashes, records native clocks and producing code, and checks complete output sets without translating the original recipes.

All five bound treatments now have verified actual outputs: directed and reference proofs, the integrated excerpt, the FX delay demonstration and the complete room-stretch set. Their [retained-execution addendum](manifests/artifacts/god-here-retained-execution-availability.json) accounts for 95 distinct required outputs, including 89 fully decoded media files. Sixty identities already had verified custody; 35 new identities are now privately preserved. The later room verification preserves the original producing revision and records that it performed no rerender. The combined code passes 150 Python, 25 room and 97 FX tests, with a verified package build and [hosted CI evidence](manifests/acceptance/god-here-corrected-code-hosted-ci.json).

The [publication receipt](manifests/acceptance/god-here-workbench-publication.json) records the earlier workbench deployment and media-delivery status; [current release verification](manifests/acceptance/2026-10-12/summary.json) records this revision’s checks. Repository integration, a saved deployment, complete media decoding, and artistic acceptance are separate gates.

## Audiovisual FX workbench

[Open the FX workbench](https://god-here-five-realities.ajpadavano.chatgpt.site/fx/).

[Play the demonstrations](https://god-here-five-realities.ajpadavano.chatgpt.site/fx/watch) uses a lightweight native video player with embedded sound, six patches and all thirteen module demonstrations. It selects portrait or landscape media for the device and links each selection to its editable rack.

The retained `web/fx/` application implements thirteen processors with extreme presets, six saved artwork patches, independent typed connections, clip and track inserts, serial and parallel paths, sends, returns, sidechains, and replayable parameter changes. Each processor has bypass, solo, wet/dry, target selection, automation and an editable Intensity macro. Intensity does not secretly blend the dry recording into fully wet sound.

The working passage is source **95.000–107.167 seconds**. Voice processing uses the shared stereo dialogue candidate. Picture processing uses random-access source frames, five moving appearance-labelled workprint mattes, authored object rectangles and a qualified hybrid room fill. Original audio and the dialogue candidate remain separately selectable.

Select a processor, then **Demonstrate selected**, to run bypass → extreme → parameter sweep → bypass in full screen. The six patches also have **Demonstrate patch**. Cached picture playback requires a complete manifest and exact matching patch and producing-engine hashes. The repaired runtime renders live when those requirements are not met. Earlier demonstrations retain their original embedded picture and sound in the separate demonstration player. Sound is computed in a worker and runs on the audio clock. Dense custom graphs can render below the 12 fps preview rate.

The workbench's Control tab adds touch faders, knobs and switches with MIDI learn; see [the controller guide](docs/FX-CONTROLLER.md). A local OSC bridge connects the same controls to TouchDesigner ([guide](docs/FX-TOUCHDESIGNER.md)).

See [the workbench guide](docs/FX-WORKBENCH.md), [the processing contract](docs/FX-ENGINE.md), and [the source specification](docs/god-here-fx-workbench-spec.md). Portable JSON fixtures and schema are in `web/fx/fixtures/` and `web/fx/rack.schema.json`. Original film and derived media stay outside the public repository.

## Horizontal / vertical layout geometry

The eight-layout comparison's placement rules are a pure module (`web/fx/layout-geometry.mjs`) that the renderer and `tools/analyze-layout-geometry.mjs` share; the measured horizontal-versus-vertical result is in [docs/LAYOUT-COMPARISON.md](docs/LAYOUT-COMPARISON.md).

## Reverse-angle ledger

`tools/reverse_angle.py` plans a same-side reverse camera and reports which reverse-view pixels the source recording genuinely shows ([guide](docs/REVERSE-ANGLE.md)).

## First implemented project: god-here

This repository now contains a working Python renderer and a static interactive edition for **God's Ears**. The artist's sketch becomes five independently scored regions in the existing shared room. Each performer keeps a visual identity across the two camera views. The five authored trial vocabularies are pressure, lag, refraction, residue, and erosion.

Implemented production outputs include source-length scored workprints, matched A–E excerpts, trained DTLN dialogue trials, learned person masks, a nonlooping source-pause room bed, 47 authored foley events, a manual arm-mask / responsive wall study, and three seeded source-unit edits. The browser edition uses one video clock, bounded shot-local history, attention selection, reduced motion, source-time score export/import, and a linear fallback.

**These are workprints and implemented studies. Audio intelligibility, performed wording, fine mask contours, artistic selection, real-phone playback, and installation calibration are not certified.** Workprint 02 uses a provisional 70% learned / 30% original dialogue blend. The single supplied stereo soundtrack does not yield original isolated voice tracks. Character return buses are routed estimates from the shared mixture. Foley is newly authored support; its stove hypothesis remains an optional separate track.

## Run

Requires Python 3.10+, FFmpeg/ffprobe with libx264 and AAC support, NumPy, SciPy, and Pillow.

```bash
python -m pip install -e .
amp validate
amp audio --source /path/to/source.mp4 --output outputs/audio
amp render --source /path/to/source.mp4 \
  --audio outputs/audio/candidate-B.wav \
  --output outputs/god-here-linear-picture.mp4 --typography
python tools/finish_production.py /path/to/source.mp4 outputs
amp generate --source outputs/god-here-linear-workprint-01.mp4 \
  --seed 17 --output outputs/variation-17.mp4
```

For the trained sound/mask pass, install `python -m pip install -e '.[learned]'` and follow [the sound-pass recipe](docs/SOUND-PASS-02.md). Model weights are external, revision-pinned and checksum-checked; no GPU or new model training is required.

Audit the full-source review record with `python -m amp.cli qualify`. Preview the historical provisional schedule with `python -m amp.cli generate --dry-run`; explicit `--generation-mode linguistic` additionally requires listened, source-bound qualification and currently fails closed because all real-source frames remain pending. These audit/preflight commands require only the Python standard library. See the [qualification contract](docs/GOD-HERE-QUALIFICATION.md) and [13 October evolution directive](docs/GOD-HERE-EVOLUTION-2026-10-13.md).

Built wheels include the native execution producers and FX engines, so `amp execute` also works outside a source checkout. FX execution additionally requires Node 20+, `@napi-rs/canvas` 0.1.100, and a font supplied through the binding. Install Canvas with `npm install --prefix /path/to/amp-js @napi-rs/canvas@0.1.100` and set `AMP_NODE_MODULES=/path/to/amp-js/node_modules`; the existing Codex runtime fallback remains supported. No media, fonts, project recipes, or private locators are included in the wheel. For an installed wheel, supply hash-addressed inputs through `--locations`; retained bindings with `repo_path` references require their source-checkout layout or equivalent hash-only references to the same bytes.

For the report that the room bed sounds like activity, use the opt-in [room-tone refinement comparisons](docs/ROOM-REFINEMENT.md). This pass recovers the earlier rack, tests shared band timing and effect bypass, and renders sustained and stationary source-shaped textures. It includes cued source-pause reviews and matched listening comparisons; the existing production default remains available pending listening selection.

The [stretch-and-stack experiment](docs/ROOM-STRETCH.md) expands the same 4.42-second source reel to 112, 84, 56, 28, and 14 seconds, repeats each to fill the film, and renders progressive and original-relative-gain stacks. The [hosted room-tone workbench](https://god-here-five-realities.ajpadavano.chatgpt.site/room-tone/) provides native playback, full-film context comparisons, source review, and lossless downloads.

Pass `--start` and `--end` for excerpts, `--layers` for the manual fine-layer variant, and `--mode soft` or `--mode spill` for authored boundary extensions. Hard boundaries are the baseline. The final source-length picture uses 3,360 frames at 30 fps. The pilot uses source frames 2,554–3,214 inclusive, 661 frames.

`tools/finish_production.py` is the **god-here production recipe**, with its explicit 112-second baseline. `amp` holds reusable processing primitives; composition-specific intervals, masks, styles, and scores live in `compositions/comp-001` (legacy alias `projects/god-here`). Original media are external inputs, not committed public assets. Source identities are recorded by SHA-256.

## Directed revision extension

Two retained implementations provide distinct authored alternatives. The [direction proof](docs/DIRECTION-PROOF.md) uses `python -m amp.direction`, the `amps.direction.v1` recipe, source-frame camera paths, and independently timed output-frame typography. The [directed revisions](docs/DIRECTED-REVISIONS.md) use `amp revision`, source-time typography and attention replay, and an editable REAPER/Fusion handoff. Their recipes are not interchangeable; the integration record explains the separate clocks and dependencies.

The second implementation includes three directorial alternatives and a complete 112.6-second candidate. Both retain their original review pages and external media requirements; their presence in this repository does not establish deployment of those pages or artistic selection.

```sh
python tools/build_directed_proof.py /path/to/source.mp4 outputs/directed
python -m amp.cli replay --source /path/to/source.mp4 --audio outputs/audio/candidate-B.wav \
  --session god-here-attention-score.json --output outputs/attention-film.mp4
```

## Earlier hosted browser edition

[Open the god-here workbench](https://god-here-five-realities.ajpadavano.chatgpt.site) · [Recovery register](https://god-here-five-realities.ajpadavano.chatgpt.site/recovery/).

The canonical Site was public when its access configuration was inspected on 2026-10-11. Private originals and performer references retain separate owner custody; the public repository holds their non-sensitive identities rather than their media bytes.

The earlier publication receipt verifies all 52 registered media files by response metadata, actual byte ranges and complete-download SHA-256. The keeper is available at its original URL, and temporary upload access has been removed. The reconciled FX runtime preserves the Site loader and the producing-code identity of earlier films. The unchanged baseline recipe has been reproduced: all 11 generated MP4s match retained hashes and all 67 generated audio/video files pass full decoding. The [publication receipt](manifests/acceptance/god-here-workbench-publication.json), [approved media payload](manifests/acceptance/god-here-public-media-delivery-review.json), and [reproduction evidence](manifests/acceptance/god-here-baseline-reproduction.json) record the scope of that earlier publication. They do not certify deployment of the revised Studio. Browser/device and artistic acceptance remain open.

For the legacy browser-only edition, serve its public `web/` assets from a static HTTP server. The authenticated Studio additionally requires its server routes and protected asset delivery; a static copy does not provide those features. Supply the legacy media under `web/media/`:

- `interactive-source.mp4`: source picture with trained-dialogue, room bed and foley support for the live shader.
- `dialogue-reference.mp4`: source picture with the provisional trained-dialogue blend.
- `god-here-linear-workprint-02.mp4`: new complete sound/person-mask study and linear fallback.
- `proof-A.mp4` through `proof-E.mp4`: aligned comparisons.
- `audio-trial-A.m4a`, `audio-trial-B.m4a`, `audio-trial-N.m4a`, `audio-trial-H.m4a`: randomized original/DSP/trained/blended trials.
- `room-tone.m4a`, `source-pauses.m4a`, and `foley-movement/glass/sip/stove.m4a`: isolated construction auditions.

The shader is a lightweight browser interpretation of the five vocabularies. Learned body preservation is delivered in the offline E proof/full film; the live shader still uses fixed regions. It is not pixel-identical to the offline renderer. Spatial identities, score timing, boundaries, and source clock are shared. If WebGL is unavailable, the native video remains playable. This first browser build has **not** been verified on an actual iPhone or Safari session.

The hosted review uses 854 × 480 phone proxies. Downloaded film workprints remain 1280 × 720. Publication succeeded; device/runtime QA and optional WebMCP registration validation were unavailable in the execution environment.

Saved attention scores are source-time control schedules, not video recordings or complete wall-clock interaction logs. Seeking recomputes the score and clears shot history. The reduced-motion mode caps deformation; sound playback remains the shared recording, without a claim to isolate the selected voice.

## Verify

```bash
python tools/check.py
```

This runs project validation, all Python tests, the score/transport checks, the room-tone browser checks, and the repaired FX regression suites. Set `AMP_TEST_FONT` to an installed font file to exercise the two typography tests when their default test font is unavailable. `npm test` runs the FX, room and score families; `tools/check.py` also runs the Studio and directed-media checks. No model download is required for these checks.

Tests cover neutral pass-through, containment, shared-space preservation, identity remapping, fallback layouts, invalid geometry, deterministic permitted transitions, attention residue, session validation, explicit retiming, and source-layout packaging. Production verification additionally fully decodes delivered videos, counts frames, checks loudness in the delivered audio encodes, and measures repaired waveform alignment.

See [production status](docs/PRODUCTION-STATUS.md), [architecture](docs/ARCHITECTURE.md), [installation readiness](docs/INSTALLATION.md), and the 38-task acceptance backlog in `projects/god-here/backlog.json`.
