"""Plan a reverse angle and reproject observed source pixels onto it.

    python tools/reverse_angle.py --demo outputs/reverse-demo
    python tools/reverse_angle.py --frame frame.png --proxy proxy.json \
        --performer-a 0 0 2.5 --performer-b 0.2 0 4.2 --subject-mask mask.png --output outputs/reverse

Writes reverse-angle.png (observed pixels; unobserved black or labelled placeholder), status.png
(observed green, outside the source frustum blue, performer-occluded red), and ledger.json.
Only the files named on the command line are read.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from amp.studies import reverse_angle as ra  # noqa: E402
from amp.studies.depth_world import reconstruct_room_proxy_geometry  # noqa: E402

PALETTE = {ra.OBSERVED: (40, 170, 90), ra.OUTSIDE_FRUSTUM: (50, 90, 200), ra.SUBJECT_OCCLUDED: (210, 60, 60)}


def write_outputs(reconstruction, plan, fill, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    image, _ = ra.fill_unobserved(reconstruction, fill)
    Image.fromarray(np.clip(image, 0, 255).astype(np.uint8)).save(output / "reverse-angle.png")
    status = np.zeros(reconstruction.status.shape + (3,), dtype=np.uint8)
    for code, colour in PALETTE.items():
        status[reconstruction.status == code] = colour
    Image.fromarray(status).save(output / "status.png")
    ledger = ra.build_ledger(reconstruction, fill_mode=fill, plan=plan)
    (output / "ledger.json").write_text(ra.ledger_json(ledger) + "\n")
    return ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", metavar="DIR", help="run on a synthetic room; reads no media")
    parser.add_argument("--frame")
    parser.add_argument("--proxy", help="room proxy JSON (amps.god_here.room_proxy.v1)")
    parser.add_argument("--subject-mask", help="image whose non-zero pixels are performers in the source frame")
    parser.add_argument("--performer-a", nargs=3, type=float, default=[0.0, 0.0, 2.5])
    parser.add_argument("--performer-b", nargs=3, type=float, default=[0.2, 0.0, 4.2])
    parser.add_argument("--standoff", type=float, default=1.1)
    parser.add_argument("--lateral", type=float, default=0.4)
    parser.add_argument("--hfov-deg", type=float, default=100.0)
    parser.add_argument("--front-depth", type=float, default=1.0)
    parser.add_argument("--fill", choices=["none", "face_mean"], default="none")
    parser.add_argument("--output", default="outputs/reverse-angle")
    args = parser.parse_args(argv)
    try:
        if args.demo:
            args.proxy = None
            width, height = 320, 180
            proxy = reconstruct_room_proxy_geometry({"overall_depth_range_m": [1.0, 6.0]},
                                                    {"fx": 450.0, "fy": 450.0, "width": width, "height": height})
            box = ra.room_box_from_proxy(proxy, args.front_depth)
            source = ra.source_camera(width, height, args.hfov_deg)
            frame = ra.render_proxy_room(box, source)
            mask = None
            output = Path(args.demo)
        else:
            if not (args.frame and args.proxy):
                parser.error("--frame and --proxy are required unless --demo is used")
            frame = np.asarray(Image.open(args.frame).convert("RGB"), dtype=np.float32)
            height, width = frame.shape[:2]
            proxy = json.loads(Path(args.proxy).read_text())
            box = ra.room_box_from_proxy(proxy, args.front_depth)
            source = ra.source_camera(width, height, args.hfov_deg)
            mask = None
            if args.subject_mask:
                mask = np.asarray(Image.open(args.subject_mask).convert("L").resize((width, height))) > 0
            output = Path(args.output)
        plan = ra.plan_reverse_camera(source, args.performer_a, args.performer_b, args.standoff, args.lateral, box)
        reconstruction = ra.reproject_observed(frame, source, plan["_camera"], box, subject_mask=mask)
        ledger = write_outputs(reconstruction, plan, args.fill, output)
    except (ValueError, OSError) as exc:
        print(json.dumps({"schema": ra.SCHEMA, "status": "blocked", "error": str(exc)}))
        return 2
    print(json.dumps({"schema": ra.SCHEMA, "status": "written", "output": str(output),
                      "observed_fraction": ledger["observed_fraction"],
                      "optical_axis_separation_deg": plan["checks"]["optical_axis_separation_deg"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
