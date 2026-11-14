"""Colour grade chains and layered compositing plans.

    python tools/colour_layers.py demo DIR
    python tools/colour_layers.py grade --chain chain.json --input in.png --output out.png
    python tools/colour_layers.py validate-chain --chain chain.json
    python tools/colour_layers.py validate-plan --plan plan.json [--ledger ledger.json]
    python tools/colour_layers.py identity-lut SIZE --output identity.cube

Every failure prints as JSON and exits 2; a failing plan or chain writes nothing.  ``demo`` uses only
synthetic frames and invented sources.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from amp.studies import colour_grade as cg, layer_plan as lp  # noqa: E402


def _fail(payload):
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2


def _load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(_fail({"error": f"cannot read {path}: {error}"}))


def synthetic_loader(plan):
    """Deterministic RGBA frames per source id, derived from nothing but the id and frame."""
    h, w = plan["comp"]["height"], plan["comp"]["width"]
    xs = (np.arange(w) + 0.5) / w
    ys = (np.arange(h) + 0.5) / h

    def load(source, frame):
        if source == "room":
            base = np.stack([np.broadcast_to(0.05 + 0.25 * xs[None, :], (h, w)),
                             np.broadcast_to(0.04 + 0.15 * ys[:, None], (h, w)),
                             np.full((h, w), 0.06)], axis=-1)
        elif source == "glow":
            pulse = 0.55 + 0.45 * np.sin(frame / 3.0)
            cx = np.exp(-(((xs[None, :] - 0.5) / 0.18) ** 2 + ((ys[:, None] - 0.7) / 0.25) ** 2))
            base = np.stack([cx * pulse, cx * pulse * 0.45, cx * pulse * 0.08], axis=-1)
        else:
            r = np.hypot(xs[None, :] - 0.5, (ys[:, None] - 0.5) * h / w)
            v = np.clip(1.0 - r * 1.6, 0.0, 1.0)
            base = np.stack([v, v, v], axis=-1)
        return np.concatenate([base, np.ones((h, w, 1))], axis=-1)
    return load


def cmd_demo(args):
    out = Path(args.dir)
    chain = cg.synthetic_chain()
    chart = cg.synthetic_chart()
    graded = cg.apply_display(chart, chain)
    plan = lp.synthetic_plan()
    report = lp.validate_plan(plan)
    if report["errors"]:
        return _fail(report)
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(chart).resize((384, 256), Image.NEAREST).save(out / "chart-before.png")
    Image.fromarray(graded).resize((384, 256), Image.NEAREST).save(out / "chart-after.png")
    grade = cg.grade_report(chart, chain)
    (out / "grade-report.json").write_text(json.dumps(grade, indent=2, sort_keys=True) + "\n")
    load = synthetic_loader(plan)
    frames = {}
    for frame in (0, 24):
        flat = cg.encode_frame(lp.flatten(plan, frame, load))
        Image.fromarray(flat).resize((384, 216), Image.NEAREST).save(out / f"comp-{frame:03d}.png")
        frames[str(frame)] = hashlib.sha256(flat.tobytes()).hexdigest()
    ledger = lp.layer_ledger(plan)
    (out / "layer-ledger.json").write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"grade": grade, "comp_sha256": frames, "plan_sha256": ledger["plan_sha256"],
                      "warnings": report["warnings"]}, indent=2, sort_keys=True))
    return 0


def cmd_grade(args):
    chain = _load_json(args.chain)
    errors = cg.validate_chain(chain)
    if errors:
        return _fail({"errors": errors})
    with Image.open(args.input) as image:
        frame = np.asarray(image.convert("RGB"), dtype=np.uint8)
    Image.fromarray(cg.apply_display(frame, chain)).save(args.output)
    print(json.dumps(cg.grade_report(frame, chain), indent=2, sort_keys=True))
    return 0


def cmd_validate_chain(args):
    errors = cg.validate_chain(_load_json(args.chain))
    if errors:
        return _fail({"errors": errors})
    print(json.dumps({"ok": True, "chain_sha256": cg.chain_hash(_load_json(args.chain))}))
    return 0


def cmd_validate_plan(args):
    plan = _load_json(args.plan)
    report = lp.validate_plan(plan)
    if report["errors"]:
        return _fail(report)
    if args.ledger:
        Path(args.ledger).write_text(json.dumps(lp.layer_ledger(plan), indent=2, sort_keys=True) + "\n")
    print(json.dumps({"ok": True, "warnings": report["warnings"], "plan_sha256": lp.plan_hash(plan)}, indent=2))
    return 0


def cmd_identity_lut(args):
    if not 2 <= args.size <= cg.MAX_LUT_SIZE:
        return _fail({"error": f"size must be 2..{cg.MAX_LUT_SIZE}"})
    Path(args.output).write_text(cg.dumps_cube(cg.identity_lut(args.size), "identity"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo"); p.add_argument("dir"); p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("grade"); p.add_argument("--chain", required=True); p.add_argument("--input", required=True)
    p.add_argument("--output", required=True); p.set_defaults(fn=cmd_grade)
    p = sub.add_parser("validate-chain"); p.add_argument("--chain", required=True); p.set_defaults(fn=cmd_validate_chain)
    p = sub.add_parser("validate-plan"); p.add_argument("--plan", required=True); p.add_argument("--ledger")
    p.set_defaults(fn=cmd_validate_plan)
    p = sub.add_parser("identity-lut"); p.add_argument("size", type=int); p.add_argument("--output", required=True)
    p.set_defaults(fn=cmd_identity_lut)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
