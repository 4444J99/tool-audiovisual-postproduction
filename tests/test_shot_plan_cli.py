import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "shot_plan.py"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


class ShotPlanCliTests(unittest.TestCase):
    def test_demo_writes_cards_sheet_ledger_and_edl_deterministically(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = run("--demo", a), run("--demo", b)
            self.assertEqual(first.returncode, 0, first.stderr)
            one, two = json.loads(first.stdout), json.loads(second.stdout)
            self.assertEqual(one["card_sha256"], two["card_sha256"])
            self.assertEqual(one["contact_sheet_sha256"], two["contact_sheet_sha256"])
            self.assertEqual(one["shots"], 3)
            for name in ("S01", "S02", "S03"):
                with Image.open(Path(a) / "cards" / f"{name}.png") as card:
                    self.assertEqual(card.size, (640, 360))
            with Image.open(Path(a) / "contact-sheet.png") as sheet:
                self.assertEqual(sheet.size, (1920, 360))
            ledger = json.loads((Path(a) / "shot-ledger.json").read_text())
            self.assertEqual(ledger["total_frames"], 540)
            self.assertEqual(ledger["plan_sha256"], one["plan_sha256"])
            self.assertIn("003  S03", (Path(a) / "edit.edl").read_text())

    def test_invalid_plan_prints_all_errors_and_creates_nothing(self):
        from amp.studies import shot_plan as sp
        plan = sp.synthetic_plan()
        plan["shots"][1]["id"] = "S01"
        plan["shots"][0]["render"] = {}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(plan))
            out = Path(tmp) / "out"
            result = run("--plan", str(path), "--output", str(out))
            self.assertEqual(result.returncode, 2)
            self.assertGreaterEqual(len(json.loads(result.stdout)["errors"]), 2)
            self.assertFalse(out.exists())

    def test_validate_only_and_argument_errors(self):
        from amp.studies import shot_plan as sp
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(sp.synthetic_plan()))
            result = run("--plan", str(path), "--validate-only")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])
            self.assertEqual(run("--plan", str(Path(tmp) / "missing.json")).returncode, 2)
        self.assertEqual(run().returncode, 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    def test_video_has_exact_frame_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run("--demo", tmp, "--video")
            self.assertEqual(result.returncode, 0, result.stderr)
            probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                    "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(Path(tmp) / "animatic.mp4")],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(int(probe.stdout.strip()), 540)


if __name__ == "__main__":
    unittest.main()
