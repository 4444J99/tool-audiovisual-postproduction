"""Layered compositing plan: validate a layer stack before rendering and flatten it for reference.

A plan (``amps.layer_plan.v1``) lists layers bottom to top.  Each layer reads a source, may be
graded by a named colour chain, may be matted by another layer, and blends onto what is below it.
``validate_plan`` returns every problem; a plan with errors is never rendered.  ``flatten`` is a
small reference compositor (straight alpha, scene-linear float) used to check blend and matte
arithmetic on synthetic frames, and as the definition a faster renderer must match.

Rules checked:

* unique layer IDs; known kinds, blend modes and matte modes; opacity within 0..1 (or keyframes);
* every layer's frame range is positive and inside the composition;
* a plate, fx or text layer names a defined source; a solid carries a colour; an adjustment layer
  carries a colour chain and no source; audio sources cannot be layers;
* image, video and render sources are content-addressed (SHA-256) and use relative paths only, so a
  plan can be published without leaking where private media lives;
* a matte layer is of kind ``matte``, is not itself matted, lies below the layer it mattes and covers
  its frames; a matte layer is never drawn;
* a layer's chain exists and is valid; a source's colour space is known and a mismatch with the
  composition's working space is reported as a warning (it is converted);
* every composition frame is covered by at least one drawn base layer, unless ``allow_gaps``.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping

import numpy as np

from . import colour_grade as cg

SCHEMA = "amps.layer_plan.v1"
KINDS = ("plate", "matte", "fx", "text", "solid", "adjustment")
BLENDS = ("normal", "multiply", "screen", "add", "overlay")
MATTE_MODES = ("alpha", "luma", "invert_luma")
SOURCE_KINDS = ("video", "image", "render", "audio")
COLOURSPACES = ("srgb", "linear")
LAYER_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def plan_hash(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(plan).encode()).hexdigest()


def _is_relative(path: Any) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith(("/", "~", "\\"))
            and ".." not in path.replace("\\", "/").split("/") and not re.match(r"^[A-Za-z]:", path)
            and "://" not in path)


def _triple(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 3
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v) for v in value))


def opacity_at(layer: Mapping[str, Any], frame: int) -> float:
    value = layer.get("opacity", 1.0)
    if isinstance(value, list):
        return float(np.interp(frame, [k[0] for k in value], [k[1] for k in value]))
    return float(value)


def validate_plan(plan: Any) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(plan, Mapping) or plan.get("schema") != SCHEMA:
        return {"errors": [f"plan: schema must be {SCHEMA}"], "warnings": []}
    comp = plan.get("comp", {})
    try:
        frames, working = int(comp["frames"]), comp.get("working_space", "linear")
        if frames < 1 or int(comp["width"]) < 1 or int(comp["height"]) < 1 or not comp["fps"] > 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return {"errors": ["comp: width, height, fps and frames must be positive"], "warnings": []}
    if working != "linear":
        errors.append("comp: working_space must be linear (grading and blending are defined in linear light)")
    sources = plan.get("sources", {})
    chains = plan.get("chains", {})
    layers = plan.get("layers")
    if not isinstance(sources, Mapping) or not isinstance(chains, Mapping) or not isinstance(layers, list) or not layers:
        return {"errors": ["plan: sources and chains must be objects and layers a non-empty list"], "warnings": []}

    valid_sources: set[str] = set()
    for sid, src in sources.items():
        where = f"source {sid}"
        if not isinstance(src, Mapping) or src.get("kind") not in SOURCE_KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(SOURCE_KINDS)}")
            continue
        valid_sources.add(sid)
        if src.get("colourspace", "srgb") not in COLOURSPACES:
            errors.append(f"{where}: colourspace must be srgb or linear")
        if src["kind"] != "audio" and src.get("colourspace", "srgb") != working:
            warnings.append(f"{where}: {src.get('colourspace', 'srgb')} source is converted to {working}")
        if not SHA256.match(str(src.get("sha256", ""))):
            errors.append(f"{where}: sha256 is required so the source is content-addressed")
        if not _is_relative(src.get("path")):
            errors.append(f"{where}: path must be relative (no absolute paths, URLs or ..)")
    for cid, chain in chains.items():
        for problem in cg.validate_chain(chain):
            errors.append(f"chain {cid}: {problem}")

    index: dict[str, int] = {}
    for i, layer in enumerate(layers):
        lid = layer.get("id") if isinstance(layer, Mapping) else None
        if not isinstance(layer, Mapping):
            errors.append(f"layer[{i}]: must be an object")
            continue
        if not isinstance(lid, str) or not LAYER_ID.match(lid):
            errors.append(f"layer[{i}]: id must match {LAYER_ID.pattern}")
        elif lid in index:
            errors.append(f"layer {lid}: duplicate id")
        else:
            index[lid] = i

    covered = np.zeros(frames, dtype=bool)
    matted_ids = {l["matte"]["layer"] for l in layers if isinstance(l, Mapping) and isinstance(l.get("matte"), Mapping)
                  and isinstance(l["matte"].get("layer"), str)}
    for i, layer in enumerate(layers):
        if not isinstance(layer, Mapping) or not isinstance(layer.get("id"), str) or layer["id"] not in index:
            continue
        lid, where = layer["id"], f"layer {layer['id']}"
        kind = layer.get("kind")
        if kind not in KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(KINDS)}")
            continue
        if layer.get("blend", "normal") not in BLENDS:
            errors.append(f"{where}: blend must be one of {', '.join(BLENDS)}")
        fr = layer.get("frames")
        fr = fr if isinstance(fr, Mapping) else {}
        start, end = fr.get("start"), fr.get("end")
        in_range = (isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool)
                    and 0 <= start < end <= frames)
        if not in_range:
            errors.append(f"{where}: frames need integers with 0 <= start < end <= {frames}")
        op = layer.get("opacity", 1.0)
        op_valid = True
        if isinstance(op, list):
            ok = (len(op) >= 2 and all(isinstance(k, list) and len(k) == 2 and all(isinstance(v, (int, float)) for v in k) for k in op)
                  and all(b[0] > a[0] for a, b in zip(op, op[1:])) and all(0 <= k[1] <= 1 for k in op))
            op_valid = bool(ok)
            if not ok:
                errors.append(f"{where}: opacity keyframes must be [frame, value] pairs, frames increasing, values 0..1")
            elif in_range and (op[0][0] > start or op[-1][0] < end - 1):
                warnings.append(f"{where}: opacity keyframes do not span the layer; end values are held")
        elif not isinstance(op, (int, float)) or isinstance(op, bool) or not 0 <= op <= 1:
            op_valid = False
            errors.append(f"{where}: opacity must be 0..1")
        src = layer.get("source")
        if kind in ("plate", "fx", "text"):
            if not isinstance(src, str) or src not in sources:
                errors.append(f"{where}: source {src!r} is not defined")
            elif src in valid_sources and sources[src].get("kind") == "audio":
                errors.append(f"{where}: audio source {src} cannot be a picture layer")
        elif kind == "matte":
            if "solid" in layer:
                if not _triple(layer["solid"]):
                    errors.append(f"{where}: solid needs a colour of three finite numbers in linear light")
            elif not isinstance(src, str) or src not in sources:
                errors.append(f"{where}: matte needs a defined source or a solid")
            elif src in valid_sources and sources[src].get("kind") == "audio":
                errors.append(f"{where}: audio source {src} cannot be a picture layer")
            if layer.get("matte"):
                errors.append(f"{where}: a matte layer cannot itself be matted")
            if lid not in matted_ids:
                warnings.append(f"{where}: matte layer is not used by any layer")
        elif kind == "solid":
            if not _triple(layer.get("solid")):
                errors.append(f"{where}: solid needs a colour of three finite numbers in linear light")
        elif kind == "adjustment":
            if src is not None:
                errors.append(f"{where}: an adjustment layer has no source")
            if not layer.get("chain"):
                errors.append(f"{where}: an adjustment layer needs a chain")
            if layer.get("blend", "normal") != "normal":
                errors.append(f"{where}: an adjustment layer blends by opacity only (blend must be normal)")
            if i == 0:
                errors.append(f"{where}: an adjustment layer has nothing below it to adjust")
        chain = layer.get("chain")
        if chain is not None and (not isinstance(chain, str) or chain not in chains):
            errors.append(f"{where}: chain {chain!r} is not defined")
        matte = layer.get("matte")
        if matte is not None and not isinstance(matte, Mapping):
            errors.append(f"{where}: matte must be an object with a layer and optional mode")
            matte = None
            layer = {**layer, "matte": None}
        if matte is not None:
            target = matte.get("layer")
            if not isinstance(target, str) or target not in index:
                errors.append(f"{where}: matte layer {target!r} does not exist")
            else:
                if target == lid:
                    errors.append(f"{where}: a layer cannot matte itself")
                elif layers[index[target]].get("kind") != "matte":
                    errors.append(f"{where}: matte target {target} must be of kind matte")
                elif index[target] >= i:
                    errors.append(f"{where}: matte {target} must lie below the layer it mattes")
                else:
                    mf = layers[index[target]].get("frames")
                    mf = mf if isinstance(mf, Mapping) else {}
                    if in_range and not (isinstance(mf.get("start"), int) and isinstance(mf.get("end"), int)
                                         and mf["start"] <= start and mf["end"] >= end):
                        errors.append(f"{where}: matte {target} does not cover frames {start}-{end}")
            if matte.get("mode", "alpha") not in MATTE_MODES:
                errors.append(f"{where}: matte mode must be one of {', '.join(MATTE_MODES)}")
        if (in_range and kind in ("plate", "fx", "text", "solid") and not matte
                and layer.get("blend", "normal") == "normal"
                and op_valid):
            covered[start:end] |= np.array([opacity_at(layer, f) >= 1.0 for f in range(start, end)])
    if not plan.get("allow_gaps") and not covered.all():
        gaps = np.flatnonzero(~covered)
        errors.append(f"comp: frames {int(gaps[0])}-{int(gaps[-1])} have no opaque base layer (set allow_gaps to permit)")
    return {"errors": errors, "warnings": warnings}


def active_layers(plan: Mapping[str, Any], frame: int) -> list[str]:
    """Drawn layers at a frame, bottom to top (matte layers are excluded)."""
    return [l["id"] for l in plan["layers"]
            if l["kind"] != "matte" and l["frames"]["start"] <= frame < l["frames"]["end"]]


def layer_ledger(plan: Mapping[str, Any]) -> dict[str, Any]:
    rows = [{"id": l["id"], "kind": l["kind"], "start": l["frames"]["start"], "end": l["frames"]["end"],
             "blend": l.get("blend", "normal"), "chain": l.get("chain"),
             "chain_sha256": cg.chain_hash(plan.get("chains", {})[l["chain"]]) if l.get("chain") else None,
             "matte": (l.get("matte") or {}).get("layer"), "source": l.get("source"),
             "source_sha256": plan.get("sources", {})[l["source"]]["sha256"] if l.get("source") in plan.get("sources", {}) else None}
            for l in plan["layers"]]
    return {"schema": "amps.layer_ledger.v1", "plan_sha256": plan_hash(plan), "layers": rows}


# ------------------------------------------------------------------------------ reference compositor

def _blend(base: np.ndarray, top: np.ndarray, mode: str) -> np.ndarray:
    if mode == "normal":
        return top
    if mode == "multiply":
        return base * top
    if mode == "screen":
        return 1.0 - (1.0 - base) * (1.0 - top)
    if mode == "add":
        return base + top
    if mode == "overlay":
        return np.where(base <= 0.5, 2.0 * base * top, 1.0 - 2.0 * (1.0 - base) * (1.0 - top))
    raise ValueError(mode)


def _matte_value(rgba: np.ndarray, mode: str) -> np.ndarray:
    if mode == "alpha":
        return rgba[..., 3]
    y = cg.luma(rgba[..., :3])
    return y if mode == "luma" else 1.0 - y


Loader = Callable[[str, int], np.ndarray]


def _load_linear(plan: Mapping[str, Any], load: Loader, source: str, frame: int) -> np.ndarray:
    """Load RGBA in the source's declared colour space and return it in linear light (alpha untouched)."""
    rgba = np.array(load(source, frame), dtype=np.float64)
    if plan["sources"][source].get("colourspace", "srgb") == "srgb":
        rgba[..., :3] = cg.srgb_to_linear(rgba[..., :3])
    return rgba


def flatten(plan: Mapping[str, Any], frame: int, load: Loader) -> np.ndarray:
    """Composite one frame.  ``load(source_id, frame)`` returns float RGBA (H, W, 4), straight alpha, in the
    source's declared colour space; sRGB sources are decoded to linear here.

    Returns linear RGB (H, W, 3) composited over black, using the W3C compositing formula (blend modes act
    only where the backdrop has alpha).  The plan must validate; ``allow_gaps`` areas are black.
    """
    report = validate_plan(plan)
    if report["errors"]:
        raise ValueError("\n".join(report["errors"]))
    h, w = int(plan["comp"]["height"]), int(plan["comp"]["width"])
    layers = {l["id"]: l for l in plan["layers"]}
    out = np.zeros((h, w, 3), dtype=np.float64)      # straight (unpremultiplied) backdrop colour
    out_a = np.zeros((h, w), dtype=np.float64)
    for lid in active_layers(plan, frame):
        layer = layers[lid]
        graded_chain = plan["chains"][layer["chain"]] if layer.get("chain") else None
        weight = np.full((h, w), opacity_at(layer, frame), dtype=np.float64)
        matte = layer.get("matte")
        if matte:
            mlayer = layers[matte["layer"]]
            if "solid" in mlayer:
                mrgba = np.broadcast_to(np.asarray(list(mlayer["solid"]) + [1.0], dtype=np.float64), (h, w, 4)).copy()
            else:
                mrgba = _load_linear(plan, load, mlayer["source"], frame)
            if mlayer.get("chain"):
                mrgba[..., :3] = cg.apply_linear(mrgba[..., :3], plan["chains"][mlayer["chain"]])
            value = np.clip(_matte_value(mrgba, matte.get("mode", "alpha")), 0.0, 1.0)
            weight = weight * value * opacity_at(mlayer, frame)
        if layer["kind"] == "adjustment":
            adjusted = cg.apply_linear(out, graded_chain)
            out = out + (weight * (out_a > 0))[..., None] * (adjusted - out)
            continue
        if layer["kind"] == "solid":
            rgb, alpha = np.broadcast_to(np.asarray(layer["solid"], dtype=np.float64), (h, w, 3)), np.ones((h, w))
        else:
            rgba = _load_linear(plan, load, layer["source"], frame)
            rgb, alpha = rgba[..., :3], rgba[..., 3]
        if graded_chain:
            rgb = cg.apply_linear(rgb, graded_chain)
        a_s = np.clip(alpha, 0.0, 1.0) * weight
        a_r = a_s + out_a * (1.0 - a_s)
        mixed = (1.0 - out_a)[..., None] * rgb + out_a[..., None] * _blend(out, rgb, layer.get("blend", "normal"))
        share = np.divide(a_s, a_r, out=np.zeros_like(a_s), where=a_r > 0)[..., None]
        out = (1.0 - share) * out + share * mixed
        out_a = a_r
    return out * out_a[..., None]


# ---------------------------------------------------------------------------------- synthetic plan

def synthetic_plan() -> dict[str, Any]:
    """Three-layer stack: plate, graded fx glow through a luma matte, and a closing adjustment."""
    def sha(label: str) -> str:
        return hashlib.sha256(label.encode()).hexdigest()
    return {
        "schema": SCHEMA,
        "comp": {"width": 64, "height": 36, "fps": 24, "frames": 48, "working_space": "linear"},
        "sources": {
            "room": {"kind": "video", "path": "plates/room.mov", "sha256": sha("room"), "colourspace": "srgb"},
            "glow": {"kind": "render", "path": "renders/glow.exr", "sha256": sha("glow"), "colourspace": "linear"},
            "vignette": {"kind": "image", "path": "mattes/vignette.png", "sha256": sha("vignette"), "colourspace": "linear"},
        },
        "chains": {
            "warm": {"schema": cg.SCHEMA, "ops": [{"op": "cdl", "slope": [1.1, 1.0, 0.85], "offset": [0, 0, 0],
                                                   "power": [1, 1, 1], "saturation": 1.0}]},
            "film": cg.synthetic_chain(),
        },
        "layers": [
            {"id": "base", "kind": "plate", "source": "room", "frames": {"start": 0, "end": 48}},
            {"id": "vig", "kind": "matte", "source": "vignette", "frames": {"start": 0, "end": 48}},
            {"id": "fire", "kind": "fx", "source": "glow", "frames": {"start": 6, "end": 42}, "blend": "screen",
             "opacity": [[6, 0.0], [12, 0.8], [36, 0.8], [41, 0.0]], "chain": "warm",
             "matte": {"layer": "vig", "mode": "luma"}},
            {"id": "look", "kind": "adjustment", "frames": {"start": 0, "end": 48}, "chain": "film", "opacity": 1.0},
        ],
    }
