"""Fill digital-silence holes in dialogue with level-matched room tone taken from the same recording.

    python tools/room_tone_fill.py demo outputs/room-tone-demo
    python tools/room_tone_fill.py fill --input dialogue.wav --output filled.wav --report report.json [--seed 0]
    python tools/room_tone_fill.py scan --input dialogue.wav            # holes and tone regions only

Reads PCM 16-bit, PCM 32-bit or float32 WAV and writes the same sample format.  ``demo`` uses an
invented harmonic-burst stand-in for speech over a coloured room tone, so no recording is needed.
The report lists every hole, the tone used, the observed level and spectral match, whether any
digital silence remains and whether every sample outside the patched zones is bit-identical.
Exit status 2: unreadable audio, no room tone to borrow, or a check that failed.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import room_tone_fill as rt  # noqa: E402

LEVEL_TOLERANCE_DB = 2.0
EDGE_TOLERANCE_DB = 6.0
BAND_TOLERANCE_DB = 6.0


def read_wav(path):
    sr, data = wavfile.read(path)
    kind = data.dtype
    if kind == np.int16:
        audio = data.astype(np.float64) / 32768.0
    elif kind == np.int32:
        audio = data.astype(np.float64) / 2147483648.0
    elif kind == np.float32:
        audio = data.astype(np.float64)
    else:
        raise ValueError(f"unsupported WAV sample format {kind}; use PCM 16, PCM 32 or float32")
    return sr, audio, kind


def write_wav(path, sr, audio, kind):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if kind == np.int16:
        data = np.clip(np.round(audio * 32768.0), -32768, 32767).astype(np.int16)
    elif kind == np.int32:
        data = np.clip(np.round(audio * 2147483648.0), -2147483648, 2147483647).astype(np.int32)
    else:
        data = audio.astype(np.float32)
    wavfile.write(path, sr, data)


def run_fill(sr, audio, seed):
    filled, report = rt.fill_holes(audio, sr, seed=seed)
    return filled, rt.fill_report(audio, filled, sr, report)


def verdict(report):
    """Every way the fill falls outside tolerance; an empty list means all checks passed."""
    problems = []
    if report["remaining_digital_silence"]:
        problems.append(f"digital silence remains: {report['remaining_digital_silence']}")
    if not report.get("untouched_identical", True):
        problems.append("samples outside the patched zones changed")
    for check in report.get("checks", []):
        if check["level_error_db"] > LEVEL_TOLERANCE_DB:
            problems.append(f"hole at {check['start_s']} s: level off by {check['level_error_db']} dB")
        if check["edge_level_deviation_db"] is not None and check["edge_level_deviation_db"] > EDGE_TOLERANCE_DB:
            problems.append(f"hole at {check['start_s']} s: edge level step {check['edge_level_deviation_db']} dB")
        if check["band_shape_deviation_db"] is not None and check["band_shape_deviation_db"] > BAND_TOLERANCE_DB:
            problems.append(f"hole at {check['start_s']} s: octave-band shape off by {check['band_shape_deviation_db']} dB")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("output")
    demo.add_argument("--seed", type=int, default=0)
    for name in ("fill", "scan"):
        p = sub.add_parser(name)
        p.add_argument("--input", required=True)
        if name == "fill":
            p.add_argument("--output", required=True)
            p.add_argument("--report")
            p.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            audio, meta = rt.synthetic_dialogue()
            sr, kind = meta["sample_rate"], np.int16
            out = Path(args.output)
            write_wav(out / "dialogue-with-holes.wav", sr, audio, kind)
            sr, audio, kind = read_wav(out / "dialogue-with-holes.wav")
        else:
            sr, audio, kind = read_wav(args.input)
        if args.command == "scan":
            print(json.dumps({"sample_rate": sr, "seconds": round(len(audio) / sr, 3),
                              "holes": [[round(s / sr, 4), round(e / sr, 4)] for s, e in rt.find_holes(audio, sr)],
                              "tone_regions": [[round(s / sr, 4), round(e / sr, 4)] for s, e in rt.find_tone_regions(audio, sr)]}))
            return 0
        filled, report = run_fill(sr, audio, args.seed)
    except (OSError, ValueError, rt.ToneError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    problems = verdict(report)
    report["problems"] = problems
    if args.command == "demo":
        write_wav(out / "filled.wav", sr, filled, kind)
        (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        if not problems:
            write_wav(args.output, sr, filled, kind)
        if args.report:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"holes_filled": report["holes_filled"], "problems": problems,
                      "output_sha256": report["output_sha256"]}, sort_keys=True))
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
