"""Typography's picture chain in the generated network against the workbench's own VisualEngine.

The real engine draws a Typography processor onto a recording canvas. Every word it writes (`fillText`) is remembered on the canvas it
was written to, so each later `drawImage` of that canvas carries the word, the alpha and the offset it was drawn with. The network's
expressions, evaluated for the same parameters, time and words (authored, or the supplied cues), must choose the same word for every
copy and place, fade and size them the same way. Glyph pixels are not compared: the Text TOP draws its own. TouchDesigner is not used.
"""
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path

import tests.test_td_effects as effects
from tests.test_td_effects import evaluate, rows_for, single_patch

ROOT = Path(__file__).resolve().parents[1]
CUES = [
    {"in": 0.4, "out": 0.9, "text": "HOW", "probability": 0.9},
    {"in": 1.3, "out": 1.8, "text": "WHY", "probability": 0.3},
    {"in": 2.2, "text": "WHAT", "probability": 0.8},
    {"in": 3.1, "out": 3.6, "text": "OUT"},
    {"in": 6.0, "out": 6.5, "text": "STOP", "probability": 0.51},
]

SCRIPT = """
import {VisualEngine} from './web/fx/visual-engine.mjs';
const W = 640, H = 360;
const log = [];
const make = () => {
  const owner = {width: W, height: H, word: undefined};
  const ctx = {
    globalAlpha: 1, globalCompositeOperation: 'source-over', fillStyle: '', font: '', textAlign: '', textBaseline: '', imageSmoothingEnabled: true,
    setTransform() {}, clearRect() { owner.word = undefined; }, fillRect() {}, save() {}, restore() {}, translate() {}, rotate() {}, scale() {},
    measureText: () => ({width: 100}),
    fillText(value, x, y, max) { owner.word = value; log.push({op: 'text', value, x, y, max, font: this.font, fill: this.fillStyle, alpha: this.globalAlpha}); },
    drawImage(source, ...rest) { log.push({op: 'draw', word: source?.word ?? '', own: source?.id, rest, alpha: this.globalAlpha}); },
  };
  owner.getContext = () => ctx;
  return owner;
};
const cues = __CUES__;
const out = [];
const sizes = [0.05, 1.05, 1.6];
for (const source of ['authored', 'cues']) {
  const engine = new VisualEngine({duration: 12.166666667, fps: 12}, {cues: {words: cues}}, {width: W, height: H, createCanvas: make});
  engine.patch = {seed: 1, nodes: [], connections: [], metadata: source === 'authored' ? {wordMode: 'authored', words: 'HOW WHY WHAT'} : {}};
  engine.nodeMap = new Map();
  for (const mode of [0, 1, 2]) for (const size of mode === 0 || mode === 1 ? sizes : [1.05]) for (const copies of [1, 4, 9, 32]) for (const persistence of [0.1, 3.2, 8])
    for (const fragment of mode === 0 ? [0, 0.3, 0.31, 0.65, 1] : [0.65]) for (const drift of mode === 0 ? [0, 0.55, 1] : [0.55])
      for (const t of [0, 0.2, 0.7, 1.3, 2.1, 3.05, 4.9, 7.7, 11.9]) {
        const p = {size, copies, persistence, fragment, mode, drift};
        log.length = 0; engine.poolIndex = 0;
        const node = {id: 'fx-1', type: 'typography', params: p, targets: ['text', 'picture'], bypass: false, wet: 1};
        engine.input = () => ({image: make(), active: true});
        engine.effect(node, t, 'picture', time => ({time}));
        out.push({source, p, t, log: log.slice()});
      }
}
process.stdout.write(JSON.stringify({W, H, out}));
"""


class FakeTable:
    def __init__(self, rows):
        self.data = rows
        self.numRows = len(rows)

    def __getitem__(self, key):
        return effects.Cell(self.data[key[0]][key[1]])

    def rows(self):
        return [[effects.Cell(value) for value in row] for row in self.data]


def spec_with(words, cues=None):
    path = Path(single_patch("typography"))
    patch = json.loads(path.read_text())
    patch["metadata"] = {**patch.get("metadata", {}), **({"wordMode": "authored", "words": words} if words else {})}
    path.write_text(json.dumps(patch))
    directory = Path(tempfile.mkdtemp(prefix="td-ty-"))
    command = ["node", str(ROOT / "tools" / "fx-td-network.mjs"), str(path), "--out", str(directory)]
    if cues is not None:
        cue_file = directory / "cues.json"
        cue_file.write_text(json.dumps(cues))
        command += ["--word-cues", str(cue_file)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(next(directory.glob("*.network.json")).read_text())


class TypographyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT.replace("__CUES__", json.dumps(CUES))], capture_output=True, text=True, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.data = json.loads(result.stdout)
        cls.specs = {"authored": spec_with("HOW WHY WHAT"), "cues": spec_with(None, CUES)}
        cls.chains = {key: {o["name"]: o for o in spec["operators"] if o["parent"] == "fx_fx_1" and o["name"].startswith("ty_")} for key, spec in cls.specs.items()}
        original = effects.Comp.op

        def op(comp, name):
            if name == "ty_words":
                return FakeTable(cls.table)
            return original(comp, name)
        effects.Comp.op = op
        cls.original_op = original

    @classmethod
    def tearDownClass(cls):
        effects.Comp.op = cls.original_op

    def table_for(self, source):
        chain = self.chains[source]
        self.__class__.table = chain["ty_words"]["table"]

    def value(self, source, name, par, rows, t):
        self.table_for(source)
        return evaluate(self.chains[source][name]["pars"][par]["expr"], rows, t)

    def rows(self, p):
        return rows_for(None, {key: p[key] for key in ("size", "copies", "persistence", "fragment", "mode", "drift")})

    def test_the_authored_words_and_the_supplied_cues_are_carried_as_data_and_nothing_else(self):
        self.assertEqual(self.chains["authored"]["ty_words"]["table"], [["HOW"], ["WHY"], ["WHAT"]])
        self.assertEqual(self.chains["cues"]["ty_words"]["table"][0], ["0.4", "0.9", "HOW", "0.9"])
        self.assertEqual(len(self.chains["cues"]["ty_words"]["table"]), len(CUES))
        bare = spec_with(None)
        self.assertEqual(next(o for o in bare["operators"] if o["name"] == "ty_words")["table"], [])
        self.assertTrue(any("word is OUT" in warning for warning in bare["warnings"]))

    def test_every_word_copy_and_strip_the_engine_draws_is_chosen_faded_and_placed_like_the_network_does(self):
        W, H, compared = self.data["W"], self.data["H"], 0
        for case in self.data["out"]:
            p, t, source, log = case["p"], case["t"], case["source"], case["log"]
            rows, mode = self.rows(p), round(p["mode"])
            self.table_for(source)
            pick = self.value(source, "ty_pick", "index", rows, t)
            self.assertEqual(pick, 1 if mode == 1 else 0, case)
            words = self.value(source, "ty_pick_words", "index", rows, t)
            self.assertEqual(words, 1 if mode == 2 else 0, case)
            if mode == 0:
                draws = [entry for entry in log if entry["op"] == "draw" and len(entry["rest"]) == 2]
                copies = min(32, round(p["copies"]))
                self.assertEqual(len(draws), copies, case)
                text = self.value(source, "ty_text", "text", rows, t)
                for position, draw in enumerate(draws):
                    k = copies - 1 - position
                    self.assertEqual(draw["word"], text, (k, case))
                    self.assertAlmostEqual(self.value(source, "ty_copy_%d" % k, "opacity", rows, t), draw["alpha"], msg=(k, case))
                    self.assertAlmostEqual(self.value(source, "ty_move_%d" % k, "tx", rows, t), draw["rest"][0] / W, places=9, msg=(k, case))
                    # the engine's offset is canvas-down from the word's own place, which sits 0.035 of the height below the centre
                    self.assertAlmostEqual(self.value(source, "ty_move_%d" % k, "ty", rows, t), -0.035 - draw["rest"][1] / H, places=9, msg=(k, case))
                    compared += 1
                for k in range(copies, 32):
                    self.assertEqual(self.value(source, "ty_copy_%d" % k, "opacity", rows, t), 0, (k, case))
                strips = [entry for entry in log if entry["op"] == "draw" and len(entry["rest"]) == 8]
                self.assertEqual(len(strips), 12 if p["fragment"] > 0.3 else 0, case)
                for k in range(12):
                    on = self.value(source, "ty_frag_%d" % k, "opacity", rows, t)
                    self.assertEqual(on, 1 if p["fragment"] > 0.3 else 0, (k, case))
                    if strips:
                        sx, sy, sw, sh, dx, dy, dw, dh = strips[k]["rest"]
                        self.assertAlmostEqual(sy, k * H / 12)
                        self.assertAlmostEqual(self.value(source, "ty_strip_%d" % k, "tx", rows, t), dx / W, places=9, msg=(k, case))
                        crop = self.chains[source]["ty_crop_%d" % k]["pars"]
                        self.assertAlmostEqual(crop["croptop"], 1 - sy / H)
                        self.assertAlmostEqual(crop["cropbottom"], 1 - (sy + sh) / H)
                        self.assertAlmostEqual(self.chains[source]["ty_strip_%d" % k]["pars"]["ty"], 0.5 - (dy + dh / 2) / H)
                        compared += 1
            elif mode == 2:
                texts = [entry for entry in log if entry["op"] == "text"]
                self.assertEqual(len(texts), round(p["copies"]), case)
                for i, entry in enumerate(texts):
                    word = self.value(source, "ty_wall_%d" % i, "text", rows, t)
                    self.assertEqual(entry["value"], word, (i, case))
                    px = float(entry["font"].split("px")[0].split()[-1])
                    self.assertAlmostEqual(self.value(source, "ty_wall_%d" % i, "fontsizex", rows, t) / 1080, px / H, places=9, msg=(i, case))
                    self.assertAlmostEqual(self.value(source, "ty_wmove_%d" % i, "tx", rows, t) + 10 / 1920, (entry["x"] + 10) / W, places=9, msg=(i, case))
                    self.assertAlmostEqual(self.value(source, "ty_wmove_%d" % i, "ty", rows, t), 0.5 - entry["y"] / H, places=9, msg=(i, case))
                    self.assertEqual(self.value(source, "ty_wgain_%d" % i, "opacity", rows, t), 1)
                    compared += 1
                for i in range(round(p["copies"]), 32):
                    self.assertEqual(self.value(source, "ty_wgain_%d" % i, "opacity", rows, t), 0, (i, case))
            else:
                texts = [entry for entry in log if entry["op"] == "text"]
                network = self.value(source, "ty_ap_text", "text", rows, t)
                if not texts:
                    self.assertEqual(network, "", case)   # a word past its persistence: the engine writes nothing, the network's text is blank
                else:
                    self.assertEqual(len(texts), 1, case)
                    self.assertEqual(texts[0]["fill"], "white")
                    self.assertEqual(texts[0]["value"], network, case)
                    px = float(texts[0]["font"].split("px")[0].split()[-1])
                    self.assertAlmostEqual(self.value(source, "ty_ap_text", "fontsizex", rows, t) / 1080 * H, px, delta=0.5, msg=case)
                compared += 1
        self.assertGreater(compared, 20000)

    def test_an_authored_word_field_of_only_whitespace_falls_back_to_how_as_the_workbench_does(self):
        # Codex P2 on this pull request: the empty table made the expression divide by zero
        script = ("import {VisualEngine} from './web/fx/visual-engine.mjs';"
                  "const engine = new VisualEngine({duration: 12, fps: 12}, {}, {width: 64, height: 36, createCanvas: () => ({getContext: () => ({})})});"
                  "const patch = {metadata: {wordMode: 'authored', words: '   '}};"
                  "process.stdout.write(JSON.stringify([0, 0.3, 0.7, 1.5, 2.8].map(t => [t, engine.word(t, patch, 8)])));")
        engine = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, cwd=ROOT, check=False)
        self.assertEqual(engine.returncode, 0, engine.stderr)
        spec = spec_with("   ")
        chain = {o["name"]: o for o in spec["operators"] if o["parent"] == "fx_fx_1" and o["name"].startswith("ty_")}
        self.assertEqual(chain["ty_words"]["table"], [["HOW"]])
        self.__class__.table = chain["ty_words"]["table"]
        rows = rows_for(None, {"size": 1.05, "copies": 9, "persistence": 8, "fragment": 0.65, "mode": 0, "drift": 0.55})
        for t, word in json.loads(engine.stdout):
            self.assertEqual(word, "HOW")
            self.assertEqual(evaluate(chain["ty_text"]["pars"]["text"]["expr"], rows, t), word)

    def test_the_wall_copies_read_older_words_and_the_word_follows_both_clocks(self):
        # the wall's copy i asks for the word 0.32 i seconds earlier: the network's text expression reads its own clock minus that
        self.table_for("authored")
        rows = rows_for(None, {"size": 1.05, "copies": 9, "persistence": 8, "fragment": 0.65, "mode": 2, "drift": 0.55})
        seen = {evaluate(self.chains["authored"]["ty_wall_%d" % i]["pars"]["text"]["expr"], rows, 1.0) for i in range(9)}
        self.assertEqual(seen, {"HOW", "WHY"})
        self.table_for("cues")
        self.assertEqual(evaluate(self.chains["cues"]["ty_text"]["pars"]["text"]["expr"], rows, 0.1), "OUT")   # before any cue
        self.assertEqual(evaluate(self.chains["cues"]["ty_text"]["pars"]["text"]["expr"], rows, 0.5), "HOW")
        self.assertEqual(evaluate(self.chains["cues"]["ty_text"]["pars"]["text"]["expr"], rows, 1.4), "HOW")   # WHY is improbable (0.3) and ignored

    def test_the_layers_are_stacked_with_copy_zero_on_top_and_the_modes_switch_between_them(self):
        chain = self.chains["authored"]
        self.assertEqual(chain["ty_over_31"]["inputs"], ["ty_copy_31", "ty_s_base"])
        self.assertEqual(chain["ty_over_0"]["inputs"], ["ty_copy_0", "ty_over_1"])
        self.assertEqual(chain["ty_fover_0"]["inputs"], ["ty_frag_0", "ty_over_0"])
        self.assertEqual(chain["ty_pick_words"]["inputs"], ["ty_fover_11", "ty_wover_0"])
        self.assertEqual(chain["ty_on_picture"]["inputs"], ["ty_pick_words", "in_picture"])
        self.assertEqual(chain["ty_pick"]["inputs"], ["ty_on_picture", "ty_ap_over"])
        proc = next(o for o in self.specs["authored"]["operators"] if o["name"] == "proc_picture")
        self.assertEqual(proc["inputs"], ["ty_pick"])

    def test_every_numeric_expression_is_finite_at_the_catalog_minimum_default_and_maximum(self):
        params, evaluated = effects.catalog("typography"), 0
        for source in ("authored", "cues"):
            self.table_for(source)
            for corner in ("min", "default", "max"):
                rows = rows_for(None, {p["key"]: p[corner] for p in params})
                for operator in self.chains[source].values():
                    for name, value in operator["pars"].items():
                        if isinstance(value, dict) and "resolved" in value and name != "text":
                            for time in (0.0, 0.4, 3.7, 12.0):
                                self.assertTrue(math.isfinite(float(evaluate(value["expr"], rows, time))), (source, corner, operator["name"], name, time))
                                evaluated += 1
        self.assertGreater(evaluated, 3000)

    def test_the_spec_says_what_is_not_built(self):
        text = " ".join(self.specs["authored"]["warnings"])
        self.assertIn("text stream (captions port) is not built", text)
        self.assertIn("does not squeeze", text)


if __name__ == "__main__":
    unittest.main()
