import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from amp.studies import coverage_matrix as cm

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "coverage_matrix.py"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


class CoverageMatrixCliTests(unittest.TestCase):
    def test_demo_reports_gaps_then_suggestions_that_close_them(self):
        with tempfile.TemporaryDirectory() as d:
            result = run("--demo", d)
            self.assertEqual(result.returncode, 2, result.stdout)           # the plan has gaps
            summary = json.loads(result.stdout)
            self.assertEqual((summary["gaps"], summary["suggested"], summary["unplaced"]), (2, 2, 0))
            self.assertEqual(summary["errors_after_suggestions"], [])
            report = json.loads((Path(d) / "coverage-report.json").read_text())
            self.assertEqual(len(report["gaps"]), 2)
            self.assertIn("| B |", (Path(d) / "coverage-matrix.md").read_text())
            suggested = json.loads((Path(d) / "suggested-plan.json").read_text())
            self.assertEqual(len(suggested["shots"]), 5)

    def test_suggested_plan_feeds_the_shot_plan_tool(self):
        with tempfile.TemporaryDirectory() as d:
            run("--demo", d)
            shot_tool = ROOT / "tools" / "shot_plan.py"
            checked = subprocess.run([sys.executable, str(shot_tool), "--plan", str(Path(d) / "suggested-plan.json"), "--validate-only"],
                                     capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_plan_and_coverage_files_exit_zero_when_met(self):
        plan, spec = cm.synthetic_inputs()
        spec["required"] = [{"actor": "B", "size": "wide"}]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "plan.json").write_text(json.dumps(plan))
            (Path(d) / "cov.json").write_text(json.dumps(spec))
            result = run("--plan", str(Path(d) / "plan.json"), "--coverage", str(Path(d) / "cov.json"), "--output", str(Path(d) / "out"))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue((Path(d) / "out" / "coverage-report.json").exists())
            self.assertFalse((Path(d) / "out" / "suggested-plan.json").exists())

    def test_unreadable_or_missing_inputs_are_refused(self):
        self.assertEqual(run("--plan", "/nonexistent.json", "--coverage", "/nonexistent.json").returncode, 2)
        self.assertEqual(run().returncode, 2)

    def test_invalid_plan_still_writes_a_report_with_the_errors(self):
        plan, spec = cm.synthetic_inputs()
        plan["shots"][0]["camera"]["lens_mm"] = 3
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "plan.json").write_text(json.dumps(plan))
            (Path(d) / "cov.json").write_text(json.dumps(spec))
            result = run("--plan", str(Path(d) / "plan.json"), "--coverage", str(Path(d) / "cov.json"), "--output", str(Path(d) / "out"), "--suggest")
            self.assertEqual(result.returncode, 2)
            report = json.loads((Path(d) / "out" / "coverage-report.json").read_text())
            self.assertTrue(report["errors"][0].startswith("shot plan:"))


if __name__ == "__main__":
    unittest.main()
