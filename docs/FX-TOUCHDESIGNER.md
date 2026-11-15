# FX rack and TouchDesigner (OSC bridge)

> **Status, stated plainly (2026-11-14): none of the TouchDesigner work in this repository has been run in TouchDesigner.**
> The generated network has never been built in the real application, the OSC contract has never been exercised by a real
> TouchDesigner OSC In/Out, and no `.toe` or `.tox` produced by this code has ever been opened. Nothing below claims it works
> there. What exists is checked **structurally and numerically only**: the spec validates against its own validator and is
> byte-identical across runs; operator and parameter names match the application's parameter reference (`check-vocabulary.mjs`);
> the builder runs against a fake operator tree; the generated Python module runs under CPython (`headless_td.py`); and the
> numeric models (delays, grain weights, Feedback return rule, Damage hold) are compared with the workbench engine's own
> arithmetic. The first in-app check is the [checklist below](#first-in-app-check-for-the-owner).

The workbench's Control tab can be driven from, and can report to, TouchDesigner. A browser cannot send UDP, so a
small local program, the **bridge**, sits between them:

```
workbench page --JSON over WebSocket--> bridge --OSC over UDP--> TouchDesigner   (rack state, feedback)
workbench page <--JSON over WebSocket-- bridge <--OSC over UDP-- TouchDesigner   (control)
```

Everything TouchDesigner sends goes through the same parameter setter as the mouse, touch and MIDI, so recording,
undo, custom-setting memory and rendering behave identically. Mappings and learn are not involved: OSC addresses are
fixed (below). The processing engines and preview signatures are untouched.

## Run it

```
node tools/fx-td-bridge.mjs            # or: npm run bridge:td
```

Requires only Node 22 or newer (the tests use its built-in WebSocket client), already needed for the workbench tests; no packages are installed. Defaults:

| Option | Default | Meaning |
|---|---|---|
| `--ws-port` / `--host` | 9120 / 127.0.0.1 | where the workbench page connects |
| `--td-port` / `--td-host` | 7000 / 127.0.0.1 | where TouchDesigner listens (OSC In) |
| `--osc-port` / `--osc-host` | 7001 / 127.0.0.1 | where the bridge listens for TouchDesigner (OSC Out) |
| `--allow-origin URL` | none | extra web origins allowed to connect (repeatable) |

Then, in the workbench, open **Control > TouchDesigner (OSC)** and choose **Connect bridge**. The address is remembered
in this browser. The page reconnects by itself (0.5 s growing to 8 s) if the bridge restarts. **Send rack state back**
turns the outbound half off, for a one-way setup or to rule out a feedback loop.

**Who may connect.** The bridge listens on loopback only. A browser page is accepted when it is itself served from
`localhost`, `127.0.0.1` or `[::1]`, or when its exact origin is named with `--allow-origin` (for example the published
workbench, `--allow-origin https://your-site.example`). Any other page is refused with HTTP 403, so a web page you happen
to visit cannot steer the rack or send OSC through your machine. Clients that send no `Origin` header (TouchDesigner's
WebSocket DAT, scripts) are accepted; this is safe only because the socket is loopback. Binding another host
(`--host 0.0.0.0`) exposes the bridge to your network and is your decision; there is no password.

## Try the bridge without TouchDesigner or the film

The workbench page needs its media pack before it draws the Control tab. Without the private pack, generate a stand-in
and run a scripted TouchDesigner against the real page:

```
node tools/make-synthetic-asset-pack.mjs web/fx/assets      # git-ignored; synthetic: true in manifest and provenance
node tools/fx-td-bridge.mjs                                  # terminal 1
python3 -m http.server 8765 --bind 127.0.0.1                 # terminal 2, repository root
node tools/fx-td-simulator.mjs outputs/bridge-e2e.json       # terminal 3: waits for the page to link
```

Open `http://127.0.0.1:8765/web/fx/index.html?patch=closed-inbox` (a fresh page otherwise loads `message-impact`, whose processors differ from the network's), then **Control > TouchDesigner (OSC) > Connect bridge**. The simulator
asks for the rack state until the page answers, then sends knob 0, switch 0 and transport loop (each to a value different from the one the rack reported, since a repeat is an echo, not an edit), then an unknown address, a wrong argument type and an unknown node id, and
checks what the rack reports back. It exits 0 only when every check passes. The synthetic pack carries the source
identity the factory patches bind to, so it is a local fixture: never publish or commit it.

## Addresses

Values for ranges are **normalised 0 to 1** and are scaled onto each control's own range and step, exactly as a MIDI
fader would be; out-of-range values are clamped. Switches take 0/1, a boolean, or (for triggers) no argument.

Into the rack (TouchDesigner OSC Out):

| Address | Argument | Effect |
|---|---|---|
| `/fx/selected/knob/<n>` | float | n-th range control of the selected processor (0 Intensity, 1 Wet / dry, then its parameters) |
| `/fx/selected/switch/<n>` | 0/1 | 0 Bypass, 1 Solo on the selected processor |
| `/fx/node/<id>/intensity`, `/wet` | float | by processor id |
| `/fx/node/<id>/params/<key>` | float | a processor parameter by catalog key |
| `/fx/node/<id>/bypass`, `/solo` | 0/1 | switches |
| `/fx/transport/play`, `/record`, `/loop` | 0/1 | the workbench's own buttons |
| `/fx/selected` | string id | select a processor |
| `/fx/select/next`, `/fx/select/prev` | trigger | move the selection |
| `/fx/sync` | none | send the whole rack state again |

From the rack (TouchDesigner OSC In), sent when the link opens, on `/fx/sync`, and whenever something changes:

| Address | Argument |
|---|---|
| `/fx/count` | number of processors |
| `/fx/node/<id>/type` | processor type, string |
| `/fx/node/<id>/intensity`, `/wet`, `/params/<key>` | normalised float, 4 decimals |
| `/fx/node/<id>/bypass`, `/solo` | 0 or 1 |
| `/fx/selected`, `/fx/selected/type` | selected id and type, strings |
| `/fx/transport/play`, `/record`, `/loop` | 0 or 1 |
| `/fx/time` | playhead seconds, at most about 25 messages per second |
| `/fx/node/<id>/released/...`, `/event/...`, `/lane/...`, `/macro/...` | rule changes: see Rule sync below |

`<id>` and `<key>` are percent-encoded for anything outside `A-Z a-z 0-9 _ . -` (a space becomes `%20`, a slash `%2F`),
so no address segment holds a character OSC reserves. Unknown addresses, wrong argument types and ids that do not exist
are ignored; they never reach the rack and never close the link.

Echo: a value that only repeats what the rack just reported (within 0.0002 of the current position on the normalised
scale, the 4-decimal and float32 precision of the report) is not an edit, so wiring TouchDesigner's output to its own
input settles instead of looping, and an off-grid preset or automation value is not snapped onto the step grid by its own
report. Moves smaller than that are below the resolution of the report and are ignored.

## The TouchDesigner network (generated)

The rack has a TouchDesigner version that is generated from a patch, not drawn: the patch's processors, wiring and
parameter ranges become a network, and the OSC address contract above becomes a Python module inside it. No `.toe`
is needed to produce or review it; it is data and text.

```
node tools/fx-td-network.mjs web/fx/fixtures/closed-inbox.json --out outputs/td     # or: npm run td:network -- <patch> --out <dir>
```

This writes `<id>.network.json` (the spec: operators, parameters, wiring, contract), `<id>.build.py` (self-contained,
with the spec embedded) and `modules/` (the Python that ends up in Text DATs, for reading). Options: `--picture-fps` (12, the workbench's picture bank rate: the grid Feedback's reset epoch is latched on), `--source-file`,
`--word-cues` (a JSON list of word cues for Typography; none by default), `--picture-size` (`WxH`, the picture's proportions, which size Granular's tile bank; 16:9), `--osc-in-port` (7000), `--osc-out-port` (7001), `--host`, `--root-name` (`fx_rack`). Output is deterministic: the
same patch gives the same bytes.

In TouchDesigner's Textport:

```
exec(open("/path/to/closed-inbox.build.py", encoding="utf-8").read(), {})
exec(open("/path/to/closed-inbox.build.py", encoding="utf-8").read(), {"FX_PARENT": "/project1", "FX_SAVE_TOX": "/path/fx_rack.tox"})
```

The second form also saves the component as a `.tox`. Building again replaces the component of that name.

**What it builds.** One component, `fx_rack` (under `/project1` unless told otherwise):

| Part | What it is |
|---|---|
| `fx_<id>` for every patch node | A Base COMP per node. Processors carry the workbench's own controls as custom parameters, named from the catalog (`Intensity`, `Wet`, `Bypass`, `Solo`, then each processor parameter, for example `Taps`) with the catalog's range, step and default; integer parameters are Int. The original id is kept in the read-only `Nodeid`. Operator names are made legal (`prior-returns` becomes `fx_prior_returns`; collisions get `_2`), the id in OSC addresses is never changed. |
| inside a processor | Per port: a Select (`sel_<port>_<n>`, pointing at the upstream `out_<port>`), a gain operator where the wire has a gain, `in_<port>`, and `out_<port>`. Picture and audio also get a real wet/dry blend (`blend_*`, Cross TOP and Math CHOPs driven by `Wet`) and a bypass switch (`bypass_*`, driven by `Bypass`). The effect chain sits between `in_*` and the `proc_picture` / `proc_audio` Null (see Effects below); a processor without an implemented effect leaves it a plain pass-through. Each processor also holds a `resolved` Table DAT (see Resolved parameters below). |
| source, mix, output | The same ports; picture and audio take the node's `Gain`. The source reads `Sourcefile` (Movie File In, Audio File In); matte is full, text empty, control 0. Several wires meeting at a mix are combined (Add TOP, Math CHOP). |
| `osc_in`, `osc_out` | OSC In DAT on the bridge's `--td-port`, OSC Out DAT to the bridge's `--osc-port`. |
| `td_osc`, `td_contract` | The Python module and the contract JSON. `par_exec_nodes` and `par_exec_rack` (Parameter Execute DATs) turn edits into control messages; `osc_in_callbacks` hands received messages to the module. |
| `video_out`, `audio_out` | Stable taps on the output node. |

The rack component's own parameters: `Selected`, `Time`, `Count` and `Status` (linked / mismatch), transport
`Play`, `Record`, `Loop`, and pulses `Next`, `Prev`, `Sync`.

**How it speaks OSC.** The rack reports its state on the same addresses TouchDesigner uses to control it, so one
table serves both ways. A report sets the matching parameter (a normalised value is scaled onto the parameter's own
range without snapping it to the step, so an off-grid preset or automation value comes back unchanged; integer parameters
get the nearest integer); a parameter edit in TouchDesigner sends the normalised
value on that address. A value that only repeats what was last exchanged on that address (within 0.0002) is an echo
and is not sent. `Status` reads `linked: N processors` once the rack's report matches the network, and names any
type or count mismatch. The module has no address parsing at all: the contract lists every address, and an address
that is not in it is ignored.

**Resolved parameters (macros, automation, events, sidechain, Solo).** A custom parameter is a processor's *base* value.
What its blend, bypass (and the effects, as they are added) read is the **resolved** value in the processor's own
`resolved` Table DAT (rows `intensity`, `wet`, `bypass`, `solo`, `active`, then `param.<key>` for each catalog
parameter, and `epoch` for a processor with a Reset time, see Feedback below). An Execute DAT (`frame_exec`) calls `td_resolve` at the start of every frame, which reproduces the
workbench's `evaluatedNode` at the rack's playhead (`Time`, reported by the workbench on `/fx/time`):

1. Intensity and Wet: base, then the automation lane, then the patch's events, clamped to 0 to 1, then sidechain modulation.
2. The patch's macro mappings remap the processor parameters from the resolved Intensity (linear, ease-in, ease-out, smoothstep).
3. A direct automation lane or event on a parameter wins over its macro; sidechain modulation (add or multiply, by the
   level of the processor's `in_control` channel, clamped to 0 to 1) is applied last; integer parameters are rounded.
4. Solo is rack-scoped: when any processor is soloed (its Solo switch or a `solo` event), every other processor is
   inactive; a clip-scoped processor is inactive outside its range. Bypass is the processor's own Bypass (or `bypass`
   event) OR not active. The processor's bypass switch and wet/dry blend read these resolved rows.

The resolver (`tools/td-network/python/fx_td_resolve.py`) is pure Python and is checked against the workbench's own
`evaluatedNode`: `tests/test_td_resolve.py` compares every fixture patch at sampled times (every automation keyframe, event
and clip edge, plus or minus 10 ms) with sidechain levels 0, 0.37 and 1, and the same patches rewritten to use every macro
curve, step and linear lanes, add and multiply modulation, a clip scope and a Solo event (`--stress`). A change in the
resolver that moves any value fails there. The sidechain level is produced inside the network: the source component derives the same envelope as the workbench's
`amplitudeEnvelope` (the larger magnitude of the two source channels, sample by sample, 6 ms attack, 75 ms release, times 5, limited to 1) from its audio
and sends it down the control wires; the Lag CHOP's two values are attack and release as that operator defines them, and
its default unit is assumed to be seconds. The envelope is computed with a per-sample Python expression, which costs CPU at audio rates. Only the first channel is read.

**Rule sync (the workbench's automation, events and macros reach the network).** The rules in the contract (automation
lanes, recorded events, macro mappings) are the network's copy of the patch's rules, and the workbench keeps it
current. Evaluated values at the playhead cannot tell a hand edit from playing automation, so the rack reports rule
changes explicitly, after the control values, on addresses the contract lists for every control (`rules` in each
control entry):

| Address (from the rack) | Arguments | Meaning |
|---|---|---|
| `/fx/node/<id>/released/<path>` | none | the control's events, automation lane and macro mapping are gone (`<path>` is `intensity`, `wet`, `bypass`, `solo` or `params/<key>`) |
| `/fx/node/<id>/event/<path>` | time (s, 4 decimals), value (booleans as 0 / 1) | one recorded event: added, or its value replaced at that time |
| `/fx/node/<id>/lane/<path>` | one JSON string `{interpolation, keyframes}` | the control's automation lane, replacing any lane it had |
| `/fx/node/<id>/macro/params/<key>` | one JSON string `{min, max, curve}` | the processor parameter's macro mapping, replacing any mapping it had |

A release drops *all* of a control's rules, so when only some vanished (Clear removes events but not lanes, a preset
swaps a lane) the rack follows the release with the control's remaining rules. A processor the rack has not reported
since the link opened (the first report, a reconnect, `/fx/sync`) is resynchronised whole: every control released,
then every current rule sent, so a network generated from an older version of the patch converges on connect. After
that only differences are sent, so an unchanged rack is silent. The page computes the differences from the patch itself
(`web/fx/parameter-rules.mjs`, `processorRules`), the same code path the parameter setter uses (`applyParameterEdit`),
so recording, a hand edit, the automation form, Clear and a preset restore are all mirrored. In the network,
`td_osc` hands a rule message to `td_resolve.apply`, which edits the rules the next frame resolves from; a malformed
message is refused and changes nothing. A hand edit made in TouchDesigner changes nothing in the network's rules
by itself: the workbench applies it, decides what it does to the control's rules (it drops them, or, while recording,
adds an event and keeps the lane) and reports that, so the two cannot diverge. With **Send rack state back** off the
network hears none of this, so its copy of the rules is then the snapshot it was generated with.

`tests/test_td_rulesync.py` checks the whole path: `tools/td-network/oracle-rulesync.mjs` edits a stressed patch the way
the workbench does (a hand edit of an automated control, of a macro-driven parameter and of a recorded control; recording
with a merged re-edit; the automation form; Clear; a preset restore), collects the rack's real feedback after every
edit, and samples `evaluatedNode`; the generated Python module receives those messages from a network generated
before the patch had its rules, and its resolver must return the same values after every step.

Not mirrored: sidechain modulation and clip scope (set by editing the graph, not by a control edit; regenerate the network
after changing them), event times beyond 4 decimals (a sub-millisecond rounding), and a lane longer than 8,000
characters (about 220 keyframes), which would not fit one UDP datagram (macOS refuses more than 9,216 bytes): the control
is released in the network and stays so.
Editing a base parameter in TouchDesigner never sends the resolved value back over OSC: only edits made to the custom
parameters are exchanged.

**Effects (all thirteen have a picture chain).** Between `in_<port>` and `proc_<port>` a processor now holds the effect chain
(`tools/td-network/effects.mjs`). Every effect parameter is an expression over the processor's resolved values, so
macros, automation, events and sidechain drive it; the chain stays inside the processor, keeps the input's resolution,
and the network stays static (a count parameter is a fixed maximum of stages switched by its resolved value).

| Processor | Picture | Audio |
|---|---|---|
| Delay / Echo | Cache TOP with eight Cache Select TOPs at `n x Tap delay` frames back, Level TOP gain `Tap decay^n` while `n <= Echo taps`, summed with the input; the cache holds 240 frames (4 s at 60 fps) and a tap that would reach further back is switched off, not collapsed onto the oldest frame | eight Delay CHOPs at `n x Tap delay`, same gains, the delayed returns only (the processor's dry branch carries the direct signal, so a fully wet delay is silent before its first tap) |
| Freeze / Hold | for `Hold duration` seconds from `Capture time`: a Cache TOP stops taking frames and its newest frame is shown through a Switch TOP, faded by a Level TOP toward 0.2 by `Hold decay`, shaken sideways by a Transform TOP (`Micro motion`) | two Delay CHOPs at the age of two grains of `Voice hold grain` seconds started a half grain apart, Hann-weighted (`0.5 - 0.5 cos`) and complementary, decaying by `exp(-Hold decay x elapsed)` with a 6 ms onset and a `Release edge` tail; outside the hold the input passes. The first half grain of a hold reads the live signal for the second grain, where the workbench reads ahead |
| Gate / Erase | Level TOP opacity from the gate | Math CHOP gain from the gate: periodic (`Open fraction`, `Gate period`, `Gate phase` against the playhead) or sidechain threshold (with no control cable the gate reads zero and stays closed); `Edge time` smooths it with a Lag CHOP; `Closed-state residue` is its floor |
| Signal Damage | Level TOP stepping (`1 / Picture tone levels`), then Fit TOP down to the requested mosaic cells and back up to the input size, both nearest | drive (Math CHOP), sample-rate hold (Expression CHOP: a new sample every `sample rate / Audio sample-rate reduction` samples, taken where the workbench's hold phase reaches 1, within one sample), bit-depth quantise (Expression CHOP), bandwidth limit (Lag CHOP, `1 / (2 pi band)`), independent output level |
| Feedback | up to 16 returns, each a Cache Select tap `k x Feedback delay` back, a Transform TOP (`Scale per recurrence^k`, `Rotation per recurrence x k`, `extend` zero so the area outside the scaled picture is transparent), a Level TOP whose opacity is `Return gain^k`, and an Over TOP laying it over the picture so far (source-over, as the workbench draws it); return k exists while `k <= min(Maximum recurrences, floor((time - epoch) / delay))`, `Return gain^k >= Tail threshold`, and the tap is inside the 240-frame cache; `epoch` is the playhead time at which `Reset time` was last reached (0 before), a resolved row the resolver latches frame by frame as the workbench does, so a Reset time that tracks the playhead never cuts the returns off | none |
| Displacement | moving Noise TOP (`Field scale`, `Field movement`) pulling the picture through a Displace TOP along `Direction` | none |
| Reverse | a Cache TOP of the input read at the resolver's `depth` (see Latency below), then a Transform TOP sliding it by `shift` in route mode; mode, span, speed and pivot come from the workbench's own `reverseSourceTime` | only delayed by the processor's latency (the reversal itself needs a per-sample history reader, not built) |
| Stutter | three sources chosen by the resolver's `pick`: the input delayed by the latency, a Cache TOP that stops taking frames once the captured window has arrived (read at `depth`), and a transparent frame for the gap between repeats | only delayed by the processor's latency |
| Routing | up to eight trace copies over a transparent base, copy 0 on top: copy k reads `k x 0.085` s back from a Cache TOP, is scaled to 0.62, placed at the path position `clamp(progress - k x 0.12 x Trace spread)` between Origin and Destination (a triangle wave of period `2 x Travel`) and lifted by `0.3 sin(q pi)`, at opacity `(1 - k / (copies + 1)) x 0.95`; the copy count is the resolved `Trace copies` | the voice pans the mono sum of the shared recording along the same travel, equal power (`cos` / `sin` of the pan angle x sqrt 2): one Expression CHOP reads both source channels per sample, with the pan evaluated once per cook (block rate, as Freeze's voice envelope is). The text burst (`Impact scatter`) is not built |
| Exchange | the frame is cut into 5 strips (the workbench's fallback when the media pack names no people; the network cannot read the pack's regions) and dealt out again by a cyclic permutation: slot `d` shows strip `(d - ((floor(time / Exchange interval) x Address offset + 1) mod 5)) mod 5`, cut by a Crop TOP from a Cache TOP read `History offset` back, put in its slot by a Transform TOP on a full-size canvas (`tx` = the slot's centre) and laid side by side with Over TOPs; the picture changes at a hard cut, as the workbench's does. Mode 1 (voice addresses) passes the picture through a Switch TOP. Before `History offset` seconds of history exist the cache's oldest frame is shown, where the workbench clamps to the start of the media | none (the voice addresses need a per-sample history reader, not built) |
| Queue / Overflow | the workbench's own queue arithmetic per playhead second inside each `Burst interval`: arrivals `floor(phase x Arrival rate)`, released `floor(phase x Release rate x 0.2)`, waiting `max(0, arrivals - released)`, shown `min(Queue capacity, waiting)`. Up to 24 entries (the catalog's largest capacity), entry `j` a Cache Select read back the age of the arrival it shows (first in first out, or reversed for last in first out), a Transform TOP scaling it to 0.48 and placing and turning it as the engine's `translate`/`rotate` do (top-left corner at `(0.08 W + 0.025 W j, 0.15 H + 0.055 H j)` plus, once more is waiting than the capacity, a scatter of `Overflow spread x phase / period` with the engine's noise hash and the processor's seed (`patch.seed` plus the id's character codes) and a turn of up to 0.4 rad), a Level TOP switching entries beyond the count off, laid over each other with entry 0 at the bottom over a Level TOP that is the frame itself while nothing waits. The noise is the engine's 32-bit hash as a Python expression, equal bit for bit | none |
| Typography | the workbench's word (authored words step every 0.7 s and go blank once older than `Word persistence`; otherwise the latest checked cue that has started with probability above 0.5, `OUT` before any, blank `Word persistence` after its end) in a Table DAT `ty_words` (the patch's authored words, or the cues given with `--word-cues cues.json`: a list of `{in, out, text, probability}`; the media pack's own cues are private to it and are not read). Solid obstruction (mode 0): one Text TOP (Arial-style bold, `H x Canvas-relative word size` px, `#dbfb65`), up to 32 copies, copy 0 opaque and on top, the others at 0.18 shifted by `(sin(time x Word drift + k) x 0.16, cos(...) x 0.18)` of the frame, then for `Letter fragmentation` above 0.3 twelve Crop TOP strips of the word shifted by `sin(4k + floor(5 time)) x fragmentation x 0.045`; Inbox wall (mode 2): up to 32 left-aligned words, three per row, the row height giving the size, the word of copy `i` the one from `0.32 i` s earlier; Image aperture (mode 1): the picture multiplied by white text over a near-black frame. Modes 0 and 2 lay the words over the picture, mode 1 replaces it | none (visual only, as in the workbench) |
| Granular | the frame fractures into `Visual divisions` columns by the rows that gives at the frame's aspect (`max(2, round(columns x H / W))`), a bank of 24 columns by the rows the declared picture gives (336 at 16:9, 14 rows; `--picture-size WxH` sizes it for another aspect, for example 576 stages for a square picture), each redrawn every `Audio grain size` seconds: tile `i` is a Crop TOP of grain bin `i mod 5` (five Cache Selects), cut where the workbench cuts (the tile's own place, or with `Ordering` shuffled a seeded one), stretched across a full-size canvas by a Transform TOP whose scale returns it to `1.12 / columns` by `1.12 / rows`, centred where the engine's translate and rotate put its enlarged copy (a seeded `Spatial scatter` and turn), and switched off beyond the grid; tile 0 is drawn first and the last on top over a transparent base. Bin `k` shows source time `t + (noise - 0.5) x 2 x Source time scatter`, which is ahead of the playhead for half the grains, so Granular declares the catalog's largest scatter (2 s) as look-ahead latency, as Reverse does, and reads `latency - offset` seconds behind its input's live edge. The geometry and the engine's noise hash live in a Text DAT `gr_math` (read through `.module`), so each parameter expression is one call into it | none (the voice grains need a per-sample history reader, not built; the voice is delayed by the latency) |

**Latency (look-ahead buffers).** Reverse, Stutter and Granular read source time ahead of their playhead, which a live network cannot read.
Each is given a fixed, declared latency instead (`tools/td-network/latency.mjs`): the stream it receives runs `lookahead` seconds
ahead of the stream it emits, and it reads a Cache TOP of its input at depths up to that far ahead. The bounds come from the workbench's own
functions at the catalog's extremes, and `tests/test_td_latency.py` sweeps `reverseSourceTime` and `stutterSourceTime` to check that no read goes further
ahead than the bound and that the bound is reached: Reverse reads at most `span` ahead (5 s at the catalog maximum), Stutter at most `window x (1 - 1 / rate)` (1 s).

- **Capped.** The default cap is 2.5 s (`maxLatency` option of the generator): at 60 frames per second the Reverse cache holds 5 s, 302 frames, which is already
  gigabytes at 1080p. Every latency cache is sized by an expression of `project.cookRate` (the rate its read indices use), so it covers the delay at any cook rate, at a proportional memory cost. A Reverse whose Span the cap cannot cover has its resolved Span limited to the latency (`limits` in the resolve block, and a warning in the spec);
  Stutter's 1 s is under the cap. A Stutter whose bound the cap cannot cover reads the newest frame instead of one it has not got.
- **A path sum.** Every stream has a lag, the seconds its newest frame is behind the live edge (the source: 0). A processor without look-ahead keeps its inputs' lag;
  one with look-ahead adds its own. Where wires of different lag meet (a mix, a processor with several inputs) the shorter ones are delayed to the longest: picture through a Cache TOP read back,
  audio and control through a Delay CHOP. A processor with look-ahead delays its own dry branch by the same amount, so wet / dry and bypass stay aligned.
  Two Reverses in series therefore declare 5 s. The rack's latency is the lag of the output node, `contract.latency.seconds`, with each node's `lagIn`, `lookahead` and `lag` beside it.
- **Its own clock.** Everything a processor evaluates (automation, events, macros, the clip scope, Solo, Feedback's reset epoch, Gate's period, Displacement's field, Freeze's capture)
  reads the rack Time minus its output lag: the resolver evaluates at that time and writes it as the `clock` row, and the effects read `{clock}` where they would read the rack Time.
  Solo is judged at that clock too, as `evaluatedNode` asks. A rack with no look-ahead is unchanged: no `clock` row, no extra operators, byte-identical apart from the new `latency` field of the contract.
- **How the workbench compensates.** The output at rack Time T shows workbench time T minus the latency. When the rack reports its count (and on every `/fx/sync`) the network
  sends `/fx/latency <seconds>`; the workbench then reports `/fx/time` as the playhead plus that many seconds, immediately and from then on, and forgets it on reconnect.
  A latency outside 0 to 60 s is ignored. A rack with no latency sends 0, which clears what an earlier rack on the same link declared (a patch replaced without reconnecting).
- **Sidechain.** A look-ahead processor resolves at its own clock, so it reads the sidechain level delayed by the same look-ahead (`dry_control`), not the one arriving now.
- **What a processor does not target.** A signal a look-ahead processor does not process (the voice of a picture-only Reverse, the picture of a voice-only Stutter) is delayed by the same latency on its wet branch, so it leaves the processor as late as the one that is processed.
- **Stutter's frozen endpoint.** The repeat cache stops at the end of the captured window and holds what it held; the resolver remembers that endpoint (per processor) so a Window edited or automated during the hold does not move it. A repeat that reaches past what was captured shows the last captured frame.
- **What the resolver names.** For Reverse, `depth` (seconds behind the input's live edge) and `shift`; for Stutter, `pick`, `depth` and `record`. The tests compare them with the engine's source time over grids of
  every parameter's extremes (`tests/test_td_latency.py`), and the resolved values of every fixture with `evaluatedNode` at the processor's clock (`tests/test_td_resolve.py`).
- **Limits.** A seek reads a cache that was not filled in order (the same limit as Freeze, issue 151). The matte and text wires are not delayed by a look-ahead processor (a warning says so; the matte is constant in this network and text is empty). Reverse's route mode needs the same latency as playback mode because the mode is an editable parameter.
  Nothing here has been run in TouchDesigner; the operator and parameter names are checked against its reference (`check-vocabulary.mjs`), and the Cache Select `index` sign convention is the one Delay and Feedback already use.

Like the workbench, a processor builds its audio chain only when it targets `voice` and its picture chain only when it targets `picture`, `body`, `objects` or `room`; otherwise that signal passes through and the spec's `warnings` say so.

`tests/test_td_granular.py` records the engine's transform stack for Granular (each tile's source rectangle, drawn size, canvas centre, turn and the source time of its bin) over Visual divisions x Audio grain size x Source time scatter x Spatial scatter x Ordering x 4 times (more than 80,000 tiles, over 40,000 of them scattered and turned), compares them with the network's crop, scale, place, turn and cache-read expressions (the `gr_math` module is run as a Text DAT would run it), checks that the declared 2 s look-ahead covers every bin read and that the sweep reaches both ends of it, that a latency cap below 2 s limits `Source time scatter` and says so, that a square, portrait and landscape picture get banks of 576, 1,032 and 336 stages and the square one is compared tile by tile with the engine on a square canvas, and that the module's noise equals the engine's on 2,800 inputs.

`tests/test_td_typography.py` records the engine's words (`fillText` on a canvas that remembers its word, so every later `drawImage` of it carries the word, alpha and offset) for authored words and for supplied cues, over mode x size x copies x persistence x fragmentation x drift x 9 times, and compares each copy's word, opacity and offset, each fragment strip's crop edges and shift, each wall word's text, size and position and the aperture's word and size with the network's expressions (more than 20,000 comparisons). Glyph pixels are not compared: the Text TOP draws its own, and it does not squeeze a word wider than the frame as the workbench does. The captions text stream is not built.

`tests/test_td_queue.py` records the engine's transform stack for Queue / Overflow (each copy's size, centre on the canvas, turn and source time) over Queue capacity x Arrival rate x Release rate x Burst interval x Overflow spread x order x 9 times (more than 15,000 copy comparisons, over 2,000 of them scattered and turned by an overflow) and compares them with the network's expressions, plus the noise hash against the engine's text on 1,800 inputs. The text port's smaller copies are not built.

`tests/test_td_exchange.py` does the same for Exchange: every strip the engine deals (source rectangle, destination rectangle, the source time asked for) over a grid of Exchange interval x Address offset x History offset x mode x times, against the network's crop edges, slot positions and cache read-back (more than 5,000 strip comparisons).

`tests/test_td_routing_voice.py` runs the engine's `processAudioModule('routing')` over a deterministic stereo signal for every combination of Travel x Spread x Origin x Destination (and an off-catalog travel) and compares every channel of a grid of sample positions with the Expression CHOP's expression evaluated at that time (more than 10,000 samples, equal to the engine's 32-bit output within 2e-7); the pan really moves (the left-right difference takes more than four values over a travel) and the pair is equal power.

`tests/test_td_routing.py` runs the real `VisualEngine` on a recording canvas and compares each draw call of a Routing processor (position, size, opacity, the source time it asked for) with the network's expressions over a grid of every parameter's extremes and times. `tests/test_td_effects.py` evaluates every one of the other expressions (they are Python) against stand-ins for what they read,
at each parameter's minimum, default and maximum, and checks the arithmetic: tap gains and delays, the gate's duty
cycle, the bit-depth step grid, the mosaic cell counts, the displaced axis and the feedback transform.

`tests/test_td_epoch.py` runs the real `VisualEngine.feedbackEpoch` and the resolver's `EpochLatch` over the same sequences of times (ramped, crossing, stepped, constant and never-reached Reset time, with backward seeks): they agree. `tests/test_td_effects.py` also compares the network against the workbench's own engines: Freeze's hold window against
`freezeSourceTime` and its voice against `processAudioModule('freeze')` run over a ramp (each output sample then names the
source time it read, so the delays and grain weights are checked sample by sample), Feedback's return rule over a grid of
every parameter's extremes, and Damage's hold positions against the engine's hold phase.

Sample-wise expressions (Signal Damage's hold, the source's sidechain envelope) use the Expression CHOP's `me.sampleIndex`, `me.chanIndex` and `me.inputVal`, the members its class documents (an earlier `me.inputIndex` is not one; the test stand-in now has only the documented members) and read the channel they are evaluated for, so a stereo signal keeps its right channel.

**Known limit, Freeze / Hold voice:** the grain weights, decay and 6 ms onset are Math CHOP gains and the grain positions Delay CHOP delays, each evaluated once per cook from the playhead, not per audio sample. At a 60 Hz cook the 6 ms onset and the 25 ms minimum grain crossfade step instead of ramping, so the voice hold is a block-rate approximation of the workbench's sample-accurate overlap (the test checks the model at each tested timestamp, not the cook-rate stepping). Sample-rate envelopes need a CHOP-generated ramp chain that can only be verified in TouchDesigner.

**Known limits, stateful operations in a live network:** (1) Signal Damage's sample-rate hold is an Expression CHOP, which sees one time slice at a time: the hold phase and the held sample do not carry from one cook to the next, so a hold longer than a slice (reduction under about 3 kHz at a 60 Hz cook) restarts at each slice boundary; a faithful hold needs a stateful operator (a trigger at the reduced rate into a Hold CHOP). (2) Freeze / Hold's picture is the newest frame the Cache TOP held when it stopped taking frames, so the playhead moving into a hold from outside it (a seek) shows the cache's newest frame, not the frame at the capture time; the workbench re-samples the capture time. (3) The voice envelope is block-rate, above. All three are in issue 151.

**Scrubbing backward through Feedback's reset epoch.** `EpochLatch` keeps its state every 64 picture frames as the walk passes, so a backward seek replays at most 64 frames from the nearest saved state instead of the whole history; measured on a 2,000-frame walk, 200 backward single-frame seeks took 1.40 s before and 0.023 s after, with identical answers (`tests/test_td_epoch.py`).

**Not applied by this network:** every processor's picture chain is now built; what is not applied is named in the
spec's `warnings`: the voice of Granular, Reverse and Stutter (delayed only, see Latency);
Exchange's voice addresses; the audio of Feedback and Displacement; Signal Damage's picture `Signal drive`; Displacement's `Boundary spill`, audio warp and ring
blend; the source excerpt offset (`Offset`, read-only); and audio device output. Text wires carry no gain, and several
text wires at one mix read only the first. Menu tokens can be read from the sample projects inside the application: `toeexpand` on `Samples/Learn/PythonExamples.toe` (a copy; it writes beside its input) stores only non-default values and shows `outputresolution custom`, `fontsizexunit pixels`, `alignx left` / `right` and `aligny top` / `center`, so `custom` resolution is verified and the Text TOP tokens below are. The other menu values these chains set (`nearest` filtering,
`add` combining) are not in the application's parameter reference, which lists parameters but not menu tokens, so they stay
unverified until a TouchDesigner session builds the network. The Delay CHOP's and Lag CHOP's unit menus are left at their defaults, which the expressions assume to be seconds.

Parameter and operator names the network relies on are listed in one table, `tools/td-network/vocabulary.mjs`. The
spec carries a copy and the builder refuses a spec with a name outside it before creating anything. On a machine with
TouchDesigner installed, `node tools/td-network/check-vocabulary.mjs` checks every name in that table against the
parameter reference inside the application (read-only; it found and corrected `chop`, `const0name` and `const0value`).

### Try the contract without TouchDesigner

```
node tools/fx-td-network.mjs web/fx/fixtures/closed-inbox.json --out outputs/td
node tools/fx-td-bridge.mjs
python3 tools/td-network/headless_td.py outputs/td/closed-inbox.network.json      # the generated module behind a UDP socket
node tools/fx-td-simulator.mjs outputs/bridge-e2e.json --spec outputs/td/closed-inbox.network.json
```

`headless_td.py` runs the generated `td_osc` module for real (with its own small OSC codec, independent of the
bridge's) and answers JSON commands on stdin. The simulator accepts `--spec`: with it, its checks come from the
network's contract (every processor's Intensity and Bypass address, the transport, the rack's count and types)
instead of the fixed sequence. Without the flag it behaves as before. Nothing here replaces the original simulator.

## First in-app check for the owner

Not yet done. Do this once, in this order, and stop at the first step that fails; record what you saw (a screenshot of the
network and the Textport output is enough) before changing anything. Do not run `tools/fx-td-simulator.mjs` during this
check: it binds UDP 7000 (it stands in for TouchDesigner) and would collide with the real `osc_in`; keep it for the
no-TouchDesigner procedure above.

1. Prepare, without TouchDesigner (Node 22 or newer):
   `node tools/fx-td-network.mjs web/fx/fixtures/closed-inbox.json --out outputs/td --source-file /absolute/path/to/any-short-clip-with-audio.mp4`
   (without `--source-file`, `fx_source.Sourcefile` is empty and nothing flows, so there is no picture or audio to judge).
   Confirm `outputs/td/closed-inbox.build.py` and `closed-inbox.network.json` exist. For the workbench side, if `web/fx/assets` already holds your private media pack, leave it alone (the generator
   overwrites that folder without asking); otherwise run
   `node tools/make-synthetic-asset-pack.mjs web/fx/assets` and `python3 -m http.server 8765 --bind 127.0.0.1` from the
   repository root.
2. Open TouchDesigner and a new empty project. Open **Dialogs > Textport and DATs** (Alt+T).
3. In the Textport run
   `exec(open("/absolute/path/to/outputs/td/closed-inbox.build.py", encoding="utf-8").read(), {"FX_PARENT": "/project1"})`.
   Look for: no Python traceback, and a component named `fx_rack` appearing in `/project1`. A traceback names the operator or
   parameter that does not exist in your build: copy it verbatim.
4. Open `fx_rack`. Look for: one `fx_<id>` Base COMP per processor in the patch, `osc_in` and `osc_out` DATs, `td_osc` and
   `td_contract` Text DATs, and `video_out` / `audio_out` Nulls. Check no operator is red (error) or yellow (warning),
   and that `fx_source` shows the clip on its picture output.
5. Confirm the menu tokens the network sets but that the parameter reference does not list (the only values not verified
   against the application). In this `closed-inbox` network: `chopop` = `add` on `mixin_*` and `blend_audio`. The remaining
   tokens live in other fixtures, so build each as a separate component, one at a time (rerun step 3 with the
   `--out outputs/td-<name>` build file; a build replaces a component of the same name, so set `FX_PARENT` to a fresh
   container or rename with `--root-name`): `web/fx/fixtures/demo-damage.json` for `inputfiltertype` = `nearest` on
   `dmg_small` and `dmg_full` (Fit TOPs, `tools/td-network/effects.mjs` lines 46 to 51); `demo-delay.json` for `chopop` =
   `add` on `dly_sum`; `demo-freeze.json` for `chopop` = `add` on `fz_sum`. Also confirm the Delay and Lag CHOPs' unit menus
   read seconds by default (the expressions assume seconds). While there, glance at every other string menu value `tools/td-network/effects.mjs` sets (for example `extend` = `zero`) for a parameter error. A wrong token shows as a parameter error on that operator;
   note the token the menu actually offers.
6. Start the bridge in a terminal: `node tools/fx-td-bridge.mjs`. In a browser open
   `http://127.0.0.1:8765/web/fx/index.html?patch=closed-inbox` (a fresh page otherwise loads `message-impact`, whose processors differ from the network's), then **Control > TouchDesigner (OSC) > Connect bridge**. The bridge prints
   only connection lines (`[fx-bridge]`), not OSC messages. In TouchDesigner, `osc_in` should listen on 7000 and
   `osc_out` target 7001 (see their parameters).
7. Rack to TouchDesigner: the page reports its state on connect. Look for: rows arriving in the `osc_in` DAT
   (`/fx/count`, `/fx/node/<id>/type`, ...). Pulse the rack's `Sync` parameter to ask for the state again (it sends
   `/fx/sync` through `osc_out` to the bridge; do not send `/fx/sync` to port 7000, which ignores it). The rack's
   `Status` should read `linked: N processors`, not `mismatch`.
8. TouchDesigner to rack: edit a processor's `Intensity` custom parameter. Look for: the matching knob in the workbench's
   Control tab moving to the same normalised position (that is the observable result; the bridge does not log it). Then set
   a processor's `Bypass` to 1 and look for the workbench's Bypass switch turning on. Look also for: the rack's `Time`
   following the workbench playhead, the picture and audio at `video_out` / `audio_out` changing when Intensity, Wet and
   Bypass change, and no feedback loop (a parameter wired to its own report must settle, not oscillate).
9. Report back which step first failed, or that all passed. Until then every TouchDesigner claim in this repository is
   unverified.

## Verification status

Automated, in `tests/fx` (run by `tools/check.py`): the OSC codec including the specification's own byte example and
every malformed-packet path; the address interpreter and the feedback differ; the page-side session against a fake
WebSocket (reconnect back-off, malformed frames, feedback on and off); and the bridge itself on real sockets, with a UDP
socket standing in for TouchDesigner. One test runs the page-side session and the real bridge together and checks the
whole loop: state out on connect, a knob moved over OSC, the rack's new value reported back, and `/fx/sync`.

The generated network is covered by `tests/fx/td-network.test.mjs` and `tests/test_td_network.py`: every factory, demo and
single-processor patch yields a spec that passes the structural validator (legal names, sibling wiring by family,
selects that point at the right upstream port, expressions that use defined parameters, the contract's ranges against the
catalog, unique addresses) and is byte-identical across runs; every address the workbench reports has a receive entry
and every address the network can send is accepted by the workbench's own interpreter, including the selected-slot
aliases; a report from the rack sets the Python module's parameters to the rack's values and an edit becomes the message
the workbench applies; the builder runs against a fake operator tree that only offers the parameters the vocabulary
lists; and one test runs a workbench session, the real bridge and the generated module over real UDP and WebSocket
connections.

**Why it was not built in TouchDesigner here (observed 2026-11-14, macOS, TouchDesigner.app arm64).** The bundle's
`Contents/MacOS` holds `TouchDesigner`, `TouchEngine`, `toeexpand` and `toecollapse`. `toeexpand` and `toecollapse` only
convert a `.toe` or `.tox` to and from a directory (their usage text lists no script or run option). `TouchDesigner -h`
printed nothing and did not exit (it was stopped by a 10 s timeout), so the binary shows no command-line interface; no
`-nowindow` or script flag appears in a read of its strings. `TouchEngine` exited 0 with no output when run bare; it
is the player behind `libTouchEngine.dylib`, which a host application loads to run a `.tox`, and no host is installed
or built here. There is no `.toe` on the machine to carry a start-up script. A run would therefore need the full
application window, which this lane does not open. What does run here is the parameter reference inside the
application (`check-vocabulary.mjs`, exit 0 against it), the generated module under CPython, and the builder against a
fake operator tree.

**Not verified:** a real TouchDesigner session (the generated network has never been built in TouchDesigner: the
operator and parameter names are checked against TouchDesigner's own parameter reference, but the network, the `.module`
access pattern and the Parameter Execute behaviour have not been run); the Control tab's new section in a browser against the private media
pack (the surface was checked with a stand-in rack only); a physical controller. The bridge's WebSocket server is a
small in-repo implementation of the RFC 6455 text subset (no extensions, no binary frames); it is tested against Node's
own WebSocket client but not against TouchDesigner's.
