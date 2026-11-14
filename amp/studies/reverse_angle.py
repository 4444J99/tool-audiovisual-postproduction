"""Reverse-angle planning and plane-based reconstruction with an observed/unobserved ledger.

A reverse angle looks back along the axis of action toward the place the original camera
stood.  A single-camera recording shows only part of what such a camera would see: the
room surfaces that fell inside the original frustum and were not hidden by a performer.
This module makes that boundary explicit.

* ``plan_reverse_camera`` places the reverse camera behind the second performer on the
  same side of the axis of action as the source camera (the 180 degree rule), keeps the
  eyeline height, aims it at the first performer and reports every constraint it checked.
* ``reproject_observed`` renders the reverse view of the room proxy (the schema emitted by
  ``depth_world.reconstruct_room_proxy_geometry``) by carrying observed source pixels onto
  the proxy surfaces.  Every output pixel receives a status: observed, outside the source
  frustum, or occluded by a performer.  Nothing is invented.
* ``fill_unobserved`` offers a labelled placeholder fill.  Filled pixels stay unobserved in
  the ledger; a placeholder is never reported as reconstruction.

Coordinates follow ``depth_world``: x right, y up, z forward from the source camera, metres.
Planes satisfy ``n . p + d = 0``.  Performers are not part of the proxy, so their pixels are
excluded rather than projected onto the walls.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import numpy as np

SCHEMA = "amps.god_here.reverse_angle.v1"
FACE_NAMES = ("left", "right", "floor", "ceiling", "front", "back")
OBSERVED, OUTSIDE_FRUSTUM, SUBJECT_OCCLUDED = 0, 1, 2
STATUS_NAMES = {OBSERVED: "observed", OUTSIDE_FRUSTUM: "outside_source_frustum",
                SUBJECT_OCCLUDED: "performer_occluded"}
_EPS = 1e-6


@dataclass(frozen=True)
class Camera:
    position: tuple[float, float, float]
    rotation: tuple[tuple[float, float, float], ...]  # columns are right, up, forward in world axes
    fx: float
    fy: float
    width: int
    height: int

    def matrix(self) -> np.ndarray:
        return np.array(self.rotation, dtype=np.float64)

    def as_dict(self) -> dict[str, Any]:
        forward = self.matrix()[:, 2]
        return {"position_m": [round(v, 6) for v in self.position],
                "forward": [round(float(v), 6) for v in forward],
                "yaw_deg": round(math.degrees(math.atan2(forward[0], forward[2])), 4),
                "fx": self.fx, "fy": self.fy, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class RoomBox:
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]

    def contains(self, point: Sequence[float], margin: float = 0.0) -> bool:
        return all(self.lo[i] + margin < point[i] < self.hi[i] - margin for i in range(3))


@dataclass
class Reconstruction:
    image: np.ndarray       # float32 (h, w, 3), zero where unobserved
    status: np.ndarray      # uint8 (h, w)
    face: np.ndarray        # uint8 (h, w) index into FACE_NAMES
    stretched: np.ndarray   # bool (h, w): observed but magnified beyond the stated limit


def source_camera(width: int, height: int, hfov_deg: float = 65.0,
                  position: Sequence[float] = (0.0, 0.0, 0.0)) -> Camera:
    if width <= 0 or height <= 0 or not 1.0 <= hfov_deg <= 170.0:
        raise ValueError("invalid source camera dimensions or field of view")
    fx = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    return Camera(tuple(float(v) for v in position), identity, fx, fx, width, height)


def room_box_from_proxy(proxy: Mapping[str, Any], front_depth_m: float = 1.0) -> RoomBox:
    """Close the open proxy into a box: the original camera stands ``front_depth_m`` from the front face."""
    if not isinstance(proxy, Mapping) or proxy.get("schema") != "amps.god_here.room_proxy.v1":
        raise ValueError("proxy must use schema amps.god_here.room_proxy.v1")
    if not (isinstance(front_depth_m, (int, float)) and front_depth_m > 0):
        raise ValueError("front_depth_m must be positive")
    box = proxy.get("bounding_box_m")
    if not (isinstance(box, (list, tuple)) and len(box) == 6):
        raise ValueError("proxy bounding_box_m must hold six numbers")
    x0, y0, near, x1, y1, far = (float(v) for v in box)
    planes = proxy.get("wall_planes", {})
    floor = proxy.get("floor_plane")
    expected = {"floor": (floor, (0, 1, 0), y0), "back": (planes.get("back"), (0, 0, -1), far),
                "left": (planes.get("left"), (1, 0, 0), x0), "right": (planes.get("right"), (-1, 0, 0), x1)}
    for name, (plane, normal, coordinate) in expected.items():
        if not (isinstance(plane, (list, tuple)) and len(plane) == 4):
            raise ValueError(f"proxy plane {name} is missing")
        if tuple(float(v) for v in plane[:3]) != tuple(float(v) for v in normal):
            raise ValueError(f"proxy plane {name} is not axis aligned as expected")
        axis = [i for i, v in enumerate(normal) if v][0]
        offset = -float(plane[3]) / normal[axis]
        if abs(offset - coordinate) > 5e-3:
            raise ValueError(f"proxy plane {name} disagrees with bounding_box_m")
    if not (x0 < x1 and y0 < y1 and 0 < near < far):
        raise ValueError("proxy bounding box is degenerate")
    return RoomBox((x0, y0, -float(front_depth_m)), (x1, y1, far))


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < _EPS:
        raise ValueError("zero-length direction")
    return v / n


def _cross2(a: Sequence[float], b: Sequence[float]) -> float:
    """Signed area in the ground plane (x, z)."""
    return a[0] * b[1] - a[1] * b[0]


def _side(a_xz: Sequence[float], b_xz: Sequence[float], p_xz: Sequence[float]) -> float:
    return _cross2((b_xz[0] - a_xz[0], b_xz[1] - a_xz[1]), (p_xz[0] - a_xz[0], p_xz[1] - a_xz[1]))


def _ground(p: Sequence[float]) -> tuple[float, float]:
    return float(p[0]), float(p[2])


def look_at(position: Sequence[float], target: Sequence[float], template: Camera) -> Camera:
    """Upright camera (no roll) at ``position`` aimed at ``target``; intrinsics from ``template``."""
    forward = _unit(np.array(target, dtype=np.float64) - np.array(position, dtype=np.float64))
    up_hint = np.array([0.0, 1.0, 0.0])
    right = np.cross(up_hint, forward)
    if np.linalg.norm(right) < 1e-4:
        raise ValueError("camera cannot look straight up or down")
    right = _unit(right)
    up = np.cross(forward, right)
    rotation = tuple(tuple(float(m[i]) for m in (right, up, forward)) for i in range(3))
    return Camera(tuple(float(v) for v in position), rotation, template.fx, template.fy,
                  template.width, template.height)


def plan_reverse_camera(source: Camera, subject_a: Sequence[float], subject_b: Sequence[float],
                        standoff_m: float, lateral_m: float, box: RoomBox,
                        eye_height_m: Optional[float] = None) -> dict[str, Any]:
    """Place a same-side reverse camera behind ``subject_b`` looking toward ``subject_a``.

    Raises ``ValueError`` when the source camera lies on the axis of action, when the
    offsets would cross the axis or leave the room, or when the performers coincide.
    """
    if not (standoff_m > 0 and lateral_m > 0):
        raise ValueError("standoff_m and lateral_m must be positive so the camera stays beyond and beside the performer")
    a = np.array(subject_a, dtype=np.float64)
    b = np.array(subject_b, dtype=np.float64)
    ga, gb, gs = _ground(a), _ground(b), _ground(source.position)
    axis = np.array([gb[0] - ga[0], gb[1] - ga[1]])
    if np.linalg.norm(axis) < 1e-3:
        raise ValueError("performers coincide on the ground plane; no axis of action")
    source_side = _side(ga, gb, gs)
    if abs(source_side) < 1e-6:
        raise ValueError("source camera lies on the axis of action; the same-side rule is undefined")
    u = axis / np.linalg.norm(axis)
    sign = 1.0 if source_side > 0 else -1.0
    normal = sign * np.array([-u[1], u[0]])
    ground = np.array(gb) + u * standoff_m + normal * lateral_m
    eye = float(source.position[1] if eye_height_m is None else eye_height_m)
    position = (float(ground[0]), eye, float(ground[1]))
    if not box.contains(position, margin=0.05):
        raise ValueError("reverse camera would stand outside the room")
    camera = look_at(position, (a[0], eye, a[2]), source)
    reverse_side = _side(ga, gb, _ground(position))
    if reverse_side * source_side <= 0:
        raise ValueError("reverse camera crosses the axis of action")
    src_forward = source.matrix()[:, 2]
    rev_forward = camera.matrix()[:, 2]
    separation = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(src_forward, rev_forward))))))
    return {"schema": SCHEMA, "kind": "reverse_camera_plan", "camera": camera.as_dict(), "_camera": camera,
            "checks": {"same_side_of_axis": True, "axis_of_action_ground_xz": [round(float(v), 6) for v in u],
                       "optical_axis_separation_deg": round(separation, 3),
                       "eyeline_height_m": eye, "eyeline_matches_source": eye == float(source.position[1]),
                       "inside_room": True, "performer_in_frame_centre": True}}


def _ray_dirs(camera: Camera) -> np.ndarray:
    u = np.arange(camera.width, dtype=np.float64) + 0.5
    v = np.arange(camera.height, dtype=np.float64) + 0.5
    x = (u[None, :] - camera.width / 2.0) / camera.fx
    y = -(v[:, None] - camera.height / 2.0) / camera.fy
    cam = np.stack([np.broadcast_to(x, (camera.height, camera.width)),
                    np.broadcast_to(y, (camera.height, camera.width)),
                    np.ones((camera.height, camera.width))], axis=-1)
    world = cam @ camera.matrix().T
    return world / np.linalg.norm(world, axis=-1, keepdims=True)


def _box_hits(box: RoomBox, origin: Sequence[float], dirs: np.ndarray):
    o = np.array(origin, dtype=np.float64)
    if not box.contains(o):
        raise ValueError("camera origin must lie inside the room box")
    lo, hi = np.array(box.lo), np.array(box.hi)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_lo, t_hi = (lo - o) / dirs, (hi - o) / dirs
    t_axis = np.where(dirs > _EPS, t_hi, np.where(dirs < -_EPS, t_lo, np.inf))
    axis = np.argmin(t_axis, axis=-1)
    t = np.take_along_axis(t_axis, axis[..., None], axis=-1)[..., 0]
    positive = np.take_along_axis(dirs, axis[..., None], axis=-1)[..., 0] > 0
    face = (axis * 2 + positive.astype(np.int64)).astype(np.uint8)
    return t, face, o + dirs * t[..., None]


def _face_coords(box: RoomBox, face: np.ndarray, points: np.ndarray):
    lo, hi = np.array(box.lo), np.array(box.hi)
    s = np.zeros(face.shape)
    t = np.zeros(face.shape)
    for index in range(6):
        select = face == index
        if not select.any():
            continue
        a, b = [ax for ax in range(3) if ax != index // 2]
        s[select] = (points[..., a][select] - lo[a]) / (hi[a] - lo[a])
        t[select] = (points[..., b][select] - lo[b]) / (hi[b] - lo[b])
    return s, t


_BASE = np.array([[200, 90, 90], [90, 200, 90], [90, 90, 200], [200, 200, 90], [200, 90, 200], [90, 200, 200]], dtype=np.float64)


def render_proxy_room(box: RoomBox, camera: Camera) -> np.ndarray:
    """Render the closed proxy with a smooth, face-identifiable procedural texture (previs and test source)."""
    _, face, points = _box_hits(box, camera.position, _ray_dirs(camera))
    s, t = _face_coords(box, face, points)
    shade = 0.78 + 0.22 * np.sin(2 * math.pi * 1.5 * s) * np.cos(2 * math.pi * 1.0 * t)
    return np.clip(_BASE[face] * shade[..., None], 0, 255).astype(np.float32)


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    out = mask.copy()
    for _ in range(max(0, radius)):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


def _bilinear(image: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    u = np.clip(u, 0, w - 1)
    v = np.clip(v, 0, h - 1)
    u0 = np.floor(u).astype(int)
    v0 = np.floor(v).astype(int)
    u1 = np.minimum(u0 + 1, w - 1)
    v1 = np.minimum(v0 + 1, h - 1)
    fu = (u - u0)[..., None]
    fv = (v - v0)[..., None]
    return (image[v0, u0] * (1 - fu) * (1 - fv) + image[v0, u1] * fu * (1 - fv)
            + image[v1, u0] * (1 - fu) * fv + image[v1, u1] * fu * fv)


def reproject_observed(frame: np.ndarray, source: Camera, target: Camera, box: RoomBox,
                       subject_mask: Optional[np.ndarray] = None, mask_margin_px: int = 2,
                       max_magnification: float = 4.0) -> Reconstruction:
    """Carry observed source pixels to ``target`` through the room proxy.

    Performer pixels (``subject_mask`` true) and everything outside the source frustum stay
    unobserved.  A pixel is ``stretched`` when the source supplies it at more than
    ``max_magnification`` output pixels per source pixel.
    """
    frame = np.asarray(frame, dtype=np.float32)
    if frame.shape != (source.height, source.width, 3):
        raise ValueError("frame shape must match the source camera")
    if subject_mask is not None:
        subject_mask = np.asarray(subject_mask, dtype=bool)
        if subject_mask.shape != frame.shape[:2]:
            raise ValueError("subject_mask shape must match the frame")
        subject_mask = _dilate(subject_mask, int(mask_margin_px))
    if not max_magnification > 0:
        raise ValueError("max_magnification must be positive")
    _, face, points = _box_hits(box, target.position, _ray_dirs(target))
    relative = points - np.array(source.position)
    camera_space = relative @ source.matrix()
    z = camera_space[..., 2]
    ahead = z > _EPS
    safe_z = np.where(ahead, z, 1.0)
    u = source.fx * camera_space[..., 0] / safe_z + source.width / 2.0 - 0.5
    v = -source.fy * camera_space[..., 1] / safe_z + source.height / 2.0 - 0.5
    slack = 1e-6  # absorbs float error at the exact frame edge
    inside = ahead & (u >= -slack) & (u <= source.width - 1 + slack) & (v >= -slack) & (v <= source.height - 1 + slack)
    status = np.full(face.shape, OUTSIDE_FRUSTUM, dtype=np.uint8)
    status[inside] = OBSERVED
    if subject_mask is not None:
        ui = np.clip(np.rint(u).astype(int), 0, source.width - 1)
        vi = np.clip(np.rint(v).astype(int), 0, source.height - 1)
        status[inside & subject_mask[vi, ui]] = SUBJECT_OCCLUDED
    observed = status == OBSERVED
    image = np.zeros(frame.shape[:2] + (3,), dtype=np.float32)
    if observed.any():
        image[observed] = _bilinear(frame, u[observed], v[observed]).astype(np.float32)
    du_dy, du_dx = np.gradient(u)
    dv_dy, dv_dx = np.gradient(v)
    area = np.abs(du_dx * dv_dy - du_dy * dv_dx)
    stable = np.ones(face.shape, dtype=bool)
    for shift, axis in ((1, 0), (-1, 0), (1, 1), (-1, 1)):
        stable &= face == np.roll(face, shift, axis=axis)
    stretched = observed & stable & (area > 0) & (area < 1.0 / (max_magnification ** 2))
    return Reconstruction(image, status, face, stretched)


def fill_unobserved(reconstruction: Reconstruction, mode: str = "face_mean") -> tuple[np.ndarray, np.ndarray]:
    """Return (image, filled_mask).  Placeholders only: filled pixels remain unobserved in the ledger."""
    if mode not in {"none", "face_mean"}:
        raise ValueError("mode must be none or face_mean")
    image = reconstruction.image.copy()
    missing = reconstruction.status != OBSERVED
    if mode == "none":
        return image, np.zeros_like(missing)
    for index in range(len(FACE_NAMES)):
        on_face = reconstruction.face == index
        seen = on_face & ~missing
        value = reconstruction.image[seen].mean(axis=0) if seen.any() else np.array([128.0, 128.0, 128.0])
        image[on_face & missing] = value
    return image, missing


def build_ledger(reconstruction: Reconstruction, fill_mode: str = "none", plan: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    total = int(reconstruction.status.size)
    faces: dict[str, Any] = {}
    for index, name in enumerate(FACE_NAMES):
        on_face = reconstruction.face == index
        count = int(on_face.sum())
        if not count:
            continue
        faces[name] = {"pixels": count}
        for code, label in STATUS_NAMES.items():
            faces[name][label] = int(((reconstruction.status == code) & on_face).sum())
        faces[name]["observed_fraction"] = round(faces[name]["observed"] / count, 6)
    observed = int((reconstruction.status == OBSERVED).sum())
    identity = hashlib.sha256(reconstruction.status.tobytes() + reconstruction.face.tobytes()).hexdigest()
    ledger = {"schema": SCHEMA, "kind": "reconstruction_ledger", "pixels": total,
              "observed": observed, "observed_fraction": round(observed / total, 6),
              "unobserved": {label: int((reconstruction.status == code).sum()) for code, label in STATUS_NAMES.items() if code},
              "stretched_observed": int(reconstruction.stretched.sum()),
              "faces": faces, "fill": {"mode": fill_mode, "pixels_filled": 0 if fill_mode == "none" else total - observed,
                                      "status": "placeholder; filled pixels are not reconstruction"},
              "geometry_identity_sha256": identity,
              "acceptance": "pending; geometry carries observed pixels only, performers and unseen surfaces are not reconstructed"}
    if plan is not None:
        ledger["plan"] = {k: v for k, v in plan.items() if not k.startswith("_")}
    return ledger


def ledger_json(ledger: Mapping[str, Any]) -> str:
    return json.dumps(ledger, indent=2, sort_keys=True)
