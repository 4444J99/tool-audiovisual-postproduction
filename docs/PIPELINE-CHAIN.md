# End-to-end synthetic chain

`tools/av_pipeline.py` runs the post-production tools in the order a set is used and pins every hand-off by a SHA-256, so a change anywhere upstream is visible downstream.

```bash
python tools/av_pipeline.py run --output outputs/av-pipeline [--blender]
python tools/av_pipeline.py verify --output outputs/av-pipeline
```

| Stage | Reads | Writes |
|---|---|---|
| `set` | the synthetic set spec ([SET-SCENE](SET-SCENE.md)) | spec, scene-graph summary, the shot plan's `set` block |
| `shot_plan` | the `set` block | shot plan ([SHOT-PLAN](SHOT-PLAN.md)); the plan's set is byte-equal to the exported block |
| `dialogue` | nothing | synthetic dialogue with two digital-silence holes (16-bit mono WAV) |
| `requirement` | shot plan, dialogue | coverage requirement whose recorded room tone is the tone *measured* in the dialogue |
| `coverage` | shot plan, requirement | report, then the plan plus one validated shot per gap ([COVERAGE-MATRIX](COVERAGE-MATRIX.md)) |
| `room_tone` | dialogue | filled dialogue and fill report ([ROOM-TONE-FILL](ROOM-TONE-FILL.md)) |
| `final_ledger` | the suggested plan | per-shot ledger and edit timeline |
| `colour` | final ledger, set graph hash, filled dialogue | layer plan and ledger ([COLOUR-AND-LAYERS](COLOUR-AND-LAYERS.md)) |

`pipeline-manifest.json` lists each stage's input and output hashes and a hash over the whole manifest. `verify` reports, by name, an artefact whose bytes differ from the manifest, a stage input that is not an earlier stage's output, and a manifest edited after the fact. It also checks content links a hash cannot show: the final ledger was derived from the suggested plan, the colour plan's dialogue source is the filled WAV, and each plate's identity is derived from the set graph hash and its ledger row.

The Blender build, when requested, is recorded as a final `blender` stage with the hashes of its report and `.blend`.

Plate identities are declared, not rendered: no render exists in this chain, and the manifest says so. `--blender` additionally builds the set spec headless and requires Blender's graph hash to equal the `set` stage's.

Everything is invented. A real set spec stays with the set it describes, never in this repository; build it with `tools/set_scene.py` from a private path.
