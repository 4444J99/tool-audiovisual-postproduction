"""Validate a set spec and build its Blender scene.

    python tools/set_scene.py demo-spec out/spec.json
    python tools/set_scene.py validate --spec set.json
    python tools/set_scene.py graph --spec set.json            # scene graph summary, no Blender
    python tools/set_scene.py shot-set --spec set.json         # the `set` block for a shot plan
    python tools/set_scene.py build --spec set.json --blend out/scene.blend --report out/report.json \
        [--still out/still.png --width 320 --height 180 --samples 16]

``build`` runs Blender headless (``$AMP_BLENDER``, ``blender`` on PATH, or the macOS application) with
``tools/blender_set_scene.py`` and fails if Blender's measured scene differs from the scene graph.
Every other command is plain Python.  A spec that fails validation prints every failure as JSON,
exits 2 and writes nothing.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import set_spec  # noqa: E402

MACOS_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"


def find_blender():
    for candidate in (os.environ.get("AMP_BLENDER"), shutil.which("blender"), MACOS_BLENDER):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def compare(expected, observed):
    """List every way the measured scene differs from the scene graph (empty when they agree)."""
    problems = []
    for key in ("mesh_count", "light_count", "camera_count"):
        if expected[key] != observed[key]:
            problems.append(f"{key}: expected {expected[key]}, Blender holds {observed[key]}")
    if expected["active_camera"] != observed["active_camera"]:
        problems.append(f"active camera: expected {expected['active_camera']}, Blender uses {observed['active_camera']}")
    by_name = {row["name"]: row for row in observed["objects"]}
    for row in expected["objects"]:
        seen = by_name.get(row["name"])
        if not seen or not seen.get("present"):
            problems.append(f"{row['name']}: missing from the Blender scene")
            continue
        if any(abs(a - b) > 1e-4 for a, b in zip(row["location"], seen.get("location", []))) or len(seen.get("location", [])) != 3:
            problems.append(f"{row['name']}: location expected {row['location']}, Blender holds {seen.get('location')}")
        if row["type"] != "mesh":
            continue
        for key in ("vertices", "faces"):
            if row[key] != seen[key]:
                problems.append(f"{row['name']}: {key} expected {row[key]}, Blender holds {seen[key]}")
        if any(abs(a - b) > 1e-4 for a, b in zip(row["dimensions"], seen["dimensions"])):
            problems.append(f"{row['name']}: dimensions expected {row['dimensions']}, Blender measures {seen['dimensions']}")
        if row["materials"] != seen["materials"]:
            problems.append(f"{row['name']}: materials expected {row['materials']}, Blender holds {seen['materials']}")
    return problems


def load_spec(path):
    try:
        return json.loads(Path(path).read_text()), None
    except (OSError, json.JSONDecodeError) as error:
        return None, f"cannot read spec: {error}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo-spec")
    demo.add_argument("output")
    for name in ("validate", "graph", "shot-set", "build"):
        p = sub.add_parser(name)
        p.add_argument("--spec", required=True)
        if name == "build":
            p.add_argument("--blend")
            p.add_argument("--report")
            p.add_argument("--still")
            p.add_argument("--width", type=int, default=320)
            p.add_argument("--height", type=int, default=180)
            p.add_argument("--samples", type=int, default=16)
            p.add_argument("--camera")
            p.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args(argv)
    if args.command == "demo-spec":
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(set_spec.synthetic_spec(), indent=2, sort_keys=True) + "\n")
        return 0
    spec, problem = load_spec(args.spec)
    if problem:
        print(json.dumps({"error": problem}))
        return 2
    report = set_spec.validate_spec(spec)
    if report["errors"]:
        print(json.dumps({"errors": report["errors"], "warnings": report["warnings"]}, indent=2))
        return 2
    if args.command == "validate":
        print(json.dumps({"ok": True, "warnings": report["warnings"], "spec_sha256": set_spec.spec_hash(spec)}, indent=2))
        return 0
    if args.command == "graph":
        print(json.dumps(set_spec.graph_summary(set_spec.scene_graph(spec)), indent=2, sort_keys=True))
        return 0
    if args.command == "shot-set":
        print(json.dumps(set_spec.shot_plan_set(spec), indent=2, sort_keys=True))
        return 0
    blender = find_blender()
    if not blender:
        print(json.dumps({"error": "Blender not found; set AMP_BLENDER or put blender on PATH"}))
        return 2
    scratch = None
    report_path = args.report
    if not report_path:
        scratch = tempfile.TemporaryDirectory()
        report_path = str(Path(scratch.name) / "report.json")
    command = [blender, "-b", "--factory-startup", "--python", str(ROOT / "tools" / "blender_set_scene.py"), "--",
               "--spec", args.spec, "--report", report_path, "--width", str(args.width), "--height", str(args.height),
               "--samples", str(args.samples)]
    for flag in ("blend", "still", "camera"):
        if getattr(args, flag):
            command += [f"--{flag}", getattr(args, flag)]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
        if run.returncode != 0 or "SET_SCENE_OK" not in run.stdout:
            print(json.dumps({"error": "Blender build failed", "returncode": run.returncode, "tail": run.stdout[-1500:] + run.stderr[-1500:]}))
            return 2
        data = json.loads(Path(report_path).read_text())
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": f"Blender exceeded {args.timeout} s"}))
        return 2
    finally:
        if scratch:
            scratch.cleanup()
    mismatches = compare(data["expected"], data["observed"])      # always checked, with or without --report
    if mismatches:
        print(json.dumps({"error": "Blender scene differs from the scene graph", "mismatches": mismatches}, indent=2))
        return 2
    print([line for line in run.stdout.splitlines() if line.startswith("SET_SCENE_OK")][-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
