"""Feedback's reset epoch in the generated network against the workbench's VisualEngine.feedbackEpoch.

The engine latches the epoch at the picture frame where Reset time becomes reached, and keeps it until the next crossing; an
automated Reset time that tracks the playhead never crosses, so the epoch cannot be recomputed from the current value
(Codex review of the feedback chain). `EpochLatch` in td_resolve walks the same frames. Each scenario is evaluated by the real
engine and by the latch over the same sequence of times, including backward seeks; they must agree. TouchDesigner is not used.
"""
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fx_td_resolve", ROOT / "tools" / "td-network" / "python" / "fx_td_resolve.py")
resolve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolve)

SCRIPT = """
import {VisualEngine} from './web/fx/visual-engine.mjs';
import {createModule, createSingleModulePatch} from './web/fx/core.mjs';
import {buildNetworkSpec} from './tools/td-network/network-spec.mjs';
const FPS = 12;
const canvas = () => ({width: 32, height: 18, getContext: () => ({globalAlpha: 1, drawImage() {}, fillRect() {}, clearRect() {}, setTransform() {}})});
const scenarios = {
  ramp: {params: {time: .25, repeats: 8, reset: 0}, lane: {interpolation: 'linear', keyframes: [{time: 0, value: 0}, {time: 4, value: 4}]}, times: [0, 0.5, 2, 3.9, 1, 4, 0]},
  crossing: {params: {reset: 1}, lane: {interpolation: 'linear', keyframes: [{time: 0, value: 1}, {time: 3, value: 2}]}, times: [0, 0.9, 1.2, 2.75, 1, 2, 3, 0.4]},
  step: {params: {reset: 0}, lane: {interpolation: 'step', keyframes: [{time: 0, value: 0}, {time: 1, value: 3}, {time: 2, value: 2}]}, times: [2.5, 1.5, 0.5, 1, 2, 2.9, 0]},
  constant: {params: {reset: 2.4}, lane: null, times: [0, 1, 2.3, 2.4, 2.5, 3, 1, 3.5]},
  never: {params: {reset: 11.5}, lane: null, times: [0, 5, 11, 3]},
};
const out = {};
for (const [name, item] of Object.entries(scenarios)) {
  const patch = createSingleModulePatch('feedback', {duration: 12, demonstration: false});
  const node = patch.nodes[1];
  Object.assign(node.params, item.params); node.macro = [];
  node.automation = item.lane ? [{parameter: 'params.reset', ...item.lane}] : [];
  const engine = new VisualEngine({duration: 12, fps: FPS}, {}, {width: 32, height: 18, createCanvas: canvas});
  engine.patch = patch; engine.nodeMap = new Map(patch.nodes.map(n => [n.id, n]));
  const contract = buildNetworkSpec(patch).contract;
  const entry = contract.nodes.find(n => n.id === node.id);
  const base = {intensity: node.intensity, wet: node.wet, params: {...node.params}, bypass: false, solo: false};
  out[name] = {resolve: entry.resolve, base, fps: FPS, steps: item.times.map(t => [t, engine.feedbackEpoch(node, t)])};
}
process.stdout.write(JSON.stringify(out));
"""


class EpochTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT], capture_output=True, text=True, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.data = json.loads(result.stdout)

    def test_the_latch_follows_the_engine_through_every_scenario_and_backward_seek(self):
        compared = 0
        for name, item in self.data.items():
            latch = resolve.EpochLatch(item["resolve"], item["base"], fps=item["fps"])
            for time, want in item["steps"]:
                self.assertAlmostEqual(latch.at(time), want, places=9, msg="%s at %r" % (name, time))
                compared += 1
        self.assertGreater(compared, 30)

    def test_a_reset_that_tracks_the_playhead_never_suppresses_the_returns(self):
        """The failure Codex found: epoch = Reset time whenever time >= Reset time; for the ramp reset = time that is the playhead
        itself, so passes = 0 throughout. The engine latches epoch 0 at the first frame."""
        ramp = self.data["ramp"]
        latch = resolve.EpochLatch(ramp["resolve"], ramp["base"], fps=ramp["fps"])
        self.assertEqual([latch.at(t) for t in (0.0, 1.0, 2.0, 3.5)], [0.0, 0.0, 0.0, 0.0])

    def test_the_epoch_row_is_written_for_feedback_only(self):
        ramp = self.data["ramp"]
        node = {"order": ramp["resolve"]["order"]}
        self.assertTrue(resolve.has_epoch(node))
        self.assertFalse(resolve.has_epoch({"order": ["a"]}))
        resolved = {"intensity": 1, "wet": 1, "bypass": False, "solo": False, "active": True, "params": {k: 0 for k in node["order"]}}
        self.assertEqual(resolve.resolved_rows(node, resolved, 1.5)[-1], ["epoch", 1.5])
        self.assertEqual(resolve.resolved_rows({"order": ["a"], }, {**resolved, "params": {"a": 0}})[-1][0], "param.a")


if __name__ == "__main__":
    unittest.main()
