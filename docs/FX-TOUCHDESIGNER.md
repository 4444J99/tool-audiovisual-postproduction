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
| inside a processor | Per port: a Select (`sel_<port>_<n>`, pointing at the upstream `out_<port>`), a gain operator where the wire has a gain, `in_<port>`, and `out_<port>`. Picture and audio also get a real wet/dry blend (`blend_*`, Cross TOP and Math CHOPs driven by `Wet`) and a bypass switch (`bypass_*`, driven by `Bypass`). The effect itself belongs in the empty `proc_picture` / `proc_audio` Null between `in_*` and the blend. |
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

**Not applied by this network:** the effects themselves (a freshly built rack passes signal through unchanged), the
patch's automation events and macro mappings, sidechain modulation (the control wire is routed to `in_control` and the
mapping is recorded in the contract), Solo (mirrored as state only), the source excerpt offset (`Offset`, read-only),
and audio device output. Text wires carry no gain, and several text wires at one mix read only the first.

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

**Not verified:** a real TouchDesigner session (the generated network has never been built in TouchDesigner: the
operator and parameter names are checked against TouchDesigner's own parameter reference, but the network, the `.module`
access pattern and the Parameter Execute behaviour have not been run); the Control tab's new section in a browser against the private media
pack (the surface was checked with a stand-in rack only); a physical controller. The bridge's WebSocket server is a
small in-repo implementation of the RFC 6455 text subset (no extensions, no binary frames); it is tested against Node's
own WebSocket client but not against TouchDesigner's.
