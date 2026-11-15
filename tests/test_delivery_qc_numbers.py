"""Cold-review regression tests (issue 179): non-finite report numbers and coerced frame counts."""
import json
import shutil
import unittest

from amp.studies import delivery_qc as qc
from amp.studies import pipeline_chain as pc
from tests.test_delivery_qc import Base


class NumberTests(Base):
    def fresh(self):
        """``copy`` names its target after the test, so a test that makes several copies clears the previous one."""
        shutil.rmtree(self.tmp.name + f"/copy-{self.id().rsplit('.', 1)[-1]}", ignore_errors=True)
        return self.copy()

    def write_raw(self, target, rel, value):
        (target / rel).write_text(json.dumps(value))        # json.dumps writes the NaN token the standard library also reads

    def test_nan_in_the_delivery_report_does_not_satisfy_the_agreement_check(self):
        for key in ("integrated_lufs", "true_peak_dbtp"):
            target = self.fresh()
            report = self.jload(target, "audio/delivery-report.json")
            report["after"][key] = float("nan")
            self.write_raw(target, "audio/delivery-report.json", report)
            result = self.derive(target)
            check = self.check(result, "loudness.report_agreement")
            self.assertFalse(check["ok"], key)
            self.assertTrue(any(key.split("_")[0] in p or "finite" in p for p in check["problems"]), check["problems"])

    def test_infinity_in_the_delivery_report_is_refused_too(self):
        target = self.fresh()
        report = self.jload(target, "audio/delivery-report.json")
        report["after"]["integrated_lufs"] = float("inf")
        self.write_raw(target, "audio/delivery-report.json", report)
        self.assertFalse(self.check(self.derive(target), "loudness.report_agreement")["ok"])

    def test_a_ledger_whose_frame_count_is_not_a_whole_number_is_refused_by_the_standalone_qc(self):
        for bad in (636.9, "636", True):
            target = self.fresh()
            ledger = self.jload(target, "plan/final-ledger.json")
            ledger["total_frames"] = bad
            self.jsave(target, "plan/final-ledger.json", ledger)
            with self.assertRaises(qc.QcError, msg=repr(bad)):
                self.derive(target)

    def test_the_clean_chain_still_passes(self):
        self.assertEqual(self.report["status"], "pass")


if __name__ == "__main__":
    unittest.main()
