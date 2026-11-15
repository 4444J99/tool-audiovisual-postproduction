"""End-to-end synthetic chain across the post-production tools, with every hand-off pinned by a hash.

    set spec -> shot plan -> coverage matrix (-> suggested shots) -> room-tone fill -> colour/compositing plan

Each stage reads only artefacts an earlier stage wrote and records the SHA-256 of what it read and what it
wrote.  ``verify`` recomputes every hash from the files on disk and checks that every input of every stage
is an output of an earlier stage, so a changed artefact, a swapped file or a stage fed from the wrong
upstream is reported by name.  Everything is invented: the set, the performers, the dialogue stand-in and
the plates; no private media, names or set specifications are involved.

The plate sources of the colour plan are *declared* identities (a hash of the set graph and the shot's
ledger row), because no render exists in this chain; the manifest says so in ``plate_identity``.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from . import colour_grade as cg
from . import coverage_matrix as cm
from . import layer_plan as lp
from . import room_tone_fill as rt
from . import set_spec as ss
from . import shot_plan as sp

SCHEMA = "amps.pipeline_chain.v1"
SAMPLE_RATE = 16000
STAGES = ("set", "shot_plan", "dialogue", "requirement", "coverage", "room_tone", "final_ledger", "colour")


class ChainError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(value))
    return sha256_file(path)


def pcm16_bytes(audio: np.ndarray, sr: int) -> bytes:
    """A canonical 16-bit mono WAV (44-byte header + samples), so the file hash is reproducible."""
    data = np.clip(np.round(audio * 32768.0), -32768, 32767).astype("<i2").tobytes()
    header = (b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
              + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + sr.to_bytes(4, "little")
              + (sr * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
              + b"data" + len(data).to_bytes(4, "little"))
    return header + data


def read_pcm16(path: Path) -> tuple[int, np.ndarray]:
    raw = Path(path).read_bytes()
    sr = int.from_bytes(raw[24:28], "little")
    return sr, np.frombuffer(raw[44:], dtype="<i2").astype(np.float64) / 32768.0


# ------------------------------------------------------------------------------------------ inputs

def synthetic_set_spec() -> dict[str, Any]:
    spec = ss.synthetic_spec()
    spec["caches"] = [{"id": "flame", "frames": [1, 240], "kind": "physical"}]
    return spec


def synthetic_shot_plan(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Three shots on the spec's own set block; every added element is used by a shot."""
    plan = sp.synthetic_plan()
    plan["title"] = "synthetic chain pilot"
    plan["set"] = ss.shot_plan_set(spec)
    plan["performers"] = [{"id": "A", "position": [-1.6, 1.0], "facing_toward": "B"},
                          {"id": "B", "position": [1.6, 1.0], "facing_toward": "A"}]
    uses = [[{"id": "table"}, {"id": "flame"}, {"id": "arch", "contribution": "visible"},
             {"id": "brazier", "contribution": "visible"}],
            [{"id": "flame", "contribution": "shadow"}, {"id": "arch", "contribution": "reflection"}],
            [{"id": "flame"}]]
    for shot, use in zip(plan["shots"], uses):
        shot["uses"] = use
    return plan


def requirement(plan: Mapping[str, Any], tone_seconds: float) -> dict[str, Any]:
    """Coverage requirement: lines inside the plan's shots, a cell the pilot lacks, measured room tone."""
    return {"schema": cm.SCHEMA,
            "required": [{"actor": "B", "size": "close_up", "angle": "three_quarter"},
                         {"actor": "A", "size": "medium", "angle": "front"},
                         {"actor": "B", "size": "wide"}],
            "lines": [{"id": "L1", "actor": "B", "frames": [100, 150]}, {"id": "L2", "actor": "A", "frames": [20, 60]}],
            "room_tone": [{"set": plan["set"]["id"], "seconds": round(tone_seconds, 4)}],
            "room_tone_min_s": 2.0}


# ------------------------------------------------------------------------------------------ stages

def plate_hash(graph_sha256: str, row: Mapping[str, Any]) -> str:
    return sha256_bytes(ss.canonical({"set_graph_sha256": graph_sha256, "shot": row}).encode())


def colour_plan(ledger: Mapping[str, Any], graph_sha256: str, audio_sha256: str, fps: int) -> dict[str, Any]:
    """One plate layer per shot at its edit position, a warm glow on the flame shots, a closing grade."""
    total = ledger["total_frames"]
    sources: dict[str, Any] = {}
    layers: list[dict[str, Any]] = []
    for row in ledger["shots"]:
        sid = f"plate-{row['id'].lower()}"
        sources[sid] = {"kind": "render", "path": row["render"]["output"], "colourspace": "linear",
                        "sha256": plate_hash(graph_sha256, row)}
        layers.append({"id": f"shot-{row['id'].lower()}", "kind": "plate", "source": sid,
                       "frames": {"start": row["edit_in"], "end": row["edit_out"]}})
    sources["dialogue"] = {"kind": "audio", "path": "audio/dialogue-filled.wav", "sha256": audio_sha256}
    sources["glow"] = {"kind": "render", "path": "renders/flame-glow.exr", "colourspace": "linear",
                       "sha256": sha256_bytes(f"glow:{graph_sha256}".encode())}
    layers.append({"id": "flame", "kind": "fx", "source": "glow", "blend": "screen", "chain": "warm",
                   "frames": {"start": 0, "end": total}, "opacity": [[0, 0.0], [12, 0.7], [total - 12, 0.7], [total - 1, 0.0]]})
    layers.append({"id": "look", "kind": "adjustment", "chain": "film", "frames": {"start": 0, "end": total}})
    return {"schema": lp.SCHEMA, "comp": {"width": 64, "height": 36, "fps": fps, "frames": total, "working_space": "linear"},
            "sources": sources,
            "chains": {"warm": {"schema": cg.SCHEMA, "ops": [{"op": "cdl", "slope": [1.1, 1.0, 0.85], "offset": [0, 0, 0],
                                                              "power": [1, 1, 1], "saturation": 1.0}]},
                       "film": cg.synthetic_chain()},
            "layers": layers}


def run_chain(out: Path, *, seed: int = 0) -> dict[str, Any]:
    """Run every stage into ``out`` and write ``pipeline-manifest.json``; returns the manifest."""
    out = Path(out)
    stages: list[dict[str, Any]] = []

    def stage(name: str, inputs: dict[str, str], outputs: dict[str, str], **facts: Any) -> None:
        stages.append({"stage": name, "inputs": inputs, "outputs": outputs, **facts})

    # 1. set spec -> scene graph + shot-plan set block
    spec = synthetic_set_spec()
    report = ss.validate_spec(spec)
    if report["errors"]:
        raise ChainError([f"set spec: {e}" for e in report["errors"]])
    graph = ss.scene_graph(spec)
    summary = ss.graph_summary(graph)
    spec_h = write_json(out / "set" / "set-spec.json", spec)
    graph_h = write_json(out / "set" / "scene-graph-summary.json", summary)
    block_h = write_json(out / "set" / "shot-plan-set.json", ss.shot_plan_set(spec))
    stage("set", {}, {"set-spec": spec_h, "scene-graph-summary": graph_h, "shot-plan-set": block_h},
          spec_sha256=ss.spec_hash(spec), graph_sha256=summary["graph_sha256"], meshes=summary["mesh_count"])

    # 2. shot plan on that set block
    set_block = json.loads((out / "set" / "shot-plan-set.json").read_text())
    plan = synthetic_shot_plan(spec)
    if plan["set"] != set_block:
        raise ChainError(["shot plan: the set block differs from the one the set stage exported"])
    plan_report = sp.validate_plan(plan)
    if plan_report["errors"]:
        raise ChainError([f"shot plan: {e}" for e in plan_report["errors"]])
    plan_h = write_json(out / "plan" / "shot-plan.json", plan)
    stage("shot_plan", {"shot-plan-set": block_h}, {"shot-plan": plan_h}, plan_sha256=sp.plan_hash(plan),
          warnings=plan_report["warnings"])

    # 3. dialogue stand-in -> measured room tone -> coverage requirement
    dialogue, meta = rt.synthetic_dialogue(sr=SAMPLE_RATE)
    wav_in = pcm16_bytes(dialogue, SAMPLE_RATE)
    (out / "audio").mkdir(parents=True, exist_ok=True)
    (out / "audio" / "dialogue-with-holes.wav").write_bytes(wav_in)
    dialogue_h = sha256_bytes(wav_in)
    sr, heard = read_pcm16(out / "audio" / "dialogue-with-holes.wav")
    regions = rt.find_tone_regions(heard, sr)
    tone_s = sum(e - s for s, e in regions) / sr
    stage("dialogue", {}, {"dialogue-with-holes": dialogue_h}, sample_rate=sr, holes_s=meta["holes_s"],
          tone_regions=len(regions), source="rt.synthetic_dialogue: invented harmonic bursts over coloured room tone")
    need = requirement(plan, tone_s)
    need_h = write_json(out / "coverage" / "requirement.json", need)
    stage("requirement", {"shot-plan": plan_h, "dialogue-with-holes": dialogue_h}, {"requirement": need_h},
          tone_seconds=round(tone_s, 4), room_tone_min_s=need["room_tone_min_s"])
    coverage = cm.validate_coverage(plan, need)
    suggestion = cm.suggest_shots(plan, need)
    final_plan = suggestion["plan"]
    after = cm.validate_coverage(final_plan, need)
    if after["errors"]:
        raise ChainError([f"coverage after suggestions: {e}" for e in after["errors"]])
    report_h = write_json(out / "coverage" / "coverage-report.json", coverage)
    final_h = write_json(out / "coverage" / "suggested-plan.json", final_plan)
    table = cm.render_table(after).encode()
    (out / "coverage" / "coverage-matrix.md").write_bytes(table)
    table_h = sha256_bytes(table)
    stage("coverage", {"shot-plan": plan_h, "requirement": need_h},
          {"coverage-report": report_h, "suggested-plan": final_h, "coverage-matrix": table_h},
          gaps_before=len(coverage["gaps"]), shots_suggested=len(suggestion["shots"]),
          errors_after=after["errors"], final_plan_sha256=sp.plan_hash(final_plan))

    # 4. room-tone fill on the same dialogue
    filled, fill = rt.fill_holes(heard, sr, seed=seed)
    fill_report = rt.fill_report(heard, filled, sr, fill)
    wav_out = pcm16_bytes(filled, sr)
    (out / "audio" / "dialogue-filled.wav").write_bytes(wav_out)
    filled_h = sha256_bytes(wav_out)
    fill_h = write_json(out / "audio" / "fill-report.json", fill_report)
    stage("room_tone", {"dialogue-with-holes": dialogue_h}, {"dialogue-filled": filled_h, "fill-report": fill_h},
          holes_filled=fill_report["holes_filled"], remaining_digital_silence=fill_report["remaining_digital_silence"],
          untouched_identical=fill_report["untouched_identical"])

    # 5. colour / compositing plan over the final plan's edit, the filled dialogue and the set graph
    ledger = sp.build_ledger(final_plan)
    ledger_h = write_json(out / "plan" / "final-ledger.json", ledger)
    stage("final_ledger", {"suggested-plan": final_h}, {"final-ledger": ledger_h}, total_frames=ledger["total_frames"])
    cplan = colour_plan(ledger, summary["graph_sha256"], filled_h, final_plan["fps"])
    creport = lp.validate_plan(cplan)
    if creport["errors"]:
        raise ChainError([f"colour plan: {e}" for e in creport["errors"]])
    cplan_h = write_json(out / "colour" / "layer-plan.json", cplan)
    cledger = lp.layer_ledger(cplan)
    cledger_h = write_json(out / "colour" / "layer-ledger.json", cledger)
    stage("colour", {"suggested-plan": final_h, "final-ledger": ledger_h, "scene-graph-summary": graph_h,
                     "dialogue-filled": filled_h},
          {"layer-plan": cplan_h, "layer-ledger": cledger_h},
          layer_plan_sha256=lp.plan_hash(cplan), edit_frames=ledger["total_frames"], warnings=creport["warnings"],
          plate_identity="declared: sha256 of the set graph hash and the shot ledger row; no render exists in this chain")
    manifest = {"schema": SCHEMA, "stages": stages}
    manifest["chain_sha256"] = chain_hash(manifest)
    write_json(out / "pipeline-manifest.json", manifest)
    return manifest


def chain_hash(manifest: Mapping[str, Any]) -> str:
    body = {k: v for k, v in manifest.items() if k != "chain_sha256"}
    return sha256_bytes(ss.canonical(body).encode())


LOCATIONS = {"set-spec": "set/set-spec.json", "scene-graph-summary": "set/scene-graph-summary.json",
             "shot-plan-set": "set/shot-plan-set.json", "shot-plan": "plan/shot-plan.json",
             "dialogue-with-holes": "audio/dialogue-with-holes.wav", "requirement": "coverage/requirement.json",
             "coverage-report": "coverage/coverage-report.json", "coverage-matrix": "coverage/coverage-matrix.md", "suggested-plan": "coverage/suggested-plan.json",
             "dialogue-filled": "audio/dialogue-filled.wav", "fill-report": "audio/fill-report.json",
             "final-ledger": "plan/final-ledger.json", "layer-plan": "colour/layer-plan.json",
             "layer-ledger": "colour/layer-ledger.json"}


def verify_chain(out: Path) -> list[str]:
    """Every way the artefacts under ``out`` disagree with the manifest; empty when the chain holds."""
    out = Path(out)
    try:
        manifest = json.loads((out / "pipeline-manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        return [f"manifest: cannot read ({error})"]
    problems: list[str] = []
    if manifest.get("schema") != SCHEMA:
        return [f"manifest: schema must be {SCHEMA}"]
    if manifest.get("chain_sha256") != chain_hash(manifest):
        problems.append("manifest: chain_sha256 does not match its stages")
    produced: dict[str, str] = {}
    for entry in manifest.get("stages", []):
        name = entry["stage"]
        for label, digest in entry["inputs"].items():
            if produced.get(label) != digest:
                problems.append(f"{name}: input {label} is {digest[:12]} but the upstream output is "
                                f"{(produced.get(label) or 'absent')[:12]}")
        for label, digest in entry["outputs"].items():
            path = out / LOCATIONS[label]
            actual = sha256_file(path) if path.is_file() else None
            if actual != digest:
                problems.append(f"{name}: output {label} on disk is {(actual or 'missing')[:12]}, manifest says {digest[:12]}")
            produced[label] = digest
    return problems


def cross_checks(out: Path) -> list[str]:
    """Content links the hashes alone do not show: the same set id, plan hash, graph hash and audio hash."""
    out = Path(out)
    plan = json.loads((out / LOCATIONS["suggested-plan"]).read_text())
    cplan = json.loads((out / LOCATIONS["layer-plan"]).read_text())
    summary = json.loads((out / LOCATIONS["scene-graph-summary"]).read_text())
    ledger = json.loads((out / LOCATIONS["final-ledger"]).read_text())
    problems = []
    if ledger["plan_sha256"] != sp.plan_hash(plan):
        problems.append("final ledger was not derived from the suggested plan")
    if cplan["sources"]["dialogue"]["sha256"] != sha256_file(out / LOCATIONS["dialogue-filled"]):
        problems.append("colour plan dialogue source is not the filled dialogue")
    for row in ledger["shots"]:
        if cplan["sources"][f"plate-{row['id'].lower()}"]["sha256"] != plate_hash(summary["graph_sha256"], row):
            problems.append(f"plate for {row['id']} is not derived from the set graph and its ledger row")
    if {s["id"] for s in plan["shots"]} != {r["id"] for r in ledger["shots"]}:
        problems.append("ledger shots differ from the suggested plan's shots")
    return problems
