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
        moved = json.loads(json.dumps(observed))
        moved["objects"][1]["location"][0] += 3.0
        self.assertEqual(len(set_scene.compare(expected, moved)), 1)
        observed["objects"][1]["dimensions"][0] += 0.5
        observed["objects"][2]["vertices"] -= 1
        observed["objects"][-1]["present"] = False
        observed["mesh_count"] -= 1
        found = set_scene.compare(expected, observed)
        self.assertEqual(len(found), 4, found)

    def test_demo_spec_v1_1_validates_and_reports_its_negotiation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "demo11.json"
            self.assertEqual(run("demo-spec", str(path), "--version", "1.1").returncode, 0)
            ok = json.loads(run("validate", "--spec", str(path)).stdout)
            self.assertEqual((ok["schema"], ok["required_schema"]), (set_spec.SCHEMA_V11, set_spec.SCHEMA_V11))
            self.assertEqual(json.loads(run("graph", "--spec", str(path)).stdout)["mesh_count"], 16)
            plain = Path(d) / "demo1.json"
            run("demo-spec", str(plain))
            self.assertEqual(json.loads(run("validate", "--spec", str(plain)).stdout)["required_schema"], set_spec.SCHEMA_V1)

    def test_v1_declaration_with_v1_1_content_is_refused_and_writes_nothing(self):
        spec = set_spec.synthetic_spec("1.1")
        spec["schema"] = set_spec.SCHEMA_V1
        with tempfile.TemporaryDirectory() as d:
            blend = Path(d) / "scene.blend"
            result = run("build", "--spec", self.write_spec(d, spec), "--blend", str(blend))
            self.assertEqual(result.returncode, 2)
            out = json.loads(result.stdout)
            self.assertTrue(any("needs schema amps.set_spec.v1.1" in e for e in out["errors"]))
            self.assertFalse(out["negotiation"]["ok"])
            self.assertFalse(blend.exists())

    def test_upgrade_redeclares_a_v1_spec_as_v1_1_without_changing_its_graph(self):
        with tempfile.TemporaryDirectory() as d:
            src = self.write_spec(d)
            out = Path(d) / "up.json"
            self.assertEqual(run("upgrade", "--spec", src, str(out)).returncode, 0)
            up = json.loads(out.read_text())
            self.assertEqual(up["schema"], set_spec.SCHEMA_V11)
            self.assertEqual(set_spec.scene_graph(up)["objects"], set_spec.scene_graph(set_spec.synthetic_spec())["objects"])
            bad = set_spec.synthetic_spec()
            bad["lights"][0]["energy"] = 0
            refused = run("upgrade", "--spec", self.write_spec(d, bad), str(Path(d) / "no.json"))
            self.assertEqual(refused.returncode, 2)
            self.assertFalse((Path(d) / "no.json").exists())
            for payload in ("null", "[]", "3", '"text"'):
                odd = Path(d) / "odd.json"
                odd.write_text(payload)
                result = run("upgrade", "--spec", str(odd), str(Path(d) / "no.json"))
                self.assertEqual(result.returncode, 2, (payload, result.stderr))
                self.assertIn("errors", json.loads(result.stdout))
            unknown = set_spec.synthetic_spec()
            unknown["schema"] = "other"
            self.assertEqual(run("upgrade", "--spec", self.write_spec(d, unknown), str(Path(d) / "no.json")).returncode, 2)

    def test_compare_catches_a_primitive_that_lands_in_the_wrong_place_or_shape(self):
        expected = set_spec.graph_summary(set_spec.scene_graph(set_spec.synthetic_spec("1.1")))

        def observed():
            seen = json.loads(json.dumps(expected))
            for row in seen["objects"]:
                row["present"] = True
                if row["type"] == "mesh":
                    row["watertight"] = True
            return seen
        self.assertEqual(set_scene.compare(expected, observed()), [])
        for index, row in enumerate(expected["objects"]):
            if row["name"] == "rail":
                rail = index
        wrong_axis = observed()                       # an x-axis rail that Blender stood up along z: bounds differ, volume does not
        wrong_axis["objects"][rail]["world_bounds"] = {"min": [-0.12, -2.12, 0.9], "max": [0.12, -1.88, 4.9]}
        found = set_scene.compare(expected, wrong_axis)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all("world" in f for f in found))
        inside_out = observed()
        inside_out["objects"][rail]["volume"] = -expected["objects"][rail]["volume"]
        self.assertTrue(any("volume expected" in f for f in set_scene.compare(expected, inside_out)))
        open_mesh = observed()
        open_mesh["objects"][rail]["watertight"] = False
        self.assertTrue(any("non-manifold" in f for f in set_scene.compare(expected, open_mesh)))
        unmeasured = observed()
        for key in ("world_bounds", "volume"):
            del unmeasured["objects"][rail][key]
        found = set_scene.compare(expected, unmeasured)
        self.assertTrue(any("world bounds were not measured" in f for f in found) and any("volume was not measured" in f for f in found))


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

    def test_every_blender_mesh_is_watertight_with_positive_volume(self):
        for row in self.report["observed"]["objects"]:
            if "volume" in row:
                self.assertTrue(row["watertight"], row["name"])
                self.assertGreater(row["volume"], 0.0, row["name"])


@unittest.skipUnless(BLENDER, "Blender is not installed; v1.1 primitive checks inside Blender did not run")
class BlenderV11PrimitiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.spec = cls.dir / "spec.json"
        cls.spec.write_text(json.dumps(set_spec.synthetic_spec("1.1")))
        cls.report_path = cls.dir / "report.json"
        cls.build = run("build", "--spec", str(cls.spec), "--report", str(cls.report_path))
        cls.report = json.loads(cls.report_path.read_text()) if cls.report_path.exists() else {}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rows(self):
        return {o["name"]: o for o in self.report["observed"]["objects"]}

    def test_build_succeeds_and_blender_matches_the_graph_for_every_new_primitive(self):
        self.assertEqual(self.build.returncode, 0, self.build.stdout + self.build.stderr)
        self.assertEqual(set_scene.compare(self.report["expected"], self.report["observed"]), [])
        self.assertEqual(self.report["observed"]["mesh_count"], 16)

    def test_blender_measures_the_primitives_in_world_axes(self):
        rows = self.rows()
        self.assertEqual(rows["rail"]["dimensions"], [4.0, 0.24, 0.24])
        self.assertEqual(rows["rail"]["world_bounds"], {"min": [-2.0, -2.12, 0.9], "max": [2.0, -1.88, 1.14]})
        self.assertEqual(rows["post"]["dimensions"], [0.3, 3.0, 0.3])
        self.assertEqual(rows["obelisk"]["dimensions"], [1.2, 1.2, 2.4])
        self.assertEqual(rows["finial"]["dimensions"], [0.7, 0.7, 0.7])
        self.assertEqual(rows["hoop"]["dimensions"], [2.6, 0.2, 2.6])                   # hole along depth: a ring standing in x-z
        self.assertEqual(rows["steps"]["dimensions"], [2.4, 1.6, 0.9])
        self.assertEqual(rows["steps-turned"]["world_bounds"]["min"][0], -5.1)           # quarter turn swaps its x and depth
        self.assertAlmostEqual(rows["steps"]["volume"], 2.16, places=5)
        for name in ("rail", "post", "obelisk", "finial", "hoop", "steps", "steps-turned"):
            self.assertEqual(rows[name]["collection"], ["Dressing"] if name in ("rail", "post", "finial", "hoop") else ["Architecture"])

    def test_a_horizontal_cylinder_at_every_axis_and_turn_matches_the_graph(self):
        spec = set_spec.synthetic_spec("1.1")
        spec["elements"] = [
            {"id": f"c-{axis}-{int(turn)}", "kind": "cylinder", "axis": axis, "center": [x, z], "radius": 0.2, "height": 1.0,
             "material": "wood", "rotation_deg": turn, "segments": 16}
            for axis, x in (("x", -3.0), ("y", 0.0), ("z", 3.0)) for turn, z in ((0.0, -3.0), (37.0, 0.0), (90.0, 3.0))]
        path = self.dir / "matrix.json"
        path.write_text(json.dumps(spec))
        result = run("build", "--spec", str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
