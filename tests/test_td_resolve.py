"""The generated network's parameter resolver against the workbench's own evaluatedNode.

`tools/td-network/oracle-evaluated.mjs` runs core.mjs `evaluatedNode` over each fixture patch at sampled times,
with sidechain envelopes of 0, 0.37 and 1; the Python resolver gets the same base values, times and envelopes and
must return the same intensity, wet, every processor parameter, Solo, Bypass and active state. TouchDesigner is not
used.
"""
import importlib.util
import json
import math
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "web" / "fx" / "fixtures"
def is_patch(path):
    data = json.loads(path.read_text())
    return isinstance(data, dict) and isinstance(data.get("nodes"), list) and isinstance(data.get("connections"), list)


NAMES = sorted(p.name for p in FIXTURES.glob("*.json") if is_patch(p))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve = load("fx_td_resolve", ROOT / "tools" / "td-network" / "python" / "fx_td_resolve.py")


def oracle(name, *flags):
    result = subprocess.run(["node", str(ROOT / "tools" / "td-network" / "oracle-evaluated.mjs"), str(FIXTURES / name), *flags],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def bases_of(contract):
    return {node["id"]: resolve.base_values(node, lambda par, node=node: next(c["value"] for c in node["controls"] if c["par"] == par))
            for node in contract["nodes"]}


class ParityTests(unittest.TestCase):
    def test_every_fixture_resolves_like_evaluated_node(self):
        self.assertGreater(self.compare_all(), 2000)

    def test_stressed_fixtures_resolve_like_evaluated_node(self):
        """Every macro curve, step and linear lanes, add and multiply modulation, a clip scope and a Solo event."""
        self.assertGreater(self.compare_all("--stress"), 2000)

    def compare_all(self, *flags):
        compared = 0
        for name in NAMES:
            data = oracle(name, *flags)   # a patch the oracle rejects fails the test; it is never skipped
            bases = bases_of(data["contract"])
            for case in data["cases"]:
                got = resolve.resolve_rack(data["contract"], bases, case["time"], case["controls"])
                for node_id, want in case["evaluated"].items():
                    have = got[node_id]
                    where = "%s %s t=%r" % (name, node_id, case["time"])
                    self.assertTrue(math.isclose(have["intensity"], want["intensity"], abs_tol=1e-9), where + " intensity")
                    self.assertTrue(math.isclose(have["wet"], want["wet"], abs_tol=1e-9), where + " wet")
                    self.assertEqual(set(have["params"]), set(want["params"]), where)
                    for key, value in want["params"].items():
                        self.assertTrue(math.isclose(have["params"][key], value, abs_tol=1e-9), "%s %s: %r != %r" % (where, key, have["params"][key], value))
                    self.assertEqual((have["solo"], have["bypass"], have["active"]), (want["solo"], want["bypass"], want["active"]), where)
                    compared += 1
        return compared

    def test_every_patch_fixture_is_compared(self):
        self.assertGreaterEqual(len(NAMES), 20)
        self.assertIn("closed-inbox.json", NAMES)
        self.assertNotIn("index.json", NAMES)

    def test_the_fixtures_exercise_automation_events_modulation_and_macros(self):
        seen = {"automation": False, "events": False, "modulation": False, "macro": False, "solo": False}
        for name in NAMES:
            data = oracle(name)
            for node in data["contract"]["nodes"]:
                block = node["resolve"]
                seen["automation"] |= bool(block["automation"])
                seen["events"] |= bool(block["events"])
                seen["modulation"] |= "modulation" in block
                seen["macro"] |= bool(block["macro"])
                seen["solo"] |= any(c["path"] == "solo" and c["value"] for c in node["controls"]) or any(e["parameter"] == "solo" for e in block["events"])
        self.assertEqual(seen, {key: True for key in seen}, seen)


def block(**extra):
    return {"order": ["a"], "params": {"a": {"min": 0, "max": 10, "step": 0.01}}, "macro": [], "automation": [], "events": [], **extra}


def contract_of(*nodes):
    return {"nodes": [{"id": i, "resolve": b} for i, b in nodes]}


def base(solo=False, bypass=False, a=1.0, intensity=1.0, wet=1.0):
    return {"intensity": intensity, "wet": wet, "params": {"a": a}, "bypass": bypass, "solo": solo}


class RackScopeTests(unittest.TestCase):
    def test_solo_silences_every_other_processor_and_unsolo_restores_them(self):
        contract = contract_of(("one", block()), ("two", block()), ("three", block()))
        got = resolve.resolve_rack(contract, {"one": base(), "two": base(solo=True), "three": base()}, 1.0)
        self.assertEqual({k: (v["active"], v["bypass"]) for k, v in got.items()}, {"one": (False, True), "two": (True, False), "three": (False, True)})
        got = resolve.resolve_rack(contract, {"one": base(), "two": base(), "three": base()}, 1.0)
        self.assertTrue(all(v["active"] and not v["bypass"] for v in got.values()))

    def test_a_solo_event_in_one_processor_affects_the_others_at_that_time_only(self):
        events = [{"time": 2, "parameter": "solo", "value": True}, {"time": 4, "parameter": "solo", "value": False}]
        contract = contract_of(("one", block(events=events)), ("two", block()))
        bases = {"one": base(), "two": base()}
        before, during, after = (resolve.resolve_rack(contract, bases, t)["two"]["bypass"] for t in (1.99, 2.0, 4.0))
        self.assertEqual((before, during, after), (False, True, False))

    def test_a_clip_scope_bypasses_outside_its_range_and_does_not_count_as_a_solo(self):
        contract = contract_of(("one", block(scope={"type": "clip", "start": 1, "end": 2})), ("two", block()))
        bases = {"one": base(), "two": base()}
        at = lambda t: {k: v["active"] for k, v in resolve.resolve_rack(contract, bases, t).items()}
        self.assertEqual(at(0.5), {"one": False, "two": True})
        self.assertEqual(at(1.0), {"one": True, "two": True})
        self.assertEqual(at(2.0), {"one": False, "two": True})

    def test_a_non_finite_time_is_refused(self):
        for bad in (math.nan, math.inf, "1"):
            with self.assertRaises(ValueError):
                resolve.resolve_rack(contract_of(("one", block())), {"one": base()}, bad)


class ReleaseTests(unittest.TestCase):
    def test_release_drops_the_events_lane_and_macro_of_one_control_only(self):
        spec = block(automation=[{"parameter": "params.a", "interpolation": "linear", "keyframes": [{"time": 0, "value": 1}, {"time": 1, "value": 5}]},
                                 {"parameter": "wet", "interpolation": "step", "keyframes": [{"time": 0, "value": 0.2}]}],
                     events=[{"time": 0.5, "parameter": "params.a", "value": 3}, {"time": 0.5, "parameter": "wet", "value": 0.4}],
                     macro=[{"parameter": "a", "min": 0, "max": 8, "curve": "linear"}])
        self.assertEqual(resolve.resolve_node(spec, base(), 1.0)["params"]["a"], 3)
        self.assertTrue(resolve.release_rules(spec, "params.a"))
        self.assertEqual((spec["events"], spec["macro"], [lane["parameter"] for lane in spec["automation"]]), ([{"time": 0.5, "parameter": "wet", "value": 0.4}], [], ["wet"]))
        self.assertEqual(resolve.resolve_node(spec, base(a=7.0), 1.0)["params"]["a"], 7.0)   # the hand-set base now stands
        self.assertFalse(resolve.release_rules(spec, "params.a"))
        self.assertEqual(resolve.resolve_node(spec, base(wet=0.9), 1.0)["wet"], 0.4)           # the wet rules are untouched


class Table:
    def __init__(self):
        self.rows, self.clears = [], 0

    def clear(self):
        self.rows, self.clears = [], self.clears + 1

    def appendRow(self, row):
        self.rows.append(list(row))


class Chop:
    def __init__(self, value):
        self.numChans, self.value = 1, value

    def __getitem__(self, index):
        return self

    def eval(self):
        return self.value


class Par:
    def __init__(self, value):
        self.value = value

    def eval(self):
        return self.value


class Ns:
    def __init__(self, **pars):
        for k, v in pars.items():
            setattr(self, k, Par(v))


class Comp:
    def __init__(self, pars, control=0.0):
        self.par, self.table, self.control = Ns(**pars), Table(), Chop(control)

    def op(self, name):
        return {"resolved": self.table, "in_control": self.control}[name]


class Dat:
    def __init__(self, text):
        self.text = text


class Rack:
    path = "/project1/fx_rack"

    def __init__(self, contract, time, comps):
        self.contract, self.comps = contract, comps
        self.par = Ns(Time=time)

    def op(self, name):
        if name == "td_contract":
            return Dat(json.dumps(self.contract))
        return self.comps[name]


class TickTests(unittest.TestCase):
    def test_tick_writes_the_resolved_table_and_only_when_it_changes(self):
        resolve.reset()
        node = {"id": "a", "comp": "fx_a", "resolve": block(macro=[{"parameter": "a", "min": 0, "max": 8, "curve": "linear"}], modulation={"parameter": "intensity", "mode": "add", "amount": -0.5}),
                "controls": [{"par": "Intensity", "path": "intensity", "kind": "range", "value": 1}, {"par": "Wet", "path": "wet", "kind": "range", "value": 1},
                             {"par": "A", "path": "params.a", "kind": "range", "value": 1}, {"par": "Bypass", "path": "bypass", "kind": "toggle", "value": False},
                             {"par": "Solo", "path": "solo", "kind": "toggle", "value": False}]}
        contract = {"nodes": [node]}
        comp = Comp({"Intensity": 1.0, "Wet": 1.0, "A": 1.0, "Bypass": 0, "Solo": 0}, control=1.0)
        rack = Rack(contract, 0.0, {"fx_a": comp})
        resolved = resolve.tick(rack)
        self.assertEqual(resolved["a"]["intensity"], 0.5)
        self.assertEqual(dict(map(tuple, comp.table.rows)), {"intensity": 0.5, "wet": 1.0, "bypass": 0, "solo": 0, "active": 1, "param.a": 4.0})
        self.assertEqual([r[0] for r in comp.table.rows], ["intensity", "wet", "bypass", "solo", "active", "param.a"])
        resolve.tick(rack)
        self.assertEqual(comp.table.clears, 1)
        comp.control.value = 0.0
        resolve.tick(rack)
        self.assertEqual((comp.table.clears, dict(map(tuple, comp.table.rows))["param.a"]), (2, 8.0))

    def test_a_reported_release_drops_the_rules_for_that_control_and_the_next_tick_follows_the_base(self):
        resolve.reset()
        node = {"id": "a", "comp": "fx_a", "resolve": block(events=[{"time": 0, "parameter": "params.a", "value": 6}]),
                "controls": [{"par": "Intensity", "path": "intensity", "kind": "range", "value": 1}, {"par": "Wet", "path": "wet", "kind": "range", "value": 1},
                             {"par": "A", "path": "params.a", "kind": "range", "value": 1}, {"par": "Bypass", "path": "bypass", "kind": "toggle", "value": False},
                             {"par": "Solo", "path": "solo", "kind": "toggle", "value": False}]}
        comp = Comp({"Intensity": 1.0, "Wet": 1.0, "A": 2.0, "Bypass": 0, "Solo": 0})
        rack = Rack({"nodes": [node]}, 1.0, {"fx_a": comp})
        self.assertEqual(resolve.tick(rack)["a"]["params"]["a"], 6)
        self.assertTrue(resolve.apply(rack, "fx_a", "released", "params.a", None))
        self.assertEqual(resolve.tick(rack)["a"]["params"]["a"], 2.0)
        self.assertEqual(dict(map(tuple, comp.table.rows))["param.a"], 2.0)
        self.assertFalse(resolve.apply(rack, "fx_a", "released", "params.a", None))
        self.assertFalse(resolve.apply(rack, "fx_missing", "released", "params.a", None))


if __name__ == "__main__":
    unittest.main()
