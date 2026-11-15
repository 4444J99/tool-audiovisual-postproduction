"""Measure, check and normalise programme loudness (BS.1770-4 / EBU R128) and true peak.

    python tools/loudness.py demo outputs/loudness-demo
    python tools/loudness.py measure   --input mix.wav [--layout stereo|mono|5.1|1,1,1,0,1.41,1.41]
    python tools/loudness.py check     --input mix.wav --spec ebu_r128|atsc_a85|streaming|podcast|spec.json
    python tools/loudness.py normalize --input mix.wav --output out.wav --spec ebu_r128 [--report r.json]
                                       [--target-lufs -23] [--ceiling-dbtp -1] [--on-ceiling refuse|cap]

Reads PCM 16-bit, PCM 32-bit or float32 WAV and writes the same sample format.  ``normalize`` applies one
linear gain, writes the file, reads the written file back and measures that, so the report states what
the bytes on disk measure.  If the gain needed to reach the target would put the true peak over the
ceiling, ``--on-ceiling refuse`` (default) writes nothing and exits 2; ``cap`` lowers the gain to the
ceiling and reports how far short of the target the result stays.  No limiter is applied.
A spec file is JSON: {"name": "...", "integrated_lufs": {"target": -23, "tolerance": 0.5},
"true_peak_dbtp_max": -1, "loudness_range_max_lu": 20}.
Exit status 2: unreadable or too-short audio, a spec the audio fails, or a refused gain.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import loudness as ld  # noqa: E402


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


def encode(audio, kind):
    if kind == np.int16:
        return np.clip(np.round(audio * 32768.0), -32768, 32767).astype(np.int16)
    if kind == np.int32:
        return np.clip(np.round(audio * 2147483648.0), -2147483648, 2147483647).astype(np.int32)
    return audio.astype(np.float32)


def write_wav(path, sr, audio, kind):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(path, sr, encode(audio, kind))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _refuse_constant(name):
    raise ld.LoudnessError(f"spec contains the non-JSON number {name}")


def load_spec(value):
    if value in ld.PRESETS:
        return ld.normalize_spec(value)
    path = Path(value)
    if not path.is_file():
        raise ld.LoudnessError(f"{value!r} is neither a preset ({sorted(ld.PRESETS)}) nor a spec file")
    try:
        return ld.normalize_spec(json.loads(path.read_text(), parse_constant=_refuse_constant))
    except json.JSONDecodeError as error:
        raise ld.LoudnessError(f"spec file {value} is not valid JSON: {error}") from error


def emit(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("output")
    for name in ("measure", "check", "normalize"):
        p = sub.add_parser(name)
        p.add_argument("--input", required=True)
        p.add_argument("--layout")
        if name in ("check", "normalize"):
            p.add_argument("--spec")
        if name == "normalize":
            p.add_argument("--output", required=True)
            p.add_argument("--report")
            p.add_argument("--target-lufs", type=float)
            p.add_argument("--ceiling-dbtp", type=float)
            p.add_argument("--on-ceiling", choices=("refuse", "cap"), default="refuse")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            out = Path(args.output)
            programme = ld.synthetic_programme() * 0.35        # 9 dB down, so the demo has real gain to apply
            write_wav(out / "programme.wav", 48000, programme, np.int16)
            sr, audio, kind = read_wav(out / "programme.wav")
            before = ld.measure(audio, sr)
            gain = ld.plan_gain(before, -23.0, -1.0)
            write_wav(out / "normalized.wav", sr, ld.apply_gain(audio, gain["gain_db"]), kind)
            after = ld.measure(read_wav(out / "normalized.wav")[1], sr)
            problems = ld.check(after, "ebu_r128")
            (out / "report.json").write_text(json.dumps({"before": before, "plan": gain, "after": after, "problems": problems},
                                                        indent=2, sort_keys=True) + "\n")
            emit({"before_lufs": before["integrated_lufs"], "after_lufs": after["integrated_lufs"],
                  "true_peak_dbtp": after["true_peak_dbtp"], "problems": problems, "output_sha256": sha256_file(out / "normalized.wav")})
            return 2 if problems else 0
        sr, audio, kind = read_wav(args.input)
        measurement = ld.measure(audio, sr, args.layout)
        if args.command == "measure":
            emit(measurement)
            return 0
        if args.command == "check":
            if not args.spec:
                raise ld.LoudnessError("check needs --spec")
            problems = ld.check(measurement, load_spec(args.spec))
            emit({"integrated_lufs": measurement["integrated_lufs"], "true_peak_dbtp": measurement["true_peak_dbtp"],
                  "loudness_range_lu": measurement["loudness_range_lu"], "problems": problems})
            return 2 if problems else 0
        spec = load_spec(args.spec) if args.spec else None
        target = args.target_lufs if args.target_lufs is not None else (spec["integrated_lufs"]["target"] if spec else None)
        ceiling = args.ceiling_dbtp if args.ceiling_dbtp is not None else (spec.get("true_peak_dbtp_max") if spec else None)
        if target is None:
            raise ld.LoudnessError("normalize needs --spec or --target-lufs")
        plan = ld.solve_gain(audio, sr, target, ceiling, args.on_ceiling, args.layout)
        report = {"input": args.input, "input_sha256": sha256_file(args.input), "before": measurement, "plan": plan, "spec": spec}
        if not plan["ok"]:
            report["problems"] = [plan["reason"]]
            write_report(args.report, report)
            emit({"problems": report["problems"]})
            return 2
        gain = plan["gain_db"]
        for attempt in range(1, 5):
            scaled = ld.apply_gain(audio, gain)
            if kind != np.float32 and float(np.abs(scaled).max()) >= 1.0:
                report["problems"] = ["the gain would clip the sample format; nothing was written"]
                write_report(args.report, report)
                emit({"problems": report["problems"]})
                return 2
            write_wav(args.output, sr, scaled, kind)
            after = ld.measure(read_wav(args.output)[1], sr, args.layout)
            excess = after["true_peak_dbtp"] - ceiling if ceiling is not None else 0.0
            if excess <= 1e-9:
                break
            gain -= excess + 0.002        # integer rounding put a sample over the ceiling: back off and rewrite
        plan["gain_db"], plan["write_attempts"] = round(gain, 4), attempt
        report.update(output=args.output, output_sha256=sha256_file(args.output), after=after)
        problems = ld.check(after, spec) if spec else ([] if plan.get("capped") else ld.check(after, {"integrated_lufs": {"target": target, "tolerance": 0.1}}))
        if ceiling is not None and after["true_peak_dbtp"] > ceiling + 1e-9 and not any("true peak" in p for p in problems):
            problems.append(f"true peak {after['true_peak_dbtp']} dBTP is over the {ceiling} dBTP ceiling after writing")
        report["problems"] = problems
        write_report(args.report, report)
        emit({"gain_db": plan["gain_db"], "capped": plan.get("capped", False), "shortfall_lu": plan.get("shortfall_lu", 0.0), "before_lufs": measurement["integrated_lufs"],
              "after_lufs": after["integrated_lufs"], "true_peak_dbtp": after["true_peak_dbtp"], "problems": problems,
              "output_sha256": report["output_sha256"]})
        return 2 if problems else 0
    except (OSError, ValueError) as error:       # LoudnessError and wavfile errors are ValueError
        emit({"error": str(error)})
        return 2


def write_report(path, report):
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    sys.exit(main())
