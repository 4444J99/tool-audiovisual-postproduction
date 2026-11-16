"""Issue 191: replacing a marked demo directory is atomic (build beside it, swap in on success), and an output that is
or contains the working directory, the repository, the home directory or the filesystem root is refused up front."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "short_film_demo.py"
sys.path.insert(0, str(ROOT / "tools"))
import short_film_demo as demo  # noqa: E402


def cli(*args, cwd=ROOT, env=None):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=cwd, env=env)


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

    def test_filesystem_root_and_repository_are_refused(self):
        for unsafe in ("/", str(ROOT), str(ROOT.parent)):
            with self.subTest(unsafe=unsafe):
                self.assertCleanRefusal(cli("run", "--output", unsafe, cwd=self.base))

    def test_home_directory_is_refused_even_when_it_is_a_marked_demo_output(self):
        home = self.marked("home")
        env = {**os.environ, "HOME": str(home)}
        self.assertCleanRefusal(cli("run", "--output", str(home), cwd=self.base, env=env))
        self.assertEqual((home / "keep.txt").read_text(), "old output")
        self.assertEqual((home / demo.MARKER).read_text(), demo.SCHEMA)

    def test_a_differently_cased_spelling_of_a_guarded_directory_is_refused(self):
        home = self.marked("Home-Mixed")
        shouting = Path(str(home).upper())
        if not shouting.exists():
            self.skipTest("this volume is case-sensitive")
        env = {**os.environ, "HOME": str(home)}
        self.assertCleanRefusal(cli("run", "--output", str(shouting), cwd=self.base, env=env))
        self.assertEqual((home / "keep.txt").read_text(), "old output")
        inner = home / "inner"
        inner.mkdir()
        self.assertCleanRefusal(cli("run", "--output", str(shouting), cwd=inner))
        self.assertEqual((home / "keep.txt").read_text(), "old output")

    def test_interruption_between_the_two_renames_leaves_the_old_output_in_place(self):
        target = self.marked()
        real_rename = os.rename
        seen = []

        def interrupting(src, dst, *args, **kwargs):
            if Path(src) == target or Path(dst) == target:
                seen.append((str(src), str(dst)))
                if len(seen) == 2:
                    raise KeyboardInterrupt
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch.object(os, "rename", interrupting):
            with self.assertRaises(KeyboardInterrupt):
                demo.run_demo(target)
        self.assertEqual(len(seen), 3, seen)  # out to retired, the interrupted build to out, retired back to out
        self.assertEqual((target / "keep.txt").read_text(), "old output")
        self.assertEqual((target / demo.MARKER).read_text(), demo.SCHEMA)
        self.assertEqual(self.siblings(), ["marked"])

    def test_stale_siblings_of_this_output_only_are_swept(self):
        target = self.marked()
        old = time.time() - 7200

        def stale(name, marked=True, age=old):
            path = self.base / name
            path.mkdir()
            (path / "leftover.txt").write_text("x")
            if marked:
                (path / demo.MARKER).write_text(demo.SCHEMA)
            os.utime(path, (age, age))
            return path
        swept_build = stale(".marked.build-abcd1234")
        swept_old = stale(".marked.old-abcd1234")
        young = stale(".marked.build-young123", age=time.time())
        unmarked = stale(".marked.build-nomarker", marked=False)
        other = stale(".other.build-abcd1234")
        odd = stale(".marked.build-x")
        result = cli("run", "--output", str(target), "--delivery", "streaming")
        self.assertCleanRefusal(result)
        self.assertFalse(swept_build.exists())
        self.assertFalse(swept_old.exists())
        for survivor in (young, unmarked, other, odd):
            self.assertTrue(survivor.exists(), survivor)

    @unittest.skipIf(os.geteuid() == 0, "root removes anything")
    def test_a_tree_that_cannot_be_removed_is_reported_as_a_warning(self):
        tree = self.base / "stuck"
        (tree / "audio").mkdir(parents=True)
        (tree / "audio" / "a.wav").write_text("x")
        (tree / "audio").chmod(0o555)
        warnings = []
        try:
            with mock.patch.object(os, "chmod", side_effect=PermissionError("no")):
                demo.remove_tree(tree, warnings)
            self.assertTrue(warnings, "a removal that failed must be reported")
        finally:
            (tree / "audio").chmod(0o755)

    def test_warnings_reach_the_json_summary(self):
        hashes = {"chain_sha256": "c", "mp4_sha256": "m", "deterministic": {}}

        def fake(out, delivery, qc_spec, warnings):
            warnings.append("could not remove /somewhere")
            return hashes
        captured = io.StringIO()
        with mock.patch.object(demo, "run_demo", fake), contextlib.redirect_stdout(captured):
            self.assertEqual(demo.main(["run", "--output", str(self.base / "x")]), 0)
        self.assertEqual(json.loads(captured.getvalue())["warnings"], ["could not remove /somewhere"])


if __name__ == "__main__":
    unittest.main()
