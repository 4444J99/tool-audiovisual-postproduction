"""Reverse-angle geometry on a synthetic room; no source media is read."""
import json
import unittest

import numpy as np

from amp.studies import reverse_angle as ra
from amp.studies.depth_world import reconstruct_room_proxy_geometry


def fixture():
    proxy = reconstruct_room_proxy_geometry(
        {"overall_depth_range_m": [1.0, 6.0]}, {"fx": 450.0, "fy": 450.0, "width": 320, "height": 180})
    box = ra.room_box_from_proxy(proxy, front_depth_m=1.0)
    source = ra.source_camera(320, 180, hfov_deg=100.0)
    plan = ra.plan_reverse_camera(source, (0.0, 0.0, 2.5), (0.2, 0.0, 4.2), standoff_m=1.1, lateral_m=0.4, box=box)
    return proxy, box, source, plan


class PlanTests(unittest.TestCase):
    def test_reverse_camera_is_same_side_upright_and_roughly_opposite(self):
        _, box, source, plan = fixture()
        checks = plan["checks"]
        self.assertTrue(checks["same_side_of_axis"])
        self.assertTrue(checks["eyeline_matches_source"])
        self.assertGreater(checks["optical_axis_separation_deg"], 165.0)
        camera = plan["_camera"]
        self.assertTrue(box.contains(camera.position))
        up = camera.matrix()[:, 1]
        self.assertAlmostEqual(float(up[1]), 1.0, places=9)
        # performer A sits on the reverse camera's optical axis
        forward = camera.matrix()[:, 2]
        to_a = np.array([0.0, 0.0, 2.5]) - np.array(camera.position)
        to_a /= np.linalg.norm(to_a)
        self.assertGreater(float(forward @ to_a), 0.9999)

    def test_rule_violations_are_refused(self):
        _, box, source, _ = fixture()
        on_axis = ra.source_camera(320, 180)
        with self.assertRaisesRegex(ValueError, "on the axis of action"):
            ra.plan_reverse_camera(on_axis, (0, 0, 2.0), (0, 0, 4.0), 1.0, 0.3, box)
        with self.assertRaisesRegex(ValueError, "outside the room"):
            ra.plan_reverse_camera(source, (0, 0, 2.5), (0.2, 0, 4.2), 3.0, 0.4, box)
        with self.assertRaisesRegex(ValueError, "coincide"):
            ra.plan_reverse_camera(source, (1, 0, 3), (1, 0, 3), 1.0, 0.3, box)
        with self.assertRaisesRegex(ValueError, "positive"):
            ra.plan_reverse_camera(source, (0, 0, 2.5), (0.2, 0, 4.2), 1.0, 0.0, box)

    def test_mirrored_source_gives_mirrored_side(self):
        _, box, _, _ = fixture()
        left = ra.plan_reverse_camera(ra.source_camera(320, 180, position=(0.5, 0, 0)), (0, 0, 2.5), (0.2, 0, 4.2), 1.1, 0.4, box)
        right = ra.plan_reverse_camera(ra.source_camera(320, 180, position=(-0.5, 0, 0)), (0, 0, 2.5), (0.2, 0, 4.2), 1.1, 0.4, box)
        self.assertGreater(left["_camera"].position[0], 0.2)
        self.assertLess(right["_camera"].position[0], 0.2 + 1e-9)
        self.assertNotAlmostEqual(left["_camera"].position[0], right["_camera"].position[0])

    def test_proxy_must_match_schema_and_bounds(self):
        proxy, *_ = fixture()
        bad = json.loads(json.dumps(proxy))
        bad["wall_planes"]["back"][3] = 9.0
        with self.assertRaisesRegex(ValueError, "disagrees"):
            ra.room_box_from_proxy(bad)
        with self.assertRaisesRegex(ValueError, "schema"):
            ra.room_box_from_proxy({"schema": "other"})


class ReconstructionTests(unittest.TestCase):
    def setUp(self):
        _, self.box, self.source, self.plan = fixture()
        self.target = self.plan["_camera"]
        self.frame = ra.render_proxy_room(self.box, self.source)
        self.truth = ra.render_proxy_room(self.box, self.target)

    def test_observed_pixels_match_ground_truth_render(self):
        rec = ra.reproject_observed(self.frame, self.source, self.target, self.box)
        seen = rec.status == ra.OBSERVED
        self.assertGreater(seen.mean(), 0.15)
        self.assertLess(seen.mean(), 0.95)
        # stretched pixels are low-quality by definition; compare the well-sampled ones
        good = seen & ~rec.stretched
        self.assertGreater(good.sum(), 1000)
        error = np.abs(rec.image[good] - self.truth[good])
        self.assertLess(float(error.mean()), 2.5)
        self.assertLess(float(np.percentile(error, 99)), 14.0)

    def test_front_wall_is_never_claimed_as_observed(self):
        rec = ra.reproject_observed(self.frame, self.source, self.target, self.box)
        front = rec.face == ra.FACE_NAMES.index("front")
        self.assertGreater(front.sum(), 500)
        self.assertFalse((rec.status[front] == ra.OBSERVED).any())
        ledger = ra.build_ledger(rec, plan=self.plan)
        self.assertEqual(ledger["faces"]["front"]["observed"], 0)
        self.assertEqual(ledger["faces"]["front"]["observed_fraction"], 0.0)

    def test_performer_mask_removes_pixels_and_hides_them_from_fill_stats(self):
        mask = np.zeros(self.frame.shape[:2], dtype=bool)
        mask[40:140, 120:200] = True
        plain = ra.reproject_observed(self.frame, self.source, self.target, self.box)
        masked = ra.reproject_observed(self.frame, self.source, self.target, self.box, subject_mask=mask)
        self.assertLess((masked.status == ra.OBSERVED).sum(), (plain.status == ra.OBSERVED).sum())
        self.assertGreater((masked.status == ra.SUBJECT_OCCLUDED).sum(), 0)
        # marked pixels never carry source colour
        self.assertTrue((masked.image[masked.status != ra.OBSERVED] == 0).all())

    def test_fill_is_labelled_placeholder_and_not_counted_as_observed(self):
        rec = ra.reproject_observed(self.frame, self.source, self.target, self.box)
        image, filled = ra.fill_unobserved(rec, "face_mean")
        self.assertEqual(int(filled.sum()), int((rec.status != ra.OBSERVED).sum()))
        self.assertTrue((image[filled].sum(axis=-1) > 0).all())
        ledger = ra.build_ledger(rec, fill_mode="face_mean")
        self.assertEqual(ledger["observed"], int((rec.status == ra.OBSERVED).sum()))
        self.assertIn("placeholder", ledger["fill"]["status"])
        none_image, none_filled = ra.fill_unobserved(rec, "none")
        self.assertFalse(none_filled.any())
        with self.assertRaises(ValueError):
            ra.fill_unobserved(rec, "inpaint")

    def test_identity_is_deterministic_and_responds_to_geometry(self):
        a = ra.build_ledger(ra.reproject_observed(self.frame, self.source, self.target, self.box))
        b = ra.build_ledger(ra.reproject_observed(self.frame, self.source, self.target, self.box))
        self.assertEqual(a, b)
        other = ra.plan_reverse_camera(self.source, (0, 0, 2.5), (0.2, 0, 4.2), 0.9, 0.9, self.box)["_camera"]
        c = ra.build_ledger(ra.reproject_observed(self.frame, self.source, other, self.box))
        self.assertNotEqual(a["geometry_identity_sha256"], c["geometry_identity_sha256"])

    def test_same_camera_reprojection_is_identity(self):
        rec = ra.reproject_observed(self.frame, self.source, self.source, self.box)
        self.assertTrue((rec.status == ra.OBSERVED).all())
        self.assertLess(float(np.abs(rec.image - self.frame).max()), 1e-3)

    def test_shape_and_origin_validation(self):
        with self.assertRaises(ValueError):
            ra.reproject_observed(self.frame[:10], self.source, self.target, self.box)
        with self.assertRaises(ValueError):
            ra.reproject_observed(self.frame, self.source, self.target, self.box, subject_mask=np.zeros((3, 3)))
        outside = ra.Camera((50.0, 0.0, 0.0), self.source.rotation, 250, 250, 320, 180)
        with self.assertRaisesRegex(ValueError, "inside the room"):
            ra.reproject_observed(self.frame, self.source, outside, self.box)


if __name__ == "__main__":
    unittest.main()
