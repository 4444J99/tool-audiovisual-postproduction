# Set spec to Blender scene

`amp/studies/set_spec.py`, `tools/set_scene.py` and `tools/blender_set_scene.py` turn a written set specification into a Blender environment, headless, and check that Blender holds what the spec says.

## Spec format (`amps.set_spec.v1`)

Metres; x right, y up, z forward (depth into the set), the same convention as the shot plan. Blender is Z up, so a spec point `(x, y, z)` becomes Blender `(x, z, y)`.

| Field | Meaning |
|---|---|
| `bounds` | `x` and `z` ranges (min < max) and a positive `height`. |
| `materials` | name to `{color: "RRGGBB", roughness, emission}`; colours are sRGB hex and are converted to linear for Blender. |
| `floor` | optional `{material}`; a thin slab covering the bounds. |
| `elements[]` | `box` (`size` w, h, d), `cylinder` (`radius`, `height`) or `arch_bands`; each has `id`, `center` [x, z], optional `y` base, `rotation_deg`, `role` (`architecture` or `dressing`), `added`. |
| `arch_bands` | `outer_radius`, `depth`, `step_depth`, `segments`, and `bands[]` of `name`, `color` (hex or material), `width`. Bands nest inward from the outer radius; each band's front recedes by `step_depth` while the backs stay flush, and its inner face (soffit) gets a darker derived material. |
| `lights[]` | `point`, `sun` or `area` with `position`, `energy`, `color`, and `aim` for sun and area. |
| `cameras[]`, `active_camera` | `position`, `aim`, `lens_mm` (8 to 400), `sensor_width_mm`. |

Checked before anything is built, every failure reported together: unique ids (and `FLOOR` reserved), defined materials, footprints and heights inside the bounds (rotated boxes use their rotated extent), band widths that leave an opening, depths that survive every reveal, object names inside Blender's 63 character limit, horizontal camera aims, a defined active camera.

## Commands

```bash
python tools/set_scene.py demo-spec out/spec.json        # invented archway hall, no private source
python tools/set_scene.py validate --spec out/spec.json
python tools/set_scene.py graph --spec out/spec.json     # counts, extents, graph hash; no Blender
python tools/set_scene.py shot-set --spec out/spec.json  # the `set` block for tools/shot_plan.py
python tools/set_scene.py build --spec out/spec.json --blend out/scene.blend --report out/report.json \
    --still out/still.png --width 320 --height 180 --samples 16
```

`build` finds Blender through `$AMP_BLENDER`, `blender` on `PATH`, or the macOS application, runs it with `-b --factory-startup`, and fails (exit 2) if Blender's measured mesh, light and camera counts, vertex and face counts, object dimensions, material lists or active camera differ from the scene graph. The report keeps the expected and observed halves side by side.

## What the tests assert

`tests/studies/test_set_spec.py` is pure Python: validation rules, the deterministic graph hash, the axis swap, arch band nesting and flush backs, closed meshes (every half-edge has its opposite), and that `shot_plan_set` produces a set block the shot plan validator accepts, including its rule that an added element must serve a shot.

`tests/test_set_scene_blender.py` runs Blender headless when it is installed (the skip is visible otherwise): object counts, collections, dimensions, per-face material histograms, camera lens and forward vector, light type and energy, a saved `.blend` reopened in a fresh Blender process, and a tiny still render.

A passing build fixes geometry and bookkeeping, not artistic approval of the set. Real set specs and their renders belong with the set they describe; this repository carries only the tool and a synthetic fixture.
