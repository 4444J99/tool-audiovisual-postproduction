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

Deterministic JSON (no clock, no host): `schema`, `spec`, `status` (`pass` or `fail`), `failed`, `inputs` (SHA-256 of each of the twelve artefacts read: caption report, SRT, WebVTT, burned frames, burn-in report, delivery report, delivered dialogue, final ledger, colour plan, set spec, shot plan, suggested plan), and `checks`: seventeen entries, each with `id`, `ok`, `detail`, `independence` (see below) and the measured facts; and `cannot_fail_independently`, the ids of the checks that pass by construction on an unmodified chain.

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
| `captions.covers_dialogue` | every dialogue-active span, found by the placement detector and by the independent detector, is covered by cues to at least `independent_overlap_min` (0.75): a spoken line with its cue deleted fails, even when every hash, report and the burn-in are re-derived |
| `captions.placement_independent` | every cue lies at least `independent_overlap_min` inside speech heard by the independent detector (below), with a fixed two-frame tolerance a spec cannot widen |
| `captions.max_length` | no cue is longer than `max_cue_frames` |
| `captions.reading_rate` | no cue reads faster than `max_cps` characters per second, measured from the SRT file (line breaks not counted) |
| `captions.inside_edit` | cues are ordered, do not overlap and lie inside the edit |
| `captions.burned_frames` | the frames whose pixels differ from a caption-less render of the same background are exactly the frames the cues name |
| `audio.within_edit` | the delivered dialogue is not longer than the edit |
| `loudness.spec` | the delivered WAV, measured again here (BS.1770-4), meets the spec recorded in the delivery report; deviation from target and true-peak headroom are reported |
| `loudness.report_agreement` | the new measurement matches the delivery report's (`report_tolerance`, default 0.01) and the report describes the delivered file |

**Dialogue-active spans** come from an energy detector on the delivered WAV, not from any earlier stage's opinion: 20 ms frames, digital silence ignored, active when 7 dB above the 10th-percentile level, runs under 0.1 s apart joined, runs under 0.2 s dropped (`spec.vad`). On the invented dialogue this finds four spans inside the four synthetic bursts.

## The QC spec

`delivery_qc.DEFAULT_SPEC`: `max_cue_frames` 120, `max_cps` 17, `span_tolerance_frames` 2, `report_tolerance` 0.01, and the `vad` block. `--qc-spec file.json` overrides any key (unknown keys and invalid values are refused with exit 2). `verify` uses the default unless `--qc-spec` is given, so a report that records a looser spec than the one you declare is named (`records a different QC spec than the one declared`). As with the loudness spec, a forger who re-derives everything under a spec they chose is only caught by declaring the spec you require.

## What changed in the captions stage

The captions are no longer laid on a timeline unrelated to the audio. The `captions` stage now takes the delivered dialogue as an input, finds its dialogue-active spans with a fixed detector (the default `vad`, recorded on the stage with the reading rate, so a `--qc-spec` never changes the artefacts it judges), and lays one short invented sentence into each span (`captions.words_in_spans`; a sentence that cannot be read inside its span at `max_cps` is refused before any file is written). The chain's caption spec is the `broadcast` preset with a 0.4 s minimum duration (`broadcast-short`), because the shortest invented speech span is under half a second. The sentences are still invented; they are not a transcript.

## Independence of the clean pass

On the clean chain the caption placement and `captions.inside_dialogue` use the same detector at its default setting, so a clean pass shows that the stage did what it was written to do, not that the placement is right in any independent sense. The report now says so itself: every check carries `independence: {class, note}` and `cannot_fail_independently` lists the checks whose class is not `independent`:

| Class | Meaning | Checks |
|---|---|---|
| `independent` | can fail for a reason the stage it judges did not guard against | the six `timebase` checks other than `timecode`, `captions.covers_dialogue`, `captions.placement_independent` |
| `shared-detector` | same detector at its default as the captions stage | `captions.inside_dialogue` |
| `shared-limit` | the stage already refuses the same limit | `captions.max_length`, `captions.reading_rate`, `captions.inside_edit` |
| `shared-renderer` | compared with a re-render by the renderer that wrote the frames | `captions.burned_frames` |
| `shared-measurer` | same BS.1770 code as the delivery stage | `loudness.report_agreement` |
| `self-declared` | the spec is read from the artefact being judged | `loudness.spec` |
| `partly-structural` | one half is an identity | `timebase.timecode`: the shot round trip cannot fail, the stamp comparisons can |
| `weak` | fails only in one direction | `audio.within_edit`: shorter audio than the edit passes |

The independent detector (`independent_spans`) uses a different method from the placement detector: 30 ms RMS windows on a 10 ms hop and a two-class Otsu split of the live frame levels, with no percentile and no margin. Its constants are fixed in `INDEPENDENT_DETECTOR`, not in the QC spec, so a `--qc-spec` that loosens `vad` or `span_tolerance_frames` cannot move what it hears. Audio whose 5th-to-95th percentile live levels are under 6 dB apart has no two classes and yields no span, which fails both checks that use it. `independent_overlap_min` may be set from 0.5 to 1 (lower is refused). Known limit, failing closed: where the gaps between lines are digital silence, the live frames are all speech and the split falls between loud and quiet speech, so soft word tails can drop out of the detector's active class and a correctly placed cue can fail `captions.placement_independent`; the invented dialogue carries a room-tone floor, so it does not arise on this chain. It still reads the same delivered WAV, so a forger who swaps in audio that suits the captions defeats every audio-based check; that is the limit of a consistency judgement.

## Tamper detection

Four layers, each tested (`tests/test_delivery_qc.py`):

1. **Hashes.** An edit to `qc-report.json` is named by the manifest.
2. **Input pinning.** The report records the hash of every artefact it read; a report made from a different caption file, ledger or WAV is named by which artefact differs.
3. **Re-derivation.** The report must equal what the artefacts derive now. A forger who moves a cue out of the dialogue, rewrites the SRT, WebVTT, caption report, re-renders the burned frames and the burn-in chain and re-seals every manifest hash passes the caption and burn-in checks and is still named, because `captions.inside_dialogue` fails. If the forger also re-derives the report honestly, it says `fail`; if they keep the old `pass` report, it differs from the derivation; if they write a `pass` report under a loosened spec, `verify` names the spec.
4. **Refusal.** `run` raises instead of sealing when any check fails, so a sealed chain always carries a passing report (the failing report is still written to `qc/qc-report.json` for inspection, with no manifest beside it).

## What this does not show

It is a consistency judgement over invented material. It does not say the mix is good, that the cues say what was said (the captions are invented), that a distributor accepts the files, or that the timecode matches a drop-frame master (labels are non-drop at the nominal rate). The loudness check trusts the delivery report's spec unless `av_pipeline.py verify --delivery <spec>` declares the required one.
