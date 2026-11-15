"""Burn SRT/WebVTT caption cues into synthetic frames and verify the burned frames by re-deriving them.

    python tools/caption_burnin.py demo --output outputs/caption-burnin [--preview]
    python tools/caption_burnin.py burn --captions captions.srt --output outputs/caption-burnin --fps 24 --frames 240 [--width 320 --height 180 --scale 1] [--preview]
    python tools/caption_burnin.py verify --captions captions.srt --output outputs/caption-burnin
    python tools/caption_burnin.py show --output outputs/caption-burnin --frame 60

``burn`` writes ``frames.y8`` (8-bit luma frames back to back), ``burnin-report.json`` (parameters, every frame's
SHA-256, the hash-chain head, the cues as burned) and ``contact-sheet.pgm`` (the middle frame of each cue).
``verify`` re-renders every frame from the caption file and the report's parameters and compares them byte for byte
with ``frames.y8``; it reports the first frame that differs.  ``show`` prints one frame as text.  ``--preview``
also wraps the frames in a lossless FFV1 file with ffmpeg (``preview.mkv``), checks that decoding it returns the
same bytes, and records nothing about it in the chain: the file is a convenience for looking at the result.
Exit status 2: invalid captions, geometry or frames, a cue that cannot be drawn, or a verification failure.
Everything is invented; nothing here reads private media.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import caption_burnin as cb  # noqa: E402
from amp.studies import captions as cap  # noqa: E402


def emit(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False))


def contact_sheet(payload, report):
    """The middle frame of each cue side by side, as a binary PGM (P5)."""
    import numpy as np
    geometry = report["params"]["geometry"]
    w, h = geometry["width"], geometry["height"]
    picks = [(c["start"] + c["end"] - 1) // 2 for c in report["cues"]]
    rows = [np.frombuffer(payload[f * w * h:(f + 1) * w * h], dtype=np.uint8).reshape(h, w) for f in picks]
    sheet = np.concatenate(rows, axis=0)
    return b"P5\n%d %d\n255\n" % (sheet.shape[1], sheet.shape[0]) + sheet.tobytes()


def preview(out, report):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"preview": "skipped: ffmpeg not found"}
    p = report["params"]
    size = f"{p['geometry']['width']}x{p['geometry']['height']}"
    target = out / "preview.mkv"
    run = subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", size, "-r", p["fps"],
                          "-i", str(out / cb.FRAMES_FILE), "-c:v", "ffv1", str(target)], capture_output=True, text=True)
    if run.returncode != 0:
        return {"preview": "failed", "detail": run.stderr[-300:]}
    back = subprocess.run([ffmpeg, "-v", "error", "-i", str(target), "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
    same = back.returncode == 0 and back.stdout == (out / cb.FRAMES_FILE).read_bytes()
    return {"preview": str(target), "preview_decodes_to_the_same_frames": same}


def burn_to(captions_path, out, fps, frames, geometry, with_preview):
    text = cb.read_captions(captions_path)
    payload, report = cb.burn(text, fps, frames, geometry)
    out.mkdir(parents=True, exist_ok=True)
    (out / cb.FRAMES_FILE).write_bytes(payload)
    (out / "burnin-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out / "contact-sheet.pgm").write_bytes(contact_sheet(payload, report))
    result = {"chain_head": report["chain_head"], "frames": frames, "captioned_frames": report["captioned_frames"],
              "cues": len(report["cues"]), "frames_sha256": report["frames_sha256"]}
    problems = cb.verify((out / cb.FRAMES_FILE).read_bytes(), json.loads((out / "burnin-report.json").read_text()), text)
    if problems:
        raise cb.BurnError([f"written frames fail verification: {p}" for p in problems])
    if with_preview:
        result |= preview(out, report)
        if result.get("preview_decodes_to_the_same_frames") is False:
            raise cb.BurnError("the FFV1 preview does not decode to the burned frames")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("demo", "burn", "verify", "show"))
    parser.add_argument("--captions")
    parser.add_argument("--output", default="outputs/caption-burnin")
    parser.add_argument("--fps", default="24")
    parser.add_argument("--frames", type=int)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--scale", type=int)
    parser.add_argument("--frame", type=int, default=0)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.output)
    geometry = {k: v for k, v in (("width", args.width), ("height", args.height), ("scale", args.scale)) if v is not None}
    try:
        if args.command == "demo":
            out.mkdir(parents=True, exist_ok=True)
            built = cap.build(cap.synthetic_words(), 24, "broadcast")
            (out / "captions.srt").write_text(built["srt"], encoding="utf-8")
            (out / "captions.vtt").write_text(built["vtt"], encoding="utf-8")
            frames = args.frames or built["cues"][-1]["end"] + 24
            emit(burn_to(out / "captions.srt", out, 24, frames, geometry, args.preview))
            return 0
        if args.command == "burn":
            if not args.captions or not args.frames:
                parser.error("burn needs --captions and --frames")
            emit(burn_to(args.captions, out, args.fps, args.frames, geometry, args.preview))
            return 0
        report = json.loads((out / "burnin-report.json").read_text())
        payload = (out / cb.FRAMES_FILE).read_bytes()
        if args.command == "show":
            g = report["params"]["geometry"]
            size = g["width"] * g["height"]
            if not 0 <= args.frame < report["params"]["frames"]:
                raise cb.BurnError(f"frame {args.frame} is outside 0-{report['params']['frames'] - 1}")
            print(cb.ascii_art(payload[args.frame * size:(args.frame + 1) * size], g["width"], g["height"]))
            return 0
        if not args.captions:
            parser.error("verify needs --captions")
        problems = cb.verify(payload, report, cb.read_captions(args.captions))
        if problems:
            emit({"errors": problems})
            return 2
        emit({"ok": True, "chain_head": report["chain_head"]})
        return 0
    except (cb.BurnError, cap.CaptionError) as error:
        emit({"errors": error.errors if isinstance(error, cb.BurnError) else (error.args[0] if isinstance(error.args[0], list) else [str(error)])})
        return 2
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        emit({"errors": [f"cannot read: {error}"]})
        return 2


if __name__ == "__main__":
    sys.exit(main())
