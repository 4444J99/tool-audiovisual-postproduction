# Actor-coverage matrix

`amp/studies/coverage_matrix.py` and `tools/coverage_matrix.py` check a shot plan ([guide](SHOT-PLAN.md)) against what the edit will need from each actor, and propose shots for the gaps. Everything is derived from the plan's own geometry; no media is read.

## Inputs

The shot plan (`amps.shot_plan.v1`). Its performers may carry `facing_deg` (0 = +z, clockwise) or `facing_toward` (another performer's id); the shot-plan validator ignores both, so existing plans stay valid. A requirement file (`amps.coverage_matrix.v1`):

| Field | Meaning |
|---|---|
| `required[]` | `actor` and any of `size`, `angle`, `ots`, `min_shots` (default 1). Omitted fields match anything. |
| `lines[]` | Dialogue `id`, `actor`, scene `frames` `[first, last]`, optional `offscreen_ok`. |
| `room_tone[]`, `room_tone_min_s` | Recorded room-tone seconds per `set` id; each set needs at least `room_tone_min_s` (default 30 s), the material `tools/room_tone_fill.py` borrows from ([guide](ROOM-TONE-FILL.md)). |

## What each shot gives each listed actor

- **Size** from the horizontal frame width at the actor's distance: `close_up` to 0.9 m, `medium` to 2.5 m, `wide` to 6 m, `establishing` beyond. The width uses the lens and sensor width the shot plan already holds.
- **Angle** from the bearing between the actor's facing and the camera: `front` within 30 degrees, `three_quarter` to 75, `profile` to 120, `back` beyond.
- **Over the shoulder** (`ots`): the shot lists one actor and its camera is within 1.2 m of another actor.

## Checks

Every unmet required cell, every dialogue line with no covering shot that shows its speaker (unless `offscreen_ok`), every set short of room tone, malformed requirement entries, angle requirements for an actor with no facing, and any shot plan error. An actor who appears in no shot is a warning. Failures are reported together; the report also holds the per-actor matrix, line coverage, room-tone totals and the gaps.

## Suggestions

`--suggest` proposes one new shot per unmet cell, at the distance and bearing the cell asks for (50 mm lens, either side of the actor's facing, three distance factors), starting after the last planned frame. A candidate is kept only if the shot plan validator accepts the extended plan and the shot really gives that cell; a camera is not placed inside a set element the actor is not in. A gap that cannot be placed is listed in `unplaced` with the reason rather than forced. `suggested-plan.json` goes straight into `tools/shot_plan.py`.

```bash
python tools/coverage_matrix.py --demo outputs/coverage-demo
python tools/coverage_matrix.py --plan plan.json --coverage coverage.json --output outputs/coverage [--suggest]
```

Outputs: `coverage-report.json`, `coverage-matrix.md`, and with `--suggest` `suggestions.json` and `suggested-plan.json`. Exit status 2 means the plan has unmet requirements (the report is still written). A matrix without gaps fixes coverage bookkeeping, not editorial judgment about performance or continuity.
