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
  blip: {params: {reset: 0}, lane: {interpolation: 'step', keyframes: [{time: 0, value: 0}, {time: 1.02, value: 3}, {time: 1.04, value: 0}]}, times: [0, 1.0, 1.1, 2, 0.5, 3]},
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
  out[name] = {resolve: entry.resolve, base, fps: contract.pictureFps, steps: item.times.map(t => [t, engine.feedbackEpoch(node, t)])};
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

    def test_production_uses_the_contracts_picture_rate_not_a_fixed_grid(self):
        """Codex: the latch ran on a 60 Hz grid in production while the engine samples crossings at the media's frame rate; a 20 ms
        blip of Reset time between two 12 fps frames is a crossing on one grid and not on the other."""
        blip = self.data["blip"]
        self.assertEqual(blip["fps"], 12)
        self.assertEqual(resolve.picture_fps({"pictureFps": 12}), 12.0)
        self.assertEqual(resolve.picture_fps({}), resolve.EPOCH_FPS)
        self.assertEqual(resolve.picture_fps({"pictureFps": -3}), resolve.EPOCH_FPS)
        engine_epochs = [epoch for _, epoch in blip["steps"]]
        self.assertEqual(engine_epochs, [0, 0, 0, 0, 0, 0], "the engine never sees the blip")
        production = resolve.EpochLatch(blip["resolve"], blip["base"], resolve.picture_fps({"pictureFps": blip["fps"]}))
        self.assertEqual([production.at(t) for t, _ in blip["steps"]], engine_epochs)
        sixty = resolve.EpochLatch(blip["resolve"], blip["base"], fps=60.0)
        self.assertGreater(sixty.at(2.0), 1.0, "on a 60 Hz grid the blip is a crossing: the grid matters")

    def test_a_reset_that_tracks_the_playhead_never_suppresses_the_returns(self):
        """The failure Codex found: epoch = Reset time whenever time >= Reset time; for the ramp reset = time that is the playhead
        itself, so passes = 0 throughout. The engine latches epoch 0 at the first frame."""
        ramp = self.data["ramp"]
        latch = resolve.EpochLatch(ramp["resolve"], ramp["base"], fps=ramp["fps"])
        self.assertEqual([latch.at(t) for t in (0.0, 1.0, 2.0, 3.5)], [0.0, 0.0, 0.0, 0.0])

    def test_a_sidechain_modulated_reset_is_judged_on_the_levels_it_had(self):
        """CodeRabbit: Reset time 0 with additive modulation of 3 and a sidechain level of 1 on [1, 2) and 0 elsewhere. The workbench
        evaluates its envelope at each historical frame, so Reset time is 3 on those frames only and is reached at every other frame:
        the epoch latches at the last frame where it is reached again, 2. Applying the latest level to every walked frame latched 13/12."""
        spec = {"order": ["reset"], "params": {"reset": {"min": 0, "max": 12, "step": 0.01}}, "macro": [], "automation": [], "events": [],
                "modulation": {"parameter": "params.reset", "mode": "add", "amount": 3}}
        base = {"intensity": 1.0, "wet": 1.0, "params": {"reset": 0.0}, "bypass": False, "solo": False}
        level = lambda t: 1.0 if 1.0 <= t < 2.0 else 0.0
        fps = 12.0

        def workbench(time):   # every frame, with the level that frame had
            active, epoch = False, 0.0
            for frame in range(int(time * fps + 1e-6) + 1):
                reached = frame / fps + 1e-9 >= resolve.resolve_node(spec, base, frame / fps, level(frame / fps))["params"]["reset"]
                if reached and not active:
                    epoch = frame / fps
                active = reached
            return epoch

        latch = resolve.EpochLatch(spec, base, fps)
        for time in (0.0, 1.0, 2.0):   # ticks at the moments the level changes
            self.assertAlmostEqual(latch.at(time, level(time)), workbench(time), places=9, msg=time)
        self.assertAlmostEqual(latch.at(2.0, level(2.0)), 2.0)
        playing = resolve.EpochLatch(spec, base, fps)   # a tick at every frame, then a backward seek and forward again
        for frame in range(0, 37):
            playing.at(frame / fps, level(frame / fps))
        for time in (0.5, 1.5, 3.0, 0.0, 2.0):
            self.assertAlmostEqual(playing.at(time, level(time)), workbench(time), places=9, msg=time)

    def test_the_level_a_frame_was_judged_on_is_kept_when_the_playhead_cooks_faster_than_the_pictures(self):
        """Codex: with several cooks inside one 12 fps frame, a later cook must not overwrite the level that frame was judged on; a
        backward seek and a return would otherwise give a different epoch."""
        spec = {"order": ["reset"], "params": {"reset": {"min": 0, "max": 12, "step": 0.01}}, "macro": [], "automation": [], "events": [],
                "modulation": {"parameter": "params.reset", "mode": "add", "amount": 3}}
        base = {"intensity": 1.0, "wet": 1.0, "params": {"reset": 0.0}, "bypass": False, "solo": False}
        latch = resolve.EpochLatch(spec, base, 12.0)
        for time, level in ((0.0, 0.0), (1 / 12, 1.0), (0.15, 0.0), (2 / 12, 0.0)):
            epoch = latch.at(time, level)
        self.assertAlmostEqual(epoch, 2 / 12)
        self.assertEqual(latch.at(0.0, 0.0), 0.0)
        self.assertAlmostEqual(latch.at(2 / 12, 0.0), 2 / 12, msg="the same history after a seek and a return")
        self.assertEqual(latch.levels[1], 1.0)

    def test_a_backward_seek_replays_from_a_saved_state_and_gives_the_same_epoch(self):
        """CodeRabbit on #150: every backward seek replayed from frame 0, so scrubbing backward cost quadratic time. The latch now keeps
        a state every CHECKPOINT frames; the answers must equal a latch that replays from the start, and a seek must walk at most
        CHECKPOINT frames."""
        spec = {"order": ["reset"], "params": {"reset": {"min": 0, "max": 12, "step": 0.01}}, "macro": [], "automation": [
            {"parameter": "params.reset", "interpolation": "step", "keyframes": [{"time": 0, "value": 0}, {"time": 7.0, "value": 40}, {"time": 14.0, "value": 0},
                                                                              {"time": 21.0, "value": 40}, {"time": 28.0, "value": 0}]}], "events": []}
        base = {"intensity": 1.0, "wet": 1.0, "params": {"reset": 0.0}, "bypass": False, "solo": False}
        fps = 12.0
        walked = []
        original = resolve.resolve_node

        def counting(*args, **kwargs):
            walked.append(1)
            return original(*args, **kwargs)

        fast, slow = resolve.EpochLatch(spec, base, fps), resolve.EpochLatch(spec, base, fps)
        slow.CHECKPOINT = 10 ** 9   # never saves a state: every backward seek replays from the start
        resolve.resolve_node = counting
        try:
            fast.at(40.0)
            slow.at(40.0)
            for time in (35.5, 35.0, 29.9, 28.0, 22.2, 14.01, 13.9, 7.5, 6.9, 0.0, 12.0, 30.0, 27.99, 40.0):
                before, was = len(walked), fast.frame
                answer = fast.at(time)
                cost = len(walked) - before
                self.assertEqual(answer, slow.at(time), msg=time)
                target = int(max(0.0, time) * fps + 1e-6)
                if target < was:   # a backward seek; going forward walks the frames in between
                    self.assertLessEqual(cost, resolve.EpochLatch.CHECKPOINT, msg=time)
        finally:
            resolve.resolve_node = original

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
