"""Cold-review regression tests (issue 178): the burned frames must show the caption text, not only the caption frames."""
import unittest

from amp.studies import caption_burnin as cb
from amp.studies import delivery_qc as qc
from amp.studies import pipeline_chain as pc
from tests.test_delivery_qc import Base


class BurnedTextTests(Base):
    def burn_text(self, target, old, new):
        """Re-render the burn-in from an SRT with ``old`` replaced by ``new`` and write only the frame file."""
        ledger = self.jload(target, "plan/final-ledger.json")
        graph = self.jload(target, "set/scene-graph-summary.json")["graph_sha256"]
        srt = (target / "captions" / "captions.srt").read_text().replace(old, new)
        payload, _ = cb.burn(srt, ledger["fps"], ledger["total_frames"], pc.BURNIN_GEOMETRY, pc.burnin_segments(ledger, graph))
        (target / "captions" / "frames.y8").write_bytes(payload)

    def test_other_text_burned_on_the_frames_the_cues_name_is_caught_by_the_standalone_qc(self):
        target = self.copy()
        self.burn_text(target, "Hello now.", "Buy crypto!")
        report = self.derive(target)
        check = self.check(report, "captions.burned_frames")
        self.assertFalse(check["ok"])
        self.assertEqual(report["failed"], ["captions.burned_frames"])
        self.assertTrue(any("cue 1" in p and "text" in p for p in check["problems"]), check["problems"])
        self.assertEqual(check["text_mismatch_first_frame"], 12)

    def test_the_text_check_holds_when_the_burn_report_is_re_derived_for_the_forged_frames(self):
        target = self.copy()
        ledger = self.jload(target, "plan/final-ledger.json")
        graph = self.jload(target, "set/scene-graph-summary.json")["graph_sha256"]
        srt = (target / "captions" / "captions.srt").read_text().replace("Steady.", "Wire money.")
        payload, burn = cb.burn(srt, ledger["fps"], ledger["total_frames"], pc.BURNIN_GEOMETRY, pc.burnin_segments(ledger, graph))
        (target / "captions" / "frames.y8").write_bytes(payload)
        self.jsave(target, "captions/burnin-report.json", burn)
        self.reseal_everything(target, "honest")
        self.assertEqual(self.derive(target)["failed"], ["captions.burned_frames"])

    def test_a_caption_the_renderer_cannot_draw_fails_the_check_instead_of_raising(self):
        target = self.copy()
        srt = (target / "captions" / "captions.srt").read_text().replace("Steady.", "Stéady.")
        (target / "captions" / "captions.srt").write_text(srt)
        report = self.derive(target)
        check = self.check(report, "captions.burned_frames")
        self.assertFalse(check["ok"])
        self.assertTrue(any("cannot be re-rendered" in p for p in check["problems"]), check["problems"])

    def test_the_clean_chain_reports_no_text_mismatch(self):
        check = self.check(self.report, "captions.burned_frames")
        self.assertTrue(check["ok"])
        self.assertIsNone(check["text_mismatch_first_frame"])
        self.assertEqual(check["problems"], [])


if __name__ == "__main__":
    unittest.main()
