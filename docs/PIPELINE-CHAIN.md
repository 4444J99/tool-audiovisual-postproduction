# End-to-end synthetic chain

`tools/av_pipeline.py` runs the post-production tools in the order a set is used and pins every hand-off by a SHA-256, so a change anywhere upstream is visible downstream.

```bash
python tools/av_pipeline.py run --output outputs/av-pipeline [--blender] [--delivery ebu_r128|atsc_a85|streaming|podcast|spec.json]
python tools/av_pipeline.py verify --output outputs/av-pipeline [--delivery ebu_r128|...|spec.json]
```

| Stage | Reads | Writes |
|---|---|---|
| `set` | the synthetic set spec ([SET-SCENE](SET-SCENE.md)) | spec, scene-graph summary, the shot plan's `set` block |
| `shot_plan` | the `set` block | shot plan ([SHOT-PLAN](SHOT-PLAN.md)); the plan's set is byte-equal to the exported block |
| `dialogue` | nothing | synthetic dialogue with two digital-silence holes (16-bit mono WAV) |
| `requirement` | shot plan, dialogue | coverage requirement whose recorded room tone is the tone *measured* in the dialogue |
| `coverage` | shot plan, requirement | report, then the plan plus one validated shot per gap ([COVERAGE-MATRIX](COVERAGE-MATRIX.md)) |
| `room_tone` | dialogue | filled dialogue and fill report ([ROOM-TONE-FILL](ROOM-TONE-FILL.md)) |
| `delivery` | filled dialogue | delivered dialogue and delivery report ([LOUDNESS](LOUDNESS.md)) |
| `final_ledger` | the suggested plan, delivered dialogue | per-shot ledger and edit timeline, naming the delivered audio |
| `colour` | final ledger, set graph hash, delivered dialogue | layer plan and ledger ([COLOUR-AND-LAYERS](COLOUR-AND-LAYERS.md)) |
| `captions` | delivered dialogue, final ledger (frame rate) | SRT, WebVTT and report: invented sentences laid into the dialogue-active spans measured on the delivered WAV, on the edit's frame grid ([CAPTIONS](CAPTIONS.md)) |
| `burnin` | SRT, final ledger, set graph hash | the cues drawn onto 240x48 luma frames (one per edit frame, background tinted per shot) and a hash-chained frame ledger ([CAPTIONS](CAPTIONS.md#burn-in)) |
| `qc` | every artefact above that bears on delivery (twelve, listed in [DELIVERY-QC](DELIVERY-QC.md)) | `qc/qc-report.json`: caption timing against the dialogue, frame rate and timecode across set spec, shot plan and burned frames, loudness spec against a fresh measurement; re-derived by `verify` ([DELIVERY-QC](DELIVERY-QC.md)) |

`pipeline-manifest.json` lists each stage's input and output hashes and a hash over the whole manifest. `verify` reports, by name, an artefact whose bytes differ from the manifest, a stage input that is not an earlier stage's output, and a manifest edited after the fact. It also checks content links a hash cannot show: the final ledger was derived from the suggested plan, the colour plan's dialogue source is the filled WAV, and each plate's identity is derived from the set graph hash and its ledger row.

One command that runs this chain and also writes a small mp4 judged against it: [DEMO](DEMO.md).

## Delivery stage

`delivery` runs after the room-tone fill and before the final ledger. It measures the filled dialogue (BS.1770-4 integrated loudness, loudness range, true peak), plans the one linear gain that reaches the declared spec (`solve_gain`, refuse-on-ceiling), applies it, writes `audio/dialogue-delivered.wav`, **reads the written file back and measures that**, and checks it against the spec. The gain is rounded to four decimals first, so the delivered bytes can be re-derived from the filled WAV and the report alone. No limiter or other dynamics processing is applied. The default spec is `ebu_r128` (-23 LUFS, +-0.5 LU, -1 dBTP); `--delivery` takes any preset in [LOUDNESS](LOUDNESS.md) or a spec JSON file, and the spec's hash is recorded in the stage.

A chain whose audio cannot meet its spec is refused, not sealed: nothing is written to the manifest. With the invented dialogue (-26.6 LUFS, -13.3 dBTP peak) `ebu_r128`, `atsc_a85` and `podcast` deliver; the `streaming` preset (-14 LUFS) is refused because the +12.6 dB it needs would put the true peak at -0.7 dBTP, over its -1 dBTP ceiling. That refusal is the behaviour the stage exists to show; reducing the peak is a limiter's job, which this chain does not do.

The final ledger records the delivered audio's hash (`dialogue_sha256`) and the colour plan's dialogue source is the delivered WAV, so a change to the delivered audio is visible to every later stage.

### Tamper detection

`verify` has three layers, each tested:

1. **Hashes.** Any edit to `dialogue-delivered.wav` or `delivery-report.json` is named (`output dialogue-delivered on disk is ...`).
2. **Content links.** The ledger and colour plan must name the delivered WAV; the report's input hash must be the filled WAV on disk and its output hash the delivered WAV on disk.
3. **Re-derivation.** The delivered WAV must equal the filled WAV at the report's gain, byte for byte, and must measure within the report's spec *now*. This catches a forger who swaps the delivered WAV for a louder file and re-seals the report, manifest, ledger and colour plan so every hash agrees (a test does exactly that).

A forger can still edit the recorded spec to one the audio meets. `verify --delivery <spec>` closes that: it fails when the spec recorded in the chain differs from the one you declare. This is verification of the delivery file against its own source and spec, not a claim that any distributor will accept it.

The Blender build, when requested, is recorded as a final `blender` stage with the hashes of its report and `.blend`.

Plate identities are declared, not rendered: no render exists in this chain, and the manifest says so. `--blender` additionally builds the set spec headless and requires Blender's graph hash to equal the `set` stage's.

Everything is invented. A real set spec stays with the set it describes, never in this repository; build it with `tools/set_scene.py` from a private path.

### Burn-in tamper detection

`verify` re-renders all frames of the `burnin` stage from the SRT file, the final ledger and the recorded parameters and compares them with `captions/frames.y8` byte for byte, so a forger who edits a frame, the digest list, the chain head, the report and the manifest together is still named by the first frame that differs. The caption file must be the one the report was made from, the WebVTT file must carry the same cues as the SRT file, and the burn-in background segments must be the final ledger's shots. Tests cover a flipped pixel, a re-sealed forgery, a reworded caption and rewritten backgrounds.

### QC tamper detection

`verify` re-derives `qc/qc-report.json` from the artefacts on disk (`tools/av_pipeline.py verify [--qc-spec spec.json]`) and requires the file to be byte-equal and every check to pass, so a forger who fixes the caption files, the burned frames and every manifest hash still fails when a cue sits outside the dialogue ([DELIVERY-QC](DELIVERY-QC.md#tamper-detection)).
