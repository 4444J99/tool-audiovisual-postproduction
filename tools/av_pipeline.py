"""Run and verify the synthetic end-to-end chain: set spec -> shot plan -> coverage -> room-tone fill -> colour plan.

    python tools/av_pipeline.py run --output outputs/av-pipeline [--blender] [--delivery ebu_r128|atsc_a85|streaming|podcast|spec.json]
    python tools/av_pipeline.py verify --output outputs/av-pipeline [--delivery ebu_r128|...|spec.json]

``run`` writes every stage's artefacts and ``pipeline-manifest.json`` (each stage's input and output SHA-256).
``--blender`` also builds the set spec in headless Blender through ``tools/set_scene.py`` and requires
Blender's graph hash to equal the set stage's.  ``verify`` recomputes every hash on disk, checks that every
stage input is an earlier stage's output, and checks the content links (same plan, graph and audio).
The ``delivery`` stage measures the filled dialogue, applies one linear gain to the declared spec (default EBU R128),
and reads the written file back; ``verify --delivery X`` also requires the chain to have been delivered to X and
re-derives the delivered WAV from the filled one.  Every failure prints as JSON and exits 2.  Nothing here reads private media; every input is invented.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import pipeline_chain as pc  # noqa: E402


def blender_stage(out):
    run = subprocess.run([sys.executable, str(ROOT / "tools" / "set_scene.py"), "build", "--spec", str(out / "set" / "set-spec.json"),
                          "--blend", str(out / "set" / "scene.blend"), "--report", str(out / "set" / "blender-report.json")],
                         capture_output=True, text=True, timeout=300)
    if run.returncode != 0:
        return None, run.stdout[-1500:] + run.stderr[-500:]
    line = [l for l in run.stdout.splitlines() if l.startswith("SET_SCENE_OK")][-1]
    return json.loads(line.split(" ", 1)[1]), None


def load_spec(value):
    """A preset name, or a JSON spec file validated by the loudness module (the same loader as tools/loudness.py)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import loudness
    return loudness.load_spec(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("run", "verify"))
    parser.add_argument("--output", default="outputs/av-pipeline")
    parser.add_argument("--blender", action="store_true")
    parser.add_argument("--delivery", help="loudness preset or spec JSON file (run default ebu_r128; verify: require this spec)")
    args = parser.parse_args(argv)
    out = Path(args.output)
    try:
        delivery = load_spec(args.delivery) if args.delivery else None
    except (ValueError, OSError) as error:
        print(json.dumps({"errors": [f"delivery spec: {error}"]}, indent=2))
        return 2
    if args.command == "run":
        try:
            manifest = pc.run_chain(out, delivery=delivery or pc.DEFAULT_DELIVERY)
        except pc.ChainError as error:
            print(json.dumps({"errors": error.errors}, indent=2))
            return 2
        result = {"chain_sha256": manifest["chain_sha256"], "delivery": {k: next(x for x in manifest["stages"] if x["stage"] == "delivery")[k] for k in ("spec_name", "gain_db", "before_lufs", "after_lufs", "after_true_peak_dbtp")}, "stages": [s["stage"] for s in manifest["stages"]]}
        if args.blender:
            built, failure = blender_stage(out)
            expected = manifest["stages"][0]["graph_sha256"]
            if failure or built["graph_sha256"] != expected:
                print(json.dumps({"error": "Blender build failed or its graph hash differs", "detail": failure,
                                  "expected": expected, "observed": (built or {}).get("graph_sha256")}))
                return 2
            try:
                result["chain_sha256"] = pc.append_blender_stage(out, built["graph_sha256"])["chain_sha256"]
            except pc.ChainError as error:
                print(json.dumps({"errors": error.errors}, indent=2))
                return 2
            result["blender_graph_sha256"] = built["graph_sha256"]
            result["stages"].append("blender")
        problems = pc.verify_chain(out) + pc.cross_checks(out, delivery)
        if problems:
            print(json.dumps({"errors": problems}, indent=2))
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    try:
        problems = pc.verify_chain(out)
        problems += pc.cross_checks(out, delivery) if not problems else []
    except (OSError, KeyError, json.JSONDecodeError) as error:
        problems = [f"cannot verify: {error}"]
    if problems:
        print(json.dumps({"errors": problems}, indent=2))
        return 2
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
