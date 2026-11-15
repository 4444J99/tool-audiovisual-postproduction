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
DOC = ROOT / "docs" / "DEMO.md"
sys.path.insert(0, str(ROOT / "tools"))
import short_film_demo as demo  # noqa: E402


DOCUMENTED = ("chain_sha256", "qc_report_sha256", "frames_sha256", "delivered_dialogue_sha256", "captions_srt_sha256")


def cli(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


def encoders_available():
    try:
        demo.ffmpeg_tools()
    except demo.DemoError:
        return False
    return True


@unittest.skipUnless(encoders_available(), "ffmpeg with libx264 and aac is required to build the mp4")
class ShortFilmDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.first, cls.second = base / "first", base / "second"
        cls.run_a = cli("run", "--output", str(cls.first))
        cls.run_b = cli("run", "--output", str(cls.second))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self):
        target = Path(self.tmp.name) / f"copy-{self.id().rsplit('.', 1)[-1]}"
        shutil.copytree(self.first, target)
        return target

    def hashes(self, out):
        return json.loads((out / demo.HASHES).read_text())

    def test_both_runs_succeed_and_print_the_same_summary(self):
        self.assertEqual(self.run_a.returncode, 0, self.run_a.stdout + self.run_a.stderr)
        self.assertEqual(self.run_b.returncode, 0, self.run_b.stdout + self.run_b.stderr)
        a, b = json.loads(self.run_a.stdout), json.loads(self.run_b.stdout)
        for key in ("chain_sha256", "mp4_sha256", "frames_sha256", "delivered_dialogue_sha256", "captions_srt_sha256", "qc_report_sha256"):
            self.assertRegex(a[key], r"^[0-9a-f]{64}$")
            self.assertEqual(a[key], b[key], key)

    def test_hashes_are_stable_across_two_runs_for_every_file(self):
        a, b = self.hashes(self.first), self.hashes(self.second)
        self.assertEqual(a, b)
        self.assertEqual(sorted(a["files"]), sorted(str(p.relative_to(self.first)) for p in self.first.rglob("*") if p.is_file() and str(p.relative_to(self.first)) not in (demo.HASHES, demo.DEMO_QC, demo.MARKER)))
        for name in a["files"]:
            self.assertEqual((self.first / name).read_bytes(), (self.second / name).read_bytes(), name)

    def test_chain_and_qc_hashes_repeat_and_are_the_hashes_of_the_files(self):
        a, b = self.hashes(self.first), self.hashes(self.second)
        manifest = json.loads((self.first / "pipeline-manifest.json").read_text())
        self.assertEqual(a["chain_sha256"], b["chain_sha256"])
        self.assertEqual(a["chain_sha256"], manifest["chain_sha256"])
        self.assertEqual(a["deterministic"]["qc_report_sha256"], b["deterministic"]["qc_report_sha256"])
        self.assertEqual(a["deterministic"]["qc_report_sha256"], demo.sha256_file(self.first / "qc" / "qc-report.json"))
        self.assertEqual(a["files"]["pipeline-manifest.json"], b["files"]["pipeline-manifest.json"])
        self.assertEqual(a["files"]["qc/qc-report.json"], b["files"]["qc/qc-report.json"])

    def test_every_hash_is_the_same_from_another_cwd_a_relative_path_and_a_changed_environment(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        env = dict(os.environ, PYTHONHASHSEED="4242", TZ="Pacific/Auckland", LC_ALL="C", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        run = subprocess.run([sys.executable, str(TOOL), "run", "--output", "deep/../relative-out"], capture_output=True, text=True, cwd=elsewhere, env=env)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        moved = elsewhere / "relative-out"
        self.assertEqual(self.hashes(moved), self.hashes(self.first))
        for name in ("pipeline-manifest.json", "qc/qc-report.json", demo.DEMO_QC, demo.HASHES):
            self.assertEqual((moved / name).read_bytes(), (self.first / name).read_bytes(), name)
        self.assertEqual(cli("verify", "--output", str(moved)).returncode, 0)

    def test_mp4_bytes_repeat_and_are_the_hashed_file(self):
        self.assertEqual((self.first / demo.MP4).read_bytes(), (self.second / demo.MP4).read_bytes())
        self.assertEqual(self.hashes(self.first)["mp4_sha256"], demo.sha256_file(self.first / demo.MP4))
        self.assertEqual(self.hashes(self.first)["files"][demo.MP4], demo.sha256_file(self.first / demo.MP4))
        self.assertLess((self.first / demo.MP4).stat().st_size, 1_000_000)

    def test_whole_chain_ran_and_qc_passes(self):
        stages = self.hashes(self.first)["stages"]
        self.assertEqual(stages, ["set", "shot_plan", "dialogue", "requirement", "coverage", "room_tone", "delivery", "final_ledger", "colour", "captions", "burnin", "qc"])
        qc = json.loads((self.first / "qc" / "qc-report.json").read_text())
        self.assertTrue(all(check["ok"] for check in qc["checks"]))
        manifest = json.loads((self.first / "pipeline-manifest.json").read_text())
        self.assertEqual(self.hashes(self.first)["chain_sha256"], manifest["chain_sha256"])
        self.assertEqual(self.hashes(self.first)["deterministic"]["qc_report_sha256"], demo.sha256_file(self.first / "qc" / "qc-report.json"))

    def test_mp4_is_judged_against_the_chain(self):
        report = json.loads((self.first / demo.DEMO_QC).read_text())
        self.assertTrue(report["ok"])
        ids = {check["id"] for check in report["checks"]}
        self.assertEqual(ids, {"mp4.streams", "mp4.video_geometry", "mp4.video_pixel_format", "mp4.audio_format", "mp4.duration", "mp4.frame_count", "mp4.luma_equals_burnin", "mp4.chroma_flat", "mp4.audio_loudness", "mp4.audio_matches_delivered", "mp4.audio_blocks_match_delivered", "mp4.audio_padding_silent"})
        luma = next(c for c in report["checks"] if c["id"] == "mp4.luma_equals_burnin")
        self.assertEqual(luma["decoded_luma_sha256"], self.hashes(self.first)["deterministic"]["frames_sha256"])

    def test_verify_accepts_a_fresh_run_and_names_a_flipped_mp4_byte(self):
        self.assertEqual(cli("verify", "--output", str(self.first)).returncode, 0)
        out = self.copy()
        data = bytearray((out / demo.MP4).read_bytes())
        data[len(data) // 2] ^= 0xFF
        (out / demo.MP4).write_bytes(bytes(data))
        result = cli("verify", "--output", str(out))
        self.assertEqual(result.returncode, 2)
        self.assertIn(demo.MP4, result.stdout)

    def test_resealed_lossy_mp4_is_named_by_the_decoded_luma(self):
        out = self.copy()
        lossy = out / "lossy.mp4"
        ffmpeg, _ = demo.ffmpeg_tools()
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(out / demo.MP4), "-c:v", "libx264", "-crf", "45", "-c:a", "copy", str(lossy)], check=True)
        (out / demo.MP4).write_bytes(lossy.read_bytes())
        lossy.unlink()
        sealed = self.hashes(out)
        sealed["mp4_sha256"] = demo.sha256_file(out / demo.MP4)
        sealed["files"][demo.MP4] = sealed["mp4_sha256"]
        (out / demo.HASHES).write_text(json.dumps(sealed))
        result = cli("verify", "--output", str(out))
        self.assertEqual(result.returncode, 2)
        self.assertIn("mp4.luma_equals_burnin", result.stdout)

    def test_verify_names_an_edited_chain_artefact(self):
        out = self.copy()
        path = out / "captions" / "captions.srt"
        path.write_text(path.read_text().replace("Hello", "Jello"))
        result = cli("verify", "--output", str(out))
        self.assertEqual(result.returncode, 2)
        self.assertIn("captions", result.stdout)

    def test_foreign_non_empty_directory_is_refused_and_left_alone(self):
        target = Path(self.tmp.name) / "foreign"
        target.mkdir()
        (target / "mine.txt").write_text("keep")
        result = cli("run", "--output", str(target))
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a previous demo output", result.stdout)
        self.assertEqual((target / "mine.txt").read_text(), "keep")

    def test_a_failed_run_does_not_lock_its_directory(self):
        target = Path(self.tmp.name) / "retry"
        self.assertEqual(cli("run", "--output", str(target), "--delivery", "streaming").returncode, 2)
        self.assertTrue(any(target.iterdir()), "the refused run should have left chain files behind")
        result = cli("run", "--output", str(target))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((target / demo.MP4).is_file())

    def test_a_stray_hash_file_does_not_authorise_replacing_a_foreign_directory(self):
        target = Path(self.tmp.name) / "stray"
        target.mkdir()
        (target / demo.HASHES).write_text("{}")
        (target / "mine.txt").write_text("keep")
        self.assertEqual(cli("run", "--output", str(target)).returncode, 2)
        self.assertEqual((target / "mine.txt").read_text(), "keep")

    def test_rerun_over_its_own_output_gives_the_same_hashes(self):
        out = self.copy()
        before = self.hashes(out)
        self.assertEqual(cli("run", "--output", str(out)).returncode, 0)
        self.assertEqual(self.hashes(out), before)

    def test_a_delivery_spec_the_audio_cannot_meet_is_refused_without_an_mp4(self):
        target = Path(self.tmp.name) / "refused"
        result = cli("run", "--output", str(target), "--delivery", "streaming")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((target / demo.MP4).exists())

    def test_demo_doc_lists_the_command_and_the_hashes_this_run_produces(self):
        text = DOC.read_text()
        self.assertIn("python tools/short_film_demo.py run --output outputs/short-film-demo", text)
        summary = json.loads(self.run_a.stdout)
        stale = {key: summary[key] for key in DOCUMENTED if summary[key] not in text}
        self.assertEqual(stale, {}, "docs/DEMO.md does not list these observed hashes")
