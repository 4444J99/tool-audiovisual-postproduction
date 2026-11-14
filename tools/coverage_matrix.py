"""Check actor coverage of a shot plan and propose shots for the gaps.

    python tools/coverage_matrix.py --demo outputs/coverage-demo
    python tools/coverage_matrix.py --plan plan.json --coverage coverage.json [--output outputs/coverage]
    python tools/coverage_matrix.py --plan plan.json --coverage coverage.json --suggest --output outputs/coverage

Reads a shot plan (``amps.shot_plan.v1``, performers may carry ``facing_deg`` or ``facing_toward``) and a
requirement (``amps.coverage_matrix.v1``: required actor/size/angle cells, dialogue lines, recorded room
tone).  Writes ``coverage-report.json`` and ``coverage-matrix.md``; with ``--suggest`` also
``suggested-plan.json`` (the plan plus one validated shot per gap, ready for ``tools/shot_plan.py``) and
``suggestions.json``.  Exit status 2 when the plan has unmet requirements, after writing the report.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import coverage_matrix as cm  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", metavar="DIR", help="synthetic round-table pilot with deliberate gaps; reads no media")
    parser.add_argument("--plan")
    parser.add_argument("--coverage")
    parser.add_argument("--output", default="outputs/coverage")
    parser.add_argument("--suggest", action="store_true")
    args = parser.parse_args(argv)
    if args.demo:
        plan, spec = cm.synthetic_inputs()
        args.suggest = True
        out = Path(args.demo)
    elif args.plan and args.coverage:
        try:
            plan = json.loads(Path(args.plan).read_text())
            spec = json.loads(Path(args.coverage).read_text())
        except (OSError, json.JSONDecodeError) as error:
            print(json.dumps({"error": f"cannot read input: {error}"}))
            return 2
        out = Path(args.output)
    else:
        print(json.dumps({"error": "give --demo, or --plan and --coverage"}))
        return 2
    report = cm.validate_coverage(plan, spec)
    out.mkdir(parents=True, exist_ok=True)
    (out / "coverage-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out / "coverage-matrix.md").write_text(cm.render_table(report))
    summary = {"errors": report["errors"], "warnings": report["warnings"], "gaps": len(report["gaps"])}
    if args.suggest and report["matrix"]:
        suggestion = cm.suggest_shots(plan, spec)
        (out / "suggestions.json").write_text(json.dumps({"shots": suggestion["shots"], "unplaced": suggestion["unplaced"]},
                                                         indent=2, sort_keys=True) + "\n")
        (out / "suggested-plan.json").write_text(json.dumps(suggestion["plan"], indent=2, sort_keys=True) + "\n")
        after = cm.validate_coverage(suggestion["plan"], spec)
        summary.update(suggested=len(suggestion["shots"]), unplaced=len(suggestion["unplaced"]),
                       errors_after_suggestions=after["errors"])
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 2 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
