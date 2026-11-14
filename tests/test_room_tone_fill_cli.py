import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "room_tone_fill.py"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


class RoomToneFillCliTests(unittest.TestCase):
    def test_demo_fills_both_holes_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = run("demo", a), run("demo", b)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            one, two = json.loads(first.stdout), json.loads(second.stdout)
            self.assertEqual(one["holes_filled"], 2)
            self.assertEqual(one["problems"], [])
            self.assertEqual(one["output_sha256"], two["output_sha256"])
            sr, before = wavfile.read(Path(a) / "dialogue-with-holes.wav")
            sr2, after = wavfile.read(Path(a) / "filled.wav")
            self.assertEqual((sr, before.dtype), (sr2, after.dtype))
            self.assertTrue((before[30400:33600] == 0).all())        # the demo's first hole really was silent
            self.assertGreater(np.abs(after[30400:33600].astype(float)).mean(), 50)
            report = json.loads((Path(a) / "report.json").read_text())
            self.assertTrue(report["untouched_identical"])
            self.assertEqual(report["remaining_digital_silence"], [])

    def test_fill_writes_wav_and_report_for_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            run("demo", d)
            out, rep = Path(d) / "out.wav", Path(d) / "rep.json"
            result = run("fill", "--input", str(Path(d) / "dialogue-with-holes.wav"), "--output", str(out), "--report", str(rep), "--seed", "4")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(out.exists())
            self.assertEqual(json.loads(rep.read_text())["seed"], 4)

    def test_scan_lists_holes_and_tone(self):
        with tempfile.TemporaryDirectory() as d:
            run("demo", d)
            result = run("scan", "--input", str(Path(d) / "dialogue-with-holes.wav"))
            data = json.loads(result.stdout)
            self.assertEqual(data["holes"], [[1.9, 2.1], [3.5, 4.4]])
            self.assertGreaterEqual(len(data["tone_regions"]), 3)

    def test_float_and_int32_wavs_round_trip_their_format(self):
        with tempfile.TemporaryDirectory() as d:
            run("demo", d)
            sr, pcm = wavfile.read(Path(d) / "dialogue-with-holes.wav")
            for name, data in (("f32.wav", (pcm / 32768.0).astype(np.float32)), ("i32.wav", pcm.astype(np.int32) * 65536)):
                src, dst = Path(d) / name, Path(d) / ("out-" + name)
                wavfile.write(src, sr, data)
                result = run("fill", "--input", str(src), "--output", str(dst))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(wavfile.read(dst)[1].dtype, data.dtype)

    def test_recording_with_no_pause_is_refused_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            rng = np.random.default_rng(0)
            gain = np.where((np.arange(16000 * 3) // 1600) % 2 == 0, 6000.0, 60.0)   # 0.1 s swings: no steady stretch
            loud = (rng.standard_normal(16000 * 3) * gain).astype(np.int16)
            loud[8000:12000] = 0
            src, dst = Path(d) / "loud.wav", Path(d) / "out.wav"
            wavfile.write(src, 16000, loud)
            result = run("fill", "--input", str(src), "--output", str(dst))
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stdout))
            self.assertFalse(dst.exists())

    def test_unreadable_and_unsupported_audio_are_refused(self):
        self.assertEqual(run("scan", "--input", "/nonexistent.wav").returncode, 2)
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "u8.wav"
            wavfile.write(src, 8000, np.full(800, 128, dtype=np.uint8))
            result = run("scan", "--input", str(src))
            self.assertEqual(result.returncode, 2)
            self.assertIn("unsupported", result.stdout)

    def test_speech_adjacent_fill_is_not_rejected_by_the_cli(self):
        from amp.studies import room_tone_fill as rt
        sys.path.insert(0, str(ROOT / "tools"))
        import room_tone_fill as cli
        audio, meta = rt.synthetic_dialogue()
        start = int(1.2 * meta["sample_rate"])
        audio[start:start + int(0.3 * meta["sample_rate"])] = 0.0
        filled, report = cli.run_fill(meta["sample_rate"], audio, 1)
        self.assertEqual(cli.verdict(report), [])

    def test_verdict_names_every_failed_check(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import room_tone_fill as cli
        report = {"remaining_digital_silence": [{"start_s": 1.0, "end_s": 1.2}], "untouched_identical": False,
                  "checks": [{"start_s": 1.0, "level_error_db": 5.0, "edge_level_deviation_db": 9.0, "band_shape_deviation_db": 8.0}]}
        self.assertEqual(len(cli.verdict(report)), 5)


if __name__ == "__main__":
    unittest.main()
