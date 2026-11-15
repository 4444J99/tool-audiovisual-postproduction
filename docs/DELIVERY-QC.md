# Delivery QC

`amp/studies/delivery_qc.py` is the last stage of the hash-chained pipeline ([PIPELINE-CHAIN](PIPELINE-CHAIN.md)). It reads **only the sealed artefacts** under the output directory, re-computes every number it reports from those bytes, and writes `qc/qc-report.json`. The report is a hashed output of the `qc` stage, so the chain hash covers it; `verify` re-derives it and compares byte for byte.

```bash
python tools/av_pipeline.py run --output outputs/av-pipeline [--qc-spec qc.json]     # refuses to seal a chain that fails its QC
python tools/av_pipeline.py verify --output outputs/av-pipeline [--qc-spec qc.json]  # re-derives qc-report.json
python tools/delivery_qc.py check --output outputs/av-pipeline [--qc-spec qc.json] [--write report.json]   # look at a failing delivery; exit 2 on any failed check
python tools/delivery_qc.py verify --output outputs/av-pipeline [--qc-spec qc.json]
```

"Hashed into the final ledger" is read here as: the report is a stage output of the chain's last stage and `chain_sha256` covers the manifest that lists it. `plan/final-ledger.json` is the edit's per-shot ledger and is written before the captions exist, so it is not changed.

## The report

Deterministic JSON (no clock, no host): `schema`, `spec`, `status` (`pass` or `fail`), `failed`, `inputs` (SHA-256 of each of the eleven artefacts read: caption report, SRT, WebVTT, burned frames, burn-in report, delivery report, delivered dialogue, final ledger, colour plan, set spec, shot plan, suggested plan), and `checks`: fifteen entries, each with `id`, `ok`, `detail` and the measured facts.

| Check | What it re-derives and requires |
|---|---|
| `timebase.frame_rate` | shot plan, final ledger, colour plan, caption report and burn-in parameters carry one exact rate (`24000/1001` is not `24`) |
| `timebase.edit_contiguous` | the ledger's shots tile frame 0 to the total, no gap or overlap |
| `timebase.edit_length` | ledger frames, colour plan frames, burn-in frames and the frame file's byte size (`frames x width x height`) give one length; the ledger's seconds are frames over the rate |
| `timebase.burnin_segments` | burn-in background segments tile the edit |
| `timebase.shot_lengths` | per shot: the suggested plan's frames are the edit length and the ledger's scene frames; render frames are the plan range plus its handles; the colour plate layer is the shot's edit range |
| `timebase.set_caches` | each cache named by a shot exists in the set spec and covers the shot's render frames **including handles**; the plan's set block lists the same caches. The set spec has no frame rate of its own, so its frame numbers are read at the plan's rate |
| `timebase.timecode` | shot boundaries convert to non-drop `HH:MM:SS:FF` and back (the label uses the nominal integer rate; the exact rate is recorded); every SRT stamp converts to the frame the caption and burn-in reports record; the WebVTT stamps stand for the same frames |
| `captions.inside_dialogue` | every cue lies inside one dialogue-active span of the **delivered WAV**, within `span_tolerance_frames` |
| `captions.max_length` | no cue is longer than `max_cue_frames` |
| `captions.reading_rate` | no cue reads faster than `max_cps` characters per second, measured from the SRT file (line breaks not counted) |
| `captions.inside_edit` | cues are ordered, do not overlap and lie inside the edit |
| `captions.burned_frames` | the frames whose pixels differ from a caption-less render of the same background are exactly the frames the cues name, and every frame equals a re-render of the SRT's text (other text on the right frames fails; a caption the renderer cannot draw fails instead of raising) |
| `audio.within_edit` | the delivered dialogue is not longer than the edit |
| `loudness.spec` | the delivered WAV, measured again here (BS.1770-4), meets the spec recorded in the delivery report; deviation from target and true-peak headroom are reported |
| `loudness.report_agreement` | the new measurement matches the delivery report's (`report_tolerance`, default 0.01) and the report describes the delivered file |

**Dialogue-active spans** come from an energy detector on the delivered WAV, not from any earlier stage's opinion: 20 ms frames, digital silence ignored, active when 7 dB above the 10th-percentile level, runs under 0.1 s apart joined, runs under 0.2 s dropped (`spec.vad`). On the invented dialogue this finds four spans inside the four synthetic bursts.

## The QC spec

`delivery_qc.DEFAULT_SPEC`: `max_cue_frames` 120, `max_cps` 17, `span_tolerance_frames` 2, `report_tolerance` 0.01, and the `vad` block. `--qc-spec file.json` overrides any key (unknown keys and invalid values are refused with exit 2). `verify` uses the default unless `--qc-spec` is given, so a report that records a looser spec than the one you declare is named (`records a different QC spec than the one declared`). As with the loudness spec, a forger who re-derives everything under a spec they chose is only caught by declaring the spec you require.

## What changed in the captions stage

The captions are no longer laid on a timeline unrelated to the audio. The `captions` stage now takes the delivered dialogue as an input, finds its dialogue-active spans with a fixed detector (the default `vad`, recorded on the stage with the reading rate, so a `--qc-spec` never changes the artefacts it judges), and lays one short invented sentence into each span (`captions.words_in_spans`; a sentence that cannot be read inside its span at `max_cps` is refused before any file is written). The chain's caption spec is the `broadcast` preset with a 0.4 s minimum duration (`broadcast-short`), because the shortest invented speech span is under half a second. The sentences are still invented; they are not a transcript.

## Independence of the clean pass

On the clean chain the caption placement and `captions.inside_dialogue` use the same detector at its default setting, so a clean pass is close to tautological: it shows that the stage did what it was written to do, not that the placement is right in any independent sense. The check earns its keep under change: a cue moved after the fact, a different `--qc-spec` detector, a swapped WAV or an edited caption file all fail it, because the judgement is re-derived from the bytes on disk rather than read from the stage's record.

## Tamper detection

Four layers, each tested (`tests/test_delivery_qc.py`):

1. **Hashes.** An edit to `qc-report.json` is named by the manifest.
2. **Input pinning.** The report records the hash of every artefact it read; a report made from a different caption file, ledger or WAV is named by which artefact differs.
3. **Re-derivation.** The report must equal what the artefacts derive now. A forger who moves a cue out of the dialogue, rewrites the SRT, WebVTT, caption report, re-renders the burned frames and the burn-in chain and re-seals every manifest hash passes the caption and burn-in checks and is still named, because `captions.inside_dialogue` fails. If the forger also re-derives the report honestly, it says `fail`; if they keep the old `pass` report, it differs from the derivation; if they write a `pass` report under a loosened spec, `verify` names the spec.
4. **Refusal.** `run` raises instead of sealing when any check fails, so a sealed chain always carries a passing report (the failing report is still written to `qc/qc-report.json` for inspection, with no manifest beside it).

## What this does not show

It is a consistency judgement over invented material. It does not say the mix is good, that the cues say what was said (the captions are invented), that a distributor accepts the files, or that the timecode matches a drop-frame master (labels are non-drop at the nominal rate). The loudness check trusts the delivery report's spec unless `av_pipeline.py verify --delivery <spec>` declares the required one.
