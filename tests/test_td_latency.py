"""The declared latency of the generated network: Reverse and Stutter read source time ahead of their playhead.

Everything here is checked against the workbench's own functions (`reverseSourceTime`, `stutterSourceTime`, `evaluatedNode`),
run by Node over grids that include the catalog's extremes, and against the generated spec. TouchDesigner is not used.

1. The look-ahead each processor declares bounds what the engine reads: a sweep of the engine's source-time functions over
   the catalog extremes never reads further ahead than the declared bound, and the bound is reached (it is not slack).
2. The resolver's derived rows (`depth`, `pick`, `record`, `shift`) name the same source time the engine reads.
3. Latency is a path sum: two look-ahead processors in series add, parallel branches are delayed to meet, the dry branch
   of a processor with look-ahead is delayed by the same amount, and a rack with no look-ahead is unchanged.
4. The network's expressions, evaluated for real, read the cache at the depth the resolver names.
"""
import importlib.util
import itertools
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_td_effects import evaluate, rows_for

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "web" / "fx" / "fixtures"


def load(name, path):
    module_spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


resolve = load("fx_td_resolve", ROOT / "tools" / "td-network" / "python" / "fx_td_resolve.py")

ENGINE = """
import {reverseSourceTime, stutterSourceTime} from './web/fx/audio-engine.mjs';
import {CATALOG_BY_TYPE} from './web/fx/catalog.mjs';
import {LOOKAHEAD, lookaheadOf, HISTORY} from './tools/td-network/latency.mjs';
const range = (type, key) => CATALOG_BY_TYPE[type].params.find(p => p.key === key);
const corners = (type, key) => { const p = range(type, key); return [...new Set([p.min, p.default, p.max, (p.min + p.max) / 2])]; };
const duration = 12.166666667;
const times = Array.from({length: 61}, (_, i) => i * duration / 60);
const reverse = [], stutter = [];
for (const span of corners('reverse', 'span')) for (const speed of corners('reverse', 'speed')) for (const pivot of [0, 1.3, 4, 9.9, duration])
  for (const t of times) reverse.push({span, speed, pivot, t, s: reverseSourceTime(t, {span, speed, pivot, mode: 0}, duration)});
for (const window of corners('stutter', 'window')) for (const rate of corners('stutter', 'rate')) for (const gap of corners('stutter', 'gap'))
  for (const repeats of [1, 3, 16, 40]) for (const capture of [0, 2.88, 7.5])
    for (let t = 0; t < duration; t += 0.0625) stutter.push({window, rate, gap, repeats, capture, t, s: stutterSourceTime(t, {window, rate, gap, repeats, capture})});
process.stdout.write(JSON.stringify({bounds: {reverse: LOOKAHEAD.reverse(), stutter: LOOKAHEAD.stutter(), reverseCapped: lookaheadOf('reverse'), stutterCapped: lookaheadOf('stutter')},
  history: {reverse: HISTORY.reverse(lookaheadOf('reverse')), stutter: HISTORY.stutter()}, duration, reverse, stutter}));
"""


def engine():
    result = subprocess.run(["node", "--input-type=module", "-e", ENGINE], capture_output=True, text=True, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def spec_of(patch, *flags):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(patch, handle)
    handle.close()
    directory = Path(tempfile.mkdtemp(prefix="td-lat-"))
    result = subprocess.run(["node", str(ROOT / "tools" / "fx-td-network.mjs"), handle.name, "--out", str(directory), *flags], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(next(directory.glob("*.network.json")).read_text())


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def connect(patch, edges):
    """Replace the patch's wires: every listed (from, to) pair carries all five signal ports."""
    patch["connections"] = [{"from": a, "to": b, "port": port, "gain": 1, "role": "insert"} for a, b in edges for port in ("audio", "picture", "text", "matte", "control")]
    return patch


def module(patch, node_id, type_name):
    from_fixture = fixture({"reverse": "demo-reverse.json", "delay": "demo-delay.json"}.get(type_name, "demo-reverse.json"))
    node = json.loads(json.dumps(next(n for n in from_fixture["nodes"] if n["type"] == type_name)))
    node["id"] = node_id
    patch["nodes"].insert(-1, node)
    return node


class LookaheadBoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = engine()

    def test_reverse_never_reads_further_ahead_than_its_span_and_reaches_it(self):
        lead = [item["s"] - item["t"] for item in self.data["reverse"]]
        bound = self.data["bounds"]["reverse"]
        self.assertLessEqual(max(lead), bound + 1e-9)
        self.assertGreater(max(lead), 0.9 * bound, "the bound is not slack: a block start reads almost a whole span ahead")
        for item in self.data["reverse"]:
            self.assertLessEqual(item["s"] - item["t"], item["span"] + 1e-9)   # per request, the lead is at most the requested span

    def test_reverse_reads_back_no_further_than_one_span_behind(self):
        for item in self.data["reverse"]:
            self.assertGreaterEqual(item["s"], item["t"] - item["span"] - 2e-7)   # the engine takes 1e-7 off the interval end

    def test_stutter_lead_is_bounded_by_window_times_one_minus_one_over_rate(self):
        bound = self.data["bounds"]["stutter"]
        leads = [item["s"] - item["t"] for item in self.data["stutter"] if item["s"] is not None]
        self.assertLessEqual(max(leads), bound + 1e-9)
        self.assertGreater(max(leads), 0.97 * bound)
        for item in self.data["stutter"]:
            if item["s"] is not None:
                self.assertLessEqual(item["s"] - item["t"], item["window"] * (1 - 1 / max(item["rate"], 1)) + 1e-9)

    def test_stutter_reads_stay_inside_the_captured_window(self):
        for item in self.data["stutter"]:
            s = item["s"]
            if s is None or s == item["t"]:
                continue
            self.assertGreaterEqual(s, item["capture"] - 1e-9)
            self.assertLess(s, item["capture"] + item["window"] + 1e-9)

    def test_the_declared_latency_is_capped_and_the_cache_history_covers_every_read(self):
        bounds = self.data["bounds"]
        self.assertEqual(bounds["reverse"], 5)
        self.assertEqual(bounds["reverseCapped"], 2.5)
        self.assertAlmostEqual(bounds["stutterCapped"], bounds["stutter"])   # 1.0 s, under the cap
        history = self.data["history"]
        self.assertEqual(history["reverse"], 5.0)   # the capped look-ahead plus the capped span
        self.assertEqual(history["stutter"], 1.5)


class DerivedRowTests(unittest.TestCase):
    """The resolver's rows name the source time the engine reads."""
    @classmethod
    def setUpClass(cls):
        cls.data = engine()

    def test_reverse_depth_names_the_engines_source_time(self):
        lookahead, checked = 2.5, 0
        block = {"lookahead": lookahead, "duration": self.data["duration"]}
        for item in self.data["reverse"]:
            if item["span"] > lookahead:
                continue   # the resolver limits Span to the latency (tested below); the engine would read further
            params = {"span": item["span"], "speed": item["speed"], "pivot": item["pivot"], "mode": 0}
            rows = dict(resolve.derived_rows("reverse", block, params, item["t"]))
            self.assertAlmostEqual(item["t"] + lookahead - rows["depth"], item["s"], places=9, msg=item)
            self.assertGreaterEqual(rows["depth"], 0.0)
            self.assertLessEqual(rows["depth"], self.data["history"]["reverse"] + 1e-9)
            checked += 1
        self.assertGreater(checked, 500)

    def test_reverse_route_mode_reads_the_running_picture_and_slides_it(self):
        block = {"lookahead": 2.5, "duration": 12.166666667}
        for span, speed, t in itertools.product((0.15, 2.2, 2.5), (0.25, 1.8, 3), (0.0, 0.7, 5.3, 12.0)):
            rows = dict(resolve.derived_rows("reverse", block, {"span": span, "speed": speed, "pivot": 4, "mode": 1}, t))
            self.assertEqual(rows["depth"], 2.5)
            fraction = (t * speed) % max(0.15, span) / span   # visual-engine.mjs: mod(t * speed, max(.15, span)) / span
            self.assertAlmostEqual(rows["shift"], (1 - 2 * fraction) * 0.75)

    def test_stutter_rows_name_the_engines_source_time_and_the_gap(self):
        lookahead, checked = 1.0, 0
        block = {"lookahead": lookahead, "duration": self.data["duration"]}
        for item in self.data["stutter"]:
            params = {key: item[key] for key in ("window", "rate", "gap", "repeats", "capture")}
            rows = dict(resolve.derived_rows("stutter", block, params, item["t"]))
            if item["s"] is None:
                self.assertEqual(rows["pick"], 2, item)
            elif item["s"] == item["t"] and rows["pick"] == 0:
                self.assertEqual(rows["depth"], lookahead)   # outside the stutter: the running picture, read at the latency
            else:
                self.assertEqual(rows["pick"], 1, item)
                frozen = rows["record"] == 0
                newest = item["capture"] + item["window"] if frozen else item["t"] + lookahead
                self.assertAlmostEqual(newest - rows["depth"], item["s"], places=9, msg=item)
                self.assertLessEqual(rows["depth"], self.data["history"]["stutter"] + 1e-9)
                self.assertGreaterEqual(newest, item["s"] - 1e-9, "the repeat reads a frame the cache already holds")
            checked += 1
        self.assertGreater(checked, 5000)

    def test_the_repeat_cache_is_held_still_for_the_whole_stutter_and_not_before_the_window_arrived(self):
        block = {"lookahead": 1.0, "duration": 12.0}
        params = {"window": 0.24, "rate": 1.0, "gap": 0.035, "repeats": 4, "capture": 2.88}
        run = 4 * (0.24 + 0.035)
        for clock in (1.0, 2.0, 2.11):
            self.assertEqual(dict(resolve.derived_rows("stutter", block, params, clock))["record"], 1, clock)   # window not yet arrived (live edge = clock + 1 before 3.12)
        for clock in (2.5, 3.0, 2.88 + run - 0.01):
            if clock + 1.0 >= 2.88 + 0.24:
                self.assertEqual(dict(resolve.derived_rows("stutter", block, params, clock))["record"], 0, clock)
        self.assertEqual(dict(resolve.derived_rows("stutter", block, params, 2.88 + run + 0.5))["record"], 1)   # after the last repeat the picture records again

    def test_span_is_limited_to_the_declared_latency(self):
        spec = {"order": ["span"], "params": {"span": {"min": 0.15, "max": 5, "step": 0.01}}, "macro": [], "automation": [], "events": [], "limits": {"span": 2.5}}
        base = {"intensity": 1.0, "wet": 1.0, "params": {"span": 4.0}, "bypass": False, "solo": False}
        self.assertEqual(resolve.resolve_node(spec, base, 1.0)["params"]["span"], 2.5)
        base["params"]["span"] = 1.2
        self.assertEqual(resolve.resolve_node(spec, base, 1.0)["params"]["span"], 1.2)


class LatencyPlanTests(unittest.TestCase):
    def test_a_rack_without_lookahead_declares_no_latency(self):
        spec = spec_of(fixture("closed-inbox.json"))
        self.assertEqual(spec["contract"]["latency"]["seconds"], 0)
        self.assertEqual(spec["contract"]["latency"]["nodes"], [])
        self.assertFalse(any("align_" in o["name"] or o["name"].startswith("dry_") and o["name"] != "dry_audio" for o in spec["operators"]))
        for node in spec["contract"]["nodes"]:
            self.assertNotIn("lag", node["resolve"])

    def test_one_reverse_declares_its_lookahead_as_the_rack_latency(self):
        spec = spec_of(fixture("demo-reverse.json"))
        latency = spec["contract"]["latency"]
        self.assertEqual(latency["seconds"], 2.5)
        entry = {item["id"]: item for item in latency["nodes"]}
        self.assertEqual((entry["fx-1"]["lagIn"], entry["fx-1"]["lookahead"], entry["fx-1"]["lag"]), (0, 2.5, 2.5))
        self.assertEqual(entry["output"]["lag"], 2.5)
        self.assertTrue(any("capped at 2.5" in warning and "span is limited to 2.5" in warning for warning in spec["warnings"]))

    def test_two_lookahead_processors_in_series_add(self):
        patch = fixture("demo-reverse.json")
        module(patch, "fx-2", "reverse")
        patch["nodes"][-2]["type"] = "reverse"
        stutter = fixture("demo-stutter.json") if (FIXTURES / "demo-stutter.json").exists() else None
        if stutter:
            node = json.loads(json.dumps(next(n for n in stutter["nodes"] if n["type"] == "stutter")))
            node["id"] = "fx-2"
            patch["nodes"] = [n for n in patch["nodes"] if n["id"] != "fx-2"]
            patch["nodes"].insert(-1, node)
        connect(patch, [("source", "fx-1"), ("fx-1", "fx-2"), ("fx-2", "output")])
        spec = spec_of(patch)
        entry = {item["id"]: item for item in spec["contract"]["latency"]["nodes"]}
        second = 1.0 if stutter else 2.5
        self.assertEqual(entry["fx-1"]["lag"], 2.5)
        self.assertEqual((entry["fx-2"]["lagIn"], entry["fx-2"]["lookahead"], entry["fx-2"]["lag"]), (2.5, second, 2.5 + second))
        self.assertEqual(spec["contract"]["latency"]["seconds"], 2.5 + second)
        # the second processor evaluates its rules at its own clock, the rack time minus the lag of what it emits
        resolved = {node["id"]: node["resolve"] for node in spec["contract"]["nodes"]}
        self.assertEqual(resolved["fx-2"]["lag"], 2.5 + second)

    def test_parallel_branches_are_delayed_to_meet_and_the_dry_branch_matches(self):
        patch = fixture("demo-reverse.json")
        module(patch, "fx-2", "delay")
        patch["nodes"].insert(-1, {"id": "sum", "type": "mix", "gain": 1})
        connect(patch, [("source", "fx-1"), ("source", "fx-2"), ("fx-1", "sum"), ("fx-2", "sum"), ("sum", "output")])
        spec = spec_of(patch)
        self.assertEqual(spec["contract"]["latency"]["seconds"], 2.5)
        by_path = {o["path"]: o for o in spec["operators"]}
        # the delay's wire to the mix is 2.5 s short: it gets a cache read back 150 frames at 60 fps, picture, and a Delay CHOP for audio
        select = by_path["fx_sum/align_picture_1"]
        self.assertEqual(select["type"], "cacheselectTOP")
        self.assertEqual(evaluate(select["pars"]["index"]["expr"], {}), -150)
        self.assertEqual(by_path["fx_sum/align_audio_1"]["type"], "delayCHOP")
        self.assertEqual(by_path["fx_sum/align_audio_1"]["pars"]["delay"], 2.5)
        self.assertNotIn("fx_sum/align_picture_0", by_path)   # the reverse branch already has the longest lag
        # the reverse's own dry branch is delayed by its latency; the bypass switch and the wet / dry blend read it
        self.assertEqual(by_path["fx_fx_1/blend_picture"]["inputs"], ["dry_picture", "proc_picture"])
        self.assertEqual(by_path["fx_fx_1/bypass_picture"]["inputs"], ["blend_picture", "dry_picture"])
        self.assertEqual(by_path["fx_fx_1/dry_audio"]["inputs"], ["dry_audio_in"])
        self.assertEqual(by_path["fx_fx_1/out_control"]["inputs"], ["dry_control"])
        # a processor with no look-ahead keeps its plain dry branch
        self.assertEqual(by_path["fx_fx_2/blend_picture"]["inputs"], ["in_picture", "proc_picture"])
        # the delay behind no buffer still evaluates at the lag of what it emits: none, it is 2.5 s short and is aligned at the mix instead
        self.assertNotIn("lag", {node["id"]: node["resolve"] for node in spec["contract"]["nodes"]}["fx-2"])

    def test_a_processor_after_a_lookahead_processor_reads_its_own_clock(self):
        patch = fixture("demo-reverse.json")
        module(patch, "fx-2", "delay")
        connect(patch, [("source", "fx-1"), ("fx-1", "fx-2"), ("fx-2", "output")])
        spec = spec_of(patch)
        resolved = {node["id"]: node["resolve"] for node in spec["contract"]["nodes"]}
        self.assertEqual(resolved["fx-2"]["lag"], 2.5)
        self.assertEqual(resolved["fx-2"]["lookahead"], 0)
        table = next(o for o in spec["operators"] if o["path"] == "fx_fx_2/resolved")["table"]
        self.assertIn("clock", [row[0] for row in table])
        self.assertNotIn("depth", [row[0] for row in table])

    def test_every_processor_expression_evaluates_at_the_depth_the_resolver_names(self):
        """The generated expressions read the cache at depth * cookRate frames, bounded by the cache."""
        spec = spec_of(fixture("demo-reverse.json"))
        by_name = {o["name"]: o for o in spec["operators"] if o["parent"] == "fx_fx_1"}
        frames = by_name["rv_cache"]["pars"]["cachesize"]
        self.assertEqual(frames, 302)   # 5 s of history (2.5 s look-ahead + 2.5 s span) at 60 fps, plus two
        for depth in (0.0, 0.5, 2.5, 5.0, 9.0):
            rows = rows_for(None, {})
            rows.update({"depth": depth, "shift": 0.0})
            index = evaluate(by_name["rv_frame"]["pars"]["index"]["expr"], rows)
            self.assertEqual(index, -min(frames - 1, round(depth * 60)))
        rows.update({"shift": -0.375})
        self.assertEqual(evaluate(by_name["rv_move"]["pars"]["tx"]["expr"], rows), -0.375)

    def test_stutter_chain_picks_between_the_running_picture_a_repeat_and_nothing(self):
        stutter = fixture("demo-stutter.json") if (FIXTURES / "demo-stutter.json").exists() else None
        if not stutter:
            self.skipTest("no stutter demo fixture")
        spec = spec_of(stutter)
        by_name = {o["name"]: o for o in spec["operators"] if o["parent"] == "fx_fx_1"}
        self.assertEqual(by_name["st_pick"]["inputs"], ["st_live", "st_frame", "st_blank"])
        self.assertEqual(by_name["st_blank"]["pars"]["alpha"], 0)
        for pick in (0, 1, 2):
            rows = rows_for(None, {})
            rows["pick"] = float(pick)
            self.assertEqual(evaluate(by_name["st_pick"]["pars"]["index"]["expr"], rows), pick)
        rows["record"] = 0.0
        self.assertEqual(evaluate(by_name["st_cache"]["pars"]["active"]["expr"], rows), 0)
        self.assertEqual(evaluate(by_name["st_live"]["pars"]["index"]["expr"], {}), -60)   # one second of look-ahead at 60 fps
        self.assertEqual(spec["contract"]["latency"]["seconds"], 1.0)


class Backend:
    def __init__(self):
        self.sent, self.values = [], {}

    def send(self, address, args):
        self.sent.append((address, list(args)))

    def set(self, comp, par, value):
        self.values[(comp, par)] = value

    def get(self, comp, par):
        return self.values.get((comp, par))

    def rule(self, comp, kind, path, payload):
        return True


class AnnounceTests(unittest.TestCase):
    def link(self, name):
        osc = load("fx_td_osc", ROOT / "tools" / "td-network" / "python" / "fx_td_osc.py")
        spec = spec_of(fixture(name))
        backend = Backend()
        return osc.RackLink(spec["contract"], backend), backend, spec

    def test_a_rack_with_latency_announces_it_when_the_workbench_reports_its_count_and_when_asked_to_sync(self):
        link, backend, spec = self.link("demo-reverse.json")
        link.receive(spec["contract"]["rack"]["count"]["address"], [1])
        self.assertEqual(backend.sent, [("/fx/latency", [2.5])])
        backend.sent.clear()
        link.pulse("", spec["contract"]["rack"]["sync"]["par"])
        self.assertEqual(backend.sent, [("/fx/sync", []), ("/fx/latency", [2.5])])

    def test_a_rack_without_latency_stays_silent(self):
        link, backend, spec = self.link("closed-inbox.json")
        link.receive(spec["contract"]["rack"]["count"]["address"], [3])
        self.assertEqual(backend.sent, [])


class WorkbenchCompensationTests(unittest.TestCase):
    def test_the_contract_names_the_address_the_workbench_listens_on(self):
        spec = spec_of(fixture("demo-reverse.json"))
        self.assertEqual(spec["contract"]["latency"]["address"], "/fx/latency")


if __name__ == "__main__":
    unittest.main()
