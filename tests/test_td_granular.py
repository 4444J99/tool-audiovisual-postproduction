"""Granular's picture chain in the generated network against the workbench's own VisualEngine.

The real engine draws a Granular processor onto a recording canvas that keeps the transform stack: every tile it draws
(`drawImage(bin, sx, sy, cw, ch, -cw/2, -ch/2, cw * 1.12, ch * 1.12)` under `translate` and `rotate`) is captured with the
source rectangle, the canvas position and turn it ended up with and the source time of the bin it came from. The network's
expressions (the Crop, Transform and Cache Select parameters, with the `gr_math` module they call), evaluated for the same
parameters, time and seed, must cut, place, turn and read the same tiles in the same order. TouchDesigner is not used.
"""
import json
import math
import subprocess
import unittest
from pathlib import Path

import tests.test_td_effects as effects
from tests.test_td_effects import evaluate, rows_for, single_patch, spec_for

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = """
import {VisualEngine} from './web/fx/visual-engine.mjs';
import {CATALOG_BY_TYPE} from './web/fx/catalog.mjs';
const W = __W__, H = __H__, SEED = __SEED__;
const calls = [];
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
      if (rest.length !== 8) return;
      const [sx, sy, sw, sh, dx, dy, dw, dh] = rest;
      const cx = m[0] * (dx + dw / 2) + m[2] * (dy + dh / 2) + m[4], cy = m[1] * (dx + dw / 2) + m[3] * (dy + dh / 2) + m[5];
      calls.push({time: source?.time ?? null, src: [sx, sy, sw, sh], dw, dh, cx, cy, angle: Math.atan2(m[1], m[0])});
    },
  };
};
const canvas = () => { const c = context(); return {width: W, height: H, getContext: () => c}; };
const engine = new VisualEngine({duration: 12.166666667, fps: 12}, {}, {width: W, height: H, createCanvas: canvas});
engine.patch = {seed: SEED, nodes: [], connections: []};
const out = [];
for (const tiles of __TILES__) for (const size of __SIZES__) for (const scatter of [0, 0.7, 2]) for (const spatial of [0, 0.95, 1]) for (const order of [0, 1])
  for (const t of __TIMES__) {
    const p = {size, density: 30, scatter, pitch: 7, spatial, tiles, order};
    calls.length = 0; engine.poolIndex = 0;
    engine.effect({id: 'fx-1', type: 'granular', params: p}, t, 'picture', time => ({time}));
    out.push({p, t, draws: calls.map(call => ({...call}))});
  }
process.stdout.write(JSON.stringify({W, H, out}));
"""


class Module:
    """What `op('gr_math').module` is in TouchDesigner: the Text DAT's code, run."""
    def __init__(self, text):
        namespace = {}
        exec(compile(text, "gr_math", "exec"), namespace)
        self.__dict__.update(namespace)


class GranularTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = spec_for(single_patch("granular"))
        # the engine adds the node id's character codes to the patch seed itself
        cls.data = cls.engine(640, 360, [2, 5, 7, 14, 24], [0.015, 0.07, 0.5], [0, 0.31, 2.6, 7.4])
        cls.chain = {o["name"]: o for o in cls.spec["operators"] if o["parent"] == "fx_fx_1" and o["name"].startswith("gr_")}
        cls.module = Module(cls.chain["gr_math"]["text"])
        original = effects.Comp.op
        cls.original_op = original

        def op(comp, name):
            if name == "gr_math":
                return type("Op", (), {"module": cls.module})()
            return original(comp, name)
        effects.Comp.op = op

    @classmethod
    def engine(cls, width, height, tiles, sizes, times):
        script = SCRIPT.replace("__SEED__", str(cls.spec["patch"]["seed"])).replace("__W__", str(width)).replace("__H__", str(height)).replace("__TILES__", json.dumps(tiles)).replace("__SIZES__", json.dumps(sizes)).replace("__TIMES__", json.dumps(times))
        result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        effects.Comp.op = cls.original_op

    def rows(self, p, t=0.0):
        # behind a look-ahead buffer the processor evaluates at its own clock, the `clock` row the resolver writes: the time the engine drew at
        rows = rows_for(None, {key: p[key] for key in ("size", "density", "scatter", "pitch", "spatial", "tiles", "order")})
        rows["clock"] = t
        return rows

    def pars(self, name):
        return self.chain[name]["pars"]

    def test_every_tile_the_engine_draws_is_cut_scaled_placed_turned_and_stacked_like_the_network_does(self):
        W, H, compared, scattered = self.data["W"], self.data["H"], 0, 0
        L = self.spec["contract"]["latency"]["seconds"]
        self.assertEqual(L, 2)
        for case in self.data["out"]:
            p, t = case["p"], case["t"]
            rows = self.rows(p, t)
            draws = case["draws"]
            cols = round(p["tiles"])
            grid = cols * max(2, round(cols * H / W))
            self.assertEqual(len(draws), grid, case)
            for i, d in enumerate(draws):
                sx, sy, sw, sh = d["src"]
                crop = self.pars("gr_crop_%d" % i)
                left = evaluate(crop["cropleft"]["expr"], rows, t)
                self.assertAlmostEqual(left, sx / W, places=9, msg=(i, case))
                self.assertAlmostEqual(evaluate(crop["cropright"]["expr"], rows, t) - left, sw / W, places=9, msg=(i, case))
                self.assertAlmostEqual(evaluate(crop["croptop"]["expr"], rows, t), 1 - sy / H, places=9, msg=(i, case))
                self.assertAlmostEqual(evaluate(crop["cropbottom"]["expr"], rows, t), 1 - (sy + sh) / H, places=9, msg=(i, case))
                tile = self.pars("gr_tile_%d" % i)
                # a custom output resolution stretches the cropped tile across the canvas: the scale returns it to the drawn size
                self.assertAlmostEqual(evaluate(tile["sx"]["expr"], rows, t), d["dw"] / W, places=9, msg=(i, case))
                self.assertAlmostEqual(evaluate(tile["sy"]["expr"], rows, t), d["dh"] / H, places=9, msg=(i, case))
                self.assertAlmostEqual(evaluate(tile["tx"]["expr"], rows, t), d["cx"] / W - 0.5, places=9, msg=(i, case))
                self.assertAlmostEqual(evaluate(tile["ty"]["expr"], rows, t), -(d["cy"] / H - 0.5), places=9, msg=(i, case))
                self.assertAlmostEqual(evaluate(tile["rotate"]["expr"], rows, t), -math.degrees(d["angle"]), places=6, msg=(i, case))
                self.assertEqual(evaluate(self.pars("gr_gain_%d" % i)["opacity"]["expr"], rows, t), 1, (i, case))
                # the tile reads its bin (i mod 5), which shows source time t + offset: the network's read is that far behind the input's live edge
                self.assertEqual(self.chain["gr_crop_%d" % i]["inputs"], ["gr_bin_%d" % (i % 5)])
                index = evaluate(self.pars("gr_bin_%d" % (i % 5))["index"]["expr"], rows, t)
                self.assertAlmostEqual((t + L) + index / 60, d["time"], delta=1 / 120 + 1e-9, msg=(i, case))
                size = evaluate(self.pars("gr_cache")["cachesize"]["expr"], rows, t)
                self.assertLess(-index, size)
                scattered += abs(d["angle"]) > 0
                compared += 1
            for i in range(grid, 336):
                self.assertEqual(evaluate(self.pars("gr_gain_%d" % i)["opacity"]["expr"], rows, t), 0, (i, case))
        self.assertGreater(compared, 80000)
        self.assertGreater(scattered, 40000)

    def test_a_square_picture_gets_a_tile_bank_that_holds_all_its_rows(self):
        """Codex P2 on this pull request: the grid's rows follow the input's aspect, so the bank is sized for the declared picture."""
        import tempfile
        self.assertEqual(sum(1 for name in self.chain if name.startswith("gr_gain_")), 336)   # 16:9: 24 columns by 14 rows
        directory = Path(tempfile.mkdtemp(prefix="td-gr-"))
        banks = {}
        for size, rows in (("1080x1080", 24), ("1080x1920", 43), ("1920x1080", 14)):
            out = directory / size
            result = subprocess.run(["node", str(ROOT / "tools" / "fx-td-network.mjs"), single_patch("granular"), "--out", str(out), "--picture-size", size], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            spec = json.loads(next(out.glob("*.network.json")).read_text())
            names = {o["name"] for o in spec["operators"] if o["name"].startswith("gr_gain_")}
            banks[size] = (len(names), spec)
            self.assertEqual(len(names), 24 * rows, size)
        data = self.engine(360, 360, [3, 11, 24], [0.07], [0.31, 2.6])
        spec = banks["1080x1080"][1]
        chain = {o["name"]: o for o in spec["operators"] if o["parent"] == "fx_fx_1" and o["name"].startswith("gr_")}
        original = effects.Sized.width, effects.Sized.height
        effects.Sized.width = effects.Sized.height = 1080
        try:
            drawn = 0
            for case in data["out"]:
                p, t = case["p"], case["t"]
                rows = self.rows(p, t)
                cols = round(p["tiles"])
                self.assertEqual(len(case["draws"]), cols * cols, case)   # square: as many rows as columns
                for i, d in enumerate(case["draws"]):
                    tile = chain["gr_tile_%d" % i]["pars"]
                    self.assertAlmostEqual(evaluate(tile["tx"]["expr"], rows, t), d["cx"] / 360 - 0.5, places=9, msg=(i, case))
                    self.assertAlmostEqual(evaluate(tile["ty"]["expr"], rows, t), -(d["cy"] / 360 - 0.5), places=9, msg=(i, case))
                    self.assertEqual(evaluate(chain["gr_gain_%d" % i]["pars"]["opacity"]["expr"], rows, t), 1)
                    drawn += 1
                for i in range(cols * cols, 576):
                    self.assertEqual(evaluate(chain["gr_gain_%d" % i]["pars"]["opacity"]["expr"], rows, t), 0)
        finally:
            effects.Sized.width, effects.Sized.height = original
        self.assertGreater(drawn, 5000)
        self.assertTrue(any("tile bank is 576 stages" in warning for warning in spec["warnings"]))

    def test_the_declared_look_ahead_covers_the_largest_scatter_and_every_bin_read(self):
        from tests.test_td_effects import catalog
        scatter = next(item for item in catalog("granular") if item["key"] == "scatter")["max"]
        self.assertEqual(scatter, 2)
        rows = self.rows({"size": 0.015, "density": 30, "scatter": scatter, "pitch": 7, "spatial": 1, "tiles": 24, "order": 1})
        size = evaluate(self.pars("gr_cache")["cachesize"]["expr"], rows, 0)
        deepest, shallowest = 0.0, 99.0
        for grain in range(4000):
            for k in range(5):
                rows["clock"] = grain * 0.015
                depth = -evaluate(self.pars("gr_bin_%d" % k)["index"]["expr"], rows, grain * 0.015) / 60
                deepest, shallowest = max(deepest, depth), min(shallowest, depth)
                self.assertLess(depth * 60, size)
        # a bin reads from `scatter` ahead of the output (depth 0 at the live edge) to `scatter` behind it (depth 2 x scatter), and the sweep reaches both ends
        self.assertGreaterEqual(deepest, 0.9 * 2 * scatter)
        self.assertLessEqual(shallowest, 0.1 * 2 * scatter)
        self.assertLessEqual(deepest, 2 * scatter + 1 / 60)

    def test_a_scatter_the_latency_cap_cannot_cover_is_limited_and_named(self):
        import tempfile
        script = ("import {buildNetworkSpec} from './tools/td-network/network-spec.mjs'; import {readFileSync} from 'node:fs';"
                  "const patch = JSON.parse(readFileSync(process.argv[1], 'utf8')); const spec = buildNetworkSpec(patch, {maxLatency: 1.2});"
                  "process.stdout.write(JSON.stringify({limits: spec.contract.nodes.find(n => n.id === 'fx-1').resolve.limits, latency: spec.contract.latency.seconds, warnings: spec.warnings}));")
        path = single_patch("granular")
        result = subprocess.run(["node", "--input-type=module", "-e", script, path], capture_output=True, text=True, cwd=ROOT, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["limits"], {"scatter": 1.2})
        self.assertEqual(data["latency"], 1.2)
        self.assertTrue(any("scatter is limited to 1.2 s" in warning for warning in data["warnings"]), data["warnings"])

    def test_the_noise_in_the_module_is_the_workbench_hash_bit_for_bit(self):
        script = ("const noise = (seed, i, j = 0) => { let n = (seed ^ Math.imul(i + 1, 374761393) ^ Math.imul(j + 1, 668265263)) >>> 0; n = Math.imul(n ^ n >>> 13, 1274126177); return ((n ^ n >>> 16) >>> 0) / 4294967296; };"
                  "const out = []; for (const seed of [1, 773, 0, 4294967295, 2147483648]) for (let i = 0; i < 80; i++) for (const j of [0, 3, 5, 9, 21, 44, 99999]) out.push([seed, i, j, noise(seed, i, j)]); process.stdout.write(JSON.stringify(out));")
        result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, cwd=ROOT, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for seed, i, j, expected in json.loads(result.stdout):
            self.assertEqual(self.module.noise(seed, i, j), expected, (seed, i, j))

    def test_the_tiles_are_stacked_first_to_last_over_a_transparent_base(self):
        self.assertEqual(self.chain["gr_over_0"]["inputs"], ["gr_gain_0", "gr_base"])
        for i in range(1, 336):
            self.assertEqual(self.chain["gr_over_%d" % i]["inputs"], ["gr_gain_%d" % i, "gr_over_%d" % (i - 1)])
        proc = next(o for o in self.spec["operators"] if o["name"] == "proc_picture")
        self.assertEqual(proc["inputs"], ["gr_over_335"])
        self.assertEqual(self.chain["gr_base"]["pars"]["alpha"], 0)

    def test_the_spec_says_what_is_not_built(self):
        text = " ".join(self.spec["warnings"])
        self.assertIn("the voice grains are not built", text)
        self.assertIn("tile bank is 336 stages", text)


if __name__ == "__main__":
    unittest.main()
