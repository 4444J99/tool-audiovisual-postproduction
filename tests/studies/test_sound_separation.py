"""Tests for Wing 1 sound separation framework (Issues #70, #71, #72, #73, W01)."""
import json
import unittest
from pathlib import Path

from amp.composition.containers import MaterialRef, SourceSpan, StudyBinding, StudyContext, Timebase
from amp.studies.sound_separation import (
    build_source_event_palette,
    compare_voice_and_object_separation,
    compose_separated_instrument_study,
    run_sound_separation,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class SoundSeparationTests(unittest.TestCase):
    def setUp(self):
        self.timebase = Timebase(30, 1)
        self.span = SourceSpan(start=0, end=90, timebase=self.timebase)
        self.mat = MaterialRef(id="src-01", sha256="c" * 64, kind="audio")
        self.binding = StudyBinding(
            operator="amp.roomstretch.v1",
            materials=(self.mat,),
            span=self.span,
        )
        self.context = StudyContext(
            study_id="study-sound-sep-01",
            composition_id="comp-001",
        )

        prompts_path = REPO_ROOT / "compositions/comp-001/studies/w01/prompts.json"
        self.prompts_bundle = json.loads(prompts_path.read_text())

        palette_path = REPO_ROOT / "compositions/comp-001/studies/w01/palette.json"
        self.palette_data = json.loads(palette_path.read_text())

    def test_run_sound_separation_valid_prompts(self):
        # Filter out the negative fixture for the happy path
        clean_prompts = [p for p in self.prompts_bundle["prompts"] if not p.get("is_negative_fixture")]
        bundle = {"prompts": clean_prompts}

        result = run_sound_separation(self.binding, self.context, bundle)
        self.assertEqual(result.metadata["status"], "executed")
        self.assertEqual(result.metadata["wing"], "wing_1_sound")
        self.assertEqual(len(result.products), 3)  # speech, object, residual stems
        self.assertGreater(result.metadata["metrics"]["overall_snr_db"], 20.0)

    def test_offscreen_visual_mask_rejection(self):
        # Visual mask prompt targeting off-screen target MUST be rejected
        offscreen_prompt = {
            "prompt_id": "PRM-BAD-01",
            "prompt_type": "visual_mask",
            "target_body": "offscreen_intercom",
            "on_screen": False,
        }
        bundle = {"prompts": [offscreen_prompt]}

        with self.assertRaises(ValueError) as cm:
            run_sound_separation(self.binding, self.context, bundle)
        self.assertIn("Cannot invent visual mask for off-screen target", str(cm.exception))

    def test_compare_voice_and_object_separation(self):
        candidates = [
            {
                "candidate_id": "R01",
                "name": "sam-audio-base",
                "metrics": {
                    "speech_snr_db": 24.2,
                    "object_snr_db": 20.1,
                    "bleed_db": -29.4,
                    "artifact_score": 0.03,
                },
            },
            {
                "candidate_id": "R02",
                "name": "sam-audio-large",
                "metrics": {
                    "speech_snr_db": 27.5,
                    "object_snr_db": 22.8,
                    "bleed_db": -32.0,
                    "artifact_score": 0.02,
                },
            },
            {
                "candidate_id": "R03",
                "name": "sam-audio-stem-separator",
                "metrics": {
                    "speech_snr_db": 21.0,
                    "object_snr_db": 25.5,
                    "bleed_db": -26.0,
                    "artifact_score": 0.05,
                },
            },
        ]

        report = compare_voice_and_object_separation("BM-01", candidates)
        self.assertEqual(report["unit_id"], "BM-01")
        self.assertIn(report["winner"], ["R01", "R02", "R03"])
        self.assertEqual(len(report["ranking"]), 3)
        self.assertEqual(report["winner"], "R02")  # R02 had highest SNR and lowest bleed

    def test_build_source_event_palette(self):
        palette = build_source_event_palette(self.palette_data["entries"])
        self.assertEqual(palette["schema"], "amps.god_here.palette.v1")
        self.assertGreater(palette["total_stems"], 0)
        self.assertIn("speech", palette["index_by_category"])
        self.assertIn("impact", palette["index_by_category"])
        self.assertIn("BM-01", palette["index_by_unit"])

        # Invalid entry (negative SNR) rejection
        bad_entry = [
            {
                "stem_id": "BAD-01",
                "unit_id": "BM-01",
                "category": "speech",
                "acoustic_descriptors": {},
                "metrics": {"snr_db": -10.0},
                "provenance": {},
            }
        ]
        with self.assertRaises(ValueError):
            build_source_event_palette(bad_entry)

    def test_compose_separated_instrument_study(self):
        palette = build_source_event_palette(self.palette_data["entries"])
        phrase_spec = {
            "study_name": "phrase-test-01",
            "bpm": 120.0,
            "fps": 30,
            "events": [
                {
                    "stem_id": "STEM-VOICE-01",
                    "offset_frames": 0,
                    "gain": 1.0,
                    "pan": -0.2,
                    "envelope": {"attack_ms": 10.0, "release_ms": 40.0},
                },
                {
                    "stem_id": "STEM-IMPACT-01",
                    "offset_frames": 30,
                    "gain": 1.2,
                    "pan": 0.4,
                    "envelope": {"attack_ms": 5.0, "release_ms": 20.0},
                },
            ],
        }

        composition = compose_separated_instrument_study(palette, phrase_spec)
        self.assertEqual(composition["schema"], "amps.god_here.instrument_study.v1")
        self.assertEqual(composition["event_count"], 2)
        self.assertGreater(composition["total_frames"], 30)

        # Verify provenance trace
        ev0 = composition["scheduled_events"][0]
        self.assertEqual(ev0["stem_id"], "STEM-VOICE-01")
        self.assertEqual(ev0["provenance_trace"]["unit_id"], "BM-01")

        # Invalid gain / pan rejection
        bad_gain_spec = {
            "events": [
                {"stem_id": "STEM-VOICE-01", "offset_frames": 0, "gain": 3.5}  # Out of [0, 2]
            ]
        }
        with self.assertRaises(ValueError):
            compose_separated_instrument_study(palette, bad_gain_spec)

        # Unknown stem rejection
        bad_stem_spec = {
            "events": [
                {"stem_id": "UNKNOWN-STEM-999", "offset_frames": 0, "gain": 1.0}
            ]
        }
        with self.assertRaises(ValueError):
            compose_separated_instrument_study(palette, bad_stem_spec)


if __name__ == "__main__":
    unittest.main()
