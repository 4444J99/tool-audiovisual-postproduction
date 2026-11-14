"""Validate a shot plan and write its ledger, EDL and animatic.

    python tools/shot_plan.py --demo outputs/shot-plan-demo [--video]
    python tools/shot_plan.py --plan plan.json --output outputs/shot-plan [--video]
    python tools/shot_plan.py --plan plan.json --validate-only

Reads only the plan named on the command line.  A plan that fails any check prints every failure as
JSON, exits 2 and creates no output directory.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from amp.studies import shot_animatic, shot_plan as sp  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", metavar="DIR", help="write the synthetic three-shot pilot; reads no media")
    parser.add_argument("--plan", help="shot plan JSON (amps.shot_plan.v1)")
    parser.add_argument("--output", default="outputs/shot-plan")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--video", action="store_true", help="also encode animatic.mp4 with ffmpeg")
    args = parser.parse_args(argv)
    if bool(args.demo) == bool(args.plan):
        print(json.dumps({"error": "give exactly one of --demo or --plan"}))
        return 2
    try:
        plan = sp.synthetic_plan() if args.demo else json.loads(Path(args.plan).read_text())
    except (OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": f"cannot read plan: {error}"}))
        return 2
    report = sp.validate_plan(plan)
    if report["errors"]:
        print(json.dumps({"errors": report["errors"], "warnings": report["warnings"]}, indent=2))
        return 2
    if args.validate_only:
        print(json.dumps({"ok": True, "warnings": report["warnings"], "plan_sha256": sp.plan_hash(plan)}, indent=2))
        return 0
    try:
        summary = shot_animatic.write_animatic(plan, Path(args.demo or args.output), video=args.video)
    except (RuntimeError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    summary["warnings"] = report["warnings"]
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
