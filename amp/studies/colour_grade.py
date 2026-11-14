"""Colour pipeline: an ordered, hashable grade chain applied to float frames.

A chain (``amps.colour_chain.v1``) is a list of operations applied in order in scene-linear light, so
that exposure, slope and tint behave like light rather than like code values.  Frames enter and
leave as sRGB-encoded 8-bit images or as float arrays; ``apply_linear`` works on float RGB that is
already linear.  Everything is deterministic numpy; no media is read here.

Operations (``op``):

* ``exposure``  ``stops``                      -- multiply by 2**stops.
* ``cdl``       ``slope``, ``offset``, ``power``, ``saturation`` -- ASC CDL: clamp(in*slope+offset)**power,
                then saturation about Rec.709 luma.
* ``curve``     ``points`` [[x, y], ...], ``channel`` (``luma``/``r``/``g``/``b``/``all``) -- monotone
                piecewise-linear tone curve in linear light.
* ``mono``      ``weights`` [r, g, b], optional ``tint`` [r, g, b] -- channel-mixer black and white.
* ``lut``       ``cube`` (inline 3D LUT as ``.cube`` text) or ``identity`` size, ``space`` (``srgb`` or
                ``linear``) -- trilinear 3D LUT; ``srgb`` applies it to the encoded signal.
* ``region_tint`` ``bands`` [{x0, x1, tint, strength}] -- vertical bands of the frame (fractions of
                width) multiplied toward a tint: coloured regions of a shared room without per-shot masks.

``validate_chain`` returns every problem; ``measure`` reports luma, clipping and saturation so a grade
can be accepted on numbers.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

import numpy as np

SCHEMA = "amps.colour_chain.v1"
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
OPS = ("exposure", "cdl", "curve", "mono", "lut", "region_tint")
CHANNELS = ("luma", "r", "g", "b", "all")
LUT_SPACES = ("srgb", "linear")
MAX_LUT_SIZE = 65


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def chain_hash(chain: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(chain).encode()).hexdigest()


# ---------------------------------------------------------------------------- transfer functions

def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    x = np.clip(x, 0.0, None)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def decode_frame(frame: np.ndarray) -> np.ndarray:
    """uint8 sRGB (H, W, 3) -> float64 linear."""
    if frame.dtype != np.uint8:
        raise ValueError("decode_frame expects uint8 sRGB frames")
    return srgb_to_linear(frame.astype(np.float64) / 255.0)


def encode_frame(linear: np.ndarray) -> np.ndarray:
    """float linear -> uint8 sRGB, clipped to the display range."""
    return np.rint(np.clip(linear_to_srgb(linear), 0.0, 1.0) * 255.0).astype(np.uint8)


def luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ LUMA


# ---------------------------------------------------------------------------------- 3D LUT (.cube)

def identity_lut(size: int) -> np.ndarray:
    """Lattice (size, size, size, 3) indexed [r, g, b]."""
    axis = np.linspace(0.0, 1.0, size)
    r, g, b = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([r, g, b], axis=-1)


def parse_cube(text: str) -> np.ndarray:
    """Parse an Adobe/Resolve ``.cube`` 3D LUT; red varies fastest in the file."""
    size = None
    rows: list[list[float]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head = line.split()[0]
        if head == "LUT_3D_SIZE":
            size = int(line.split()[1])
        elif head in ("TITLE", "DOMAIN_MIN", "DOMAIN_MAX"):
            if head != "TITLE" and [float(v) for v in line.split()[1:]] != ([0.0] * 3 if head == "DOMAIN_MIN" else [1.0] * 3):
                raise ValueError(f"unsupported {head}; only the 0..1 domain is handled")
        elif head == "LUT_1D_SIZE":
            raise ValueError("1D LUTs are not supported; use a curve operation")
        else:
            parts = line.split()
            if len(parts) != 3:
                raise ValueError(f"bad LUT row: {raw!r}")
            rows.append([float(p) for p in parts])
    if size is None or not 2 <= size <= MAX_LUT_SIZE:
        raise ValueError("LUT_3D_SIZE missing or outside 2..%d" % MAX_LUT_SIZE)
    if len(rows) != size ** 3:
        raise ValueError(f"expected {size ** 3} rows, found {len(rows)}")
    flat = np.array(rows, dtype=np.float64)
    return flat.reshape(size, size, size, 3).transpose(2, 1, 0, 3)  # file order is b, g, r -> [r, g, b]


def dumps_cube(lut: np.ndarray, title: str = "lut") -> str:
    size = lut.shape[0]
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    flat = lut.transpose(2, 1, 0, 3).reshape(-1, 3)
    lines.extend(f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in flat)
    return "\n".join(lines) + "\n"


def apply_lut(rgb: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Trilinear interpolation; input is clamped to 0..1 as the format defines no extrapolation."""
    size = lut.shape[0]
    x = np.clip(rgb, 0.0, 1.0) * (size - 1)
    lo = np.minimum(np.floor(x).astype(np.int64), size - 2)
    f = x - lo
    out = np.zeros(rgb.shape, dtype=np.float64)
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = ((f[..., 0] if dr else 1 - f[..., 0]) * (f[..., 1] if dg else 1 - f[..., 1])
                     * (f[..., 2] if db else 1 - f[..., 2]))
                out += w[..., None] * lut[lo[..., 0] + dr, lo[..., 1] + dg, lo[..., 2] + db]
    return out


# ----------------------------------------------------------------------------------- operations

def _vec3(value: Any, name: str, errors: list[str], where: str) -> bool:
    ok = (isinstance(value, list) and len(value) == 3
          and all(isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v) for v in value))
    if not ok:
        errors.append(f"{where}: {name} must be three finite numbers")
    return ok


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and bool(np.isfinite(value))


def validate_chain(chain: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(chain, Mapping) or chain.get("schema") != SCHEMA:
        return [f"chain: schema must be {SCHEMA}"]
    ops = chain.get("ops")
    if not isinstance(ops, list) or not ops:
        return ["chain: ops must be a non-empty list"]
    for i, op in enumerate(ops):
        where = f"op[{i}]"
        if not isinstance(op, Mapping) or op.get("op") not in OPS:
            errors.append(f"{where}: op must be one of {', '.join(OPS)}")
            continue
        kind = op["op"]
        where = f"op[{i}] {kind}"
        if kind == "exposure":
            if not _number(op.get("stops")) or abs(op["stops"]) > 10:
                errors.append(f"{where}: stops must be a number within +-10")
        elif kind == "cdl":
            for key in ("slope", "offset", "power"):
                if _vec3(op.get(key), key, errors, where):
                    if key == "slope" and min(op[key]) < 0:
                        errors.append(f"{where}: slope must be non-negative")
                    if key == "power" and min(op[key]) <= 0:
                        errors.append(f"{where}: power must be positive")
            if not _number(op.get("saturation")) or op["saturation"] < 0:
                errors.append(f"{where}: saturation must be a non-negative number")
        elif kind == "curve":
            pts = op.get("points")
            if op.get("channel", "luma") not in CHANNELS:
                errors.append(f"{where}: channel must be one of {', '.join(CHANNELS)}")
            if (not isinstance(pts, list) or len(pts) < 2
                    or not all(isinstance(p, list) and len(p) == 2 and all(_number(v) for v in p) for p in pts)):
                errors.append(f"{where}: points must be a list of at least two [x, y] pairs")
            else:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                if any(b <= a for a, b in zip(xs, xs[1:])):
                    errors.append(f"{where}: x values must strictly increase")
                if any(b < a for a, b in zip(ys, ys[1:])):
                    errors.append(f"{where}: curve must be monotone non-decreasing (no tone inversion)")
                if min(xs) > 0 or max(xs) < 1:
                    errors.append(f"{where}: points must span x from 0 to 1")
        elif kind == "mono":
            if _vec3(op.get("weights"), "weights", errors, where) and not 0.99 <= sum(op["weights"]) <= 1.01:
                errors.append(f"{where}: weights must sum to 1 so white stays white")
            if "tint" in op:
                _vec3(op["tint"], "tint", errors, where)
        elif kind == "lut":
            if op.get("space", "srgb") not in LUT_SPACES:
                errors.append(f"{where}: space must be srgb or linear")
            has_cube, has_identity = "cube" in op, "identity" in op
            if has_cube == has_identity:
                errors.append(f"{where}: give exactly one of cube or identity")
            elif has_cube:
                try:
                    parse_cube(op["cube"])
                except (ValueError, TypeError) as error:
                    errors.append(f"{where}: {error}")
            elif not (isinstance(op["identity"], int) and not isinstance(op["identity"], bool)
                      and 2 <= op["identity"] <= MAX_LUT_SIZE):
                errors.append(f"{where}: identity must be an integer size from 2 to {MAX_LUT_SIZE}")
        elif kind == "region_tint":
            bands = op.get("bands")
            if not isinstance(bands, list) or not bands:
                errors.append(f"{where}: bands must be a non-empty list")
                continue
            last_end = 0.0
            for j, band in enumerate(bands):
                bw = f"{where} band[{j}]"
                if not isinstance(band, Mapping) or not all(_number(band.get(k)) for k in ("x0", "x1", "strength")):
                    errors.append(f"{bw}: x0, x1 and strength are required numbers")
                    continue
                if not (0 <= band["x0"] < band["x1"] <= 1):
                    errors.append(f"{bw}: need 0 <= x0 < x1 <= 1")
                elif band["x0"] < last_end - 1e-12:
                    errors.append(f"{bw}: bands must be ordered and non-overlapping")
                else:
                    last_end = band["x1"]
                if not 0 <= band["strength"] <= 1:
                    errors.append(f"{bw}: strength must be within 0..1")
                _vec3(band.get("tint"), "tint", errors, bw)
    return errors


def _curve(values: np.ndarray, points: list[list[float]]) -> np.ndarray:
    xs = np.array([p[0] for p in points], dtype=np.float64)
    ys = np.array([p[1] for p in points], dtype=np.float64)
    return np.interp(values, xs, ys)


def _apply_op(rgb: np.ndarray, op: Mapping[str, Any]) -> np.ndarray:
    kind = op["op"]
    if kind == "exposure":
        return rgb * (2.0 ** op["stops"])
    if kind == "cdl":
        out = np.clip(rgb * np.array(op["slope"]) + np.array(op["offset"]), 0.0, None)
        out = out ** np.array(op["power"])
        y = luma(out)[..., None]
        return y + op["saturation"] * (out - y)
    if kind == "curve":
        channel = op.get("channel", "luma")
        if channel == "all":
            return _curve(rgb, op["points"])
        if channel == "luma":
            y = luma(rgb)
            mapped = _curve(y, op["points"])
            scale = np.divide(mapped, y, out=np.ones_like(y), where=y > 1e-9)
            return np.where((y > 1e-9)[..., None], rgb * scale[..., None], mapped[..., None])
        out = rgb.copy()
        idx = "rgb".index(channel)
        out[..., idx] = _curve(rgb[..., idx], op["points"])
        return out
    if kind == "mono":
        y = (rgb @ np.array(op["weights"], dtype=np.float64))[..., None]
        return y * np.array(op.get("tint", [1.0, 1.0, 1.0]), dtype=np.float64)
    if kind == "lut":
        lut = parse_cube(op["cube"]) if "cube" in op else identity_lut(op["identity"])
        if op.get("space", "srgb") == "srgb":
            return srgb_to_linear(apply_lut(np.clip(linear_to_srgb(rgb), 0.0, 1.0), lut))
        return apply_lut(rgb, lut)
    if kind == "region_tint":
        width = rgb.shape[1]
        gain = np.ones((width, 3), dtype=np.float64)
        centres = (np.arange(width) + 0.5) / width
        for band in op["bands"]:
            inside = (centres >= band["x0"]) & (centres < band["x1"])
            tint = np.array(band["tint"], dtype=np.float64)
            gain[inside] = 1.0 + band["strength"] * (tint - 1.0)
        return rgb * gain[None, :, :]
    raise ValueError(f"unknown op {kind}")


def apply_linear(rgb: np.ndarray, chain: Mapping[str, Any]) -> np.ndarray:
    """Apply a validated chain to float linear RGB (H, W, 3).  Raises on an invalid chain."""
    errors = validate_chain(chain)
    if errors:
        raise ValueError("\n".join(errors))
    out = np.asarray(rgb, dtype=np.float64)
    for op in chain["ops"]:
        out = _apply_op(out, op)
    return out


def apply_display(frame: np.ndarray, chain: Mapping[str, Any]) -> np.ndarray:
    """uint8 sRGB in, uint8 sRGB out, graded in linear light."""
    return encode_frame(apply_linear(decode_frame(frame), chain))


def measure(frame: np.ndarray) -> dict[str, float]:
    """Numbers a grade is accepted on, for a uint8 sRGB frame."""
    linear = decode_frame(frame)
    y = luma(linear)
    mx, mn = linear.max(axis=-1), linear.min(axis=-1)
    chroma = np.divide(mx - mn, mx, out=np.zeros_like(mx), where=mx > 1e-9)
    return {
        "mean_luma_linear": round(float(y.mean()), 6),
        "mean_chroma": round(float(chroma.mean()), 6),
        "clipped_high": round(float((frame.max(axis=-1) >= 255).mean()), 6),
        "crushed_low": round(float((frame.max(axis=-1) <= 0).mean()), 6),
    }


def grade_report(frame: np.ndarray, chain: Mapping[str, Any]) -> dict[str, Any]:
    graded = apply_display(frame, chain)
    return {"schema": "amps.grade_report.v1", "chain_sha256": chain_hash(chain),
            "before": measure(frame), "after": measure(graded),
            "graded_sha256": hashlib.sha256(graded.tobytes()).hexdigest()}


# --------------------------------------------------------------------------- synthetic fixtures

def synthetic_ramp(width: int = 256, height: int = 32) -> np.ndarray:
    """Neutral grey ramp 0..255 across the width."""
    row = np.linspace(0, 255, width).round().astype(np.uint8)
    return np.repeat(np.repeat(row[None, :, None], height, axis=0), 3, axis=2)


def synthetic_chart() -> np.ndarray:
    """4 x 6 patch chart: six saturated colours, six mid colours, six greys, six skin-like tones."""
    patches = [
        (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255),
        (180, 90, 60), (90, 140, 70), (70, 100, 170), (200, 170, 60), (120, 80, 150), (90, 170, 160),
        (16, 16, 16), (64, 64, 64), (110, 110, 110), (150, 150, 150), (200, 200, 200), (240, 240, 240),
        (224, 172, 148), (198, 134, 108), (141, 85, 64), (97, 58, 41), (240, 205, 180), (176, 120, 90),
    ]
    frame = np.zeros((4 * 16, 6 * 16, 3), dtype=np.uint8)
    for i, colour in enumerate(patches):
        r, c = divmod(i, 6)
        frame[r * 16:(r + 1) * 16, c * 16:(c + 1) * 16] = colour
    return frame


def synthetic_chain() -> dict[str, Any]:
    """A film-style grade: mild contrast curve, warm slope, slightly reduced saturation."""
    return {"schema": SCHEMA, "name": "synthetic-demo", "ops": [
        {"op": "exposure", "stops": 0.25},
        {"op": "curve", "channel": "luma", "points": [[0, 0], [0.18, 0.16], [0.6, 0.66], [1, 1]]},
        {"op": "cdl", "slope": [1.04, 1.0, 0.94], "offset": [0.0, 0.0, 0.0], "power": [1.0, 1.0, 1.0], "saturation": 0.9},
    ]}
