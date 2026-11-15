"""The effect chains of the generated network: their parameter expressions, evaluated for real.

TouchDesigner parameter expressions are Python, so each expression in the generated spec can be evaluated here
against a stand-in for the objects it reads (the processor's `resolved` table, the rack's Time, sibling operators,
`project.cookRate`, `me.inputVal`). That checks the arithmetic of every effect (tap gains, the gate's duty cycle,
bit-depth steps, mosaic cell counts) at the catalog's minimum, default and maximum for every parameter, and that the
chains are wired the way the processors are described. It does not check what TouchDesigner's operators do with the
numbers: that needs a TouchDesigner session (docs/FX-TOUCHDESIGNER.md, Verification status).
"""
import itertools
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "web" / "fx" / "fixtures"
IMPLEMENTED = ["damage", "delay", "displacement", "feedback", "freeze", "gate"]


def spec_for(patch_path):
    directory = Path(tempfile.mkdtemp(prefix="td-fx-"))
    result = subprocess.run(["node", str(ROOT / "tools" / "fx-td-network.mjs"), str(patch_path), "--out", str(directory)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(next(directory.glob("*.network.json")).read_text())


def single_patch(type_name, targets=None):
    """A one-processor patch for a processor type, written by the workbench's own factory."""
    script = ("import {freshPatch} from './tests/fx/runtime.mjs'; const patch = freshPatch(%s); const targets = %s; "
              "if (targets) patch.nodes.find(node => node.id === 'fx-1').targets = targets; process.stdout.write(JSON.stringify(patch));" % (json.dumps(type_name), json.dumps(targets)))
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    handle.write(result.stdout)
    handle.close()
    return handle.name


class Cell:
    def __init__(self, val):
        self.val = val


class Chan:
    def __init__(self, value):
        self.value = value

    def eval(self):
        return self.value


class Sized:
    width, height = 1920, 1080

    def __getitem__(self, index):
        return Chan(0.0)


class Table:
    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, key):
        return Cell(self.rows[key[0]])


class Channels(list):
    @property
    def numChans(self):
        return len(self)


class Audio:
    """Two source channels; indexing a channel by sample gives that sample."""
    def __init__(self, left, right, rate=48000.0):
        self.channels = [left, right]
        self.numChans = 2
        self.rate = rate

    def __getitem__(self, index):
        return self.channels[index]


class Comp:
    def __init__(self, rows, time, control, gate):
        self.table, self.control, self.gate = Table(rows), control, gate
        self.audio = Audio([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
        self.par = type("P", (), {"Time": time})()

    def op(self, name):
        if name == "resolved":
            return self.table
        if name == "in_control":
            return Channels() if self.control is None else Channels([Chan(self.control)])
        if name == "gate_ctrl":
            return [Chan(self.gate)]
        if name == "in_picture":
            return Sized()
        if name in ("audio", "dmg_drive"):   # a CHOP read sample by sample: the source, or the driven signal (same stand-in samples)
            return self.audio
        raise KeyError(name)

    def parent(self):
        return self


class Project:
    cookRate = 60.0


class Me:
    """The members of an Expression CHOP's `me` (docs.derivative.ca ExpressionCHOP_Class): inputVal, sampleIndex, chanIndex."""
    def __init__(self, value, index=0, chan=0):
        self.inputVal, self.sampleIndex, self.chanIndex = value, index, chan


def evaluate(text, rows, time=0.0, control=0.0, gate=1.0, sample=0.0, audio=None, index=0, rate=48000.0, chan=0):
    comp = Comp(rows, time, control, gate)
    if audio:
        comp.audio = Audio(*audio, rate=rate)
    return eval(text, {"parent": lambda: comp, "project": Project, "me": Me(sample, index, chan), "math": math, "float": float, "int": int, "max": max, "min": min, "round": round})


def rows_for(definition_params, picks):
    rows = {"intensity": 1.0, "wet": 1.0, "bypass": 0, "solo": 0, "active": 1, "epoch": 0.0}
    for key, value in picks.items():
        rows["param." + key] = value
    return rows


def catalog(type_name):
    script = ("import {CATALOG_BY_TYPE} from './web/fx/catalog.mjs'; process.stdout.write(JSON.stringify(CATALOG_BY_TYPE[%s].params));" % json.dumps(type_name))
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, cwd=ROOT, check=False)
    return json.loads(result.stdout)


class EffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.specs = {t: spec_for(single_patch(t)) for t in IMPLEMENTED}
        cls.params = {t: catalog(t) for t in IMPLEMENTED}

    def chain(self, type_name):
        return [o for o in self.specs[type_name]["operators"] if o["parent"].startswith("fx_") and o["parent"].count("/") == 0 and o["name"] not in ("resolved",) and o["path"].split("/")[1].startswith(("dmg_", "dly_", "gate_", "fb_", "dsp_", "fz_"))]

    def test_every_effect_expression_is_finite_at_the_catalog_minimum_default_and_maximum(self):
        evaluated = 0
        for type_name in IMPLEMENTED:
            params = self.params[type_name]
            for corner in ("min", "default", "max"):
                rows = rows_for(params, {p["key"]: p[corner] for p in params})
                for operator in self.chain(type_name):
                    for name, value in operator["pars"].items():
                        if isinstance(value, dict) and "resolved" in value:
                            for time, control, sample in itertools.product((0.0, 3.7), (0.0, 1.0), (-1.0, 0.3)):
                                result = evaluate(value["expr"], rows, time, control, 0.5, sample)
                                self.assertTrue(math.isfinite(float(result)), "%s %s %s %s -> %r" % (type_name, corner, operator["path"], name, result))
                                evaluated += 1
        self.assertGreater(evaluated, 1000)

    def test_every_implemented_processor_has_a_chain_into_its_proc_slot(self):
        for type_name in IMPLEMENTED:
            spec = self.specs[type_name]
            by_name = {o["name"]: o for o in spec["operators"] if o["parent"].startswith("fx_") and "/" not in o["parent"]}
            for slot, entry in (("proc_picture", "in_picture"), ("proc_audio", "in_audio")):
                implemented = next(e for e in spec["effects"] if e["type"] == type_name)["picture" if slot == "proc_picture" else "audio"]
                source = by_name[slot]["inputs"][0]
                self.assertEqual(source == entry, not implemented, "%s %s" % (type_name, slot))
                # walk upstream from the slot: an implemented chain reaches the input without leaving the component
                seen, frontier = set(), [source]
                while frontier:
                    name = frontier.pop()
                    if name in seen:
                        continue
                    seen.add(name)
                    frontier += by_name[name]["inputs"]
                if implemented:
                    self.assertIn(entry, seen, "%s %s is not fed by the input" % (type_name, slot))

    def test_delay_tap_gains_follow_decay_to_the_tap_number_and_stop_at_the_tap_count(self):
        rows = rows_for(None, {"time": 0.5, "taps": 3, "decay": 0.5, "spread": 0, "scale": 1})
        gains = {o["name"]: evaluate(o["pars"]["gain"]["expr"], rows) for o in self.chain("delay") if o["name"].startswith("dly_gain_")}
        self.assertEqual([gains["dly_gain_%d" % i] for i in range(8)], [0.5, 0.25, 0.125, 0, 0, 0, 0, 0])
        delays = {o["name"]: evaluate(o["pars"]["delay"]["expr"], rows) for o in self.chain("delay") if o["name"].startswith("dly_tap_")}
        self.assertEqual([delays["dly_tap_%d" % i] for i in range(4)], [0.5, 1.0, 1.5, 2.0])
        frame = {o["name"]: evaluate(o["pars"]["index"]["expr"], rows) for o in self.chain("delay") if o["name"].startswith("dly_frame_")}
        self.assertEqual([frame["dly_frame_%d" % i] for i in range(3)], [-30, -60, -90])
        by_name = {o["name"]: o for o in self.chain("delay")}
        self.assertNotIn("in_audio", by_name["dly_sum"]["inputs"])   # a fully wet delay is silent before its first tap
        self.assertEqual(len(by_name["dly_sum"]["inputs"]), 8)

    def test_a_picture_tap_beyond_the_cache_is_switched_off_not_collapsed_onto_the_oldest_frame(self):
        by_name = {o["name"]: o for o in self.chain("delay")}
        rows = rows_for(None, {"time": 2.0, "taps": 8, "decay": 0.9, "spread": 0, "scale": 1})   # 120 frames per tap at 60 fps; the cache holds 240
        offsets = [evaluate(by_name["dly_frame_%d" % i]["pars"]["index"]["expr"], rows) for i in range(8)]
        opacities = [evaluate(by_name["dly_copy_%d" % i]["pars"]["opacity"]["expr"], rows) for i in range(8)]
        self.assertEqual(offsets[:2], [-120, -239])
        self.assertEqual([o > 0 for o in opacities], [True, False, False, False, False, False, False, False])
        self.assertEqual(by_name["dly_cache"]["pars"]["cachesize"], 240)

    def test_the_gate_opens_for_its_duty_fraction_of_each_period(self):
        raw = next(o for o in self.chain("gate") if o["name"] == "gate_raw")["pars"]["const0value"]["expr"]
        rows = rows_for(None, {"duty": 0.25, "period": 2.0, "edge": 0.01, "phase": 0.0, "retain": 0.1, "threshold": 0.5, "detector": 0})
        opened = [evaluate(raw, rows, time=t / 100) for t in range(0, 400)]
        self.assertAlmostEqual(sum(1 for v in opened if v > 0.5) / len(opened), 0.25, delta=0.01)
        self.assertEqual(set(round(v, 6) for v in opened), {0.1, 1.0})   # closed = residue, open = full
        rows["param.detector"] = 1
        self.assertEqual(evaluate(raw, rows, control=0.9), 1.0)
        self.assertAlmostEqual(evaluate(raw, rows, control=0.2), 0.1)
        self.assertAlmostEqual(evaluate(raw, rows, control=None), 0.1)   # no control cable: the gate stays closed, no index error

    def test_signal_damage_quantises_to_the_bit_depth_and_the_mosaic_has_the_requested_cells(self):
        by_name = {o["name"]: o for o in self.chain("damage")}
        rows = rows_for(None, {"bits": 3, "rate": 3200, "block": 0.125, "levels": 4, "drive": 2.0, "band": 900, "output": 0.7})
        quantise = by_name["dmg_bits"]["pars"]["expr0expr"]["expr"]
        steps = {evaluate(quantise, rows, sample=x / 1000) for x in range(-1000, 1001)}
        self.assertEqual(sorted(steps), [-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0])   # 2 ** (3 - 1) = 4 steps per side
        self.assertEqual(evaluate(by_name["dmg_small"]["pars"]["resolutionw"]["expr"], rows), 8)
        self.assertEqual(evaluate(by_name["dmg_small"]["pars"]["resolutionh"]["expr"], rows), 4)   # 8 cells wide on a 16:9 frame: 4.5 rounds to even
        self.assertEqual(evaluate(by_name["dmg_full"]["pars"]["resolutionw"]["expr"], rows), 1920)
        self.assertEqual(evaluate(by_name["dmg_levels"]["pars"]["stepsize"]["expr"], rows), 0.25)
        self.assertAlmostEqual(evaluate(by_name["dmg_out"]["pars"]["gain"]["expr"], rows), 0.7)

    def test_displacement_direction_selects_the_displaced_axis(self):
        pull = next(o for o in self.chain("displacement") if o["name"] == "dsp_pull")["pars"]
        for direction, (x, y) in {0: (True, False), 1: (False, True), 2: (True, True)}.items():
            rows = rows_for(None, {"amount": 0.5, "scale": 4, "frequency": 1, "speed": 1, "spill": 1, "direction": direction, "warp": 0, "ring": 0})
            self.assertEqual((evaluate(pull["displaceweightx"]["expr"], rows) > 0, evaluate(pull["displaceweighty"]["expr"], rows) > 0), (x, y), direction)

    def test_feedback_returns_follow_the_workbenchs_pass_count_tail_and_reset_rule(self):
        """visual-engine.mjs: passes = min(round(repeats), floor((t - epoch) / time)); a return k draws when gain ** k >= threshold;
        epoch is the latched reset crossing the resolver supplies. Compared over a grid of every parameter's extremes and epochs."""
        by_name = {o["name"]: o for o in self.chain("feedback")}
        grid = itertools.product((0.04, 0.26, 1.5), (0.0, 0.5, 0.9), (1, 4, 10, 16), (0.001, 0.005, 0.1), (0.0, 3.0, 11.5), (0.0, 0.7, 2.9, 3.0, 8.0, 12.0))
        checked = 0
        for delay, gain, repeats, threshold, epoch, now in grid:
            rows = rows_for(None, {"time": delay, "gain": gain, "repeats": repeats, "scale": 0.81, "rotation": 0.25, "reset": 11.5, "threshold": threshold})
            rows["epoch"] = epoch
            passes = min(round(repeats), math.floor((now - epoch) / delay))
            for k in range(1, 17):
                wanted = gain ** k if (k <= passes and gain ** k >= threshold and round(delay * k * 60) < 240) else 0
                got = evaluate(by_name["fb_gain_%d" % k]["pars"]["opacity"]["expr"], rows, time=now)
                self.assertAlmostEqual(got, wanted, places=12, msg="k=%d %r" % (k, (delay, gain, repeats, threshold, epoch, now)))
                checked += 1
        self.assertGreater(checked, 5000)

    def test_feedback_recurrences_scale_and_rotate_by_the_recurrence_number_and_read_k_delays_back(self):
        by_name = {o["name"]: o for o in self.chain("feedback")}
        rows = rows_for(None, {"time": 0.1, "gain": 0.8, "repeats": 10, "scale": 0.81, "rotation": 0.25, "reset": 11.5, "threshold": 0.005})
        for k in (1, 2, 7, 16):
            move = by_name["fb_move_%d" % k]["pars"]
            self.assertAlmostEqual(evaluate(move["sx"]["expr"], rows), 0.81 ** k)
            self.assertEqual(evaluate(move["sx"]["expr"], rows), evaluate(move["sy"]["expr"], rows))
            self.assertAlmostEqual(evaluate(move["rotate"]["expr"], rows), 90.0 * k)
            self.assertEqual(move["extend"], "zero", "outside the transformed picture the return is transparent")
            self.assertEqual(evaluate(by_name["fb_frame_%d" % k]["pars"]["index"]["expr"], rows), -min(239, round(0.1 * k * 60)))
        self.assertEqual(by_name["fb_cache"]["pars"]["cachesize"], 240)
        self.assertEqual([by_name["fb_over_%d" % k]["inputs"] for k in (1, 2, 16)], [["fb_gain_1", "in_picture"], ["fb_gain_2", "fb_over_1"], ["fb_gain_16", "fb_over_15"]])   # each return over the picture so far, k = 1 first
        self.assertEqual({o["type"] for o in self.chain("feedback") if o["name"].startswith("fb_over_")}, {"overTOP"})
        self.assertNotIn("feedbackTOP", {o["type"] for o in self.chain("feedback")}, "returns read the history; there is no loop to diverge")

    def test_a_feedback_return_beyond_the_cache_is_switched_off(self):
        by_name = {o["name"]: o for o in self.chain("feedback")}
        rows = rows_for(None, {"time": 1.5, "gain": 0.9, "repeats": 16, "scale": 1, "rotation": 0, "reset": 0, "threshold": 0.001})   # 90 frames per return: only k = 1, 2 fit in 240
        gains = [evaluate(by_name["fb_gain_%d" % k]["pars"]["opacity"]["expr"], rows, time=100.0) for k in (1, 2, 3)]
        self.assertEqual([g > 0 for g in gains], [True, True, False])

    # -- Freeze / Hold -------------------------------------------------------------------------------------------------

    def js_freeze(self, combos, rate=2000, seconds=12.0):
        """The workbench's own freezeSourceTime, and its audio processor run over a ramp (sample i = i / rate), so that each output
        sample says which source time it read: out = (src(capture + phase) * w1 + src(capture + phase2) * w2) * gain."""
        script = """
import {processAudioModule, freezeSourceTime} from './web/fx/audio-engine.mjs';
const [combos, rate, seconds] = JSON.parse(process.argv[1]);
const n = Math.round(rate * seconds), ramp = Float32Array.from({length: n}, (_, i) => i / rate);
const out = combos.map(params => {
  const node = {id: 'fz', params, wet: 1, intensity: 1, macro: [], automation: []};
  const audio = processAudioModule('freeze', [ramp, ramp], rate, node).channels[0];
  const samples = [];
  for (let i = 0; i < n; i += 37) samples.push([i / rate, audio[i], freezeSourceTime(i / rate, params)]);
  return samples;
});
process.stdout.write(JSON.stringify(out));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps([combos, rate, seconds])], capture_output=True, text=True, cwd=ROOT, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_freeze_holds_exactly_where_the_workbench_holds_and_reads_the_same_source_times(self):
        """Checks the model: every scalar expression is evaluated afresh at each tested timestamp. In TouchDesigner they are evaluated once
        per cook, so the envelope steps at the cook rate (a known limit, issue 151)."""
        combos = [{"capture": 2.92, "duration": 4.4, "grain": 0.08, "decay": 0.12, "release": 0.05, "motion": 0, "capture_": 0},
                  {"capture": 1.0, "duration": 2.0, "grain": 0.3, "decay": 1.0, "release": 0.5},
                  {"capture": 0.0, "duration": 8.0, "grain": 0.025, "decay": 0.0, "release": 0.005},
                  {"capture": 10.0, "duration": 0.1, "grain": 0.1, "decay": 0.5, "release": 0.05}]
        combos = [{k: v for k, v in c.items() if k in ("capture", "duration", "grain", "decay", "release")} for c in combos]
        by_name = {o["name"]: o for o in self.chain("freeze")}
        compared = held_samples = 0
        for params, samples in zip(combos, self.js_freeze(combos)):
            rows = rows_for(None, {**params, "motion": 0.0})
            for t, out, source_time in samples:
                held = evaluate(by_name["fz_pick"]["pars"]["index"]["expr"], rows, time=t)
                self.assertEqual(held, 1 if source_time == params["capture"] else 0, (params, t))
                self.assertEqual(evaluate(by_name["fz_cache"]["pars"]["active"]["expr"], rows, time=t), 1 - held)
                tap1 = evaluate(by_name["fz_tap_1"]["pars"]["delay"]["expr"], rows, time=t)
                tap2 = evaluate(by_name["fz_tap_2"]["pars"]["delay"]["expr"], rows, time=t)
                g1 = evaluate(by_name["fz_grain_1"]["pars"]["gain"]["expr"], rows, time=t)
                g2 = evaluate(by_name["fz_grain_2"]["pars"]["gain"]["expr"], rows, time=t)
                through = evaluate(by_name["fz_through"]["pars"]["gain"]["expr"], rows, time=t)
                modelled = t * through + (t - tap1) * g1 + (t - tap2) * g2   # the ramp read through the two delays
                elapsed = t - params["capture"]
                size = max(0.001, params["grain"])
                late = held and (elapsed % size + size / 2) % size > elapsed   # the second grain would start after the live signal: clamped, see the docstring
                if not late:
                    self.assertAlmostEqual(modelled, out, delta=3e-4, msg=(params, t))
                    compared += 1
                held_samples += held
        self.assertGreater(compared, 1000)
        self.assertGreater(held_samples, 300)

    def test_freeze_picture_fades_toward_a_fifth_and_shakes_only_while_held(self):
        by_name = {o["name"]: o for o in self.chain("freeze")}
        rows = rows_for(None, {"capture": 2.0, "duration": 4.0, "grain": 0.08, "decay": 1.0, "release": 0.05, "motion": 0.1})
        fade = by_name["fz_fade"]["pars"]["opacity"]["expr"]
        shake = by_name["fz_shake"]["pars"]["tx"]["expr"]
        self.assertEqual([evaluate(fade, rows, time=t) for t in (1.9, 6.0)], [1, 1])
        self.assertAlmostEqual(evaluate(fade, rows, time=2.0), 1.0)
        self.assertAlmostEqual(evaluate(fade, rows, time=4.0), 0.5)
        self.assertAlmostEqual(evaluate(fade, rows, time=5.99), 0.2)   # floor of the workbench's max(0.2, ...)
        self.assertEqual([evaluate(shake, rows, time=t) for t in (1.9, 6.0)], [0, 0])
        self.assertAlmostEqual(evaluate(shake, rows, time=3.0), math.sin(21.0) * 0.1)

    def test_freeze_without_a_voice_or_picture_target_builds_only_the_other_chain(self):
        spec = spec_for(single_patch("freeze", ["voice"]))
        names = {o["name"] for o in spec["operators"] if o["parent"] == "fx_fx_1"}
        self.assertTrue({"fz_tap_1", "fz_sum"} <= names)
        self.assertFalse({"fz_cache", "fz_fade"} & names)

    # -- Signal Damage: sample-rate reduction ---------------------------------------------------------------------------

    def test_damage_holds_each_run_of_samples_like_the_workbenchs_sample_and_hold(self):
        """audio-engine.mjs damage: holdPhase += clamp(rate, 100, sr) / sr per sample, a new sample is taken whenever it reaches 1."""
        by_name = {o["name"]: o for o in self.chain("damage")}
        expression = by_name["dmg_hold"]["pars"]["expr0expr"]["expr"]
        self.assertEqual(by_name["dmg_bits"]["inputs"], ["dmg_hold"])
        sr, n = 48000.0, 3000
        ramp = [float(i) for i in range(n)]
        right = [1000.0 + 2 * i for i in range(n)]   # a different signal in the right channel: it must not be replaced by the left
        for rate in (100, 600, 1800, 3200, 12000, 24000, 48000, 90000):
            rows = rows_for(None, {"bits": 16, "rate": rate, "block": 0.1, "levels": 4, "drive": 1.0, "band": 900, "output": 1.0})
            phase, taken_at, last = 1.0, [], 0
            for i in range(n):
                phase += min(sr, max(100, rate)) / sr
                if phase >= 1:
                    phase %= 1
                    last = i
                taken_at.append(last)
            for i in range(0, n, 7):
                got = int(evaluate(expression, rows, audio=(ramp, right), index=i, rate=sr))   # the ramp, so the value is the sample index read
                got_right = evaluate(expression, rows, audio=(ramp, right), index=i, rate=sr, chan=1)
                self.assertEqual(got_right, 1000.0 + 2 * round(evaluate(expression.replace("[me.chanIndex]", "[0]"), rows, audio=(ramp, ramp), index=i, rate=sr)), "the right channel reads its own samples")
                # a hold boundary falls within one sample of where the workbench's accumulated phase puts it (the phase sums floats, so a
                # boundary sample can land either side): the read must match the hold of this sample or of a neighbour
                near = [taken_at[j] for j in (i - 1, i, i + 1) if 0 <= j < n]
                self.assertTrue(any(abs(got - held) <= 1 for held in near), "rate %r sample %d: read %d, workbench holds %r" % (rate, i, got, near))


class TargetTests(unittest.TestCase):
    def build(self, targets):
        return spec_for(single_patch("damage", targets))

    def tails(self, spec):
        inside = {o["name"]: o for o in spec["operators"] if o["parent"] == "fx_fx_1"}
        return inside["proc_picture"]["inputs"][0], inside["proc_audio"]["inputs"][0]

    def test_a_processor_without_a_voice_target_leaves_the_audio_alone(self):
        spec = self.build(["picture"])
        self.assertEqual(self.tails(spec), ("dmg_full", "in_audio"))
        self.assertEqual(spec["effects"], [{"id": "fx-1", "type": "damage", "picture": True, "audio": False}])
        self.assertTrue(any("no voice target" in w for w in spec["warnings"]))

    def test_a_processor_without_a_visual_target_leaves_the_picture_alone(self):
        spec = self.build(["voice"])
        self.assertEqual(self.tails(spec), ("in_picture", "dmg_out"))
        self.assertEqual(spec["effects"][0]["picture"], False)
        self.assertTrue(any("no picture target" in w for w in spec["warnings"]))

    def test_the_default_targets_build_both(self):
        self.assertEqual(self.tails(self.build(None)), ("dmg_full", "dmg_out"))


class EnvelopeTests(unittest.TestCase):
    def test_the_source_produces_the_sidechain_envelope_the_workbench_derives(self):
        spec = spec_for(FIXTURES / "message-impact.json")
        source = {o["name"]: o for o in spec["operators"] if o["parent"] == "fx_source"}
        self.assertEqual(source["out_control"]["inputs"], ["control"])
        self.assertEqual(source["control"]["inputs"], ["env_follow"])
        self.assertEqual(source["env_follow"]["inputs"], ["env_peak"])
        self.assertEqual(source["env_peak"]["inputs"], ["audio"])
        self.assertEqual(source["env_follow"]["pars"], {"lag1": 0.006, "lag2": 0.075})   # attack and release of amplitudeEnvelope
        peak, limit = (source[n]["pars"]["expr0expr"]["expr"] for n in ("env_peak", "control"))
        # the larger of the two channels' magnitudes, sample by sample, as amplitudeEnvelope takes it
        left, right = [0.1, -0.2, 0.0], [0.0, 0.05, -0.7]
        self.assertEqual([evaluate(peak, {}, audio=(left, right), index=i) for i in range(3)], [0.1, 0.2, 0.7])
        self.assertEqual(evaluate(peak, {}, audio=([0.0, 0.0, 0.0], [0.0, 0.9, 0.0]), index=1), 0.9)   # signal in the right channel only
        self.assertEqual([evaluate(limit, {}, sample=x) for x in (0.1, 0.5, 0.9)], [0.5, 1, 1])   # times 5, limited to 1
        self.assertFalse([o for o in spec["operators"] if o["type"] == "constantCHOP" and o["name"] == "control"])


if __name__ == "__main__":
    unittest.main()
