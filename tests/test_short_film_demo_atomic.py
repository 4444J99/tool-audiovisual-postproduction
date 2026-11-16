"""Issue 191: replacing a marked demo directory is atomic (build beside it, swap in on success), and an output that is
or contains the working directory, the repository, the home directory or the filesystem root is refused up front."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "short_film_demo.py"
sys.path.insert(0, str(ROOT / "tools"))
import short_film_demo as demo  # noqa: E402


def cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=cwd)


def encoders_available():
    try:
        demo.ffmpeg_tools()
    except demo.DemoError:
        return False
    return True


@unittest.skipUnless(encoders_available(), "ffmpeg with libx264 and aac is required to build the mp4")
class AtomicReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def marked(self, name="marked"):
        target = self.base / name
        target.mkdir()
        (target / demo.MARKER).write_text(demo.SCHEMA)
        (target / "keep.txt").write_text("old output")
        return target

    def assertCleanRefusal(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIsInstance(json.loads(result.stdout)["errors"], list)

    def siblings(self):
        return sorted(p.name for p in self.base.iterdir())

    def test_failed_run_leaves_the_old_output_intact_and_no_stray_directories(self):
        target = self.marked()
        result = cli("run", "--output", str(target), "--delivery", "streaming")
        self.assertCleanRefusal(result)
        self.assertEqual((target / "keep.txt").read_text(), "old output")
        self.assertEqual((target / demo.MARKER).read_text(), demo.SCHEMA)
        self.assertEqual(self.siblings(), ["marked"])

    @unittest.skipIf(os.geteuid() == 0, "root writes anywhere")
    def test_read_only_subdirectory_does_not_lock_the_directory(self):
        target = self.marked()
        audio = target / "audio"
        audio.mkdir()
        (audio / "stem.wav").write_text("old")
        audio.chmod(0o555)
        try:
            failed = cli("run", "--output", str(target), "--delivery", "streaming")
            self.assertCleanRefusal(failed)
            self.assertEqual((audio / "stem.wav").read_text(), "old")
            self.assertEqual((target / demo.MARKER).read_text(), demo.SCHEMA)
            rerun = cli("run", "--output", str(target))
            self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        finally:
            for path in self.base.rglob("audio"):
                path.chmod(0o755)
        self.assertTrue((target / demo.MP4).is_file())
        self.assertEqual((target / demo.MARKER).read_text(), demo.SCHEMA)
        self.assertFalse((target / "keep.txt").exists())
        self.assertEqual(cli("verify", "--output", str(target)).returncode, 0)
        self.assertEqual(self.siblings(), ["marked"])

    def test_output_dot_is_refused_without_deleting_anything(self):
        target = self.marked()
        self.assertCleanRefusal(cli("run", "--output", ".", cwd=target))
        self.assertEqual((target / "keep.txt").read_text(), "old output")
        self.assertEqual((target / demo.MARKER).read_text(), demo.SCHEMA)

    def test_output_parent_of_the_working_directory_is_refused_without_deleting_anything(self):
        target = self.marked()
        inner = target / "inner"
        inner.mkdir()
        self.assertCleanRefusal(cli("run", "--output", "..", cwd=inner))
        self.assertCleanRefusal(cli("run", "--output", str(target), cwd=inner))
        self.assertEqual((target / "keep.txt").read_text(), "old output")
        self.assertTrue(inner.is_dir())

    def test_filesystem_root_home_and_repository_are_refused(self):
        for unsafe in ("/", str(Path.home()), str(ROOT), str(ROOT.parent)):
            with self.subTest(unsafe=unsafe):
                self.assertCleanRefusal(cli("run", "--output", unsafe, cwd=self.base))


if __name__ == "__main__":
    unittest.main()
