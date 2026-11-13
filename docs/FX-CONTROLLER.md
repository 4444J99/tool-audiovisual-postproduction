# FX rack controller surface

The workbench's **Control** tab is a touch and MIDI control surface for the FX rack, in the manner of a
DAW's device strip. It drives whichever processor is selected, so one set of hardware controls
operates the whole rack.

## What it provides

- **Faders** for Intensity and Wet / dry; **knobs** for every processor parameter; **Bypass** and **Solo** switches;
  **Play**, **Record** and **Loop** transport switches. Previous and next buttons move the selection.
- Touch and mouse: drag a knob vertically (hold Shift for fine control), drag or tap a fader, tap a switch,
  double-click a knob or fader to restore its default. Keyboard: arrows, Page Up/Down, Home and End on a focused control.
- Hardware MIDI through the Web MIDI API (control change, notes, pitch bend). System-exclusive access is never requested.
  Chromium-family browsers support it; elsewhere the touch controls still work and the tab says so.

## Mapping hardware

1. Choose **Connect MIDI** and allow access. Controllers plugged in later are picked up automatically.
2. **Load template**: CC 20–27 (any channel) drive the first eight controls of the selected processor, in the order
   Intensity, Wet / dry, then the processor's parameters. Notes 36 and 37 switch Bypass and Solo; notes 38, 39 and 40
   switch Play, Record and Loop.
3. Or use **MIDI learn**: turn it on, tap a control on screen (it is outlined), then move a knob, fader or press a key
   on the controller. One binding exists per hardware source and per target; binding again replaces the earlier one.

Bindings follow the selected processor by slot (`knob:N`, `switch:N`) unless they were learned for a specific
processor, which is shown as "fixed". Switch bindings learned from a note toggle; those learned from a control change
follow the button (on at 64 or above). Relative encoders (two's complement: 1–63 up, 65–127 down) are supported by
binding mode `relative` in an imported map.

**Soft takeover.** An absolute hardware control is ignored until it reaches or crosses the on-screen value, so a physical
knob that disagrees with the rack never makes the value jump. The control shows "Move hardware to N% to take over".
Takeover restarts when the selection or patch changes, after a touch edit, or whenever the value is changed from elsewhere (a rack slider, preset, undo or automation) after the knob last wrote it. Relative encoders accumulate fractional ticks, so one-tick turns still move coarse stepped parameters.

## Behaviour inside the rack

Every processor change (faders, knobs, Bypass, Solo), from touch or MIDI, goes through the workbench's own parameter
setter, so it is recorded when **Record controls** is on, remembered as the processor's custom setting, and rendered like
a mouse edit. One undo entry covers a whole gesture: a touch drag for as long as the pointer is down, or a stream of
MIDI messages with pauses under 600 ms; never one entry per message. Play, Record and Loop are transport switches: they
click the workbench's own buttons, are not recorded as processor changes and have no undo entry. Hardware play requests
may be refused by the browser's audio policy until the page has had a click or key press.

## Persistence and portability

Mappings are a per-user device convenience: they are kept in the browser's local storage (best effort) and are **not**
part of a portable patch. **Export map** and **Import map** move them as `fx.controller-map.v1` JSON. Imports are strictly
validated: version, at most 128 bindings, known source types, channel 0–16 and number 0–127, parameter paths of a valid form (a path for a parameter the processor lacks is accepted and simply does nothing),
modes that match the target type, and a 200 KB size limit. An invalid file changes nothing.

## Source

`web/fx/controller-map.mjs` is the pure core (MIDI parsing, binding, learn, scaling against the catalog's own range
and step, takeover, gestures). `web/fx/controller-surface.mjs` is the interface and Web MIDI wiring, and
`web/fx/controller.css` styles it. Tests are `tests/fx/controller-map.test.mjs` and `tests/fx/controller-surface.test.mjs`.
The processing engines and preview signatures are untouched.

## Verification status

Unit tests cover parsing, scaling, binding, learn, takeover, relative encoders, switches, selection following, import
validation, gesture grouping and Web MIDI wiring against a fake MIDI access object. The surface itself was exercised in a
browser with a synthetic rack (drag, keyboard, double-click, template, learn, takeover, mobile width). It has **not** been
run inside the full workbench against the private media pack, and no physical controller was attached.
