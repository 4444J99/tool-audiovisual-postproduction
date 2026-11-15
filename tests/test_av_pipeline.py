import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from amp.studies import loudness as ld
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
        self.assertEqual(cplan["sources"]["dialogue"]["sha256"], self.stage("delivery")["outputs"]["dialogue-delivered"])
        self.assertEqual(ledger["dialogue_sha256"], self.stage("delivery")["outputs"]["dialogue-delivered"])
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

    def test_delivery_stage_sits_between_room_tone_and_the_final_ledger(self):
        names = [s["stage"] for s in self.manifest["stages"]]
        self.assertEqual(names[names.index("room_tone") + 1:names.index("final_ledger")], ["delivery"])
        delivery = self.stage("delivery")
        self.assertEqual(delivery["inputs"], {"dialogue-filled": self.stage("room_tone")["outputs"]["dialogue-filled"]})
        self.assertEqual(self.stage("final_ledger")["inputs"]["dialogue-delivered"], delivery["outputs"]["dialogue-delivered"])

    def test_delivered_audio_is_measured_and_meets_the_declared_spec(self):
        report = json.loads((self.out / "audio" / "delivery-report.json").read_text())
        delivery = self.stage("delivery")
        self.assertEqual(delivery["spec_name"], "ebu_r128")
        self.assertEqual(report["input_sha256"], self.stage("room_tone")["outputs"]["dialogue-filled"])
        self.assertEqual(report["output_sha256"], delivery["outputs"]["dialogue-delivered"])
        self.assertNotEqual(report["input_sha256"], report["output_sha256"])
        sr, delivered = pc.read_pcm16(self.out / "audio" / "dialogue-delivered.wav")
        measured = ld.measure(delivered, sr)       # measured here, from the bytes on disk, not from the report
        self.assertEqual(ld.check(measured, "ebu_r128"), [])
        self.assertAlmostEqual(measured["integrated_lufs"], -23.0, delta=0.05)
        self.assertEqual(measured, report["after"])
        sr, filled = pc.read_pcm16(self.out / "audio" / "dialogue-filled.wav")
        self.assertAlmostEqual(measured["integrated_lufs"] - ld.measure(filled, sr)["integrated_lufs"], report["gain_db"], delta=0.02)
        self.assertEqual(report["problems"], [])

    def test_delivery_spec_is_selectable_and_recorded(self):
        for preset in ("podcast", "atsc_a85"):
            with tempfile.TemporaryDirectory() as d:
                manifest = pc.run_chain(Path(d), delivery=preset)
                stage = next(s for s in manifest["stages"] if s["stage"] == "delivery")
                self.assertEqual(stage["spec_name"], preset)
                self.assertAlmostEqual(stage["after_lufs"], ld.PRESETS[preset]["integrated_lufs"]["target"], delta=0.05)
                self.assertEqual(pc.verify_chain(Path(d)) + pc.cross_checks(Path(d), preset), [])
                self.assertNotEqual(manifest["chain_sha256"], self.manifest["chain_sha256"])

    def test_a_spec_the_audio_cannot_meet_is_refused_not_sealed(self):
        """The synthetic dialogue peaks at -13.3 dBTP; -3 LUFS would need +23 dB, far over a -1 dBTP ceiling."""
        spec = {"name": "too-hot", "integrated_lufs": {"target": -3.0, "tolerance": 0.5}, "true_peak_dbtp_max": -1.0}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(pc.ChainError) as raised:
                pc.run_chain(Path(d), delivery=spec)
            self.assertIn("true peak", raised.exception.errors[0])
            self.assertFalse((Path(d) / "pipeline-manifest.json").exists())
            self.assertFalse((Path(d) / "audio" / "dialogue-delivered.wav").exists())
        with self.assertRaises(pc.ChainError) as streaming:      # the real streaming preset (-14 LUFS) needs +12.6 dB: peak -0.7 dBTP
            pc.run_chain(Path(self.tmp.name) / "streaming", delivery="streaming")
        self.assertIn("over the -1.0 dBTP ceiling", streaming.exception.errors[0])
        with self.assertRaises(pc.ChainError):
            pc.run_chain(Path(self.tmp.name) / "bad-spec", delivery={"integrated_lufs": "loud"})

    def test_tampered_delivered_audio_is_named(self):
        target = self.copy()
        path = target / "audio" / "dialogue-delivered.wav"
        path.write_bytes(path.read_bytes()[:-2] + b"\x7f\x7f")
        problems = pc.verify_chain(target)
        self.assertTrue(any("output dialogue-delivered on disk" in p for p in problems), problems)

    def test_rehashed_swap_of_the_delivered_audio_is_caught_by_content_checks(self):
        """Replace the delivered WAV with a louder file and rewrite the report, the manifest and the colour plan
        so every hash agrees: only re-deriving the audio and re-measuring it can still object."""
        target = self.copy()
        sr, filled = pc.read_pcm16(target / "audio" / "dialogue-filled.wav")
        loud = pc.pcm16_bytes(ld.apply_gain(filled, 12.0), sr)
        (target / "audio" / "dialogue-delivered.wav").write_bytes(loud)
        digest = pc.sha256_bytes(loud)
        report_path = target / "audio" / "delivery-report.json"
        report = json.loads(report_path.read_text())
        report["output_sha256"] = digest
        report_h = pc.write_json(report_path, report)
        manifest = json.loads((target / "pipeline-manifest.json").read_text())
        old = next(s for s in manifest["stages"] if s["stage"] == "delivery")["outputs"]["dialogue-delivered"]
        for entry in manifest["stages"]:
            for side in ("inputs", "outputs"):
                for label, value in entry[side].items():
                    if value == old:
                        entry[side][label] = digest
            if entry["stage"] == "delivery":
                entry["outputs"]["delivery-report"] = report_h
        manifest["chain_sha256"] = pc.chain_hash(manifest)
        (target / "pipeline-manifest.json").write_text(json.dumps(manifest))
        for rel, keys in (("colour/layer-plan.json", None), ("plan/final-ledger.json", None)):
            text = (target / rel).read_text().replace(old, digest)
            (target / rel).write_text(text)
        for entry in manifest["stages"]:       # the two JSON files changed, so re-seal their hashes too
            for label, rel in (("layer-plan", "colour/layer-plan.json"), ("final-ledger", "plan/final-ledger.json")):
                if label in entry["outputs"]:
                    entry["outputs"][label] = pc.sha256_file(target / rel)
                if label in entry["inputs"]:
                    entry["inputs"][label] = pc.sha256_file(target / rel)
        manifest["chain_sha256"] = pc.chain_hash(manifest)
        (target / "pipeline-manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(pc.verify_chain(target), [], "the forged chain is internally consistent")
        problems = pc.cross_checks(target)
        self.assertTrue(any("not the filled dialogue at the recorded gain" in p for p in problems), problems)
        self.assertTrue(any("measured now" in p and "LUFS" in p for p in problems), problems)

    def test_declared_spec_must_match_the_recorded_one(self):
        self.assertEqual(pc.cross_checks(self.out, "ebu_r128"), [])
        problems = pc.cross_checks(self.out, "podcast")
        self.assertEqual(problems, ["delivery spec recorded in the chain (ebu_r128) is not the declared one"])

    def test_delivery_report_edit_is_caught(self):
        target = self.copy()
        report_path = target / "audio" / "delivery-report.json"
        report = json.loads(report_path.read_text())
        report["gain_db"] += 1.0
        report_path.write_text(json.dumps(report))
        self.assertTrue(any("delivered dialogue is not the filled dialogue at the recorded gain" in p for p in pc.cross_checks(target)))

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

    def test_delivery_option_runs_and_verify_pins_the_declared_spec(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "chain"
            ran = cli("run", "--output", str(out), "--delivery", "podcast")
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertEqual(json.loads(ran.stdout)["delivery"]["spec_name"], "podcast")
            self.assertEqual(cli("verify", "--output", str(out), "--delivery", "podcast").returncode, 0)
            wrong = cli("verify", "--output", str(out), "--delivery", "ebu_r128")
            self.assertEqual(wrong.returncode, 2)
            self.assertIn("is not the declared one", wrong.stdout)
            path = out / "audio" / "dialogue-delivered.wav"
            path.write_bytes(path.read_bytes() + b"\0\0")
            self.assertIn("output dialogue-delivered on disk", cli("verify", "--output", str(out)).stdout)

    def test_unknown_delivery_spec_and_unreachable_spec_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            bad = cli("run", "--output", str(Path(d) / "a"), "--delivery", "no-such-preset")
            self.assertEqual(bad.returncode, 2)
            self.assertIn("neither a preset", bad.stdout)
            spec = Path(d) / "hot.json"
            spec.write_text(json.dumps({"name": "hot", "integrated_lufs": {"target": -3, "tolerance": 0.5}, "true_peak_dbtp_max": -1}))
            hot = cli("run", "--output", str(Path(d) / "b"), "--delivery", str(spec))
            self.assertEqual(hot.returncode, 2)
            self.assertIn("true peak", hot.stdout)
            self.assertFalse((Path(d) / "b" / "pipeline-manifest.json").exists())

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
