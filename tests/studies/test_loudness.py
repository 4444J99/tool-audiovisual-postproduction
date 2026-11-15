import json
import math
import shutil
import subprocess
import unittest

import numpy as np
from scipy.signal import freqz

from amp.studies import loudness as ld

SR = 48000


def sine(freq, dbfs, seconds, sr=SR, phase=0.0, channels=2):
    t = np.arange(int(seconds * sr)) / sr
    x = 10 ** (dbfs / 20) * np.sin(2 * math.pi * freq * t + phase)
    return np.stack([x] * channels, axis=1) if channels > 1 else x


class KWeightingTests(unittest.TestCase):
    def test_48k_coefficients_equal_the_standards_table(self):
        (b1, a1), (b2, a2) = ld.k_weighting_coefficients(48000)
        np.testing.assert_allclose(b1, [1.53512485958697, -2.69169618940638, 1.19839281085285], atol=1e-11)
        np.testing.assert_allclose(a1, [1.0, -1.69065929318241, 0.73248077421585], atol=1e-11)
        np.testing.assert_allclose(b2, [1.0, -2.0, 1.0])
        np.testing.assert_allclose(a2, [1.0, -1.99004745483398, 0.99007225036621], atol=1e-11)

    def test_response_is_shelf_up_and_high_pass_down_at_every_rate(self):
        for sr in (32000, 44100, 48000, 88200, 96000):
            (b1, a1), (b2, a2) = ld.k_weighting_coefficients(sr)
            f = np.array([20.0, 1000.0, 10000.0])
            _, h1 = freqz(b1, a1, worN=f, fs=sr)
            _, h2 = freqz(b2, a2, worN=f, fs=sr)
            gain = 20 * np.log10(np.abs(h1 * h2))
            self.assertLess(gain[0], -10.0, sr)                   # RLB removes the lowest octaves
            self.assertAlmostEqual(gain[1], 0.69, delta=0.05, msg=sr)
            self.assertAlmostEqual(gain[2], 4.0, delta=0.4, msg=sr)   # shelf is nearly +4 dB at 10 kHz

    def test_bad_sample_rate_is_refused(self):
        with self.assertRaises(ld.LoudnessError):
            ld.k_weighting_coefficients(0)


class IntegratedLoudnessTests(unittest.TestCase):
    def test_ebu_stereo_1khz_at_minus_23_reads_minus_23(self):
        m = ld.measure(sine(1000, -23.0, 20), SR)
        self.assertAlmostEqual(m["integrated_lufs"], -23.0, delta=0.05)
        self.assertEqual(m["gated_blocks"], m["total_blocks"])

    def test_measurement_equals_the_closed_form(self):
        # L = -0.691 + 10 log10(sum_i G_i * ms_i), ms of a sine of peak A is A^2/2, K gain from the response.
        (b1, a1), (b2, a2) = ld.k_weighting_coefficients(SR)
        _, h = freqz(np.convolve(b1, b2), np.convolve(a1, a2), worN=[997.0], fs=SR)
        for dbfs in (-40.0, -23.0, -9.5):
            expected = -0.691 + 10 * math.log10(2 * (10 ** (dbfs / 20)) ** 2 / 2 * abs(h[0]) ** 2)
            self.assertAlmostEqual(ld.measure(sine(997, dbfs, 12), SR)["integrated_lufs"], expected, delta=0.01)

    def test_mono_reads_3_db_below_the_same_stereo(self):
        stereo = ld.measure(sine(1000, -20, 6), SR)["integrated_lufs"]
        mono = ld.measure(sine(1000, -20, 6, channels=1), SR)["integrated_lufs"]
        self.assertAlmostEqual(stereo - mono, 10 * math.log10(2), delta=0.005)

    def test_level_is_independent_of_sample_rate(self):
        readings = [ld.measure(sine(1000, -23, 10, sr), sr)["integrated_lufs"] for sr in (32000, 44100, 48000, 96000)]
        self.assertLess(max(readings) - min(readings), 0.05)

    def test_gain_moves_the_reading_by_exactly_that_gain(self):
        x = ld.synthetic_programme(SR)
        base = ld.measure(x, SR)
        for gain in (-12.0, -3.3, 6.0206):
            shifted = ld.measure(ld.apply_gain(x, gain), SR)
            self.assertAlmostEqual(shifted["integrated_lufs"] - base["integrated_lufs"], gain, delta=0.0015)
            self.assertAlmostEqual(shifted["true_peak_dbtp"] - base["true_peak_dbtp"], gain, delta=0.0015)
            self.assertAlmostEqual(shifted["loudness_range_lu"], base["loudness_range_lu"], delta=0.0015)

    def test_absolute_gate_ignores_everything_under_minus_70(self):
        loud = sine(1000, -20, 10)
        floor = sine(1000, -85, 10)
        alone = ld.measure(loud, SR)["integrated_lufs"]
        both = ld.measure(np.vstack([loud, floor]), SR)
        # blocks straddling the join are partly loud and stay in: a tenth of a unit, not the 25 LU the floor would cost
        self.assertAlmostEqual(both["integrated_lufs"], alone, delta=0.1)
        self.assertLess(both["gated_blocks"], both["total_blocks"])

    def test_programme_wholly_under_the_absolute_gate_is_undefined(self):
        under = ld.measure(sine(1000, -72, 6), SR)           # about -72 LUFS: every block is under -70
        self.assertIsNone(under["integrated_lufs"])
        self.assertEqual(under["gated_blocks"], 0)
        self.assertAlmostEqual(ld.measure(sine(1000, -68, 6), SR)["integrated_lufs"], -68.0, delta=0.02)

    def test_relative_gate_ignores_passages_over_10_lu_below_the_programme(self):
        loud = sine(1000, -20, 10)
        quiet = sine(1000, -50, 10)                       # above -70 LUFS, 30 LU under the loud part
        alone = ld.measure(loud, SR)["integrated_lufs"]
        both = ld.measure(np.vstack([loud, quiet]), SR)["integrated_lufs"]
        self.assertAlmostEqual(both, alone, delta=0.1)
        ungated = -0.691 + 10 * math.log10((10 ** ((alone + 0.691) / 10) + 10 ** ((alone - 30 + 0.691) / 10)) / 2)
        self.assertGreater(alone - ungated, 2.5)          # the gate matters: a plain mean would read 3 LU lower

    def test_a_passage_within_10_lu_is_kept(self):
        a, b = sine(1000, -20, 10), sine(1000, -27, 10)
        read = ld.measure(np.vstack([a, b]), SR)["integrated_lufs"]
        expected = ld.measure(a, SR)["integrated_lufs"] + 10 * math.log10((1 + 10 ** -0.7) / 2)
        self.assertAlmostEqual(read, expected, delta=0.03)

    def test_silence_has_no_integrated_loudness(self):
        m = ld.measure(np.zeros((SR * 2, 2)), SR)
        self.assertIsNone(m["integrated_lufs"])
        self.assertIsNone(m["loudness_range_lu"])
        self.assertEqual(m["true_peak_dbtp"], -240.0)
        self.assertIsNone(m["momentary_max_lufs"])                       # not -inf: strict JSON must hold it
        self.assertIsNone(m["short_term_max_lufs"])
        json.dumps(m, allow_nan=False)
        lfe_only = np.zeros((SR * 2, 6))
        lfe_only[:, 3] = sine(1000, -20, 2, channels=1)
        json.dumps(ld.measure(lfe_only, SR), allow_nan=False)

    def test_audio_under_one_block_is_refused(self):
        with self.assertRaisesRegex(ld.LoudnessError, "shorter"):
            ld.measure(sine(1000, -20, 0.3), SR)

    def test_bad_shapes_and_values_are_refused(self):
        with self.assertRaises(ld.LoudnessError):
            ld.measure(np.zeros((SR, 2, 2)), SR)
        bad = sine(1000, -20, 1)
        bad[10, 0] = np.nan
        with self.assertRaisesRegex(ld.LoudnessError, "NaN"):
            ld.measure(bad, SR)


class ChannelWeightTests(unittest.TestCase):
    def six(self, channel, dbfs=-20.0):
        x = np.zeros((SR * 4, 6))
        x[:, channel] = sine(1000, dbfs, 4, channels=1)
        return x

    def test_surround_reads_1_5_db_over_front_and_lfe_is_excluded(self):
        front = ld.measure(self.six(0), SR)["integrated_lufs"]
        centre = ld.measure(self.six(2), SR)["integrated_lufs"]
        surround = ld.measure(self.six(4), SR)["integrated_lufs"]
        self.assertAlmostEqual(front, centre, delta=1e-9)
        self.assertAlmostEqual(surround - front, 10 * math.log10(1.41), delta=0.002)
        self.assertIsNone(ld.measure(self.six(3), SR)["integrated_lufs"])

    def test_layouts_and_weights_are_validated(self):
        self.assertEqual(ld.channel_weights(2), (1.0, 1.0))
        self.assertEqual(ld.channel_weights(3, "1,1,0.5"), (1.0, 1.0, 0.5))
        for channels, layout in ((3, None), (2, "5.1"), (2, "1,x"), (2, "1,-1"), (2, "1,1,1")):
            with self.assertRaises(ld.LoudnessError, msg=(channels, layout)):
                ld.channel_weights(channels, layout)


class TruePeakTests(unittest.TestCase):
    def test_peak_between_samples_is_found(self):
        # A sine at a quarter of the sample rate, 45 degrees off, puts every sample at 0.7071 of the real peak.
        x = sine(SR / 4, 0.0, 1.0, phase=math.pi / 4, channels=1)
        m = ld.measure(np.stack([x, x], axis=1), SR)
        self.assertAlmostEqual(m["sample_peak_dbfs"], -3.01, delta=0.01)
        self.assertAlmostEqual(m["true_peak_dbtp"], 0.0, delta=0.15)

    def test_true_peak_never_under_sample_peak(self):
        rng = np.random.default_rng(3)
        x = rng.uniform(-0.9, 0.9, (SR, 2))
        sample, true = ld.true_peak(x, SR)
        self.assertGreaterEqual(true, sample)

    def test_high_rates_oversample_less_and_192k_not_at_all(self):
        x = sine(20000, -6, 1.0, 192000, phase=0.3)
        sample, true = ld.true_peak(x, 192000)
        self.assertEqual(sample, true)


class LoudnessRangeTests(unittest.TestCase):
    def test_steady_tone_has_no_range(self):
        self.assertAlmostEqual(ld.measure(sine(1000, -23, 20), SR)["loudness_range_lu"], 0.0, delta=0.01)

    def test_two_levels_give_their_difference(self):
        x = np.vstack([sine(1000, -20, 30), sine(1000, -30, 30)])
        m = ld.measure(x, SR)
        self.assertAlmostEqual(m["loudness_range_lu"], 10.0, delta=0.3)

    def test_under_three_seconds_has_no_range(self):
        self.assertIsNone(ld.measure(sine(1000, -23, 2), SR)["loudness_range_lu"])

    def test_range_gate_drops_passages_over_20_lu_down(self):
        x = np.vstack([sine(1000, -20, 30), sine(1000, -55, 30)])
        self.assertLess(ld.measure(x, SR)["loudness_range_lu"], 0.5)


class SpecTests(unittest.TestCase):
    def measurement(self, **kw):
        base = {"integrated_lufs": -23.0, "true_peak_dbtp": -3.0, "loudness_range_lu": 8.0}
        base.update(kw)
        return base

    def test_compliant_measurement_has_no_problems(self):
        for name in ld.PRESETS:
            target = ld.PRESETS[name]["integrated_lufs"]["target"]
            ceiling = ld.PRESETS[name]["true_peak_dbtp_max"]
            self.assertEqual(ld.check(self.measurement(integrated_lufs=target, true_peak_dbtp=ceiling - 0.1), name), [], name)

    def test_every_failure_is_reported_together(self):
        spec = {"integrated_lufs": {"target": -23, "tolerance": 0.5}, "true_peak_dbtp_max": -1, "loudness_range_max_lu": 7}
        problems = ld.check(self.measurement(integrated_lufs=-20.0, true_peak_dbtp=0.2), spec)
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("+3.00 LU" in p for p in problems))

    def test_tolerance_edge_is_inclusive(self):
        self.assertEqual(ld.check(self.measurement(integrated_lufs=-22.5), "ebu_r128"), [])
        self.assertEqual(len(ld.check(self.measurement(integrated_lufs=-22.49), "ebu_r128")), 1)

    def test_undefined_values_fail_rather_than_pass(self):
        self.assertEqual(len(ld.check(self.measurement(integrated_lufs=None), "ebu_r128")), 1)
        spec = {"integrated_lufs": {"target": -23}, "loudness_range_max_lu": 10}
        self.assertEqual(len(ld.check(self.measurement(loudness_range_lu=None), spec)), 1)

    def test_malformed_specs_are_refused(self):
        nan, inf = float("nan"), float("inf")
        for spec in ({"integrated_lufs": {"target": nan}, "true_peak_dbtp_max": nan},
                     {"integrated_lufs": {"target": -23, "tolerance": nan}},
                     {"integrated_lufs": {"target": -23}, "loudness_range_max_lu": inf},
                     {"integrated_lufs": {"target": inf}}):
            with self.assertRaises(ld.LoudnessError, msg=spec):
                ld.normalize_spec(spec)
        for spec in ("nope", {}, {"integrated_lufs": {"target": "x"}}, {"integrated_lufs": {"target": -23, "tolerance": -1}},
                     {"integrated_lufs": {"target": -23}, "true_peak_dbtp_max": "low"}, {"integrated_lufs": {"target": True}}):
            with self.assertRaises(ld.LoudnessError, msg=spec):
                ld.normalize_spec(spec)


class PlanGainTests(unittest.TestCase):
    def test_gain_reaches_the_target_when_the_peak_allows(self):
        plan = ld.plan_gain({"integrated_lufs": -30.0, "true_peak_dbtp": -12.0}, -23.0, -1.0)
        self.assertEqual((plan["ok"], plan["gain_db"], plan["capped"]), (True, 7.0, False))

    def test_refuse_names_the_peak_it_would_cause(self):
        plan = ld.plan_gain({"integrated_lufs": -30.0, "true_peak_dbtp": -3.0}, -23.0, -1.0)
        self.assertFalse(plan["ok"])
        self.assertIn("4.00 dBTP", plan["reason"])
        self.assertEqual(plan["gain_db"], 0.0)

    def test_cap_stops_at_the_ceiling_and_states_the_shortfall(self):
        plan = ld.plan_gain({"integrated_lufs": -30.0, "true_peak_dbtp": -3.0}, -23.0, -1.0, "cap")
        self.assertEqual((plan["ok"], plan["gain_db"], plan["capped"], plan["shortfall_lu"]), (True, 2.0, True, 5.0))

    def test_negative_gain_never_trips_the_ceiling(self):
        plan = ld.plan_gain({"integrated_lufs": -10.0, "true_peak_dbtp": -0.5}, -23.0, -1.0)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["gain_db"], -13.0)

    def test_silent_input_and_bad_mode(self):
        self.assertFalse(ld.plan_gain({"integrated_lufs": None, "true_peak_dbtp": -240.0}, -23.0, -1.0)["ok"])
        with self.assertRaises(ld.LoudnessError):
            ld.plan_gain({"integrated_lufs": -30.0, "true_peak_dbtp": -3.0}, -23.0, -1.0, "clip")

    def test_non_finite_targets_and_ceilings_are_refused(self):
        m = {"integrated_lufs": -30.0, "true_peak_dbtp": -12.0}
        for target, ceiling in ((float("nan"), -1.0), (-23.0, float("nan")), (float("inf"), None)):
            with self.assertRaises(ld.LoudnessError):
                ld.plan_gain(m, target, ceiling)

    def gate_straddling_programme(self):
        # Half at about -68 LUFS, half at about -72: the quiet half is under the absolute gate until gain lifts it.
        return np.vstack([sine(1000, -68, 10), sine(1000, -72, 10)])

    def test_a_plain_gain_misses_when_gain_changes_gate_membership(self):
        x = self.gate_straddling_programme()
        before = ld.measure(x, SR)
        plain = ld.plan_gain(before, -23.0, 0.0)
        after = ld.measure(ld.apply_gain(x, plain["gain_db"]), SR)["integrated_lufs"]
        self.assertLess(after, -23.5)                                     # the review's case: about 1.5 LU low

    def test_solve_gain_corrects_for_the_gates_and_lands_on_target(self):
        x = self.gate_straddling_programme()
        plan = ld.solve_gain(x, SR, -23.0, 0.0)
        self.assertTrue(plan["ok"], plan)
        self.assertGreater(plan["iterations"], 1)
        after = ld.measure(ld.apply_gain(x, plan["gain_db"]), SR)["integrated_lufs"]
        self.assertAlmostEqual(after, -23.0, delta=0.012)
        self.assertGreater(plan["gain_db"], plan["first_gain_db"])

    def test_solve_gain_says_so_when_it_cannot_converge(self):
        plan = ld.solve_gain(self.gate_straddling_programme(), SR, -23.0, 0.0, max_iterations=1)
        self.assertFalse(plan["ok"])
        self.assertIn("no single gain", plan["reason"])

    def test_solve_gain_respects_the_ceiling_after_correction(self):
        x = self.gate_straddling_programme()
        x[SR, :] = 10 ** (-9 / 20)                                       # a lone peak the corrected gain cannot afford
        self.assertFalse(ld.solve_gain(x, SR, -23.0, -1.0)["ok"])
        capped = ld.solve_gain(x, SR, -23.0, -1.0, "cap")
        self.assertTrue(capped["ok"] and capped["capped"])
        peak = ld.measure(ld.apply_gain(x, capped["gain_db"]), SR)["true_peak_dbtp"]
        self.assertAlmostEqual(peak, -1.0, delta=0.01)

    def test_solve_gain_does_not_refuse_on_a_summary_estimate_the_gates_overturn(self):
        # About -30 and -34 LUFS, target -68, ceiling -67: the summary says the peak lands near -66.4 (refuse), but
        # about -38 dB of gain drops the quiet half under the absolute gate and the real answer clears the ceiling.
        x = np.vstack([sine(1000, -30, 10), sine(1000, -34, 10)])
        summary = ld.plan_gain(ld.measure(x, SR), -68.0, -67.0)
        self.assertFalse(summary["ok"])
        plan = ld.solve_gain(x, SR, -68.0, -67.0)
        self.assertTrue(plan["ok"], plan)
        after = ld.measure(ld.apply_gain(x, plan["gain_db"]), SR)
        self.assertAlmostEqual(after["integrated_lufs"], -68.0, delta=0.012)
        self.assertLessEqual(after["true_peak_dbtp"], -67.0)
        self.assertFalse(plan["capped"])

    def test_solve_gain_does_not_cap_when_the_converged_gain_needs_no_cap(self):
        x = np.vstack([sine(1000, -30, 10), sine(1000, -34, 10)])
        plan = ld.solve_gain(x, SR, -68.0, -67.0, "cap")
        self.assertFalse(plan["capped"])
        self.assertEqual(plan["shortfall_lu"], 0.0)

    def test_solve_gain_validates_its_inputs(self):
        x = sine(1000, -30, 5)
        for args in ((float("nan"), -1.0, "refuse"), (-23.0, float("nan"), "refuse"), (-23.0, -1.0, "clip")):
            with self.assertRaises(ld.LoudnessError):
                ld.solve_gain(x, SR, *args)
        self.assertFalse(ld.solve_gain(np.zeros((SR * 2, 2)), SR, -23.0, -1.0)["ok"])

    def test_solve_gain_is_the_plain_plan_when_nothing_crosses_a_gate(self):
        x = sine(1000, -34.0, 10)
        plan = ld.solve_gain(x, SR, -23.0, -1.0)
        self.assertEqual(plan["iterations"], 1)
        self.assertAlmostEqual(plan["gain_db"], plan["first_gain_db"], delta=0.02)

    def test_planned_gain_lands_on_target_when_measured(self):
        x = ld.synthetic_programme(SR)
        before = ld.measure(x, SR)
        plan = ld.plan_gain(before, -23.0, 0.0)
        after = ld.measure(ld.apply_gain(x, plan["gain_db"]), SR)
        self.assertAlmostEqual(after["integrated_lufs"], -23.0, delta=0.005)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class AgainstFfmpegTests(unittest.TestCase):
    """An independent implementation (FFmpeg's ebur128 filter) must agree on invented programme material."""

    def ffmpeg_summary(self, x, tmp):
        from scipy.io import wavfile
        path = f"{tmp}/in.wav"
        wavfile.write(path, SR, x.astype(np.float32))
        run = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"],
                             capture_output=True, text=True)
        text = run.stderr.split("Summary:")[-1]
        def number(label, unit):
            line = [l for l in text.splitlines() if l.strip().startswith(label)][0]
            return float(line.split(":")[1].split(unit)[0])
        return number("I:", "LUFS"), number("LRA:", "LU"), number("Peak:", "dBFS")

    def test_integrated_range_and_true_peak_agree(self):
        import tempfile
        x = np.vstack([ld.synthetic_programme(SR, 1), 0.3 * ld.synthetic_programme(SR, 2), 2.0 * ld.synthetic_programme(SR, 3)])
        x = np.clip(x, -1, 1)
        with tempfile.TemporaryDirectory() as tmp:
            i, lra, peak = self.ffmpeg_summary(x, tmp)
        mine = ld.measure(x.astype(np.float32).astype(np.float64), SR)
        self.assertAlmostEqual(mine["integrated_lufs"], i, delta=0.05)
        self.assertAlmostEqual(mine["loudness_range_lu"], lra, delta=0.6)
        self.assertAlmostEqual(mine["true_peak_dbtp"], peak, delta=0.2)


if __name__ == "__main__":
    unittest.main()
