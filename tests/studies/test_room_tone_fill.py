import unittest

import numpy as np

from amp.studies import room_tone_fill as rt

SR = 16000


def dialogue():
    return rt.synthetic_dialogue(SR)


def fill(audio, **kw):
    filled, report = rt.fill_holes(audio, SR, **kw)
    return filled, rt.fill_report(audio, filled, SR, report)


class DetectionTests(unittest.TestCase):
    def test_holes_are_found_at_sample_accuracy(self):
        audio, meta = dialogue()
        holes = rt.find_holes(audio, SR)
        self.assertEqual(holes, [(int(a * SR), int(b * SR)) for a, b in meta["holes_s"]])

    def test_short_dropouts_are_not_holes(self):
        audio, _ = dialogue()
        audio[100:200] = 0.0                      # 6 ms
        self.assertEqual(len(rt.find_holes(audio, SR)), 2)
        self.assertEqual(len(rt.find_holes(audio, SR, min_hole_s=0.004)), 3)

    def test_stereo_hole_needs_silence_on_both_channels(self):
        audio, _ = dialogue()
        stereo = np.stack([audio, audio], axis=1)
        self.assertEqual(len(rt.find_holes(stereo, SR)), 2)
        stereo[30400:33600, 1] = 0.01             # right channel still carries something
        self.assertEqual(len(rt.find_holes(stereo, SR)), 1)

    def test_tone_regions_avoid_speech_and_holes(self):
        audio, meta = dialogue()
        regions = rt.find_tone_regions(audio, SR)
        self.assertGreaterEqual(len(regions), 3)
        for start, end in regions:
            for a, b in meta["bursts_s"]:
                core = (a + 0.15) * SR, (b - 0.15) * SR            # burst envelopes taper at the ends
                self.assertTrue(end <= core[0] or start >= core[1], (start, end, a, b))
            for hs, he in rt.find_holes(audio, SR):
                self.assertTrue(end <= hs or start >= he)

    def test_silence_only_audio_has_no_regions(self):
        self.assertEqual(rt.find_tone_regions(np.zeros(SR), SR), [])


class BedTests(unittest.TestCase):
    def test_bed_is_deterministic_and_seed_dependent(self):
        audio, _ = dialogue()
        regions = rt.find_tone_regions(audio, SR)
        a, _ = rt.build_bed(audio, regions, SR, SR, seed=3)
        b, _ = rt.build_bed(audio, regions, SR, SR, seed=3)
        c, _ = rt.build_bed(audio, regions, SR, SR, seed=4)
        self.assertTrue(np.array_equal(a, b))
        self.assertFalse(np.array_equal(a, c))

    def test_long_bed_reuses_regions_but_never_back_to_back(self):
        audio, _ = dialogue()
        regions = rt.find_tone_regions(audio, SR)
        bed, info = rt.build_bed(audio, regions, 12 * SR, SR, seed=5)
        self.assertEqual(len(bed), 12 * SR)
        self.assertGreater(len(info["pieces"]), len(regions) - 1)
        order = [p["region"] for p in info["pieces"]]
        self.assertTrue(all(x != y for x, y in zip(order, order[1:])))
        self.assertLess(info["longest_piece_s"], 1.2)          # no single stretch dominates

    def test_bed_level_and_splices_have_no_clicks(self):
        audio, _ = dialogue()
        regions = rt.find_tone_regions(audio, SR)
        bed, _ = rt.build_bed(audio, regions, 6 * SR, SR, seed=2)
        source = np.concatenate([audio[s:e] for s, e in regions])
        self.assertLess(abs(20 * np.log10(np.std(bed) / np.std(source))), 1.5)
        step = np.abs(np.diff(bed[:, 0]))
        self.assertLess(step.max(), 8 * np.std(np.diff(source)))   # a click would exceed several sigma of the source

    def test_splice_offset_finds_a_planted_match(self):
        rng = np.random.default_rng(1)
        head = rng.standard_normal(4000)
        tail = head[640:640 + 800].copy()
        self.assertEqual(rt.best_splice_offset(tail, head, 1000, decimate=4), 640)

    def test_no_usable_tone_is_an_error(self):
        audio, _ = dialogue()
        with self.assertRaises(rt.ToneError):
            rt.build_bed(audio, [], 100, SR)
        with self.assertRaises(rt.ToneError):
            rt.build_bed(audio, [(0, 100)], 100, SR)


class FillTests(unittest.TestCase):
    def test_every_hole_is_filled_and_everything_else_is_untouched(self):
        audio, _ = dialogue()
        filled, report = fill(audio, seed=1)
        self.assertEqual(report["holes_filled"], 2)
        self.assertEqual(report["remaining_digital_silence"], [])
        self.assertTrue(report["untouched_identical"])
        self.assertEqual(report["untouched_samples"] + report["patched_samples"], len(audio))
        self.assertEqual(rt.find_holes(filled, SR), [])

    def test_fill_matches_level_colour_and_edges(self):
        audio, _ = dialogue()
        _, report = fill(audio, seed=1)
        for check in report["checks"]:
            self.assertLess(check["level_error_db"], 1.5)
            self.assertLess(check["band_shape_deviation_db"], 4.0)
            self.assertLess(check["edge_level_deviation_db"], 5.0)

    def test_speech_is_never_faded_or_altered(self):
        audio, meta = dialogue()
        filled, _ = fill(audio, seed=1)
        for a, b in meta["bursts_s"]:
            core = slice(int((a + 0.2) * SR), int((b - 0.2) * SR))
            if any(int(hs) < core.stop and core.start < int(he) for hs, he in rt.find_holes(audio, SR)):
                continue
            self.assertTrue(np.array_equal(audio[core], filled[core]))

    def test_fill_is_deterministic_per_seed_and_holes_differ_from_each_other(self):
        audio, _ = dialogue()
        a, ra = rt.fill_holes(audio, SR, seed=9)
        b, rb = rt.fill_holes(audio, SR, seed=9)
        c, _ = rt.fill_holes(audio, SR, seed=10)
        self.assertTrue(np.array_equal(a, b))
        self.assertEqual(ra["output_sha256"], rb["output_sha256"])
        self.assertFalse(np.array_equal(a, c))
        first = a[int(1.95 * SR):int(2.05 * SR)]
        second = a[int(3.6 * SR):int(3.7 * SR)]
        self.assertFalse(np.allclose(first, second))

    def test_input_without_holes_is_returned_unchanged(self):
        audio, _ = dialogue()
        audio[audio == 0.0] = 1e-3
        filled, report = rt.fill_holes(audio, SR)
        self.assertEqual(report["holes_filled"], 0)
        self.assertTrue(np.array_equal(audio, filled))

    def test_holes_without_any_tone_are_refused(self):
        audio = np.zeros(SR)
        audio[1000:2000] = 0.5
        with self.assertRaises(rt.ToneError):
            rt.fill_holes(audio, SR)

    def test_stereo_keeps_channel_shape_and_fills_both(self):
        audio, _ = dialogue()
        stereo = np.stack([audio, 0.8 * audio], axis=1)
        filled, report = fill(stereo, seed=2)
        self.assertEqual(filled.shape, stereo.shape)
        self.assertEqual(report["remaining_digital_silence"], [])
        hole = slice(int(3.6 * SR), int(4.3 * SR))
        ratio = np.std(filled[hole, 1]) / np.std(filled[hole, 0])
        self.assertAlmostEqual(ratio, 0.8, delta=0.05)     # inter-channel relationship carried into the fill

    def test_mono_vector_stays_a_vector(self):
        audio, _ = dialogue()
        filled, _ = rt.fill_holes(audio, SR)
        self.assertEqual(filled.shape, audio.shape)

    def test_bad_shapes_and_rates_are_refused(self):
        with self.assertRaises(rt.ToneError):
            rt.fill_holes(np.zeros((2, 2, 2)), SR)
        with self.assertRaises(rt.ToneError):
            rt.fill_holes(np.zeros(100), 0)

    def test_hole_next_to_speech_does_not_fade_the_speech(self):
        audio, _ = dialogue()
        # cut a hole that starts right at the end of the first burst's loud part
        start = int(1.2 * SR)
        audio[start:start + int(0.3 * SR)] = 0.0
        filled, report = fill(audio, seed=1)
        self.assertTrue(report["untouched_identical"])
        hole = next(h for h in report["holes"] if abs(h["start_s"] - 1.2) < 1e-3)
        self.assertEqual(hole["crossfade_before_s"], 0.0)   # the neighbour is speech, so no crossfade over it
        self.assertTrue(np.array_equal(audio[start - 800:start], filled[start - 800:start]))


if __name__ == "__main__":
    unittest.main()
