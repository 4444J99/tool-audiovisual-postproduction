import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from amp.studies import caption_burnin as cb
from amp.studies import captions as cap

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "caption_burnin.py"
GEOMETRY = {"width": 240, "height": 48}
FPS, FRAMES = 24, 120


def cli(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


def srt_for(*cues):
    """cues are (start_frame, end_frame, [lines]); times are exact on the 24 fps grid."""
    def stamp(frame):
        ms = round(frame * 1000 / FPS)
        return f"00:00:{ms // 1000:02d},{ms % 1000:03d}"
    return "".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n" + "\n".join(lines) + "\n\n" for i, (a, b, lines) in enumerate(cues, 1))


SRT = srt_for((12, 36, ["Hello there."]), (48, 84, ["Second cue, two", "lines & a pause"]))


def frame_of(payload, f, size=240 * 48):
    return payload[f * size:(f + 1) * size]


class FontTests(unittest.TestCase):
    def test_every_printable_ascii_character_has_a_glyph_and_visible_ones_differ(self):
        self.assertEqual(sorted(cb.GLYPHS), [chr(c) for c in range(32, 127)])
        self.assertEqual(sum(cb.GLYPHS[" "]), 0)
        shapes = {}
        for ch, cols in cb.GLYPHS.items():
            if ch != " ":
                self.assertTrue(any(cols), ch)
                shapes.setdefault(cols, []).append(ch)
        self.assertEqual([chars for chars in shapes.values() if len(chars) > 1], [])

    def test_known_glyphs_read_back_as_their_letters(self):
        rows = ["".join("#" if r >> c & 1 else "." for c in range(5)) for r in cb.glyph_rows("A")]
        self.assertEqual(rows[:7], ["..#..", ".#.#.", "#...#", "#...#", "#####", "#...#", "#...#"])
        self.assertEqual(cb.glyph_rows("y")[7], 0b01110)       # the descender row is used

    def test_font_hash_pins_the_table(self):
        self.assertEqual(len(cb.FONT_SHA256), 64)
        self.assertEqual(cb.FONT_SHA256, cb.sha256_bytes(json.dumps({c: list(g) for c, g in sorted(cb.GLYPHS.items())}, sort_keys=True).encode()))


class BurnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload, cls.report = cb.burn(SRT, FPS, FRAMES, GEOMETRY)

    def burned(self, text=SRT, **kw):
        return cb.burn(text, FPS, FRAMES, GEOMETRY, **kw)

    def test_the_report_describes_the_frames_and_verifies(self):
        self.assertEqual(len(self.payload), FRAMES * 240 * 48)
        self.assertEqual(len(self.report["frame_sha256"]), FRAMES)
        self.assertEqual(self.report["captioned_frames"], (36 - 12) + (84 - 48))
        self.assertEqual(cb.verify(self.payload, self.report, SRT), [])

    def test_a_cue_shows_for_start_up_to_but_not_including_end(self):
        seg = self.report["params"]["segments"][0]["seed"]
        clean = lambda f: cb.background(f, 240, 48, seg).tobytes()          # noqa: E731
        for f in (0, 11, 36, 47, 84, 119):
            self.assertEqual(frame_of(self.payload, f), clean(f), f"frame {f} has no cue")
        for f in (12, 13, 35, 48, 83):
            self.assertNotEqual(frame_of(self.payload, f), clean(f), f"frame {f} has a cue")

    def test_glyph_pixels_are_drawn_inside_a_darkened_bar_at_the_bottom(self):
        seg = self.report["params"]["segments"][0]["seed"]
        clean = np.frombuffer(cb.background(20, 240, 48, seg).tobytes(), dtype=np.uint8).reshape(48, 240)
        shown = np.frombuffer(frame_of(self.payload, 20), dtype=np.uint8).reshape(48, 240)
        changed = np.argwhere(shown != clean)
        self.assertGreater(len(changed), 0)
        self.assertGreaterEqual(changed[:, 0].min(), 48 - 8 - 14)        # one 8 px line, 3 px padding, 8 px margin
        self.assertLess(changed[:, 0].max(), 48 - 8)
        self.assertEqual((shown == 255).any(axis=0).sum() > 20, True)
        top, left, mask = cb.caption_stamp(["Hello there."], cb.normalize_geometry(GEOMETRY))
        box = shown[top:top + mask.shape[0], left:left + mask.shape[1]]
        self.assertTrue((box[mask] == 255).all())
        dark = (clean[top:top + mask.shape[0], left:left + mask.shape[1]].astype(int) * 35 // 100)
        self.assertTrue((box[~mask] == dark[~mask]).all())

    def test_frames_differ_from_one_another_so_a_frame_swap_is_visible(self):
        self.assertEqual(len({frame_of(self.payload, f) for f in range(0, 12)}), 12)

    def test_the_same_inputs_give_the_same_bytes_and_head(self):
        again, report = self.burned()
        self.assertEqual(again, self.payload)
        self.assertEqual(report["chain_head"], self.report["chain_head"])

    def test_webvtt_burns_the_same_frames_with_its_own_hash(self):
        cues = cap.parse(SRT, FPS)
        vtt = cap.render_vtt(cues)
        payload, report = self.burned(vtt)
        self.assertEqual(payload, self.payload)                           # '&' round-trips through &amp;
        self.assertNotEqual(report["chain_head"], self.report["chain_head"])
        self.assertEqual(cb.verify(payload, report, vtt), [])

    def test_smart_punctuation_is_replaced_and_reported_as_ascii(self):
        text = srt_for((12, 36, ["Wait… “ok” — fine"]))
        payload, report = self.burned(text)
        plain, _ = self.burned(srt_for((12, 36, ['Wait... "ok" - fine'])))
        self.assertEqual(frame_of(payload, 20), frame_of(plain, 20))
        self.assertEqual(cb.verify(payload, report, text), [])

    def test_segments_tint_the_background_and_are_part_of_the_params(self):
        seg = [{"start": 0, "end": 60, "seed": "10" * 32}, {"start": 60, "end": FRAMES, "seed": "f0" * 32}]
        payload, report = self.burned(segments=seg)
        self.assertNotEqual(frame_of(payload, 0), frame_of(self.payload, 0))
        self.assertEqual(cb.verify(payload, report, SRT), [])
        self.assertNotEqual(report["chain_head"], self.report["chain_head"])
        self.assertEqual(report["params"]["segments"], seg)

    def test_chain_links_commit_to_every_earlier_frame_and_the_params(self):
        links = cb.chain_links(self.report["params"], self.report["frame_sha256"])
        self.assertEqual(links[-1], self.report["chain_head"])
        digests = list(self.report["frame_sha256"])
        digests[50] = cb.sha256_bytes(b"x")
        changed = cb.chain_links(self.report["params"], digests)
        self.assertEqual(changed[:50], links[:50])
        self.assertTrue(all(a != b for a, b in zip(changed[50:], links[50:])))
        other = dict(self.report["params"], fps="25")
        self.assertNotEqual(cb.chain_links(other, self.report["frame_sha256"])[0], links[0])


class TamperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload, cls.report = cb.burn(SRT, FPS, FRAMES, GEOMETRY)

    def report_copy(self):
        return json.loads(json.dumps(self.report))

    def flip(self, f, offset=7):
        data = bytearray(self.payload)
        data[f * 240 * 48 + offset] ^= 0x01
        return bytes(data)

    def reseal(self, payload, report):
        """Rewrite every digest, the frames hash and the chain head so the report agrees with the edited frames."""
        size = 240 * 48
        digests = [cb.sha256_bytes(payload[i * size:(i + 1) * size]) for i in range(FRAMES)]
        report["frame_sha256"] = digests
        report["frames_sha256"] = cb.sha256_bytes(payload)
        report["chain_head"] = cb.chain_links(report["params"], digests)[-1]
        return report

    def test_one_flipped_pixel_in_an_uncaptioned_frame_names_the_frame(self):
        problems = cb.verify(self.flip(5), self.report, SRT)
        self.assertTrue(any(p.startswith("frame 5 differs") for p in problems), problems)
        self.assertTrue(any("chain head does not match" in p for p in problems), problems)

    def test_one_flipped_pixel_in_a_captioned_frame_names_the_frame(self):
        problems = cb.verify(self.flip(60, offset=240 * 40 + 100), self.report, SRT)
        self.assertTrue(any(p.startswith("frame 60 differs") for p in problems), problems)

    def test_a_resealed_report_still_fails_because_frames_are_rederived(self):
        edited = self.flip(70)
        report = self.reseal(edited, self.report_copy())
        self.assertEqual(report["chain_head"], cb.chain_links(report["params"], report["frame_sha256"])[-1])
        self.assertEqual([p for p in cb.verify(edited, report, SRT) if "digest" in p or "chain head" in p or "frames hash" in p], [])
        problems = cb.verify(edited, report, SRT)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("frame 70 differs"), problems)

    def test_two_swapped_frames_are_found(self):
        size = 240 * 48
        data = bytearray(self.payload)
        data[30 * size:31 * size], data[90 * size:91 * size] = self.payload[90 * size:91 * size], self.payload[30 * size:31 * size]
        problems = cb.verify(bytes(data), self.report, SRT)
        self.assertTrue(any(p.startswith("frame 30 (and 1 more, last 90) differs") for p in problems), problems)

    def test_a_truncated_or_padded_frames_file_is_refused(self):
        for payload in (self.payload[:-240 * 48], self.payload + b"\0"):
            problems = cb.verify(payload, self.report, SRT)
            self.assertTrue(any("frames file holds" in p for p in problems), problems)

    def test_a_changed_word_in_the_caption_file_is_caught(self):
        edited = SRT.replace("Hello", "Jello")
        problems = cb.verify(self.payload, self.report, edited)
        self.assertTrue(any("caption file is not the one the frames were burned from" in p for p in problems), problems)
        problems = cb.verify(self.payload, self.report_copy() | {"params": dict(self.report["params"], srt_sha256=cap.sha256_text(edited))}, edited)
        self.assertTrue(any(p.startswith("frame 1") and "differs" in p for p in problems), problems)

    def test_a_cue_moved_by_one_frame_is_caught(self):
        later = srt_for((13, 37, ["Hello there."]), (48, 84, ["Second cue, two", "lines & a pause"]))
        problems = cb.verify(self.payload, self.report, later)
        self.assertTrue(any("hash differs" in p for p in problems), problems)
        report = self.report_copy()
        report["params"]["srt_sha256"] = cap.sha256_text(later)
        problems = cb.verify(self.payload, report, later)
        self.assertTrue(any(p.startswith("frame 12 ") and "differs" in p for p in problems), problems)

    def test_edited_params_in_the_report_are_caught(self):
        for key, value in (("fps", "25"), ("frames", FRAMES - 1), ("font_sha256", "0" * 64)):
            report = self.report_copy()
            report["params"][key] = value
            self.assertNotEqual(cb.verify(self.payload, report, SRT), [], key)
        report = self.report_copy()
        report["params"]["geometry"]["bar_percent"] = 90
        self.assertTrue(any(p.startswith("frame") for p in cb.verify(self.payload, report, SRT)))

    def test_an_edited_recorded_digest_head_or_count_is_caught(self):
        report = self.report_copy()
        report["frame_sha256"][3] = "0" * 64
        self.assertTrue(any("recorded digest of frame 3" in p for p in cb.verify(self.payload, report, SRT)))
        report = self.report_copy()
        report["chain_head"] = "0" * 64
        self.assertEqual(cb.verify(self.payload, report, SRT), ["chain head does not match the frames file"])
        report = self.report_copy()
        report["captioned_frames"] += 1
        self.assertEqual(cb.verify(self.payload, report, SRT), ["captioned frame count does not match the caption file"])

    def test_a_malformed_report_is_reported_not_raised(self):
        self.assertTrue(cb.verify(self.payload, {}, SRT)[0].startswith("burn-in report cannot be checked"))
        self.assertTrue(cb.verify(self.payload, {"params": {"schema": "other"}}, SRT)[0].startswith("burn-in report: schema"))


class RefusalTests(unittest.TestCase):
    def expect(self, text, needle, frames=FRAMES, geometry=GEOMETRY, fps=FPS):
        with self.assertRaises(cb.BurnError) as raised:
            cb.burn(text, fps, frames, geometry)
        self.assertTrue(any(needle in e for e in raised.exception.errors), raised.exception.errors)

    def test_a_character_without_a_glyph_is_refused_not_drawn_as_a_box(self):
        self.expect(srt_for((12, 36, ["café"])), "no glyph for")

    def test_a_cue_past_the_last_frame_is_refused(self):
        self.expect(srt_for((100, 130, ["Late."])), "outside the 120 frames")

    def test_overlapping_cues_are_refused(self):
        self.expect(srt_for((12, 40, ["One."]), (36, 60, ["Two."])), "would share a frame")

    def test_text_wider_than_the_frame_is_refused(self):
        self.expect(srt_for((12, 36, ["x" * 60])), "does not fit")
        self.expect(srt_for((12, 36, ["Hello there."])), "does not fit", geometry={"width": 240, "height": 48, "scale": 4})

    def test_an_empty_caption_file_and_bad_settings_are_refused(self):
        self.expect("", "no cues")
        self.expect(SRT, "frames must be", frames=0)
        self.expect(SRT, "geometry width", geometry={"width": 0})
        self.expect(SRT, "unknown geometry key", geometry={"shape": 1})
        self.expect(SRT, "fps", fps=29.97)

    def test_bad_segments_are_refused(self):
        for seg, needle in (([{"start": 0, "end": 50, "seed": "aa"}], "cover 50 frames"),
                            ([{"start": 5, "end": FRAMES, "seed": "aa"}], "do not continue"),
                            ([{"start": 0, "end": FRAMES, "seed": "ZZ"}], "lowercase hex")):
            with self.assertRaises(cb.BurnError) as raised:
                cb.burn(SRT, FPS, FRAMES, GEOMETRY, seg)
            self.assertTrue(any(needle in e for e in raised.exception.errors), raised.exception.errors)

    def test_a_malformed_caption_file_is_refused_with_the_parser_message(self):
        self.expect("1\nnot a time\ntext\n\n", "captions:")


class CliTests(unittest.TestCase):
    def test_demo_then_verify_then_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "o"
            ran = cli("demo", "--output", str(out))
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            result = json.loads(ran.stdout)
            self.assertEqual(result["cues"], 5)
            self.assertEqual(result["frames"], len((out / "frames.y8").read_bytes()) // (320 * 180))
            self.assertTrue((out / "contact-sheet.pgm").read_bytes().startswith(b"P5\n320 900\n255\n"))
            ok = cli("verify", "--captions", str(out / "captions.srt"), "--output", str(out))
            self.assertEqual(ok.returncode, 0, ok.stdout)
            self.assertEqual(json.loads(ok.stdout)["chain_head"], result["chain_head"])
            shown = cli("show", "--output", str(out), "--frame", "70")
            self.assertEqual(shown.returncode, 0)
            self.assertEqual(len(shown.stdout.splitlines()), 180)
            data = bytearray((out / "frames.y8").read_bytes())
            data[40 * 320 * 180 + 9] ^= 4
            (out / "frames.y8").write_bytes(bytes(data))
            bad = cli("verify", "--captions", str(out / "captions.srt"), "--output", str(out))
            self.assertEqual(bad.returncode, 2)
            self.assertIn("frame 40 differs", bad.stdout)

    def test_burn_refuses_an_unrenderable_caption_with_status_2(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "c.srt").write_text(srt_for((12, 36, ["café"])), encoding="utf-8")
            bad = cli("burn", "--captions", str(Path(d) / "c.srt"), "--output", str(Path(d) / "o"), "--frames", "60")
            self.assertEqual(bad.returncode, 2)
            self.assertIn("no glyph for", bad.stdout)
            self.assertEqual(cli("verify", "--captions", str(Path(d) / "c.srt"), "--output", str(Path(d) / "none")).returncode, 2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_preview_decodes_to_the_burned_frames(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "c.srt").write_text(SRT, encoding="utf-8")
            ran = cli("burn", "--captions", str(Path(d) / "c.srt"), "--output", str(Path(d) / "o"), "--frames", "120",
                      "--width", "240", "--height", "48", "--preview")
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIs(json.loads(ran.stdout)["preview_decodes_to_the_same_frames"], True)


if __name__ == "__main__":
    unittest.main()
