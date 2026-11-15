# Caption timing from word cues

`amp/studies/captions.py` and `tools/captions.py` lay word cues (what a speech aligner or a script timing sheet gives: `text`, `start`, `end` in seconds) out as SRT and WebVTT cues that obey a declared reading-speed and layout spec. They place words; they do not listen to audio or decide what is said.

```bash
python tools/captions.py demo outputs/captions-demo
python tools/captions.py build --words words.json --output outputs/captions --fps 24 [--spec broadcast|web|spec.json]
python tools/captions.py check --input captions.srt|captions.vtt --fps 24 [--spec ...] [--words words.json]
```

`words.json` is a list of `{"text", "start", "end"}` or `{"fps": 24, "words": [...]}`. `build` writes `captions.srt`, `captions.vtt` and `captions-report.json`, then **parses both written files back** and requires them to equal the cues. `check` judges any SRT or WebVTT file, including one made elsewhere; with `--words` it also requires every word to appear once, in order. Exit status 2: invalid words or spec, a cue list that breaks the spec, unreadable input.

## What it does

1. **Validates the words:** finite seconds, `0 <= start <= end`, starts non-decreasing, one whitespace-free token each, no word wider than a line. Every problem is listed.
2. **Groups words into cues.** A cue ends at a sentence end, at a silence of `pause_split_s` or more, at a clause mark (`, ; : - —`) once the cue is 60 % full, and wherever the next word would not fit `max_lines` lines or would pass `max_duration_s`.
3. **Breaks lines.** One line when it fits; otherwise two lines with the smallest width difference, a break after punctuation preferred, and a one-word last line avoided when another layout exists.
4. **Snaps to frames and keeps gaps.** Start and end are whole frames of the declared rate, `min_gap_frames` apart; overlapping word times are separated by trimming the earlier cue.
5. **Stretches forward** (never into the next cue's gap, never past `max_duration_s`) until the cue meets `min_duration_s` and `max_cps`.

What the spec cannot be made to hold for is **reported in `problems`, not hidden**: fast speech with no room to stretch reads over `max_cps`, and a cue trimmed to keep the gap can fall under the minimum duration. The build still returns the cues (so you can see them) and the CLI exits 2.

Frame rates are exact: `24`, `25`, `30` or a ratio such as `24000/1001`. A rounded decimal (`23.976`) is refused. Durations and reading speed are judged on the frames the written milliseconds stand for (a cue of exactly 20 frames at 24 fps is 833 ms and still meets a 5/6 s minimum). Times are integer frames internally and milliseconds are derived from them, so SRT and WebVTT carry the same instants and parse back to the same frames.

## Formats

**SRT** needs `hh:mm:ss,mmm`. **WebVTT** parsing accepts what other tools write: hours optional (`00:01.000`), any cue identifier or none, cue settings after the times, and NOTE, STYLE and REGION blocks (skipped); cue tags are dropped and `&amp; &lt; &gt;` decoded so `check` judges what a viewer reads. WebVTT output **escapes** `&`, `<` and `>`, so a word such as `<inaudible>` is shown, not swallowed as a tag, and reads back unchanged. SRT has no escape and many players read `<...>` as styling: such words are listed in the report's `warnings` (the build is not failed for them).

## Presets

Commonly published defaults, not any broadcaster's or platform's requirement; a JSON spec overrides any key.

| Preset | Characters per line | Lines | Max chars/s | Min / max duration | Gap |
|---|---|---|---|---|---|
| `broadcast` | 37 | 2 | 17 | 1.0 s / 7 s | 2 frames |
| `web` | 42 | 2 | 20 | 0.833 s / 7 s | 2 frames |

```json
{"name": "tight", "max_chars_per_line": 32, "max_lines": 2, "max_cps": 15, "min_duration_s": 1.2, "max_duration_s": 6, "min_gap_frames": 3, "pause_split_s": 0.6}
```

## Burn-in

`tools/caption_burnin.py` draws the cues of a written SRT or WebVTT file onto synthetic frames and makes the result checkable.

```bash
python tools/caption_burnin.py demo --output outputs/caption-burnin [--preview]
python tools/caption_burnin.py burn --captions captions.srt --output outputs/caption-burnin --fps 24 --frames 240 [--width 320 --height 180 --scale 1] [--preview]
python tools/caption_burnin.py verify --captions captions.srt --output outputs/caption-burnin
python tools/caption_burnin.py show --output outputs/caption-burnin --frame 60
```

**Why a Python frame writer.** The ffmpeg observed on the development host (9.0.2, Homebrew) lists neither the `subtitles` nor the `drawtext` filter and its build configuration shows no libass or freetype (CI's ffmpeg was not inspected), and a rasteriser's output changes with its version. The frames are therefore made by `amp/studies/caption_burnin.py` with integer arithmetic over an embedded 5x7 bitmap font (ASCII 32-126; “…”, dashes and curly quotes become their ASCII forms). A character with no glyph, a cue past the last frame, two cues on one frame, or text wider than the frame is **refused**, never drawn as a box or clipped.

**What it writes.** `frames.y8` (8-bit luma, rows back to back, one frame after another), `burnin-report.json` and `contact-sheet.pgm` (the middle frame of each cue). A cue is on screen for frames `start <= f < end`, the same half-open rule as the SRT end stamp. The background is an invented test pattern (a tinted ramp, a sweeping bar and a tick at the frame index) so frames differ from one another and a swapped frame shows.

**The chain.** `h0 = sha256(canonical params)`; `h_i = sha256(h_{i-1} || sha256(frame_i))`. The params pin the geometry, frame rate, frame count, background segments, the SHA-256 of the caption file and the SHA-256 of the font table, so the head commits to every pixel and to the inputs that made it. `verify` does not trust the recorded digests or head: it re-renders every frame from the caption file and the params, compares bytes, and names the first differing frame, then also checks the recorded digests, head, frames hash and captioned-frame count. A forger who edits a frame and rewrites the digests, head and frames hash still fails on re-derivation.

**Optional preview.** `--preview` wraps the frames in a lossless FFV1 `preview.mkv` and checks that ffmpeg decodes it back to the same bytes. The file is not hashed into anything and is not evidence; it is for looking at.

**In the pipeline.** `tools/av_pipeline.py` adds a `captions` and a `burnin` stage after `colour` ([PIPELINE-CHAIN](PIPELINE-CHAIN.md)): the SRT/WebVTT are hashed outputs, the burn-in's input is the SRT hash, and `verify` re-derives the frames. The caption words there are the invented demo timing, not a transcript of the pipeline's dialogue stand-in.

What this does not show: that the text is placed where a broadcaster's safe-area rules want it, that the bitmap font is legible at a real delivery size (it is a 5x7 test font, one pixel per font pixel at scale 1), or how a player renders the same cues. It proves the cues in the file are the cues on the frames.

## What the tests assert

`tests/test_captions.py`: the invented demo meets the spec, keeps every word and reads back identically from SRT and WebVTT; a short word is stretched to the minimum without crossing the next cue; sentence, silence, clause and duration splits; no line over the limit across 36 words; line balance, punctuation preference and orphan avoidance; single-line specs; overlapping word times; unfixable reading speed is reported; `24000/1001` derives 1001 ms from frame 24 and rounded decimals are refused; every invalid word and spec is named; `check` reports each rule (overlap, gap, minimum, maximum, line length, line count, reading speed, frame grid, empty span) and refuses malformed files; dropped or reordered words are detected; the review regressions (a 20-frame cue not reported short at 24 fps across seven rates, WebVTT from other tools, markup-like words escaped and round-tripped, SRT warnings); CLI exit statuses and a tampered file.

These are timing and layout rules. They do not say the words are right, that a human would break the lines there, or that a distributor will accept the file.

`tests/test_caption_burnin.py`: the font covers ASCII and its visible glyphs are distinct; a cue shows for `start <= f < end`; glyph pixels sit inside the darkened bar; determinism, WebVTT equivalence and smart-punctuation mapping; the chain commits to every earlier frame and the params; one flipped pixel in a captioned or uncaptioned frame, a re-sealed forgery, two swapped frames, a truncated or padded file, a reworded caption, a cue moved one frame, and edited params, digests, head or counts are each named; unrenderable characters, late or overlapping cues, oversized text and bad settings are refused; the CLI demo, verify, show and tamper exit statuses; and, where ffmpeg exists, that the FFV1 preview decodes to the same bytes.
