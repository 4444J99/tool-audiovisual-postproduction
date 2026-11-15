"""Caption timing from word cues: SRT and WebVTT cues that obey a declared reading-speed and layout spec.

Input is a list of word cues (``text``, ``start`` and ``end`` in seconds), as a speech aligner or a script
timing sheet would give them.  Output is cues whose edges sit on the frame grid of the declared frame rate,
whose lines respect a character limit, and whose reading speed, duration and gap respect a spec; anything
the spec cannot be made to hold for is *reported*, never silently accepted.

What this does, in order:

* validates the words (finite, ordered, non-empty, no word wider than a line);
* groups words into cues, splitting at sentence ends, at silences of ``pause_split_s`` or more, at a clause
  mark once a cue is mostly full, and wherever the next word would not fit ``max_lines`` lines or would pass
  ``max_duration_s``;
* breaks each cue into one or two lines, balancing their widths and preferring a break after punctuation;
* snaps start and end to frames, keeps ``min_gap_frames`` between cues, and stretches a cue forward (never
  into the next cue's gap, never past ``max_duration_s``) until it meets ``min_duration_s`` and ``max_cps``.

Times are integer frames internally, so SRT and WebVTT timestamps (milliseconds) are derived from one number
and read back to the same frames.  ``check`` judges any cue list, including one parsed from a file written
by another tool.  Nothing here listens to audio or decides what is said: it lays out the words it is given.
The presets are commonly published defaults, not the requirement of any broadcaster or platform.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from fractions import Fraction
from typing import Any, Mapping, Sequence

SCHEMA = "amps.captions.v1"
SENTENCE_END = (".", "?", "!", "…")
CLAUSE_END = (",", ";", ":", "—", "-")

PRESETS: dict[str, dict[str, Any]] = {
    "broadcast": {"name": "broadcast", "max_chars_per_line": 37, "max_lines": 2, "max_cps": 17.0, "min_duration_s": 1.0,
                  "max_duration_s": 7.0, "min_gap_frames": 2, "pause_split_s": 0.8},
    "web": {"name": "web", "max_chars_per_line": 42, "max_lines": 2, "max_cps": 20.0, "min_duration_s": 5 / 6,
            "max_duration_s": 7.0, "min_gap_frames": 2, "pause_split_s": 0.8},
}


class CaptionError(ValueError):
    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_fps(value: Any) -> Fraction:
    """24, 25, 29.97 is refused (say ``30000/1001``): a frame rate is a ratio, not a rounded decimal."""
    if isinstance(value, bool):
        raise CaptionError("fps must be a positive integer or a ratio such as 24000/1001")
    if isinstance(value, Fraction):
        fps = value
    elif isinstance(value, int):
        fps = Fraction(value)
    elif isinstance(value, str) and re.fullmatch(r"\d+(/\d+)?", value.strip()):
        num, _, den = value.strip().partition("/")
        if int(den or 1) == 0:
            raise CaptionError("fps denominator is zero")
        fps = Fraction(int(num), int(den or 1))
    elif isinstance(value, float) and value.is_integer():
        fps = Fraction(int(value))
    else:
        raise CaptionError("fps must be a positive integer or a ratio such as 24000/1001, not a rounded decimal")
    if fps <= 0 or fps > 240:
        raise CaptionError("fps must be between 0 and 240")
    return fps


def normalize_spec(spec: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(spec, str):
        if spec not in PRESETS:
            raise CaptionError(f"unknown caption preset {spec!r}; use {sorted(PRESETS)} or a spec file")
        return dict(PRESETS[spec])
    out = dict(PRESETS["broadcast"]) | dict(spec)
    out.setdefault("name", "custom")
    errors = []
    for key in ("max_chars_per_line", "max_lines", "min_gap_frames"):
        if not isinstance(out[key], int) or isinstance(out[key], bool) or out[key] < (0 if key == "min_gap_frames" else 1):
            errors.append(f"{key} must be a whole number >= {0 if key == 'min_gap_frames' else 1}")
    for key in ("max_cps", "min_duration_s", "max_duration_s", "pause_split_s"):
        if not _finite(out[key]) or out[key] <= 0:
            errors.append(f"{key} must be a finite number > 0")
    if not errors and out["max_lines"] > 2:
        errors.append("max_lines must be 1 or 2")
    if not errors and out["min_duration_s"] > out["max_duration_s"]:
        errors.append("min_duration_s is over max_duration_s")
    if errors:
        raise CaptionError(errors)
    return out


def validate_words(words: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(words, Sequence) or isinstance(words, (str, bytes)) or not words:
        return ["words must be a non-empty list"]
    previous = None
    for i, word in enumerate(words):
        if not isinstance(word, Mapping):
            errors.append(f"word {i}: not an object")
            continue
        text, start, end = word.get("text"), word.get("start"), word.get("end")
        if not isinstance(text, str) or not text.strip() or re.search(r"\s", text.strip()):
            errors.append(f"word {i}: text must be one non-empty token without whitespace")
        elif len(text) > spec["max_chars_per_line"]:
            errors.append(f"word {i}: {text!r} is wider than a {spec['max_chars_per_line']}-character line")
        if not _finite(start) or not _finite(end) or start < 0 or end < start:
            errors.append(f"word {i}: start and end must be finite seconds with 0 <= start <= end")
            continue
        if previous is not None and start < previous - 1e-9:
            errors.append(f"word {i}: starts at {start} s, before the previous word's start {previous} s")
        previous = start
    return errors


def break_lines(words: Sequence[str], max_chars: int, max_lines: int) -> list[str] | None:
    """The best line layout for ``words`` within the limits, or None when none fits.  One line when it fits;
    otherwise two lines with the smallest width difference, a break after punctuation winning over a
    break of the same balance elsewhere, and a one-word last line avoided when another layout exists."""
    text = " ".join(words)
    if len(text) <= max_chars:
        return [text]
    if max_lines < 2 or len(words) < 2:
        return None
    best, best_score = None, None
    for cut in range(1, len(words)):
        first, second = " ".join(words[:cut]), " ".join(words[cut:])
        if len(first) > max_chars or len(second) > max_chars:
            continue
        score = abs(len(first) - len(second)) - (8 if words[cut - 1].endswith(SENTENCE_END + CLAUSE_END) else 0) \
            + (6 if len(words) - cut == 1 else 0)
        if best_score is None or score < best_score:
            best, best_score = [first, second], score
    return best


def group_words(words: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]) -> list[list[int]]:
    """Indices of the words in each cue, in order, every word in exactly one cue."""
    cues: list[list[int]] = []
    current: list[int] = []
    capacity = spec["max_chars_per_line"] * spec["max_lines"]
    for i, word in enumerate(words):
        if current:
            tokens = [words[j]["text"] for j in current] + [word["text"]]
            last = words[current[-1]]
            split = (word["start"] - last["end"] >= spec["pause_split_s"]
                     or last["text"].endswith(SENTENCE_END)
                     or word["end"] - words[current[0]]["start"] > spec["max_duration_s"]
                     or break_lines(tokens, spec["max_chars_per_line"], spec["max_lines"]) is None
                     or (last["text"].endswith(CLAUSE_END) and len(" ".join(tokens[:-1])) >= 0.6 * capacity))
            if split:
                cues.append(current)
                current = []
        current.append(i)
    if current:
        cues.append(current)
    return cues


def ms_of(frames: int, fps: Fraction) -> int:
    return int(round(Fraction(frames) * 1000 / fps))


def build(words: Sequence[Mapping[str, Any]], fps: Any, spec: str | Mapping[str, Any] = "broadcast") -> dict[str, Any]:
    """Cues, SRT and WebVTT text, and a report.  Raises ``CaptionError`` on invalid words or spec."""
    spec = normalize_spec(spec)
    rate = parse_fps(fps)
    errors = validate_words(words, spec)
    if errors:
        raise CaptionError(errors)
    groups = group_words(words, spec)
    gap = spec["min_gap_frames"]
    to_frames = lambda seconds: int(round(Fraction(seconds).limit_denominator(1_000_000) * rate))  # noqa: E731
    cues = []
    for group in groups:
        lines = break_lines([words[j]["text"] for j in group], spec["max_chars_per_line"], spec["max_lines"])
        start, end = to_frames(words[group[0]]["start"]), to_frames(max(words[j]["end"] for j in group))
        cues.append({"words": [group[0], group[-1]], "lines": lines, "start": start, "end": max(end, start + 1)})
    for i in range(1, len(cues)):                  # rounding or overlapping words must not run cues together
        prev, cur = cues[i - 1], cues[i]
        prev["end"] = max(prev["start"] + 1, min(prev["end"], cur["start"] - gap))
        cur["start"] = max(cur["start"], prev["end"] + gap)
        cur["end"] = max(cur["end"], cur["start"] + 1)
    for i, cue in enumerate(cues):                 # stretch forward into free space to meet duration and reading speed
        limit = cues[i + 1]["start"] - gap if i + 1 < len(cues) else None
        chars = len("".join(cue["lines"]))
        want = max(cue["end"] - cue["start"], int(math.ceil(spec["min_duration_s"] * rate)),
                   int(math.ceil(Fraction(chars) / Fraction(spec["max_cps"]).limit_denominator(1000) * rate)))
        want = min(want, int(math.floor(spec["max_duration_s"] * rate)))
        end = cue["start"] + want
        cue["end"] = max(cue["end"], end if limit is None else min(end, limit))
    for n, cue in enumerate(cues, 1):
        cue.update(index=n, start_ms=ms_of(cue["start"], rate), end_ms=ms_of(cue["end"], rate))
    problems = check(cues, rate, spec)
    srt, vtt = render_srt(cues), render_vtt(cues)
    return {"schema": SCHEMA, "spec": spec, "fps": str(rate), "cues": cues, "srt": srt, "vtt": vtt, "problems": problems,
            "words": len(words), "srt_sha256": sha256_text(srt), "vtt_sha256": sha256_text(vtt)}


def _stamp(ms: int, sep: str) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{milli:03d}"


def render_srt(cues: Sequence[Mapping[str, Any]]) -> str:
    return "".join(f"{c['index']}\n{_stamp(c['start_ms'], ',')} --> {_stamp(c['end_ms'], ',')}\n" + "\n".join(c["lines"]) + "\n\n" for c in cues)


def render_vtt(cues: Sequence[Mapping[str, Any]]) -> str:
    return "WEBVTT\n\n" + "".join(f"{c['index']}\n{_stamp(c['start_ms'], '.')} --> {_stamp(c['end_ms'], '.')}\n" + "\n".join(c["lines"]) + "\n\n" for c in cues)


_STAMP = r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})"


def parse(text: str, fps: Any) -> list[dict[str, Any]]:
    """Cues from SRT or WebVTT text, with each time converted back to the nearest frame.  Raises on malformed blocks."""
    rate = parse_fps(fps)
    body = text.replace("\r\n", "\n").lstrip("﻿")
    if body.startswith("WEBVTT"):
        body = body.split("\n\n", 1)[1] if "\n\n" in body else ""
    cues: list[dict[str, Any]] = []
    for block in [b for b in body.split("\n\n") if b.strip()]:
        rows = block.strip("\n").split("\n")
        if re.fullmatch(r"\d+", rows[0].strip()) and len(rows) > 1:
            rows = rows[1:]
        match = re.fullmatch(rf"{_STAMP} --> {_STAMP}(?: .*)?", rows[0].strip())
        if not match or len(rows) < 2:
            raise CaptionError(f"cue block {len(cues) + 1} is malformed: {rows[0][:60]!r}")
        g = [int(x) for x in match.groups()]
        start_ms, end_ms = ((g[0] * 60 + g[1]) * 60 + g[2]) * 1000 + g[3], ((g[4] * 60 + g[5]) * 60 + g[6]) * 1000 + g[7]
        cues.append({"index": len(cues) + 1, "start_ms": start_ms, "end_ms": end_ms, "lines": rows[1:],
                     "start": int(round(Fraction(start_ms) * rate / 1000)), "end": int(round(Fraction(end_ms) * rate / 1000))})
    return cues


def check(cues: Sequence[Mapping[str, Any]], fps: Any, spec: str | Mapping[str, Any]) -> list[str]:
    """Every way a cue list breaks the spec.  Empty means it complies; it does not mean the words are right."""
    spec, rate = normalize_spec(spec), parse_fps(fps)
    problems: list[str] = []
    for i, cue in enumerate(cues):
        tag = f"cue {cue.get('index', i + 1)}"
        start, end = cue["start_ms"], cue["end_ms"]
        if end <= start:
            problems.append(f"{tag}: ends at {end} ms, not after its start {start} ms")
            continue
        for key, ms in (("start", start), ("end", end)):
            if abs(ms_of(int(round(Fraction(ms) * rate / 1000)), rate) - ms) > 0:
                problems.append(f"{tag}: {key} {ms} ms is not on the {rate} fps frame grid")
        seconds = (end - start) / 1000
        lines = cue["lines"]
        if len(lines) > spec["max_lines"]:
            problems.append(f"{tag}: {len(lines)} lines, over the limit of {spec['max_lines']}")
        for line in lines:
            if len(line) > spec["max_chars_per_line"]:
                problems.append(f"{tag}: line of {len(line)} characters is over the {spec['max_chars_per_line']} limit")
        cps = len("".join(lines)) / seconds
        if cps > spec["max_cps"] + 1e-9:
            problems.append(f"{tag}: reads at {cps:.1f} characters per second, over {spec['max_cps']}")
        if seconds < spec["min_duration_s"] - 1e-9:
            problems.append(f"{tag}: lasts {seconds:.3f} s, under the {spec['min_duration_s']:.3f} s minimum")
        if seconds > spec["max_duration_s"] + 1e-9:
            problems.append(f"{tag}: lasts {seconds:.3f} s, over the {spec['max_duration_s']} s maximum")
        if i:
            before = cues[i - 1]
            if start < before["end_ms"]:
                problems.append(f"{tag}: starts at {start} ms before the previous cue ends at {before['end_ms']} ms")
            elif int(round(Fraction(start) * rate / 1000)) - int(round(Fraction(before["end_ms"]) * rate / 1000)) < spec["min_gap_frames"]:
                problems.append(f"{tag}: gap to the previous cue is under {spec['min_gap_frames']} frames")
    return problems


def words_preserved(words: Sequence[Mapping[str, Any]], cues: Sequence[Mapping[str, Any]]) -> bool:
    """Every input word appears once, in order, in the cue text: nothing dropped, added or reordered."""
    return " ".join(w["text"] for w in words) == " ".join(" ".join(c["lines"]) for c in cues)


def synthetic_words() -> list[dict[str, Any]]:
    """Invented speech timing: a fast run (needs stretching), a long sentence (needs two lines), a silence."""
    script = ("Welcome back to the show. Today we are looking at how a long sentence has to be split across two balanced lines, "
              "without losing a single word. Yes. Then a pause, and the closing remark arrives much later.")
    words, t = [], 0.2
    for spoken in script.split():
        dur = 0.1 + 0.05 * len(spoken.strip(".,"))
        words.append({"text": spoken, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur + (0.02 if not spoken.endswith((".", ",")) else 0.3)
        if spoken == "Yes.":
            t += 2.4
    return words
