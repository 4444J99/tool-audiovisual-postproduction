"""Routing's picture chain in the generated network against the workbench's own VisualEngine.

The real engine draws a Routing processor onto a recording canvas: each `drawImage(source, x, y, w, h)` it makes is captured with the
alpha it was drawn at and the source time it asked for (the sample callback returns a marker carrying that time). The network's operator
expressions, evaluated for the same parameters and time, must place, scale, fade and read the same copies. TouchDesigner is not used.
"""
import itertools
import json
import math
import subprocess
import unittest
from pathlib import Path

from tests.test_td_effects import evaluate, rows_for, spec_for, single_patch

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = """
import {VisualEngine} from './web/fx/visual-engine.mjs';
import {CATALOG_BY_TYPE} from './web/fx/catalog.mjs';
const W = 640, H = 360;
const calls = [];
const context = () => ({
  globalAlpha: 1, globalCompositeOperation: 'source-over', fillStyle: '', imageSmoothingEnabled: true,
  setTransform() {}, clearRect() {}, fillRect() {}, save() {}, restore() {}, translate() {}, rotate() {}, scale() {},
  drawImage(source, ...rest) { calls.push({time: source?.time ?? null, rect: rest, alpha: this.globalAlpha}); },
});
const canvas = () => { const c = context(); return {width: W, height: H, getContext: () => c}; };
const engine = new VisualEngine({duration: 12.166666667, fps: 12}, {}, {width: W, height: H, createCanvas: canvas});
engine.patch = {seed: 1, nodes: [], connections: []};
const params = CATALOG_BY_TYPE.routing.params;
const corners = key => { const p = params.find(item => item.key === key); return [...new Set([p.min, p.default, p.max])]; };
const out = [];
for (const travel of corners('travel')) for (const copies of corners('copies')) for (const spread of corners('spread')) for (const origin of [0, .08, .5]) for (const destination of [.5, .92, 1])
  for (const t of [0, 0.3, 0.9, 1.7, 2.6, 5.05, 11.9]) {
    const p = {travel, copies, spread, arrival: 1, origin, destination};
    calls.length = 0;
    const sample = time => ({time});
    engine.poolIndex = 0;
    engine.effect({id: 'fx-1', type: 'routing', params: p}, t, 'picture', sample);
    out.push({p, t, draws: calls.filter(call => call.time !== null).map(call => ({time: call.time, rect: call.rect, alpha: call.alpha}))});
  }
process.stdout.write(JSON.stringify({W, H, out}));
"""


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT], capture_output=True, text=True, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.data = json.loads(result.stdout)
        cls.spec = spec_for(single_patch("routing"))
        cls.chain = {o["name"]: o for o in cls.spec["operators"] if o["parent"] == "fx_fx_1" and o["name"].startswith("rt_")}

    def rows(self, p):
        return rows_for(None, {key: p[key] for key in ("travel", "copies", "spread", "arrival", "origin", "destination")})

    def test_every_copy_the_engine_draws_is_placed_scaled_faded_and_read_like_the_network_does(self):
        W, H, compared = self.data["W"], self.data["H"], 0
        for case in self.data["out"]:
            p, t, rows = case["p"], case["t"], self.rows(case["p"])
            count = round(p["copies"])
            drawn = {round((t - d["time"]) / 0.085): d for d in case["draws"] if d["time"] is not None}
            self.assertEqual(sorted(drawn), list(range(count)), case)
            for k in range(8):
                opacity = evaluate(self.chain["rt_gain_%d" % k]["pars"]["opacity"]["expr"], rows, t)
                if k >= count:
                    self.assertEqual(opacity, 0, (k, case))
                    continue
                d = drawn[k]
                x, y, w, h = d["rect"]
                self.assertAlmostEqual(opacity, d["alpha"], places=9, msg=(k, case))
                move = self.chain["rt_move_%d" % k]["pars"]
                self.assertEqual((move["sx"], move["sy"]), (0.62, 0.62))
                self.assertAlmostEqual(w / W, 0.62)
                self.assertAlmostEqual(h / H, 0.62)
                # the engine's top-left corner, as the centre's offset from the frame centre (y up in TouchDesigner)
                tx = evaluate(move["tx"]["expr"], rows, t)
                ty = evaluate(move["ty"]["expr"], rows, t)
                self.assertAlmostEqual(tx, (x + w / 2) / W - 0.5, places=9, msg=(k, case))
                self.assertAlmostEqual(ty, -((y + h / 2) / H - 0.5), places=9, msg=(k, case))
                # the copy reads k x 0.085 s earlier: a cache read back that many frames
                index = evaluate(self.chain["rt_frame_%d" % k]["pars"]["index"]["expr"], rows, t)
                size = evaluate(self.chain["rt_cache"]["pars"]["cachesize"]["expr"], rows, t)
                self.assertEqual(index, -min(size - 1, round(k * 0.085 * 60)))
                compared += 1
        self.assertGreater(compared, 6000)

    def test_every_expression_is_finite_at_the_catalog_minimum_default_and_maximum(self):
        from tests.test_td_effects import catalog
        params, evaluated = catalog("routing"), 0
        for corner in ("min", "default", "max"):
            rows = rows_for(None, {p["key"]: p[corner] for p in params})
            for operator in self.chain.values():
                for name, value in operator["pars"].items():
                    if isinstance(value, dict) and "resolved" in value:
                        for time in (0.0, 0.4, 3.7, 12.0):
                            self.assertTrue(math.isfinite(float(evaluate(value["expr"], rows, time))), (corner, operator["name"], name, time))
                            evaluated += 1
        self.assertGreater(evaluated, 300)

    def test_the_copies_are_laid_over_each_other_with_copy_zero_on_top(self):
        self.assertEqual(self.chain["rt_over_7"]["inputs"], ["rt_gain_7", "rt_base"])
        for k in range(7):
            self.assertEqual(self.chain["rt_over_%d" % k]["inputs"], ["rt_gain_%d" % k, "rt_over_%d" % (k + 1)])
        proc = next(o for o in self.spec["operators"] if o["name"] == "proc_picture")
        self.assertEqual(proc["inputs"], ["rt_over_0"])
        self.assertEqual(self.chain["rt_base"]["pars"]["alpha"], 0)

    def test_the_oldest_copy_is_inside_the_cache(self):
        import tests.test_td_effects as effects
        original = effects.Project.cookRate
        try:
            for rate in (24.0, 60.0, 120.0):
                effects.Project.cookRate = rate
                size = evaluate(self.chain["rt_cache"]["pars"]["cachesize"]["expr"], {})
                oldest = evaluate(self.chain["rt_frame_7"]["pars"]["index"]["expr"], {})
                self.assertGreaterEqual(size, round(7 * 0.085 * rate) + 2, rate)
                self.assertEqual(oldest, -round(7 * 0.085 * rate), rate)
        finally:
            effects.Project.cookRate = original


if __name__ == "__main__":
    unittest.main()
