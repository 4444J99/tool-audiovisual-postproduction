"""Cold-review regression tests for the delivery QC stage (issues 177 and 180): coverage of the dialogue, a detector the
captions stage does not share, and a report that says which checks cannot fail independently."""
import json
import unittest

import numpy as np

from amp.studies import caption_burnin as cb
from amp.studies import captions as cap
from amp.studies import delivery_qc as qc
from amp.studies import pipeline_chain as pc
from tests.test_delivery_qc import Base

INDEPENDENT_FIELD = "independence"
# Checks whose clean-chain pass shows the stage did what it was written to do, not that the delivery is right.
CANNOT_FAIL_INDEPENDENTLY = {"timebase.timecode", "captions.inside_dialogue", "captions.max_length", "captions.reading_rate",
                             "captions.inside_edit", "captions.burned_frames", "audio.within_edit", "loudness.spec",
                             "loudness.report_agreement"}


class CoverageAndIndependenceTests(Base):
    def drop_cue(self, target, index):
        """Delete one cue from the SRT, WebVTT, caption report and (re-rendered) burn-in, then re-derive and re-seal."""
        ledger = self.jload(target, "plan/final-ledger.json")
        graph = self.jload(target, "set/scene-graph-summary.json")["graph_sha256"]
        cues = [c for c in cap.parse((target / "captions" / "captions.srt").read_text(), ledger["fps"]) if c["index"] != index]
        for number, cue in enumerate(cues, 1):
            cue["index"] = number
        srt, vtt = cap.render_srt(cues), cap.render_vtt(cues)
        (target / "captions" / "captions.srt").write_bytes(srt.encode())
        (target / "captions" / "captions.vtt").write_bytes(vtt.encode())
        report = self.jload(target, "captions/captions-report.json")
        report["cues"] = [r for r in report["cues"] if r["index"] != index]
        for number, row in enumerate(report["cues"], 1):
            row["index"] = number
        report["srt_sha256"], report["vtt_sha256"] = cap.sha256_text(srt), cap.sha256_text(vtt)
        self.jsave(target, "captions/captions-report.json", report)
        payload, burn = cb.burn(srt, ledger["fps"], ledger["total_frames"], pc.BURNIN_GEOMETRY, pc.burnin_segments(ledger, graph))
        (target / "captions" / "frames.y8").write_bytes(payload)
        self.jsave(target, "captions/burnin-report.json", burn)
        self.reseal_everything(target, "honest")

    def test_a_dialogue_span_with_no_caption_is_named_even_when_everything_is_re_derived_and_re_sealed(self):
        target = self.copy()
        self.drop_cue(target, 2)
        report = self.derive(target)
        self.assertEqual(report["status"], "fail")
        self.assertIn("captions.covers_dialogue", report["failed"])
        self.assertTrue(any("2.3" in p or "2.9" in p for p in self.check(report, "captions.covers_dialogue")["problems"]))
        self.assertTrue(pc.verify_chain(target) == [] and any(p.startswith("qc ") for p in pc.cross_checks(target)))

    def test_the_clean_chain_covers_every_span_and_places_every_cue_under_both_detectors(self):
        covers = self.check(self.report, "captions.covers_dialogue")
        placed = self.check(self.report, "captions.placement_independent")
        self.assertTrue(covers["ok"] and placed["ok"])
        self.assertEqual(len(covers["spans"]["placement"]), 4)
        self.assertEqual(len(covers["spans"]["independent"]), 4)
        self.assertGreaterEqual(min(placed["overlaps"]), qc.DEFAULT_SPEC["independent_overlap_min"])

    def test_the_independent_detector_shares_no_parameter_with_the_captions_stage_or_the_qc_spec(self):
        audio = np.zeros(16000 * 4)
        audio[16000:32000] = 0.3 * np.sin(np.arange(16000) * 0.2)
        spans = qc.independent_spans(audio, 16000)
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0][0] / 16000, 1.0, delta=0.05)
        self.assertAlmostEqual(spans[0][1] / 16000, 2.0, delta=0.05)
        loose = {"vad": {"margin_db": 0.001}}
        self.assertEqual(qc.independent_spans(audio, 16000), spans)
        self.assertNotIn("vad", qc.INDEPENDENT_DETECTOR)
        self.assertEqual(qc.normalize_spec(loose)["vad"]["margin_db"], 0.001)

    def test_digital_silence_and_flat_noise_give_the_independent_detector_nothing(self):
        self.assertEqual(qc.independent_spans(np.zeros(16000 * 2), 16000), [])
        self.assertEqual(qc.independent_spans(np.full(16000 * 2, 0.1), 16000), [])
        self.assertEqual(qc.independent_spans(np.zeros(10), 16000), [])

    def test_a_loosened_vad_cannot_hide_a_cue_the_independent_detector_does_not_hear(self):
        target = self.copy()
        self.shift_cue(target, 2, 40)          # cue 2 moves 1.67 s into silence
        loose = {"vad": {"margin_db": 0.001, "merge_gap_s": 5.0}, "span_tolerance_frames": 1000}
        report = self.derive(target, loose)
        self.assertIn("captions.placement_independent", report["failed"])

    def test_every_check_states_its_independence_and_the_report_lists_the_ones_that_cannot_fail_independently(self):
        for check in self.report["checks"]:
            self.assertIn(INDEPENDENT_FIELD, check, check["id"])
            self.assertIn(check[INDEPENDENT_FIELD]["class"], qc.INDEPENDENCE_CLASSES)
            self.assertTrue(check[INDEPENDENT_FIELD]["note"])
        self.assertEqual(set(self.report["cannot_fail_independently"]), CANNOT_FAIL_INDEPENDENTLY)
        self.assertNotIn("captions.placement_independent", self.report["cannot_fail_independently"])
        self.assertIn("captions.covers_dialogue", [c["id"] for c in self.report["checks"]])

    def test_the_shot_timecode_round_trip_is_declared_structural_and_the_stamp_half_is_not(self):
        timecode = self.check(self.report, "timebase.timecode")
        self.assertEqual(timecode[INDEPENDENT_FIELD]["class"], "partly-structural")
        self.assertIn("identity", timecode[INDEPENDENT_FIELD]["note"])

    def test_the_audio_length_check_says_it_cannot_fail_on_short_audio(self):
        within = self.check(self.report, "audio.within_edit")
        self.assertEqual(within[INDEPENDENT_FIELD]["class"], "weak")
        self.assertLess(within["audio_seconds"], within["edit_seconds"])
        self.assertIn("shorter", within[INDEPENDENT_FIELD]["note"])

    def test_the_loudness_spec_check_says_the_spec_comes_from_the_artefact_it_judges(self):
        loud = self.check(self.report, "loudness.spec")
        self.assertEqual(loud[INDEPENDENT_FIELD]["class"], "self-declared")

    def test_the_report_with_the_new_fields_still_round_trips_byte_for_byte(self):
        again = qc.dumps(self.derive(self.out)).encode()
        self.assertEqual(again, (self.out / "qc" / "qc-report.json").read_bytes())
        self.assertEqual(json.loads(again)["schema"], qc.SCHEMA)


if __name__ == "__main__":
    unittest.main()
