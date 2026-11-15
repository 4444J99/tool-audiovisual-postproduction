"""Burn caption cues into synthetic frames, hash every frame into a chain, and re-derive them to verify.

The cues come from a written SRT or WebVTT file (``captions.parse``), never from the generator's memory, so a
file changed after it was laid out burns in what the file now says.  Frames are 8-bit luma (``gray``), row by
row, written back to back into one ``frames.y8`` file.  Everything is integer arithmetic over an embedded 5x7
bitmap font, so the same inputs give the same bytes on any machine; no ffmpeg subtitle filter, system font or
image library takes part (the installed ffmpeg has neither ``subtitles`` nor ``drawtext``, and a rasteriser's
output would change with its version).  ``tools/caption_burnin.py preview`` optionally wraps the frames in a
lossless FFV1 file with ffmpeg; that file is a convenience and is not hashed or trusted.

Timing: a cue is on screen for frames ``start <= f < end`` (SRT's end stamp is the first frame it is gone), so
the frame grid of ``captions.py`` and the burned frames agree by construction.

The hash chain: ``h0 = sha256(canonical(params))`` where ``params`` pins the geometry, the frame rate, the
background segments, the SRT hash and the font hash; ``h_i = sha256(h_{i-1} || sha256(frame_i))``.  The head
therefore commits to every pixel of every frame *and* to the settings that produced them, and one flipped
pixel changes every later link.  ``verify`` does not trust the recorded digests: it re-renders every frame
from the SRT and the params and compares it with the bytes on disk, so a file, a digest list and a head that
were all edited together are still caught, and the first differing frame is named.

The background is an invented moving test pattern, tinted per segment (the pipeline passes one segment per
shot, seeded by the shot's declared plate identity).  No private media is read.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

from . import captions as cap

SCHEMA = "amps.caption_burnin.v1"
FRAMES_FILE = "frames.y8"

GEOMETRY_DEFAULT = {"width": 320, "height": 180, "scale": 1, "margin": 8, "pad": 3, "bar_percent": 35}

# Classic 5x7 font, ASCII 32..126, five column bytes per glyph, least significant bit at the top
# (bit 7 is the descender row of g, p, q, y and the comma).  Whitespace and '|' in the table are not significant.
_FONT_ROWS = """
00 00 00 00 00|00 00 5F 00 00|00 07 00 07 00|14 7F 14 7F 14|24 2A 7F 2A 12|23 13 08 64 62|36 49 56 20 50|00 08 07 03 00
00 1C 22 41 00|00 41 22 1C 00|2A 1C 7F 1C 2A|08 08 3E 08 08|00 80 70 30 00|08 08 08 08 08|00 00 60 60 00|20 10 08 04 02
3E 51 49 45 3E|00 42 7F 40 00|72 49 49 49 46|21 41 49 4D 33|18 14 12 7F 10|27 45 45 45 39|3C 4A 49 49 31|41 21 11 09 07
36 49 49 49 36|46 49 49 29 1E|00 00 14 00 00|00 40 34 00 00|00 08 14 22 41|14 14 14 14 14|00 41 22 14 08|02 01 59 09 06
3E 41 5D 59 4E|7C 12 11 12 7C|7F 49 49 49 36|3E 41 41 41 22|7F 41 41 41 3E|7F 49 49 49 41|7F 09 09 09 01|3E 41 41 51 73
7F 08 08 08 7F|00 41 7F 41 00|20 40 41 3F 01|7F 08 14 22 41|7F 40 40 40 40|7F 02 1C 02 7F|7F 04 08 10 7F|3E 41 41 41 3E
7F 09 09 09 06|3E 41 51 21 5E|7F 09 19 29 46|26 49 49 49 32|03 01 7F 01 03|3F 40 40 40 3F|1F 20 40 20 1F|3F 40 38 40 3F
63 14 08 14 63|03 04 78 04 03|61 59 49 4D 43|00 7F 41 41 41|02 04 08 10 20|00 41 41 41 7F|04 02 01 02 04|40 40 40 40 40
00 03 07 08 00|20 54 54 78 40|7F 28 44 44 38|38 44 44 44 28|38 44 44 28 7F|38 54 54 54 18|00 08 7E 09 02|18 A4 A4 9C 78
7F 08 04 04 78|00 44 7D 40 00|20 40 40 3D 00|7F 10 28 44 00|00 41 7F 40 00|7C 04 78 04 78|7C 08 04 04 78|38 44 44 44 38
FC 18 24 24 18|18 24 24 18 FC|7C 08 04 04 08|48 54 54 54 24|04 04 3F 44 24|3C 40 40 20 7C|1C 20 40 20 1C|3C 40 30 40 3C
44 28 10 28 44|4C 90 90 90 7C|44 64 54 4C 44|00 08 36 41 00|00 00 77 00 00|00 41 36 08 00|02 01 02 04 02
"""
_FONT_HEX = "".join(_FONT_ROWS.replace("|", "").split())
assert len(_FONT_HEX) == 95 * 10, len(_FONT_HEX)
GLYPHS: dict[str, tuple[int, ...]] = {chr(32 + i): tuple(int(_FONT_HEX[i * 10 + 2 * c:i * 10 + 2 * c + 2], 16) for c in range(5))
                                      for i in range(95)}
FONT_SHA256 = hashlib.sha256(json.dumps({c: list(g) for c, g in sorted(GLYPHS.items())}, sort_keys=True).encode()).hexdigest()
GLYPH_W, GLYPH_H, ADVANCE, LEADING = 5, 8, 6, 2          # advance and leading are in font pixels, before scaling
REPLACEMENTS = {"…": "...", "—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', " ": " "}


class BurnError(ValueError):
    def __init__(self, errors: Sequence[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


# ------------------------------------------------------------------------------------------ parameters

def normalize_geometry(geometry: Mapping[str, Any] | None) -> dict[str, int]:
    out = dict(GEOMETRY_DEFAULT) | dict(geometry or {})
    unknown = sorted(set(out) - set(GEOMETRY_DEFAULT))
    errors = [f"unknown geometry key {k!r}" for k in unknown]
    for key in GEOMETRY_DEFAULT:
        value = out.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < (0 if key in ("margin", "pad") else 1):
            errors.append(f"geometry {key} must be a whole number >= {0 if key in ('margin', 'pad') else 1}")
    if not errors and out["bar_percent"] > 100:
        errors.append("geometry bar_percent must be at most 100")
    if not errors and (out["width"] > 4096 or out["height"] > 4096):
        errors.append("geometry width and height are limited to 4096")
    if errors:
        raise BurnError(errors)
    return {k: out[k] for k in GEOMETRY_DEFAULT}


def normalize_segments(segments: Sequence[Mapping[str, Any]] | None, frames: int) -> list[dict[str, Any]]:
    """Background segments ``{"start", "end", "seed"}`` covering ``[0, frames)`` exactly, in order, without overlap."""
    if not segments:
        return [{"start": 0, "end": frames, "seed": sha256_bytes(b"caption-burnin:default")}]
    out, at, errors = [], 0, []
    for i, seg in enumerate(segments):
        start, end, seed = seg.get("start"), seg.get("end"), seg.get("seed")
        if not (isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool) and not isinstance(end, bool)):
            errors.append(f"segment {i + 1}: start and end must be whole frames")
            continue
        if not (isinstance(seed, str) and len(seed) >= 2 and all(c in "0123456789abcdef" for c in seed)):
            errors.append(f"segment {i + 1}: seed must be a lowercase hex string")
            continue
        if start != at or end <= start:
            errors.append(f"segment {i + 1}: frames {start}-{end} do not continue from frame {at}")
            continue
        out.append({"start": start, "end": end, "seed": seed})
        at = end
    if not errors and at != frames:
        errors.append(f"segments cover {at} frames, not {frames}")
    if errors:
        raise BurnError(errors)
    return out


def make_params(srt_text: str, fps: Any, frames: int, geometry: Mapping[str, Any] | None = None,
                segments: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    try:
        rate = cap.parse_fps(fps)
    except cap.CaptionError as error:
        raise BurnError(f"fps: {error}") from error
    if not isinstance(frames, int) or isinstance(frames, bool) or frames < 1 or frames > 100_000:
        raise BurnError("frames must be a whole number between 1 and 100000")
    return {"schema": SCHEMA, "pixel_format": "gray", "fps": str(rate), "frames": frames, "geometry": normalize_geometry(geometry),
            "segments": normalize_segments(segments, frames), "srt_sha256": cap.sha256_text(srt_text), "font_sha256": FONT_SHA256}


# ------------------------------------------------------------------------------------------ pixels

def background(frame: int, width: int, height: int, seed: str) -> np.ndarray:
    """An invented test pattern: a seed-tinted ramp, a bar sweeping right 3 px per frame and a column tick at the frame index."""
    tint = 40 + int(seed[:2], 16) % 80
    y, x = np.mgrid[0:height, 0:width]
    img = (tint + (x * 2 + y) * 60 // (2 * width + height)).astype(np.int32)
    sweep = (frame * 3) % width
    img[:, sweep:sweep + 4] += 70
    img[0:3, (frame % width):(frame % width) + 1] = 255
    return np.clip(img, 0, 255).astype(np.uint8)


def glyph_rows(char: str) -> list[int]:
    cols = GLYPHS[char]
    return [sum(((cols[c] >> r) & 1) << c for c in range(5)) for r in range(8)]


def unsupported(lines: Sequence[str]) -> list[str]:
    return sorted({c for line in lines for c in prepare_line(line) if c not in GLYPHS})


def prepare_line(line: str) -> str:
    return "".join(REPLACEMENTS.get(c, c) for c in line)


def text_size(lines: Sequence[str], scale: int) -> tuple[int, int]:
    prepared = [prepare_line(line) for line in lines]
    width = max((len(line) * ADVANCE - (ADVANCE - GLYPH_W)) * scale for line in prepared)
    height = (len(prepared) * (GLYPH_H + LEADING) - LEADING) * scale
    return width, height


def caption_stamp(lines: Sequence[str], geometry: Mapping[str, int]) -> tuple[int, int, np.ndarray]:
    """(top, left, glyph mask) of the caption box: lines centred above the bottom margin; the mask is the whole box, True where a glyph pixel sits."""
    scale, pad, margin = geometry["scale"], geometry["pad"], geometry["margin"]
    text_w, text_h = text_size(lines, scale)
    box_w, box_h = text_w + 2 * pad, text_h + 2 * pad
    mask = np.zeros((box_h, box_w), dtype=bool)
    y = pad
    for line in lines:
        prepared = prepare_line(line)
        x = pad + (text_w - (len(prepared) * ADVANCE - (ADVANCE - GLYPH_W)) * scale) // 2
        for char in prepared:
            for r, bits in enumerate(glyph_rows(char)):
                for c in range(GLYPH_W):
                    if bits >> c & 1:
                        mask[y + r * scale:y + (r + 1) * scale, x + c * scale:x + (c + 1) * scale] = True
            x += ADVANCE * scale
        y += (GLYPH_H + LEADING) * scale
    return geometry["height"] - margin - box_h, (geometry["width"] - box_w) // 2, mask


def draw_caption(img: np.ndarray, stamp: tuple[int, int, np.ndarray], geometry: Mapping[str, int]) -> np.ndarray:
    """Darken the box behind the text to ``bar_percent`` (integer arithmetic) and set the glyph pixels to 255."""
    top, left, mask = stamp
    out = img.copy()
    box = out[top:top + mask.shape[0], left:left + mask.shape[1]]
    box[:] = (box.astype(np.int32) * geometry["bar_percent"] // 100).astype(np.uint8)
    box[mask] = 255
    return out


def active_cues(cues: Sequence[Mapping[str, Any]], frames: int, geometry: Mapping[str, int]) -> list[str]:
    """Every reason the cues cannot be burned into this frame count and size; empty when they can."""
    problems: list[str] = []
    spans = []
    for i, cue in enumerate(cues):
        tag = f"cue {cue.get('index', i + 1)}"
        start, end, lines = cue["start"], cue["end"], cue["lines"]
        if end <= start:
            problems.append(f"{tag}: ends at frame {end}, not after its start {start}")
            continue
        if start < 0 or end > frames:
            problems.append(f"{tag}: frames {start}-{end} are outside the {frames} frames rendered")
        if not lines or any(not line for line in lines):
            problems.append(f"{tag}: has no text to draw")
            continue
        bad = unsupported(lines)
        if bad:
            problems.append(f"{tag}: no glyph for {''.join(bad)!r}")
            continue
        text_w, text_h = text_size(lines, geometry["scale"])
        if text_w + 2 * geometry["pad"] > geometry["width"] or text_h + 2 * geometry["pad"] + geometry["margin"] > geometry["height"]:
            problems.append(f"{tag}: the text ({text_w}x{text_h} px) does not fit the {geometry['width']}x{geometry['height']} frame")
        spans.append((start, end, tag))
    spans.sort()
    for (_, end, a), (start, _, b) in zip(spans, spans[1:]):
        if start < end:
            problems.append(f"{b} starts at frame {start} before {a} ends at frame {end}; two captions would share a frame")
    return problems


def frame_stream(params: Mapping[str, Any], cues: Sequence[Mapping[str, Any]]) -> Iterator[bytes]:
    """The raw bytes of every frame, in order, derived only from ``params`` and ``cues``."""
    geometry = params["geometry"]
    problems = active_cues(cues, params["frames"], geometry)
    if problems:
        raise BurnError(problems)
    on_screen: dict[int, Mapping[str, Any]] = {}
    for cue in cues:
        for f in range(cue["start"], cue["end"]):
            on_screen[f] = cue
    stamps: dict[tuple[str, ...], tuple[int, int, np.ndarray]] = {}
    seg = iter(params["segments"])
    current = next(seg)
    for f in range(params["frames"]):
        while f >= current["end"]:
            current = next(seg)
        img = background(f, geometry["width"], geometry["height"], current["seed"])
        if f in on_screen:
            key = tuple(on_screen[f]["lines"])
            if key not in stamps:
                stamps[key] = caption_stamp(key, geometry)
            img = draw_caption(img, stamps[key], geometry)
        yield img.tobytes()


# ------------------------------------------------------------------------------------------ chain

def chain_seed(params: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical(params).encode())


def extend(link: str, frame_digest: str) -> str:
    return sha256_bytes(bytes.fromhex(link) + bytes.fromhex(frame_digest))


def chain_links(params: Mapping[str, Any], frame_digests: Sequence[str]) -> list[str]:
    links, link = [], chain_seed(params)
    for digest in frame_digests:
        link = extend(link, digest)
        links.append(link)
    return links


def burn(srt_text: str, fps: Any, frames: int, geometry: Mapping[str, Any] | None = None,
         segments: Sequence[Mapping[str, Any]] | None = None) -> tuple[bytes, dict[str, Any]]:
    """Burn the cues of ``srt_text`` (SRT or WebVTT) into ``frames`` frames; returns (frame bytes, report)."""
    params = make_params(srt_text, fps, frames, geometry, segments)
    try:
        cues = cap.parse(srt_text, params["fps"])
    except cap.CaptionError as error:
        raise BurnError([f"captions: {e}" for e in error.args[0]] if isinstance(error.args[0], list) else [f"captions: {error}"]) from error
    if not cues:
        raise BurnError("the caption file has no cues to burn in")
    payload = b"".join(frame_stream(params, cues))
    size = params["geometry"]["width"] * params["geometry"]["height"]
    digests = [sha256_bytes(payload[i * size:(i + 1) * size]) for i in range(frames)]
    links = chain_links(params, digests)
    on = sorted({f for c in cues for f in range(c["start"], c["end"])})
    report = {"schema": SCHEMA, "params": params, "frame_bytes": size, "frames_file": FRAMES_FILE, "frames_sha256": sha256_bytes(payload),
              "frame_sha256": digests, "chain_head": links[-1],
              "cues": [{"index": c["index"], "start": c["start"], "end": c["end"], "lines": c["lines"]} for c in cues],
              "captioned_frames": len(on),
              "note": "frames are 8-bit luma, row by row, one after another; a cue shows for start <= frame < end"}
    return payload, report


def verify(payload: bytes, report: Mapping[str, Any], srt_text: str) -> list[str]:
    """Re-render every frame from ``srt_text`` and the report's params and compare with ``payload``; empty when all agree.

    Recorded digests and head are checked as well, but they are never the reference: the frames are."""
    problems: list[str] = []
    try:
        params = report["params"]
        if params.get("schema") != SCHEMA:
            return [f"burn-in report: schema must be {SCHEMA}"]
        if params["font_sha256"] != FONT_SHA256:
            problems.append("burn-in report was made with a different font than this code carries")
        if params["srt_sha256"] != cap.sha256_text(srt_text):
            problems.append("caption file is not the one the frames were burned from (hash differs)")
        rebuilt = make_params(srt_text, params["fps"], params["frames"], params["geometry"], params["segments"])
        if rebuilt != params:
            problems.append("burn-in report params are not the canonical params for this caption file")
        cues = cap.parse(srt_text, params["fps"])
        size = params["geometry"]["width"] * params["geometry"]["height"]
        frames = params["frames"]
        if report["frame_bytes"] != size:
            problems.append(f"report frame size {report['frame_bytes']} is not {size}")
        if len(payload) != size * frames:
            problems.append(f"frames file holds {len(payload)} bytes, {size * frames} expected for {frames} frames")
        digests = report["frame_sha256"]
        if len(digests) != frames:
            problems.append(f"report lists {len(digests)} frame digests for {frames} frames")
        if problems and len(payload) != size * frames:
            return problems
        first_bad: list[int] = []
        for f, expected in enumerate(frame_stream(rebuilt, cues)):
            got = payload[f * size:(f + 1) * size]
            if got != expected:
                first_bad.append(f)
        if first_bad:
            span = f"{first_bad[0]}" if len(first_bad) == 1 else f"{first_bad[0]} (and {len(first_bad) - 1} more, last {first_bad[-1]})"
            problems.append(f"frame {span} differs from the frame re-rendered from the caption file")
        actual = [sha256_bytes(payload[i * size:(i + 1) * size]) for i in range(frames)]
        if len(digests) == frames:
            bad = [i for i in range(frames) if digests[i] != actual[i]]
            if bad:
                problems.append(f"recorded digest of frame {bad[0]} does not match the frames file ({len(bad)} differ)")
        links = chain_links(params, actual)
        if report["chain_head"] != links[-1]:
            problems.append("chain head does not match the frames file")
        if report["frames_sha256"] != sha256_bytes(payload):
            problems.append("recorded frames hash does not match the frames file")
        shown = sorted({f for c in cues for f in range(c["start"], c["end"])})
        if report["captioned_frames"] != len(shown):
            problems.append("captioned frame count does not match the caption file")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        problems.append(f"burn-in report cannot be checked ({type(error).__name__}: {error})")
    except cap.CaptionError as error:
        problems.append(f"captions: {error}")
    return problems


def ascii_art(frame: bytes, width: int, height: int, step: int = 1) -> str:
    """A rough text picture of one frame for eyeballing in a terminal."""
    ramp = " .:-=+*#%@"
    img = np.frombuffer(frame, dtype=np.uint8).reshape(height, width)
    return "\n".join("".join(ramp[int(v) * 9 // 255] for v in row[::step]) for row in img[::step])
