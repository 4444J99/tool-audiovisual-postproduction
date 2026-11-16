"""Delivery QC for the hash-chained pipeline: judge the sealed artefacts, from the sealed artefacts only.

``derive`` reads the delivered dialogue, the caption files, the burned frames, the shot plan, the set spec, the
final ledger, the colour plan and the delivery report from disk and re-computes every number it reports from
those bytes.  It does not read the caption stage's or the burn-in stage's own ``problems`` fields as evidence.
The result (``qc-report.json``) is deterministic: no clock, no host, no random draw, so ``verify`` can re-derive
it and compare byte for byte.

Three groups of checks, each a named entry in ``checks``:

* **captions against the dialogue**: every cue lies inside a dialogue-active span (found here by an energy
  detector on the delivered WAV, not taken from any earlier stage), no cue is longer than ``max_cue_frames``,
  no cue reads faster than ``max_cps`` characters per second, every cue is inside the edit, and the frames
  that carry burned-in text are exactly the frames the cues name (the burned frames are compared with a
  caption-less render of the same background).
* **frame rate and timecode**: the shot plan, the final ledger, the colour plan, the caption report and the
  burn-in parameters agree on one exact rate; the edit is contiguous and its length is the same in the ledger,
  the colour plan, the burn-in frames and the frame file's byte size; each shot's render length equals its edit
  length; the set spec's cache ranges cover the frames the shots use; shot boundaries and cue times are given as
  timecodes and every caption stamp converts back to the frame it was made for.
* **loudness**: the delivered WAV is measured again here (BS.1770-4 through ``loudness.measure``) and judged
  against the spec recorded in the delivery report, and the new measurement must agree with the report's.

This is a consistency judgement over invented material.  It does not say the audio is a good mix, that a
distributor accepts the files, or that the captions say what was said.
"""
from __future__ import annotations

import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from . import caption_burnin as cb
from . import captions as cap
from . import loudness as ld

SCHEMA = "amps.delivery_qc.v2"      # v2: per-check independence, cannot_fail_independently, the coverage and independent-placement checks

DEFAULT_SPEC: dict[str, Any] = {
    "name": "chain-default",
    "max_cue_frames": 120,            # five seconds at 24 fps
    "max_cps": 17.0,                  # characters per second, line breaks not counted
    "span_tolerance_frames": 2,       # a cue may start or end this many frames outside its dialogue span
    "report_tolerance": 0.01,         # LU / dB the re-measurement may differ from the delivery report
    "independent_overlap_min": 0.75,  # share of a cue / span the second detector must agree on
    "vad": {"frame_s": 0.02, "margin_db": 7.0, "merge_gap_s": 0.1, "min_span_s": 0.2},
}

# The second detector's constants are not part of the QC spec and not shared with the captions stage's detector: a spec
# (or a caller) that loosens ``vad`` cannot move what this one hears.  It uses a different method (a 30 ms window on a
# 10 ms hop and a two-class Otsu split of the frame levels, with no margin above a noise floor; it gates only on a 5th-to-95th
# percentile spread of at least ``min_separation_db``, and below that spread it yields no span, so the checks that use it fail
# closed), so a placement error that the percentile-and-margin detector tolerates is not tolerated by construction.
INDEPENDENT_DETECTOR = {"window_s": 0.03, "hop_s": 0.01, "bins": 64, "merge_gap_s": 0.1, "min_span_s": 0.2,
                        "min_separation_db": 6.0, "tolerance_frames": 2}

# The longest stretch of a dialogue-active span that no cue may cover, whatever share of the span the cues cover.  Both
# detectors join speech runs less than 0.1 s apart, so one span can hold several cues; a share floor alone lets a cue
# shorter than (1 - floor) of a long span be deleted unnoticed.  Fixed here, not in the QC spec, so a spec cannot widen it.
# Honest cues start and end within about 0.05 s of their span (captions.words_in_spans keeps 0.04 s) and sit two frames
# apart; 0.3 s is under the chain's 0.4 s minimum cue duration, so a deleted cue of the chain's own captions always leaves
# more than this.  Bound: a deleted cue shorter than this passes.
MAX_UNCOVERED_S = Fraction(3, 10)

INDEPENDENCE_CLASSES = ("independent", "shared-detector", "shared-limit", "shared-renderer", "shared-measurer",
                        "self-declared", "partly-structural", "weak")
# What each check can and cannot show on a chain this repository produced.  ``independent`` means the check can fail
# for a reason the stage it judges did not itself guard against; every other class is a check that, on the stage's own
# unmodified output, passes by construction and earns its keep only when artefacts are changed afterwards.
INDEPENDENCE: dict[str, tuple[str, str]] = {
    "timebase.frame_rate": ("independent", "five artefacts written by different stages must agree on one exact rate"),
    "timebase.edit_contiguous": ("independent", "the ledger's shot ranges are judged for gaps and overlaps"),
    "timebase.edit_length": ("independent", "four artefacts written by different stages must give one length"),
    "timebase.burnin_segments": ("independent", "the burn-in background tiling is compared with the ledger's own shot boundaries"),
    "timebase.shot_lengths": ("independent", "plan, ledger, render handles and colour plate layers are written by different stages"),
    "timebase.set_caches": ("independent", "the set spec's cache ranges are written before any shot is planned against them"),
    "timebase.timecode": ("partly-structural", "the shot-boundary round trip is an identity (the same nominal-rate function in both directions) and cannot fail; "
                                              "only the comparison of caption stamps with the caption and burn-in reports can"),
    "captions.inside_dialogue": ("shared-detector", "the captions stage places cues with the same detector at its default; on an unmodified chain this passes by construction "
                                                   "and only a later edit, a swapped WAV or a different spec makes it fail"),
    "captions.covers_dialogue": ("independent", "every dialogue-active span, under the placement detector and under the independent one, must be covered by cues; "
                                               "the placement half shares its detector with the captions stage, the independent half does not"),
    "captions.placement_independent": ("independent", "cues are judged against a second detector with fixed constants that neither the QC spec nor the captions stage sets"),
    "captions.max_length": ("shared-limit", "the captions stage refuses cues over the same default limit, so this passes by construction on unmodified output"),
    "captions.reading_rate": ("shared-limit", "the captions stage refuses words over the same default reading rate, so this passes by construction on unmodified output"),
    "captions.inside_edit": ("shared-limit", "the caption and burn-in stages enforce order, overlap and the edit bounds themselves"),
    "captions.burned_frames": ("shared-renderer", "the frames are compared with a re-render by the same renderer that wrote them; it is independent of the stages' reports, not of the renderer"),
    "audio.within_edit": ("weak", "fails only when the audio is longer than the edit; audio shorter than the edit passes (the invented dialogue covers a third of its edit)"),
    "loudness.spec": ("self-declared", "the spec is read from the delivery report being judged; a forger who rewrites it is caught only by a caller who declares the required spec"),
    "loudness.report_agreement": ("shared-measurer", "the re-measurement uses the same BS.1770 code as the delivery stage; it is independent of the stage's recorded numbers only"),
}

# Labels the report reads, in the order they are hashed into its ``inputs``.
INPUT_LABELS = ("captions-report", "captions-srt", "captions-vtt", "burnin-frames", "burnin-report", "delivery-report",
                "dialogue-delivered", "final-ledger", "layer-plan", "set-spec", "shot-plan", "suggested-plan")


class QcError(ValueError):
    """Raised when the artefacts cannot be read at all (missing file, malformed WAV or JSON)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_spec(spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The QC spec with defaults filled in; every field validated."""
    merged = json.loads(json.dumps(DEFAULT_SPEC))
    if spec:
        extra = set(spec) - set(DEFAULT_SPEC)
        if extra:
            raise QcError(f"unknown QC spec keys: {sorted(extra)}")
        for key, value in spec.items():
            if key == "vad":
                if not isinstance(value, Mapping) or set(value) - set(DEFAULT_SPEC["vad"]):
                    raise QcError(f"vad must be a mapping of {sorted(DEFAULT_SPEC['vad'])}")
                merged["vad"].update(value)
            else:
                merged[key] = value
    if not isinstance(merged["name"], str) or not merged["name"]:
        raise QcError("name must be a non-empty string")
    if not isinstance(merged["max_cue_frames"], int) or isinstance(merged["max_cue_frames"], bool) or merged["max_cue_frames"] < 1:
        raise QcError("max_cue_frames must be a positive integer")
    if not _number(merged["max_cps"]) or merged["max_cps"] <= 0:
        raise QcError("max_cps must be a positive number")
    if not isinstance(merged["span_tolerance_frames"], int) or isinstance(merged["span_tolerance_frames"], bool) or merged["span_tolerance_frames"] < 0:
        raise QcError("span_tolerance_frames must be a non-negative integer")
    if not _number(merged["report_tolerance"]) or merged["report_tolerance"] < 0:
        raise QcError("report_tolerance must be a non-negative number")
    if not _number(merged["independent_overlap_min"]) or not 0.5 <= merged["independent_overlap_min"] <= 1:
        raise QcError("independent_overlap_min must be a number from 0.5 to 1: a cue or span that is mostly outside the speech cannot be accepted")
    for key, value in merged["vad"].items():
        if not _number(value) or value <= 0:
            raise QcError(f"vad.{key} must be a positive number")
    return merged


# ------------------------------------------------------------------------------------------ reading

def read_wav16(raw: bytes) -> tuple[int, np.ndarray]:
    """Sample rate and samples of a canonical 16-bit mono PCM WAV (44-byte header); anything else is refused."""
    if len(raw) < 44 or raw[0:4] != b"RIFF" or raw[8:16] != b"WAVEfmt " or raw[36:40] != b"data":
        raise QcError("dialogue-delivered is not a canonical 44-byte-header WAV")
    fmt, channels, rate = int.from_bytes(raw[20:22], "little"), int.from_bytes(raw[22:24], "little"), int.from_bytes(raw[24:28], "little")
    bits, size = int.from_bytes(raw[34:36], "little"), int.from_bytes(raw[40:44], "little")
    if fmt != 1 or channels != 1 or bits != 16 or rate <= 0:
        raise QcError(f"dialogue-delivered must be 16-bit mono PCM (format {fmt}, {channels} channel(s), {bits} bit)")
    if size != len(raw) - 44 or size % 2:
        raise QcError("dialogue-delivered data length does not match its header")
    return rate, np.frombuffer(raw[44:], dtype="<i2").astype(np.float64) / 32768.0


def dialogue_spans(audio: np.ndarray, rate: int, vad: Mapping[str, float]) -> list[tuple[int, int]]:
    """Dialogue-active ``(start, end)`` sample ranges.

    Frame RMS is taken every ``frame_s``; digital-silence frames are ignored; the floor is the 10th percentile of
    the remaining frame levels; a frame is active when it is at least ``margin_db`` above that floor.  Active runs
    separated by less than ``merge_gap_s`` are joined and runs shorter than ``min_span_s`` are dropped."""
    frame = max(1, int(round(vad["frame_s"] * rate)))
    count = len(audio) // frame
    if count == 0:
        return []
    levels = np.sqrt(np.mean(np.square(audio[:count * frame].reshape(count, frame)), axis=1))
    live = levels > 1e-9
    if not live.any():
        return []
    db = 20.0 * np.log10(np.maximum(levels, 1e-12))
    active = live & (db >= float(np.percentile(db[live], 10)) + vad["margin_db"])
    padded = np.concatenate(([False], active, [False]))
    edge = np.flatnonzero(padded[1:] != padded[:-1])
    runs = [[int(a) * frame, int(b) * frame] for a, b in zip(edge[0::2], edge[1::2])]
    merged: list[list[int]] = []
    gap = vad["merge_gap_s"] * rate
    for run in runs:
        if merged and run[0] - merged[-1][1] < gap:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    return [(a, b) for a, b in merged if b - a >= vad["min_span_s"] * rate]


def independent_spans(audio: np.ndarray, rate: int) -> list[tuple[int, int]]:
    """Dialogue-active ``(start, end)`` sample ranges from a detector that shares nothing with ``dialogue_spans``.

    Frame RMS over a 30 ms window every 10 ms; digital-silence frames are ignored; the live frame levels (dB) are
    split into two classes at the Otsu threshold of a 64-bin histogram and the upper class is active.  Runs under
    0.1 s apart join and runs under 0.2 s drop.  No margin above a floor and no value from the QC spec; the only percentile use is the gate:
    a recording with a 5th-to-95th percentile spread under ``min_separation_db`` (flat noise, a pure tone) has no two classes and
    yields nothing, which fails closed (the checks that use the detector require at least one span)."""
    cfg = INDEPENDENT_DETECTOR
    window, hop = max(1, int(round(cfg["window_s"] * rate))), max(1, int(round(cfg["hop_s"] * rate)))
    if len(audio) < window:
        return []
    count = (len(audio) - window) // hop + 1
    frames = np.lib.stride_tricks.sliding_window_view(audio, window)[::hop][:count]       # a view: no copy of the overlapping windows
    levels = np.sqrt(np.einsum("ij,ij->i", frames, frames) / window)
    live = levels > 1e-9
    if not live.any():
        return []
    db = 20.0 * np.log10(np.maximum(levels, 1e-12))
    low, high = np.percentile(db[live], [5, 95])
    if high - low < cfg["min_separation_db"]:        # percentiles, not extremes: one outlier frame is not a second class
        return []
    hist, edges = np.histogram(db[live], bins=cfg["bins"])
    weight = hist / hist.sum()
    centre = (edges[:-1] + edges[1:]) / 2.0
    below, mass = np.cumsum(weight), np.cumsum(weight * centre)
    between = (mass[-1] * below - mass) ** 2 / np.maximum(below * (1.0 - below), 1e-12)
    threshold = edges[int(np.argmax(between)) + 1]     # the lower class owns all of its bin, so the cut is the bin's upper edge
    active = live & (db > threshold)
    padded = np.concatenate(([False], active, [False]))
    edge = np.flatnonzero(padded[1:] != padded[:-1])
    lead = (window - hop) // 2
    runs = [[int(a) * hop + lead, int(b) * hop + lead] for a, b in zip(edge[0::2], edge[1::2])]
    merged: list[list[int]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < cfg["merge_gap_s"] * rate:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    clipped = [(max(0, a), min(len(audio), b)) for a, b in merged]
    return [(a, b) for a, b in clipped if b - a >= cfg["min_span_s"] * rate]


def _uncovered_runs(lo: Fraction, hi: Fraction, intervals: list[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    """The maximal stretches of ``[lo, hi)`` that lie outside the union of ``intervals``."""
    runs = []
    reach = lo
    for a, b in sorted(intervals):
        if b <= reach or a >= hi:
            continue
        if a > reach:
            runs.append((reach, a))
        reach = max(reach, b)
    if reach < hi:
        runs.append((reach, hi))
    return runs


def _covered(lo: Fraction, hi: Fraction, intervals: list[tuple[Fraction, Fraction]]) -> Fraction:
    """Length of ``[lo, hi)`` that lies inside the union of ``intervals``."""
    total = Fraction(0)
    reach = lo
    for a, b in sorted(intervals):
        a, b = max(a, reach), min(b, hi)
        if b > a:
            total += b - a
            reach = b
    return total


# ------------------------------------------------------------------------------------------ timecode

def timecode(frame: int, rate: Fraction) -> str:
    """Non-drop ``HH:MM:SS:FF`` at the nominal integer rate; fractional rates keep their exact value elsewhere."""
    nominal = int(round(rate))
    seconds, ff = divmod(frame, nominal)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}:{ff:02d}"


def frame_of_timecode(label: str, rate: Fraction) -> int:
    hh, mm, ss, ff = (int(p) for p in label.split(":"))
    return (hh * 3600 + mm * 60 + ss) * int(round(rate)) + ff


def _check(cid: str, ok: bool, detail: str, **facts: Any) -> dict[str, Any]:
    kind, note = INDEPENDENCE[cid]
    return {"id": cid, "ok": bool(ok), "detail": detail, "independence": {"class": kind, "note": note}, **facts}


# ------------------------------------------------------------------------------------------ derivation

def _load(root: Path, locations: Mapping[str, str]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for label in INPUT_LABELS:
        path = root / locations[label]
        try:
            files[label] = path.read_bytes()
        except OSError as error:
            raise QcError(f"cannot read {label} ({locations[label]}): {error}") from error
    return files


def _json(files: Mapping[str, bytes], label: str) -> Any:
    try:
        return json.loads(files[label])
    except json.JSONDecodeError as error:
        raise QcError(f"{label} is not valid JSON: {error}") from error


def derive(root: Path | str, locations: Mapping[str, str], spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The QC report for the artefacts under ``root``; ``locations`` maps each label in ``INPUT_LABELS`` to its path.

    Raises ``QcError`` when an artefact is missing, malformed or lacks a field the checks read."""
    try:
        return _derive(Path(root), locations, spec)
    except (KeyError, TypeError, IndexError, ValueError, ZeroDivisionError, StopIteration) as error:
        if isinstance(error, QcError):
            raise
        raise QcError(f"an artefact is malformed: {error!r}") from error


def _derive(root: Path, locations: Mapping[str, str], spec: Mapping[str, Any] | None) -> dict[str, Any]:
    root = Path(root)
    spec = normalize_spec(spec)
    files = _load(root, locations)
    try:
        plan, ledger = _json(files, "shot-plan"), _json(files, "final-ledger")
        final_plan = _json(files, "suggested-plan")
        colour, delivery = _json(files, "layer-plan"), _json(files, "delivery-report")
        set_spec, cap_report, burn_report = _json(files, "set-spec"), _json(files, "captions-report"), _json(files, "burnin-report")
        rate_wav, audio = read_wav16(files["dialogue-delivered"])
        srt, vtt = files["captions-srt"].decode("utf-8"), files["captions-vtt"].decode("utf-8")
        fps_values = {"shot-plan": plan["fps"], "final-ledger": ledger["fps"], "layer-plan": colour["comp"]["fps"],
                      "captions-report": cap_report["fps"], "burnin-report": burn_report["params"]["fps"]}
        fps = cap.parse_fps(ledger["fps"])
        total = ledger["total_frames"]
        if not isinstance(total, int) or isinstance(total, bool) or total < 1:
            raise ValueError(f"final-ledger total_frames must be a positive whole number, not {total!r}")
        cues = cap.parse(srt, fps)
        vtt_cues = cap.parse(vtt, fps)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, cap.CaptionError) as error:
        raise QcError(f"the sealed artefacts cannot be read: {error!r}") from error
    checks: list[dict[str, Any]] = []

    # -- frame rate ------------------------------------------------------------------------------
    parsed: dict[str, str] = {}
    for label, value in fps_values.items():
        try:
            parsed[label] = str(cap.parse_fps(value))
        except (ValueError, cap.CaptionError, ZeroDivisionError, TypeError):
            parsed[label] = f"unreadable:{value!r}"
    checks.append(_check("timebase.frame_rate", len(set(parsed.values())) == 1,
                         "shot plan, final ledger, colour plan, caption report and burn-in parameters carry one exact frame rate",
                         rates=parsed, rate=str(fps)))

    # -- edit length and contiguity ----------------------------------------------------------------
    shots = ledger["shots"]
    contiguous = bool(shots) and shots[0]["edit_in"] == 0 and all(a["edit_out"] == b["edit_in"] for a, b in zip(shots, shots[1:])) \
        and shots[-1]["edit_out"] == total and all(s["edit_out"] > s["edit_in"] for s in shots)
    checks.append(_check("timebase.edit_contiguous", contiguous, "shots tile the edit from frame 0 to the ledger's total with no gap or overlap",
                         total_frames=total, shots=[[s["id"], s["edit_in"], s["edit_out"]] for s in shots]))
    seconds_ok = abs(float(ledger["total_seconds"]) - float(Fraction(total) / fps)) <= 0.00005 + 1e-9   # the ledger rounds to four decimals
    width, height = burn_report["params"]["geometry"]["width"], burn_report["params"]["geometry"]["height"]
    lengths = {"final-ledger": total, "layer-plan": colour["comp"]["frames"], "burnin-report": burn_report["params"]["frames"],
               "burnin-frames": len(files["burnin-frames"]) // (width * height) if len(files["burnin-frames"]) % (width * height) == 0 else -1}
    checks.append(_check("timebase.edit_length", len(set(lengths.values())) == 1 and seconds_ok,
                         "ledger, colour plan, burn-in parameters and the frame file's byte size give one length, and the ledger's seconds are frames over the rate",
                         frames=lengths, total_seconds=ledger["total_seconds"]))
    covered = burn_report["params"]["segments"]
    segment_ok = bool(covered) and covered[0]["start"] == 0 and covered[-1]["end"] == total and all(a["end"] == b["start"] for a, b in zip(covered, covered[1:]))
    checks.append(_check("timebase.burnin_segments", segment_ok, "burn-in background segments tile frames 0 to the edit length",
                         segments=[[s["start"], s["end"]] for s in covered]))

    # -- shot lengths: plan, render, edit, colour plan --------------------------------------------
    plan_frames = {s["id"]: s["frames"] for s in final_plan["shots"]}
    layer_frames = {l["id"]: (l["frames"]["start"], l["frames"]["end"]) for l in colour["layers"] if l.get("kind") == "plate"}
    shot_problems = []
    for s in shots:
        length = s["edit_out"] - s["edit_in"]
        planned = plan_frames.get(s["id"])
        if planned is None:
            continue
        hin, hout = planned.get("handle_in", 0), planned.get("handle_out", 0)
        if planned["end"] - planned["start"] + 1 != length or s["scene_frames"] != [planned["start"], planned["end"]]:
            shot_problems.append(f"{s['id']}: plan frames {planned['start']}-{planned['end']}, scene frames {s['scene_frames']}, edit {length}")
        if s["render_frames"] != [planned["start"] - hin, planned["end"] + hout] or s["render_frame_count"] != length + hin + hout:
            shot_problems.append(f"{s['id']}: render frames {s['render_frames']} ({s['render_frame_count']}) are not the plan's range plus its handles {hin}/{hout}")
        plate = layer_frames.get("shot-" + s["id"].lower())
        if plate != (s["edit_in"], s["edit_out"]):
            shot_problems.append(f"{s['id']}: colour plate layer {plate} is not its edit range")
    if set(plan_frames) != {s["id"] for s in shots}:
        shot_problems.append("the plan's shots are not the ledger's shots")
    checks.append(_check("timebase.shot_lengths", not shot_problems,
                         "each shot's planned frames, edit frames, render frames (planned frames plus handles) and colour plate layer agree", problems=shot_problems))

    # -- set spec caches cover the frames the shots use -------------------------------------------
    cache_ranges = {c["id"]: c["frames"] for c in set_spec.get("caches", [])}
    cache_problems = []
    for s in shots:
        for cache in s.get("caches", []):
            span = cache_ranges.get(cache)
            if span is None:
                cache_problems.append(f"{s['id']}: cache {cache} is not in the set spec")
            elif s["render_frames"][0] < span[0] or s["render_frames"][1] > span[1]:
                cache_problems.append(f"{s['id']}: render frames {s['render_frames']} (handles included) are outside cache {cache} {span}")
    plan_caches = {c["id"]: c["frames"] for c in plan.get("set", {}).get("caches", [])}
    if plan_caches != cache_ranges:
        cache_problems.append("the shot plan's set block lists different caches than the set spec")
    checks.append(_check("timebase.set_caches", not cache_problems,
                         "the set spec's cache frame ranges cover each shot's render frames, handles included; the set spec has no frame rate of its own, so its frame numbers are read at the shot plan's rate",
                         caches=cache_ranges, problems=cache_problems))

    # -- timecode ----------------------------------------------------------------------------------
    table = [{"shot": s["id"], "in": timecode(s["edit_in"], fps), "out": timecode(s["edit_out"], fps)} for s in shots]
    cue_table = [{"cue": c["index"], "in": timecode(c["start"], fps), "out": timecode(c["end"], fps),
                  "start_ms": c["start_ms"], "end_ms": c["end_ms"]} for c in cues]
    roundtrip = [t for t in table if frame_of_timecode(t["in"], fps) != next(s["edit_in"] for s in shots if s["id"] == t["shot"])
                 or frame_of_timecode(t["out"], fps) != next(s["edit_out"] for s in shots if s["id"] == t["shot"])]
    recorded = {c["index"]: (c["start"], c["end"]) for c in cap_report["cues"]}
    burn_cues = {c["index"]: (c["start"], c["end"]) for c in burn_report["cues"]}
    parsed_cues = {c["index"]: (c["start"], c["end"]) for c in cues}
    stamp_problems = []
    for index, frames in parsed_cues.items():
        for label, other in (("caption report", recorded), ("burn-in report", burn_cues)):
            if other.get(index) != frames:
                stamp_problems.append(f"cue {index}: SRT stamps stand for frames {list(frames)}, {label} says {list(other.get(index, []))}")
    if len(recorded) != len(parsed_cues) or len(burn_cues) != len(parsed_cues):
        stamp_problems.append(f"the SRT file has {len(parsed_cues)} cues, the caption report {len(recorded)}, the burn-in report {len(burn_cues)}")
    if [(c["start"], c["end"]) for c in vtt_cues] != [(c["start"], c["end"]) for c in cues]:
        stamp_problems.append("the WebVTT stamps stand for different frames than the SRT stamps")
    if [c["lines"] for c in vtt_cues] != [c["lines"] for c in cues]:
        stamp_problems.append("the WebVTT text differs from the SRT text")
    checks.append(_check("timebase.timecode", not roundtrip and not stamp_problems,
                         "shot boundaries convert to a timecode and back; every caption stamp stands for the frame the caption and burn-in reports record",
                         basis=f"non-drop HH:MM:SS:FF at the nominal {int(round(fps))} fps label; exact rate {fps}",
                         shots=table, cues=cue_table, problems=stamp_problems + [f"timecode round trip failed for {t['shot']}" for t in roundtrip]))

    # -- captions against the dialogue -------------------------------------------------------------
    spans = dialogue_spans(audio, rate_wav, spec["vad"])
    tolerance = Fraction(spec["span_tolerance_frames"]) / fps
    outside = []
    for c in cues:
        begin, end = Fraction(c["start"]) / fps, Fraction(c["end"]) / fps
        home = next((i for i, (a, b) in enumerate(spans) if Fraction(a, rate_wav) - tolerance <= begin and end <= Fraction(b, rate_wav) + tolerance), None)
        if home is None:
            outside.append(f"cue {c['index']} ({float(begin):.3f}-{float(end):.3f} s) is not inside one dialogue-active span")
    span_list = [{"start_s": round(a / rate_wav, 4), "end_s": round(b / rate_wav, 4)} for a, b in spans]
    checks.append(_check("captions.inside_dialogue", bool(cues) and bool(spans) and not outside,
                         "every cue lies inside one dialogue-active span of the delivered audio, within the spec's frame tolerance",
                         tolerance_frames=spec["span_tolerance_frames"], spans=span_list, problems=outside))
    cue_ranges = [(Fraction(c["start"]) / fps, Fraction(c["end"]) / fps) for c in cues]
    other = independent_spans(audio, rate_wav)
    floor = Fraction(spec["independent_overlap_min"]).limit_denominator(10 ** 6)
    uncovered = []
    for label, found in (("placement", spans), ("independent", other)):
        for a, b in found:
            lo, hi = Fraction(a, rate_wav), Fraction(b, rate_wav)
            share = _covered(lo, hi, cue_ranges) / (hi - lo)
            if share < floor:
                uncovered.append(f"dialogue span {float(lo):.3f}-{float(hi):.3f} s ({label} detector) is {float(share):.1%} covered by cues, under {float(floor):.1%}")
            for gap_lo, gap_hi in _uncovered_runs(lo, hi, cue_ranges):
                if gap_hi - gap_lo > MAX_UNCOVERED_S:
                    uncovered.append(f"dialogue span {float(lo):.3f}-{float(hi):.3f} s ({label} detector) has {float(gap_hi - gap_lo):.3f} s with no cue "
                                     f"({float(gap_lo):.3f}-{float(gap_hi):.3f} s), over {float(MAX_UNCOVERED_S):.2f} s")
    checks.append(_check("captions.covers_dialogue", bool(spans) and bool(other) and not uncovered,
                         "every dialogue-active span, under the placement detector and under the independent one, is covered by cues to at least independent_overlap_min",
                         independent_overlap_min=spec["independent_overlap_min"],
                         spans={"placement": span_list, "independent": [{"start_s": round(a / rate_wav, 4), "end_s": round(b / rate_wav, 4)} for a, b in other]},
                         problems=uncovered))
    fixed = Fraction(INDEPENDENT_DETECTOR["tolerance_frames"]) / fps        # not the spec's tolerance: a loosened spec cannot widen this
    heard = [(Fraction(a, rate_wav) - fixed, Fraction(b, rate_wav) + fixed) for a, b in other]
    overlaps, unheard = [], []
    for c, (begin, end) in zip(cues, cue_ranges):
        share = _covered(begin, end, heard) / (end - begin) if end > begin else Fraction(0)
        overlaps.append(round(float(share), 4))
        if share < floor:
            unheard.append(f"cue {c['index']} ({float(begin):.3f}-{float(end):.3f} s) is only {float(share):.1%} inside speech heard by the independent detector, under {float(floor):.1%}")
    checks.append(_check("captions.placement_independent", bool(cues) and bool(other) and not unheard,
                         "every cue lies mostly inside speech found by a detector whose constants neither the QC spec nor the captions stage sets",
                         independent_overlap_min=spec["independent_overlap_min"], overlaps=overlaps, problems=unheard))
    long_cues = [f"cue {c['index']} lasts {c['end'] - c['start']} frames, over {spec['max_cue_frames']}" for c in cues if c["end"] - c["start"] > spec["max_cue_frames"]]
    checks.append(_check("captions.max_length", not long_cues, "no cue is longer than max_cue_frames",
                         max_cue_frames=spec["max_cue_frames"], longest=max((c["end"] - c["start"] for c in cues), default=0), problems=long_cues))
    rates = []
    fast = []
    for c in cues:
        frames = c["end"] - c["start"]
        cps = len("".join(c["lines"])) / float(Fraction(frames) / fps) if frames > 0 else math.inf
        rates.append(round(cps, 3) if math.isfinite(cps) else None)
        if cps > spec["max_cps"] + 1e-9:
            fast.append(f"cue {c['index']} reads at {cps:.1f} characters per second, over {spec['max_cps']}")
    checks.append(_check("captions.reading_rate", not fast, "no cue reads faster than max_cps characters per second",
                         max_cps=spec["max_cps"], rates=rates, problems=fast))
    edit_problems = [f"cue {c['index']} frames {c['start']}-{c['end']} are outside the edit 0-{total}" for c in cues if c["start"] < 0 or c["end"] > total or c["end"] <= c["start"]]
    order = [f"cue {b['index']} starts before cue {a['index']} ends" for a, b in zip(cues, cues[1:]) if b["start"] < a["end"]]
    checks.append(_check("captions.inside_edit", not edit_problems and not order, "cues are ordered, do not overlap and lie inside the edit",
                         problems=edit_problems + order))
    burned, mismatch, render_problems = _burned_frames(files["burnin-frames"], burn_report, total, cues)
    expected = sorted({f for c in cues for f in range(c["start"], c["end"])})
    owner = {f: c["index"] for c in cues for f in range(c["start"], c["end"])}
    text_problems = [f"cue {owner.get(f, '?')}: frame {f} shows other text than the caption file gives it" for f in mismatch[:5]]
    if len(mismatch) > 5:
        text_problems.append(f"{len(mismatch) - 5} more frames show other text than the caption file")
    checks.append(_check("captions.burned_frames", burned == expected and not mismatch and not render_problems,
                         "the frames whose pixels differ from a caption-less render of the same background are exactly the frames the cues name, and every frame equals a re-render of the caption file's text",
                         burned_count=len(burned), expected_count=len(expected),
                         first_difference=next((f for f in sorted(set(burned) ^ set(expected))), None),
                         text_mismatch_first_frame=mismatch[0] if mismatch else None,
                         problems=render_problems + text_problems))

    # -- audio length and loudness -----------------------------------------------------------------
    audio_seconds = Fraction(len(audio), rate_wav)
    checks.append(_check("audio.within_edit", audio_seconds <= Fraction(total) / fps,
                         "the delivered dialogue is not longer than the edit it belongs to",
                         audio_seconds=round(float(audio_seconds), 4), edit_seconds=round(float(Fraction(total) / fps), 4)))
    try:
        loud_spec = ld.normalize_spec(delivery["spec"])
        measured = ld.measure(audio, rate_wav)
        spec_problems = ld.check(measured, loud_spec)
        after = delivery["after"]
        tol = spec["report_tolerance"]
        drift = []
        finite = {key: _number(after[key]) for key in ("integrated_lufs", "true_peak_dbtp")}
        drift += [f"the delivery report's {key} is {after[key]!r}, not a finite number, so it cannot agree with a measurement" for key, ok in finite.items() if not ok]
        if finite["integrated_lufs"] and (measured["integrated_lufs"] is None or abs(measured["integrated_lufs"] - after["integrated_lufs"]) > tol):
            drift.append(f"integrated loudness {measured['integrated_lufs']} differs from the delivery report's {after['integrated_lufs']}")
        if finite["true_peak_dbtp"] and abs(measured["true_peak_dbtp"] - after["true_peak_dbtp"]) > tol:
            drift.append(f"true peak {measured['true_peak_dbtp']} differs from the delivery report's {after['true_peak_dbtp']}")
        if delivery.get("output_sha256") != sha256_bytes(files["dialogue-delivered"]):
            drift.append("the delivery report does not describe the delivered file")
        headroom = None if loud_spec.get("true_peak_dbtp_max") is None else round(loud_spec["true_peak_dbtp_max"] - measured["true_peak_dbtp"], 3)
        deviation = None if measured["integrated_lufs"] is None else round(measured["integrated_lufs"] - loud_spec["integrated_lufs"]["target"], 3)
        measured_facts = {k: measured[k] for k in ("integrated_lufs", "loudness_range_lu", "true_peak_dbtp", "sample_peak_dbfs", "sample_rate", "seconds")}
    except (ld.LoudnessError, KeyError, TypeError, ValueError) as error:
        raise QcError(f"loudness cannot be judged: {error!r}") from error
    checks.append(_check("loudness.spec", not spec_problems, "the delivered dialogue, measured now, meets the spec recorded in the delivery report",
                         spec=loud_spec, measured=measured_facts, deviation_lu=deviation, true_peak_headroom_db=headroom, problems=spec_problems))
    checks.append(_check("loudness.report_agreement", not drift, "the measurement made here agrees with the delivery report's own", problems=drift))

    failed = [c["id"] for c in checks if not c["ok"]]
    return {"schema": SCHEMA, "spec": spec, "status": "pass" if not failed else "fail", "failed": failed,
            "cannot_fail_independently": sorted(c["id"] for c in checks if c["independence"]["class"] != "independent"),
            "inputs": {label: sha256_bytes(files[label]) for label in INPUT_LABELS}, "checks": checks,
            "scope": "consistency of the sealed artefacts with one another and with the declared spec; not a listening test, "
                     "not a distributor's acceptance and not a check that the captions say what was said"}


def _burned_frames(payload: bytes, burn_report: Mapping[str, Any], total: int,
                   cues: list[dict[str, Any]]) -> tuple[list[int], list[int], list[str]]:
    """``(frames that differ from the caption-less render, frames that differ from the render of ``cues``, problems)``.

    The first list says which frames carry pixels, the second says whether those pixels are the caption file's text."""
    params = burn_report["params"]
    size = params["geometry"]["width"] * params["geometry"]["height"]
    if len(payload) != size * params["frames"] or params["frames"] != total:
        return [-1], [], []              # a marker no cue can equal: the frame file is not the burn-in's shape
    burned: list[int] = []
    for i, frame in enumerate(cb.frame_stream(params, [])):                 # one frame at a time: no second copy of the video
        if payload[i * size:(i + 1) * size] != frame:
            burned.append(i)
    try:
        mismatch = [i for i, frame in enumerate(cb.frame_stream(params, cues)) if payload[i * size:(i + 1) * size] != frame]
    except cb.BurnError as error:
        return burned, [], [f"the caption file cannot be re-rendered: {'; '.join(error.errors[:3])}"]
    return burned, mismatch, []


def verify(root: Path | str, locations: Mapping[str, str], recorded: bytes, spec: Mapping[str, Any] | None = None) -> list[str]:
    """Why ``recorded`` (the bytes of ``qc-report.json``) is not what the artefacts derive; empty when it is exact and passes."""
    try:
        report = derive(root, locations, spec)
    except QcError as error:
        return [f"qc: cannot derive ({error})"]
    problems: list[str] = []
    if dumps(report).encode() != recorded:
        problems.append("qc-report.json is not what the sealed artefacts derive")
        try:
            on_disk = json.loads(recorded)
            if on_disk.get("spec") != report["spec"]:
                problems.append("qc-report.json records a different QC spec than the one declared")
            for label, digest in report["inputs"].items():
                if on_disk.get("inputs", {}).get(label) != digest:
                    problems.append(f"qc-report.json was made from a different {label}")
            seen = {c["id"]: c for c in on_disk.get("checks", []) if isinstance(c, dict)}
            for check in report["checks"]:
                if seen.get(check["id"]) != check:
                    problems.append(f"qc check {check['id']} differs from its re-derivation")
        except (json.JSONDecodeError, AttributeError, TypeError):
            problems.append("qc-report.json is not readable JSON")
    problems += [f"qc {c['id']} fails: {c['detail']}" + (f" ({'; '.join(c['problems'])})" if c.get("problems") else "")
                 for c in report["checks"] if not c["ok"]]
    return problems
