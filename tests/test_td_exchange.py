"""Exchange's picture chain in the generated network against the workbench's own VisualEngine.

The real engine draws an Exchange processor onto a recording canvas: every strip it deals (`drawImage(source, sx, 0, sw, H, dx, 0, dw, H)`)
is captured with the source time its `sample` callback was asked for. The network's Crop and Transform expressions, evaluated for the same
parameters and time, must cut the same strip from the same time and put it in the same slot. TouchDesigner is not used.
"""
import json
import math
import subprocess
import unittest
from pathlib import Path

from tests.test_td_effects import evaluate, rows_for, spec_for, single_patch

ROOT = Path(__file__).resolve().parents[1]
REGIONS = 5

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
const params = CATALOG_BY_TYPE.exchange.params;
const corners = key => { const p = params.find(item => item.key === key); return [...new Set([p.min, p.default, p.max])]; };
const out = [];
for (const interval of [...corners('interval'), 0.31]) for (const offset of [...corners('offset'), 4, 5, 7]) for (const timeOffset of corners('timeOffset')) for (const mode of [0, 1, 2])
  for (const t of [0, 0.05, 0.3, 0.9, 1.7, 2.6, 5.05, 7.4, 11.9]) {
    const p = {interval, offset, timeOffset, mode, transition: 0.05};
    calls.length = 0;
    engine.poolIndex = 0;
    engine.effect({id: 'fx-1', type: 'exchange', params: p}, t, 'picture', time => ({time}));
    out.push({p, t, draws: calls.filter(call => call.time !== null).map(call => ({time: call.time, rect: call.rect}))});
  }
process.stdout.write(JSON.stringify({W, H, out}));
"""


class ExchangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT], capture_output=True, text=True, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.data = json.loads(result.stdout)
        cls.spec = spec_for(single_patch("exchange"))
        cls.chain = {o["name"]: o for o in cls.spec["operators"] if o["parent"] == "fx_fx_1" and o["name"].startswith("xc_")}

    def rows(self, p):
        return rows_for(None, {key: p[key] for key in ("interval", "offset", "timeOffset", "mode", "transition")})

    def slot_strips(self, rows, t):
        return [round(evaluate(self.chain["xc_crop_%d" % d]["pars"]["cropleft"]["expr"], rows, t) * REGIONS) for d in range(REGIONS)]

    def test_every_strip_the_engine_deals_is_cut_and_placed_like_the_network_does(self):
        W, compared = self.data["W"], 0
        for case in self.data["out"]:
            p, t, rows = case["p"], case["t"], self.rows(case["p"])
            picked = evaluate(self.chain["xc_pick"]["pars"]["index"]["expr"], rows, t)
            if p["mode"] == 1:
                # voice addresses: the engine draws the frame whole and the network's switch passes the input
                self.assertEqual(picked, 0, case)
                self.assertEqual(len(case["draws"]), 1)
                self.assertEqual(case["draws"][0]["time"], t)
                continue
            self.assertEqual(picked, 1, case)
            self.assertEqual(len(case["draws"]), REGIONS, case)
            strips = self.slot_strips(rows, t)
            self.assertEqual(sorted(strips), list(range(REGIONS)), case)   # a permutation: every strip once
            for draw in case["draws"]:
                sx, _, sw, _, dx, _, dw, _ = draw["rect"]
                strip, slot = round(sx / (W / REGIONS)), round(dx / (W / REGIONS))
                self.assertAlmostEqual(sw, W / REGIONS)
                self.assertAlmostEqual(dw, W / REGIONS)
                self.assertEqual(strips[slot], strip, (case, draw))
                # the crop's edges are that strip's edges, and the slot's centre is where the engine drew it
                crop = self.chain["xc_crop_%d" % slot]["pars"]
                self.assertAlmostEqual(evaluate(crop["cropleft"]["expr"], rows, t), sx / W)
                self.assertAlmostEqual(evaluate(crop["cropright"]["expr"], rows, t), (sx + sw) / W)
                self.assertAlmostEqual(self.chain["xc_slot_%d" % slot]["pars"]["tx"], (dx + dw / 2) / W - 0.5)
                # the source time is the playhead less History offset, never before the start of the media
                self.assertAlmostEqual(draw["time"], max(0, t - p["timeOffset"]), places=9, msg=case)
                compared += 1
        self.assertGreater(compared, 5000)

    def test_the_history_read_is_the_engine_source_time_once_the_history_exists(self):
        read = 0
        for case in self.data["out"]:
            p, t = case["p"], case["t"]
            if p["mode"] == 1 or t < p["timeOffset"]:
                continue
            rows = self.rows(p)
            size = evaluate(self.chain["xc_cache"]["pars"]["cachesize"]["expr"], rows, t)
            index = evaluate(self.chain["xc_frame"]["pars"]["index"]["expr"], rows, t)
            self.assertEqual(index, -round(p["timeOffset"] * 60), case)
            self.assertLess(-index, size)
            self.assertAlmostEqual(t + index / 60, case["draws"][0]["time"], delta=1 / 120 + 1e-9, msg=case)
            read += 1
        self.assertGreater(read, 300)

    def test_every_expression_is_finite_at_the_catalog_minimum_default_and_maximum(self):
        from tests.test_td_effects import catalog
        params, evaluated = catalog("exchange"), 0
        for corner in ("min", "default", "max"):
            rows = rows_for(None, {p["key"]: p[corner] for p in params})
            for operator in self.chain.values():
                for name, value in operator["pars"].items():
                    if isinstance(value, dict) and "resolved" in value:
                        for time in (0.0, 0.4, 3.7, 12.0):
                            self.assertTrue(math.isfinite(float(evaluate(value["expr"], rows, time))), (corner, operator["name"], name, time))
                            evaluated += 1
        self.assertGreater(evaluated, 100)

    def test_the_slots_are_laid_side_by_side_and_feed_the_processor_slot(self):
        self.assertEqual(self.chain["xc_over_3"]["inputs"], ["xc_slot_3", "xc_slot_4"])
        for d in range(3):
            self.assertEqual(self.chain["xc_over_%d" % d]["inputs"], ["xc_slot_%d" % d, "xc_over_%d" % (d + 1)])
        self.assertEqual(self.chain["xc_pick"]["inputs"], ["in_picture", "xc_over_0"])
        proc = next(o for o in self.spec["operators"] if o["name"] == "proc_picture")
        self.assertEqual(proc["inputs"], ["xc_pick"])

    def test_the_spec_says_what_is_assumed(self):
        text = " ".join(self.spec["warnings"])
        self.assertIn("5 strips", text)
        self.assertIn("voice addresses are not built", text)

    def test_the_history_is_inside_the_cache_at_every_cook_rate(self):
        import tests.test_td_effects as effects
        original = effects.Project.cookRate
        try:
            for rate in (24.0, 60.0, 120.0):
                effects.Project.cookRate = rate
                rows = self.rows({"interval": 0.72, "offset": 1, "timeOffset": 3, "mode": 0, "transition": 0.05})
                size = evaluate(self.chain["xc_cache"]["pars"]["cachesize"]["expr"], rows)
                index = evaluate(self.chain["xc_frame"]["pars"]["index"]["expr"], rows)
                self.assertEqual(index, -round(3 * rate), rate)
                self.assertGreaterEqual(size, round(3 * rate) + 2, rate)
        finally:
            effects.Project.cookRate = original


if __name__ == "__main__":
    unittest.main()
