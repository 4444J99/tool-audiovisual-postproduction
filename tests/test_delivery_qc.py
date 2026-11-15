import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import numpy as np

from amp.studies import caption_burnin as cb
from amp.studies import captions as cap
from amp.studies import delivery_qc as qc
from amp.studies import pipeline_chain as pc

ROOT = Path(__file__).resolve().parents[1]
PIPE = ROOT / "tools" / "av_pipeline.py"
QC = ROOT / "tools" / "delivery_qc.py"
CHECK_IDS = ["timebase.frame_rate", "timebase.edit_contiguous", "timebase.edit_length", "timebase.burnin_segments", "timebase.shot_lengths",
             "timebase.set_caches", "timebase.timecode", "captions.inside_dialogue", "captions.max_length", "captions.reading_rate",
             "captions.inside_edit", "captions.burned_frames", "audio.within_edit", "loudness.spec", "loudness.report_agreement"]


def run(tool, *args):
    return subprocess.run([sys.executable, str(tool), *args], capture_output=True, text=True, cwd=ROOT)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "a"
        cls.manifest = pc.run_chain(cls.out)
        cls.report = json.loads((cls.out / "qc" / "qc-report.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self):
        target = Path(self.tmp.name) / f"copy-{self.id().rsplit('.', 1)[-1]}"
        shutil.copytree(self.out, target)
        return target

    @staticmethod
    def derive(target, spec=None):
        return qc.derive(target, pc.LOCATIONS, spec)

    def check(self, report, cid):
        return next(c for c in report["checks"] if c["id"] == cid)

    def jload(self, target, rel):
        return json.loads((target / rel).read_text())

    def jsave(self, target, rel, value):
        (target / rel).write_text(pc.dumps(value))

    def reseal_everything(self, target, qc_report=None):
        """A forger who fixes every hash in the manifest to agree with the files on disk, the burn-in chain included.
        ``qc_report`` None leaves ``qc/qc-report.json`` as it was; "honest" re-derives it from the forged files."""
        if qc_report == "honest":
            (target / "qc" / "qc-report.json").write_text(qc.dumps(self.derive(target)))
        manifest = json.loads((target / "pipeline-manifest.json").read_text())
        produced = {}
        for entry in manifest["stages"]:
            for label in entry["inputs"]:
                entry["inputs"][label] = produced[label]
            for label in entry["outputs"]:
                entry["outputs"][label] = pc.sha256_file(target / pc.LOCATIONS[label])
                produced[label] = entry["outputs"][label]
        manifest["chain_sha256"] = pc.chain_hash(manifest)
        (target / "pipeline-manifest.json").write_text(json.dumps(manifest))

    def shift_cue(self, target, index, frames, burn=True):
        """Move one cue by ``frames`` in the SRT, WebVTT, caption report and (re-rendered) burned frames.
        ``burn=False`` leaves the burned frames alone, for shifts the burn-in writer itself refuses."""
        ledger = self.jload(target, "plan/final-ledger.json")
        graph = self.jload(target, "set/scene-graph-summary.json")["graph_sha256"]
        fps = ledger["fps"]
        cues = cap.parse((target / "captions" / "captions.srt").read_text(), fps)
        rate = cap.parse_fps(fps)
        for c in cues:
            if c["index"] == index:
                c["start"] += frames
                c["end"] += frames
                c["start_ms"], c["end_ms"] = cap.ms_of(c["start"], rate), cap.ms_of(c["end"], rate)
        srt, vtt = cap.render_srt(cues), cap.render_vtt(cues)
        (target / "captions" / "captions.srt").write_bytes(srt.encode())
        (target / "captions" / "captions.vtt").write_bytes(vtt.encode())
        report = self.jload(target, "captions/captions-report.json")
        for row in report["cues"]:
            if row["index"] == index:
                row["start"] += frames
                row["end"] += frames
                row["start_ms"], row["end_ms"] = cap.ms_of(row["start"], rate), cap.ms_of(row["end"], rate)
        report["srt_sha256"], report["vtt_sha256"] = cap.sha256_text(srt), cap.sha256_text(vtt)
        self.jsave(target, "captions/captions-report.json", report)
        if not burn:
            return
        payload, burn = cb.burn(srt, fps, ledger["total_frames"], pc.BURNIN_GEOMETRY, pc.burnin_segments(ledger, graph))
        (target / "captions" / "frames.y8").write_bytes(payload)
        self.jsave(target, "captions/burnin-report.json", burn)


class CleanChainTests(Base):
    def test_a_clean_chain_passes_every_named_check(self):
        self.assertEqual(self.report["schema"], qc.SCHEMA)
        self.assertEqual(self.report["status"], "pass")
        self.assertEqual(self.report["failed"], [])
        self.assertEqual([c["id"] for c in self.report["checks"]], CHECK_IDS)
        self.assertTrue(all(c["ok"] for c in self.report["checks"]))

    def test_the_report_is_deterministic_and_equals_the_file_on_disk(self):
        again = self.derive(self.out)
        self.assertEqual(again, self.report)
        self.assertEqual(qc.dumps(again).encode(), (self.out / "qc" / "qc-report.json").read_bytes())

    def test_the_report_is_a_hashed_output_of_the_last_stage_and_pins_what_it_read(self):
        stage = self.manifest["stages"][-1]
        self.assertEqual(stage["stage"], "qc")
        self.assertEqual(stage["outputs"], {"qc-report": pc.sha256_file(self.out / "qc" / "qc-report.json")})
        self.assertEqual(stage["inputs"], self.report["inputs"])
        self.assertEqual(sorted(stage["inputs"]), sorted(qc.INPUT_LABELS))
        self.assertEqual(self.manifest["chain_sha256"], pc.chain_hash(self.manifest))
        self.assertEqual(stage["checks"], len(CHECK_IDS))

    def test_the_measured_facts_are_in_the_report(self):
        loud = self.check(self.report, "loudness.spec")
        self.assertEqual(loud["spec"]["name"], "ebu_r128")
        self.assertAlmostEqual(loud["measured"]["integrated_lufs"], -23.0, delta=0.5)
        self.assertGreater(loud["true_peak_headroom_db"], 0)
        spans = self.check(self.report, "captions.inside_dialogue")["spans"]
        bursts = [(0.3, 1.5), (2.2, 3.1), (4.7, 6.0), (6.9, 7.6)]
        self.assertEqual(len(spans), 4)
        for span, (a, b) in zip(spans, bursts):
            self.assertGreaterEqual(span["start_s"], a - 0.05)
            self.assertLessEqual(span["end_s"], b + 0.05)
        tc = self.check(self.report, "timebase.timecode")
        self.assertEqual(tc["shots"][0], {"shot": "S01", "in": "00:00:00:00", "out": "00:00:10:00"})
        self.assertEqual(tc["shots"][-1]["out"], "00:00:26:12")
        self.assertEqual(self.check(self.report, "timebase.frame_rate")["rate"], "24")

    def test_the_stage_is_verified_by_the_chain(self):
        self.assertEqual(pc.verify_chain(self.out), [])
        self.assertEqual(pc.cross_checks(self.out), [])
        self.assertEqual(pc.qc_checks(self.out), [])

    def test_flipping_one_byte_of_the_report_is_named_by_the_manifest_and_by_re_derivation(self):
        target = self.copy()
        path = target / "qc" / "qc-report.json"
        path.write_text(path.read_text().replace('"status": "pass"', '"status": "fail"'))
        self.assertTrue(any("output qc-report on disk" in p for p in pc.verify_chain(target)))
        self.assertTrue(any("not what the sealed artefacts derive" in p for p in pc.qc_checks(target)))


class SpanTests(unittest.TestCase):
    vad = qc.DEFAULT_SPEC["vad"]

    def tone(self, parts, rate=16000):
        out = np.zeros(int(rate * 3))
        rng = np.random.default_rng(1)
        out += rng.standard_normal(len(out)) * 0.002
        for a, b in parts:
            t = np.arange(int(a * rate), int(b * rate)) / rate
            out[int(a * rate):int(b * rate)] += 0.2 * np.sin(2 * np.pi * 200 * t)
        return out

    def test_two_bursts_give_two_spans_near_their_edges(self):
        spans = qc.dialogue_spans(self.tone([(0.5, 1.0), (2.0, 2.6)]), 16000, self.vad)
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0][0] / 16000, 0.5, delta=0.04)
        self.assertAlmostEqual(spans[1][1] / 16000, 2.6, delta=0.04)

    def test_digital_silence_and_empty_input_have_no_spans(self):
        self.assertEqual(qc.dialogue_spans(np.zeros(16000), 16000, self.vad), [])
        self.assertEqual(qc.dialogue_spans(np.zeros(10), 16000, self.vad), [])

    def test_a_blip_shorter_than_the_minimum_is_dropped_and_close_runs_merge(self):
        self.assertEqual(qc.dialogue_spans(self.tone([(1.0, 1.1)]), 16000, self.vad), [])
        spans = qc.dialogue_spans(self.tone([(0.5, 1.0), (1.05, 1.5)]), 16000, self.vad)
        self.assertEqual(len(spans), 1)

    def test_timecode_round_trips_and_uses_the_nominal_rate_label(self):
        for rate in (Fraction(24), Fraction(25), Fraction(30), Fraction(24000, 1001)):
            for frame in (0, 1, 23, 24, 25, 1439, 86400 * 24 + 7):
                self.assertEqual(qc.frame_of_timecode(qc.timecode(frame, rate), rate), frame)
        self.assertEqual(qc.timecode(24 * 3600 + 1, Fraction(24)), "01:00:00:01")
        self.assertEqual(qc.timecode(25 * 61 + 3, Fraction(25)), "00:01:01:03")

    def test_spec_validation_names_each_bad_field(self):
        for bad in ({"max_cue_frames": 0}, {"max_cue_frames": 1.5}, {"max_cps": -1}, {"span_tolerance_frames": -1}, {"report_tolerance": "x"},
                    {"vad": {"margin_db": 0}}, {"vad": {"nope": 1}}, {"name": ""}, {"surprise": 1}):
            with self.assertRaises(qc.QcError, msg=bad):
                qc.normalize_spec(bad)
        self.assertEqual(qc.normalize_spec({"max_cue_frames": 9})["max_cue_frames"], 9)
        self.assertEqual(qc.normalize_spec({"vad": {"margin_db": 5}})["vad"]["frame_s"], 0.02)

    def test_words_in_spans_fill_each_span_and_refuse_what_cannot_be_read(self):
        words = cap.words_in_spans(["Hello now.", "Go."], [(1.0, 2.0), (3.0, 3.5)], 17.0)
        self.assertEqual([w["text"] for w in words], ["Hello", "now.", "Go."])
        self.assertGreaterEqual(words[0]["start"], 1.0)
        self.assertLessEqual(words[1]["end"], 2.0)
        self.assertGreaterEqual(words[2]["start"], 3.0)
        self.assertLessEqual(words[2]["end"], 3.5)
        with self.assertRaises(cap.CaptionError):
            cap.words_in_spans(["A very long sentence for a short span."], [(0.0, 0.5)], 17.0)
        with self.assertRaises(cap.CaptionError):
            cap.words_in_spans(["One."], [(0.0, 1.0), (2.0, 3.0)], 17.0)


class LimitTests(Base):
    def test_a_cue_over_the_frame_limit_is_named(self):
        report = self.derive(self.out, {"max_cue_frames": 12})
        found = self.check(report, "captions.max_length")
        self.assertFalse(found["ok"])
        self.assertTrue(any("cue 1" in p and "over 12" in p for p in found["problems"]), found)
        self.assertEqual(report["status"], "fail")

    def test_a_reading_rate_over_the_limit_is_named(self):
        found = self.check(self.derive(self.out, {"max_cps": 8.0}), "captions.reading_rate")
        self.assertFalse(found["ok"])
        self.assertTrue(any("over 8.0" in p for p in found["problems"]), found)

    def test_the_reading_rate_is_measured_from_the_caption_file_not_the_report(self):
        target = self.copy()
        report = self.jload(target, "captions/captions-report.json")
        report["problems"] = []
        report["cues"][0]["lines"] = ["short"]
        self.jsave(target, "captions/captions-report.json", report)
        rates = self.check(self.derive(target), "captions.reading_rate")["rates"]
        self.assertEqual(rates, self.check(self.report, "captions.reading_rate")["rates"])

    def test_a_spec_too_tight_for_the_audio_refuses_the_chain(self):
        with self.assertRaises(pc.ChainError) as caught:
            pc.run_chain(Path(self.tmp.name) / "tight", qc_spec={"max_cue_frames": 8})
        self.assertTrue(any("captions.max_length" in e for e in caught.exception.errors), caught.exception.errors)
        self.assertFalse((Path(self.tmp.name) / "tight" / "pipeline-manifest.json").exists())
        self.assertEqual(json.loads((Path(self.tmp.name) / "tight" / "qc" / "qc-report.json").read_text())["status"], "fail")

    def test_audio_with_no_dialogue_span_refuses_before_any_caption_is_made(self):
        with self.assertRaises(pc.ChainError) as caught:
            pc.run_chain(Path(self.tmp.name) / "nospan", qc_spec={"vad": {"margin_db": 60.0}})
        self.assertTrue(any("no speech spans" in e for e in caught.exception.errors), caught.exception.errors)

    def test_a_looser_declared_spec_is_recorded_and_verified_only_when_declared(self):
        loose = {"name": "loose", "max_cue_frames": 400}
        chain = Path(self.tmp.name) / "loose"
        pc.run_chain(chain, qc_spec=loose)
        self.assertEqual(pc.qc_checks(chain, loose), [])
        self.assertTrue(any("different QC spec" in p for p in pc.qc_checks(chain)), pc.qc_checks(chain))


class ForgeryTests(Base):
    def test_a_cue_moved_out_of_the_dialogue_is_caught_even_when_every_hash_and_the_burn_in_are_fixed(self):
        target = self.copy()
        self.shift_cue(target, 2, 40)
        self.reseal_everything(target)
        self.assertEqual(pc.verify_chain(target), [])
        self.assertEqual(pc.burnin_checks(target), [], "the caption and burn-in stages are internally consistent")
        problems = pc.qc_checks(target)
        self.assertTrue(any("different captions-srt" in p for p in problems), problems)
        self.assertTrue(any("captions.inside_dialogue fails" in p and "cue 2" in p for p in problems), problems)

    def test_an_honestly_re_derived_report_of_a_forged_chain_fails_instead_of_passing(self):
        target = self.copy()
        self.shift_cue(target, 2, 40)
        self.reseal_everything(target, "honest")
        report = json.loads((target / "qc" / "qc-report.json").read_text())
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failed"], ["captions.inside_dialogue"])
        self.assertTrue(any("captions.inside_dialogue fails" in p for p in pc.qc_checks(target)))

    def test_a_pass_report_copied_from_the_clean_chain_does_not_survive(self):
        target = self.copy()
        self.shift_cue(target, 3, 30)
        self.reseal_everything(target)
        self.assertEqual(json.loads((target / "qc" / "qc-report.json").read_text())["status"], "pass")
        self.assertTrue(pc.qc_checks(target))

    def test_a_loosened_spec_written_into_the_report_is_refused_unless_declared(self):
        target = self.copy()
        self.shift_cue(target, 2, 40)
        loose = {"name": "forged", "span_tolerance_frames": 500}
        (target / "qc" / "qc-report.json").write_text(qc.dumps(self.derive(target, loose)))
        self.reseal_everything(target)
        self.assertEqual(json.loads((target / "qc" / "qc-report.json").read_text())["status"], "pass")
        problems = pc.qc_checks(target)
        self.assertTrue(any("different QC spec than the one declared" in p for p in problems), problems)
        self.assertEqual(pc.qc_checks(target, loose), [], "declaring the loosened spec is the operator's decision, as for the loudness spec")

    def test_a_replaced_loudness_spec_in_the_delivery_report_is_caught_by_the_re_measurement(self):
        target = self.copy()
        sr, audio = pc.read_pcm16(target / "audio" / "dialogue-delivered.wav")
        louder = audio * 4.0
        (target / "audio" / "dialogue-delivered.wav").write_bytes(pc.pcm16_bytes(louder, sr))
        self.reseal_everything(target)
        report = self.derive(target)
        self.assertFalse(self.check(report, "loudness.spec")["ok"])
        self.assertFalse(self.check(report, "loudness.report_agreement")["ok"])
        self.assertIn("loudness.spec", report["failed"])
        self.assertTrue(any("loudness.spec fails" in p for p in pc.qc_checks(target)))

    def test_delivery_report_describing_another_file_is_a_disagreement(self):
        target = self.copy()
        report = self.jload(target, "audio/delivery-report.json")
        report["after"]["integrated_lufs"] = -20.0
        self.jsave(target, "audio/delivery-report.json", report)
        found = self.check(self.derive(target), "loudness.report_agreement")
        self.assertFalse(found["ok"])
        self.assertTrue(any("integrated loudness" in p for p in found["problems"]), found)

    def test_frame_rate_disagreement_between_any_two_artefacts_is_named(self):
        for rel, edit in (("captions/burnin-report.json", lambda d: d["params"].__setitem__("fps", "25")),
                          ("colour/layer-plan.json", lambda d: d["comp"].__setitem__("fps", 30)),
                          ("captions/captions-report.json", lambda d: d.__setitem__("fps", "24000/1001"))):
            target = Path(self.tmp.name) / ("fps-" + rel.replace("/", "-"))
            shutil.copytree(self.out, target)
            data = self.jload(target, rel)
            edit(data)
            self.jsave(target, rel, data)
            found = self.check(self.derive(target), "timebase.frame_rate")
            self.assertFalse(found["ok"], rel)

    def test_plan_fps_disagreement_is_named(self):
        target = self.copy()
        plan = self.jload(target, "plan/shot-plan.json")
        plan["fps"] = 25
        self.jsave(target, "plan/shot-plan.json", plan)
        self.assertFalse(self.check(self.derive(target), "timebase.frame_rate")["ok"])

    def test_edit_length_and_contiguity_are_judged_across_ledger_colour_plan_and_frame_file(self):
        target = self.copy()
        colour = self.jload(target, "colour/layer-plan.json")
        colour["comp"]["frames"] += 1
        self.jsave(target, "colour/layer-plan.json", colour)
        self.assertFalse(self.check(self.derive(target), "timebase.edit_length")["ok"])
        target = Path(self.tmp.name) / "short-frames"
        shutil.copytree(self.out, target)
        path = target / "captions" / "frames.y8"
        path.write_bytes(path.read_bytes()[:-1])
        self.assertFalse(self.check(self.derive(target), "timebase.edit_length")["ok"])
        target = Path(self.tmp.name) / "gap"
        shutil.copytree(self.out, target)
        ledger = self.jload(target, "plan/final-ledger.json")
        ledger["shots"][1]["edit_in"] += 2
        self.jsave(target, "plan/final-ledger.json", ledger)
        self.assertFalse(self.check(self.derive(target), "timebase.edit_contiguous")["ok"])

    def test_a_shot_whose_render_range_is_not_its_plan_range_plus_handles_is_named(self):
        target = self.copy()
        ledger = self.jload(target, "plan/final-ledger.json")
        ledger["shots"][1]["render_frame_count"] -= 1
        self.jsave(target, "plan/final-ledger.json", ledger)
        found = self.check(self.derive(target), "timebase.shot_lengths")
        self.assertFalse(found["ok"])
        self.assertTrue(any("S02" in p and "handles" in p for p in found["problems"]), found)

    def test_a_colour_plate_layer_that_is_not_the_shot_range_is_named(self):
        target = self.copy()
        colour = self.jload(target, "colour/layer-plan.json")
        next(l for l in colour["layers"] if l["id"] == "shot-s03")["frames"]["end"] -= 5
        self.jsave(target, "colour/layer-plan.json", colour)
        found = self.check(self.derive(target), "timebase.shot_lengths")
        self.assertTrue(any("S03" in p and "colour plate layer" in p for p in found["problems"]), found)

    def test_a_cache_that_does_not_cover_a_shot_with_its_handles_is_named(self):
        target = self.copy()
        spec = self.jload(target, "set/set-spec.json")
        spec["caches"][0]["frames"] = [7, 240]
        self.jsave(target, "set/set-spec.json", spec)
        found = self.check(self.derive(target), "timebase.set_caches")
        self.assertFalse(found["ok"])
        self.assertTrue(any("S02" in p and "handles included" in p for p in found["problems"]), found)

    def test_a_caption_stamp_that_stands_for_another_frame_than_the_reports_say_is_named(self):
        target = self.copy()
        report = self.jload(target, "captions/captions-report.json")
        report["cues"][0]["start"] += 1
        self.jsave(target, "captions/captions-report.json", report)
        found = self.check(self.derive(target), "timebase.timecode")
        self.assertFalse(found["ok"])
        self.assertTrue(any("cue 1" in p and "caption report" in p for p in found["problems"]), found)

    def test_a_webvtt_file_with_other_stamps_than_the_srt_is_named(self):
        target = self.copy()
        path = target / "captions" / "captions.vtt"
        path.write_text(path.read_text().replace("00:00.500", "00:00.750").replace("00:00:00.500", "00:00:00.750"))
        found = self.check(self.derive(target), "timebase.timecode")
        self.assertFalse(found["ok"], found)

    def test_a_cue_past_the_end_of_the_edit_is_named(self):
        target = self.copy()
        self.shift_cue(target, 4, 700, burn=False)
        found = self.check(self.derive(target), "captions.inside_edit")
        self.assertFalse(found["ok"])
        self.assertTrue(any("cue 4" in p and "outside the edit" in p for p in found["problems"]), found)

    def test_overlapping_cues_are_named(self):
        target = self.copy()
        self.shift_cue(target, 2, -40, burn=False)
        found = self.check(self.derive(target), "captions.inside_edit")
        self.assertFalse(found["ok"])

    def test_burned_text_on_a_frame_no_cue_names_is_caught(self):
        target = self.copy()
        report = self.jload(target, "captions/burnin-report.json")
        size = report["frame_bytes"]
        data = bytearray((target / "captions" / "frames.y8").read_bytes())
        data[100 * size + 10] ^= 0xFF                           # frame 100 carries no cue
        (target / "captions" / "frames.y8").write_bytes(bytes(data))
        found = self.check(self.derive(target), "captions.burned_frames")
        self.assertFalse(found["ok"])
        self.assertEqual(found["first_difference"], 100)

    def test_a_cue_whose_frames_were_burned_blank_is_caught(self):
        target = self.copy()
        report = self.jload(target, "captions/burnin-report.json")
        size, first = report["frame_bytes"], report["cues"][0]["start"]
        blank = b"".join(cb.frame_stream(report["params"], []))
        data = bytearray((target / "captions" / "frames.y8").read_bytes())
        data[first * size:(first + 1) * size] = blank[first * size:(first + 1) * size]
        (target / "captions" / "frames.y8").write_bytes(bytes(data))
        found = self.check(self.derive(target), "captions.burned_frames")
        self.assertFalse(found["ok"])
        self.assertEqual(found["first_difference"], first)

    def test_an_audio_track_longer_than_the_edit_is_named(self):
        target = self.copy()
        ledger = self.jload(target, "plan/final-ledger.json")
        sr, audio = pc.read_pcm16(target / "audio" / "dialogue-delivered.wav")
        (target / "audio" / "dialogue-delivered.wav").write_bytes(pc.pcm16_bytes(np.tile(audio, 5), sr))
        self.assertLess(ledger["total_seconds"], len(audio) * 5 / sr)
        self.assertFalse(self.check(self.derive(target), "audio.within_edit")["ok"])

    def test_unreadable_artefacts_raise_qc_error_not_a_traceback(self):
        target = self.copy()
        (target / "audio" / "dialogue-delivered.wav").write_bytes(b"RIFFnot a wav")
        with self.assertRaises(qc.QcError):
            self.derive(target)
        self.assertTrue(any(p.startswith("qc:") for p in pc.qc_checks(target)))
        target = Path(self.tmp.name) / "missing"
        shutil.copytree(self.out, target)
        (target / "captions" / "captions.vtt").unlink()
        with self.assertRaises(qc.QcError):
            self.derive(target)
        target = Path(self.tmp.name) / "badjson"
        shutil.copytree(self.out, target)
        (target / "plan" / "final-ledger.json").write_text("{")
        with self.assertRaises(qc.QcError):
            self.derive(target)

    def test_a_ledger_missing_a_field_the_checks_read_raises_qc_error(self):
        target = self.copy()
        ledger = self.jload(target, "plan/final-ledger.json")
        del ledger["shots"][1]["render_frames"]
        self.jsave(target, "plan/final-ledger.json", ledger)
        with self.assertRaises(qc.QcError):
            self.derive(target)
        self.assertTrue(any(p.startswith("qc:") for p in pc.qc_checks(target)))

    def test_the_manifest_cannot_drop_the_qc_stage_or_its_inputs(self):
        target = self.copy()
        manifest = json.loads((target / "pipeline-manifest.json").read_text())
        manifest["stages"] = [s for s in manifest["stages"] if s["stage"] != "qc"]
        manifest["chain_sha256"] = pc.chain_hash(manifest)
        (target / "pipeline-manifest.json").write_text(json.dumps(manifest))
        self.assertTrue(any("missing qc" in p for p in pc.verify_chain(target)))
        target = Path(self.tmp.name) / "fewer-qc-inputs"
        shutil.copytree(self.out, target)
        manifest = json.loads((target / "pipeline-manifest.json").read_text())
        del manifest["stages"][-1]["inputs"]["suggested-plan"]
        manifest["chain_sha256"] = pc.chain_hash(manifest)
        (target / "pipeline-manifest.json").write_text(json.dumps(manifest))
        self.assertTrue(any("qc: inputs are" in p for p in pc.verify_chain(target)))


class CliTests(Base):
    def test_check_prints_the_report_and_exits_zero_on_a_clean_chain(self):
        ran = run(QC, "check", "--output", str(self.out))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(json.loads(ran.stdout), self.report)

    def test_check_writes_a_report_and_exits_two_when_a_check_fails(self):
        written = Path(self.tmp.name) / "failing-qc.json"
        spec = Path(self.tmp.name) / "tight.json"
        spec.write_text(json.dumps({"max_cue_frames": 5}))
        ran = run(QC, "check", "--output", str(self.out), "--qc-spec", str(spec), "--write", str(written))
        self.assertEqual(ran.returncode, 2)
        self.assertEqual(json.loads(written.read_text())["status"], "fail")
        self.assertIn("captions.max_length", json.loads(ran.stdout)["failed"])

    def test_verify_accepts_a_clean_chain_and_names_a_tampered_one(self):
        self.assertEqual(run(QC, "verify", "--output", str(self.out)).returncode, 0)
        target = self.copy()
        path = target / "qc" / "qc-report.json"
        path.write_text(path.read_text().replace("pass", "fail", 1))
        ran = run(QC, "verify", "--output", str(target))
        self.assertEqual(ran.returncode, 2)
        self.assertIn("qc-report.json", ran.stdout)

    def test_a_bad_qc_spec_file_is_refused_with_status_two(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps({"max_cue_frames": 0}))
        for tool, command in ((QC, "check"), (PIPE, "verify")):
            ran = run(tool, command, "--output", str(self.out), "--qc-spec", str(bad))
            self.assertEqual(ran.returncode, 2, tool)
            self.assertIn("qc spec", ran.stdout)
        missing = run(QC, "check", "--output", str(self.out), "--qc-spec", str(Path(self.tmp.name) / "none.json"))
        self.assertEqual(missing.returncode, 2)

    def test_the_pipeline_cli_runs_with_the_stage_and_verifies_it(self):
        out = Path(self.tmp.name) / "cli"
        ran = run(PIPE, "run", "--output", str(out))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(json.loads(ran.stdout)["stages"][-1], "qc")
        self.assertEqual(run(PIPE, "verify", "--output", str(out)).returncode, 0)
        path = out / "qc" / "qc-report.json"
        path.write_text(path.read_text() + " ")
        self.assertEqual(run(PIPE, "verify", "--output", str(out)).returncode, 2)

    def test_the_pipeline_cli_refuses_a_chain_that_fails_its_qc(self):
        spec = Path(self.tmp.name) / "tight2.json"
        spec.write_text(json.dumps({"max_cue_frames": 5}))
        out = Path(self.tmp.name) / "cli-refused"
        ran = run(PIPE, "run", "--output", str(out), "--qc-spec", str(spec))
        self.assertEqual(ran.returncode, 2)
        self.assertIn("captions.max_length", ran.stdout)
        self.assertFalse((out / "pipeline-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
