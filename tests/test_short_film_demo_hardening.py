"""Failure modes of the demo's CLI (issue 192: exit 2 and JSON, never a traceback) and the fields of the sealed files
that verify must compare (issue 193: demo-qc.json, schema, stages, encoder, deterministic)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
class DemoHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.first = cls.base / "first"
        cls.run_a = cli("run", "--output", str(cls.first))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self):
        target = self.base / f"copy-{self.id().rsplit('.', 1)[-1]}"
        shutil.copytree(self.first, target)
        return target

    def assertCleanRefusal(self, result, needle=None):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIsInstance(json.loads(result.stdout)["errors"], list)
        if needle:
            self.assertIn(needle, result.stdout)

    def seal(self, out, edit):
        sealed = json.loads((out / demo.HASHES).read_text())
        edit(sealed)
        (out / demo.HASHES).write_text(json.dumps(sealed))

    def test_the_fixture_run_succeeded(self):
        self.assertEqual(self.run_a.returncode, 0, self.run_a.stdout + self.run_a.stderr)

    # issue 192
    def test_hash_file_without_files_key_is_a_clean_refusal(self):
        out = self.copy()
        self.seal(out, lambda sealed: sealed.pop("files"))
        self.assertCleanRefusal(cli("verify", "--output", str(out)))

    def test_hash_file_that_is_a_list_is_a_clean_refusal(self):
        out = self.copy()
        (out / demo.HASHES).write_text("[]")
        self.assertCleanRefusal(cli("verify", "--output", str(out)))

    def test_hash_file_with_a_non_object_files_value_is_a_clean_refusal(self):
        out = self.copy()
        self.seal(out, lambda sealed: sealed.update(files=["x"]))
        self.assertCleanRefusal(cli("verify", "--output", str(out)))

    def test_matroska_remux_resealed_is_a_clean_refusal(self):
        out = self.copy()
        ffmpeg, _ = demo.ffmpeg_tools()
        mkv = out / "remux.mkv"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(out / demo.MP4), "-c", "copy", str(mkv)], check=True)
        (out / demo.MP4).write_bytes(mkv.read_bytes())
        mkv.unlink()

        def edit(sealed):
            sealed["mp4_sha256"] = demo.sha256_file(out / demo.MP4)
            sealed["files"][demo.MP4] = sealed["mp4_sha256"]
        self.seal(out, edit)
        self.assertCleanRefusal(cli("verify", "--output", str(out)))

    def test_output_that_is_a_regular_file_is_a_clean_refusal(self):
        target = self.base / "regular-file"
        target.write_text("not a directory")
        self.assertCleanRefusal(cli("run", "--output", str(target)))
        self.assertEqual(target.read_text(), "not a directory")

    def test_output_that_is_a_symlink_to_a_marked_directory_is_refused_and_left_alone(self):
        real = self.copy()
        link = self.base / "link-to-demo"
        link.symlink_to(real, target_is_directory=True)
        self.assertCleanRefusal(cli("run", "--output", str(link)))
        self.assertTrue((real / demo.MP4).is_file())
        self.assertTrue(link.is_symlink())

    def test_output_that_is_a_dangling_symlink_is_a_clean_refusal(self):
        link = self.base / "dangling"
        link.symlink_to(self.base / "nowhere")
        self.assertCleanRefusal(cli("run", "--output", str(link)))
        self.assertTrue(link.is_symlink())

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_unreadable_marker_is_a_clean_refusal(self):
        target = self.base / "unreadable-marker"
        target.mkdir()
        (target / demo.MARKER).write_text(demo.SCHEMA)
        (target / "mine.txt").write_text("keep")
        (target / demo.MARKER).chmod(0)
        try:
            self.assertCleanRefusal(cli("run", "--output", str(target)))
        finally:
            (target / demo.MARKER).chmod(0o644)
        self.assertEqual((target / "mine.txt").read_text(), "keep")

    # issue 193
    def test_demo_qc_overwritten_with_a_failing_empty_report_is_named(self):
        out = self.copy()
        (out / demo.DEMO_QC).write_text(json.dumps({"ok": False, "checks": []}))
        result = cli("verify", "--output", str(out))
        self.assertCleanRefusal(result, demo.DEMO_QC)

    def test_demo_qc_with_one_check_flipped_is_named(self):
        out = self.copy()
        report = json.loads((out / demo.DEMO_QC).read_text())
        report["checks"][0]["detail"] = "forged"
        (out / demo.DEMO_QC).write_text(json.dumps(report))
        self.assertCleanRefusal(cli("verify", "--output", str(out)), demo.DEMO_QC)

    def test_demo_qc_that_is_missing_is_named(self):
        out = self.copy()
        (out / demo.DEMO_QC).unlink()
        self.assertCleanRefusal(cli("verify", "--output", str(out)), demo.DEMO_QC)

    def test_forged_deterministic_hash_is_named(self):
        out = self.copy()
        self.seal(out, lambda sealed: sealed["deterministic"].update(frames_sha256="0" * 64))
        self.assertCleanRefusal(cli("verify", "--output", str(out)), "deterministic")

    def test_forged_stage_list_is_named(self):
        out = self.copy()
        self.seal(out, lambda sealed: sealed.update(stages=["nothing"]))
        self.assertCleanRefusal(cli("verify", "--output", str(out)), "stages")

    def test_forged_encoder_is_named(self):
        out = self.copy()
        self.seal(out, lambda sealed: sealed["encoder"].update(ffmpeg="forged"))
        self.assertCleanRefusal(cli("verify", "--output", str(out)), "encoder")

    def test_forged_schema_is_named(self):
        out = self.copy()
        self.seal(out, lambda sealed: sealed.update(schema="bogus"))
        self.assertCleanRefusal(cli("verify", "--output", str(out)), "schema")

    def test_an_untouched_copy_still_verifies(self):
        self.assertEqual(cli("verify", "--output", str(self.copy())).returncode, 0)


if __name__ == "__main__":
    unittest.main()
