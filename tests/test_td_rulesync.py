"""Rule sync: the workbench's edits to automation, events and macros reach the generated network's resolver.

`tools/td-network/oracle-rulesync.mjs` edits a stressed patch the way the workbench does (the parameter setter's own code,
the automation form, Clear, a preset), collects the rack's real `OscFeedback` after every edit and samples
`evaluatedNode`. The network here starts from the snapshot generated BEFORE the patch had its rules. Every step's
messages go through the generated `td_osc` module (`RackLink.receive`) into the resolver; after each step the resolver must
return what `evaluatedNode` returns. TouchDesigner is not used.
"""
import importlib.util
import json
import math
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIR = ROOT / "tools" / "td-network" / "python"
FIXTURES = ROOT / "web" / "fx" / "fixtures"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


osc = load("fx_td_osc", PYTHON_DIR / "fx_td_osc.py")
resolve = load("fx_td_resolve", PYTHON_DIR / "fx_td_resolve.py")


def oracle(name):
    result = subprocess.run(["node", str(ROOT / "tools" / "td-network" / "oracle-rulesync.mjs"), str(FIXTURES / name)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class Mirror:
    """A backend whose resolver edits the contract's own resolve blocks, as td_resolve does inside TouchDesigner."""
    def __init__(self, contract):
        self.contract = contract
        self.blocks = {node["comp"]: node["resolve"] for node in contract["nodes"]}
        self.rules = []

    def get(self, comp, par):
        return 0

    def set(self, comp, par, value):
        pass

    def send(self, address, args):
        pass

    def rule(self, comp, kind, path, payload):
        self.rules.append((comp, kind, path))
        resolve.apply_rule(self.blocks[comp], kind, path, payload)


def close(a, b):
    return math.isclose(a, b, abs_tol=1e-6)


class RuleSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = oracle("closed-inbox.json")

    def test_every_step_leaves_the_resolver_where_the_workbench_is(self):
        contract = json.loads(json.dumps(self.data["contract"]))
        backend = Mirror(contract)
        link = osc.RackLink(contract, backend)
        compared = 0
        for step in self.data["steps"]:
            for message in step["messages"]:
                self.assertTrue(link.receive(message["address"], message["args"]), (step["label"], message))
            bases = step["bases"]
            resolved_bases = {node["id"]: resolve.base_values(node, lambda par, node=node: self.base_of(node, par, bases[node["id"]])) for node in contract["nodes"]}
            for case in step["cases"]:
                got = resolve.resolve_rack(contract, resolved_bases, case["time"], case["controls"])
                for node_id, want in case["evaluated"].items():
                    have = got[node_id]
                    where = "%s | %s t=%r" % (step["label"], node_id, case["time"])
                    self.assertTrue(close(have["intensity"], want["intensity"]), "%s intensity %r != %r" % (where, have["intensity"], want["intensity"]))
                    self.assertTrue(close(have["wet"], want["wet"]), "%s wet %r != %r" % (where, have["wet"], want["wet"]))
                    for key, value in want["params"].items():
                        self.assertTrue(close(have["params"][key], value), "%s %s: %r != %r" % (where, key, have["params"][key], value))
                    self.assertEqual((have["solo"], have["bypass"], have["active"]), (want["solo"], want["bypass"], want["active"]), where)
                    compared += 1
        self.assertGreater(compared, 1000)

    @staticmethod
    def base_of(node, par, base):
        control = next(c for c in node["controls"] if c["par"] == par)
        path = control["path"]
        if path in ("bypass", "solo"):
            return 1 if base[path] else 0
        return base[path] if path in ("intensity", "wet") else base["params"][path[7:]]

    def test_an_unchanged_rack_sends_nothing_and_the_connect_sends_every_control_released_first(self):
        steps = self.data["steps"]
        self.assertEqual(steps[1]["messages"], [])
        connect = steps[0]["messages"]
        for node in self.data["contract"]["nodes"]:
            kinds = [m["address"].split("/")[4] for m in connect if m["address"].startswith("/fx/node/%s/" % node["id"])]
            self.assertEqual(kinds, sorted(kinds, key=lambda k: k != "released"), "%s: every release precedes every rule" % node["id"])
        released = {m["address"] for m in connect if "/released/" in m["address"]}
        contract_controls = sum(len(node["controls"]) for node in self.data["contract"]["nodes"])
        self.assertEqual(len(released), contract_controls)

    def test_every_address_the_rack_reports_is_in_the_contract(self):
        known = {address for node in self.data["contract"]["nodes"] for control in node["controls"] for address in control["rules"].values()}
        for step in self.data["steps"]:
            for message in step["messages"]:
                self.assertIn(message["address"], known, message)

    def test_a_hand_edit_in_the_workbench_of_an_automated_control_stops_the_network_playing_the_old_lane(self):
        """The Codex finding on the resolver PR: before the sync, editing Wet or Intensity in the workbench left the network's
        frame resolver overriding the new base with the old automation."""
        step = next(s for s in self.data["steps"] if s["label"].startswith("hand edit of an automated control"))
        self.assertEqual([m["address"].split("/")[4] for m in step["messages"]], ["released"])
        self.assertTrue(step["messages"][0]["address"].endswith("/released/intensity"))

    def test_malformed_rule_messages_are_refused_and_change_nothing(self):
        contract = json.loads(json.dumps(self.data["contract"]))
        backend = Mirror(contract)
        link = osc.RackLink(contract, backend)
        node = next(n for n in contract["nodes"] if any(c["path"].startswith("params.") for c in n["controls"]))
        control = next(c for c in node["controls"] if c["path"].startswith("params."))
        before = json.dumps(contract["nodes"])
        bad = [(control["rules"]["event"], []), (control["rules"]["event"], [1.0]), (control["rules"]["event"], ["a", 1.0]), (control["rules"]["event"], [float("nan"), 1.0]),
               (control["rules"]["lane"], []), (control["rules"]["lane"], [3]), (control["rules"]["lane"], ["not json"]), (control["rules"]["lane"], ["[1,2]"]),
               (control["rules"]["macro"], ["{}"]), (control["rules"]["macro"], [json.dumps({"min": 0, "max": 1, "curve": "wobble"})]), (control["rules"]["macro"], [json.dumps({"min": "a", "max": 1, "curve": "linear"})]),
               (control["rules"]["lane"], [json.dumps({"interpolation": "cubic", "keyframes": [{"time": 0, "value": 1}]})]),
               (control["rules"]["lane"], [json.dumps({"interpolation": "linear", "keyframes": []})]),
               (control["rules"]["lane"], [json.dumps({"interpolation": "linear", "keyframes": [{"time": 0}]})])]
        for address, args in bad:
            ok = link.receive(address, args)
            if ok:   # understood as a message, but the resolver must have refused the payload
                continue
        self.assertEqual(json.dumps(contract["nodes"]), before)

    def test_apply_rule_edge_cases(self):
        block = {"order": ["a"], "params": {"a": {"min": 0, "max": 10, "step": 0.01}}, "macro": [], "automation": [], "events": []}
        self.assertTrue(resolve.apply_rule(block, "event", "params.a", [2.0, 5.0]))
        self.assertTrue(resolve.apply_rule(block, "event", "params.a", [1.0, 3.0]))
        self.assertTrue(resolve.apply_rule(block, "event", "params.a", [2.00001, 7.0]))   # same event at 4 decimals: replaced
        self.assertEqual([(e["time"], e["value"]) for e in block["events"]], [(1.0, 3.0), (2.0, 7.0)])
        self.assertFalse(resolve.apply_rule(block, "event", "params.a", [float("inf"), 1.0]))
        self.assertFalse(resolve.apply_rule(block, "macro", "intensity", {"min": 0, "max": 1, "curve": "linear"}))   # only processor parameters have macros
        self.assertFalse(resolve.apply_rule(block, "lane", "wet", None))
        self.assertFalse(resolve.apply_rule(block, "nonsense", "wet", None))
        self.assertTrue(resolve.apply_rule(block, "released", "params.a", None))
        self.assertEqual(block["events"], [])
        self.assertFalse(resolve.apply_rule(block, "released", "params.a", None))


if __name__ == "__main__":
    unittest.main()
