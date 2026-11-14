"""Actor-coverage matrix: which shots cover which actors, at what size and from which angle.

A scene needs each actor covered the way the edit will want them: a close-up from the front for the
reaction, a profile two-shot for the exchange, an over-the-shoulder for the answer.  This module reads a
shot plan (``amps.shot_plan.v1``) plus a coverage requirement (``amps.coverage_matrix.v1``) and reports,
from the plan's own geometry, what every shot gives every actor listed in it.

Size comes from the horizontal frame width at the actor's distance: ``close_up`` up to 0.9 m,
``medium`` up to 2.5 m, ``wide`` up to 6 m, ``establishing`` beyond.  Angle is the bearing from the
actor to the camera measured from the way the actor faces: ``front`` within 30 degrees,
``three_quarter`` to 75, ``profile`` to 120, ``back`` beyond.  A shot listing one actor with its camera
within 1.2 m of another actor is an over-the-shoulder (``ots``) of the first.

Checks (every failure is reported):

* the requirement is well formed, names only defined actors, and every needed actor has a facing;
* every required ``(actor, size, angle)`` cell, with ``min_shots``, is met by some shot;
* every dialogue line has a shot that covers its frames and shows its speaker, unless the line is
  declared ``offscreen_ok``;
* every set has at least ``room_tone_min_s`` seconds of recorded room tone, which is what the
  room-tone fill borrows from;
* a defined actor who appears in no shot is a warning.

``suggest_shots`` proposes one new shot per unmet cell, placed at the distance and bearing the cell asks
for, and keeps only those the shot plan validator accepts, so the output feeds ``tools/shot_plan.py``
directly.  A gap it cannot place is reported with the reason.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping

from . import shot_plan as sp

SCHEMA = "amps.coverage_matrix.v1"
SIZES = ("close_up", "medium", "wide", "establishing")
SIZE_LIMITS = (("close_up", 0.9), ("medium", 2.5), ("wide", 6.0))
SIZE_WIDTH_TARGET = {"close_up": 0.7, "medium": 1.8, "wide": 4.0, "establishing": 8.0}
ANGLES = ("front", "three_quarter", "profile", "back")
ANGLE_LIMITS = (("front", 30.0), ("three_quarter", 75.0), ("profile", 120.0))
ANGLE_TARGET = {"front": 10.0, "three_quarter": 52.0, "profile": 98.0, "back": 150.0}
OTS_DISTANCE = 1.2
DEFAULT_ROOM_TONE_MIN_S = 30.0


def classify_size(frame_width_m: float) -> str:
    for name, limit in SIZE_LIMITS:
        if frame_width_m <= limit:
            return name
    return "establishing"


def classify_angle(off_axis_deg: float) -> str:
    for name, limit in ANGLE_LIMITS:
        if off_axis_deg <= limit:
            return name
    return "back"


def facing_vector(performer: Mapping[str, Any], performers: Mapping[str, Mapping[str, Any]]):
    """Unit (x, z) vector the performer faces, from ``facing_deg`` (0 = +z, clockwise) or ``facing_toward``."""
    if sp._is_num(performer.get("facing_deg")):
        a = math.radians(performer["facing_deg"])
        return (math.sin(a), math.cos(a))
    target = performer.get("facing_toward")
    if isinstance(target, str) and target in performers and target != performer["id"]:
        dx = performers[target]["position"][0] - performer["position"][0]
        dz = performers[target]["position"][1] - performer["position"][1]
        length = math.hypot(dx, dz)
        if length > 1e-9:
            return (dx / length, dz / length)
    return None


def shot_cells(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per (shot, listed actor): size, angle, distance, over-the-shoulder flag."""
    performers = {p["id"]: p for p in plan.get("performers", [])}
    rows = []
    for shot in plan["shots"]:
        camera = shot["camera"]
        px, _, pz = camera["position"]
        half = sp.hfov_deg(camera["lens_mm"], camera.get("sensor_width_mm", 36.0)) / 2.0
        listed = shot.get("performers", [])
        for pid in listed:
            performer = performers.get(pid)
            if performer is None:
                continue
            ax, az = performer["position"]
            distance = math.hypot(ax - px, az - pz)
            width = 2.0 * distance * math.tan(math.radians(half))
            face = facing_vector(performer, performers)
            angle = None
            if face is not None and distance > 1e-9:
                to_cam = ((px - ax) / distance, (pz - az) / distance)
                dot = max(-1.0, min(1.0, face[0] * to_cam[0] + face[1] * to_cam[1]))
                angle = classify_angle(math.degrees(math.acos(dot)))
            ots = False
            if len(listed) == 1:
                ots = any(o != pid and math.hypot(performers[o]["position"][0] - px, performers[o]["position"][1] - pz) <= OTS_DISTANCE
                          for o in performers)
            rows.append({"shot": shot["id"], "actor": pid, "size": classify_size(width), "angle": angle, "ots": ots,
                         "distance_m": round(distance, 3), "frame_width_m": round(width, 3), "camera": camera["name"],
                         "frames": [shot["frames"]["start"], shot["frames"]["end"]]})
    return rows


def validate_spec(spec: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, Mapping) or spec.get("schema") != SCHEMA:
        return [f"coverage: schema must be {SCHEMA}"]
    actors = {p["id"] for p in plan.get("performers", [])}

    def known(actor) -> bool:
        return isinstance(actor, str) and actor in actors

    required = spec.get("required", [])
    if not isinstance(required, list):
        errors.append("coverage: required must be a list")
        required = []
    for i, cell in enumerate(required):
        if not isinstance(cell, dict) or not known(cell.get("actor")):
            errors.append(f"coverage: required[{i}] needs an actor defined in the plan")
            continue
        if "size" in cell and cell["size"] not in SIZES:
            errors.append(f"coverage: required[{i}] size must be one of {', '.join(SIZES)}")
        if "angle" in cell and cell["angle"] not in ANGLES:
            errors.append(f"coverage: required[{i}] angle must be one of {', '.join(ANGLES)}")
        if "min_shots" in cell and not (isinstance(cell["min_shots"], int) and not isinstance(cell["min_shots"], bool)
                                        and cell["min_shots"] >= 1):
            errors.append(f"coverage: required[{i}] min_shots must be an integer >= 1")
        if "ots" in cell and not isinstance(cell["ots"], bool):
            errors.append(f"coverage: required[{i}] ots must be true or false")
    lines = spec.get("lines", [])
    seen = set()
    if not isinstance(lines, list):
        errors.append("coverage: lines must be a list")
        lines = []
    for i, line in enumerate(lines):
        lid = line.get("id") if isinstance(line, dict) else None
        if not isinstance(lid, str) or not lid or lid in seen:
            errors.append(f"coverage: lines[{i}] needs a unique id")
            continue
        seen.add(lid)
        if not isinstance(line.get("actor"), str) or line["actor"] not in actors:
            errors.append(f"coverage: line {lid} actor must be defined in the plan")
        if "offscreen_ok" in line and not isinstance(line["offscreen_ok"], bool):
            errors.append(f"coverage: line {lid} offscreen_ok must be true or false")
        if not sp._int_range(line.get("frames")):
            errors.append(f"coverage: line {lid} frames must be integer [first, last]")
    tone = spec.get("room_tone", [])
    if not isinstance(tone, list):
        errors.append("coverage: room_tone must be a list")
        tone = []
    for i, entry in enumerate(tone):
        if not (isinstance(entry, dict) and isinstance(entry.get("set"), str) and sp._is_num(entry.get("seconds")) and entry["seconds"] >= 0):
            errors.append(f"coverage: room_tone[{i}] needs a set id and seconds >= 0")
    minimum = spec.get("room_tone_min_s", DEFAULT_ROOM_TONE_MIN_S)
    if not (sp._is_num(minimum) and minimum >= 0):
        errors.append("coverage: room_tone_min_s must be a number >= 0")
    needs_facing = {c["actor"] for c in required if isinstance(c, dict) and "angle" in c and known(c.get("actor"))}
    for p in plan.get("performers", []):
        if p["id"] in needs_facing and facing_vector(p, {q["id"]: q for q in plan.get("performers", [])}) is None:
            errors.append(f"coverage: performer {p['id']} needs facing_deg or facing_toward to judge angles")
    return errors


def _matches(row: Mapping[str, Any], cell: Mapping[str, Any]) -> bool:
    if row["actor"] != cell["actor"]:
        return False
    if "size" in cell and row["size"] != cell["size"]:
        return False
    if "angle" in cell and row["angle"] != cell["angle"]:
        return False
    if "ots" in cell and row["ots"] != cell["ots"]:
        return False
    return True


def validate_coverage(plan: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    """Full report: errors, warnings, the per-actor matrix, line coverage, room-tone totals and gaps."""
    plan_report = sp.validate_plan(plan)
    errors = [f"shot plan: {e}" for e in plan_report["errors"]]
    warnings = list(plan_report["warnings"])
    if errors:
        return {"errors": errors, "warnings": warnings, "matrix": {}, "lines": [], "gaps": [], "room_tone": []}
    errors += validate_spec(spec, plan)
    if errors:
        return {"errors": errors, "warnings": warnings, "matrix": {}, "lines": [], "gaps": [], "room_tone": []}
    rows = shot_cells(plan)
    matrix: dict[str, dict[str, Any]] = {}
    for p in plan.get("performers", []):
        cells: dict[str, list[str]] = {}
        for row in rows:
            if row["actor"] != p["id"]:
                continue
            key = f"{row['size']}/{row['angle'] or 'unknown'}" + ("+ots" if row["ots"] else "")
            cells.setdefault(key, []).append(row["shot"])
        matrix[p["id"]] = {"cells": cells, "shots": sorted({r["shot"] for r in rows if r["actor"] == p["id"]})}
        if not matrix[p["id"]]["shots"]:
            warnings.append(f"actor {p['id']} appears in no shot")
    gaps = []
    for cell in spec.get("required", []):
        have = sorted({r["shot"] for r in rows if _matches(r, cell)})
        need = cell.get("min_shots", 1)
        if len(have) < need:
            gap = {k: cell[k] for k in ("actor", "size", "angle", "ots") if k in cell}
            gap.update(need=need, have=len(have), shots=have)
            gaps.append(gap)
            wanted = ", ".join(f"{k}={gap[k]}" for k in ("size", "angle", "ots") if k in gap) or "any"
            errors.append(f"actor {cell['actor']} needs {need} shot(s) of {wanted}; the plan has {len(have)}")
    lines = []
    for line in spec.get("lines", []):
        first, last = line["frames"]
        covering = [r["shot"] for r in rows if r["actor"] == line["actor"] and r["frames"][0] <= first and last <= r["frames"][1]]
        any_cover = [s["id"] for s in plan["shots"] if s["frames"]["start"] <= first and last <= s["frames"]["end"]]
        lines.append({"id": line["id"], "actor": line["actor"], "frames": [first, last], "shots_showing_speaker": covering,
                      "shots_covering_frames": any_cover})
        if not covering and not line.get("offscreen_ok"):
            if any_cover:
                errors.append(f"line {line['id']}: {line['actor']} speaks in frames {first}-{last} but no shot covering them shows {line['actor']}; "
                              f"mark it offscreen_ok if that is intended")
            else:
                errors.append(f"line {line['id']}: no shot covers frames {first}-{last}")
    minimum = spec.get("room_tone_min_s", DEFAULT_ROOM_TONE_MIN_S)
    totals: dict[str, float] = {}
    for entry in spec.get("room_tone", []):
        totals[entry["set"]] = totals.get(entry["set"], 0.0) + entry["seconds"]
    set_id = plan["set"].get("id", "set")
    for name in sorted(set(totals) | {set_id}):
        if totals.get(name, 0.0) < minimum:
            errors.append(f"set {name}: {totals.get(name, 0.0):g} s of room tone recorded, {minimum:g} s needed for a fill")
    room = [{"set": name, "seconds": totals.get(name, 0.0), "needed_s": minimum} for name in sorted(set(totals) | {set_id})]
    return {"errors": errors, "warnings": warnings, "matrix": matrix, "lines": lines, "gaps": gaps, "room_tone": room,
            "plan_sha256": sp.plan_hash(plan)}


def suggest_shots(plan: Mapping[str, Any], spec: Mapping[str, Any], default_frames: int = 48) -> dict[str, Any]:
    """Propose a validated shot for each unmet cell; returns ``{"shots": [...], "unplaced": [...], "plan": extended plan}``."""
    report = validate_coverage(plan, spec)
    if report["matrix"] == {} and report["errors"]:
        return {"shots": [], "unplaced": [{"reason": e} for e in report["errors"]], "plan": copy.deepcopy(plan)}
    working = copy.deepcopy(plan)
    performers = {p["id"]: p for p in working.get("performers", [])}
    cursor = max(s["frames"]["end"] for s in working["shots"]) + 1
    template = working["shots"][0]
    added, unplaced = [], []
    taken = {s["id"] for s in working["shots"]}
    counter = 1
    for gap in report["gaps"]:
        actor = performers[gap["actor"]]
        size, angle = gap.get("size", "medium"), gap.get("angle", "front")
        face = facing_vector(actor, performers) or (0.0, -1.0)
        width = SIZE_WIDTH_TARGET[size]
        lens = 50.0
        distance = width * lens / 36.0
        base = math.degrees(math.atan2(face[0], face[1]))
        placed = 0
        for _ in range(gap["need"] - gap["have"]):
            ok = False
            for sign in (1, -1):
                for factor in (1.0, 0.8, 1.2):
                    bearing = math.radians(base + sign * ANGLE_TARGET[angle])
                    cx = actor["position"][0] + math.sin(bearing) * distance * factor
                    cz = actor["position"][1] + math.cos(bearing) * distance * factor
                    while f"CV{counter:02d}" in taken:
                        counter += 1
                    sid = f"CV{counter:02d}"
                    others = [p["id"] for p in working.get("performers", []) if p["id"] != gap["actor"]]
                    candidate = {
                        "id": sid, "camera": {"name": f"cov-{gap['actor']}-{size}-{angle}", "lens_mm": lens,
                                              "position": [round(cx, 3), 1.5, round(cz, 3)],
                                              "aim": [actor["position"][0], 1.5, actor["position"][1]]},
                        "frames": {"start": cursor, "end": cursor + default_frames - 1, "handle_in": 0, "handle_out": 0},
                        "performers": [gap["actor"]], "action": f"coverage: {gap['actor']} {size} {angle}",
                        "uses": [], "render": copy.deepcopy(template["render"]),
                    }
                    candidate["render"]["output"] = f"renders/{sid}/frame_####.exr"
                    pair = _axis_pair(working, gap["actor"], others)
                    if pair:
                        candidate["axis"] = {"pair": pair}          # lets the validator reject a camera that crosses the axis
                    ax, az = actor["position"]
                    if any(math.hypot(cx - e["center"][0], cz - e["center"][1]) < e.get("radius", 0.0)
                           and math.hypot(ax - e["center"][0], az - e["center"][1]) >= e.get("radius", 0.0)
                           for e in working["set"].get("elements", [])):
                        continue                              # the camera would stand inside a set element
                    trial = copy.deepcopy(working)
                    trial["shots"].append(candidate)
                    result = sp.validate_plan(trial)
                    if result["errors"]:
                        continue
                    cells = [r for r in shot_cells(trial) if r["shot"] == sid]
                    if not cells or not _matches(cells[0], {k: gap[k] for k in ("actor", "size", "angle", "ots") if k in gap}):
                        continue
                    working, ok = trial, True
                    taken.add(sid)
                    added.append(candidate)
                    cursor += default_frames
                    placed += 1
                    break
                if ok:
                    break
            if not ok:
                wanted = ", ".join(f"{k}={gap[k]}" for k in ("size", "angle", "ots") if k in gap)
                unplaced.append({"actor": gap["actor"], "cell": wanted,
                                 "reason": "no camera position inside the set bounds gives this cell and passes the shot plan checks"})
    return {"shots": added, "unplaced": unplaced, "plan": working}


def _axis_pair(plan: Mapping[str, Any], actor: str, others: list[str]) -> list[str] | None:
    """The performer pair whose axis of action the plan already respects for this actor, else actor plus one other."""
    for shot in plan["shots"]:
        pair = shot.get("axis", {}).get("pair") if isinstance(shot.get("axis"), dict) else None
        if isinstance(pair, list) and actor in pair:
            return sorted(pair)
    return sorted([actor, others[0]]) if others else None


def render_table(report: Mapping[str, Any]) -> str:
    """Markdown table of the matrix: one row per actor, one column per size, cells list angle and shots."""
    lines = ["| actor | " + " | ".join(SIZES) + " |", "|---|" + "---|" * len(SIZES)]
    for actor, data in report["matrix"].items():
        cols = []
        for size in SIZES:
            entries = [f"{key.split('/', 1)[1]} ({', '.join(shots)})" for key, shots in sorted(data["cells"].items()) if key.startswith(size + "/")]
            cols.append("; ".join(entries) or "-")
        lines.append(f"| {actor} | " + " | ".join(cols) + " |")
    return "\n".join(lines) + "\n"


def synthetic_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    """Plan and requirement for the synthetic round-table pilot, with actor facings and dialogue."""
    plan = sp.synthetic_plan()
    for performer, toward in zip(plan.get("performers", []), ("B", "A")):
        performer["facing_toward"] = toward
    spec = {
        "schema": SCHEMA,
        "required": [{"actor": "B", "size": "close_up", "angle": "three_quarter"},
                     {"actor": "A", "size": "medium", "angle": "front"},
                     {"actor": "B", "size": "wide"}],
        "lines": [{"id": "L1", "actor": "B", "frames": [100, 150]},
                  {"id": "L2", "actor": "A", "frames": [20, 60]}],
        "room_tone": [{"set": plan["set"]["id"], "seconds": 45.0}],
        "room_tone_min_s": 30.0,
    }
    return plan, spec
