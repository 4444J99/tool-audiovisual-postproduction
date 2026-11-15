# Programme loudness, true peak and gain

`amp/studies/loudness.py` and `tools/loudness.py` measure what a delivery specification asks of a mix (integrated loudness, loudness range, true peak), check it against a target, and apply the one linear gain that reaches the target. Nothing in it needs FFmpeg; the tests use FFmpeg's `ebur128` filter only as an independent cross-check when it is installed.

## What is measured (`amps.loudness.v1`)

| Field | Method |
|---|---|
| `integrated_lufs` | ITU-R BS.1770-4: K-weighting, 400 ms blocks at 75 % overlap, absolute gate -70 LUFS, relative gate 10 LU below the absolute-gated mean. `null` when no block clears the gate (silence). |
| `loudness_range_lu` | EBU Tech 3342: 3 s short-term values every 100 ms, absolute gate -70 LUFS, relative gate 20 LU below the mean, 10th to 95th percentile. `null` under 3 s of gated programme. |
| `momentary_max_lufs`, `short_term_max_lufs` | Largest 400 ms and 3 s values. |
| `sample_peak_dbfs`, `true_peak_dbtp` | True peak by 4x oversampling below 96 kHz, 2x below 192 kHz, none above. |

K-weighting coefficients are derived for the file's own sample rate from the standard's analogue prototype, so 44.1 kHz and 96 kHz are exact rather than resampled. At 48 kHz they equal the standard's published table (a test pins the published pre-filter and RLB coefficients). Channel weights: left, right and centre 1.0, surrounds 1.41, LFE excluded; mono, stereo and 5.1 (L R C LFE Ls Rs) are chosen from the channel count, any other layout needs `--layout` (a name, or weights such as `1,1,1,0,1.41,1.41`).

## Commands

```bash
python tools/loudness.py demo outputs/loudness-demo        # invented stereo programme, normalised to EBU R128, with report
python tools/loudness.py measure   --input mix.wav
python tools/loudness.py check     --input mix.wav --spec ebu_r128
python tools/loudness.py normalize --input mix.wav --output out.wav --spec ebu_r128 --report report.json
python tools/loudness.py normalize --input mix.wav --output out.wav --target-lufs -16 --ceiling-dbtp -1 --on-ceiling cap
```

Presets are commonly published defaults, not any distributor's requirement; a JSON spec overrides them:

| Preset | Integrated | True peak max |
|---|---|---|
| `ebu_r128` | -23 LUFS, +-0.5 LU | -1 dBTP |
| `atsc_a85` | -24 LKFS, +-2 LU | -2 dBTP |
| `streaming` | -14 LUFS, +-1 LU | -1 dBTP |
| `podcast` | -16 LUFS, +-1 LU | -1 dBTP |

```json
{"name": "festival", "integrated_lufs": {"target": -24, "tolerance": 1}, "true_peak_dbtp_max": -2, "loudness_range_max_lu": 15}
```

## Gain, ceiling and what is refused

`normalize` applies one linear gain, writes the file in the input's own sample format, then **reads the written file back and measures that**, so the report states what the bytes on disk measure (`before`, `plan`, `after`, input and output SHA-256, and any `problems`). There is no limiter: every loudness value and the true peak move by exactly the gain, and nothing else about the audio changes.

If the gain needed would put the true peak over the ceiling, `--on-ceiling refuse` (default) writes nothing and exits 2, naming the peak it would cause. `--on-ceiling cap` lowers the gain to meet the ceiling and reports `shortfall_lu`, how far under the target the file stays; a capped file still fails a `check` against a spec it misses, so capping never hides a miss. Integer output that the gain would clip, and silent or under-400 ms input, are refused with exit 2 and nothing written. Reducing the peak itself (limiting) is a creative and technical decision this tool does not make.

## What the tests assert

`tests/studies/test_loudness.py`: standard coefficient table at 48 kHz and the response shape at five rates; the EBU stereo 1 kHz -23 dBFS case reads -23.0; the closed form for a sine; mono is 3 LU under the same stereo; level independence across 32, 44.1, 48 and 96 kHz; a gain moves loudness, true peak and range by exactly the gain; the absolute and relative gates (a test fails when either is disabled); 5.1 weights and LFE exclusion; a between-samples peak found by oversampling (sample peak -3.01, true peak 0 dBTP); loudness range of steady, two-level and gated programmes; spec checks reporting every failure and refusing malformed specs; gain planning (reach, refuse, cap); and agreement with FFmpeg's `ebur128` on invented stereo programme (integrated within 0.05 LU, range within 0.6 LU, true peak within 0.2 dB).

`tests/test_loudness_cli.py`: every sub-command's exit status, the three WAV formats, written-file measurement, refusal and cap with no file written on refusal, clipping refusal, bad input, determinism of the demo.

These are level measurements. They do not say a mix sounds right, that dialogue is intelligible or that a distributor will accept it.
