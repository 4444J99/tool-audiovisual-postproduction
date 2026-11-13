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

## TouchDesigner side

Nothing here needs a custom component. **Not yet exercised against a running TouchDesigner** (see Verification); the
operator settings below are the standard ones and should be confirmed when first wired.

- **Receive the rack's state:** an *OSC In DAT* (it carries strings as well as numbers) on port 7000, or an *OSC In CHOP*
  for the numeric addresses only. Match the port to `--td-port`.
- **Control the rack:** an *OSC Out CHOP* (or *OSC Out DAT*) to `127.0.0.1` port 7001, matching `--osc-port`. With the CHOP,
  name each channel after its address, for example `fx/selected/knob/0`.
- **One client per role.** The bridge relays between WebSocket clients and UDP only; it never passes a message from one
  WebSocket client to another. A WebSocket DAT connected to the bridge is treated as another workbench: whatever it sends
  leaves as OSC to TouchDesigner's own OSC In port. Use OSC In/Out for TouchDesigner.

## Verification status

Automated, in `tests/fx` (run by `tools/check.py`): the OSC codec including the specification's own byte example and
every malformed-packet path; the address interpreter and the feedback differ; the page-side session against a fake
WebSocket (reconnect back-off, malformed frames, feedback on and off); and the bridge itself on real sockets, with a UDP
socket standing in for TouchDesigner. One test runs the page-side session and the real bridge together and checks the
whole loop: state out on connect, a knob moved over OSC, the rack's new value reported back, and `/fx/sync`.

**Not verified:** a real TouchDesigner session; the Control tab's new section in a browser against the private media
pack (the surface was checked with a stand-in rack only); a physical controller. The bridge's WebSocket server is a
small in-repo implementation of the RFC 6455 text subset (no extensions, no binary frames); it is tested against Node's
own WebSocket client but not against TouchDesigner's.
