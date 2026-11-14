import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from amp.studies import colour_grade as cg, layer_plan as lp

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "colour_layers.py"


def chain(*ops):
    return {"schema": cg.SCHEMA, "ops": list(ops)}


IDENTITY_CDL = {"op": "cdl", "slope": [1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "saturation": 1.0}


class TransferTests(unittest.TestCase):
    def test_srgb_round_trip_is_exact_for_every_code_value(self):
        ramp = np.arange(256, dtype=np.uint8)[None, :, None].repeat(3, axis=2)
        self.assertTrue(np.array_equal(cg.encode_frame(cg.decode_frame(ramp)), ramp))

    def test_known_points(self):
        self.assertAlmostEqual(float(cg.srgb_to_linear(np.array(0.5))), 0.214041, places=5)
        self.assertAlmostEqual(float(cg.linear_to_srgb(np.array(0.18))), 0.461356, places=5)


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.ramp = cg.synthetic_ramp()
        self.chart = cg.synthetic_chart()

    def test_identity_chain_is_identity_on_ramp_and_chart(self):
        for frame in (self.ramp, self.chart):
            self.assertTrue(np.array_equal(cg.apply_display(frame, chain(IDENTITY_CDL)), frame))

    def test_exposure_one_stop_doubles_linear_light(self):
        lin = cg.srgb_to_linear(np.full((2, 2, 3), 0.2))
        out = cg.apply_linear(lin, chain({"op": "exposure", "stops": 1}))
        np.testing.assert_allclose(out, lin * 2.0)

    def test_cdl_matches_the_asc_formula(self):
        rgb = np.array([[[0.2, 0.4, 0.6]]])
        op = {"op": "cdl", "slope": [1.2, 1.0, 0.5], "offset": [0.05, 0.0, -0.1], "power": [0.8, 1.0, 2.0], "saturation": 0.5}
        pre = np.clip(rgb * [1.2, 1.0, 0.5] + [0.05, 0.0, -0.1], 0, None) ** [0.8, 1.0, 2.0]
        y = pre @ cg.LUMA
        expected = y[..., None] + 0.5 * (pre - y[..., None])
        np.testing.assert_allclose(cg.apply_linear(rgb, chain(op)), expected)

    def test_zero_saturation_gives_equal_channels(self):
        out = cg.apply_linear(cg.decode_frame(self.chart), chain({**IDENTITY_CDL, "saturation": 0.0}))
        np.testing.assert_allclose(out[..., 0], out[..., 1], atol=1e-12)
        np.testing.assert_allclose(out[..., 1], out[..., 2], atol=1e-12)

    def test_neutral_chain_keeps_grey_patches_grey(self):
        neutral = chain({"op": "exposure", "stops": 0.5},
                        {"op": "curve", "channel": "luma", "points": [[0, 0], [0.2, 0.15], [1, 1]]},
                        {**IDENTITY_CDL, "slope": [1.1, 1.1, 1.1], "saturation": 1.2})
        out = cg.apply_display(self.chart, neutral)
        greys = out[32:48]  # third patch row holds the six greys
        self.assertTrue(np.all(greys[..., 0] == greys[..., 1]) and np.all(greys[..., 1] == greys[..., 2]))

    def test_luma_curve_is_monotone_on_the_ramp_and_preserves_chroma_ratio(self):
        c = chain({"op": "curve", "channel": "luma", "points": [[0, 0], [0.18, 0.1], [1, 1]]})
        out = cg.apply_display(self.ramp, c)[0, :, 0].astype(int)
        self.assertTrue(np.all(np.diff(out) >= 0))
        rgb = np.array([[[0.4, 0.2, 0.1]]])
        graded = cg.apply_linear(rgb, c)
        np.testing.assert_allclose(graded[..., 0] / graded[..., 1], 2.0)

    def test_mono_weights_produce_luma_and_tint(self):
        rgb = cg.decode_frame(self.chart)
        out = cg.apply_linear(rgb, chain({"op": "mono", "weights": [0.2126, 0.7152, 0.0722], "tint": [1.0, 0.8, 0.5]}))
        np.testing.assert_allclose(out[..., 1] / np.maximum(out[..., 0], 1e-12) * (out[..., 0] > 1e-9), 0.8 * (out[..., 0] > 1e-9), atol=1e-9)
        np.testing.assert_allclose(out[..., 0], cg.luma(rgb))

    def test_region_tint_changes_only_its_band(self):
        flat = np.full((4, 10, 3), 0.5)
        op = {"op": "region_tint", "bands": [{"x0": 0.2, "x1": 0.5, "tint": [1.0, 0.5, 0.5], "strength": 1.0}]}
        out = cg.apply_linear(flat, chain(op))
        np.testing.assert_allclose(out[:, :2], 0.5)
        np.testing.assert_allclose(out[:, 2:5, 1], 0.25)
        np.testing.assert_allclose(out[:, 5:], 0.5)

    def test_validation_reports_every_problem(self):
        bad = chain({"op": "exposure", "stops": 99},
                    {"op": "curve", "points": [[0, 0], [0.5, 0.9], [0.4, 1.0]]},
                    {"op": "curve", "points": [[0, 1], [1, 0]]},
                    {"op": "mono", "weights": [1, 1, 1]},
                    {"op": "cdl", "slope": [1, 1], "offset": [0, 0, 0], "power": [0, 1, 1], "saturation": -1},
                    {"op": "region_tint", "bands": [{"x0": 0.6, "x1": 0.9, "tint": [1, 1, 1], "strength": 1},
                                                     {"x0": 0.5, "x1": 0.7, "tint": [1, 1, 1], "strength": 2}]},
                    {"op": "blur"})
        errors = cg.validate_chain(bad)
        for needle in ("stops", "strictly increase", "monotone", "sum to 1", "slope must be three", "power must be positive",
                       "saturation", "non-overlapping", "strength must be within", "op must be one of"):
            self.assertTrue(any(needle in e for e in errors), needle)
        with self.assertRaises(ValueError):
            cg.apply_linear(np.zeros((1, 1, 3)), bad)

    def test_chain_hash_ignores_key_order_and_detects_change(self):
        a = chain({"op": "exposure", "stops": 1})
        b = {"ops": [{"stops": 1, "op": "exposure"}], "schema": cg.SCHEMA}
        self.assertEqual(cg.chain_hash(a), cg.chain_hash(b))
        self.assertNotEqual(cg.chain_hash(a), cg.chain_hash(chain({"op": "exposure", "stops": 2})))


class LutTests(unittest.TestCase):
    def test_cube_round_trip_and_identity_lut_is_identity(self):
        lut = cg.identity_lut(5)
        again = cg.parse_cube(cg.dumps_cube(lut))
        np.testing.assert_allclose(again, lut, atol=1e-6)
        rng = np.random.default_rng(7)
        rgb = rng.random((8, 8, 3))
        np.testing.assert_allclose(cg.apply_lut(rgb, lut), rgb, atol=1e-12)

    def test_red_varies_fastest_in_cube_file_order(self):
        lines = cg.dumps_cube(cg.identity_lut(2)).splitlines()[4:]
        self.assertEqual(lines[1].split(), ["1.000000", "0.000000", "0.000000"])
        self.assertEqual(lines[2].split(), ["0.000000", "1.000000", "0.000000"])
        self.assertEqual(lines[4].split(), ["0.000000", "0.000000", "1.000000"])

    def test_swap_lut_applies_by_trilinear_interpolation(self):
        lut = cg.identity_lut(9)[..., ::-1].copy()  # exchange red and blue
        rgb = np.array([[[0.13, 0.5, 0.91]]])
        np.testing.assert_allclose(cg.apply_lut(rgb, lut), rgb[..., ::-1], atol=1e-12)

    def test_lut_op_in_chain_and_rejections(self):
        text = cg.dumps_cube(cg.identity_lut(17))
        frame = cg.synthetic_chart()
        out = cg.apply_display(frame, chain({"op": "lut", "cube": text}))
        self.assertLessEqual(int(np.abs(out.astype(int) - frame.astype(int)).max()), 1)
        for bad in ("LUT_3D_SIZE 2\n0 0 0\n", "LUT_1D_SIZE 4\n", "LUT_3D_SIZE 2\nDOMAIN_MAX 2 2 2\n" + "0 0 0\n" * 8, "junk"):
            with self.assertRaises(ValueError):
                cg.parse_cube(bad)
        errors = cg.validate_chain(chain({"op": "lut"}, {"op": "lut", "cube": text, "identity": 3}, {"op": "lut", "identity": 1}))
        self.assertEqual(len(errors), 3)


class MeasureTests(unittest.TestCase):
    def test_report_flags_clipping_introduced_by_a_grade(self):
        frame = cg.synthetic_ramp()
        report = cg.grade_report(frame, chain({"op": "exposure", "stops": 2}))
        self.assertGreater(report["after"]["clipped_high"], report["before"]["clipped_high"])
        self.assertGreater(report["after"]["mean_luma_linear"], report["before"]["mean_luma_linear"])


# ---------------------------------------------------------------------------------------- layers

def solid_loader(colours, alphas=None):
    def load(source, frame):
        colour = colours[source]
        h, w = 2, 2
        rgba = np.ones((h, w, 4))
        rgba[..., :3] = colour
        if alphas and source in alphas:
            rgba[..., 3] = alphas[source]
        return rgba
    return load


def small_plan(**changes):
    plan = copy.deepcopy(lp.synthetic_plan())
    plan["comp"].update(width=2, height=2, frames=10)
    plan["layers"] = [
        {"id": "base", "kind": "plate", "source": "room", "frames": {"start": 0, "end": 10}},
        {"id": "top", "kind": "fx", "source": "glow", "frames": {"start": 2, "end": 8}},
    ]
    plan["sources"]["room"]["colourspace"] = "linear"
    plan.update(changes)
    return plan


class PlanValidationTests(unittest.TestCase):
    def test_synthetic_plan_is_valid_with_one_conversion_warning(self):
        report = lp.validate_plan(lp.synthetic_plan())
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], ["source room: srgb source is converted to linear"])

    def test_each_rule_fails_with_its_own_message(self):
        cases = {}

        def case(name, mutate, needle):
            plan = copy.deepcopy(lp.synthetic_plan())
            mutate(plan)
            cases[name] = (plan, needle)

        L = lambda p, i: p["layers"][i]
        case("dup", lambda p: L(p, 3).update(id="base"), "duplicate id")
        case("blend", lambda p: L(p, 2).update(blend="dodge"), "blend must be")
        case("range", lambda p: L(p, 0).update(frames={"start": 0, "end": 99}), "frames need integers")
        case("opacity", lambda p: L(p, 2).update(opacity=2), "opacity must be 0..1")
        case("keyframes", lambda p: L(p, 2).update(opacity=[[10, 0.5], [5, 1.0]]), "keyframes must be")
        case("source", lambda p: L(p, 0).update(source="nope"), "is not defined")
        case("audio", lambda p: (p["sources"].update(snd={"kind": "audio", "path": "a.wav", "sha256": "0" * 64}), L(p, 0).update(source="snd")), "audio source")
        case("sha", lambda p: p["sources"]["room"].update(sha256="abc"), "content-addressed")
        case("abs", lambda p: p["sources"]["room"].update(path="/Users/x/film.mov"), "path must be relative")
        case("dotdot", lambda p: p["sources"]["room"].update(path="../private/film.mov"), "path must be relative")
        case("url", lambda p: p["sources"]["room"].update(path="https://x.test/a.mov"), "path must be relative")
        case("space", lambda p: p["comp"].update(working_space="srgb"), "working_space must be linear")
        case("chain", lambda p: L(p, 2).update(chain="nope"), "chain 'nope' is not defined")
        case("badchain", lambda p: p["chains"]["warm"]["ops"][0].update(saturation=-1), "chain warm")
        case("matte-missing", lambda p: L(p, 2)["matte"].update(layer="ghost"), "does not exist")
        case("matte-self", lambda p: L(p, 2)["matte"].update(layer="fire"), "cannot matte itself")
        case("matte-kind", lambda p: L(p, 2)["matte"].update(layer="base"), "must be of kind matte")
        case("matte-mode", lambda p: L(p, 2)["matte"].update(mode="chroma"), "matte mode")
        case("matte-above", lambda p: p["layers"].insert(2, p["layers"].pop(1)), "must lie below")
        case("matte-cover", lambda p: L(p, 1).update(frames={"start": 10, "end": 48}), "does not cover frames")
        case("adj-src", lambda p: L(p, 3).update(source="room"), "no source")
        case("adj-chain", lambda p: L(p, 3).pop("chain"), "needs a chain")
        case("gap", lambda p: L(p, 0).update(frames={"start": 4, "end": 48}), "no opaque base layer")
        for name, (plan, needle) in cases.items():
            with self.subTest(name):
                errors = lp.validate_plan(plan)["errors"]
                self.assertTrue(any(needle in e for e in errors), (needle, errors))

    def test_matte_of_a_matte_and_unused_matte(self):
        plan = copy.deepcopy(lp.synthetic_plan())
        plan["layers"][1]["matte"] = {"layer": "vig"}
        self.assertTrue(any("cannot itself be matted" in e for e in lp.validate_plan(plan)["errors"]))
        plan = copy.deepcopy(lp.synthetic_plan())
        del plan["layers"][2]["matte"]
        self.assertTrue(any("not used by any layer" in w for w in lp.validate_plan(plan)["warnings"]))

    def test_allow_gaps_permits_a_hole_and_the_ledger_is_stable(self):
        plan = copy.deepcopy(lp.synthetic_plan())
        plan["layers"][0]["frames"] = {"start": 4, "end": 48}
        plan["allow_gaps"] = True
        self.assertEqual(lp.validate_plan(plan)["errors"], [])
        a, b = lp.layer_ledger(plan), lp.layer_ledger(json.loads(json.dumps(plan)))
        self.assertEqual(a, b)
        self.assertEqual(a["layers"][2]["chain_sha256"], cg.chain_hash(plan["chains"]["warm"]))
        self.assertEqual(a["layers"][2]["matte"], "vig")

    def test_active_layers_excludes_mattes_and_respects_ranges(self):
        plan = lp.synthetic_plan()
        self.assertEqual(lp.active_layers(plan, 0), ["base", "look"])
        self.assertEqual(lp.active_layers(plan, 10), ["base", "fire", "look"])
        self.assertEqual(lp.active_layers(plan, 42), ["base", "look"])


class FlattenTests(unittest.TestCase):
    def run_blend(self, mode, base, top, opacity=1.0):
        plan = small_plan()
        plan["layers"][1].update(blend=mode, opacity=opacity)
        plan["sources"]["glow"]["colourspace"] = "linear"
        load = solid_loader({"room": [base] * 3, "glow": [top] * 3})
        return lp.flatten(plan, 4, load)

    def test_blend_arithmetic(self):
        self.assertAlmostEqual(self.run_blend("normal", 0.2, 0.6)[0, 0, 0], 0.6)
        self.assertAlmostEqual(self.run_blend("multiply", 0.5, 0.4)[0, 0, 0], 0.2)
        self.assertAlmostEqual(self.run_blend("screen", 0.5, 0.4)[0, 0, 0], 0.7)
        self.assertAlmostEqual(self.run_blend("add", 0.5, 0.4)[0, 0, 0], 0.9)
        self.assertAlmostEqual(self.run_blend("overlay", 0.25, 0.6)[0, 0, 0], 0.3)
        self.assertAlmostEqual(self.run_blend("overlay", 0.75, 0.6)[0, 0, 0], 0.8)

    def test_opacity_and_layer_alpha_scale_the_blend(self):
        self.assertAlmostEqual(self.run_blend("normal", 0.2, 0.6, 0.25)[0, 0, 0], 0.3)
        plan = small_plan()
        plan["sources"]["glow"]["colourspace"] = "linear"
        load = solid_loader({"room": [0.2] * 3, "glow": [0.6] * 3}, {"glow": 0.5})
        self.assertAlmostEqual(lp.flatten(plan, 4, load)[0, 0, 0], 0.4)

    def test_layer_outside_its_range_is_not_drawn(self):
        plan = small_plan()
        plan["sources"]["glow"]["colourspace"] = "linear"
        load = solid_loader({"room": [0.2] * 3, "glow": [0.6] * 3})
        self.assertAlmostEqual(lp.flatten(plan, 1, load)[0, 0, 0], 0.2)
        self.assertAlmostEqual(lp.flatten(plan, 8, load)[0, 0, 0], 0.2)

    def test_srgb_sources_are_decoded_before_blending(self):
        plan = small_plan()
        plan["sources"]["room"]["colourspace"] = "srgb"
        load = solid_loader({"room": [0.5] * 3, "glow": [0.0] * 3}, {"glow": 0.0})
        self.assertAlmostEqual(lp.flatten(plan, 4, load)[0, 0, 0], 0.214041, places=5)

    def test_luma_and_inverted_luma_mattes_gate_the_layer(self):
        plan = small_plan()
        plan["sources"]["glow"]["colourspace"] = "linear"
        plan["sources"]["vignette"]["colourspace"] = "linear"
        plan["layers"] = [
            {"id": "base", "kind": "plate", "source": "room", "frames": {"start": 0, "end": 10}},
            {"id": "m", "kind": "matte", "source": "vignette", "frames": {"start": 0, "end": 10}},
            {"id": "top", "kind": "fx", "source": "glow", "frames": {"start": 0, "end": 10}, "matte": {"layer": "m", "mode": "luma"}},
        ]
        load = solid_loader({"room": [0.2] * 3, "glow": [1.0] * 3, "vignette": [0.25] * 3})
        self.assertAlmostEqual(lp.flatten(plan, 4, load)[0, 0, 0], 0.2 + 0.25 * 0.8)
        plan["layers"][2]["matte"]["mode"] = "invert_luma"
        self.assertAlmostEqual(lp.flatten(plan, 4, load)[0, 0, 0], 0.2 + 0.75 * 0.8)

    def test_layer_chain_grades_before_blending_and_adjustment_grades_everything_below(self):
        plan = small_plan()
        plan["sources"]["glow"]["colourspace"] = "linear"
        plan["chains"] = {"double": chain({"op": "exposure", "stops": 1}), "half": chain({"op": "exposure", "stops": -1})}
        plan["layers"][1]["chain"] = "double"
        plan["layers"].append({"id": "adj", "kind": "adjustment", "frames": {"start": 0, "end": 10}, "chain": "half", "opacity": 0.5})
        load = solid_loader({"room": [0.2] * 3, "glow": [0.3] * 3})
        below = 0.6  # glow doubled, normal blend, replaces the plate
        self.assertAlmostEqual(lp.flatten(plan, 4, load)[0, 0, 0], below + 0.5 * (below * 0.5 - below))

    def test_invalid_plan_is_refused_by_flatten(self):
        plan = small_plan()
        plan["layers"][1]["blend"] = "bogus"
        with self.assertRaises(ValueError):
            lp.flatten(plan, 4, solid_loader({"room": [0] * 3, "glow": [0] * 3}))


class CliTests(unittest.TestCase):
    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)

    def test_demo_is_deterministic_and_writes_expected_files(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = self.run_tool("demo", a), self.run_tool("demo", b)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
            for name in ("chart-before.png", "chart-after.png", "comp-000.png", "comp-024.png", "grade-report.json", "layer-ledger.json"):
                self.assertTrue((Path(a) / name).is_file(), name)
            with Image.open(Path(a) / "comp-024.png") as image:
                self.assertEqual(image.size, (384, 216))
            self.assertNotEqual((Path(a) / "comp-000.png").read_bytes(), (Path(a) / "comp-024.png").read_bytes())

    def test_grade_command_applies_a_chain_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            Image.fromarray(cg.synthetic_chart()).save(d / "in.png")
            (d / "c.json").write_text(json.dumps(chain({"op": "exposure", "stops": 1})))
            result = self.run_tool("grade", "--chain", str(d / "c.json"), "--input", str(d / "in.png"), "--output", str(d / "out.png"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreater(json.loads(result.stdout)["after"]["mean_luma_linear"], json.loads(result.stdout)["before"]["mean_luma_linear"])
            self.assertTrue((d / "out.png").is_file())

    def test_invalid_plan_exits_2_and_writes_no_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            plan = lp.synthetic_plan()
            plan["layers"][2]["blend"] = "bogus"
            (d / "p.json").write_text(json.dumps(plan))
            result = self.run_tool("validate-plan", "--plan", str(d / "p.json"), "--ledger", str(d / "l.json"))
            self.assertEqual(result.returncode, 2)
            self.assertIn("blend must be", result.stdout)
            self.assertFalse((d / "l.json").exists())

    def test_identity_lut_command_writes_parseable_cube(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.run_tool("identity-lut", "5", "--output", str(Path(d) / "i.cube"))
            self.assertEqual(result.returncode, 0, result.stderr)
            np.testing.assert_allclose(cg.parse_cube((Path(d) / "i.cube").read_text()), cg.identity_lut(5), atol=1e-6)
        self.assertEqual(self.run_tool("identity-lut", "1", "--output", "/dev/null").returncode, 2)


if __name__ == "__main__":
    unittest.main()
