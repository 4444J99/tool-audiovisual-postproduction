"""Routing's voice in the generated network against the workbench's own audio engine.

`processAudioModule('routing', ...)` pans the mono sum of the shared recording along the travel, equal power, per sample. The network's
Expression CHOP carries that as one expression evaluated once per cook (block rate, the same approximation Freeze's voice documents), so
at the first sample of each block it must equal the engine's output sample for the same parameters and time. The engine is run over a
deterministic stereo signal and every channel of a grid of sample positions is compared. TouchDesigner is not used.
"""
import itertools
import json
import math
import subprocess
import unittest
from pathlib import Path

import tests.test_td_effects as effects
from tests.test_td_effects import evaluate, rows_for, single_patch, spec_for

ROOT = Path(__file__).resolve().parents[1]
RATE, SECONDS = 48000, 4

SCRIPT = """
import {processAudioModule} from './web/fx/audio-engine.mjs';
import {CATALOG_BY_TYPE} from './web/fx/catalog.mjs';
const rate = %d, n = rate * %d;
const left = Float64Array.from({length: n}, (_, i) => ((i * 37) %% 101) / 50 - 1), right = Float64Array.from({length: n}, (_, i) => ((i * 53) %% 89) / 44 - 1);
const params = CATALOG_BY_TYPE.routing.params;
const corners = key => { const p = params.find(item => item.key === key); return [...new Set([p.min, p.default, p.max])]; };
const out = [];
for (const travel of [...corners('travel'), 1.7]) for (const spread of corners('spread')) for (const origin of [0, 0.08, 0.5]) for (const destination of [0.5, 0.92, 1]) {
  const p = {travel, spread, origin, destination, arrival: 1};
  const node = {id: 'rt', params: p, wet: 1, intensity: 1, macro: [], automation: []};
  const result = processAudioModule('routing', [left, right], rate, node).channels;
  const samples = [];
  for (let i = 0; i < n; i += 4093) samples.push([i, result[0][i], result[1][i]]);
  out.push({p, samples});
}
process.stdout.write(JSON.stringify({left: Array.from(left), right: Array.from(right), out}));
""" % (RATE, SECONDS)


class RoutingVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT], capture_output=True, text=True, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.data = json.loads(result.stdout)
        cls.spec = spec_for(single_patch("routing"))
        cls.voice = next(o for o in cls.spec["operators"] if o["name"] == "rt_voice")

    def test_the_voice_is_the_engine_pan_of_the_mono_sum_at_every_compared_sample(self):
        compared, left, right = 0, self.data["left"], self.data["right"]
        for case in self.data["out"]:
            p = case["p"]
            rows = rows_for(None, {key: p[key] for key in ("travel", "spread", "origin", "destination", "arrival")})
            for index, want_left, want_right in case["samples"]:
                for chan, want in ((0, want_left), (1, want_right)):
                    got = evaluate(self.voice["pars"]["expr0expr"]["expr"], rows, index / RATE, sample=0.0, audio=(left, right), index=index, rate=float(RATE), chan=chan)
                    self.assertAlmostEqual(got, want, delta=2e-7, msg=(p, index, chan))   # the engine returns 32-bit samples
                    compared += 1
        self.assertGreater(compared, 10000)

    def test_the_pan_actually_travels_so_the_comparison_is_not_a_constant(self):
        rows = rows_for(None, {"travel": 0.6, "spread": 0.9, "origin": 0.08, "destination": 0.92, "arrival": 1})
        left, right = [1.0] * 10, [1.0] * 10
        sides = {round(evaluate(self.voice["pars"]["expr0expr"]["expr"], rows, t, audio=(left, right), index=0, chan=0) - evaluate(self.voice["pars"]["expr0expr"]["expr"], rows, t, audio=(left, right), index=0, chan=1), 3) for t in (0.0, 0.15, 0.3, 0.45, 0.6, 0.9)}
        self.assertGreater(len(sides), 4)

    def test_equal_power_and_a_mono_source_pans_like_the_same_signal_on_both_channels(self):
        rows = rows_for(None, {"travel": 0.6, "spread": 2, "origin": 0.0, "destination": 1.0, "arrival": 1})
        signal = [0.3, -0.7, 0.1]
        for t in (0.0, 0.2, 0.55, 1.1):
            wet = [evaluate(self.voice["pars"]["expr0expr"]["expr"], rows, t, audio=(signal, signal), index=1, chan=c) for c in (0, 1)]
            self.assertAlmostEqual(wet[0] ** 2 + wet[1] ** 2, 2 * signal[1] ** 2, places=12)

    def test_the_chain_feeds_the_audio_slot_and_the_spec_no_longer_says_the_voice_passes(self):
        proc = next(o for o in self.spec["operators"] if o["name"] == "proc_audio")
        self.assertEqual(proc["inputs"], ["rt_voice"])
        self.assertEqual(self.voice["inputs"], ["in_audio"])
        self.assertFalse(any("routing has no audio effect" in warning for warning in self.spec["warnings"]), self.spec["warnings"])

    def test_every_expression_is_finite_at_the_catalog_minimum_default_and_maximum(self):
        params = effects.catalog("routing")
        for corner, time in itertools.product(("min", "default", "max"), (0.0, 0.4, 3.7, 12.0)):
            rows = rows_for(None, {p["key"]: p[corner] for p in params})
            for chan in (0, 1):
                value = evaluate(self.voice["pars"]["expr0expr"]["expr"], rows, time, audio=([0.5, -0.5], [0.2, 0.9]), index=1, chan=chan)
                self.assertTrue(math.isfinite(value), (corner, time, chan))


if __name__ == "__main__":
    unittest.main()
