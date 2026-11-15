"""The sealed chain must not depend on floating-point noise that the written files do not carry.

Linux CI runs of one commit gave different chain and QC hashes while frames, dialogue and captions stayed equal.  The
room-tone fill report sealed a hash of the float64 audio *before* 16-bit quantisation, so a last-bit difference in the
FFT/libm results of another CPU or library build changed the report, the stage and the chain hash while the WAV stayed
byte-identical.  These tests move every float the fill synthesises by a few ulps (what another host does) and demand that
no written file, and so no hash, changes; and they run the chain from other working directories and paths."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from amp.studies import pipeline_chain as pc
from amp.studies import room_tone_fill as rt

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "av_pipeline.py"


def tree(out):
    return {str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(out).rglob("*")) if p.is_file()}


def drifted_fill(scale):
    """``rt.fill_holes`` as another CPU might compute it: the synthesised samples differ in their last bits."""
    real = rt.fill_holes

    def fill(audio, sr, **kwargs):
        out, report = real(audio, sr, **kwargs)
        source = np.asarray(audio)
        rng = np.random.default_rng(7)
        changed = out != source
        out = np.where(changed, out * (1.0 + scale * rng.standard_normal(out.shape)), out)
        report["output_sha256"] = rt.audio_sha256(out)
        return out, report
    return fill


class ChainDeterminismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.reference = cls.base / "reference"
        pc.run_chain(cls.reference)
        cls.files = tree(cls.reference)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_last_bit_float_drift_in_the_fill_changes_no_written_file(self):
        for index, scale in enumerate((1e-16, 1e-15, 1e-13)):
            out = self.base / f"drift-{index}"
            with mock.patch.object(rt, "fill_holes", drifted_fill(scale)):
                manifest = pc.run_chain(out)
            files = tree(out)
            self.assertEqual({k: v for k, v in files.items() if k.endswith(".wav")}, {k: v for k, v in self.files.items() if k.endswith(".wav")}, "drift must not reach the quantised audio for this test to mean anything")
            self.assertEqual(files, self.files, f"scale {scale}")
            self.assertEqual(manifest["chain_sha256"], json.loads((self.reference / "pipeline-manifest.json").read_text())["chain_sha256"])

    def test_the_fill_report_describes_the_quantised_wav_it_sealed(self):
        report = json.loads((self.reference / "audio" / "fill-report.json").read_text())
        _, stored = pc.read_pcm16(self.reference / "audio" / "dialogue-filled.wav")
        self.assertEqual(report["output_sha256"], rt.audio_sha256(stored))

    def test_every_hash_is_the_same_from_another_cwd_and_a_relative_path(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        env = dict(os.environ, PYTHONHASHSEED="12345", TZ="Pacific/Auckland", LC_ALL="C", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        result = subprocess.run([sys.executable, str(TOOL), "run", "--output", "nested/../chain-out"], capture_output=True, text=True, cwd=elsewhere, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(tree(elsewhere / "chain-out"), self.files)

    def test_no_written_text_names_a_path_host_or_time(self):
        for name in self.files:
            path = self.reference / name
            if path.suffix in {".json", ".md", ".srt", ".vtt"}:
                text = path.read_text()
                for needle in (str(self.base), str(ROOT), "/home/", "/Users/", "/tmp/"):
                    self.assertNotIn(needle, text, f"{name} names {needle}")


if __name__ == "__main__":
    unittest.main()
