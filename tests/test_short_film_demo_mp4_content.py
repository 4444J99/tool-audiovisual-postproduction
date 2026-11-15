"""The mp4 is judged on what it carries, not only on its loudness and its luma plane.

Found by a cold review of the short-film demo: after re-sealing demo-hashes.json, an mp4 whose sound was noise at the
delivered loudness, whose padding carried a tone, or whose chroma planes were tinted still passed ``verify``.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "short_film_demo.py"
sys.path.insert(0, str(ROOT / "tools"))
import short_film_demo as demo  # noqa: E402


def cli(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


def encoders_available():
    try:
        demo.ffmpeg_tools()
    except demo.DemoError:
        return False
    return True


@unittest.skipUnless(encoders_available(), "ffmpeg with libx264 and aac is required to build the mp4")
class Mp4ContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.first = Path(cls.tmp.name) / "first"
        cls.completed = cli("run", "--output", str(cls.first))
        cls.ffmpeg, cls.ffprobe = demo.ffmpeg_tools()
        cls.ledger = json.loads((cls.first / "plan" / "final-ledger.json").read_text())
        cls.burnin = json.loads((cls.first / "captions" / "burnin-report.json").read_text())
        cls.rate, cls.delivered = demo.pc.read_pcm16(cls.first / "audio" / "dialogue-delivered.wav")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self):
        target = Path(self.tmp.name) / f"copy-{self.id().rsplit('.', 1)[-1]}"
        shutil.copytree(self.first, target)
        return target

    def reseal(self, out):
        sealed = json.loads((out / demo.HASHES).read_text())
        sealed["mp4_sha256"] = demo.sha256_file(out / demo.MP4)
        sealed["files"][demo.MP4] = sealed["mp4_sha256"]
        (out / demo.HASHES).write_text(json.dumps(sealed))

    def swap_audio(self, out, samples):
        """Replace the mp4's sound with ``samples`` (float, 16 kHz mono), keep the video stream byte for byte, re-seal."""
        wav = out / "swap.wav"
        wav.write_bytes(demo.pc.pcm16_bytes(samples, self.rate))
        target = out / "swapped.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(out / demo.MP4), "-i", str(wav),
                        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af", f"apad=whole_dur={self.ledger['total_seconds']}",
                        "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1", "-t", str(self.ledger["total_seconds"]),
                        "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact", "-movflags", "+faststart", str(target)], check=True)
        (out / demo.MP4).write_bytes(target.read_bytes())
        target.unlink()
        wav.unlink()
        self.reseal(out)

    def verify_names(self, out, check_id):
        result = cli("verify", "--output", str(out))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(check_id, result.stdout)

    def test_a_fresh_run_passes_and_reports_the_new_checks(self):
        self.assertEqual(self.completed.returncode, 0, self.completed.stdout + self.completed.stderr)
        self.assertEqual(cli("verify", "--output", str(self.first)).returncode, 0)
        report = json.loads((self.first / demo.DEMO_QC).read_text())
        ids = {check["id"] for check in report["checks"]}
        self.assertTrue({"mp4.audio_matches_delivered", "mp4.audio_blocks_match_delivered", "mp4.audio_padding_silent", "mp4.chroma_flat", "mp4.video_pixel_format"} <= ids, ids)

    def test_noise_at_the_delivered_loudness_in_place_of_the_dialogue_is_named(self):
        out = self.copy()
        noise = 0.05 * np.random.default_rng(7).standard_normal(len(self.delivered))
        reference = demo.ld.measure(self.delivered, self.rate)["integrated_lufs"]
        noise *= 10 ** ((reference - demo.ld.measure(noise, self.rate)["integrated_lufs"]) / 20)
        self.assertLess(abs(demo.ld.measure(noise, self.rate)["integrated_lufs"] - reference), 0.3)
        self.swap_audio(out, noise)
        self.verify_names(out, "mp4.audio_matches_delivered")

    def test_a_tone_in_the_padding_after_the_dialogue_is_named(self):
        out = self.copy()
        total = int(round(self.ledger["total_seconds"] * self.rate))
        samples = np.zeros(total)
        samples[:len(self.delivered)] = self.delivered
        samples[len(self.delivered) + 4000:] += 0.2 * np.sin(2 * np.pi * 1000 * np.arange(total - len(self.delivered) - 4000) / self.rate)
        self.swap_audio(out, samples)
        self.verify_names(out, "mp4.audio_padding_silent")

    def test_a_tinted_chroma_plane_is_named(self):
        out = self.copy()
        original = demo.yuv420_stream

        def tinted(frames, width, height):
            size = width * height
            half = (width // 2) * (height // 2)
            chroma = bytes([200]) * half + bytes([60]) * half
            return b"".join(frames[offset:offset + size] + chroma for offset in range(0, len(frames), size))

        demo.yuv420_stream = tinted
        try:
            demo.mux(out, self.ledger, self.burnin, self.ffmpeg)
        finally:
            demo.yuv420_stream = original
        self.reseal(out)
        self.verify_names(out, "mp4.chroma_flat")

    # ---- Findings of the independent review of this PR's head 405dc66 ----

    def swap_video(self, out, args, extra_inputs=()):
        """Re-encode or re-flag the mp4's video through ffmpeg, keep its audio, re-seal."""
        target = out / "swapped.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args, str(target)], check=True)
        (out / demo.MP4).write_bytes(target.read_bytes())
        target.unlink()
        self.reseal(out)

    def test_a_chroma_checkerboard_in_a_4_4_4_stream_is_named(self):
        out = self.copy()
        width, height = self.burnin["params"]["geometry"]["width"], self.burnin["params"]["geometry"]["height"]
        luma = (out / "captions" / "frames.y8").read_bytes()
        checker = ((np.indices((height, width)).sum(axis=0) % 2) * 2 - 1) * 30
        u = np.clip(128 + checker, 0, 255).astype(np.uint8).tobytes()
        v = np.clip(128 - checker, 0, 255).astype(np.uint8).tobytes()
        size = width * height
        stream = b"".join(luma[o:o + size] + u + v for o in range(0, len(luma), size))
        target = out / "swapped.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv444p",
                        "-s", f"{width}x{height}", "-r", str(self.ledger["fps"]), "-i", "pipe:0", "-i", str(out / demo.MP4),
                        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-pix_fmt", "yuv444p",
                        "-c:a", "copy", "-t", str(self.ledger["total_seconds"]), str(target)], input=stream, check=True, capture_output=True)
        (out / demo.MP4).write_bytes(target.read_bytes())
        target.unlink()
        self.reseal(out)
        self.verify_names(out, "mp4.video_pixel_format")

    def test_a_full_range_flag_or_a_stretched_pixel_aspect_ratio_is_named(self):
        for name, bsf in (("fullrange", "h264_metadata=video_full_range_flag=1"), ("sar", "h264_metadata=sample_aspect_ratio=2/1")):
            out = Path(self.tmp.name) / f"copy-{name}"
            shutil.copytree(self.first, out)
            self.swap_video(out, ["-i", str(out / demo.MP4), "-c", "copy", "-bsf:v", bsf])
            self.verify_names(out, "mp4.video_pixel_format")

    def test_a_loud_beep_just_after_the_dialogue_is_named(self):
        out = self.copy()
        total = int(round(self.ledger["total_seconds"] * self.rate))
        n = len(self.delivered)
        samples = np.zeros(total)
        samples[:n] = self.delivered
        samples[n + 1100:n + 4090] += 0.9 * np.sin(2 * np.pi * 1000 * np.arange(2990) / self.rate)
        self.swap_audio(out, samples)
        self.verify_names(out, "mp4.audio_padding_silent")

    def test_a_quiet_tone_across_the_padding_is_named(self):
        out = self.copy()
        total = int(round(self.ledger["total_seconds"] * self.rate))
        n = len(self.delivered)
        samples = np.zeros(total)
        samples[:n] = self.delivered
        samples[n:] += 0.009 * np.sin(2 * np.pi * 1000 * np.arange(total - n) / self.rate)
        self.swap_audio(out, samples)
        self.verify_names(out, "mp4.audio_padding_silent")

    def test_dialogue_delayed_by_125_ms_is_named(self):
        out = self.copy()
        total = int(round(self.ledger["total_seconds"] * self.rate))
        samples = np.zeros(total)
        samples[2000:2000 + len(self.delivered)] = self.delivered
        self.swap_audio(out, samples)
        self.verify_names(out, "mp4.audio_matches_delivered")

    # ---- Findings of the second independent review (head 5448846) ----

    def dialogue_in_padding(self, edit):
        total = int(round(self.ledger["total_seconds"] * self.rate))
        samples = np.zeros(total)
        samples[:len(self.delivered)] = self.delivered
        edit(samples, len(self.delivered))
        return samples

    def test_muting_a_quarter_of_the_dialogue_is_named(self):
        out = self.copy()
        self.swap_audio(out, self.dialogue_in_padding(lambda x, n: x.__setitem__(slice(46243, 80003), 0.0)))
        self.verify_names(out, "mp4.audio_blocks_match_delivered")

    def test_a_loud_150_ms_tone_laid_over_the_dialogue_is_named(self):
        out = self.copy()
        tone = 0.1 * np.sin(2 * np.pi * 1000 * np.arange(2400) / self.rate)
        self.swap_audio(out, self.dialogue_in_padding(lambda x, n: x.__setitem__(slice(40000, 42400), x[40000:42400] + tone)))
        self.verify_names(out, "mp4.audio_blocks_match_delivered")

    def test_a_full_scale_click_inside_the_guard_after_the_dialogue_is_named(self):
        out = self.copy()
        self.swap_audio(out, self.dialogue_in_padding(lambda x, n: x.__setitem__(slice(n + 500, n + 504), [0.99, -0.99, 0.99, -0.99])))
        self.verify_names(out, "mp4.audio_padding_silent")

    def test_colour_matrix_transfer_and_primaries_flags_are_named(self):
        for name, bsf in (("matrix", "h264_metadata=matrix_coefficients=0"), ("transfer", "h264_metadata=transfer_characteristics=16"),
                          ("primaries", "h264_metadata=colour_primaries=11")):
            out = Path(self.tmp.name) / f"copy-{name}"
            shutil.copytree(self.first, out)
            self.swap_video(out, ["-i", str(out / demo.MP4), "-c", "copy", "-bsf:v", bsf])
            self.verify_names(out, "mp4.video_pixel_format")

    def test_the_thresholds_sit_where_the_doc_says(self):
        """Pins both sides of each bound with a copy that is just inside and just outside it."""
        for name, amplitude, passes in (("pad-ok", 0.0005, True), ("pad-bad", 0.002, False)):
            out = Path(self.tmp.name) / f"copy-{name}"
            shutil.copytree(self.first, out)
            n = len(self.delivered)
            self.swap_audio(out, self.dialogue_in_padding(lambda x, m, a=amplitude: x.__setitem__(slice(m, None), x[m:] + a * np.sin(2 * np.pi * 1000 * np.arange(len(x) - m) / self.rate))))
            self.assertEqual(cli("verify", "--output", str(out)).returncode, 0 if passes else 2, name)
        for name, amplitude, passes in (("guard-ok", 0.001, True), ("guard-bad", 0.01, False)):  # inside the 1024 samples after the dialogue: bound 0.003
            out = Path(self.tmp.name) / f"copy-{name}"
            shutil.copytree(self.first, out)
            self.swap_audio(out, self.dialogue_in_padding(lambda x, m, a=amplitude: x.__setitem__(slice(m + 100, m + 900), x[m + 100:m + 900] + a * np.sin(2 * np.pi * 1000 * np.arange(800) / self.rate))))
            self.assertEqual(cli("verify", "--output", str(out)).returncode, 0 if passes else 2, name)
        for name, delay, passes in (("lag-bad", 600, False),):  # a smaller delay also fails, through the guard: the dialogue tail is not silent
            out = Path(self.tmp.name) / f"copy-{name}"
            shutil.copytree(self.first, out)
            total = int(round(self.ledger["total_seconds"] * self.rate))
            samples = np.zeros(total)
            samples[delay:delay + len(self.delivered)] = self.delivered
            self.swap_audio(out, samples)
            self.assertEqual(cli("verify", "--output", str(out)).returncode, 0 if passes else 2, name)
        for name, level, passes in (("chroma-ok", 129, True), ("chroma-bad", 130, False)):
            out = Path(self.tmp.name) / f"copy-{name}"
            shutil.copytree(self.first, out)
            original = demo.yuv420_stream

            def tinted(frames, width, height, level=level):
                size, half = width * height, (width // 2) * (height // 2)
                return b"".join(frames[o:o + size] + bytes([level]) * (2 * half) for o in range(0, len(frames), size))

            demo.yuv420_stream = tinted
            try:
                demo.mux(out, self.ledger, self.burnin, self.ffmpeg)
            finally:
                demo.yuv420_stream = original
            self.reseal(out)
            checks = demo.judge_mp4(out, self.ledger, self.burnin, (self.ffmpeg, shutil.which("ffprobe")))  # demo-qc.json must record this mp4's judgement too
            (out / demo.DEMO_QC).write_text(demo.pc.dumps({"schema": demo.SCHEMA, "ok": all(c["ok"] for c in checks), "checks": checks}))
            self.assertEqual(cli("verify", "--output", str(out)).returncode, 0 if passes else 2, name)

    def test_audio_too_short_to_measure_fails_a_check_instead_of_raising(self):
        out = self.copy()
        target = out / "swapped.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(out / demo.MP4), "-map", "0:v:0", "-map", "0:a:0",
                        "-c:v", "copy", "-af", "atrim=0:0.01", "-c:a", "aac", "-b:a", "64k", str(target)], check=True)
        (out / demo.MP4).write_bytes(target.read_bytes())
        target.unlink()
        self.reseal(out)
        result = cli("verify", "--output", str(out))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class AlignedSnrTests(unittest.TestCase):
    """CodeRabbit on this PR: a decoded track that is a little longer than the dialogue but shorter than dialogue plus lag must fail, not raise."""

    def test_a_shifted_track_cut_short_gives_a_number_not_an_exception(self):
        reference = np.random.default_rng(3).standard_normal(8000)
        heard = np.concatenate([np.zeros(100), reference[:7950]])  # best lag 100; heard[100:100 + n] would be 7950 long
        snr = demo.aligned_snr_db(heard, reference)
        self.assertTrue(np.isfinite(snr))  # 50 of 8000 samples missing: still a high, finite SNR
        heard = np.concatenate([np.zeros(2000), reference[:6000]])  # best lag 2000, a quarter of the dialogue never arrives
        snr = demo.aligned_snr_db(heard, reference)
        self.assertTrue(np.isfinite(snr))
        self.assertLess(snr, demo.AUDIO_MIN_SNR_DB)

    def test_an_exact_copy_and_a_delayed_copy_align(self):
        reference = np.random.default_rng(4).standard_normal(8000)
        self.assertEqual(demo.aligned_snr_db(np.concatenate([reference, np.zeros(10)]), reference), float("inf"))
        self.assertEqual(demo.aligned_snr_db(np.concatenate([np.zeros(300), reference, np.zeros(10)]), reference), float("inf"))

    def test_noise_and_too_short_audio_fail(self):
        reference = np.random.default_rng(5).standard_normal(8000)
        self.assertLess(demo.aligned_snr_db(np.random.default_rng(6).standard_normal(9000), reference), demo.AUDIO_MIN_SNR_DB)
        self.assertEqual(demo.aligned_snr_db(reference[:100], reference), float("-inf"))


    def test_a_copy_at_lag_zero_is_not_confused_by_a_loud_tail_that_wraps_around_the_fft(self):
        lag = demo.AUDIO_MAX_LAG
        n = (1 << 14) - lag - 1  # n + lag just under a power of two: the FFT size is 2**14, less than n + 2 * lag
        reference = np.random.default_rng(8).standard_normal(n)
        heard = np.concatenate([reference, np.zeros(1), 10 * reference[:lag]])
        self.assertEqual(demo.aligned_snr_db(heard, reference), float("inf"))

    def test_short_references_and_long_tails_never_raise(self):
        rng = np.random.default_rng(0)
        for _ in range(3000):
            n = int(rng.integers(1, 60))
            reference = rng.standard_normal(n)
            heard = rng.standard_normal(int(rng.integers(n, n + 3 * demo.AUDIO_MAX_LAG))) * float(rng.choice([0.0, 1.0, 10.0]))
            self.assertIsInstance(demo.aligned_snr_db(heard, reference), float)


    def test_an_anti_correlated_short_reference_does_not_raise(self):
        reference = np.random.default_rng(44).standard_normal(4)
        self.assertIsInstance(demo.aligned_snr_db(np.concatenate([-reference, np.zeros(600)]), reference), float)

    def test_the_snr_threshold_sits_between_12_and_18_db(self):
        reference = np.random.default_rng(9).standard_normal(16000)
        noise = np.random.default_rng(10).standard_normal(16000)
        noise *= np.sqrt(np.sum(reference ** 2) / np.sum(noise ** 2))
        self.assertLess(demo.aligned_snr_db(reference + noise * 10 ** (-12 / 20), reference), demo.AUDIO_MIN_SNR_DB)
        self.assertGreater(demo.aligned_snr_db(reference + noise * 10 ** (-18 / 20), reference), demo.AUDIO_MIN_SNR_DB)


if __name__ == "__main__":
    unittest.main()
