import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "loudness.py"
SR = 48000


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


def tone(path, dbfs, seconds=8, kind=np.int16, channels=2, peak_burst_db=None):
    t = np.arange(int(seconds * SR)) / SR
    x = 10 ** (dbfs / 20) * np.sin(2 * np.pi * 1000 * t)
    if peak_burst_db is not None:
        x[SR // 2] = 10 ** (peak_burst_db / 20)
    data = np.stack([x] * channels, axis=1) if channels > 1 else x
    scale = {np.int16: 32768.0, np.int32: 2147483648.0}.get(kind)
    wavfile.write(path, SR, np.round(data * scale).astype(kind) if scale else data.astype(np.float32))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LoudnessCliTests(unittest.TestCase):
    def test_demo_normalizes_to_ebu_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = run("demo", a), run("demo", b)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            one, two = json.loads(first.stdout), json.loads(second.stdout)
            self.assertEqual(one["problems"], [])
            self.assertAlmostEqual(one["after_lufs"], -23.0, delta=0.1)
            self.assertLessEqual(one["true_peak_dbtp"], -1.0)
            self.assertNotAlmostEqual(one["before_lufs"], -23.0, delta=1.0)    # the demo really needed gain
            self.assertEqual(one["output_sha256"], two["output_sha256"])
            self.assertEqual(one["output_sha256"], sha(Path(a) / "normalized.wav"))
            report = json.loads((Path(a) / "report.json").read_text())
            self.assertEqual(report["plan"]["ok"], True)

    def test_measure_prints_the_measurement(self):
        with tempfile.TemporaryDirectory() as d:
            tone(f"{d}/t.wav", -23.0, 10)
            result = run("measure", "--input", f"{d}/t.wav")
            self.assertEqual(result.returncode, 0, result.stderr)
            m = json.loads(result.stdout)
            self.assertAlmostEqual(m["integrated_lufs"], -23.0, delta=0.05)
            self.assertEqual((m["channels"], m["sample_rate"]), (2, SR))

    def test_check_exits_zero_when_compliant_and_two_when_not(self):
        with tempfile.TemporaryDirectory() as d:
            tone(f"{d}/ok.wav", -23.0, 10)
            tone(f"{d}/loud.wav", -0.5, 10)
            good = run("check", "--input", f"{d}/ok.wav", "--spec", "ebu_r128")
            bad = run("check", "--input", f"{d}/loud.wav", "--spec", "ebu_r128")
            self.assertEqual(good.returncode, 0, good.stdout)
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(len(json.loads(bad.stdout)["problems"]), 2)       # level and true peak both over

    def test_spec_file_overrides_the_preset(self):
        with tempfile.TemporaryDirectory() as d:
            tone(f"{d}/t.wav", -16.0, 10)
            Path(f"{d}/spec.json").write_text(json.dumps({"integrated_lufs": {"target": -16, "tolerance": 0.3}, "true_peak_dbtp_max": -1}))
            self.assertEqual(run("check", "--input", f"{d}/t.wav", "--spec", f"{d}/spec.json").returncode, 0)
            self.assertEqual(run("check", "--input", f"{d}/t.wav", "--spec", "ebu_r128").returncode, 2)

    def test_normalize_writes_a_file_that_measures_on_target_in_every_format(self):
        with tempfile.TemporaryDirectory() as d:
            for kind in (np.int16, np.int32, np.float32):
                src, out = f"{d}/{kind.__name__}.wav", f"{d}/{kind.__name__}-out.wav"
                tone(src, -34.0, 10, kind)
                result = run("normalize", "--input", src, "--output", out, "--spec", "ebu_r128", "--report", f"{d}/r.json")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                sr, data = wavfile.read(out)
                self.assertEqual(data.dtype, np.dtype(kind))
                m = json.loads(run("measure", "--input", out).stdout)
                self.assertAlmostEqual(m["integrated_lufs"], -23.0, delta=0.05, msg=kind)
                report = json.loads(Path(f"{d}/r.json").read_text())
                self.assertEqual(report["output_sha256"], sha(out))
                self.assertEqual(report["input_sha256"], sha(src))

    def test_ceiling_refuses_by_default_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            tone(f"{d}/p.wav", -34.0, 10, peak_burst_db=-8.0)       # quiet programme with one high peak
            out = Path(d) / "out.wav"
            result = run("normalize", "--input", f"{d}/p.wav", "--output", str(out), "--spec", "ebu_r128", "--report", f"{d}/r.json")
            self.assertEqual(result.returncode, 2)
            self.assertFalse(out.exists())
            self.assertIn("ceiling", json.loads(result.stdout)["problems"][0])
            self.assertEqual(json.loads(Path(f"{d}/r.json").read_text())["plan"]["ok"], False)

    def test_cap_stops_at_the_ceiling_and_reports_the_shortfall(self):
        with tempfile.TemporaryDirectory() as d:
            tone(f"{d}/p.wav", -34.0, 10, peak_burst_db=-8.0)
            out = f"{d}/out.wav"
            result = run("normalize", "--input", f"{d}/p.wav", "--output", out, "--target-lufs", "-23", "--ceiling-dbtp", "-1", "--on-ceiling", "cap")
            self.assertEqual(result.returncode, 0, result.stdout)
            summary = json.loads(result.stdout)
            self.assertTrue(summary["capped"])
            self.assertGreater(summary["shortfall_lu"], 1.0)
            self.assertAlmostEqual(json.loads(run("measure", "--input", out).stdout)["true_peak_dbtp"], -1.0, delta=0.05)
            # the same capped file against the spec still fails on level: capping never hides a miss
            self.assertEqual(run("check", "--input", out, "--spec", "ebu_r128").returncode, 2)

    def test_integer_clipping_is_refused_when_no_ceiling_is_given(self):
        with tempfile.TemporaryDirectory() as d:
            tone(f"{d}/p.wav", -34.0, 10, peak_burst_db=-8.0)
            out = Path(d) / "out.wav"
            result = run("normalize", "--input", f"{d}/p.wav", "--output", str(out), "--target-lufs", "-14")
            self.assertEqual(result.returncode, 2)
            self.assertFalse(out.exists())
            self.assertIn("clip", json.loads(result.stdout)["problems"][0])

    def test_bad_inputs_exit_two_with_a_message(self):
        with tempfile.TemporaryDirectory() as d:
            Path(f"{d}/junk.wav").write_bytes(b"not a wav")
            tone(f"{d}/short.wav", -23.0, 0.2)
            tone(f"{d}/t.wav", -23.0, 5)
            for args in (("measure", "--input", f"{d}/junk.wav"), ("measure", "--input", f"{d}/short.wav"),
                         ("measure", "--input", f"{d}/missing.wav"), ("check", "--input", f"{d}/t.wav"),
                         ("check", "--input", f"{d}/t.wav", "--spec", "no-such-preset"),
                         ("normalize", "--input", f"{d}/t.wav", "--output", f"{d}/o.wav"),
                         ("measure", "--input", f"{d}/t.wav", "--layout", "5.1")):
                result = run(*args)
                self.assertEqual(result.returncode, 2, args)
                self.assertIn("error", json.loads(result.stdout), args)

    def test_target_without_a_spec_is_still_checked_against_the_measured_result(self):
        with tempfile.TemporaryDirectory() as d:
            t = np.arange(20 * SR) / SR
            x = np.where(t < 10, 10 ** (-68 / 20), 10 ** (-72 / 20)) * np.sin(2 * np.pi * 1000 * t)
            wavfile.write(f"{d}/g.wav", SR, np.round(np.stack([x, x], 1) * 2147483648.0).astype(np.int32))
            result = run("normalize", "--input", f"{d}/g.wav", "--output", f"{d}/o.wav", "--target-lufs", "-23", "--ceiling-dbtp", "0")
            self.assertEqual(result.returncode, 0, result.stdout)
            summary = json.loads(result.stdout)
            self.assertAlmostEqual(summary["after_lufs"], -23.0, delta=0.05)
            self.assertAlmostEqual(json.loads(run("measure", "--input", f"{d}/o.wav").stdout)["integrated_lufs"], -23.0, delta=0.05)

    def test_written_file_honours_the_requested_ceiling_after_integer_rounding(self):
        with tempfile.TemporaryDirectory() as d:
            tone(f"{d}/q.wav", -64.0, 10)              # about 20 LSB: a gain to -50 dBTP lands between two integers and rounds up (-49.96 without the back-off)
            out = f"{d}/o.wav"
            result = run("normalize", "--input", f"{d}/q.wav", "--output", out, "--target-lufs", "-23", "--ceiling-dbtp", "-50",
                         "--on-ceiling", "cap", "--report", f"{d}/r.json")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertLessEqual(json.loads(run("measure", "--input", out).stdout)["true_peak_dbtp"], -50.0)
            report = json.loads(Path(f"{d}/r.json").read_text())
            self.assertLessEqual(report["after"]["true_peak_dbtp"], -50.0)
            self.assertEqual(report["problems"], [])
            self.assertEqual(report["output_sha256"], sha(out))

    def test_silent_measure_prints_strict_json(self):
        with tempfile.TemporaryDirectory() as d:
            wavfile.write(f"{d}/s.wav", SR, np.zeros((SR * 2, 2), dtype=np.int16))
            result = run("measure", "--input", f"{d}/s.wav")
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(result.stdout, parse_constant=lambda name: self.fail(f"non-JSON constant {name}"))
            self.assertIsNone(parsed["integrated_lufs"])
            self.assertIsNone(parsed["momentary_max_lufs"])

    def test_non_finite_numbers_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tone(f"{d}/t.wav", -30.0, 5)
            Path(f"{d}/nan.json").write_text('{"integrated_lufs": {"target": NaN, "tolerance": 1}, "true_peak_dbtp_max": NaN}')
            for args in (("check", "--input", f"{d}/t.wav", "--spec", f"{d}/nan.json"),
                         ("normalize", "--input", f"{d}/t.wav", "--output", f"{d}/o.wav", "--target-lufs", "nan"),
                         ("normalize", "--input", f"{d}/t.wav", "--output", f"{d}/o.wav", "--target-lufs", "-23", "--ceiling-dbtp", "nan")):
                result = run(*args)
                self.assertEqual(result.returncode, 2, args)
                self.assertIn("error", json.loads(result.stdout), args)
            self.assertFalse(Path(f"{d}/o.wav").exists())

    def test_silent_file_is_refused_not_normalized(self):
        with tempfile.TemporaryDirectory() as d:
            wavfile.write(f"{d}/s.wav", SR, np.zeros((SR * 2, 2), dtype=np.int16))
            out = Path(d) / "o.wav"
            result = run("normalize", "--input", f"{d}/s.wav", "--output", str(out), "--spec", "ebu_r128")
            self.assertEqual(result.returncode, 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
