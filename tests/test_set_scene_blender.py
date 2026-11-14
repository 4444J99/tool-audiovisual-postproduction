import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import set_scene  # noqa: E402
from amp.studies import set_spec  # noqa: E402

BLENDER = set_scene.find_blender()
TOOL = ROOT / "tools" / "set_scene.py"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


class SetSceneCliTests(unittest.TestCase):
    """Plain-Python paths: no Blender needed."""

    def write_spec(self, directory, spec=None):
        path = Path(directory) / "spec.json"
        path.write_text(json.dumps(spec or set_spec.synthetic_spec()))
        return str(path)

    def test_demo_spec_validates_and_exports_graph_and_shot_set(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "demo.json"
            self.assertEqual(run("demo-spec", str(path)).returncode, 0)
            ok = run("validate", "--spec", str(path))
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertTrue(json.loads(ok.stdout)["ok"])
            graph = json.loads(run("graph", "--spec", str(path)).stdout)
            self.assertEqual(graph["mesh_count"], 9)
            shot_set = json.loads(run("shot-set", "--spec", str(path)).stdout)
            self.assertEqual(shot_set["bounds"]["x"], [-6.0, 6.0])

    def test_invalid_spec_prints_all_errors_and_writes_nothing(self):
        spec = set_spec.synthetic_spec()
        spec["elements"][0]["material"] = "missing"
        spec["lights"][0]["energy"] = 0
        with tempfile.TemporaryDirectory() as d:
            blend = Path(d) / "scene.blend"
            result = run("build", "--spec", self.write_spec(d, spec), "--blend", str(blend))
            self.assertEqual(result.returncode, 2)
            self.assertGreaterEqual(len(json.loads(result.stdout)["errors"]), 2)
            self.assertFalse(blend.exists())

    def test_unreadable_spec_is_refused(self):
        result = run("validate", "--spec", "/nonexistent/spec.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read", result.stdout)

    def test_compare_names_every_difference(self):
        graph = set_spec.scene_graph(set_spec.synthetic_spec())
        expected = set_spec.graph_summary(graph)
        observed = json.loads(json.dumps(expected))
        for row in observed["objects"]:
            row["present"] = True
        self.assertEqual(set_scene.compare(expected, observed), [])
        observed["objects"][1]["dimensions"][0] += 0.5
        observed["objects"][2]["vertices"] -= 1
        observed["objects"][-1]["present"] = False
        observed["mesh_count"] -= 1
        found = set_scene.compare(expected, observed)
        self.assertEqual(len(found), 4, found)


@unittest.skipUnless(BLENDER, "Blender is not installed; scene-graph checks inside Blender did not run")
class BlenderSetSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        spec = cls.dir / "spec.json"
        spec.write_text(json.dumps(set_spec.synthetic_spec()))
        cls.report_path = cls.dir / "report.json"
        cls.blend = cls.dir / "scene.blend"
        cls.build = run("build", "--spec", str(spec), "--blend", str(cls.blend), "--report", str(cls.report_path))
        cls.report = json.loads(cls.report_path.read_text()) if cls.report_path.exists() else {}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_build_succeeds_and_matches_the_scene_graph(self):
        self.assertEqual(self.build.returncode, 0, self.build.stdout + self.build.stderr)
        self.assertEqual(set_scene.compare(self.report["expected"], self.report["observed"]), [])

    def test_scene_graph_inside_blender(self):
        observed = self.report["observed"]
        by_name = {o["name"]: o for o in observed["objects"]}
        self.assertEqual((observed["mesh_count"], observed["light_count"], observed["camera_count"]), (9, 2, 2))
        self.assertEqual(observed["active_camera"], "cam-wide")
        self.assertEqual(observed["collections"], ["Architecture", "Cameras", "Dressing", "Lighting"])
        self.assertEqual(by_name["table"]["dimensions"], [2.2, 2.2, 0.9])
        self.assertEqual(by_name["arch.Band_A"]["dimensions"], [6.4, 0.9, 3.2])
        self.assertEqual(by_name["arch.Band_D"]["dimensions"], [3.7, 0.6, 1.85])
        self.assertEqual(by_name["arch.Band_B"]["materials"], ["hex-4F9D8A", "hex-4F9D8A-soffit"])
        histogram = by_name["arch.Band_B"]["face_material_histogram"]
        self.assertEqual(histogram, {"0": 24 * 3 + 2, "1": 24})        # 24 segments: three lit faces and one soffit each
        self.assertEqual(by_name["cam-wide"]["lens_mm"], 24.0)
        self.assertEqual(by_name["cam-side"]["lens_mm"], 35.0)
        self.assertEqual(by_name["key"]["light_type"], "AREA")
        self.assertEqual(by_name["glow"]["light_type"], "POINT")
        self.assertEqual(by_name["key"]["energy"], 400.0)
        self.assertEqual(by_name["brazier"]["collection"], ["Dressing"])
        self.assertEqual(by_name["table"]["collection"], ["Dressing"])
        self.assertEqual(by_name["back-wall"]["collection"], ["Architecture"])

    def test_cameras_look_toward_their_aim_in_blender_axes(self):
        by_name = {o["name"]: o for o in self.report["observed"]["objects"]}
        forward = by_name["cam-wide"]["forward"]
        self.assertGreater(forward[1], 0.9)               # looks into the set (+y in Blender)
        self.assertGreater(forward[2], 0.0)               # and slightly up, aim is 0.6 m above the lens
        side = by_name["cam-side"]["forward"]
        self.assertGreater(side[0], 0.9)                  # side camera looks along +x

    def test_saved_blend_reopens_with_the_same_objects(self):
        expr = ("import bpy, json; print('REOPEN ' + json.dumps({'objects': sorted(o.name for o in bpy.data.objects),"
                " 'camera': bpy.context.scene.camera.name}))")
        result = subprocess.run([BLENDER, "-b", "--factory-startup", str(self.blend), "--python-expr", expr],
                                capture_output=True, text=True, timeout=120)
        line = next(l for l in result.stdout.splitlines() if l.startswith("REOPEN "))
        data = json.loads(line[len("REOPEN "):])
        expected = sorted(o["name"] for o in set_spec.scene_graph(set_spec.synthetic_spec())["objects"])
        self.assertEqual(data["objects"], expected)
        self.assertEqual(data["camera"], "cam-wide")

    def test_still_render_is_written(self):
        spec = self.dir / "spec.json"
        still = self.dir / "still.png"
        result = run("build", "--spec", str(spec), "--still", str(still), "--width", "64", "--height", "36",
                     "--samples", "2", "--camera", "cam-side")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        from PIL import Image
        with Image.open(still) as image:
            self.assertEqual(image.size, (64, 36))
            low, high = image.convert("L").getextrema()
            self.assertGreater(high - low, 20)

    def test_build_without_a_report_still_checks_blender_against_the_graph(self):
        spec = self.dir / "spec.json"
        original = set_scene.compare
        calls = []
        set_scene.compare = lambda expected, observed: calls.append(1) or original(expected, observed)
        try:
            self.assertEqual(set_scene.main(["build", "--spec", str(spec)]), 0)
        finally:
            set_scene.compare = original
        self.assertEqual(calls, [1])

    def test_camera_override_is_reflected_in_the_graph_hash_blender_reports(self):
        spec = self.dir / "spec.json"
        side_report = self.dir / "side.json"
        result = run("build", "--spec", str(spec), "--camera", "cam-side", "--report", str(side_report))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(side_report.read_text())
        self.assertEqual(data["observed"]["active_camera"], "cam-side")
        self.assertNotEqual(data["graph_sha256"], self.report["graph_sha256"])
        override = set_spec.synthetic_spec()
        override["active_camera"] = "cam-side"
        self.assertEqual(data["graph_sha256"], set_spec.scene_graph(override)["graph_sha256"])

    def test_rotated_arch_has_the_expected_world_extent_in_blender(self):
        spec = set_spec.synthetic_spec()
        spec["elements"][1].update(rotation_deg=90.0, center=[0.0, 4.0])
        path = self.dir / "rotated.json"
        path.write_text(json.dumps(spec))
        report = self.dir / "rotated-report.json"
        result = run("build", "--spec", str(path), "--report", str(report))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = {o["name"]: o for o in json.loads(report.read_text())["observed"]["objects"]}
        self.assertEqual(rows["arch.Band_A"]["location"][0], 0.45)       # recess moved along -x with the turn

    def test_unknown_camera_is_refused(self):
        result = run("build", "--spec", str(self.dir / "spec.json"), "--camera", "ghost")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ghost", result.stdout)


if __name__ == "__main__":
    unittest.main()
