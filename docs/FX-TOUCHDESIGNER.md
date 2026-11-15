# FX rack and TouchDesigner (OSC bridge)

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

Open `http://127.0.0.1:8765/web/fx/index.html`, then **Control > TouchDesigner (OSC) > Connect bridge**. The simulator
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
with the spec embedded) and `modules/` (the Python that ends up in Text DATs, for reading). Options: `--source-file`,
`--osc-in-port` (7000), `--osc-out-port` (7001), `--host`, `--root-name` (`fx_rack`). Output is deterministic: the
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
parameter). An Execute DAT (`frame_exec`) calls `td_resolve` at the start of every frame, which reproduces the
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

The rules in the contract (automation, events, macros) are a snapshot of the patch the network was generated from. Editing
a control by hand in TouchDesigner removes that control's rules in the network exactly as the workbench's `setParameter`
removes them, because the edit is sent to the rack, which does it there. The reverse is not mirrored: editing an automated
control in the workbench, or recording events while the network runs, does not change the network's rules, so regenerate
the network after changing a patch's rules (the rack reports evaluated values at the playhead, which cannot tell a hand edit
from playing automation). Editing a base parameter in TouchDesigner never sends the resolved value back
over OSC: only edits made to the custom parameters are exchanged.

**Effects (six of thirteen).** Between `in_<port>` and `proc_<port>` a processor now holds the effect chain
(`tools/td-network/effects.mjs`). Every effect parameter is an expression over the processor's resolved values, so
macros, automation, events and sidechain drive it; the chain stays inside the processor, keeps the input's resolution,
and the network stays static (a count parameter is a fixed maximum of stages switched by its resolved value).

| Processor | Picture | Audio |
|---|---|---|
| Delay / Echo | Cache TOP with eight Cache Select TOPs at `n x Tap delay` frames back, Level TOP gain `Tap decay^n` while `n <= Echo taps`, summed with the input; the cache holds 240 frames (4 s at 60 fps) and a tap that would reach further back is switched off, not collapsed onto the oldest frame | eight Delay CHOPs at `n x Tap delay`, same gains, the delayed returns only (the processor's dry branch carries the direct signal, so a fully wet delay is silent before its first tap) |
| Freeze / Hold | for `Hold duration` seconds from `Capture time`: a Cache TOP stops taking frames and its newest frame is shown through a Switch TOP, faded by a Level TOP toward 0.2 by `Hold decay`, shaken sideways by a Transform TOP (`Micro motion`) | two Delay CHOPs at the age of two grains of `Voice hold grain` seconds started a half grain apart, Hann-weighted (`0.5 - 0.5 cos`) and complementary, decaying by `exp(-Hold decay x elapsed)` with a 6 ms onset and a `Release edge` tail; outside the hold the input passes. The first half grain of a hold reads the live signal for the second grain, where the workbench reads ahead |
| Gate / Erase | Level TOP opacity from the gate | Math CHOP gain from the gate: periodic (`Open fraction`, `Gate period`, `Gate phase` against the playhead) or sidechain threshold (with no control cable the gate reads zero and stays closed); `Edge time` smooths it with a Lag CHOP; `Closed-state residue` is its floor |
| Signal Damage | Level TOP stepping (`1 / Picture tone levels`), then Fit TOP down to the requested mosaic cells and back up to the input size, both nearest | drive (Math CHOP), sample-rate hold (Expression CHOP: a new sample every `sample rate / Audio sample-rate reduction` samples, taken where the workbench's hold phase reaches 1, within one sample), bit-depth quantise (Expression CHOP), bandwidth limit (Lag CHOP, `1 / (2 pi band)`), independent output level |
| Feedback | up to 16 returns, each a Cache Select tap `k x Feedback delay` back, a Transform TOP (`Scale per recurrence^k`, `Rotation per recurrence x k`) and a Cross TOP stacking it on the previous return at `Return gain^k`; return k exists while `k <= min(Maximum recurrences, floor((time - epoch) / delay))`, `Return gain^k >= Tail threshold`, and the tap is inside the 240-frame cache; `epoch` is `Reset time` once the playhead has reached it, else 0 | none |
| Displacement | moving Noise TOP (`Field scale`, `Field movement`) pulling the picture through a Displace TOP along `Direction` | none |

Like the workbench, a processor builds its audio chain only when it targets `voice` and its picture chain only when it targets `picture`, `body`, `objects` or `room`; otherwise that signal passes through and the spec's `warnings` say so.

`tests/test_td_effects.py` evaluates every one of these expressions (they are Python) against stand-ins for what they read,
at each parameter's minimum, default and maximum, and checks the arithmetic: tap gains and delays, the gate's duty
cycle, the bit-depth step grid, the mosaic cell counts, the displaced axis and the feedback transform.

`tests/test_td_effects.py` also compares the network against the workbench's own engines: Freeze's hold window against
`freezeSourceTime` and its voice against `processAudioModule('freeze')` run over a ramp (each output sample then names the
source time it read, so the delays and grain weights are checked sample by sample), Feedback's return rule over a grid of
every parameter's extremes, and Damage's hold positions against the engine's hold phase.

**Not applied by this network:** the effects of the other seven processors (Routing, Stutter, Granular, Reverse,
Queue / Overflow, Exchange, Typography), which pass signal through unchanged and are named in the
spec's `warnings` (Reverse and part of Stutter read source time ahead of the playhead, which a live network cannot);
the audio of Feedback and Displacement; Signal Damage's picture `Signal drive`; Displacement's `Boundary spill`, audio warp and ring
blend; the source excerpt offset (`Offset`, read-only); and audio device output. Text wires carry no gain, and several
text wires at one mix read only the first. The menu values these chains set (`custom` resolution, `nearest` filtering,
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
