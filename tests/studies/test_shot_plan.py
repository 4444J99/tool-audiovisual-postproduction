import copy
import unittest

from amp.studies import shot_plan as sp


def plan():
    return sp.synthetic_plan()


def errors(p):
    return sp.validate_plan(p)["errors"]


class ShotPlanValidationTests(unittest.TestCase):
    def test_synthetic_plan_is_valid_and_ledger_is_contiguous(self):
        p = plan()
        self.assertEqual(sp.validate_plan(p), {"errors": [], "warnings": []})
        ledger = sp.build_ledger(p)
        cursor = 0
        for row in ledger["shots"]:
            self.assertEqual(row["edit_in"], cursor)
            cursor = row["edit_out"]
        self.assertEqual(ledger["total_frames"], cursor)
        self.assertEqual(ledger["total_seconds"], 22.5)
        s02 = ledger["shots"][1]
        self.assertEqual(s02["render_frames"], [1, 162])
        self.assertEqual(s02["render_frame_count"], 162)
        self.assertEqual(sp.build_ledger(p), ledger)

    def test_hash_changes_with_any_plan_edit(self):
        p = plan()
        q = copy.deepcopy(p)
        q["shots"][0]["camera"]["lens_mm"] = 28
        self.assertNotEqual(sp.plan_hash(p), sp.plan_hash(q))

    def test_duplicate_and_malformed_ids(self):
        p = plan()
        p["shots"][1]["id"] = "S01"
        self.assertTrue(any("duplicate id" in e for e in errors(p)))
        p = plan()
        p["shots"][1]["id"] = "shot two"
        self.assertTrue(any("id must match" in e for e in errors(p)))

    def test_frame_range_and_handles(self):
        p = plan()
        p["shots"][0]["frames"]["end"] = 0
        self.assertTrue(any("integer start <= end" in e for e in errors(p)))
        p = plan()
        p["shots"][1]["frames"]["handle_in"] = -1
        self.assertTrue(any("handle_in" in e for e in errors(p)))

    def test_camera_outside_bounds_and_degenerate_aim(self):
        p = plan()
        p["shots"][0]["camera"]["position"] = [9.0, 1.6, -4.0]
        self.assertTrue(any("outside the set bounds" in e for e in errors(p)))
        p = plan()
        p["shots"][0]["camera"]["aim"] = [0.0, 0.0, -4.0]
        self.assertTrue(any("no horizontal direction" in e for e in errors(p)))
        p = plan()
        p["shots"][0]["camera"]["lens_mm"] = 2
        self.assertTrue(any("lens_mm" in e for e in errors(p)))

    def test_performer_outside_frustum_is_refused(self):
        p = plan()
        p["shots"][2]["camera"]["aim"] = [-4.0, 1.2, -3.0 + 0.5]
        found = errors(p)
        self.assertTrue(any("performer B" in e and "outside" in e for e in found), found)

    def test_axis_crossing_needs_declared_reason(self):
        p = plan()
        p["shots"][2]["camera"]["position"] = [0.5, 1.3, 3.0]
        p["shots"][2]["camera"]["aim"] = [1.0, 1.2, 0.3]
        found = errors(p)
        self.assertTrue(any("crosses the axis of action" in e for e in found), found)
        p["shots"][2]["axis"]["crossing"] = "deliberate cut-away to the far side"
        self.assertFalse(any("crosses the axis" in e for e in errors(p)))

    def test_camera_on_axis_is_refused(self):
        p = plan()
        # point on the line through A(-1,-0.2) and B(1,0.3): z = 0.25*x + 0.05
        p["shots"][0]["camera"]["position"] = [3.0, 1.6, 0.25 * 3.0 + 0.05]
        p["shots"][0]["camera"]["aim"] = [0.0, 1.0, 0.0]
        self.assertTrue(any("stands on the axis" in e for e in errors(p)))

    def test_visible_element_must_be_in_frame_but_offscreen_contribution_may_not(self):
        p = plan()
        p["shots"][0]["uses"].append({"id": "window", "contribution": "visible"})
        self.assertTrue(any("window is declared visible" in e for e in errors(p)))
        p["shots"][0]["uses"][-1]["contribution"] = "reflection"
        self.assertEqual(errors(p), [])

    def test_unknown_use_and_bad_contribution(self):
        p = plan()
        p["shots"][0]["uses"].append({"id": "chandelier"})
        self.assertTrue(any("neither a set element nor a cache" in e for e in errors(p)))
        p = plan()
        p["shots"][0]["uses"][0]["contribution"] = "vibes"
        self.assertTrue(any("contribution must be one of" in e for e in errors(p)))

    def test_added_element_with_no_shot_is_refused(self):
        p = plan()
        p["set"]["elements"].append({"id": "balcony", "center": [3.0, 3.0], "added": True})
        self.assertTrue(any("added element balcony is used by no shot" in e for e in errors(p)))
        p["set"]["elements"][-1]["added"] = False
        self.assertEqual(errors(p), [])

    def test_cache_coverage_includes_handles(self):
        p = plan()
        p["shots"][2]["frames"]["handle_out"] = 3
        found = errors(p)
        self.assertTrue(any("cache flame covers frames 1-240" in e and "needs 85-243" in e for e in found), found)

    def test_cache_strategies(self):
        p = plan()
        p["shots"][2]["frames"]["handle_out"] = 3
        p["shots"][2]["cache_strategy"] = {"flame": {"kind": "reviewed_loop"}}
        self.assertTrue(any("needs reviewed_by" in e for e in errors(p)))
        p["shots"][2]["cache_strategy"] = {"flame": {"kind": "reviewed_loop", "reviewed_by": "operator"}}
        self.assertEqual(errors(p), [])
        p["shots"][2]["cache_strategy"] = {"flame": {"kind": "rebake", "frames": [1, 241]}}
        self.assertTrue(any("rebake range" in e for e in errors(p)))
        p["shots"][2]["cache_strategy"] = {"flame": {"kind": "rebake", "frames": [1, 300]}}
        self.assertEqual(errors(p), [])
        p["shots"][2]["cache_strategy"] = {"flame": {"kind": "loop"}}
        self.assertTrue(any("must be one of rebake, reviewed_loop" in e for e in errors(p)))

    def test_rendered_receipt_requires_exact_count_and_hash(self):
        p = plan()
        p["shots"][1]["receipt"] = {"status": "rendered", "frames_rendered": 150, "sha256": "0" * 64}
        self.assertTrue(any("frames_rendered == 162" in e for e in errors(p)))
        p["shots"][1]["receipt"] = {"status": "rendered", "frames_rendered": 162, "sha256": "zz"}
        self.assertTrue(any("SHA-256" in e for e in errors(p)))
        p["shots"][1]["receipt"] = {"status": "rendered", "frames_rendered": 162, "sha256": "ab" * 32}
        self.assertEqual(errors(p), [])
        p["shots"][1]["receipt"] = {"status": "done"}
        self.assertTrue(any("receipt.status" in e for e in errors(p)))

    def test_render_settings_are_required(self):
        p = plan()
        del p["shots"][0]["render"]["threads"]
        self.assertTrue(any("render needs" in e for e in errors(p)))

    def test_duration_target_warning_and_wrong_schema(self):
        p = plan()
        p["target_seconds"] = [30, 40]
        self.assertTrue(any("outside the target" in w for w in sp.validate_plan(p)["warnings"]))
        p["schema"] = "other"
        self.assertTrue(any("schema must be" in e for e in errors(p)))

    def test_build_ledger_raises_every_error(self):
        p = plan()
        p["shots"][0]["render"] = {}
        p["shots"][1]["id"] = "S01"
        with self.assertRaises(sp.PlanError) as ctx:
            sp.build_ledger(p)
        self.assertGreaterEqual(len(ctx.exception.errors), 2)

    def test_edl_matches_ledger(self):
        p = plan()
        edl = sp.build_edl(p)
        self.assertIn("003  S03", edl)
        self.assertIn("00:00:16:06 00:00:22:12", edl)

    def test_pair_order_does_not_hide_an_axis_crossing(self):
        p = plan()
        p["shots"][2]["camera"]["position"] = [0.5, 1.3, 3.0]
        p["shots"][2]["axis"]["pair"] = ["B", "A"]
        self.assertTrue(any("crosses the axis of action" in e for e in errors(p)))

    def test_malformed_collections_give_errors_not_exceptions(self):
        for path, value in ((("shots", 0, "performers"), 5), (("shots", 0, "uses"), "x"), (("performers",), {}),
                            (("set", "elements"), "x"), (("shots", 0, "action"), 42)):
            p = plan()
            target = p
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            self.assertTrue(errors(p), path)
        self.assertTrue(sp.validate_plan([])["errors"])
        p = plan()
        p["shots"][2]["frames"]["handle_out"] = 3
        p["shots"][2]["cache_strategy"] = {"flame": "loop"}
        self.assertTrue(any("must be an object" in e for e in errors(p)))

    def test_element_and_cache_ids_must_differ(self):
        p = plan()
        p["set"]["elements"].append({"id": "flame", "center": [1.0, 0.3]})
        self.assertTrue(any("both an element and a cache" in e for e in errors(p)))

    def test_fps_must_be_a_positive_integer(self):
        for fps in (0.5, 0, True, "24"):
            p = plan()
            p["fps"] = fps
            self.assertTrue(any("fps must be" in e for e in errors(p)), fps)


if __name__ == "__main__":
    unittest.main()
