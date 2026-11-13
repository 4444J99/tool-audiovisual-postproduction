"""Tests for Wing 2 Surface & Atlas framework (Issues #74, #75, #76, #77, W02)."""
import json
import unittest
from pathlib import Path

from amp.composition.containers import MaterialRef, SourceSpan, StudyResult
from amp.studies.surface_atlas import (
    apply_persistent_surface_edit,
    promote_accepted_surface,
    run_material_transfers,
    validate_canonical_surface,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class SurfaceAtlasTests(unittest.TestCase):
    def setUp(self):
        self.valid_atlas_spec = {
            "atlas_id": "ATLAS-FACE-01",
            "span": {"start": 155, "end": 210},
            "uv_spec": {
                "bounds": [0.0, 0.0, 1.0, 1.0],
                "max_distortion": 0.04,
            },
            "occlusion": {
                "dimensions": [1920, 1080],
                "mask_coverage": 0.25,
                "occluded_frames": [180, 181, 182],
            },
            "bijection": {
                "is_bijective": True,
                "bijective_coverage": 0.98,
            },
        }

        transfers_path = REPO_ROOT / "compositions/comp-001/studies/w02/transfers.json"
        self.transfers_data = json.loads(transfers_path.read_text())

    def test_validate_canonical_surface_valid(self):
        errors = validate_canonical_surface(self.valid_atlas_spec)
        self.assertEqual(errors, ())

    def test_validate_canonical_surface_span_and_uv_errors(self):
        # Inverted span
        bad_span = dict(self.valid_atlas_spec)
        bad_span["span"] = {"start": 200, "end": 100}
        errors_span = validate_canonical_surface(bad_span)
        self.assertTrue(any("strictly greater than start" in e for e in errors_span))

        # UV coordinate out of [0, 1]
        bad_uv = dict(self.valid_atlas_spec)
        bad_uv["uv_spec"] = {"bounds": [-0.1, 0.0, 1.0, 1.0]}
        errors_uv = validate_canonical_surface(bad_uv)
        self.assertTrue(any("UV coordinate must be finite in [0.0, 1.0]" in e for e in errors_uv))

        # Non-bijective mapping
        bad_bijection = dict(self.valid_atlas_spec)
        bad_bijection["bijection"] = {"is_bijective": False}
        errors_bij = validate_canonical_surface(bad_bijection)
        self.assertTrue(any("violates bijection" in e for e in errors_bij))

    def test_apply_persistent_surface_edit_with_occlusion_gating(self):
        edit_layer = {
            "layer_id": "LAYER-VEIN-01",
            "edit_type": "displacement",
            "intensity": 0.85,
        }

        result = apply_persistent_surface_edit(self.valid_atlas_spec, edit_layer)
        self.assertEqual(result["schema"], "amps.god_here.surface_edit.v1")
        self.assertEqual(result["layer_id"], "LAYER-VEIN-01")
        self.assertEqual(result["occlusion_gated_frames"], 3)  # Frames 180, 181, 182
        self.assertEqual(result["total_frames"], 55)
        self.assertTrue(result["stable"])
        self.assertLess(result["temporal_residual_mean"], 0.05)

    def test_run_material_transfers_3_canonical_transfers(self):
        transfers = self.transfers_data["transfers"]
        self.assertEqual(len(transfers), 3)

        report = run_material_transfers(self.valid_atlas_spec, transfers)
        self.assertEqual(report["schema"], "amps.god_here.material_transfers_report.v1")
        self.assertEqual(report["total_transfers"], 3)
        self.assertTrue(report["all_qualified"])

        for t in report["transfers"]:
            self.assertTrue(t["qualified"])
            self.assertGreaterEqual(t["edge_coherence"], t["edge_coherence_threshold"])
            self.assertLessEqual(t["temporal_flicker"], t["temporal_flicker_max"])

    def test_promote_accepted_surface_success(self):
        span = SourceSpan(start=155, end=210)
        prod = MaterialRef(id="cand-surface-01", sha256="d" * 64, kind="material")
        study_res = StudyResult(
            study_id="study-surface-01",
            operator="amp.roomstretch.v1",
            span=span,
            products=(prod,),
        )

        review_decision = {
            "decision": "accepted",
            "finite_samples_verified": True,
            "qualification_passed": True,
            "reviewer": "human_curator",
        }

        promoted = promote_accepted_surface(study_res, review_decision)
        self.assertEqual(promoted["schema"], "amps.god_here.promoted_surface.v1")
        self.assertEqual(promoted["material"]["kind"], "material")
        self.assertEqual(promoted["material"]["metadata"]["status"], "promoted")
        self.assertIn("mat-surface-", promoted["material"]["id"])

    def test_promote_rejected_surface_fails_closed(self):
        span = SourceSpan(start=155, end=210)
        study_res = StudyResult(
            study_id="study-surface-rejected",
            operator="amp.roomstretch.v1",
            span=span,
        )

        bad_decision = {
            "decision": "rejected",
            "finite_samples_verified": False,
            "qualification_passed": False,
        }

        with self.assertRaises(ValueError) as cm:
            promote_accepted_surface(study_res, bad_decision)
        self.assertIn("Cannot promote unaccepted surface study", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
