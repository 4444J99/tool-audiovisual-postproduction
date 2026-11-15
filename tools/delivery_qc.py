"""Judge the sealed artefacts of a pipeline output directory (a delivery QC report), without sealing anything.

    python tools/delivery_qc.py check --output outputs/av-pipeline [--qc-spec spec.json] [--write qc.json]
    python tools/delivery_qc.py verify --output outputs/av-pipeline [--qc-spec spec.json]

``check`` derives the report from the files on disk and prints it (exit 2 when any check fails); ``--write`` also
saves it.  It is how a failing delivery is looked at: ``av_pipeline.py run`` refuses to seal one.  ``verify``
re-derives ``qc/qc-report.json`` and requires the file on disk to be byte-equal and every check to pass.
Nothing here reads private media; the pipeline's inputs are invented.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import delivery_qc as qc  # noqa: E402
from amp.studies import pipeline_chain as pc  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "verify"))
    parser.add_argument("--output", default="outputs/av-pipeline")
    parser.add_argument("--qc-spec", help="spec JSON overriding keys of delivery_qc.DEFAULT_SPEC")
    parser.add_argument("--write", help="check: also write the derived report to this path")
    args = parser.parse_args(argv)
    out = Path(args.output)
    try:
        spec = qc.normalize_spec(json.loads(Path(args.qc_spec).read_text()) if args.qc_spec else None)
    except (qc.QcError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"errors": [f"qc spec: {error}"]}, indent=2))
        return 2
    if args.command == "check":
        try:
            report = qc.derive(out, pc.LOCATIONS, spec)
        except qc.QcError as error:
            print(json.dumps({"errors": [str(error)]}, indent=2))
            return 2
        if args.write:
            Path(args.write).write_text(qc.dumps(report))
        print(qc.dumps(report), end="")
        return 0 if report["status"] == "pass" else 2
    problems = pc.qc_checks(out, spec)
    if problems:
        print(json.dumps({"errors": problems}, indent=2))
        return 2
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
