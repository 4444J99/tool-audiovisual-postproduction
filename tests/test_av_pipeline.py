import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from amp.studies import pipeline_chain as pc
from amp.studies import shot_plan as sp

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "av_pipeline.py"


def cli(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


def blender_available():
    sys.path.insert(0, str(ROOT / "tools"))
    import set_scene
    return set_scene.find_blender() is not None


class ChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "a"
        cls.manifest = pc.run_chain(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self):
        target = Path(self.tmp.name) / f"copy-{self.id().rsplit('.', 1)[-1]}"
        shutil.copytree(self.out, target)
        return target

    def stage(self, name):
        return next(s for s in self.manifest["stages"] if s["stage"] == name)

    def test_every_file_the_run_writes_is_hashed(self):
        hashed = {pc.LOCATIONS[label] for entry in self.manifest["stages"] for label in entry["outputs"]}
        written = {str(p.relative_to(self.out)) for p in self.out.rglob("*") if p.is_file()} - {"pipeline-manifest.json"}
        self.assertEqual(written, hashed)

    def test_stage_order_and_every_input_is_an_earlier_output(self):
        self.assertEqual([s["stage"] for s in self.manifest["stages"]], list(pc.STAGES))
        produced = {}
        for entry in self.manifest["stages"]:
            for label, digest in entry["inputs"].items():
                self.assertEqual(produced.get(label), digest, f"{entry['stage']} input {label}")
            produced.update(entry["outputs"])
        self.assertEqual(pc.verify_chain(self.out), [])
        self.assertEqual(pc.cross_checks(self.out), [])

    def test_artefacts_chain_together_by_content(self):
        spec_stage, plan_stage, cover = self.stage("set"), self.stage("shot_plan"), self.stage("coverage")
        self.assertEqual(plan_stage["inputs"]["shot-plan-set"], spec_stage["outputs"]["shot-plan-set"])
        plan = json.loads((self.out / "plan" / "shot-plan.json").read_text())
        self.assertEqual(plan["set"], json.loads((self.out / "set" / "shot-plan-set.json").read_text()))
        self.assertEqual(sp.plan_hash(plan), plan_stage["plan_sha256"])
        self.assertEqual(cover["gaps_before"], 2)
        self.assertEqual(cover["errors_after"], [])
        self.assertNotEqual(cover["final_plan_sha256"], plan_stage["plan_sha256"])
        final = json.loads((self.out / "coverage" / "suggested-plan.json").read_text())
        self.assertEqual(sp.plan_hash(final), cover["final_plan_sha256"])
        self.assertEqual(len(final["shots"]), len(plan["shots"]) + cover["shots_suggested"])
        ledger = json.loads((self.out / "plan" / "final-ledger.json").read_text())
        self.assertEqual(ledger["plan_sha256"], cover["final_plan_sha256"])
        colour = self.stage("colour")
        self.assertEqual(colour["edit_frames"], ledger["total_frames"])
        cplan = json.loads((self.out / "colour" / "layer-plan.json").read_text())
        self.assertEqual(cplan["comp"]["frames"], ledger["total_frames"])
        self.assertEqual(cplan["sources"]["dialogue"]["sha256"], self.stage("room_tone")["outputs"]["dialogue-filled"])
        spans = {l["id"]: (l["frames"]["start"], l["frames"]["end"]) for l in cplan["layers"] if l["id"].startswith("shot-")}
        self.assertEqual(spans, {f"shot-{r['id'].lower()}": (r["edit_in"], r["edit_out"]) for r in ledger["shots"]})

    def test_measured_room_tone_feeds_coverage_and_the_fill_succeeds(self):
        req = json.loads((self.out / "coverage" / "requirement.json").read_text())
        self.assertAlmostEqual(req["room_tone"][0]["seconds"], self.stage("requirement")["tone_seconds"], places=4)
        self.assertGreaterEqual(req["room_tone"][0]["seconds"], req["room_tone_min_s"])
        fill = self.stage("room_tone")
        self.assertEqual(fill["holes_filled"], 2)
        self.assertEqual(fill["remaining_digital_silence"], [])
        self.assertTrue(fill["untouched_identical"])
        self.assertNotEqual(fill["outputs"]["dialogue-filled"], fill["inputs"]["dialogue-with-holes"])

    def test_run_is_deterministic(self):
        with tempfile.TemporaryDirectory() as other:
            again = pc.run_chain(Path(other))
            self.assertEqual(again["chain_sha256"], self.manifest["chain_sha256"])
            self.assertEqual(again, self.manifest)

    def test_changed_artefact_is_named(self):
        for label, rel in (("shot-plan", "plan/shot-plan.json"), ("coverage-matrix", "coverage/coverage-matrix.md"),
                           ("coverage-report", "coverage/coverage-report.json"), ("fill-report", "audio/fill-report.json"), ("dialogue-filled", "audio/dialogue-filled.wav"),
                           ("scene-graph-summary", "set/scene-graph-summary.json")):
            target = self.copy()
            path = target / rel
            path.write_bytes(path.read_bytes() + b" ")
            problems = pc.verify_chain(target)
            self.assertTrue(any(f"output {label} on disk" in p for p in problems), (label, problems))
            shutil.rmtree(target)

    def test_manifest_edit_breaks_the_chain_hash(self):
        target = self.copy()
        manifest = json.loads((target / "pipeline-manifest.json").read_text())
        manifest["stages"][1]["inputs"]["shot-plan-set"] = "0" * 64
        (target / "pipeline-manifest.json").write_text(json.dumps(manifest))
        problems = pc.verify_chain(target)
        self.assertTrue(any("chain_sha256" in p for p in problems))
        self.assertTrue(any("shot_plan: input shot-plan-set" in p for p in problems))

    def test_content_links_catch_a_rehashed_swap(self):
        """An attacker who rewrites the artefact and the manifest still cannot break the content links."""
        target = self.copy()
        cplan_path = target / "colour" / "layer-plan.json"
        cplan = json.loads(cplan_path.read_text())
        cplan["sources"]["plate-s01"]["sha256"] = "1" * 64
        cplan_path.write_text(json.dumps(cplan, indent=2, sort_keys=True) + "\n")
        problems = pc.cross_checks(target)
        self.assertEqual(problems, ["plate for S01 is not derived from the set graph and its ledger row"])

    def test_missing_file_is_reported_not_raised(self):
        target = self.copy()
        (target / "audio" / "dialogue-filled.wav").unlink()
        (target / "coverage" / "coverage-matrix.md").unlink()
        problems = pc.verify_chain(target)
        self.assertTrue(any("dialogue-filled on disk is missing" in p for p in problems))
        self.assertTrue(any("coverage-matrix on disk is missing" in p for p in problems))
        self.assertEqual(pc.verify_chain(Path(self.tmp.name) / "nowhere")[0][:9], "manifest:")


class CliTests(unittest.TestCase):
    def test_run_then_verify_then_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "chain"
            ran = cli("run", "--output", str(out))
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertEqual(json.loads(ran.stdout)["stages"], list(pc.STAGES))
            self.assertEqual(cli("verify", "--output", str(out)).returncode, 0)
            (out / "plan" / "shot-plan.json").write_text("{}")
            bad = cli("verify", "--output", str(out))
            self.assertEqual(bad.returncode, 2)
            self.assertIn("output shot-plan on disk", bad.stdout)

    def test_verify_of_an_empty_directory_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            bad = cli("verify", "--output", d)
            self.assertEqual(bad.returncode, 2)
            self.assertIn("manifest: cannot read", bad.stdout)

    @unittest.skipUnless(blender_available(), "Blender is not installed; the Blender stage is skipped")
    def test_blender_builds_the_same_graph_the_chain_hashed(self):
        with tempfile.TemporaryDirectory() as d:
            ran = cli("run", "--output", str(Path(d) / "chain"), "--blender")
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            result = json.loads(ran.stdout)
            manifest = json.loads((Path(d) / "chain" / "pipeline-manifest.json").read_text())
            self.assertEqual(result["blender_graph_sha256"], manifest["stages"][0]["graph_sha256"])
            self.assertTrue((Path(d) / "chain" / "set" / "scene.blend").is_file())
            self.assertEqual(result["chain_sha256"], manifest["chain_sha256"])
            self.assertEqual(manifest["stages"][-1]["stage"], "blender")
            self.assertEqual(manifest["stages"][-1]["graph_sha256"], result["blender_graph_sha256"])
            self.assertEqual(cli("verify", "--output", str(Path(d) / "chain")).returncode, 0)
            (Path(d) / "chain" / "set" / "scene.blend").unlink()
            bad = cli("verify", "--output", str(Path(d) / "chain"))
            self.assertEqual(bad.returncode, 2)
            self.assertIn("output scene-blend on disk is missing", bad.stdout)

    def test_blender_stage_refuses_a_different_graph_hash(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "chain"
            pc.run_chain(out)
            (out / "set" / "blender-report.json").write_text("{}")
            (out / "set" / "scene.blend").write_bytes(b"x")
            with self.assertRaises(pc.ChainError):
                pc.append_blender_stage(out, "0" * 64)
            manifest = pc.append_blender_stage(out, json.loads((out / "pipeline-manifest.json").read_text())["stages"][0]["graph_sha256"])
            self.assertEqual(pc.verify_chain(out), [])
            manifest["stages"][-1]["graph_sha256"] = "0" * 64
            manifest["chain_sha256"] = pc.chain_hash(manifest)
            (out / "pipeline-manifest.json").write_text(json.dumps(manifest))
            self.assertEqual(pc.cross_checks(out), ["the recorded Blender graph hash is not the set stage's graph hash"])


if __name__ == "__main__":
    unittest.main()
