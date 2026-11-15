"""Lay out word cues as SRT and WebVTT captions that meet a reading-speed spec, and check caption files.

    python tools/captions.py demo outputs/captions-demo
    python tools/captions.py build --words words.json --output outputs/captions --fps 24 [--spec broadcast|web|spec.json]
    python tools/captions.py check --input captions.srt|captions.vtt --fps 24 [--spec broadcast|web|spec.json] [--words words.json]

``words.json`` is a list of {"text", "start", "end"} (seconds), or {"fps": 24, "words": [...]}.  ``build`` writes
captions.srt, captions.vtt and captions-report.json (spec, cues, problems, hashes), then parses both written
files back and requires them to agree with the cues.  ``check`` judges any SRT or WebVTT file, and with
``--words`` also requires that every word appears once, in order.  A spec file is JSON with any of
max_chars_per_line, max_lines (1 or 2), max_cps, min_duration_s, max_duration_s, min_gap_frames, pause_split_s.
Exit status 2: invalid words or spec, a cue list that breaks the spec, unreadable input.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import captions as cap  # noqa: E402


def _refuse_constant(name):
    raise cap.CaptionError(f"{name} is not a JSON number")


def load_json(path):
    try:
        return json.loads(Path(path).read_text(), parse_constant=_refuse_constant)
    except json.JSONDecodeError as error:
        raise cap.CaptionError(f"{path} is not valid JSON: {error}") from error


def load_spec(value):
    if value in cap.PRESETS:
        return cap.normalize_spec(value)
    if not Path(value).is_file():
        raise cap.CaptionError(f"{value!r} is neither a preset ({sorted(cap.PRESETS)}) nor a spec file")
    return cap.normalize_spec(load_json(value))


def load_words(path, fps):
    data = load_json(path)
    if isinstance(data, dict):
        fps = data.get("fps", fps)
        data = data.get("words")
    return data, fps


def emit(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False))


def build_files(words, fps, spec, out):
    result = cap.build(words, fps, spec)
    out.mkdir(parents=True, exist_ok=True)
    (out / "captions.srt").write_text(result["srt"], encoding="utf-8")
    (out / "captions.vtt").write_text(result["vtt"], encoding="utf-8")
    keys = ("start", "end", "start_ms", "end_ms", "lines")
    for name in ("captions.srt", "captions.vtt"):        # what is on disk, not what was meant to be written
        back = cap.parse((out / name).read_text(encoding="utf-8"), fps)
        if [{k: c[k] for k in keys} for c in back] != [{k: c[k] for k in keys} for c in result["cues"]]:
            raise cap.CaptionError(f"{name} does not read back as the cues that were written")
    preserved = cap.words_preserved(words, result["cues"])
    report = {k: result[k] for k in ("schema", "spec", "fps", "cues", "problems", "words", "srt_sha256", "vtt_sha256")}
    report["words_preserved"] = preserved
    if not preserved:
        report["problems"] = report["problems"] + ["the cue text is not the input words in order"]
    (out / "captions-report.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("output")
    build = sub.add_parser("build")
    build.add_argument("--words", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--fps", default="24")
    build.add_argument("--spec", default="broadcast")
    chk = sub.add_parser("check")
    chk.add_argument("--input", required=True)
    chk.add_argument("--fps", default="24")
    chk.add_argument("--spec", default="broadcast")
    chk.add_argument("--words")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            out = Path(args.output)
            out.mkdir(parents=True, exist_ok=True)
            words = cap.synthetic_words()
            (out / "words.json").write_text(json.dumps({"fps": 24, "words": words}, indent=2) + "\n")
            report = build_files(words, 24, "broadcast", out)
            emit({"cues": len(report["cues"]), "problems": report["problems"], "srt_sha256": report["srt_sha256"]})
            return 2 if report["problems"] else 0
        fps = int(args.fps) if args.fps.isdigit() else args.fps
        if args.command == "build":
            words, fps = load_words(args.words, fps)
            report = build_files(words, fps, load_spec(args.spec), Path(args.output))
            emit({"cues": len(report["cues"]), "problems": report["problems"], "words_preserved": report["words_preserved"],
                  "srt_sha256": report["srt_sha256"], "vtt_sha256": report["vtt_sha256"]})
            return 2 if report["problems"] else 0
        cues = cap.parse(Path(args.input).read_text(encoding="utf-8"), fps)
        problems = cap.check(cues, fps, load_spec(args.spec))
        if args.words:
            words, _ = load_words(args.words, fps)
            if not cap.words_preserved(words, cues):
                problems.append("the cue text is not the input words in order")
        emit({"cues": len(cues), "problems": problems})
        return 2 if problems else 0
    except (OSError, ValueError, KeyError, TypeError) as error:       # CaptionError is a ValueError
        emit({"errors": getattr(error, "errors", [str(error)])})
        return 2


if __name__ == "__main__":
    sys.exit(main())
