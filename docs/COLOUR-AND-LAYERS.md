# Colour grade chains and layered compositing plans

`amp/studies/colour_grade.py`, `amp/studies/layer_plan.py` and `tools/colour_layers.py` cover the last two stages of a post-production pass: a repeatable colour grade, and a layer stack that is checked before anything is rendered. Both are defined in scene-linear light so a grade behaves like light, not like code values.

## Colour chain (`amps.colour_chain.v1`)

An ordered list of operations on linear RGB. sRGB 8-bit frames are decoded on entry and encoded on exit (`apply_display`); `apply_linear` takes float linear RGB.

| Op | Parameters | Definition |
|---|---|---|
| `exposure` | `stops` (within +-10) | multiply by 2^stops |
| `cdl` | `slope`, `offset`, `power`, `saturation` | ASC CDL: `clamp(in*slope+offset)^power`, then saturation about Rec.709 luma |
| `curve` | `points`, `channel` (`luma`, `r`, `g`, `b`, `all`) | monotone piecewise-linear tone curve spanning 0 to 1; the luma channel scales RGB so chroma ratios hold |
| `mono` | `weights` (sum 1), optional `tint` | channel-mixer black and white |
| `lut` | `cube` text or `identity` size, `space` (`srgb`, `linear`) | trilinear 3D LUT, `.cube` layout (red fastest), 0..1 domain only |
| `region_tint` | `bands` of `x0`, `x1`, `tint`, `strength` | vertical bands of the frame multiplied toward a tint; the way to colour regions of a shared room without per-shot masks |

`validate_chain` reports every problem (non-monotone curves, weights that do not sum to 1, overlapping bands, malformed LUTs). `chain_hash` is a SHA-256 over the canonical JSON, so a grade is addressable. `grade_report` gives mean linear luma, mean chroma and the fractions clipped high or crushed low before and after, so a grade is accepted on numbers.

## Layer plan (`amps.layer_plan.v1`)

Layers are listed bottom to top: `plate`, `matte`, `fx`, `text`, `solid` or `adjustment`. Each has a frame range inside the composition, `blend` (`normal`, `multiply`, `screen`, `add`, `overlay`), `opacity` (a number or `[frame, value]` keyframes), an optional `chain`, and an optional `matte` (`layer`, `mode` of `alpha`, `luma`, `invert_luma`).

Checked before any render: unique IDs; ranges inside the composition; every source defined, content-addressed by SHA-256 and referenced by a relative path only (no absolute paths, URLs or `..`, so a plan can be published without revealing where private media lives); audio never used as a picture layer; chains defined and valid; mattes exist, are of kind `matte`, sit below the layer they matte, cover its frames and are not themselves matted; adjustment layers carry a chain and no source; every composition frame has an opaque base layer unless `allow_gaps`. A source whose colour space differs from the composition's linear working space is a warning, because it is converted.

`flatten(plan, frame, load)` is the reference compositor (straight alpha, linear float; `load(source, frame)` returns RGBA in the source's declared colour space). A faster renderer must match it. `layer_ledger` lists each layer with its chain hash, source hash and matte, bound to the plan hash.

```bash
python tools/colour_layers.py demo outputs/colour-demo              # synthetic chart, grade report, composite frames, ledger
python tools/colour_layers.py grade --chain chain.json --input in.png --output out.png
python tools/colour_layers.py validate-chain --chain chain.json
python tools/colour_layers.py validate-plan --plan plan.json --ledger ledger.json
python tools/colour_layers.py identity-lut 33 --output identity.cube
```

A failing chain or plan prints every failure as JSON, exits 2 and writes nothing. The demo uses only invented sources and synthetic frames; real plans belong with the production they describe. A passing grade or plan fixes arithmetic and bookkeeping, not artistic approval of the look.
