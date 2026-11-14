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
IMPLEMENTED = ["damage", "delay", "displacement", "feedback", "gate"]


def spec_for(patch_path):
    directory = Path(tempfile.mkdtemp(prefix="td-fx-"))
    result = subprocess.run(["node", str(ROOT / "tools" / "fx-td-network.mjs"), str(patch_path), "--out", str(directory)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(next(directory.glob("*.network.json")).read_text())


def single_patch(type_name):
    """A one-processor patch for a processor type, written by the workbench's own factory."""
    script = ("import {freshPatch} from './tests/fx/runtime.mjs'; process.stdout.write(JSON.stringify(freshPatch(%s)));" % json.dumps(type_name))
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


class Comp:
    def __init__(self, rows, time, control, gate):
        self.table, self.control, self.gate = Table(rows), control, gate
        self.par = type("P", (), {"Time": time})()

    def op(self, name):
        if name == "resolved":
            return self.table
        if name == "in_control":
            return [Chan(self.control)]
        if name == "gate_ctrl":
            return [Chan(self.gate)]
        if name == "in_picture":
            return Sized()
        raise KeyError(name)

    def parent(self):
        return self


class Project:
    cookRate = 60.0


class Me:
    def __init__(self, value):
        self.inputVal = value


def evaluate(text, rows, time=0.0, control=0.0, gate=1.0, sample=0.0):
    comp = Comp(rows, time, control, gate)
    return eval(text, {"parent": lambda: comp, "project": Project, "me": Me(sample), "float": float, "int": int, "max": max, "min": min, "round": round})


def rows_for(definition_params, picks):
    rows = {"intensity": 1.0, "wet": 1.0, "bypass": 0, "solo": 0, "active": 1}
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
        return [o for o in self.specs[type_name]["operators"] if o["parent"].startswith("fx_") and o["parent"].count("/") == 0 and o["name"] not in ("resolved",) and o["path"].split("/")[1].startswith(("dmg_", "dly_", "gate_", "fb_", "dsp_"))]

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

    def test_the_gate_opens_for_its_duty_fraction_of_each_period(self):
        raw = next(o for o in self.chain("gate") if o["name"] == "gate_raw")["pars"]["const0value"]["expr"]
        rows = rows_for(None, {"duty": 0.25, "period": 2.0, "edge": 0.01, "phase": 0.0, "retain": 0.1, "threshold": 0.5, "detector": 0})
        opened = [evaluate(raw, rows, time=t / 100) for t in range(0, 400)]
        self.assertAlmostEqual(sum(1 for v in opened if v > 0.5) / len(opened), 0.25, delta=0.01)
        self.assertEqual(set(round(v, 6) for v in opened), {0.1, 1.0})   # closed = residue, open = full
        rows["param.detector"] = 1
        self.assertEqual(evaluate(raw, rows, control=0.9), 1.0)
        self.assertAlmostEqual(evaluate(raw, rows, control=0.2), 0.1)

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

    def test_feedback_returns_through_the_requested_scale_rotation_and_gain(self):
        by_name = {o["name"]: o for o in self.chain("feedback")}
        rows = rows_for(None, {"time": 0.26, "gain": 0.8, "repeats": 10, "scale": 0.81, "rotation": 0.25, "reset": 11.5, "threshold": 0.005})
        self.assertEqual(evaluate(by_name["fb_move"]["pars"]["sx"]["expr"], rows), 0.81)
        self.assertEqual(evaluate(by_name["fb_move"]["pars"]["rotate"]["expr"], rows), 90.0)
        self.assertEqual(evaluate(by_name["fb_gain"]["pars"]["opacity"]["expr"], rows), 0.8)
        self.assertEqual(by_name["fb_state"]["pars"]["top"], {"ref": "fx_fx_1/fb_sum"})


if __name__ == "__main__":
    unittest.main()
