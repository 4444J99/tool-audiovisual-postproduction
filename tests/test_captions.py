import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from amp.studies import captions as cap

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "captions.py"


def cli(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


def w(text, start, end):
    return {"text": text, "start": start, "end": end}


def run(tokens, fps=24, spec="broadcast", step=0.4, dur=0.3, start=0.0, **kw):
    """Words at a steady pace: one per ``step`` seconds, each ``dur`` long."""
    words = [w(t, round(start + i * step, 4), round(start + i * step + dur, 4)) for i, t in enumerate(tokens)]
    return words, cap.build(words, fps, spec, **kw)


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.words = cap.synthetic_words()
        cls.result = cap.build(cls.words, 24, "broadcast")

    def test_demo_meets_the_spec_and_keeps_every_word(self):
        self.assertEqual(self.result["problems"], [])
        self.assertTrue(cap.words_preserved(self.words, self.result["cues"]))
        self.assertEqual(len(self.result["cues"]), 5)

    def test_every_edge_is_on_the_frame_grid_and_reads_back_from_both_formats(self):
        for text in (self.result["srt"], self.result["vtt"]):
            back = cap.parse(text, 24)
            self.assertEqual([(c["start"], c["end"], c["lines"]) for c in back],
                             [(c["start"], c["end"], c["lines"]) for c in self.result["cues"]])
            self.assertEqual(cap.check(back, 24, "broadcast"), [])
        self.assertTrue(self.result["vtt"].startswith("WEBVTT\n\n"))
        self.assertIn(" --> ", self.result["srt"])
        self.assertRegex(self.result["srt"], r"\d\d:\d\d:\d\d,\d{3} --> ")
        self.assertRegex(self.result["vtt"], r"\d\d:\d\d:\d\d\.\d{3} --> ")

    def test_a_short_word_is_stretched_to_the_minimum_without_touching_the_next_cue(self):
        yes = next(c for c in self.result["cues"] if c["lines"] == ["Yes."])
        raw = next(x for x in self.words if x["text"] == "Yes.")
        self.assertLess(raw["end"] - raw["start"], 1.0)
        self.assertGreaterEqual((yes["end"] - yes["start"]) / 24, 1.0 - 1e-9)

    def test_build_is_deterministic(self):
        again = cap.build(cap.synthetic_words(), 24, "broadcast")
        self.assertEqual(again["srt_sha256"], self.result["srt_sha256"])
        self.assertEqual(again["vtt_sha256"], self.result["vtt_sha256"])


class GroupingTests(unittest.TestCase):
    def test_sentence_end_and_long_silence_start_a_new_cue(self):
        _, one = run(["Hello", "there.", "Next", "one"])
        self.assertEqual([c["lines"] for c in one["cues"]], [["Hello there."], ["Next one"]])
        words = [w("Hello", 0.0, 0.3), w("again", 2.0, 2.3)]
        self.assertEqual(len(cap.build(words, 24, "broadcast")["cues"]), 2)
        words = [w("Hello", 0.0, 0.3), w("again", 0.5, 0.8)]
        self.assertEqual(len(cap.build(words, 24, "broadcast")["cues"]), 1)

    def test_a_cue_never_passes_the_maximum_duration(self):
        _, built = run(["ab"] * 12, step=1.0, dur=0.9, spec={"max_duration_s": 3.0, "pause_split_s": 5.0})
        self.assertGreater(len(built["cues"]), 1)
        for c in built["cues"]:
            self.assertLessEqual((c["end"] - c["start"]) / 24, 3.0 + 1e-9)
        self.assertEqual(built["problems"], [])

    def test_no_line_exceeds_the_limit_and_no_word_is_lost(self):
        tokens = ("the quick brown fox jumps over the lazy dog while seven wizards quietly box " * 3).split()
        words, built = run(tokens, step=0.5, dur=0.45)
        self.assertTrue(cap.words_preserved(words, built["cues"]))
        for c in built["cues"]:
            self.assertLessEqual(len(c["lines"]), 2)
            self.assertTrue(all(len(line) <= 37 for line in c["lines"]), c["lines"])

    def test_a_clause_mark_splits_a_mostly_full_cue(self):
        tokens = "this sentence runs on for quite a long while without a pause at all, and then it continues".split()
        _, built = run(tokens, step=0.45, dur=0.4)
        self.assertTrue(built["cues"][0]["lines"][-1].endswith("at all,"), built["cues"][0]["lines"])
        self.assertEqual(" ".join(built["cues"][1]["lines"]), "and then it continues")


class LineBreakTests(unittest.TestCase):
    def test_fits_on_one_line(self):
        self.assertEqual(cap.break_lines(["a", "b"], 10, 2), ["a b"])

    def test_two_balanced_lines(self):
        self.assertEqual(cap.break_lines("aaaa bbbb cccc dddd".split(), 10, 2), ["aaaa bbbb", "cccc dddd"])

    def test_a_break_after_punctuation_beats_a_slightly_better_balance(self):
        self.assertEqual(cap.break_lines("one two, three four five".split(), 16, 2), ["one two,", "three four five"])

    def test_a_one_word_last_line_is_avoided_when_another_layout_exists(self):
        lines = cap.break_lines("aaaa bbbb ccc ddddddddddd".split(), 20, 2)       # the 13/11 split would leave one word
        self.assertEqual(lines, ["aaaa bbbb", "ccc ddddddddddd"])

    def test_nothing_fits_returns_none(self):
        self.assertIsNone(cap.break_lines("aaaa bbbb cccc".split(), 6, 1))
        self.assertIsNone(cap.break_lines("aaaaaaa bbbbbbb cccccc".split(), 8, 2))

    def test_a_single_line_spec_splits_into_more_cues(self):
        words, built = run("alpha bravo charlie delta echo foxtrot".split(), spec={"max_lines": 1, "max_chars_per_line": 14})
        self.assertGreater(len(built["cues"]), 2)
        self.assertTrue(all(len(c["lines"]) == 1 for c in built["cues"]))
        self.assertTrue(cap.words_preserved(words, built["cues"]))


class TimingTests(unittest.TestCase):
    def test_overlapping_cue_times_are_separated_by_the_minimum_gap(self):
        words = [w("First.", 0.0, 1.0), w("Second.", 0.99, 2.0)]
        built = cap.build(words, 24, "broadcast")
        a, b = built["cues"]
        self.assertGreaterEqual(b["start"] - a["end"], 2)
        # trimming the first cue to keep the gap leaves it under the minimum: reported, not hidden
        self.assertEqual(built["problems"], ["cue 1: lasts 0.917 s, under the 1.000 s minimum"])

    def test_stretching_stops_at_the_next_cue(self):
        words = [w("Quick.", 0.0, 0.2), w("Reply.", 0.5, 0.7)]
        a, b = cap.build(words, 24, "broadcast")["cues"]
        self.assertEqual(a["end"], b["start"] - 2)

    def test_reading_speed_the_words_leave_no_room_for_is_reported_not_hidden(self):
        words = [w("This", 0.0, 0.2), w("line", 0.2, 0.4), w("is", 0.4, 0.6), w("read.", 0.6, 0.8), w("Far", 0.85, 1.0),
                 w("too", 1.0, 1.1), w("fast", 1.1, 1.2), w("to", 1.2, 1.3), w("read", 1.3, 1.4), w("really.", 1.4, 1.5)]
        built = cap.build(words, 24, "broadcast")
        self.assertTrue(any("characters per second" in p for p in built["problems"]), built["problems"])
        self.assertTrue(cap.words_preserved(words, built["cues"]))

    def test_cps_is_enough_when_the_gap_allows(self):
        words = [w("Seventeen", 0.0, 0.1), w("characters", 0.1, 0.2), w("here.", 0.2, 0.3)]
        built = cap.build(words, 24, "broadcast")
        c = built["cues"][0]
        self.assertLessEqual(len("".join(c["lines"])) / ((c["end"] - c["start"]) / 24), 17.0 + 1e-9)

    def test_ntsc_rate_derives_milliseconds_from_frames(self):
        built = cap.build([w("Hello", 1.0, 2.5)], "24000/1001", "broadcast")
        c = built["cues"][0]
        self.assertEqual(c["start"], 24)
        self.assertEqual(c["start_ms"], 1001)
        self.assertEqual(cap.ms_of(c["end"], Fraction(24000, 1001)), c["end_ms"])
        self.assertEqual(cap.check(cap.parse(built["srt"], "24000/1001"), "24000/1001", "broadcast"), [])

    def test_a_rounded_decimal_frame_rate_is_refused(self):
        for bad in (23.976, "23.976", 0, -24, "24/0", True, None, 1000):
            with self.assertRaises(cap.CaptionError, msg=repr(bad)):
                cap.parse_fps(bad)
        self.assertEqual(cap.parse_fps(25.0), 25)


class ValidationTests(unittest.TestCase):
    def test_invalid_words_are_all_named(self):
        words = [w("ok", 0, 1), w("two words", 1, 2), w("x", float("nan"), 3), w("y", 5, 4), w("", 6, 7), w("back", 0.5, 0.9),
                 w("z" * 50, 8, 9), "text"]
        with self.assertRaises(cap.CaptionError) as raised:
            cap.build(words, 24)
        joined = " | ".join(raised.exception.errors)
        for needle in ("word 1", "word 2", "word 3", "word 4", "word 5", "word 6", "wider than", "word 7"):
            self.assertIn(needle, joined)
        with self.assertRaises(cap.CaptionError):
            cap.build([], 24)

    def test_spec_validation(self):
        for bad in ({"max_lines": 3}, {"max_cps": 0}, {"max_cps": float("inf")}, {"min_duration_s": 9, "max_duration_s": 3},
                    {"max_chars_per_line": 0}, {"min_gap_frames": -1}, {"max_lines": True}):
            with self.assertRaises(cap.CaptionError, msg=str(bad)):
                cap.normalize_spec(bad)
        with self.assertRaises(cap.CaptionError):
            cap.normalize_spec("nonesuch")
        self.assertEqual(cap.normalize_spec({"max_cps": 15})["max_chars_per_line"], 37)


class CheckTests(unittest.TestCase):
    def cue(self, index, start_ms, end_ms, *lines):
        return {"index": index, "start_ms": start_ms, "end_ms": end_ms, "lines": list(lines)}

    def test_each_rule_is_reported(self):
        cues = [self.cue(1, 0, 1000, "fine"),
                self.cue(2, 900, 2500, "overlaps the one before"),
                self.cue(3, 2542, 2792, "too short"),
                self.cue(4, 3000, 12000, "too long"),
                self.cue(5, 13000, 14000, "a" * 40),
                self.cue(6, 15000, 16000, "one", "two", "three"),
                self.cue(7, 17000, 17500, "x" * 30),
                self.cue(8, 18000, 18010, "off the grid"),
                self.cue(9, 20000, 20000, "empty span")]
        problems = " | ".join(cap.check(cues, 24, "broadcast"))
        for needle in ("cue 2: starts at 900 ms before", "cue 3: lasts 0.250 s, under", "gap to the previous cue", "cue 4: lasts 9.000 s, over",
                       "line of 40 characters", "3 lines", "cue 7: reads at 60.0 characters per second", "cue 8: end 18010 ms is not on the 24 fps",
                       "cue 9: ends at 20000 ms, not after"):
            self.assertIn(needle, problems)

    def test_malformed_files_are_refused(self):
        for text in ("1\n00:00:01,000 -> 00:00:02,000\nhi\n", "1\n00:00:01,000 --> 00:00:02,000\n", "1\n0:1:2 --> 3\nhi\n"):
            with self.assertRaises(cap.CaptionError, msg=text):
                cap.parse(text, 24)
        self.assertEqual(cap.parse("", 24), [])

    def test_words_that_were_dropped_or_reordered_are_detected(self):
        words = [w("a", 0, 0.5), w("b", 0.5, 1.0)]
        cues = cap.build(words, 24)["cues"]
        self.assertTrue(cap.words_preserved(words, cues))
        self.assertFalse(cap.words_preserved(words, [dict(cues[0], lines=["b a"])]))
        self.assertFalse(cap.words_preserved(words, [dict(cues[0], lines=["a"])]))


class ReviewRegressionTests(unittest.TestCase):
    """Codex review of the first head: three P2 findings, each pinned."""

    def test_a_cue_stretched_to_exactly_the_minimum_frames_is_not_reported_short_after_millisecond_rounding(self):
        built = cap.build([w("Hi", 0.0, 0.1)], 24, "web")           # 5/6 s at 24 fps is exactly 20 frames, written as 833 ms
        cue = built["cues"][0]
        self.assertEqual(cue["end"] - cue["start"], 20)
        self.assertEqual(cue["end_ms"] - cue["start_ms"], 833)
        self.assertEqual(built["problems"], [])
        self.assertEqual(cap.check(cap.parse(built["srt"], 24), 24, "web"), [])
        short = [{"index": 1, "start_ms": 0, "end_ms": 792, "lines": ["Hi"]}]        # 19 frames: genuinely short
        self.assertTrue(any("under the" in p for p in cap.check(short, 24, "web")))

    def test_the_minimum_holds_at_other_rates_after_rounding(self):
        for fps in (24, 25, 30, 50, "24000/1001", "30000/1001", "60000/1001"):
            for spec in ("broadcast", "web"):
                built = cap.build([w("Hi", 0.0, 0.05)], fps, spec)
                self.assertEqual(built["problems"], [], (fps, spec))
                self.assertEqual(cap.check(cap.parse(built["vtt"], fps), fps, spec), [], (fps, spec))

    def test_webvtt_from_other_tools_parses(self):
        text = ("WEBVTT - made elsewhere\nKind: captions\n\nNOTE a comment\nacross two lines\n\nSTYLE\n::cue { color: red }\n\n"
                "intro\n00:01.000 --> 00:02.500 align:start position:10%\n<v Ann>Hello <i>there</i>\n\n"
                "00:00:03.000 --> 00:00:04.500\nNo identifier\n\n2\n01:00:00.000 --> 01:00:01.000\nHour one\n")
        cues = cap.parse(text, 24)
        self.assertEqual([(c["start_ms"], c["end_ms"], c["lines"]) for c in cues],
                         [(1000, 2500, ["Hello there"]), (3000, 4500, ["No identifier"]), (3600000, 3601000, ["Hour one"])])
        self.assertEqual([c["start"] for c in cues], [24, 72, 86400])
        with self.assertRaises(cap.CaptionError):
            cap.parse("1\n00:01.000 --> 00:02.000\nhi\n", 24)           # SRT keeps its stricter form: hours and a comma
        with self.assertRaises(cap.CaptionError):
            cap.parse("WEBVTT\n\nonly an identifier\nand text\n", 24)

    def test_markup_like_words_are_escaped_in_webvtt_and_survive_the_round_trip(self):
        words = [w("<inaudible>", 0.0, 0.5), w("Tom", 0.6, 0.9), w("&", 0.9, 1.0), w("<b>Jerry</b>", 1.0, 1.5)]
        built = cap.build(words, 24, "broadcast")
        self.assertIn("&lt;inaudible&gt; Tom &amp; &lt;b&gt;Jerry&lt;/b&gt;", built["vtt"])
        self.assertNotIn("<", built["vtt"].split("\n\n", 1)[1].replace("-->", ""))
        back = cap.parse(built["vtt"], 24)
        self.assertEqual(back[0]["lines"], ["<inaudible> Tom & <b>Jerry</b>"])
        self.assertTrue(cap.words_preserved(words, back))
        self.assertEqual(len(built["warnings"]), 2)                       # SRT has no escape: said, not hidden
        self.assertIn("SRT players may read as markup", built["warnings"][0])
        self.assertEqual(cap.build([w("plain", 0, 1)], 24)["warnings"], [])
        self.assertEqual(cap.vtt_unescape(cap.vtt_escape("a&amp;b <c> &lt;")), "a&amp;b <c> &lt;")


    def test_whitespace_at_the_ends_of_a_token_is_refused_not_rendered(self):
        for bad in ("\nHello", "Hello\n", " Hello", "Hello ", "\u2028Hello", "Hel\tlo", "\u00a0", "-->", "a-->b"):
            with self.assertRaises(cap.CaptionError, msg=repr(bad)):
                cap.build([w(bad, 0, 1)], 24)
        self.assertEqual(cap.build([w("Hello", 0, 1)], 24)["problems"], [])


class CliTests(unittest.TestCase):
    def test_demo_then_check_then_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "c"
            ran = cli("demo", str(out))
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertEqual({p.name for p in out.iterdir()}, {"words.json", "captions.srt", "captions.vtt", "captions-report.json"})
            report = json.loads((out / "captions-report.json").read_text())
            self.assertTrue(report["words_preserved"])
            self.assertEqual(report["problems"], [])
            for name in ("captions.srt", "captions.vtt"):
                ok = cli("check", "--input", str(out / name), "--fps", "24", "--words", str(out / "words.json"))
                self.assertEqual(ok.returncode, 0, ok.stdout)
            text = (out / "captions.srt").read_text()
            (out / "tampered.srt").write_text(text.replace("Welcome back to the show.", "Welcome back to the show, friends, and everybody else who is here."))
            bad = cli("check", "--input", str(out / "tampered.srt"), "--fps", "24", "--words", str(out / "words.json"))
            self.assertEqual(bad.returncode, 2)
            self.assertIn("characters per second", bad.stdout)
            self.assertIn("not the input words", bad.stdout)

    def test_build_from_a_words_file_with_a_spec_file(self):
        with tempfile.TemporaryDirectory() as d:
            words = Path(d) / "words.json"
            words.write_text(json.dumps([w("Hello", 0.0, 0.5), w("world.", 0.5, 1.0)]))
            spec = Path(d) / "spec.json"
            spec.write_text(json.dumps({"name": "tight", "max_chars_per_line": 8, "max_lines": 2}))
            ran = cli("build", "--words", str(words), "--output", str(Path(d) / "o"), "--spec", str(spec), "--fps", "25")
            self.assertEqual(ran.returncode, 0, ran.stdout)
            self.assertEqual((Path(d) / "o" / "captions.srt").read_text().splitlines()[2:4], ["Hello", "world."])
            self.assertEqual(json.loads((Path(d) / "o" / "captions-report.json").read_text())["spec"]["name"], "tight")

    def test_markup_words_build_clean_with_a_warning_and_a_vtt_that_checks(self):
        with tempfile.TemporaryDirectory() as d:
            words = Path(d) / "w.json"
            words.write_text(json.dumps([w("<laughs>", 0.0, 0.5), w("fine.", 0.6, 1.0)]))
            ran = cli("build", "--words", str(words), "--output", str(Path(d) / "o"))
            self.assertEqual(ran.returncode, 0, ran.stdout)
            self.assertEqual(len(json.loads(ran.stdout)["warnings"]), 1)
            ok = cli("check", "--input", str(Path(d) / "o" / "captions.vtt"), "--words", str(words))
            self.assertEqual(ok.returncode, 0, ok.stdout)

    def test_bad_input_exits_2_with_json(self):
        with tempfile.TemporaryDirectory() as d:
            nope = cli("build", "--words", str(Path(d) / "missing.json"), "--output", d)
            self.assertEqual(nope.returncode, 2)
            self.assertIn("errors", json.loads(nope.stdout))
            words = Path(d) / "w.json"
            words.write_text('[{"text": "x", "start": NaN, "end": 1}]')
            self.assertEqual(cli("build", "--words", str(words), "--output", d).returncode, 2)
            words.write_text(json.dumps([w("x", 0, 1)]))
            self.assertEqual(cli("build", "--words", str(words), "--output", d, "--fps", "23.976").returncode, 2)
            self.assertEqual(cli("build", "--words", str(words), "--output", d, "--spec", "nonesuch").returncode, 2)
            self.assertEqual(cli("check", "--input", str(Path(d) / "absent.srt")).returncode, 2)


if __name__ == "__main__":
    unittest.main()
