"""Shot plan: validate camera coverage for one set and derive the edit ledger and animatic.

The production roadmap for a filmable scene asks for a stable shot ID, camera, frame range with
handles, asset versions, render settings, output location and completion receipt per shot, and for
every proposed set addition to serve a shot.  Finite physical caches (fire, water) must not be
stretched to fill time.  This module turns those rules into checks over one JSON plan
(``amps.shot_plan.v1``) so that a plan is accepted or refused before any heavy render starts.

Coordinates: metres, x right, z forward, y up.  The plan view uses x and z.  Frame numbers are
integers on the scene timeline (the clock the caches are baked on).  The edit timeline is derived
by concatenating the shots in plan order; it is zero-based.

Checks (each failure names the shot and the rule):

* unique, stable shot IDs; positive-length frame ranges; non-negative handles;
* every camera lies inside the set bounds, looks somewhere, and has a plausible lens;
* every listed performer is inside the horizontal frustum of the shot camera;
* consecutive shots on the same performer pair stay on one side of the axis of action unless the
  later shot declares a reason for crossing; a camera on the axis is refused;
* every set element a shot uses is defined; an element declared ``visible`` is inside the frame;
  an element marked ``added`` must be used by at least one shot;
* a shot that needs a cache must stay inside the baked range including handles, unless it names a
  ``rebake`` range or a ``reviewed_loop`` with a reviewer;
* a receipt may say ``rendered`` only with the exact expected frame count and a SHA-256.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

SCHEMA = "amps.shot_plan.v1"
LEDGER_SCHEMA = "amps.shot_ledger.v1"
SHOT_ID = re.compile(r"^[A-Z][A-Za-z0-9_-]{0,23}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CONTRIBUTIONS = ("visible", "reflection", "shadow", "occlusion", "effect", "offscreen_action")
RECEIPT_STATES = ("planned", "blocked", "approved", "rendered")
CACHE_STRATEGIES = ("rebake", "reviewed_loop")
EDGE_FRACTION = 0.1
_EPS = 1e-9


class PlanError(ValueError):
    """Raised with every failed check, one per line."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def plan_hash(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(plan).encode()).hexdigest()


def hfov_deg(lens_mm: float, sensor_width_mm: float = 36.0) -> float:
    return math.degrees(2.0 * math.atan(sensor_width_mm / (2.0 * lens_mm)))


def _bearing_offset_deg(camera: Mapping[str, Any], point_xz) -> float:
    """Signed horizontal angle in degrees between the optical axis and the direction to a point."""
    px, _, pz = camera["position"]
    ax, _, az = camera["aim"]
    forward = (ax - px, az - pz)
    target = (point_xz[0] - px, point_xz[1] - pz)
    cross = forward[0] * target[1] - forward[1] * target[0]
    dot = forward[0] * target[0] + forward[1] * target[1]
    return math.degrees(math.atan2(cross, dot))


def _side(a, b, point) -> float:
    return (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _vec(value, size) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == size and all(_is_num(v) for v in value)


def _int_range(value) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value) and value[0] <= value[1])


def shot_frames(shot: Mapping[str, Any]) -> dict[str, int]:
    frames = shot["frames"]
    start, end = frames["start"], frames["end"]
    hin, hout = frames.get("handle_in", 0), frames.get("handle_out", 0)
    return {"start": start, "end": end, "duration": end - start + 1, "handle_in": hin, "handle_out": hout,
            "render_start": start - hin, "render_end": end + hout, "render_count": end - start + 1 + hin + hout}


def validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return a report ``{"errors": [...], "warnings": [...]}``; never raises on content problems."""
    errors: list[str] = []
    warnings: list[str] = []
    if plan.get("schema") != SCHEMA:
        errors.append(f"plan: schema must be {SCHEMA}")
        return {"errors": errors, "warnings": warnings}
    fps = plan.get("fps")
    if not (_is_num(fps) and fps > 0):
        errors.append("plan: fps must be a positive number")
        fps = 24
    scene = plan.get("set")
    if not isinstance(scene, dict) or not isinstance(scene.get("bounds"), dict):
        errors.append("plan: set.bounds {x:[min,max], z:[min,max]} is required")
        return {"errors": errors, "warnings": warnings}
    bx, bz = scene["bounds"].get("x"), scene["bounds"].get("z")
    if not (_vec(bx, 2) and _vec(bz, 2) and bx[0] < bx[1] and bz[0] < bz[1]):
        errors.append("plan: set.bounds.x and set.bounds.z must each be [min, max] with min < max")
        return {"errors": errors, "warnings": warnings}
    elements = {}
    for element in scene.get("elements", []):
        eid = element.get("id") if isinstance(element, dict) else None
        if not isinstance(eid, str) or not eid or eid in elements or not _vec(element.get("center"), 2):
            errors.append(f"set: element {eid!r} needs a unique id and a [x, z] center")
            continue
        elements[eid] = element
    caches = {}
    for cache in scene.get("caches", []):
        cid = cache.get("id") if isinstance(cache, dict) else None
        if not isinstance(cid, str) or cid in caches or not _int_range(cache.get("frames")):
            errors.append(f"set: cache {cid!r} needs a unique id and integer frames [first, last]")
            continue
        caches[cid] = cache
    performers = {}
    for performer in plan.get("performers", []):
        pid = performer.get("id") if isinstance(performer, dict) else None
        if not isinstance(pid, str) or not pid or pid in performers or not _vec(performer.get("position"), 2):
            errors.append(f"performers: {pid!r} needs a unique id and an [x, z] position")
            continue
        performers[pid] = performer
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        errors.append("plan: at least one shot is required")
        return {"errors": errors, "warnings": warnings}

    seen: set[str] = set()
    used: set[str] = set()
    previous_side: dict[tuple[str, str], tuple[str, float]] = {}
    total_frames = 0
    for shot in shots:
        sid = shot.get("id") if isinstance(shot, dict) else None
        label = f"shot {sid}"
        if not isinstance(sid, str) or not SHOT_ID.match(sid):
            errors.append(f"shot {sid!r}: id must match {SHOT_ID.pattern}")
            continue
        if sid in seen:
            errors.append(f"{label}: duplicate id")
            continue
        seen.add(sid)
        frames = shot.get("frames")
        ok_frames = (isinstance(frames, dict) and all(isinstance(frames.get(k), int) and not isinstance(frames.get(k), bool)
                                                       for k in ("start", "end"))
                     and frames["start"] <= frames["end"])
        if ok_frames:
            for key in ("handle_in", "handle_out"):
                if key in frames and not (isinstance(frames[key], int) and not isinstance(frames[key], bool) and frames[key] >= 0):
                    errors.append(f"{label}: frames.{key} must be a non-negative integer")
                    ok_frames = False
        else:
            errors.append(f"{label}: frames needs integer start <= end")
        span = shot_frames(shot) if ok_frames else None
        if span:
            total_frames += span["duration"]
        camera = shot.get("camera")
        cam_ok = (isinstance(camera, dict) and _vec(camera.get("position"), 3) and _vec(camera.get("aim"), 3)
                  and _is_num(camera.get("lens_mm")) and isinstance(camera.get("name"), str) and camera["name"])
        if not cam_ok:
            errors.append(f"{label}: camera needs name, lens_mm, position [x,y,z] and aim [x,y,z]")
            continue
        px, py, pz = camera["position"]
        if not (bx[0] <= px <= bx[1] and bz[0] <= pz <= bz[1]) or py < 0:
            errors.append(f"{label}: camera {camera['name']} at {camera['position']} is outside the set bounds")
        if math.hypot(camera["aim"][0] - px, camera["aim"][2] - pz) < 1e-6:
            errors.append(f"{label}: camera aim has no horizontal direction from the camera position")
            continue
        if not 8.0 <= camera["lens_mm"] <= 400.0:
            errors.append(f"{label}: lens_mm {camera['lens_mm']} outside 8 to 400")
            continue
        half = hfov_deg(camera["lens_mm"], camera.get("sensor_width_mm", 36.0)) / 2.0
        listed = shot.get("performers", [])
        for pid in listed:
            if pid not in performers:
                errors.append(f"{label}: performer {pid!r} is not defined")
                continue
            offset = abs(_bearing_offset_deg(camera, performers[pid]["position"]))
            if offset >= half:
                errors.append(f"{label}: performer {pid} is {offset:.1f} deg off axis, outside the {half:.1f} deg half field of view")
            elif offset >= half * (1.0 - EDGE_FRACTION):
                warnings.append(f"{label}: performer {pid} sits in the outer {int(EDGE_FRACTION * 100)} percent of the frame")
        axis = shot.get("axis")
        if axis is not None:
            pair = axis.get("pair") if isinstance(axis, dict) else None
            if not (isinstance(pair, list) and len(pair) == 2 and all(p in performers for p in pair)):
                errors.append(f"{label}: axis.pair needs two defined performer ids")
            else:
                side = _side(performers[pair[0]]["position"], performers[pair[1]]["position"], (px, pz))
                if abs(side) < 1e-6:
                    errors.append(f"{label}: camera stands on the axis of action {pair[0]}-{pair[1]}")
                else:
                    key = (pair[0], pair[1])
                    if key in previous_side and previous_side[key][1] * side < 0 and not axis.get("crossing"):
                        errors.append(f"{label}: camera crosses the axis of action {pair[0]}-{pair[1]} "
                                      f"relative to {previous_side[key][0]} with no declared crossing reason")
                    previous_side[key] = (sid, side)
        for use in shot.get("uses", []):
            if not isinstance(use, dict) or "id" not in use:
                errors.append(f"{label}: each uses entry needs an id")
                continue
            uid, contribution = use["id"], use.get("contribution", "visible")
            if contribution not in CONTRIBUTIONS:
                errors.append(f"{label}: uses {uid}: contribution must be one of {', '.join(CONTRIBUTIONS)}")
            if uid in elements:
                used.add(uid)
                if contribution == "visible":
                    offset = abs(_bearing_offset_deg(camera, elements[uid]["center"]))
                    if offset >= half:
                        errors.append(f"{label}: {uid} is declared visible but is {offset:.1f} deg off axis (half field of view {half:.1f})")
            elif uid in caches:
                used.add(uid)
                cache = caches[uid]
                if span:
                    first, last = cache["frames"]
                    inside = first <= span["render_start"] and span["render_end"] <= last
                    strategy = shot.get("cache_strategy", {}).get(uid) if isinstance(shot.get("cache_strategy"), dict) else None
                    if not inside:
                        if strategy is None:
                            errors.append(f"{label}: cache {uid} covers frames {first}-{last} but the shot needs "
                                          f"{span['render_start']}-{span['render_end']} including handles; declare a cache_strategy")
                        elif strategy.get("kind") not in CACHE_STRATEGIES:
                            errors.append(f"{label}: cache_strategy for {uid} must be one of {', '.join(CACHE_STRATEGIES)}")
                        elif strategy["kind"] == "rebake" and not (_int_range(strategy.get("frames"))
                                  and strategy["frames"][0] <= span["render_start"] and span["render_end"] <= strategy["frames"][1]):
                            errors.append(f"{label}: rebake range for {uid} must cover {span['render_start']}-{span['render_end']}")
                        elif strategy["kind"] == "reviewed_loop" and not strategy.get("reviewed_by"):
                            errors.append(f"{label}: reviewed_loop for {uid} needs reviewed_by; an unreviewed loop is not allowed")
            else:
                errors.append(f"{label}: uses {uid!r} is neither a set element nor a cache")
        render = shot.get("render")
        if not (isinstance(render, dict) and isinstance(render.get("engine"), str) and isinstance(render.get("output"), str)
                and isinstance(render.get("threads"), int) and render["threads"] >= 1):
            errors.append(f"{label}: render needs engine, threads (integer >= 1) and output")
        receipt = shot.get("receipt", {"status": "planned"})
        status = receipt.get("status") if isinstance(receipt, dict) else None
        if status not in RECEIPT_STATES:
            errors.append(f"{label}: receipt.status must be one of {', '.join(RECEIPT_STATES)}")
        elif status == "rendered" and span:
            if receipt.get("frames_rendered") != span["render_count"]:
                errors.append(f"{label}: rendered receipt must report frames_rendered == {span['render_count']}")
            if not SHA256.match(str(receipt.get("sha256", ""))):
                errors.append(f"{label}: rendered receipt needs a SHA-256 of the output")
    for eid, element in elements.items():
        if element.get("added") and eid not in used:
            errors.append(f"set: added element {eid} is used by no shot; remove it or give a shot a reason for it")
    target = plan.get("target_seconds")
    if _vec(target, 2):
        seconds = total_frames / fps
        if not target[0] <= seconds <= target[1]:
            warnings.append(f"plan: duration {seconds:.2f}s is outside the target {target[0]} to {target[1]}s")
    return {"errors": errors, "warnings": warnings}


def require_valid(plan: Mapping[str, Any]) -> list[str]:
    report = validate_plan(plan)
    if report["errors"]:
        raise PlanError(report["errors"])
    return report["warnings"]


def build_ledger(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the per-shot ledger and zero-based edit timeline for a valid plan."""
    warnings = require_valid(plan)
    fps = plan["fps"]
    cursor = 0
    rows = []
    for shot in plan["shots"]:
        span = shot_frames(shot)
        camera = shot["camera"]
        rows.append({
            "id": shot["id"], "camera": camera["name"], "lens_mm": camera["lens_mm"],
            "hfov_deg": round(hfov_deg(camera["lens_mm"], camera.get("sensor_width_mm", 36.0)), 3),
            "scene_frames": [span["start"], span["end"]],
            "render_frames": [span["render_start"], span["render_end"]],
            "render_frame_count": span["render_count"],
            "edit_in": cursor, "edit_out": cursor + span["duration"],
            "duration_s": round(span["duration"] / fps, 4),
            "render": shot["render"], "caches": [u["id"] for u in shot.get("uses", []) if u["id"] in
                                                  {c["id"] for c in plan["set"].get("caches", [])}],
            "receipt": shot.get("receipt", {"status": "planned"}),
        })
        cursor += span["duration"]
    return {"schema": LEDGER_SCHEMA, "plan_sha256": plan_hash(plan), "fps": fps, "total_frames": cursor,
            "total_seconds": round(cursor / fps, 4), "shots": rows, "warnings": warnings}


def ledger_json(ledger: Mapping[str, Any]) -> str:
    return json.dumps(ledger, indent=2, sort_keys=True)


def _timecode(frame: int, fps: int) -> str:
    seconds, ff = divmod(frame, fps)
    minutes, ss = divmod(seconds, 60)
    hh, mm = divmod(minutes, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def build_edl(plan: Mapping[str, Any], ledger: Mapping[str, Any] | None = None) -> str:
    """CMX3600-style list: record times from the edit timeline, source times from the scene timeline."""
    ledger = ledger or build_ledger(plan)
    fps = int(round(plan["fps"]))
    lines = [f"TITLE: {plan.get('title', 'shot plan')}", "FCM: NON-DROP FRAME", ""]
    for number, (shot, row) in enumerate(zip(plan["shots"], ledger["shots"]), start=1):
        s, e = row["scene_frames"]
        lines.append(f"{number:03d}  {row['id']:<8} V     C        "
                     f"{_timecode(s, fps)} {_timecode(e + 1, fps)} {_timecode(row['edit_in'], fps)} {_timecode(row['edit_out'], fps)}")
        lines.append(f"* FROM CLIP NAME: {row['id']} {row['camera']}")
        if shot.get("action"):
            lines.append(f"* ACTION: {shot['action']}")
    return "\n".join(lines) + "\n"


def synthetic_plan() -> dict[str, Any]:
    """A fully synthetic three-shot pilot on an invented round-table set; no private media or names."""
    return {
        "schema": SCHEMA, "title": "synthetic pilot", "fps": 24, "resolution": [1280, 720], "target_seconds": [20, 30],
        "set": {
            "id": "synthetic-round-table", "bounds": {"x": [-5.0, 5.0], "z": [-5.0, 5.0]},
            "elements": [{"id": "table", "center": [0.0, 0.0], "radius": 1.2},
                         {"id": "door", "center": [0.0, 4.5], "radius": 0.5, "added": True},
                         {"id": "window", "center": [-4.5, 0.0], "radius": 0.5, "added": True}],
            "caches": [{"id": "flame", "frames": [1, 240], "kind": "physical"}],
        },
        "performers": [{"id": "A", "position": [-1.0, -0.2]}, {"id": "B", "position": [1.0, 0.3]}],
        "shots": [
            {"id": "S01", "camera": {"name": "wide", "lens_mm": 24, "position": [0.0, 1.6, -4.0], "aim": [0.0, 1.0, 0.0]},
             "frames": {"start": 1, "end": 240, "handle_in": 0, "handle_out": 0}, "performers": ["A", "B"],
             "axis": {"pair": ["A", "B"]}, "action": "A and B sit; the flame is established",
             "uses": [{"id": "table"}, {"id": "flame"}, {"id": "door", "contribution": "visible"}],
             "render": {"engine": "eevee", "threads": 2, "output": "renders/S01/frame_####.exr"}},
            {"id": "S02", "camera": {"name": "over-A", "lens_mm": 50, "position": [-2.5, 1.4, -2.0], "aim": [1.0, 1.1, 0.3]},
             "frames": {"start": 7, "end": 156, "handle_in": 6, "handle_out": 6}, "performers": ["B"],
             "axis": {"pair": ["A", "B"]}, "action": "B answers",
             "uses": [{"id": "flame", "contribution": "shadow"}, {"id": "window", "contribution": "reflection"}],
             "render": {"engine": "eevee", "threads": 2, "output": "renders/S02/frame_####.exr"}},
            {"id": "S03", "camera": {"name": "close-B", "lens_mm": 85, "position": [-0.5, 1.3, -3.0], "aim": [1.0, 1.2, 0.3]},
             "frames": {"start": 91, "end": 240, "handle_in": 6, "handle_out": 0}, "performers": ["B"],
             "axis": {"pair": ["A", "B"]}, "action": "B looks at the flame",
             "uses": [{"id": "flame"}],
             "render": {"engine": "eevee", "threads": 2, "output": "renders/S03/frame_####.exr"}},
        ],
    }
