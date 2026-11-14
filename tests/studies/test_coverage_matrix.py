import copy
import unittest

from amp.studies import coverage_matrix as cm
from amp.studies import shot_plan as sp


def inputs():
    return cm.synthetic_inputs()


def errs(plan, spec):
    return cm.validate_coverage(plan, spec)["errors"]


class ClassificationTests(unittest.TestCase):
    def test_size_and_angle_boundaries(self):
        self.assertEqual([cm.classify_size(w) for w in (0.5, 0.9, 0.91, 2.5, 2.6, 6.0, 6.1)],
                         ["close_up", "close_up", "medium", "medium", "wide", "wide", "establishing"])
        self.assertEqual([cm.classify_angle(a) for a in (0, 30, 31, 75, 76, 120, 121, 180)],
                         ["front", "front", "three_quarter", "three_quarter", "profile", "profile", "back", "back"])

    def test_facing_vectors(self):
        performers = {"A": {"id": "A", "position": [0.0, 0.0], "facing_deg": 90.0},
                      "B": {"id": "B", "position": [3.0, 4.0], "facing_toward": "A"}}
        ax, az = cm.facing_vector(performers["A"], performers)
        self.assertAlmostEqual(ax, 1.0)
        self.assertAlmostEqual(az, 0.0, places=6)
        bx, bz = cm.facing_vector(performers["B"], performers)
        self.assertAlmostEqual(bx, -0.6)
        self.assertAlmostEqual(bz, -0.8)
        self.assertIsNone(cm.facing_vector({"id": "C", "position": [0.0, 0.0]}, performers))
        self.assertIsNone(cm.facing_vector({"id": "C", "position": [0.0, 0.0], "facing_toward": "C"}, performers))


class CellTests(unittest.TestCase):
    def test_cells_follow_the_plan_geometry(self):
        plan, _ = inputs()
        rows = {(r["shot"], r["actor"]): r for r in cm.shot_cells(plan)}
        self.assertEqual(rows[("S01", "A")]["size"], "wide")
        self.assertEqual(rows[("S01", "B")]["size"], "establishing")
        self.assertEqual(rows[("S03", "B")]["size"], "medium")
        self.assertEqual(rows[("S03", "B")]["angle"], "three_quarter")
        self.assertEqual(len(rows), 4)                         # S02 and S03 list only B

    def test_a_longer_lens_tightens_the_size(self):
        plan, _ = inputs()
        plan["shots"][2]["camera"]["lens_mm"] = 300
        row = next(r for r in cm.shot_cells(plan) if r["shot"] == "S03")
        self.assertEqual(row["size"], "close_up")

    def test_over_the_shoulder_needs_a_near_foreground_actor_and_one_listed_actor(self):
        plan, _ = inputs()
        plan["shots"][1]["camera"]["position"] = [-1.4, 1.4, -0.9]     # about 0.8 m from A, looking at B
        plan["shots"][1]["camera"]["aim"] = [1.0, 1.1, 0.3]
        row = next(r for r in cm.shot_cells(plan) if r["shot"] == "S02")
        self.assertTrue(row["ots"])
        plan["shots"][1]["performers"] = ["A", "B"]
        self.assertFalse(any(r["ots"] for r in cm.shot_cells(plan) if r["shot"] == "S02"))

    def test_back_angle_when_the_camera_is_behind_the_actor(self):
        plan, _ = inputs()
        plan["performers"][1]["facing_deg"] = 180.0           # B faces -z, toward the S01 camera... turn B away
        plan["performers"][1].pop("facing_toward")
        plan["performers"][1]["facing_deg"] = 0.0             # B faces +z, away from the cameras at -z
        row = next(r for r in cm.shot_cells(plan) if r["shot"] == "S01" and r["actor"] == "B")
        self.assertEqual(row["angle"], "back")


class ValidationTests(unittest.TestCase):
    def test_demo_reports_both_deliberate_gaps_and_nothing_else(self):
        plan, spec = inputs()
        report = cm.validate_coverage(plan, spec)
        self.assertEqual(len(report["errors"]), 2, report["errors"])
        self.assertEqual({(g["actor"], g["size"]) for g in report["gaps"]}, {("B", "close_up"), ("A", "medium")})
        self.assertEqual(report["matrix"]["B"]["shots"], ["S01", "S02", "S03"])
        self.assertEqual(report["matrix"]["A"]["shots"], ["S01"])

    def test_met_requirements_are_clean(self):
        plan, spec = inputs()
        spec["required"] = [{"actor": "B", "size": "wide", "angle": "front"}, {"actor": "A"}]
        self.assertEqual(errs(plan, spec), [])

    def test_min_shots_counts_distinct_shots(self):
        plan, spec = inputs()
        spec["required"] = [{"actor": "B", "min_shots": 3}]
        self.assertEqual(errs(plan, spec), [])
        spec["required"] = [{"actor": "B", "min_shots": 4}]
        self.assertEqual(len(errs(plan, spec)), 1)

    def test_dialogue_line_needs_its_speaker_on_screen(self):
        plan, spec = inputs()
        spec["required"] = []
        plan["shots"][0]["performers"] = ["B"]                                  # S01 no longer shows A
        spec["lines"] = [{"id": "L9", "actor": "A", "frames": [100, 150]}]
        self.assertTrue(any("shows A" in e for e in errs(plan, spec)))
        spec["lines"][0]["offscreen_ok"] = True
        self.assertEqual(errs(plan, spec), [])
        spec["lines"] = [{"id": "L10", "actor": "A", "frames": [300, 340]}]
        self.assertTrue(any("no shot covers frames" in e for e in errs(plan, spec)))

    def test_room_tone_is_required_for_the_set(self):
        plan, spec = inputs()
        spec["required"] = []
        spec["room_tone"] = [{"set": plan["set"]["id"], "seconds": 12.0}]
        self.assertTrue(any("room tone" in e for e in errs(plan, spec)))
        spec["room_tone"] = [{"set": plan["set"]["id"], "seconds": 12.0}, {"set": plan["set"]["id"], "seconds": 20.0}]
        self.assertEqual(errs(plan, spec), [])
        spec["room_tone"] = []
        self.assertTrue(any("0 s of room tone" in e for e in errs(plan, spec)))

    def test_angle_requirements_need_facing(self):
        plan, spec = inputs()
        plan["performers"][0].pop("facing_toward")
        self.assertTrue(any("facing" in e for e in errs(plan, spec)))

    def test_bad_requirement_shapes_are_reported_together(self):
        plan, spec = inputs()
        spec["required"] = [{"actor": "Z"}, {"actor": "A", "size": "huge"}, {"actor": "A", "angle": "up"},
                            {"actor": "A", "min_shots": 0}, {"actor": "A", "ots": "yes"}]
        spec["lines"] = [{"id": "L", "actor": "Q", "frames": [3, 1]}, {"id": "L", "actor": "A", "frames": [1, 2]}]
        spec["room_tone"] = [{"set": 3}]
        spec["room_tone_min_s"] = -1
        found = errs(plan, spec)
        self.assertGreaterEqual(len(found), 8, found)

    def test_wrong_schema_invalid_plan_and_idle_actor(self):
        plan, spec = inputs()
        self.assertEqual(len(errs(plan, {"schema": "x"})), 1)
        bad = copy.deepcopy(plan)
        bad["shots"][0]["id"] = "bad id"
        self.assertTrue(errs(bad, spec)[0].startswith("shot plan:"))
        extra = copy.deepcopy(plan)
        extra["performers"].append({"id": "C", "position": [0.0, 3.0]})
        self.assertTrue(any("C appears in no shot" in w for w in cm.validate_coverage(extra, spec)["warnings"]))

    def test_table_lists_every_actor(self):
        plan, spec = inputs()
        table = cm.render_table(cm.validate_coverage(plan, spec))
        self.assertIn("| A | - | - | profile (S01) | - |", table)
        self.assertIn("three_quarter (S03)", table)


class SuggestionTests(unittest.TestCase):
    def test_suggested_shots_validate_and_close_every_gap(self):
        plan, spec = inputs()
        result = cm.suggest_shots(plan, spec)
        self.assertEqual(result["unplaced"], [])
        self.assertEqual(len(result["shots"]), 2)
        self.assertEqual(sp.validate_plan(result["plan"])["errors"], [])
        self.assertEqual(cm.validate_coverage(result["plan"], spec)["errors"], [])
        self.assertEqual(len(plan["shots"]), 3)                       # input untouched

    def test_suggestions_continue_the_timeline_and_never_stand_in_a_set_element_the_actor_is_outside(self):
        plan, spec = inputs()
        result = cm.suggest_shots(plan, spec)
        starts = [s["frames"]["start"] for s in result["shots"]]
        self.assertEqual(starts, [241, 289])
        for shot in result["shots"]:
            x, _, z = shot["camera"]["position"]
            for element in plan["set"]["elements"]:
                if element["id"] == "table":                          # both actors sit at the table, so it is allowed
                    continue
                self.assertGreaterEqual(((x - element["center"][0]) ** 2 + (z - element["center"][1]) ** 2) ** 0.5, element["radius"])
            self.assertEqual(shot["render"]["output"], f"renders/{shot['id']}/frame_####.exr")

    def test_suggestions_are_deterministic(self):
        plan, spec = inputs()
        self.assertEqual(cm.suggest_shots(plan, spec)["shots"], cm.suggest_shots(plan, spec)["shots"])

    def test_unplaceable_gap_is_reported_not_forced(self):
        plan, spec = inputs()
        plan["set"]["bounds"] = {"x": [-1.5, 1.5], "z": [-1.0, 1.0]}
        plan["set"]["elements"] = []
        plan["shots"] = [plan["shots"][0]]
        plan["shots"][0]["camera"]["position"] = [0.0, 1.6, -0.9]
        plan["shots"][0]["uses"] = []
        plan["performers"] = [{"id": "A", "position": [-0.5, 0.0], "facing_toward": "B"}, {"id": "B", "position": [0.5, 0.0], "facing_toward": "A"}]
        spec["required"] = [{"actor": "B", "size": "establishing", "angle": "back"}]
        spec["lines"] = []
        result = cm.suggest_shots(plan, spec)
        self.assertEqual(len(result["unplaced"]), 1)
        self.assertEqual(result["shots"], [])

    def test_invalid_inputs_return_the_reasons(self):
        plan, spec = inputs()
        spec["schema"] = "x"
        result = cm.suggest_shots(plan, spec)
        self.assertEqual(result["shots"], [])
        self.assertTrue(result["unplaced"])


if __name__ == "__main__":
    unittest.main()
