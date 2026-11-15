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

## What the tests assert

`tests/test_captions.py`: the invented demo meets the spec, keeps every word and reads back identically from SRT and WebVTT; a short word is stretched to the minimum without crossing the next cue; sentence, silence, clause and duration splits; no line over the limit across 36 words; line balance, punctuation preference and orphan avoidance; single-line specs; overlapping word times; unfixable reading speed is reported; `24000/1001` derives 1001 ms from frame 24 and rounded decimals are refused; every invalid word and spec is named; `check` reports each rule (overlap, gap, minimum, maximum, line length, line count, reading speed, frame grid, empty span) and refuses malformed files; dropped or reordered words are detected; the review regressions (a 20-frame cue not reported short at 24 fps across seven rates, WebVTT from other tools, markup-like words escaped and round-tripped, SRT warnings); CLI exit statuses and a tampered file.

These are timing and layout rules. They do not say the words are right, that a human would break the lines there, or that a distributor will accept the file.
