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
        self.assertTrue({"mp4.audio_matches_delivered", "mp4.audio_padding_silent", "mp4.chroma_flat"} <= ids, ids)

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


class AlignedSnrTests(unittest.TestCase):
    """CodeRabbit on this PR: a decoded track that is a little longer than the dialogue but shorter than dialogue plus lag must fail, not raise."""

    def test_a_shifted_track_cut_short_gives_a_number_not_an_exception(self):
        reference = np.random.default_rng(3).standard_normal(8000)
        heard = np.concatenate([np.zeros(100), reference[:7950]])  # best lag 100; heard[100:100 + n] would be 7950 long
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


if __name__ == "__main__":
    unittest.main()
