# Set spec to Blender scene

`amp/studies/set_spec.py`, `tools/set_scene.py` and `tools/blender_set_scene.py` turn a written set specification into a Blender environment, headless, and check that Blender holds what the spec says.

## Versions and negotiation

| Schema | Vocabulary |
|---|---|
| `amps.set_spec.v1` | `box`, vertical `cylinder`, `arch_bands`. |
| `amps.set_spec.v1.1` | everything in v1, plus `axis` on cylinders and the kinds `cone`, `sphere`, `torus`, `stairs`. |

v1.1 is a strict superset. A v1 spec is also a valid v1.1 spec and builds the same scene graph (same `graph_sha256`; a test pins it), so nothing already written has to change. The declared `schema` is checked against what the content needs: a spec that says v1 but uses a v1.1 primitive or `axis` is refused, with every offending element named and the schema it needs, and an unknown schema is refused with the supported list. `python tools/set_scene.py validate` prints both `schema` and `required_schema`; `python tools/set_scene.py upgrade --spec v1.json out.json` rewrites a valid spec's declaration to v1.1.

## Spec format (`amps.set_spec.v1`, plus the v1.1 additions marked)

Metres; x right, y up, z forward (depth into the set), the same convention as the shot plan. Blender is Z up, so a spec point `(x, y, z)` becomes Blender `(x, z, y)`.

| Field | Meaning |
|---|---|
| `bounds` | `x` and `z` ranges (min < max) and a positive `height`. |
| `materials` | name to `{color: "RRGGBB", roughness, emission}`; colours are sRGB hex and are converted to linear for Blender. |
| `floor` | optional `{material}`; a thin slab covering the bounds. |
| `elements[]` | `box` (`size` w, h, d), `cylinder` (`radius`, `height`) or `arch_bands`; each has `id`, `center` [x, z], optional `y` base, `rotation_deg`, `role` (`architecture` or `dressing`), `added`. The v1.1 kinds are in the next table. |
| `arch_bands` | `outer_radius`, `depth`, `step_depth`, `segments`, and `bands[]` of `name`, `color` (hex or material), `width`. Bands nest inward from the outer radius; each band's front recedes by `step_depth` while the backs stay flush, and its inner face (soffit) gets a darker derived material. |
| `lights[]` | `point`, `sun` or `area` with `position`, `energy`, `color`, and `aim` for sun and area. |
| `cameras[]`, `active_camera` | `position`, `aim`, `lens_mm` (8 to 400), `sensor_width_mm`. |

### v1.1 primitives

`y` is always the underside of the element's bounding box, so a lying cylinder with `y` 0.9 and radius 0.12 spans 0.9 to 1.14 m. `rotation_deg` turns the element about the vertical through `center` after its axis is applied. Footprint and height checks use the element's real extent for its axis and turn.

| Kind | Fields | Notes |
|---|---|---|
| `cylinder` + `axis` | `axis` `"x"`, `"y"` (default) or `"z"` | `height` is the length along the axis. `x` lies across the set, `z` runs into the depth, `y` is the v1 upright cylinder. |
| `cone` | `radius` (base), `top_radius` (default 0, a point), `height`, `axis`, `segments` | A frustum when `top_radius` > 0; an inverted frustum is allowed. |
| `sphere` | `radius`, `segments` (around, default 48), `rings` (latitude bands, default 16) | Rests on `y`. |
| `torus` | `major_radius`, `minor_radius` (smaller), `axis` = the ring's hole axis, `segments` (around the ring), `tube_segments` (default 16) | `axis` `"y"` is a ring lying flat, `"z"` or `"x"` a ring standing up. |
| `stairs` | `size` [w, h, d], `steps` (2 to 200) | A solid flight of equal treads rising toward +z (depth); `rotation_deg` turns it. |

`python tools/set_scene.py demo-spec out/spec.json --version 1.1` writes the synthetic hall plus one of each.

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

`build` finds Blender through `$AMP_BLENDER`, `blender` on `PATH`, or the macOS application, runs it with `-b --factory-startup`, and fails (exit 2) if Blender's measured mesh, light and camera counts, vertex and face counts, object dimensions, material lists or active camera differ from the scene graph. For every mesh it also compares the world bounds (so an axis, a turn or a location applied wrongly is caught even when local dimensions agree), the signed volume (a primitive wound inside out reads negative) and that no edge is open. The report keeps the expected and observed halves side by side.

## What the tests assert

`tests/studies/test_set_spec.py` is pure Python: validation rules, the deterministic graph hash, the axis swap, arch band nesting and flush backs, closed meshes (every half-edge has its opposite), and that `shot_plan_set` produces a set block the shot plan validator accepts, including its rule that an added element must serve a shot.

The v1.1 tests cover version negotiation (v1 hash pinned, v1 declaring v1.1 content refused, unknown schema, upgrade), shape rules for every new kind, closed outward-wound meshes resting on their base, analytic volumes (cylinder polygon area, frustum, sphere, torus, stairs), world bounds under axis and rotation, and the shot plan accepting the new kinds.

`tests/test_set_scene_blender.py` runs Blender headless when it is installed (the skip is visible otherwise): object counts, collections, dimensions, per-face material histograms, camera lens and forward vector, light type and energy, a saved `.blend` reopened in a fresh Blender process, and a tiny still render.

A passing build fixes geometry and bookkeeping, not artistic approval of the set. Real set specs and their renders belong with the set they describe; this repository carries only the tool and a synthetic fixture.
