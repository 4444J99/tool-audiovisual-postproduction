"""Set spec: validate a written set specification and derive a Blender-ready scene graph from it.

A set spec (``amps.set_spec.v1``) is a small JSON description of one filmable environment: bounds,
named architecture (boxes, cylinders, stepped arch bands), materials, lights and cameras.  This
module is pure Python.  It checks the spec and turns it into a deterministic scene graph of
meshes, lights and cameras in Blender coordinates (metres, Z up) so that the Blender builder
(``tools/blender_set_scene.py``) only has to copy what the graph says, and so that the same graph
can be asserted without Blender.

Spec coordinates follow the shot plan: metres, x right, y up, z forward (depth into the set).
Blender coordinates are x right, y depth, z up, so a spec point (x, y, z) becomes (x, z, y).

Checks (each failure is reported, none stops the others):

* schema, unique stable ids, finite numbers, bounds with min < max and a positive height;
* every material referenced exists and is a six-digit hex colour with roughness within 0..1;
* every element sits inside the bounds, with the footprint of its extent also inside;
* arch bands: positive widths whose sum leaves a positive opening, a depth that survives every
  stepped reveal, and band colours that are hex or a defined material;
* lights have a kind, a position, positive energy; cameras have a horizontal aim and a lens from
  8 to 400 mm; the active camera exists.

``shot_plan_set`` exports the spec's bounds and elements as the ``set`` block of a shot plan, so
the environment and the coverage plan describe one set.

Versions.  ``amps.set_spec.v1`` is the original vocabulary: box, vertical cylinder, arch bands.
``amps.set_spec.v1.1`` is a strict superset: a v1 spec is also a valid v1.1 spec and builds the same
graph.  v1.1 adds an ``axis`` ("x", "y" or "z") on cylinders so they can lie along x or z, and the
kinds ``cone`` (frustum or point), ``sphere``, ``torus`` and ``stairs``.  A spec that declares v1 but
uses any of these is refused with the features named and the schema they need (``negotiate``); an
unknown schema is refused with the supported list.  ``amp.studies.set_spec.required_schema`` says
the lowest schema a spec needs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

SCHEMA_V1 = "amps.set_spec.v1"
SCHEMA_V11 = "amps.set_spec.v1.1"
SCHEMA = SCHEMA_V1                                  # the baseline schema; v1.1 is a strict superset
SUPPORTED_SCHEMAS = (SCHEMA_V1, SCHEMA_V11)
GRAPH_SCHEMA = "amps.set_scene_graph.v1"
ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
V1_KINDS = ("box", "cylinder", "arch_bands")
V11_KINDS = ("sphere", "cone", "torus", "stairs")
ELEMENT_KINDS = V1_KINDS + V11_KINDS
AXES = ("x", "y", "z")                              # spec axes: x right, y up, z depth
AXIS_KINDS = ("cylinder", "cone", "torus")          # kinds that take an ``axis`` (v1.1)
DEFAULT_RINGS = 16
DEFAULT_TUBE_SEGMENTS = 16
MAX_STEPS = 200
LIGHT_KINDS = ("point", "sun", "area")
DEFAULT_SEGMENTS = 48
RESERVED_FLOOR = "FLOOR"
GENERATED_PREFIX = "hex-"
SOFFIT_SUFFIX = "-soffit"
FLOOR_DEFAULT = "floor-default"
BLENDER_NAME_LIMIT = 63


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def spec_hash(spec: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(spec).encode()).hexdigest()


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _vec(value, size) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == size and all(_is_num(v) for v in value)


MIN_SENSOR_MM = 1.0


def _check_caches(spec: Mapping[str, Any], errors: list[str]) -> None:
    """Caches are forwarded to the shot plan, so they follow its rules: unique string id, integer [first, last]."""
    caches = spec.get("caches", [])
    if not isinstance(caches, list):
        errors.append("caches: must be a list of {id, frames: [first, last]}")
        return
    element_ids = {e.get("id") for e in (spec.get("elements") or []) if isinstance(e, dict)}
    seen: set[str] = set()
    for cache in caches:
        cid = cache.get("id") if isinstance(cache, dict) else None
        frames = cache.get("frames") if isinstance(cache, dict) else None
        ok_frames = (isinstance(frames, list) and len(frames) == 2
                     and all(isinstance(v, int) and not isinstance(v, bool) for v in frames) and frames[0] <= frames[1])
        if not isinstance(cid, str) or not cid or cid in seen or not ok_frames:
            errors.append(f"cache {cid!r}: needs a unique string id and integer frames [first, last]")
            continue
        seen.add(cid)
        if cid in element_ids:
            errors.append(f"cache {cid!r}: id is also used by an element")


def _positive(value) -> bool:
    return _is_num(value) and value > 0


def _int_at_least(value, floor) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= floor


def srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def hex_to_linear(value: str) -> list[float]:
    raw = value.lstrip("#")
    return [round(srgb_to_linear(int(raw[i:i + 2], 16) / 255.0), 6) for i in (0, 2, 4)]


def element_extent(element: Mapping[str, Any]) -> tuple[float, float]:
    """Half-extent (x, z) of the element footprint around its centre, for bounds and plan use."""
    width, _, depth = local_size(element)
    if element["kind"] == "cylinder" and element.get("axis", "y") == "y":
        return element["radius"], element["radius"]          # a vertical cylinder is round: rotation changes nothing
    angle = math.radians(element.get("rotation_deg", 0.0))
    c, s = abs(math.cos(angle)), abs(math.sin(angle))
    return (width * c + depth * s) / 2.0, (width * s + depth * c) / 2.0


def element_radius(element: Mapping[str, Any]) -> float:
    hx, hz = element_extent(element)
    return round(max(hx, hz), 6)


def v11_features(spec: Mapping[str, Any]) -> list[str]:
    """Every v1.1 feature a spec uses, one readable line each (empty for a plain v1 spec)."""
    found: list[str] = []
    elements = spec.get("elements") if isinstance(spec, Mapping) else None
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, Mapping):
            continue
        eid = element.get("id")
        if element.get("kind") in V11_KINDS:
            found.append(f"element {eid}: kind {element['kind']!r}")
        if "axis" in element:
            found.append(f"element {eid}: axis")
    return found


def required_schema(spec: Mapping[str, Any]) -> str:
    """The lowest supported schema that can express this spec."""
    return SCHEMA_V11 if v11_features(spec) else SCHEMA_V1


def negotiate(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Declared schema, the schema the content needs, and whether the declaration is enough."""
    declared = spec.get("schema") if isinstance(spec, Mapping) else None
    required = required_schema(spec) if isinstance(spec, Mapping) else SCHEMA_V1
    ok = declared in SUPPORTED_SCHEMAS and (declared == SCHEMA_V11 or required == SCHEMA_V1)
    return {"declared": declared, "required": required, "supported": list(SUPPORTED_SCHEMAS),
            "features": v11_features(spec) if isinstance(spec, Mapping) else [], "ok": ok}


def local_size(element: Mapping[str, Any]) -> tuple[float, float, float]:
    """Unrotated bounding size (x, y, z) of one element in spec axes (x right, y up, z depth)."""
    kind = element["kind"]
    if kind in ("box", "stairs"):
        return tuple(element["size"])
    if kind == "arch_bands":
        return 2.0 * element["outer_radius"], element["outer_radius"], element["depth"]
    if kind == "sphere":
        return (2.0 * element["radius"],) * 3
    axis = element.get("axis", "y")
    if kind == "torus":
        thin, wide = 2.0 * element["minor_radius"], 2.0 * (element["major_radius"] + element["minor_radius"])
    else:
        radius = max(element["radius"], element.get("top_radius", 0.0)) if kind == "cone" else element["radius"]
        thin, wide = element["height"], 2.0 * radius
    return {"y": (wide, thin, wide), "x": (thin, wide, wide), "z": (wide, wide, thin)}[axis]


def validate_spec(spec: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return ``{"errors": [...], "warnings": [...]}``; never raises on content problems."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(spec, Mapping):
        return {"errors": ["spec: top level must be a JSON object"], "warnings": warnings}
    if spec.get("schema") not in SUPPORTED_SCHEMAS:
        return {"errors": [f"spec: schema must be one of {', '.join(SUPPORTED_SCHEMAS)}"], "warnings": warnings}
    bounds = spec.get("bounds")
    bx = bounds.get("x") if isinstance(bounds, dict) else None
    bz = bounds.get("z") if isinstance(bounds, dict) else None
    height = bounds.get("height") if isinstance(bounds, dict) else None
    if not (_vec(bx, 2) and _vec(bz, 2) and bx[0] < bx[1] and bz[0] < bz[1] and _positive(height)):
        return {"errors": ["spec: bounds needs x [min, max], z [min, max] with min < max and a positive height"],
                "warnings": warnings}

    materials: dict[str, Mapping[str, Any]] = {}
    raw_materials = spec.get("materials", {})
    if not isinstance(raw_materials, dict):
        errors.append("materials: must be an object of name -> {color, roughness, emission}")
        raw_materials = {}
    for name, material in raw_materials.items():
        if not isinstance(name, str) or not name or name.startswith(GENERATED_PREFIX) or name.endswith(SOFFIT_SUFFIX) or name == FLOOR_DEFAULT:
            errors.append(f"material {name!r}: names starting with {GENERATED_PREFIX!r}, ending with {SOFFIT_SUFFIX!r} or equal to "
                          f"{FLOOR_DEFAULT!r} are reserved for generated materials, and a name cannot be empty")
            continue
        if _name_bytes(name + SOFFIT_SUFFIX) > BLENDER_NAME_LIMIT:
            errors.append(f"material {name!r}: name plus its {SOFFIT_SUFFIX!r} derivative must fit {BLENDER_NAME_LIMIT} bytes (UTF-8)")
            continue
        if not isinstance(material, dict) or not isinstance(material.get("color"), str) or not HEX.match(material["color"]):
            errors.append(f"material {name!r}: color must be a six-digit hex string")
            continue
        rough = material.get("roughness", 0.5)
        if not (_is_num(rough) and 0.0 <= rough <= 1.0):
            errors.append(f"material {name!r}: roughness must be within 0..1")
            continue
        emission = material.get("emission", 0.0)
        if not (_is_num(emission) and emission >= 0.0):
            errors.append(f"material {name!r}: emission must be a number >= 0")
            continue
        materials[name] = material
    floor = spec.get("floor", {})
    if not isinstance(floor, dict):
        errors.append("floor: must be an object")
        floor = {}
    if floor.get("material") is not None and not (isinstance(floor["material"], str) and floor["material"] in materials):
        errors.append(f"floor: material {floor['material']!r} is not a defined material name")

    if spec["schema"] == SCHEMA_V1:
        for feature in v11_features(spec):
            errors.append(f"spec: {feature} needs schema {SCHEMA_V11}, but the spec declares {SCHEMA_V1}")

    ids: set[str] = set()

    def claim(kind: str, ident) -> bool:
        if not isinstance(ident, str) or not ELEMENT_ID.match(ident):
            errors.append(f"{kind} {ident!r}: id must match {ELEMENT_ID.pattern}")
            return False
        if ident in ids or ident == RESERVED_FLOOR:
            errors.append(f"{kind} {ident!r}: duplicate or reserved id")
            return False
        ids.add(ident)
        return True

    elements = spec.get("elements")
    if not isinstance(elements, list) or not elements:
        errors.append("spec: at least one element is required")
        elements = []
    for element in elements:
        eid = element.get("id") if isinstance(element, dict) else None
        label = f"element {eid}"
        if not claim("element", eid):
            continue
        kind = element.get("kind")
        if kind not in ELEMENT_KINDS:
            errors.append(f"{label}: kind must be one of {', '.join(ELEMENT_KINDS)}")
            continue
        if not _vec(element.get("center"), 2):
            errors.append(f"{label}: center must be [x, z]")
            continue
        if "rotation_deg" in element and not _is_num(element["rotation_deg"]):
            errors.append(f"{label}: rotation_deg must be a number")
            continue
        if "y" in element and not (_is_num(element["y"]) and element["y"] >= 0):
            errors.append(f"{label}: y (base elevation) must be a number >= 0")
            continue
        ok = True
        if kind == "box":
            ok = _vec(element.get("size"), 3) and all(v > 0 for v in element["size"])
            if not ok:
                errors.append(f"{label}: size must be [width, height, depth], all positive")
        elif kind == "cylinder":
            ok = _positive(element.get("radius")) and _positive(element.get("height"))
            if not ok:
                errors.append(f"{label}: radius and height must be positive")
            if ok and "segments" in element and not _int_at_least(element["segments"], 8):
                errors.append(f"{label}: segments must be an integer >= 8")
                ok = False
        elif kind == "arch_bands":
            ok = _arch_errors(element, label, materials, errors)
        else:
            ok = _v11_element_errors(element, label, errors)
        if ok and "axis" in element and (kind not in AXIS_KINDS or element["axis"] not in AXES):
            errors.append(f"{label}: axis must be one of {', '.join(AXES)} and applies only to {', '.join(AXIS_KINDS)}")
            ok = False
        if element.get("material") is not None and not (isinstance(element["material"], str) and element["material"] in materials):
            errors.append(f"{label}: material {element['material']!r} is not a defined material name")
            ok = False
        if kind != "arch_bands" and element.get("material") is None:
            errors.append(f"{label}: material is required")
            ok = False
        if not ok:
            continue
        top = local_size(element)[1] + element.get("y", 0.0)
        if top > height + 1e-9:
            errors.append(f"{label}: top at {top:g} m exceeds the set height {height:g} m")
        hx, hz = element_extent(element)
        cx, cz = element["center"]
        if cx - hx < bx[0] - 1e-9 or cx + hx > bx[1] + 1e-9 or cz - hz < bz[0] - 1e-9 or cz + hz > bz[1] + 1e-9:
            errors.append(f"{label}: footprint extends outside the set bounds")

    for light in spec.get("lights", []) if isinstance(spec.get("lights", []), list) else []:
        lid = light.get("id") if isinstance(light, dict) else None
        label = f"light {lid}"
        if not claim("light", lid):
            continue
        if light.get("kind") not in LIGHT_KINDS:
            errors.append(f"{label}: kind must be one of {', '.join(LIGHT_KINDS)}")
        if not _vec(light.get("position"), 3):
            errors.append(f"{label}: position must be [x, y, z]")
        if not _positive(light.get("energy")):
            errors.append(f"{label}: energy must be positive")
        if "color" in light and not (isinstance(light["color"], str) and HEX.match(light["color"])):
            errors.append(f"{label}: color must be a six-digit hex string")
        if "size" in light and not _positive(light["size"]):
            errors.append(f"{label}: size must be a finite positive number")
        if "aim" in light and not _vec(light["aim"], 3):
            errors.append(f"{label}: aim must be [x, y, z]")
        if light.get("kind") in ("sun", "area") and "aim" not in light:
            errors.append(f"{label}: a {light.get('kind')} light needs an aim [x, y, z]")
    if not isinstance(spec.get("lights", []), list):
        errors.append("lights: must be a list")

    cameras = spec.get("cameras", [])
    camera_ids: set[str] = set()
    if not isinstance(cameras, list):
        errors.append("cameras: must be a list")
        cameras = []
    for camera in cameras:
        cid = camera.get("id") if isinstance(camera, dict) else None
        label = f"camera {cid}"
        if not claim("camera", cid):
            continue
        camera_ids.add(cid)
        if not (_vec(camera.get("position"), 3) and _vec(camera.get("aim"), 3)):
            errors.append(f"{label}: position and aim must be [x, y, z]")
            continue
        px, _, pz = camera["position"]
        if math.hypot(camera["aim"][0] - px, camera["aim"][2] - pz) < 1e-6:
            errors.append(f"{label}: aim has no horizontal direction from the position")
        if not (_is_num(camera.get("lens_mm")) and 8.0 <= camera["lens_mm"] <= 400.0):
            errors.append(f"{label}: lens_mm must be within 8 to 400")
        if "sensor_width_mm" in camera and not (_is_num(camera["sensor_width_mm"]) and camera["sensor_width_mm"] >= MIN_SENSOR_MM):
            errors.append(f"{label}: sensor_width_mm must be a finite number of at least {MIN_SENSOR_MM} (Blender's minimum)")
        if not (bx[0] <= px <= bx[1] and bz[0] <= pz <= bz[1]):
            warnings.append(f"{label}: stands outside the set bounds")
    active = spec.get("active_camera")
    if cameras and active is None:
        warnings.append("spec: no active_camera; the first camera will be used")
    elif active is not None and not (isinstance(active, str) and active in camera_ids):
        errors.append(f"spec: active_camera {active!r} is not a defined camera")
    if not cameras:
        warnings.append("spec: no cameras defined")
    _check_caches(spec, errors)
    return {"errors": errors, "warnings": warnings}


def _name_bytes(name: str) -> int:
    """Blender limits names by their UTF-8 byte length, not by character count."""
    return len(name.encode("utf-8"))


def band_object_name(element_id: str, band_name: str) -> str:
    return f"{element_id}.{band_name.replace(' ', '_')}"


def _arch_errors(element: Mapping[str, Any], label: str, materials, errors: list[str]) -> bool:
    problems = len(errors)
    if not (_positive(element.get("outer_radius")) and _positive(element.get("depth"))):
        errors.append(f"{label}: outer_radius and depth must be positive")
        return False
    step = element.get("step_depth", 0.0)
    if not (_is_num(step) and step >= 0):
        errors.append(f"{label}: step_depth must be a number >= 0")
        return False
    if "segments" in element and not _int_at_least(element["segments"], 8):
        errors.append(f"{label}: segments must be an integer >= 8")
        return False
    bands = element.get("bands")
    if not isinstance(bands, list) or not bands:
        errors.append(f"{label}: bands must be a non-empty list")
        return False
    names: set[str] = set()
    total = 0.0
    for index, band in enumerate(bands):
        name = band.get("name") if isinstance(band, dict) else None
        key = band_object_name(element["id"], name) if isinstance(name, str) else None
        if not isinstance(name, str) or not name or key in names:
            errors.append(f"{label}: band {index} needs a unique name")
            continue
        if _name_bytes(key) > BLENDER_NAME_LIMIT:
            errors.append(f"{label}: band {name!r} makes an object name longer than {BLENDER_NAME_LIMIT} bytes (UTF-8)")
            continue
        names.add(key)
        if not _positive(band.get("width")):
            errors.append(f"{label}: band {name} width must be positive")
            continue
        total += band["width"]
        colour = band.get("color")
        if not (isinstance(colour, str) and (HEX.match(colour) or colour in materials)):
            errors.append(f"{label}: band {name} color must be a hex string or a defined material")
        if element["depth"] - index * step <= 1e-9:
            errors.append(f"{label}: band {name} has no depth left after {index} reveals of {step:g} m")
    if total >= element["outer_radius"] - 1e-9:
        errors.append(f"{label}: band widths sum to {total:g} m, leaving no opening inside radius {element['outer_radius']:g} m")
    return len(errors) == problems


def _v11_element_errors(element: Mapping[str, Any], label: str, errors: list[str]) -> bool:
    """Shape checks for the kinds added in v1.1 (sphere, cone, torus, stairs)."""
    kind = element["kind"]
    problems = len(errors)
    if kind == "sphere":
        if not _positive(element.get("radius")):
            errors.append(f"{label}: radius must be positive")
        if "rings" in element and not _int_at_least(element["rings"], 4):
            errors.append(f"{label}: rings must be an integer >= 4")
    elif kind == "cone":
        if not (_positive(element.get("radius")) and _positive(element.get("height"))):
            errors.append(f"{label}: radius and height must be positive")
        if "top_radius" in element and not (_is_num(element["top_radius"]) and element["top_radius"] >= 0):
            errors.append(f"{label}: top_radius must be a number >= 0 (0 makes a point)")
    elif kind == "torus":
        major, minor = element.get("major_radius"), element.get("minor_radius")
        if not (_positive(major) and _positive(minor)):
            errors.append(f"{label}: major_radius and minor_radius must be positive")
        elif minor >= major:
            errors.append(f"{label}: minor_radius {minor:g} m must be smaller than major_radius {major:g} m")
        if "tube_segments" in element and not _int_at_least(element["tube_segments"], 4):
            errors.append(f"{label}: tube_segments must be an integer >= 4")
    else:
        if not (_vec(element.get("size"), 3) and all(v > 0 for v in element["size"])):
            errors.append(f"{label}: size must be [width, height, depth], all positive")
        steps = element.get("steps")
        if not (_int_at_least(steps, 2) and steps <= MAX_STEPS):
            errors.append(f"{label}: steps must be an integer from 2 to {MAX_STEPS}")
    if kind != "stairs" and "segments" in element and not _int_at_least(element["segments"], 8):
        errors.append(f"{label}: segments must be an integer >= 8")
    return len(errors) == problems


# --- geometry ---------------------------------------------------------------------------------

def _box_mesh(width: float, height: float, depth: float) -> dict[str, Any]:
    """A box with its base on z=0 in Blender coordinates (width x, depth y, height z)."""
    hx, hy = width / 2.0, depth / 2.0
    verts = [[-hx, -hy, 0.0], [hx, -hy, 0.0], [hx, hy, 0.0], [-hx, hy, 0.0],
             [-hx, -hy, height], [hx, -hy, height], [hx, hy, height], [-hx, hy, height]]
    faces = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]
    return {"verts": verts, "faces": faces}


def _cylinder_mesh(radius: float, height: float, segments: int) -> dict[str, Any]:
    verts, faces = [], []
    for z in (0.0, height):
        for i in range(segments):
            a = 2.0 * math.pi * i / segments
            verts.append([round(radius * math.cos(a), 9), round(radius * math.sin(a), 9), z])
    for i in range(segments):
        j = (i + 1) % segments
        faces.append([i, j, segments + j, segments + i])
    faces.append(list(reversed(range(segments))))
    faces.append([segments + i for i in range(segments)])
    return {"verts": verts, "faces": faces}


def _arch_ring_mesh(inner: float, outer: float, depth: float, segments: int) -> dict[str, Any]:
    """Half annulus (the upper half, an arch) extruded along +y (spec depth) from y=0 to ``depth``.

    The arch sits in the x-z plane of Blender coordinates with its springline on z=0.
    """
    verts, faces = [], []
    for y in (0.0, depth):
        for radius in (outer, inner):
            for i in range(segments + 1):
                a = math.pi * i / segments
                verts.append([round(radius * math.cos(a), 9), y, round(radius * math.sin(a), 9)])
    n = segments + 1
    # layout: [front outer][front inner][back outer][back inner], each n verts
    fo, fi, bo, bi = 0, n, 2 * n, 3 * n
    for i in range(segments):
        faces.append([fo + i, fo + i + 1, fi + i + 1, fi + i])          # front reveal
        faces.append([bo + i, bi + i, bi + i + 1, bo + i + 1])          # back
        faces.append([fo + i, bo + i, bo + i + 1, fo + i + 1])          # outer surface
        faces.append([fi + i, fi + i + 1, bi + i + 1, bi + i])          # inner surface (soffit)
    faces.append([fo, fi, bi, bo])                                      # right end cap
    faces.append([fo + segments, bo + segments, bi + segments, fi + segments])  # left end cap
    return {"verts": verts, "faces": faces}


def _turn_to_axis(mesh: dict[str, Any], axis: str) -> dict[str, Any]:
    """Re-aim a mesh built around local Z onto a spec axis, as a cyclic permutation (winding is preserved).

    Spec ``y`` (up) is Blender Z, spec ``x`` is Blender X and spec ``z`` (depth) is Blender Y.  The mesh is
    then grounded: centred on x and y, lowest point on z = 0, so ``y`` in the spec stays the underside.
    """
    verts = mesh["verts"]
    if axis == "x":
        verts = [[c, a, b] for a, b, c in verts]
    elif axis == "z":
        verts = [[b, c, a] for a, b, c in verts]
    xs, ys, zs = zip(*verts)
    cx, cy, lo = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, min(zs)
    return {"verts": [[round(x - cx, 9), round(y - cy, 9), round(z - lo, 9)] for x, y, z in verts], "faces": mesh["faces"]}


def _frustum_mesh(radius: float, top_radius: float, height: float, segments: int) -> dict[str, Any]:
    """Cone frustum around local Z from z=0 to ``height``; ``top_radius`` 0 closes it to a point."""
    verts = [[round(radius * math.cos(2.0 * math.pi * i / segments), 9), round(radius * math.sin(2.0 * math.pi * i / segments), 9), 0.0]
             for i in range(segments)]
    faces = [list(reversed(range(segments)))]
    if top_radius > 0:
        verts += [[round(top_radius * math.cos(2.0 * math.pi * i / segments), 9),
                   round(top_radius * math.sin(2.0 * math.pi * i / segments), 9), height] for i in range(segments)]
        for i in range(segments):
            j = (i + 1) % segments
            faces.append([i, j, segments + j, segments + i])
        faces.append([segments + i for i in range(segments)])
    else:
        verts.append([0.0, 0.0, height])
        for i in range(segments):
            faces.append([i, (i + 1) % segments, segments])
    return {"verts": verts, "faces": faces}


def _sphere_mesh(radius: float, segments: int, rings: int) -> dict[str, Any]:
    """UV sphere with poles on local Z: ``rings`` bands of latitude, ``segments`` around."""
    verts = [[0.0, 0.0, -radius]]
    for k in range(1, rings):
        phi = math.pi * k / rings
        ring = radius * math.sin(phi)
        for i in range(segments):
            a = 2.0 * math.pi * i / segments
            verts.append([round(ring * math.cos(a), 9), round(ring * math.sin(a), 9), round(-radius * math.cos(phi), 9)])
    verts.append([0.0, 0.0, radius])
    top = len(verts) - 1
    faces = []
    for i in range(segments):
        j = (i + 1) % segments
        faces.append([0, 1 + j, 1 + i])
    for k in range(rings - 2):
        lo, hi = 1 + k * segments, 1 + (k + 1) * segments
        for i in range(segments):
            j = (i + 1) % segments
            faces.append([lo + i, lo + j, hi + j, hi + i])
    last = 1 + (rings - 2) * segments
    for i in range(segments):
        j = (i + 1) % segments
        faces.append([top, last + i, last + j])
    return {"verts": verts, "faces": faces}


def _torus_mesh(major: float, minor: float, segments: int, tube_segments: int) -> dict[str, Any]:
    """Ring around local Z (the hole axis): ``segments`` around the ring, ``tube_segments`` around the tube."""
    verts = []
    for i in range(segments):
        theta = 2.0 * math.pi * i / segments
        for j in range(tube_segments):
            phi = 2.0 * math.pi * j / tube_segments
            reach = major + minor * math.cos(phi)
            verts.append([round(reach * math.cos(theta), 9), round(reach * math.sin(theta), 9), round(minor * math.sin(phi), 9)])
    faces = []
    for i in range(segments):
        i2 = (i + 1) % segments
        for j in range(tube_segments):
            j2 = (j + 1) % tube_segments
            faces.append([i * tube_segments + j, i2 * tube_segments + j, i2 * tube_segments + j2, i * tube_segments + j2])
    return {"verts": verts, "faces": faces}


def _stairs_mesh(width: float, height: float, depth: float, steps: int) -> dict[str, Any]:
    """A solid flight rising toward +y (spec depth): ``steps`` equal treads, base on z=0, centred on x and y."""
    run, rise = depth / steps, height / steps
    profile = [(0.0, 0.0), (depth, 0.0)]
    for i in range(steps - 1, -1, -1):
        profile += [((i + 1) * run, (i + 1) * rise), (i * run, (i + 1) * rise)]
    n, hx = len(profile), width / 2.0
    verts = [[-hx, round(y - depth / 2.0, 9), round(z, 9)] for y, z in profile] + [[hx, round(y - depth / 2.0, 9), round(z, 9)] for y, z in profile]
    faces = [list(reversed(range(n))), [n + k for k in range(n)]]                  # left cap, right cap
    for k in range(n):
        k2 = (k + 1) % n
        faces.append([k, k2, n + k2, n + k])
    return {"verts": verts, "faces": faces}


def mesh_volume(mesh: Mapping[str, Any]) -> float:
    """Signed volume of a closed mesh with outward winding (positive when the faces point outward)."""
    verts, total = mesh["verts"], 0.0
    for face in mesh["faces"]:
        a = verts[face[0]]
        for k in range(1, len(face) - 1):
            b, c = verts[face[k]], verts[face[k + 1]]
            total += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
    return round(total, 6)


def world_bounds(obj: Mapping[str, Any]) -> dict[str, list[float]]:
    """Axis-aligned bounds of a mesh object after its Z rotation and location, in Blender coordinates."""
    angle = math.radians(obj["rotation_deg"])
    c, s = math.cos(angle), math.sin(angle)
    lx, ly, lz = obj["location"]
    pts = [(lx + x * c - y * s, ly + x * s + y * c, lz + z) for x, y, z in obj["mesh"]["verts"]]
    xs, ys, zs = zip(*pts)
    return {"min": [round(min(xs), 6), round(min(ys), 6), round(min(zs), 6)],
            "max": [round(max(xs), 6), round(max(ys), 6), round(max(zs), 6)]}


def mesh_bounds(mesh: Mapping[str, Any]) -> dict[str, list[float]]:
    xs, ys, zs = zip(*mesh["verts"])
    return {"min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)],
            "dimensions": [round(max(xs) - min(xs), 6), round(max(ys) - min(ys), 6), round(max(zs) - min(zs), 6)]}


def _blender(point) -> list[float]:
    return [point[0], point[2], point[1]]


def _material_for(name: str | None, colour: str | None, materials: dict[str, Any]) -> str:
    """Return the material name; band colours given as hex become derived materials."""
    if name is not None:
        return name
    key = f"{GENERATED_PREFIX}{colour.upper()}"
    materials.setdefault(key, {"color": colour.upper(), "roughness": 0.6, "emission": 0.0})
    return key


def scene_graph(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Build the deterministic scene graph for a valid spec; raises ``SpecError`` otherwise."""
    report = validate_spec(spec)
    if report["errors"]:
        raise SpecError(report["errors"])
    bx, bz = spec["bounds"]["x"], spec["bounds"]["z"]
    materials: dict[str, Any] = {n: {"color": m["color"].upper(), "roughness": m.get("roughness", 0.5),
                                      "emission": m.get("emission", 0.0)} for n, m in spec.get("materials", {}).items()}
    objects: list[dict[str, Any]] = []
    floor = spec.get("floor", {})
    floor_material = floor.get("material")
    if floor_material is None:
        floor_material = FLOOR_DEFAULT
        materials.setdefault(floor_material, {"color": "808080", "roughness": 0.9, "emission": 0.0})
    thickness = 0.05
    floor_mesh = _box_mesh(bx[1] - bx[0], thickness, bz[1] - bz[0])
    objects.append({"name": "FLOOR", "type": "mesh", "role": "architecture", "collection": "Architecture",
                    "mesh": floor_mesh, "location": [(bx[0] + bx[1]) / 2.0, (bz[0] + bz[1]) / 2.0, -thickness],
                    "rotation_deg": 0.0, "materials": [floor_material], "face_materials": None})
    for element in spec["elements"]:
        kind, eid = element["kind"], element["id"]
        cx, cz = element["center"]
        base = element.get("y", 0.0)
        segments = element.get("segments", DEFAULT_SEGMENTS)
        if kind in V11_KINDS or (kind == "cylinder" and element.get("axis", "y") != "y"):
            axis = element.get("axis", "y")
            if kind == "cylinder":
                mesh = _turn_to_axis(_cylinder_mesh(element["radius"], element["height"], segments), axis)
            elif kind == "cone":
                mesh = _turn_to_axis(_frustum_mesh(element["radius"], element.get("top_radius", 0.0), element["height"], segments), axis)
            elif kind == "sphere":
                mesh = _turn_to_axis(_sphere_mesh(element["radius"], segments, element.get("rings", DEFAULT_RINGS)), "y")
            elif kind == "torus":
                mesh = _turn_to_axis(_torus_mesh(element["major_radius"], element["minor_radius"], segments,
                                                 element.get("tube_segments", DEFAULT_TUBE_SEGMENTS)), axis)
            else:
                mesh = _stairs_mesh(*element["size"], element["steps"])
            objects.append({"name": eid, "type": "mesh", "role": element.get("role", "architecture"),
                            "collection": "Architecture" if element.get("role", "architecture") == "architecture" else "Dressing",
                            "mesh": mesh, "location": [cx, cz, base], "rotation_deg": element.get("rotation_deg", 0.0),
                            "materials": [element["material"]], "face_materials": None})
        elif kind == "box":
            mesh = _box_mesh(*element["size"])
            objects.append({"name": eid, "type": "mesh", "role": element.get("role", "architecture"),
                            "collection": "Architecture" if element.get("role", "architecture") == "architecture" else "Dressing",
                            "mesh": mesh, "location": [cx, cz, base], "rotation_deg": element.get("rotation_deg", 0.0),
                            "materials": [element["material"]], "face_materials": None})
        elif kind == "cylinder":
            mesh = _cylinder_mesh(element["radius"], element["height"], segments)
            objects.append({"name": eid, "type": "mesh", "role": element.get("role", "architecture"),
                            "collection": "Architecture" if element.get("role", "architecture") == "architecture" else "Dressing",
                            "mesh": mesh, "location": [cx, cz, base], "rotation_deg": element.get("rotation_deg", 0.0),
                            "materials": [element["material"]], "face_materials": None})
        else:
            step = element.get("step_depth", 0.0)
            cursor = element["outer_radius"]
            front = cz - element["depth"] / 2.0
            for index, band in enumerate(element["bands"]):
                outer, inner = cursor, cursor - band["width"]
                cursor = inner
                depth = element["depth"] - index * step
                mesh = _arch_ring_mesh(inner, outer, depth, segments)
                colour = band["color"]
                material = colour if colour in element_materials(spec) else None
                material = _material_for(material, colour, materials)
                soffit = f"{material}{SOFFIT_SUFFIX}"
                base_colour = materials[material]["color"]
                materials.setdefault(soffit, {"color": _darken(base_colour, 0.55), "roughness": 0.7, "emission": 0.0})
                # per-face material index: front, back and outer use the face material, inner the soffit
                face_materials = []
                for _ in range(segments):
                    face_materials.extend([0, 0, 0, 1])
                face_materials.extend([0, 0])                 # the two end caps
                turn = math.radians(element.get("rotation_deg", 0.0))
                offset = front + index * step - cz              # the band's recess along the arch's own depth axis
                location = [round(cx - offset * math.sin(turn), 9), round(cz + offset * math.cos(turn), 9), base]
                objects.append({"name": band_object_name(eid, band["name"]), "type": "mesh", "role": "architecture",
                                "collection": "Architecture", "mesh": mesh,
                                "location": location, "rotation_deg": element.get("rotation_deg", 0.0),
                                "materials": [material, soffit], "face_materials": face_materials,
                                "band_index": index, "arch": eid})
    for light in spec.get("lights", []):
        objects.append({"name": light["id"], "type": "light", "kind": light["kind"], "collection": "Lighting",
                        "location": _blender(light["position"]), "aim": _blender(light["aim"]) if "aim" in light else None,
                        "energy": light["energy"], "color": light.get("color", "FFFFFF").upper(),
                        "size": light.get("size", 1.0)})
    cameras = spec.get("cameras", [])
    for camera in cameras:
        objects.append({"name": camera["id"], "type": "camera", "collection": "Cameras",
                        "location": _blender(camera["position"]), "aim": _blender(camera["aim"]),
                        "lens_mm": camera["lens_mm"], "sensor_width_mm": camera.get("sensor_width_mm", 36.0)})
    active = spec.get("active_camera") or (cameras[0]["id"] if cameras else None)
    graph = {"schema": GRAPH_SCHEMA, "spec_sha256": spec_hash(spec), "title": spec.get("title", "set"),
             "units": "metres", "up": "Z", "active_camera": active,
             "materials": {name: {**m, "linear": hex_to_linear(m["color"])} for name, m in sorted(materials.items())},
             "objects": objects, "warnings": report["warnings"]}
    graph["graph_sha256"] = hashlib.sha256(canonical({k: v for k, v in graph.items() if k != "graph_sha256"}).encode()).hexdigest()
    return graph


def element_materials(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    return spec.get("materials", {})


def _darken(colour: str, factor: float) -> str:
    return "".join(f"{round(int(colour[i:i + 2], 16) * factor):02X}" for i in (0, 2, 4))


class SpecError(ValueError):
    """Raised with every failed check, one per line."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


def graph_summary(graph: Mapping[str, Any]) -> dict[str, Any]:
    """Counts and per-object extents that a Blender run must reproduce."""
    rows = []
    for obj in graph["objects"]:
        if obj["type"] == "mesh":
            bounds = mesh_bounds(obj["mesh"])
            rows.append({"name": obj["name"], "type": "mesh", "vertices": len(obj["mesh"]["verts"]),
                         "faces": len(obj["mesh"]["faces"]), "dimensions": bounds["dimensions"], "location": obj["location"],
                         "materials": obj["materials"], "world_bounds": world_bounds(obj),
                         "volume": mesh_volume(obj["mesh"])})
        else:
            rows.append({"name": obj["name"], "type": obj["type"], "location": obj["location"]})
    return {"graph_sha256": graph["graph_sha256"], "objects": rows,
            "mesh_count": sum(1 for o in graph["objects"] if o["type"] == "mesh"),
            "light_count": sum(1 for o in graph["objects"] if o["type"] == "light"),
            "camera_count": sum(1 for o in graph["objects"] if o["type"] == "camera"),
            "active_camera": graph["active_camera"]}


def shot_plan_set(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The ``set`` block of an ``amps.shot_plan.v1`` plan for this environment."""
    report = validate_spec(spec)
    if report["errors"]:
        raise SpecError(report["errors"])
    elements = []
    for element in spec["elements"]:
        row = {"id": element["id"], "center": list(element["center"]), "radius": element_radius(element)}
        if element.get("added"):
            row["added"] = True
        elements.append(row)
    return {"id": spec.get("id", "set"), "bounds": {"x": list(spec["bounds"]["x"]), "z": list(spec["bounds"]["z"])},
            "elements": elements, "caches": [dict(c) for c in spec.get("caches", [])]}


def synthetic_spec(version: str = "1") -> dict[str, Any]:
    """A fully synthetic archway hall: invented dimensions, colours and names, no private source.

    ``version="1.1"`` adds one of every v1.1 primitive (horizontal cylinders, cone, sphere, torus, stairs).
    """
    if version not in ("1", "1.1"):
        raise ValueError("version must be '1' or '1.1'")
    spec = _synthetic_v1()
    if version == "1.1":
        spec["schema"], spec["id"], spec["title"] = SCHEMA_V11, "synthetic-archway-hall-v11", "Synthetic archway hall with v1.1 primitives"
        spec["elements"] += [
            {"id": "rail", "kind": "cylinder", "axis": "x", "center": [0.0, -2.0], "radius": 0.12, "height": 4.0, "y": 0.9,
             "material": "wood", "segments": 24, "role": "dressing"},
            {"id": "post", "kind": "cylinder", "axis": "z", "center": [4.5, 0.0], "radius": 0.15, "height": 3.0, "y": 1.0,
             "material": "wood", "segments": 24, "role": "dressing"},
            {"id": "obelisk", "kind": "cone", "center": [4.5, 3.5], "radius": 0.6, "top_radius": 0.1, "height": 2.4,
             "material": "stone", "segments": 24},
            {"id": "finial", "kind": "sphere", "center": [4.5, 3.5], "radius": 0.35, "y": 2.4, "material": "ember",
             "segments": 24, "rings": 12, "role": "dressing"},
            {"id": "hoop", "kind": "torus", "axis": "z", "center": [-4.0, 4.0], "major_radius": 1.2, "minor_radius": 0.1, "y": 1.0,
             "material": "wall", "segments": 32, "tube_segments": 8, "role": "dressing"},
            {"id": "steps", "kind": "stairs", "center": [0.0, 3.8], "size": [2.4, 0.9, 1.6], "steps": 4, "material": "stone"},
            {"id": "steps-turned", "kind": "stairs", "center": [-4.5, -3.0], "size": [2.0, 0.6, 1.2], "steps": 3,
             "rotation_deg": 90.0, "material": "stone"},
        ]
    return spec


def _synthetic_v1() -> dict[str, Any]:
    return {
        "schema": SCHEMA, "id": "synthetic-archway-hall", "title": "Synthetic archway hall",
        "bounds": {"x": [-6.0, 6.0], "z": [-6.0, 8.0], "height": 7.0},
        "floor": {"material": "stone"},
        "materials": {"stone": {"color": "6E6A64", "roughness": 0.85},
                      "wall": {"color": "B8AFA0", "roughness": 0.9},
                      "wood": {"color": "7A4B2A", "roughness": 0.55},
                      "ember": {"color": "FF7A1A", "roughness": 0.4, "emission": 6.0}},
        "elements": [
            {"id": "back-wall", "kind": "box", "center": [0.0, 7.5], "size": [12.0, 6.0, 1.0], "material": "wall"},
            {"id": "arch", "kind": "arch_bands", "center": [0.0, 6.5], "outer_radius": 3.2, "depth": 0.9,
             "step_depth": 0.1, "segments": 24,
             "bands": [{"name": "Band A", "color": "3C6E8F", "width": 0.5},
                       {"name": "Band B", "color": "4F9D8A", "width": 0.45},
                       {"name": "Band C", "color": "D9B24C", "width": 0.4},
                       {"name": "Band D", "color": "C2603A", "width": 0.35}], "added": True},
            {"id": "table", "kind": "cylinder", "center": [0.0, 1.0], "radius": 1.1, "height": 0.9,
             "material": "wood", "segments": 32, "role": "dressing"},
            {"id": "brazier", "kind": "cylinder", "center": [0.0, 1.0], "radius": 0.25, "height": 0.15, "y": 0.9,
             "material": "ember", "segments": 16, "role": "dressing", "added": True},
            {"id": "bench", "kind": "box", "center": [-3.2, 1.0], "size": [0.5, 0.45, 2.4], "material": "wood",
             "role": "dressing"},
        ],
        "lights": [{"id": "key", "kind": "area", "position": [3.0, 5.0, -2.0], "aim": [0.0, 1.0, 1.0], "energy": 400.0,
                    "size": 2.0, "color": "FFF2DD"},
                   {"id": "glow", "kind": "point", "position": [0.0, 1.4, 1.0], "energy": 60.0, "color": "FF8A33"}],
        "cameras": [{"id": "cam-wide", "position": [0.0, 1.6, -5.0], "aim": [0.0, 2.2, 6.0], "lens_mm": 24.0},
                    {"id": "cam-side", "position": [-5.0, 1.5, 0.0], "aim": [0.0, 1.0, 1.0], "lens_mm": 35.0}],
        "active_camera": "cam-wide",
    }
