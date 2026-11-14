import copy
import unittest

from amp.studies import set_spec as ss
from amp.studies import shot_plan as sp


def spec():
    return ss.synthetic_spec()


def errors(s):
    return ss.validate_spec(s)["errors"]


class SetSpecValidationTests(unittest.TestCase):
    def test_synthetic_spec_is_valid(self):
        self.assertEqual(ss.validate_spec(spec()), {"errors": [], "warnings": []})

    def test_wrong_schema_and_missing_bounds_are_refused(self):
        s = spec()
        s["schema"] = "other"
        self.assertEqual(len(errors(s)), 1)
        s = spec()
        s["bounds"]["height"] = 0
        self.assertIn("bounds", errors(s)[0])

    def test_all_failures_are_reported_together(self):
        s = spec()
        s["elements"][0]["material"] = "missing"
        s["elements"][2]["radius"] = -1
        s["lights"][0]["energy"] = 0
        found = errors(s)
        self.assertGreaterEqual(len(found), 3)
        self.assertTrue(any("missing" in e for e in found))
        self.assertTrue(any("radius" in e for e in found))
        self.assertTrue(any("energy" in e for e in found))

    def test_duplicate_and_reserved_ids(self):
        s = spec()
        s["elements"][1]["id"] = "table"
        s["elements"][2]["id"] = "table"
        self.assertTrue(any("duplicate" in e for e in errors(s)))
        s = spec()
        s["elements"][0]["id"] = "FLOOR"
        self.assertTrue(any("reserved" in e for e in errors(s)))
        s = spec()
        s["cameras"][0]["id"] = "key"          # clashes with a light
        self.assertTrue(any("duplicate" in e for e in errors(s)))

    def test_footprint_and_height_must_stay_inside_the_set(self):
        s = spec()
        s["elements"][4]["center"] = [-5.9, 1.0]
        self.assertTrue(any("outside the set bounds" in e for e in errors(s)))
        s = spec()
        s["elements"][0]["size"][1] = 9.0
        self.assertTrue(any("exceeds the set height" in e for e in errors(s)))

    def test_rotated_box_footprint_uses_its_rotated_extent(self):
        s = spec()
        s["elements"][4].update(center=[-5.0, 1.0], rotation_deg=90.0)   # 0.5 x 2.4 turned: half-x 1.2
        self.assertTrue(any("outside the set bounds" in e for e in errors(s)))

    def test_arch_band_rules(self):
        s = spec()
        s["elements"][1]["bands"][0]["width"] = 3.2
        self.assertTrue(any("no opening" in e for e in errors(s)))
        s = spec()
        s["elements"][1]["step_depth"] = 0.3        # 4 bands x 0.3 reveal outruns the 0.9 m depth
        self.assertTrue(any("no depth left" in e for e in errors(s)))
        s = spec()
        s["elements"][1]["bands"][1]["name"] = "Band  A"
        s["elements"][1]["bands"][0]["name"] = "Band  A"
        self.assertTrue(any("unique name" in e for e in errors(s)))
        s = spec()
        s["elements"][1]["bands"][1]["name"] = "Band_A".replace("_", " ")   # collapses onto Band A's object name
        s["elements"][1]["bands"][0]["name"] = "Band_A"
        self.assertTrue(any("unique name" in e for e in errors(s)))
        s = spec()
        s["elements"][1]["bands"][2]["color"] = "nothex"
        self.assertTrue(any("hex" in e for e in errors(s)))
        s = spec()
        s["elements"][1]["bands"][0]["name"] = "n" * 60
        self.assertTrue(any("longer than" in e for e in errors(s)))

    def test_camera_and_light_rules(self):
        s = spec()
        s["cameras"][0]["aim"] = [0.0, 3.0, -5.0]
        s["cameras"][1]["lens_mm"] = 5
        s["active_camera"] = "ghost"
        s["lights"][0].pop("aim")
        found = errors(s)
        self.assertTrue(any("horizontal direction" in e for e in found))
        self.assertTrue(any("lens_mm" in e for e in found))
        self.assertTrue(any("active_camera" in e for e in found))
        self.assertTrue(any("needs an aim" in e for e in found))

    def test_bad_types_do_not_raise(self):
        for bad in (None, [], {"schema": ss.SCHEMA}, {**spec(), "elements": "x"}, {**spec(), "lights": 3},
                    {**spec(), "floor": 4}, {**spec(), "materials": []}):
            self.assertIsInstance(ss.validate_spec(bad)["errors"], list)
        s = spec()
        s["elements"].append("loose")
        self.assertTrue(errors(s))
        s = spec()
        s["materials"]["wood"]["roughness"] = True
        self.assertTrue(any("roughness" in e for e in errors(s)))


class SceneGraphTests(unittest.TestCase):
    def test_graph_is_deterministic_and_counts_match(self):
        a, b = ss.scene_graph(spec()), ss.scene_graph(spec())
        self.assertEqual(a["graph_sha256"], b["graph_sha256"])
        summary = ss.graph_summary(a)
        # floor + wall + 4 arch bands + table + brazier + bench
        self.assertEqual((summary["mesh_count"], summary["light_count"], summary["camera_count"]), (9, 2, 2))
        self.assertEqual(summary["active_camera"], "cam-wide")

    def test_any_spec_change_changes_the_graph_hash(self):
        s = spec()
        s["elements"][2]["radius"] = 1.2
        self.assertNotEqual(ss.scene_graph(s)["graph_sha256"], ss.scene_graph(spec())["graph_sha256"])

    def test_blender_axes_swap_depth_and_height(self):
        graph = ss.scene_graph(spec())
        cam = next(o for o in graph["objects"] if o["name"] == "cam-wide")
        self.assertEqual(cam["location"], [0.0, -5.0, 1.6])
        table = next(o for o in graph["objects"] if o["name"] == "table")
        self.assertEqual(table["location"], [0.0, 1.0, 0.0])
        self.assertEqual(ss.mesh_bounds(table["mesh"])["dimensions"], [2.2, 2.2, 0.9])

    def test_arch_bands_are_nested_stepped_and_materialised(self):
        graph = ss.scene_graph(spec())
        bands = [o for o in graph["objects"] if o.get("arch") == "arch"]
        self.assertEqual([b["name"] for b in bands], ["arch.Band_A", "arch.Band_B", "arch.Band_C", "arch.Band_D"])
        widths = [ss.mesh_bounds(b["mesh"])["dimensions"][0] for b in bands]
        self.assertEqual(widths, [6.4, 5.4, 4.5, 3.7])                # 2 x radius, shrinking by 2 x width
        depths = [ss.mesh_bounds(b["mesh"])["dimensions"][1] for b in bands]
        self.assertEqual(depths, [0.9, 0.8, 0.7, 0.6])                # each reveal recedes 0.1
        fronts = [b["location"][1] for b in bands]
        self.assertEqual([round(f, 6) for f in fronts], [6.05, 6.15, 6.25, 6.35])
        backs = {round(b["location"][1] + d, 6) for b, d in zip(bands, depths)}
        self.assertEqual(backs, {6.95})                                # backs stay flush
        for b in bands:
            self.assertEqual(len(b["face_materials"]), len(b["mesh"]["faces"]))
            self.assertEqual(b["materials"][1], b["materials"][0] + "-soffit")
            soffit = graph["materials"][b["materials"][1]]["color"]
            face = graph["materials"][b["materials"][0]]["color"]
            self.assertTrue(all(int(soffit[i:i + 2], 16) < int(face[i:i + 2], 16) for i in (0, 2, 4)))

    def test_arch_mesh_is_closed_and_indices_valid(self):
        graph = ss.scene_graph(spec())
        for obj in graph["objects"]:
            if obj["type"] != "mesh":
                continue
            count = len(obj["mesh"]["verts"])
            edges = {}
            for face in obj["mesh"]["faces"]:
                self.assertTrue(all(0 <= i < count for i in face))
                for a, b in zip(face, face[1:] + face[:1]):
                    edges[(a, b)] = edges.get((a, b), 0) + 1
            for (a, b), n in edges.items():
                self.assertEqual(n, 1, (obj["name"], a, b))
                self.assertIn((b, a), edges, f"{obj['name']} edge {a}-{b} has no opposite half-edge")

    def test_hex_materials_are_linear_and_deterministic(self):
        self.assertEqual(ss.hex_to_linear("FFFFFF"), [1.0, 1.0, 1.0])
        self.assertEqual(ss.hex_to_linear("000000"), [0.0, 0.0, 0.0])
        mid = ss.hex_to_linear("808080")[0]
        self.assertAlmostEqual(mid, 0.215861, places=5)

    def test_invalid_spec_raises_with_every_error(self):
        s = spec()
        s["elements"][0]["material"] = "missing"
        s["lights"][0]["energy"] = -1
        with self.assertRaises(ss.SpecError) as raised:
            ss.scene_graph(s)
        self.assertGreaterEqual(len(raised.exception.errors), 2)

    def test_shot_plan_set_feeds_a_valid_shot_plan(self):
        plan = sp.synthetic_plan()
        plan["set"] = ss.shot_plan_set(spec())
        plan["set"]["caches"] = [{"id": "flame", "frames": [1, 240], "kind": "physical"}]
        # The synthetic pilot's door and window are not in this set; point its shots at this set's elements.
        for shot in plan["shots"]:
            for use in shot["uses"]:
                if use["id"] == "door":
                    use["id"] = "arch"
                if use["id"] == "window":
                    use["id"] = "bench"
        plan["shots"][1]["uses"].append({"id": "brazier", "contribution": "shadow"})
        plan["performers"] = [{"id": "A", "position": [-1.0, 0.0]}, {"id": "B", "position": [1.0, 0.0]}]
        report = sp.validate_plan(plan)
        self.assertEqual(report["errors"], [], report["errors"])
        self.assertEqual({e["id"] for e in plan["set"]["elements"] if e.get("added")}, {"arch", "brazier"})

    def test_shot_plan_set_radius_matches_the_footprint(self):
        rows = {e["id"]: e for e in ss.shot_plan_set(spec())["elements"]}
        self.assertEqual(rows["table"]["radius"], 1.1)
        self.assertEqual(rows["arch"]["radius"], 3.2)
        self.assertEqual(rows["bench"]["radius"], 1.2)


if __name__ == "__main__":
    unittest.main()
