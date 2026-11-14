# Room-tone extraction and gap fill

`amp/studies/room_tone_fill.py` and `tools/room_tone_fill.py` repair the silence that dialogue edits leave behind. A pause still carries the room; a *hole* is digital silence where the room was cut away with the words. A hole is audible because the room vanishes, so it is filled with tone taken from the same recording.

## Method

1. **Find holes** (`find_holes`): runs of digital silence (|x| <= 1e-6, about -120 dBFS) on every channel, at sample accuracy, at least 50 ms long.
2. **Find tone** (`find_tone_regions`): 20 ms frames within 6 dB of the recording's noise floor (10th percentile of non-silent frame level), grouped into stretches of at least 0.3 s and trimmed 50 ms at each end so speech tails and hole edges stay out.
3. **Build a bed** (`build_bed`): tone of any length from those regions. Each piece is a random sub-stretch; the same region is never used twice in a row while another exists; each splice is an 80 ms equal-power crossfade placed at the offset where the two sides correlate best (`best_splice_offset`), so the join does not add a click or a beat. The result lists the pieces and the longest uninterrupted stretch so repetitiveness is measurable.
4. **Fill** (`fill_holes`): each hole gets its own bed (seed + hole number, so two holes never repeat each other), scaled to the RMS of the nearest tone regions (clamped to +-12 dB, with a warning). The patch crossfades with the neighbouring audio only on the side where that audio is as quiet as tone; against speech the crossfade is zero and the speech is not touched. Samples outside the patched zones stay bit-identical. Stereo and multichannel keep their channel relationship because every channel uses the same pieces and offsets.

## Report (`amps.room_tone_fill.v1`)

Per hole: position, crossfade lengths, target and applied level, pieces used. After the fill, `fill_report` adds observed checks: remaining digital silence, `untouched_identical`, level error against target, edge level deviation (20 ms inside the patch against 20 ms outside it) and octave-band shape deviation against the source tone (62.5 Hz to 8 kHz, overall level removed). `tools/room_tone_fill.py` exits 2 and writes no audio when a tolerance is exceeded (level 2 dB, edge step 6 dB, band shape 6 dB). It also refuses a recording that has holes but no usable pause.

```bash
python tools/room_tone_fill.py demo outputs/room-tone-demo      # synthetic dialogue with two holes, filled, with report
python tools/room_tone_fill.py scan --input dialogue.wav
python tools/room_tone_fill.py fill --input dialogue.wav --output filled.wav --report report.json --seed 0
```

WAV formats: PCM 16, PCM 32 and float32, written back in the same format. The demo is invented: harmonic bursts with syllable envelopes over coloured noise and a 60 Hz hum, no recorded speech. These checks show that the gap is gone and the tone matches in level and colour; they do not replace listening. Real dialogue with a changing room (traffic, a fire, a fridge cycling) needs tone from the same moment, which is why the nearest regions set the level.
