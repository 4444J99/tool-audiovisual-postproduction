import copy
import math
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

class ReviewRegressionTests(unittest.TestCase):
    def test_arch_base_elevation_counts_toward_the_set_height(self):
        s = spec()
        s["elements"][1]["y"] = 4.0                      # 4 + outer radius 3.2 = 7.2 m, above the 7 m set
        self.assertTrue(any("exceeds the set height" in e for e in errors(s)))
        s["elements"][1]["y"] = 3.8                      # 7.0 m exactly fits
        self.assertEqual(errors(s), [])

    def test_arch_rotation_turns_every_band_about_the_element_centre(self):
        s = spec()
        s["elements"][1]["rotation_deg"] = 90.0
        s["elements"][1]["center"] = [0.0, 4.0]          # room for the turned footprint (6.4 deep along z)
        graph = ss.scene_graph(s)
        bands = [o for o in graph["objects"] if o.get("arch") == "arch"]
        self.assertTrue(all(b["rotation_deg"] == 90.0 for b in bands))
        # unrotated the bands recede along +y (Blender depth); turned 90 degrees they recede along -x
        xs = [b["location"][0] for b in bands]
        ys = [round(b["location"][1], 6) for b in bands]
        self.assertEqual(ys, [4.0] * 4)
        self.assertEqual([round(x, 6) for x in xs], [0.45, 0.35, 0.25, 0.15])
        self.assertEqual(xs, sorted(xs, reverse=True))
        hx, hz = ss.element_extent(s["elements"][1])
        self.assertAlmostEqual(hx, 0.45)
        self.assertAlmostEqual(hz, 3.2)

    def test_rotated_arch_footprint_is_checked_against_the_bounds(self):
        s = spec()
        s["elements"][1]["rotation_deg"] = 90.0          # half-extent along z becomes the 3.2 m radius, past z=8
        self.assertTrue(any("outside the set bounds" in e for e in errors(s)))

    def test_invalid_base_elevation_is_reported_not_raised(self):
        for kind_index in (1, 2, 3):
            for bad in ("1", None, True, -0.5, float("inf")):
                s = spec()
                s["elements"][kind_index]["y"] = bad
                found = errors(s)
                self.assertTrue(any("base elevation" in e for e in found), (kind_index, bad, found))

    def test_user_materials_cannot_collide_with_generated_names(self):
        for name in ("hex-3C6E8F", "wood-soffit", "floor-default"):
            s = spec()
            s["materials"][name] = {"color": "123456"}
            self.assertTrue(any("reserved" in e for e in errors(s)), name)
        graph = ss.scene_graph(spec())
        self.assertEqual(graph["materials"]["hex-3C6E8F"]["color"], "3C6E8F")
        self.assertEqual(graph["materials"]["hex-3C6E8F"]["roughness"], 0.6)

    def test_non_string_references_are_reported_not_raised(self):
        for bad in ([], {}, 3, True):
            s = spec()
            s["floor"]["material"] = bad
            self.assertTrue(any("floor: material" in e for e in errors(s)), bad)
            s = spec()
            s["elements"][0]["material"] = bad
            self.assertTrue(any("material" in e for e in errors(s)), bad)
            s = spec()
            s["active_camera"] = bad
            self.assertTrue(any("active_camera" in e for e in errors(s)), bad)

    def test_sensor_width_has_blenders_one_millimetre_floor(self):
        for bad, ok in ((0.5, False), (0.999, False), (1.0, True)):
            s = spec()
            s["cameras"][0]["sensor_width_mm"] = bad
            self.assertEqual(not errors(s), ok, bad)

    def test_empty_material_name_is_refused(self):
        s = spec()
        s["materials"][""] = {"color": "123456"}
        s["floor"] = {"material": ""}
        self.assertTrue(any("material ''" in e for e in errors(s)), errors(s))

    def test_caches_follow_the_shot_plan_rules(self):
        s = spec()
        s["caches"] = [{"id": "flame", "frames": [1, 240]}]
        self.assertEqual(errors(s), [])
        self.assertEqual(ss.shot_plan_set(s)["caches"], [{"id": "flame", "frames": [1, 240]}])
        element_id = s["elements"][0]["id"]
        for bad in ([{"id": "flame", "frames": [1.5, 2.5]}], [{"id": "flame", "frames": [5, 1]}], [{"id": "flame"}],
                    [{"id": "f", "frames": [1, 2]}, {"id": "f", "frames": [1, 2]}], [{"id": element_id, "frames": [1, 2]}],
                    [{"id": "", "frames": [1, 2]}], [{"id": "x", "frames": [True, 2]}], ["flame"], "flame"):
            s = spec()
            s["caches"] = bad
            self.assertTrue(any("cache" in e for e in errors(s)), bad)

    def test_sensor_width_must_be_a_finite_positive_number(self):
        for bad in ("bad", None, 0, -36.0, float("nan"), True, [36]):
            s = spec()
            s["cameras"][0]["sensor_width_mm"] = bad
            self.assertTrue(any("sensor_width_mm" in e for e in errors(s)), bad)
        s = spec()
        s["cameras"][0]["sensor_width_mm"] = 24.0
        self.assertEqual(errors(s), [])
        graph = ss.scene_graph(s)
        self.assertEqual(next(o for o in graph["objects"] if o["name"] == "cam-wide")["sensor_width_mm"], 24.0)

    def test_optional_light_values_are_validated_before_expansion(self):
        for key, bad in (("color", None), ("color", 5), ("aim", None), ("aim", [1, 2]), ("aim", "x")):
            for index in (0, 1):                     # area light with an aim, point light without one
                s = spec()
                s["lights"][index][key] = bad
                found = errors(s)
                self.assertTrue(any(key in e for e in found), (index, key, bad, found))
                with self.assertRaises(ss.SpecError):
                    ss.scene_graph(s)
        s = spec()
        s["lights"][1]["aim"] = [0.0, 0.0, 0.0]      # a point light may carry an aim
        self.assertEqual(errors(s), [])
        ss.scene_graph(s)

    def test_name_limits_count_utf8_bytes(self):
        s = spec()
        s["materials"]["é" * 30] = {"color": "123456"}        # 30 characters but 60 bytes, 67 with the soffit suffix
        self.assertTrue(any("must fit 63 bytes" in e for e in errors(s)))
        s = spec()
        s["elements"][1]["bands"][0]["name"] = "é" * 25        # "arch." + 50 bytes + nothing = 55 ok
        self.assertEqual(errors(s), [])
        s["elements"][1]["bands"][0]["name"] = "é" * 30        # 5 + 60 = 65 bytes
        self.assertTrue(any("longer than 63 bytes" in e for e in errors(s)))

    def test_light_size_must_be_a_finite_positive_number(self):
        for bad in (None, "bad", 0, -1.0, float("inf"), True, [2]):
            s = spec()
            s["lights"][0]["size"] = bad
            self.assertTrue(any("size must be" in e for e in errors(s)), bad)
        s = spec()
        s["lights"][0]["size"] = 0.5
        self.assertEqual(errors(s), [])

    def test_generated_material_names_fit_blender(self):
        s = spec()
        long_name = "m" * 57                          # 57 + len("-soffit") = 64 > 63
        s["materials"][long_name] = {"color": "123456"}
        s["elements"][1]["bands"][0]["color"] = long_name
        self.assertTrue(any("must fit 63" in e for e in errors(s)))
        s["materials"].pop(long_name)
        s["materials"]["m" * 56] = {"color": "123456"}
        s["elements"][1]["bands"][0]["color"] = "m" * 56
        self.assertEqual(errors(s), [])
        graph = ss.scene_graph(s)
        self.assertTrue(all(len(name) <= 63 for name in graph["materials"]))

    def test_camera_override_changes_the_graph_hash(self):
        a = ss.scene_graph(spec())
        s = spec()
        s["active_camera"] = "cam-side"
        b = ss.scene_graph(s)
        self.assertEqual(b["active_camera"], "cam-side")
        self.assertNotEqual(a["graph_sha256"], b["graph_sha256"])


V1_GRAPH_HASH = "3b177162f029adcb70db815e45a9fc6c039946a909e72717a4390256e905f330"


def spec11():
    return ss.synthetic_spec("1.1")


def element(s, eid):
    return next(e for e in s["elements"] if e["id"] == eid)


def mesh_of(s, eid):
    return next(o for o in ss.scene_graph(s)["objects"] if o["name"] == eid)


class VersionNegotiationTests(unittest.TestCase):
    def test_a_v1_spec_still_builds_the_same_graph(self):
        self.assertEqual(ss.scene_graph(spec())["graph_sha256"], V1_GRAPH_HASH)

    def test_a_v1_spec_is_valid_when_redeclared_v1_1(self):
        s = spec()
        s["schema"] = ss.SCHEMA_V11
        self.assertEqual(ss.validate_spec(s), {"errors": [], "warnings": []})
        graph, base = ss.scene_graph(s), ss.scene_graph(spec())
        self.assertEqual([o["mesh"] for o in graph["objects"] if o["type"] == "mesh"],
                         [o["mesh"] for o in base["objects"] if o["type"] == "mesh"])

    def test_the_v1_1_demo_is_valid_and_declares_what_it_needs(self):
        s = spec11()
        self.assertEqual(ss.validate_spec(s), {"errors": [], "warnings": []})
        self.assertEqual(ss.required_schema(s), ss.SCHEMA_V11)
        self.assertEqual(ss.required_schema(spec()), ss.SCHEMA_V1)
        self.assertTrue(ss.negotiate(s)["ok"])

    def test_v1_declaring_a_v1_1_feature_is_refused_with_the_feature_named(self):
        s = spec11()
        s["schema"] = ss.SCHEMA_V1
        found = errors(s)
        self.assertTrue(any("element rail: axis" in e and ss.SCHEMA_V11 in e for e in found), found)
        self.assertTrue(any("element obelisk: kind 'cone'" in e for e in found), found)
        self.assertFalse(ss.negotiate(s)["ok"])
        with self.assertRaises(ss.SpecError):
            ss.scene_graph(s)
        with self.assertRaises(ss.SpecError):
            ss.shot_plan_set(s)

    def test_a_stray_axis_on_a_v1_spec_is_a_v1_1_feature(self):
        s = spec()
        s["elements"][2]["axis"] = "y"
        self.assertTrue(any("axis" in e and ss.SCHEMA_V11 in e for e in errors(s)))

    def test_unknown_schemas_list_the_supported_ones(self):
        s = spec()
        s["schema"] = "amps.set_spec.v2"
        found = errors(s)
        self.assertEqual(len(found), 1)
        self.assertIn(ss.SCHEMA_V1, found[0])
        self.assertIn(ss.SCHEMA_V11, found[0])
        self.assertFalse(ss.negotiate(s)["ok"])

    def test_negotiation_tolerates_malformed_specs(self):
        for bad in ({}, {"schema": None}, {"schema": ss.SCHEMA_V11, "elements": "x"}, {"elements": [3, None]}):
            self.assertFalse(ss.negotiate(bad)["ok"] and bad.get("schema") is None)
        self.assertEqual(ss.v11_features({"elements": [3, None, {"id": "a", "kind": "box"}]}), [])


class NewPrimitiveValidationTests(unittest.TestCase):
    def test_axis_rules(self):
        s = spec11()
        element(s, "rail")["axis"] = "w"
        self.assertTrue(any("axis must be one of x, y, z" in e for e in errors(s)))
        s = spec11()
        element(s, "steps")["axis"] = "x"                      # stairs have no axis
        self.assertTrue(any("applies only to" in e for e in errors(s)))
        s = spec11()
        element(s, "finial")["axis"] = "x"                     # nor do spheres
        self.assertTrue(any("applies only to" in e for e in errors(s)))

    def test_sphere_cone_torus_stairs_shape_rules(self):
        cases = [("finial", "radius", 0, "radius must be positive"), ("finial", "rings", 3, "rings must be an integer >= 4"),
                 ("obelisk", "height", -1, "radius and height must be positive"), ("obelisk", "top_radius", -0.1, "top_radius"),
                 ("obelisk", "segments", 4, "segments must be an integer >= 8"),
                 ("hoop", "minor_radius", 1.2, "must be smaller than major_radius"), ("hoop", "major_radius", 0, "must be positive"),
                 ("hoop", "tube_segments", 3, "tube_segments"), ("steps", "steps", 1, "steps must be an integer from 2"),
                 ("steps", "steps", 201, "steps must be an integer from 2"), ("steps", "steps", 3.5, "steps must be an integer"),
                 ("steps", "size", [1, 0, 1], "size must be [width, height, depth]")]
        for eid, key, value, message in cases:
            s = spec11()
            element(s, eid)[key] = value
            self.assertTrue(any(message in e for e in errors(s)), (eid, key, errors(s)))

    def test_new_kinds_need_a_material_and_stay_inside_the_set(self):
        s = spec11()
        del element(s, "finial")["material"]
        self.assertTrue(any("material is required" in e for e in errors(s)))
        s = spec11()
        element(s, "rail")["y"] = 6.95                          # lying cylinder: top is the underside plus the diameter
        self.assertTrue(any("exceeds the set height" in e for e in errors(s)))
        s = spec11()
        element(s, "rail")["height"] = 20.0                     # length along x pokes through the side walls
        self.assertTrue(any("outside the set bounds" in e for e in errors(s)))
        s = spec11()
        element(s, "post")["height"] = 20.0                     # and along z through the front and back
        self.assertTrue(any("outside the set bounds" in e for e in errors(s)))

    def test_horizontal_cylinder_extent_follows_axis_and_rotation(self):
        rail = {"kind": "cylinder", "axis": "x", "radius": 0.5, "height": 4.0, "center": [0, 0]}
        self.assertEqual(ss.element_extent(rail), (2.0, 0.5))
        self.assertEqual(ss.element_extent({**rail, "axis": "z"}), (0.5, 2.0))
        hx, hz = ss.element_extent({**rail, "rotation_deg": 90.0})
        self.assertAlmostEqual(hx, 0.5)
        self.assertAlmostEqual(hz, 2.0)
        self.assertEqual(ss.element_extent({**rail, "axis": "y"}), (0.5, 0.5))
        self.assertEqual(ss.local_size({**rail, "axis": "y"}), (1.0, 4.0, 1.0))
        self.assertEqual(ss.local_size({"kind": "torus", "axis": "x", "major_radius": 1.0, "minor_radius": 0.25}), (0.5, 2.5, 2.5))

    def test_the_v1_1_kinds_do_not_raise_on_bad_types(self):
        for kind in ("sphere", "cone", "torus", "stairs"):
            for bad in (None, "x", [], {"radius": "r"}):
                s = spec11()
                s["elements"] = [{"id": "e", "kind": kind, "center": [0, 0], "material": "wood", "radius": bad,
                                  "major_radius": bad, "minor_radius": bad, "size": bad, "steps": bad}]
                self.assertTrue(errors(s))


class NewPrimitiveGeometryTests(unittest.TestCase):
    def test_every_mesh_is_closed_outward_wound_and_resting_on_its_base(self):
        graph = ss.scene_graph(spec11())
        for obj in graph["objects"]:
            if obj["type"] != "mesh":
                continue
            count, edges = len(obj["mesh"]["verts"]), {}
            for face in obj["mesh"]["faces"]:
                self.assertTrue(all(0 <= i < count for i in face), obj["name"])
                for a, b in zip(face, face[1:] + face[:1]):
                    edges[(a, b)] = edges.get((a, b), 0) + 1
            for (a, b), n in edges.items():
                self.assertEqual(n, 1, (obj["name"], a, b))
                self.assertIn((b, a), edges, f"{obj['name']} edge {a}-{b} has no opposite half-edge")
            self.assertGreater(ss.mesh_volume(obj["mesh"]), 0.0, f"{obj['name']} is wound inside out")
            if obj["name"] != "FLOOR" and obj["role"] != "architecture" or obj["name"] in ("obelisk", "steps"):
                self.assertAlmostEqual(ss.mesh_bounds(obj["mesh"])["min"][2], 0.0, places=6, msg=obj["name"])

    def test_axis_turns_the_dimensions_not_the_volume(self):
        base = {"id": "c", "kind": "cylinder", "center": [0.0, 0.0], "radius": 0.5, "height": 3.0, "material": "wood", "segments": 48}
        volumes = {}
        for axis, dims in (("y", [1.0, 1.0, 3.0]), ("x", [3.0, 1.0, 1.0]), ("z", [1.0, 3.0, 1.0])):
            s = spec11()
            s["elements"] = [{**base, "axis": axis}]
            mesh = mesh_of(s, "c")["mesh"]
            self.assertEqual(ss.mesh_bounds(mesh)["dimensions"], dims, axis)     # Blender order: x, depth, up
            volumes[axis] = ss.mesh_volume(mesh)
        self.assertEqual(len(set(volumes.values())), 1)
        self.assertAlmostEqual(volumes["x"], 48 / 2 * 0.25 * math.sin(2 * math.pi / 48) * 3.0, places=5)

    def test_axis_x_cylinder_lies_along_world_x_and_sits_on_its_underside(self):
        s = spec11()
        rail = mesh_of(s, "rail")
        self.assertEqual(rail["location"], [0.0, -2.0, 0.9])
        bounds = ss.world_bounds(rail)
        self.assertEqual(bounds["min"], [-2.0, -2.12, 0.9])
        self.assertEqual(bounds["max"], [2.0, -1.88, 1.14])

    def test_rotation_turns_world_bounds_about_the_element_centre(self):
        s = spec11()
        element(s, "rail")["rotation_deg"] = 90.0
        bounds = ss.world_bounds(mesh_of(s, "rail"))
        self.assertEqual(bounds["min"], [-0.12, -4.0, 0.9])
        self.assertEqual(bounds["max"], [0.12, 0.0, 1.14])

    def test_sphere_cone_torus_volumes_match_their_formulas(self):
        s = spec11()
        sphere = ss.mesh_volume(mesh_of(s, "finial")["mesh"])
        self.assertAlmostEqual(sphere, 4 / 3 * math.pi * 0.35 ** 3, delta=0.03 * sphere)
        r0, r1, h = 0.6, 0.1, 2.4
        cone = ss.mesh_volume(mesh_of(s, "obelisk")["mesh"])
        poly = math.sin(2 * math.pi / 24) * 24 / (2 * math.pi)            # a 24-gon holds this fraction of its circle
        self.assertAlmostEqual(cone, poly * math.pi * h / 3 * (r0 * r0 + r0 * r1 + r1 * r1), places=5)
        torus = ss.mesh_volume(mesh_of(s, "hoop")["mesh"])
        self.assertAlmostEqual(torus, 2 * math.pi ** 2 * 1.2 * 0.1 ** 2, delta=0.12 * torus)         # 8 tube facets and 32 ring facets sit inside the true ring

    def test_point_cone_has_an_apex_and_a_cap(self):
        s = spec11()
        element(s, "obelisk")["top_radius"] = 0.0
        mesh = mesh_of(s, "obelisk")["mesh"]
        self.assertEqual(len(mesh["verts"]), 25)
        self.assertEqual(len(mesh["faces"]), 25)
        self.assertEqual(ss.mesh_bounds(mesh)["dimensions"], [1.2, 1.2, 2.4])
        self.assertAlmostEqual(ss.mesh_volume(mesh), poly_cone(0.6, 2.4, 24), places=5)

    def test_cone_without_top_radius_is_a_point_and_an_inverted_frustum_is_allowed(self):
        s = spec11()
        del element(s, "obelisk")["top_radius"]
        self.assertEqual(len(mesh_of(s, "obelisk")["mesh"]["verts"]), 25)
        element(s, "obelisk")["top_radius"] = 0.9
        self.assertEqual(errors(s), [])
        self.assertEqual(ss.element_extent(element(s, "obelisk")), (0.9, 0.9))
        self.assertGreater(ss.mesh_volume(mesh_of(s, "obelisk")["mesh"]), 0.0)

    def test_stairs_rise_toward_the_back_and_hold_the_stair_volume(self):
        s = spec11()
        stairs = mesh_of(s, "steps")
        mesh = stairs["mesh"]
        self.assertEqual(ss.mesh_bounds(mesh)["dimensions"], [2.4, 1.6, 0.9])
        self.assertEqual(len(mesh["verts"]), 2 * (2 * 4 + 2))
        self.assertEqual(len(mesh["faces"]), 2 + 2 * 4 + 2)
        self.assertAlmostEqual(ss.mesh_volume(mesh), 2.4 * 1.6 * 0.9 * (4 + 1) / (2 * 4), places=6)
        front = [v for v in mesh["verts"] if v[1] < -0.79 and v[2] > 0]
        back = [v for v in mesh["verts"] if v[1] > 0.79]
        self.assertAlmostEqual(max(v[2] for v in front), 0.9 / 4)          # first tread is one rise high
        self.assertAlmostEqual(max(v[2] for v in back), 0.9)

    def test_stairs_extent_and_rotation(self):
        s = spec11()
        turned = element(s, "steps-turned")
        hx, hz = ss.element_extent(turned)
        self.assertAlmostEqual(hx, 0.6)
        self.assertAlmostEqual(hz, 1.0)
        bounds = ss.world_bounds(mesh_of(s, "steps-turned"))
        self.assertAlmostEqual(bounds["min"][0], -4.5 - 0.6, places=5)
        self.assertAlmostEqual(bounds["max"][1], -3.0 + 1.0, places=5)

    def test_every_new_primitive_changes_the_graph_hash_and_summary(self):
        graph = ss.scene_graph(spec11())
        summary = ss.graph_summary(graph)
        self.assertEqual(summary["mesh_count"], 16)
        rows = {r["name"]: r for r in summary["objects"] if r["type"] == "mesh"}
        for name in ("rail", "post", "obelisk", "finial", "hoop", "steps", "steps-turned"):
            self.assertIn("world_bounds", rows[name])
            self.assertGreater(rows[name]["volume"], 0)
        s = spec11()
        element(s, "rail")["axis"] = "z"
        self.assertNotEqual(ss.scene_graph(s)["graph_sha256"], graph["graph_sha256"])

    def test_shot_plan_set_accepts_the_new_kinds(self):
        block = ss.shot_plan_set(spec11())
        radii = {e["id"]: e["radius"] for e in block["elements"]}
        self.assertEqual(radii["rail"], 2.0)
        self.assertEqual(radii["finial"], 0.35)
        self.assertEqual(radii["hoop"], 1.3)
        plan = sp.synthetic_plan()
        plan["set"] = block
        plan["set"]["caches"] = [{"id": "flame", "frames": [1, 240], "kind": "physical"}]
        for shot in plan["shots"]:
            for use in shot["uses"]:
                use["id"] = {"door": "arch", "window": "rail"}.get(use["id"], use["id"])
        plan["shots"][1]["uses"].append({"id": "brazier", "contribution": "shadow"})
        plan["performers"] = [{"id": "A", "position": [-1.0, 0.0]}, {"id": "B", "position": [1.0, 0.0]}]
        self.assertEqual(sp.validate_plan(plan)["errors"], [])

def poly_cone(radius, height, segments):
    return math.sin(2 * math.pi / segments) * segments / (2 * math.pi) * math.pi * radius * radius * height / 3.0


if __name__ == "__main__":
    unittest.main()

