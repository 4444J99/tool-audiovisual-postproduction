# Shot plan, ledger and animatic

`amp/studies/shot_plan.py`, `amp/studies/shot_animatic.py` and `tools/shot_plan.py` implement the camera-coverage stage of a short production pilot: one set, a few shots, one edit. A plan is checked before any heavy render starts; a plan that fails any rule prints every failure and writes nothing.

## Plan format (`amps.shot_plan.v1`)

Metres; x right, z forward, y up; plan view uses x and z. Frame numbers are integers on the scene timeline, the clock the caches are baked on.

| Field | Meaning |
|---|---|
| `set.bounds`, `set.elements` | Walkable extent and named elements (`center`, `radius`, `added`). |
| `set.caches` | Baked physical caches with their finite `frames` range (for example fire or water). |
| `performers` | Plan-view positions used for framing and the axis of action. |
| `shots[]` | Stable `id`, `camera` (name, `lens_mm`, `position`, `aim`), `frames` (`start`, `end`, `handle_in`, `handle_out`), `performers`, `axis`, `uses`, `cache_strategy`, `render`, `receipt`. |

`uses` entries carry a `contribution`: `visible`, `reflection`, `shadow`, `occlusion`, `effect` or `offscreen_action`, so things that matter outside the frame are declared rather than silently dropped.

## Rules checked

- Shot IDs are unique and match `^[A-Z][A-Za-z0-9_-]{0,23}$`; frame ranges are positive; handles are non-negative.
- The camera is inside the set bounds, has a horizontal aim and a lens from 8 to 400 mm.
- Every listed performer lies inside the horizontal field of view; the outer 10 percent is a warning.
- Consecutive shots on the same performer pair stay on one side of the axis of action unless the later shot sets `axis.crossing` to a reason; a camera on the axis is refused.
- An element used as `visible` must be inside the frame; an element marked `added` must be used by some shot.
- A shot that uses a cache must fit inside the baked range including handles. Otherwise it names `cache_strategy` as `rebake` (with a covering `frames` range) or `reviewed_loop` (with `reviewed_by`). An unreviewed loop is refused.
- A receipt may say `rendered` only with `frames_rendered` equal to the frames with handles and a SHA-256 of the output.

## Outputs

`shot-ledger.json` (per-shot scene range, render range with handles, zero-based edit in/out, duration, receipt, plan hash), `edit.edl`, `cards/<ID>.png` (plan view with camera frustum, performers, elements and shot data), `contact-sheet.png`, and with `--video` an `animatic.mp4` that holds each card for its shot duration. Output bytes depend only on the plan.

```bash
python tools/shot_plan.py --demo outputs/shot-plan-demo --video   # synthetic round-table set, no media
python tools/shot_plan.py --plan plan.json --validate-only
python tools/shot_plan.py --plan plan.json --output outputs/shot-plan
```

The demo uses an invented set and invented performers. Real plans belong with the set they describe; this repository carries only the tool and synthetic fixtures. A passing plan fixes camera coverage and bookkeeping, not artistic approval of the scene.
