"""Queue / Overflow's picture chain in the generated network against the workbench's own VisualEngine.

The real engine draws a Queue processor onto a recording canvas that keeps the transform stack: every copy it draws
(`drawImage(source, 0, 0, w, h)` under `translate(x, y)` and `rotate(angle)`) is captured with the source time its `sample` callback
was asked for, its size and where its centre ended up on the canvas. The network's operator expressions, evaluated for the same
parameters, time and seed, must read the same instant, scale, place and turn the same copies, in the same order. TouchDesigner is not used.
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
const W = 640, H = 360, SEED = __SEED__;
const calls = [];
// a 2D context that keeps the transform stack: a copy is recorded with its matrix
const context = () => {
  let m = [1, 0, 0, 1, 0, 0]; const stack = [];
  const mul = (a, b) => [a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3], a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5]];
  return {
    globalAlpha: 1, globalCompositeOperation: 'source-over', fillStyle: '', imageSmoothingEnabled: true,
    setTransform() {}, clearRect() {}, fillRect() {},
    save() { stack.push(m.slice()); }, restore() { m = stack.pop(); },
    translate(x, y) { m = mul(m, [1, 0, 0, 1, x, y]); },
    rotate(a) { m = mul(m, [Math.cos(a), Math.sin(a), -Math.sin(a), Math.cos(a), 0, 0]); },
    scale(x, y) { m = mul(m, [x, 0, 0, y, 0, 0]); },
    drawImage(source, ...rest) {
      const [x, y, w, h] = rest.length >= 4 ? rest : [rest[0], rest[1], null, null];
      const cw = w ?? W, ch = h ?? H;
      const cx = m[0] * (x + cw / 2) + m[2] * (y + ch / 2) + m[4], cy = m[1] * (x + cw / 2) + m[3] * (y + ch / 2) + m[5];
      calls.push({time: source?.time ?? null, w: cw, h: ch, cx, cy, angle: Math.atan2(m[1], m[0]), alpha: this.globalAlpha});
    },
  };
};
const canvas = () => { const c = context(); return {width: W, height: H, getContext: () => c}; };
const engine = new VisualEngine({duration: 12.166666667, fps: 12}, {}, {width: W, height: H, createCanvas: canvas});
engine.patch = {seed: SEED, nodes: [], connections: []};
const params = CATALOG_BY_TYPE.queue.params;
const corners = key => { const p = params.find(item => item.key === key); return [...new Set([p.min, p.default, p.max])]; };
const out = [];
for (const capacity of [...corners('capacity'), 3]) for (const rate of corners('rate')) for (const release of corners('release')) for (const burst of corners('burst')) for (const spread of [0, .95, 1]) for (const order of [0, 1])
  for (const t of [0, 0.3, 0.9, 1.7, 2.6, 3.9, 5.05, 8.2, 11.9]) {
    const p = {capacity, rate, release, burst, spread, order};
    calls.length = 0;
    engine.poolIndex = 0;
    engine.effect({id: 'fx-1', type: 'queue', params: p}, t, 'picture', time => ({time}));
    out.push({p, t, draws: calls.filter(call => call.time !== null).map(({time, w, h, cx, cy, angle}) => ({time, w, h, cx, cy, angle}))});
  }
process.stdout.write(JSON.stringify({W, H, out}));
"""


class QueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = spec_for(single_patch("queue"))
        # the engine adds the node id's character codes to the patch seed itself
        result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT.replace("__SEED__", str(cls.spec["patch"]["seed"]))], capture_output=True, text=True, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.data = json.loads(result.stdout)
        cls.chain = {o["name"]: o for o in cls.spec["operators"] if o["parent"] == "fx_fx_1" and o["name"].startswith("qu_")}

    def rows(self, p):
        return rows_for(None, {key: p[key] for key in ("capacity", "rate", "release", "burst", "spread", "order")})

    def test_every_copy_the_engine_draws_is_read_scaled_placed_turned_and_stacked_like_the_network_does(self):
        W, H, compared, overflowed, idle = self.data["W"], self.data["H"], 0, 0, 0
        for case in self.data["out"]:
            p, t, rows = case["p"], case["t"], self.rows(case["p"])
            draws = case["draws"]
            idle_opacity = evaluate(self.chain["qu_idle"]["pars"]["opacity"]["expr"], rows, t)
            if len(draws) == 1 and draws[0]["w"] == W and draws[0]["time"] == t and idle_opacity == 1:
                idle += 1   # nothing waiting: the engine draws the frame whole, and no entry is switched on
                self.assertEqual([evaluate(self.chain["qu_gain_%d" % j]["pars"]["opacity"]["expr"], rows, t) for j in range(24)], [0] * 24, case)
                continue
            self.assertEqual(idle_opacity, 0, case)
            self.assertLessEqual(len(draws), 24)
            for j in range(24):
                opacity = evaluate(self.chain["qu_gain_%d" % j]["pars"]["opacity"]["expr"], rows, t)
                if j >= len(draws):
                    self.assertEqual(opacity, 0, (j, case))
                    continue
                d = draws[j]
                self.assertEqual(opacity, 1, (j, case))
                move = self.chain["qu_move_%d" % j]["pars"]
                self.assertEqual((move["sx"], move["sy"]), (0.48, 0.48))
                self.assertAlmostEqual(d["w"] / W, 0.48)
                self.assertAlmostEqual(d["h"] / H, 0.48)
                tx, ty = evaluate(move["tx"]["expr"], rows, t), evaluate(move["ty"]["expr"], rows, t)
                self.assertAlmostEqual(tx, d["cx"] / W - 0.5, places=7, msg=(j, case))
                self.assertAlmostEqual(ty, -(d["cy"] / H - 0.5), places=7, msg=(j, case))
                # a canvas rotation is clockwise on screen, TouchDesigner's Rotate is counter-clockwise
                self.assertAlmostEqual(evaluate(move["rotate"]["expr"], rows, t), -math.degrees(d["angle"]), places=6, msg=(j, case))
                index = evaluate(self.chain["qu_frame_%d" % j]["pars"]["index"]["expr"], rows, t)
                size = evaluate(self.chain["qu_cache"]["pars"]["cachesize"]["expr"], rows, t)
                self.assertEqual(index, -round((t - d["time"]) * 60), (j, case))
                self.assertLess(-index, size)
                overflowed += abs(d["angle"]) > 0
                compared += 1
        self.assertGreater(compared, 10000)
        self.assertGreater(overflowed, 2000)   # the scatter and turn of an overflowing queue are compared, not only the orderly stack
        self.assertGreater(idle, 100)

    def test_the_noise_is_the_workbench_hash_bit_for_bit(self):
        # the engine keeps its hash private, so the formula is checked to be the engine's text and then run on a grid in JavaScript
        # next to the Python expression the network carries
        source = (ROOT / "web" / "fx" / "visual-engine.mjs").read_text()
        self.assertIn("let n = (seed ^ Math.imul(i + 1, 374761393) ^ Math.imul(j + 1, 668265263)) >>> 0;", source)
        self.assertIn("n = Math.imul(n ^ n >>> 13, 1274126177); return ((n ^ n >>> 16) >>> 0) / 4294967296;", source)
        script = ("import {noiseText} from './tools/td-network/effects.mjs';"
                  "const noise = (seed, i, j = 0) => { let n = (seed ^ Math.imul(i + 1, 374761393) ^ Math.imul(j + 1, 668265263)) >>> 0; n = Math.imul(n ^ n >>> 13, 1274126177); return ((n ^ n >>> 16) >>> 0) / 4294967296; };"
                  "const out = []; for (const seed of [1, 773, 0, 4294967295, 12345678, 2147483648]) for (let i = 0; i < 60; i++) for (const j of [0, 5, 1, 17, 99999]) out.push([noiseText(seed, String(i), String(j)), noise(seed, i, j)]);"
                  "process.stdout.write(JSON.stringify(out));")
        result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, cwd=ROOT, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = json.loads(result.stdout)
        for text, expected in pairs:
            self.assertEqual(eval(text, {"math": math}), expected, text)
        self.assertEqual(len(pairs), 1800)

    def test_every_expression_is_finite_at_the_catalog_minimum_default_and_maximum(self):
        from tests.test_td_effects import catalog
        params, evaluated = catalog("queue"), 0
        for corner in ("min", "default", "max"):
            rows = rows_for(None, {p["key"]: p[corner] for p in params})
            for operator in self.chain.values():
                for name, value in operator["pars"].items():
                    if isinstance(value, dict) and "resolved" in value:
                        for time in (0.0, 0.4, 3.7, 12.0):
                            self.assertTrue(math.isfinite(float(evaluate(value["expr"], rows, time))), (corner, operator["name"], name, time))
                            evaluated += 1
        self.assertGreater(evaluated, 1000)

    def test_the_entries_are_stacked_with_the_first_at_the_bottom_over_the_idle_frame(self):
        self.assertEqual(self.chain["qu_over_0"]["inputs"], ["qu_gain_0", "qu_idle"])
        for j in range(1, 24):
            self.assertEqual(self.chain["qu_over_%d" % j]["inputs"], ["qu_gain_%d" % j, "qu_over_%d" % (j - 1)])
        proc = next(o for o in self.spec["operators"] if o["name"] == "proc_picture")
        self.assertEqual(proc["inputs"], ["qu_over_23"])

    def test_the_oldest_entry_is_inside_the_cache_at_every_cook_rate(self):
        import tests.test_td_effects as effects
        original = effects.Project.cookRate
        try:
            for rate in (24.0, 60.0, 120.0):
                effects.Project.cookRate = rate
                rows = self.rows({"capacity": 24, "rate": 12, "release": 0.25, "burst": 6, "spread": 0.5, "order": 0})
                size = evaluate(self.chain["qu_cache"]["pars"]["cachesize"]["expr"], rows, 5.99)
                for j in range(24):
                    index = evaluate(self.chain["qu_frame_%d" % j]["pars"]["index"]["expr"], rows, 5.99)
                    self.assertLess(-index, size, (rate, j))
        finally:
            effects.Project.cookRate = original

    def test_the_spec_says_what_is_not_built(self):
        text = " ".join(self.spec["warnings"])
        self.assertIn("text port", text)


if __name__ == "__main__":
    unittest.main()
