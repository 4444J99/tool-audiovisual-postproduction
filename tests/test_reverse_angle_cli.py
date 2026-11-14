import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "reverse_angle.py"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


class ReverseAngleCliTests(unittest.TestCase):
    def test_demo_writes_images_and_ledger_without_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run("--demo", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertGreater(summary["observed_fraction"], 0.05)
            ledger = json.loads((Path(tmp) / "ledger.json").read_text())
            self.assertEqual(ledger["faces"]["front"]["observed"], 0)
            for name in ("reverse-angle.png", "status.png"):
                with Image.open(Path(tmp) / name) as image:
                    self.assertEqual(image.size, (320, 180))

    def test_frame_and_mask_path_and_blocked_output(self):
        from amp.studies import reverse_angle as ra
        from amp.studies.depth_world import reconstruct_room_proxy_geometry
        proxy = reconstruct_room_proxy_geometry({"overall_depth_range_m": [1.0, 6.0]},
                                                {"fx": 450.0, "fy": 450.0, "width": 320, "height": 180})
        box = ra.room_box_from_proxy(proxy)
        frame = ra.render_proxy_room(box, ra.source_camera(320, 180, 100.0))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            Image.fromarray(frame.astype(np.uint8)).save(tmp / "frame.png")
            mask = np.zeros((180, 320), dtype=np.uint8)
            mask[40:140, 120:200] = 255
            Image.fromarray(mask).save(tmp / "mask.png")
            (tmp / "proxy.json").write_text(json.dumps(proxy))
            ok = run("--frame", str(tmp / "frame.png"), "--proxy", str(tmp / "proxy.json"),
                     "--subject-mask", str(tmp / "mask.png"), "--fill", "face_mean", "--output", str(tmp / "out"))
            self.assertEqual(ok.returncode, 0, ok.stderr)
            ledger = json.loads((tmp / "out" / "ledger.json").read_text())
            self.assertGreater(ledger["unobserved"]["performer_occluded"], 0)
            self.assertIn("placeholder", ledger["fill"]["status"])
            bad = run("--frame", str(tmp / "frame.png"), "--proxy", str(tmp / "proxy.json"),
                      "--performer-a", "0", "0", "2", "--performer-b", "0", "0", "4", "--output", str(tmp / "bad"))
            self.assertEqual(bad.returncode, 2)
            self.assertIn("axis of action", json.loads(bad.stdout)["error"])
            self.assertFalse((tmp / "bad").exists())


if __name__ == "__main__":
    unittest.main()
