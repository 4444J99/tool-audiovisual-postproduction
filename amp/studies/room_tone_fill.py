"""Room-tone extraction and gap fill for edited dialogue.

Dialogue edits leave two kinds of silence.  A *pause* still carries the room's own sound; a *hole*
is digital silence where the tone was cut away with the words.  A hole is audible because the room
vanishes, so it is filled with room tone taken from the same recording.  This module does that in
three steps, all deterministic for a given seed:

1. ``find_holes`` finds runs of digital silence at sample accuracy; ``find_tone_regions`` finds
   steady quiet stretches (no speech, no hole) to borrow tone from.
2. ``build_bed`` assembles tone of any length from those regions without a perceptible loop: each
   splice is an equal-power crossfade at the offset where the two sides correlate best, and the
   same region is never used twice in a row while another exists.
3. ``fill_holes`` lays a patch over each hole, level-matched to the nearest real tone, with
   equal-power crossfades only where the neighbouring audio is itself quiet.  Speech is never
   faded.  Everything outside the patched zones stays bit-identical.

``fill_report`` states what was observed: remaining digital silence, untouched samples, patch level
against target, octave-band match to the source tone, and level continuity at the edges.  It does not
claim the result sounds right; it shows that the gap is gone and the tone matches in level and colour.

Audio is float64 with shape ``(samples,)`` or ``(samples, channels)``; sample rates are in Hz.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

import numpy as np

SCHEMA = "amps.room_tone_fill.v1"
EPS = 1e-6                      # digital silence threshold (about -120 dBFS)
OCTAVE_EDGES = (62.5, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0)


class ToneError(ValueError):
    """Raised when the audio cannot be processed (no tone to borrow, bad shape, bad parameters)."""


def _as_2d(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1:
        return audio[:, None]
    if audio.ndim != 2 or audio.shape[1] < 1:
        raise ToneError("audio must have shape (samples,) or (samples, channels)")
    return audio


def _db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-12))


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0


def audio_sha256(audio: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(np.asarray(audio, dtype="<f8")).tobytes()).hexdigest()


def find_holes(audio: np.ndarray, sr: int, min_hole_s: float = 0.05, eps: float = EPS) -> list[tuple[int, int]]:
    """Runs of digital silence on every channel, as ``(start, end)`` sample ranges, end exclusive."""
    a = _as_2d(audio)
    silent = np.all(np.abs(a) <= eps, axis=1)
    if not silent.any():
        return []
    padded = np.concatenate(([False], silent, [False]))
    change = np.flatnonzero(padded[1:] != padded[:-1])
    runs = list(zip(change[0::2].tolist(), change[1::2].tolist()))
    minimum = max(1, int(round(min_hole_s * sr)))
    return [(s, e) for s, e in runs if e - s >= minimum]


def find_tone_regions(audio: np.ndarray, sr: int, frame_s: float = 0.02, margin_db: float = 6.0,
                      min_region_s: float = 0.3, guard_s: float = 0.05,
                      eps: float = EPS) -> list[tuple[int, int]]:
    """Steady quiet stretches with no speech and no hole, as ``(start, end)`` sample ranges.

    The noise floor is the 10th percentile of frame level among non-silent frames.  A frame is tone
    when it lies within ``margin_db`` of that floor; a region must run at least ``min_region_s`` and is
    trimmed by ``guard_s`` at each end so speech tails and hole edges stay out.
    """
    a = _as_2d(audio)
    frame = max(1, int(round(frame_s * sr)))
    count = len(a) // frame
    if count == 0:
        return []
    # energy over every channel: opposite-polarity channels must not cancel speech into "tone"
    frames = a[:count * frame].reshape(count, frame, -1)
    levels = np.sqrt(np.mean(np.square(frames), axis=(1, 2)))
    silent = np.all(np.abs(a[:count * frame]).reshape(count, frame, -1) <= eps, axis=(1, 2))
    live = ~silent
    if not live.any():
        return []
    floor_db = _db(float(np.percentile(levels[live], 10)))
    db = 20.0 * np.log10(np.maximum(levels, 1e-12))
    tone = live & (db <= floor_db + margin_db)
    guard = int(round(guard_s * sr))
    minimum = int(round(min_region_s * sr))
    padded = np.concatenate(([False], tone, [False]))
    change = np.flatnonzero(padded[1:] != padded[:-1])
    regions = []
    for first, last in zip(change[0::2], change[1::2]):
        start, end = int(first) * frame + guard, int(last) * frame - guard
        if end - start >= minimum:
            regions.append((start, end))
    return regions


def _equal_power(length: int) -> tuple[np.ndarray, np.ndarray]:
    t = (np.arange(length) + 0.5) / length * (math.pi / 2.0)
    return np.cos(t), np.sin(t)


def best_splice_offset(tail: np.ndarray, head: np.ndarray, search: int, decimate: int = 4) -> int:
    """Offset into ``head`` (0..search) whose next ``len(tail)`` samples best match ``tail``.

    Normalised correlation on mono signals, decimated for speed.  The crossfade then runs between
    two stretches that already look alike, which keeps the splice from adding a click or a beat.
    """
    n = len(tail)
    search = max(0, min(search, len(head) - n))
    if search == 0 or n < decimate * 8:
        return 0
    ref = tail[::decimate]
    ref = ref - ref.mean()
    norm_ref = float(np.linalg.norm(ref))
    if norm_ref < 1e-12:
        return 0
    best, best_score = 0, -2.0
    # a sliding view over the decimated head avoids building every window at full rate
    sub = head[:search + n]
    for offset in range(0, search, decimate):
        window = sub[offset:offset + n:decimate]
        window = window - window.mean()
        denom = float(np.linalg.norm(window)) * norm_ref
        if denom < 1e-12:
            continue
        score = float(np.dot(window, ref)) / denom
        if score > best_score:
            best, best_score = offset, score
    return best


def build_bed(audio: np.ndarray, regions: list[tuple[int, int]], length: int, sr: int, seed: int = 0,
              crossfade_s: float = 0.08, search_s: float = 0.2) -> tuple[np.ndarray, dict[str, Any]]:
    """Room tone of ``length`` samples from ``regions`` with matched equal-power splices.

    Returns ``(bed, info)``; ``info`` lists the pieces used and the longest uninterrupted stretch of
    any single source region, so a viewer can judge how repetitive the bed is.
    """
    a = _as_2d(audio)
    if not regions:
        raise ToneError("no steady room tone found to borrow; lower min_region_s or widen margin_db")
    if length <= 0:
        return np.zeros((0, a.shape[1])), {"pieces": [], "longest_piece_s": 0.0, "regions_used": 0, "reused_regions": 0}
    rng = np.random.default_rng(seed)
    xf = int(round(crossfade_s * sr))
    search = int(round(search_s * sr))
    # a region shorter than two crossfades cannot be spliced cleanly
    usable = [r for r in regions if r[1] - r[0] >= 2 * xf + 1]
    if not usable:
        raise ToneError(f"every tone region is shorter than two crossfades ({2 * crossfade_s:g} s)")
    lead = a[:, 0]                                  # splice matching needs one waveform; every channel is blended
    bed = np.zeros((0, a.shape[1]))
    pieces = []
    use_count: dict[int, int] = {}
    previous = -1
    while len(bed) < length:
        choices = [i for i in range(len(usable)) if i != previous] or list(range(len(usable)))
        index = int(rng.choice(choices))
        start, end = usable[index]
        span = end - start
        take = int(min(span, max(2 * xf + 1, rng.integers(span // 2 + 1, span + 1))))
        offset = int(rng.integers(0, span - take + 1))
        piece = a[start + offset:start + offset + take]
        if len(bed) and xf == 0:
            bed = np.concatenate([bed, piece])               # zero-sample crossfade: butt-join
        elif len(bed):
            tail = bed[-xf:, 0]
            head = lead[start + offset:start + offset + take]
            shift = best_splice_offset(tail, head, min(search, take - xf - 1))
            piece = piece[shift:]
            fade_out, fade_in = _equal_power(xf)
            blended = bed[-xf:] * fade_out[:, None] + piece[:xf] * fade_in[:, None]
            bed = np.concatenate([bed[:-xf], blended, piece[xf:]])
        else:
            bed = piece.copy()
        pieces.append({"region": index, "start_s": round((start + offset) / sr, 4), "duration_s": round(len(piece) / sr, 4)})
        use_count[index] = use_count.get(index, 0) + 1
        previous = index
    info = {"pieces": pieces, "regions_used": len(use_count), "reused_regions": sum(1 for c in use_count.values() if c > 1),
            "longest_piece_s": max(p["duration_s"] for p in pieces)}
    return bed[:length], info


def _quiet_extent(audio2d: np.ndarray, start: int, direction: int, limit: int, sr: int, ceiling: float) -> int:
    """How many samples from ``start`` going ``direction`` (+1 or -1) stay as quiet as tone, up to ``limit``."""
    window = max(1, int(round(0.005 * sr)))
    extent = 0
    while extent < limit:
        if direction > 0:
            lo, hi = start + extent, min(len(audio2d), start + extent + window)
        else:
            lo, hi = max(0, start - extent - window), start - extent
        if hi <= lo or hi - lo < window // 2:
            break
        if _rms(audio2d[lo:hi]) > ceiling:
            break
        extent += hi - lo                      # the samples actually examined, so a partial window counts as itself
    return min(extent, limit)


def fill_holes(audio: np.ndarray, sr: int, *, seed: int = 0, min_hole_s: float = 0.05, crossfade_s: float = 0.04,
               margin_db: float = 6.0, min_region_s: float = 0.3) -> tuple[np.ndarray, dict[str, Any]]:
    """Fill every hole with level-matched room tone; return ``(audio, report)``."""
    if sr <= 0:
        raise ToneError("sample rate must be positive")
    a = _as_2d(audio)
    holes = find_holes(a, sr, min_hole_s)
    regions = find_tone_regions(a, sr, margin_db=margin_db, min_region_s=min_region_s)
    out = a.copy()
    report: dict[str, Any] = {"schema": SCHEMA, "sample_rate": sr, "samples": len(a), "channels": a.shape[1],
                              "seed": seed, "input_sha256": audio_sha256(a), "holes": [], "tone_regions": [
                                  {"start_s": round(s / sr, 4), "end_s": round(e / sr, 4), "start_sample": int(s), "end_sample": int(e)}
                                  for s, e in regions],
                              "warnings": []}
    if not holes:
        report.update(output_sha256=audio_sha256(out), holes_filled=0)
        return (out[:, 0] if np.asarray(audio).ndim == 1 else out), report
    if not regions:
        raise ToneError("holes found but no steady room tone to borrow; the recording has no usable pause")
    region_rms = [(s, e, _rms(a[s:e])) for s, e in regions]          # energy over every channel, same metric as the bed
    ceiling_all = float(np.median([r[2] for r in region_rms])) * 10 ** (margin_db / 20.0)
    xf = int(round(crossfade_s * sr))
    for number, (start, end) in enumerate(holes):
        previous_end = holes[number - 1][1] if number else 0
        next_start = holes[number + 1][0] if number + 1 < len(holes) else len(a)
        # each hole may use at most half of the tone between it and its neighbour, so patches never overlap
        pre_limit = min(xf, (start - previous_end) // 2)
        post_limit = min(xf, (next_start - end) // 2)
        centre = (start + end) // 2
        nearest = sorted(region_rms, key=lambda r: abs((r[0] + r[1]) // 2 - centre))[:2]
        target = math.sqrt(sum(r[2] ** 2 for r in nearest) / len(nearest))
        pre = _quiet_extent(a, start, -1, pre_limit, sr, ceiling_all)
        post = _quiet_extent(a, end, +1, post_limit, sr, ceiling_all)
        window = max(1, int(round(0.02 * sr)))
        # reference windows stop at the neighbouring hole, so another hole's silence never counts as tone
        wb = min(window, start - previous_end)
        wa = min(window, next_start - end)
        minimum_window = max(1, sr // 1000)
        before = _rms(a[start - wb:start])
        after = _rms(a[end:end + wa])
        tone_before = bool(wb >= minimum_window and before <= ceiling_all)
        tone_after = bool(wa >= minimum_window and after <= ceiling_all)
        left, right = start - pre, end + post
        bed, info = build_bed(a, regions, right - left, sr, seed=seed + number)
        gain = target / max(_rms(bed), 1e-12)
        clamped = min(max(gain, 10 ** (-12 / 20)), 10 ** (12 / 20))
        if clamped != gain:
            report["warnings"].append(f"hole {number}: tone gain {_db(gain):.1f} dB clamped to +-12 dB")
        patch = bed * clamped
        weight_patch = np.ones(len(patch))
        if pre:
            weight_patch[:pre] = _equal_power(pre)[1]
        if post:
            weight_patch[-post:] = _equal_power(post)[0]
        weight_keep = np.zeros(len(patch))
        if pre:
            weight_keep[:pre] = _equal_power(pre)[0]
        if post:
            weight_keep[-post:] = _equal_power(post)[1]
        out[left:right] = a[left:right] * weight_keep[:, None] + patch * weight_patch[:, None]
        report["holes"].append({
            "start_sample": int(start), "end_sample": int(end), "pre_samples": int(pre), "post_samples": int(post),
            "neighbour_before_is_tone": tone_before, "neighbour_after_is_tone": tone_after,
            "edge_window_before": int(wb), "edge_window_after": int(wa),
            "start_s": round(start / sr, 5), "end_s": round(end / sr, 5), "duration_s": round((end - start) / sr, 5),
            "crossfade_before_s": round(pre / sr, 5), "crossfade_after_s": round(post / sr, 5),
            "target_dbfs": round(_db(target), 2), "gain_db": round(_db(clamped), 2),
            "pieces": len(info["pieces"]), "longest_piece_s": info["longest_piece_s"], "reused_regions": info["reused_regions"]})
    report["holes_filled"] = len(holes)
    report["output_sha256"] = audio_sha256(out)
    return (out[:, 0] if np.asarray(audio).ndim == 1 else out), report


def band_levels(block: np.ndarray, sr: int) -> list[float]:
    """Octave-band mean-square levels in dB for the bands that lie fully below Nyquist.

    Each channel is analysed on its own and the band powers are averaged, so opposite-polarity channels
    cannot cancel.  Power is normalised to mean square (Parseval), so the level does not depend on how
    long the analysed window is.
    """
    a = _as_2d(block)
    edges = [(low, high) for low, high in zip(OCTAVE_EDGES[:-1], OCTAVE_EDGES[1:]) if high <= sr / 2.0]
    if len(a) < 64:
        return [-120.0] * len(edges)
    window = np.hanning(len(a))
    freqs = np.fft.rfftfreq(len(a), 1.0 / sr)
    power = np.zeros(len(freqs))
    for channel in range(a.shape[1]):
        spectrum = np.abs(np.fft.rfft(a[:, channel] * window)) ** 2 / (len(a) * np.sum(window ** 2))
        spectrum[1:] *= 2.0                                   # one-sided: fold negative frequencies in
        if len(a) % 2 == 0:
            spectrum[-1] /= 2.0                               # the Nyquist bin has no mirror
        power += spectrum / a.shape[1]
    levels = []
    for low, high in edges:
        mask = (freqs >= low) & (freqs < high)
        levels.append(round(10.0 * math.log10(max(float(power[mask].sum()), 1e-20)), 2) if mask.any() else -120.0)
    return levels


def fill_report(original: np.ndarray, filled: np.ndarray, sr: int, report: dict[str, Any], *, crossfade_s: float = 0.04,
                min_hole_s: float = 0.05) -> dict[str, Any]:
    """Observed facts about a fill: nothing silent remains, outside audio is untouched, tone matches."""
    a, b = _as_2d(original), _as_2d(filled)
    result = dict(report)
    remaining = find_holes(b, sr, min_hole_s)
    regions = [(r["start_sample"], r["end_sample"]) for r in report["tone_regions"]]
    reference = np.concatenate([a[s:e] for s, e in regions]) if regions else np.zeros((0, a.shape[1]))
    zones = []
    checks = []
    bands_ref = band_levels(reference, sr) if len(reference) >= int(0.1 * sr) and report["holes"] else None   # once, not per hole
    for hole in report["holes"]:
        s, e = hole["start_sample"], hole["end_sample"]              # exact sample indices, never rounded seconds
        zones.append((s - hole["pre_samples"], e + hole["post_samples"]))
        inside = b[s:e]
        wb, wa = hole["edge_window_before"], hole["edge_window_after"]      # bounded by the neighbouring hole
        deviations = []
        if hole["neighbour_before_is_tone"]:
            deviations.append(abs(_db(_rms(inside[:wb])) - _db(_rms(a[s - wb:s]))))
        if hole["neighbour_after_is_tone"]:
            deviations.append(abs(_db(_rms(inside[-wa:])) - _db(_rms(a[e:e + wa]))))
        tone_rms = _rms(inside)
        bands_in = band_levels(inside, sr) if len(inside) >= int(0.1 * sr) else None
        spread = None
        if bands_in and bands_ref:
            # compare spectral shape: remove the overall level difference, then take the worst band
            offset = float(np.mean(np.array(bands_in) - np.array(bands_ref)))
            spread = round(float(np.max(np.abs(np.array(bands_in) - np.array(bands_ref) - offset))), 2)
        checks.append({"start_s": hole["start_s"], "filled_dbfs": round(_db(tone_rms), 2), "target_dbfs": hole["target_dbfs"],
                       "level_error_db": round(abs(_db(tone_rms) - hole["target_dbfs"]), 2),
                       "edge_level_deviation_db": round(max(deviations), 2) if deviations else None,   # tone neighbours only
                       "band_shape_deviation_db": spread})
    keep = np.ones(len(a), dtype=bool)
    for left, right in zones:
        keep[max(0, left):right] = False
    result["checks"] = checks
    result["remaining_digital_silence"] = [{"start_s": round(s / sr, 4), "end_s": round(e / sr, 4)} for s, e in remaining]
    result["untouched_samples"] = int(keep.sum())
    result["untouched_identical"] = bool(np.array_equal(a[keep], b[keep]))
    result["patched_samples"] = int((~keep).sum())
    return result


def synthetic_dialogue(sr: int = 16000, seconds: float = 8.0, seed: int = 7) -> tuple[np.ndarray, dict[str, Any]]:
    """Invented dialogue stand-in: voiced bursts over a coloured room tone, with two tone-less holes.

    Bursts are harmonic stacks with syllable envelopes; the room is low-passed noise plus a quiet
    hum.  Two pauses are zeroed (the holes).  No speech, no real recording.
    """
    rng = np.random.default_rng(seed)
    n = int(sr * seconds)
    white = rng.standard_normal(n)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    spectrum *= 1.0 / np.sqrt(np.maximum(freqs, 30.0) / 30.0)          # pink-ish room colour
    room = np.fft.irfft(spectrum, n)
    room = room / np.std(room) * 0.01
    t = np.arange(n) / sr
    room += 0.002 * np.sin(2 * np.pi * 60.0 * t)
    speech = np.zeros(n)
    bursts = [(0.3, 1.5, 140.0), (2.2, 3.1, 180.0), (4.7, 6.0, 120.0), (6.9, 7.6, 160.0)]
    for start, end, f0 in bursts:
        i, j = int(start * sr), int(end * sr)
        local = t[i:j] - start
        carrier = sum(np.sin(2 * np.pi * f0 * k * local) / k for k in range(1, 9))
        envelope = np.hanning(j - i) * (0.6 + 0.4 * np.sin(2 * np.pi * 4.0 * local) ** 2)
        speech[i:j] = 0.12 * carrier * envelope
    dialogue = room + speech
    holes = [(1.9, 2.1), (3.5, 4.4)]
    for start, end in holes:
        dialogue[int(start * sr):int(end * sr)] = 0.0
    return dialogue, {"sample_rate": sr, "holes_s": holes, "bursts_s": [(a, b) for a, b, _ in bursts], "seed": seed}
