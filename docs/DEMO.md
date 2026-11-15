# Short-film demo: the whole chain from one command

`tools/short_film_demo.py` runs every stage of the synthetic chain ([PIPELINE-CHAIN](PIPELINE-CHAIN.md): set spec, shot plan, coverage, room-tone fill, loudness delivery, colour plan, captions, caption burn-in, delivery QC), verifies the chain, and muxes its burned-in frames and delivered dialogue into one small mp4. The mp4 is then judged against the sealed chain. Everything is invented; nothing is played (ffmpeg only writes and reads files, no audio device and no browser).

## Command

```bash
python tools/short_film_demo.py run --output outputs/short-film-demo
python tools/short_film_demo.py verify --output outputs/short-film-demo
```

Needs NumPy and SciPy (the chain's loudness stage) and an ffmpeg with `libx264` and `aac`. `outputs/` is git-ignored, so the output is scratch. `--delivery` (a preset or spec JSON) and `--qc-spec` pass through to the chain. A non-empty `--output` that is not a previous demo output is refused and left alone; a previous demo output is replaced.

## What it writes (`outputs/short-film-demo/`)

| File | What it is |
|---|---|
| `short-film.mp4` | 26.5 s, 240x48, exactly 24 fps, 636 frames, H.264 (lossless, yuv420p) + AAC 16 kHz mono (the 8 s of delivered dialogue, padded with silence to the picture length). About 145 KB. |
| `demo-hashes.json` | SHA-256 of every file written, the chain hash, the stage list and the ffmpeg build that made the mp4 |
| `demo-qc.json` | the mp4 judged against the chain (below) |
| `qc/qc-report.json` | the chain's own delivery QC ([DELIVERY-QC](DELIVERY-QC.md)) |
| everything else | the chain's artefacts, listed in [PIPELINE-CHAIN](PIPELINE-CHAIN.md) |

`demo-qc.json` checks: one video and one audio stream; H.264 at the burn-in geometry and the ledger's exact frame rate; AAC 16 kHz mono; container duration equals the final ledger's seconds; frame count equals the ledger's; **the decoded luma plane equals `captions/frames.y8` byte for byte** (the video is lossless, and the chroma planes are flat grey); and the mp4's audio measures within 1 LU of the delivered dialogue (AAC is lossy, so this is a tolerance, observed -22.93 against -23.00 LUFS).

## Expected hashes

These were observed on 2026-11-15 (macOS arm64, Python 3.14, NumPy/SciPy as installed, ffmpeg 9.0.2 Homebrew with libx264). The rows above the mp4 come from Python and NumPy arithmetic and are what a stale-doc test in `tests/test_short_film_demo.py` compares against. The frames, delivered dialogue, QC report and captions hashes were identical on both platforms; only the chain hash differs between them (the manifest records measured floating-point loudness fields), so both values are listed and the test accepts the platform it runs on.

| Output | SHA-256 |
|---|---|
| chain (`pipeline-manifest.json` hash), macOS arm64 | `71053aa9a10d7fdb97b556d38f19e9de34a20cfd0bf48666e063d7f9f8404321` |
| chain, Linux x86_64 Python 3.12 (CI runner) | `a445ad783117cf2bc6dc77484a93e0e1cb13678c5c66ca46aefe7c122c5fd142` |
| burned-in frames (`captions/frames.y8`) | `3215d7b1177fdd06feba51d60cc7a0f9d85b32ce492dab368c519dd9fc411c83` |
| delivered dialogue (`audio/dialogue-delivered.wav`) | `40bef4b2a87ecdd5d850b7c0687a848d873e160bf92210765c371b6c57043f0b` |
| chain QC report (`qc/qc-report.json`) | `89bfbe59de8e2770bd601a52c7abc5bdc9aa3a108c53ef27b9ad63f0352358d7` |
| captions (`captions/captions.srt`) | `8de8f3861419cd913521044a04c3dd3c2fe2fc3200cc22e63ee3c42681dfb16a` |
| `short-film.mp4` (this ffmpeg build only) | `a22adfc7d9f192dcff05535e9961ee2cacf46662bd03cbb294498db516a6640e` |

The mp4 hash is not pinned anywhere: it depends on the ffmpeg, libx264 and AAC encoder build, so another build may write different bytes for the same picture and sound. What does not change is `mp4.luma_equals_burnin`, which ties the picture to `frames.y8` whatever encoder wrote it. Encoding uses `-threads 1`, bit-exact flags, no metadata and `-qp 0`, which is what makes the bytes repeat on one build.

## What the test asserts

`tests/test_short_film_demo.py` runs the command twice into two directories and asserts: both exit 0 and print the same summary; `demo-hashes.json`, the mp4 and every other written file are byte-identical across the two runs; the whole twelve-stage chain ran and its QC report passes; every mp4 check passes; `verify` accepts a fresh run; a flipped mp4 byte, an edited caption, and a lossy re-encoded mp4 whose hash file was re-sealed (named by the decoded-luma check) are each refused; a foreign non-empty directory is refused untouched; a rerun over its own output gives the same hashes; a delivery spec the audio cannot meet (`streaming`) writes no mp4; and the hashes in this document are the ones the run produces. The test is skipped where ffmpeg lacks `libx264` or `aac`.

## What this does not show

That the film looks or sounds good (the picture is a 5x7 test font over an invented ramp; the dialogue is a synthetic stand-in), that the mp4 meets any distributor's specification, or how a player renders it. It shows the files the chain sealed are the files in the mp4, and that the whole run repeats.
