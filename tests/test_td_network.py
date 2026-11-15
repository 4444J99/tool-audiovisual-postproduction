"""The generated TouchDesigner network: builder against a fake operator tree, and the OSC module's behaviour.

TouchDesigner itself is not needed or used. The fake enforces the spec's own vocabulary: an operator only
has the parameters the vocabulary lists for its type (plus its custom parameters), so a builder that sets
anything else fails here the way it would fail in TouchDesigner.
"""
import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIR = ROOT / "tools" / "td-network" / "python"
FIXTURE = ROOT / "web" / "fx" / "fixtures" / "closed-inbox.json"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_lib = load("fx_td_build", PYTHON_DIR / "fx_td_build.py")
osc_lib = load("fx_td_osc", PYTHON_DIR / "fx_td_osc.py")


class FakePar:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name
        self.val, self.expr, self.default = None, None, None
        self.min = self.max = self.normMin = self.normMax = None
        self.clampMin = self.clampMax = self.readOnly = False

    def eval(self):
        return self.val


class FakePage:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    def _append(self, style):
        def make(name, label=None):
            par = FakePar(self.owner, name)
            par.style, par.label, par.page = style, label, self.name
            self.owner.custom[name] = par
            return (par,)   # like TouchDesigner's ParGroup: indexable, first member is the parameter
        return make

    def __getattr__(self, attribute):
        if attribute.startswith("append"):
            return self._append(attribute[len("append"):])
        raise AttributeError(attribute)


class FakeConnector:
    def __init__(self):
        self.source = None

    def connect(self, other):
        self.source = other


class ParNamespace:
    def __init__(self, owner):
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        owner = self._owner
        if name in owner.custom:
            return owner.custom[name]
        if name in owner.vocabulary_pars:
            return owner.builtin.setdefault(name, FakePar(owner, name))
        raise AttributeError(name)


class FakeOp:
    VOCABULARY = {}

    def __init__(self, parent, type_name, name):
        self.parent_op, self.type, self.name = parent, type_name, name
        self.path = (parent.path if parent else "") + "/" + name if parent else "/" + name
        self.children, self.custom, self.builtin = {}, {}, {}
        self.vocabulary_pars = set(self.VOCABULARY.get(type_name, {}).get("pars", []))
        self.par = ParNamespace(self)
        self.inputConnectors = [FakeConnector() for _ in range(8)]
        self.text, self.rows, self.saved, self.destroyed = "", [], None, False

    def create(self, type_name, name):
        if name in self.children:
            raise RuntimeError("name already used: " + name)
        if type_name not in self.VOCABULARY:
            raise RuntimeError("no such operator type " + type_name)
        child = FakeOp(self, type_name, name)
        self.children[name] = child
        return child

    def op(self, name):
        return self.children.get(name)

    def destroy(self):
        self.destroyed = True
        del self.parent_op.children[self.name]

    def appendCustomPage(self, name):
        return FakePage(self, name)

    def clear(self):
        self.rows = []

    def appendRow(self, row):
        self.rows.append(row)

    def save(self, path):
        self.saved = path


def generate(patch_path=FIXTURE, *extra):
    directory = Path(tempfile.mkdtemp(prefix="td-net-"))
    result = subprocess.run(["node", str(ROOT / "tools" / "fx-td-network.mjs"), str(patch_path), "--out", str(directory), *extra],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return directory


class BuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = generate()
        cls.spec = json.loads((cls.directory / "closed-inbox.network.json").read_text())
        FakeOp.VOCABULARY = cls.spec["vocabulary"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def build(self, spec=None, **kwargs):
        project = FakeOp(None, "baseCOMP", "project1")
        rack = build_lib.build(project, spec or self.spec, **kwargs)
        return project, rack

    def test_every_operator_is_created_with_its_type_parameters_and_text(self):
        _, rack = self.build()
        for operator in self.spec["operators"]:
            node = rack
            for part in operator["path"].split("/"):
                node = node.children[part]
            self.assertEqual(node.type, operator["type"], operator["path"])
            for name, value in operator["pars"].items():
                par = node.builtin[name]
                if isinstance(value, dict) and "expr" in value:
                    self.assertEqual(par.expr, value["expr"])
                elif isinstance(value, dict):
                    self.assertEqual(par.val, rack.path + "/" + value["ref"])
                else:
                    self.assertEqual(par.val, value)
            if "text" in operator:
                self.assertEqual(node.text, operator["text"], operator["path"])
        self.assertEqual(len(rack.children), sum(1 for o in self.spec["operators"] if o["parent"] == ""))

    def test_wiring_matches_the_spec_input_for_input(self):
        _, rack = self.build()
        wired = 0
        for operator in self.spec["operators"]:
            parent = rack
            for part in operator["parent"].split("/") if operator["parent"] else []:
                parent = parent.children[part]
            node = parent.children[operator["name"]]
            for index, name in enumerate(operator["inputs"]):
                self.assertIs(node.inputConnectors[index].source, parent.children[name], operator["path"])
                wired += 1
            self.assertIsNone(node.inputConnectors[len(operator["inputs"])].source)
        self.assertGreater(wired, 100)

    def test_custom_parameters_carry_the_catalog_ranges(self):
        _, rack = self.build()
        for node in self.spec["contract"]["nodes"]:
            comp = rack.children[node["comp"]]
            for control in node["controls"]:
                par = comp.custom[control["par"]]
                if control["kind"] == "toggle":
                    self.assertEqual(par.style, "Toggle")
                    self.assertEqual(par.val, 1 if control["value"] else 0)
                else:
                    self.assertEqual((par.min, par.max, par.default, par.val), (control["min"], control["max"], control["default"], control["value"]))
                    self.assertTrue(par.clampMin and par.clampMax)
            self.assertEqual(comp.custom["Nodeid"].val, node["id"])
            self.assertTrue(comp.custom["Nodeid"].readOnly)
        self.assertEqual(set(rack.custom), {item["name"] for item in self.spec["root"]["customPars"]})

    def test_a_second_build_replaces_the_first(self):
        project, first = self.build()
        rack = build_lib.build(project, self.spec, save_to="/tmp/rack.tox")
        self.assertTrue(first.destroyed)
        self.assertIs(project.children["fx_rack"], rack)
        self.assertEqual(rack.saved, "/tmp/rack.tox")

    def test_specs_the_builder_cannot_honour_fail_before_anything_is_created(self):
        cases = {
            "unknown type": lambda s: s["operators"][2].update(type="noiseTOP"),
            "parameter outside the vocabulary": lambda s: next(o for o in s["operators"] if o["type"] == "selectTOP")["pars"].update(bogus=1),
            "child before parent": lambda s: s["operators"].insert(0, next(o for o in s["operators"] if o["parent"])),
            "input not a sibling": lambda s: next(o for o in s["operators"] if o["inputs"]).update(inputs=["nowhere"]),
            "dangling reference": lambda s: next(o for o in s["operators"] if o["type"] == "selectTOP")["pars"].update(top={"ref": "ghost/out"}),
            "wrong schema": lambda s: s.update(schema="amps.td-network.v0"),
        }
        for label, mutate in cases.items():
            broken = copy.deepcopy(self.spec)
            mutate(broken)
            project = FakeOp(None, "baseCOMP", "project1")
            with self.assertRaises(build_lib.BuildError, msg=label):
                build_lib.build(project, broken)
            self.assertEqual(project.children, {}, label)

    def test_a_parameter_missing_in_this_touchdesigner_build_is_named(self):
        FakeOp.VOCABULARY = copy.deepcopy(self.spec["vocabulary"])
        spec = copy.deepcopy(self.spec)
        try:
            FakeOp.VOCABULARY["crossTOP"]["pars"] = []   # the fake build has no 'cross' parameter
            with self.assertRaises(build_lib.BuildError) as raised:
                self.build(spec)
            self.assertIn("cross", str(raised.exception))
        finally:
            FakeOp.VOCABULARY = self.spec["vocabulary"]

    def test_the_generated_script_defines_the_network_without_creating_it(self):
        namespace = {}
        exec(compile((self.directory / "closed-inbox.build.py").read_text(), "closed-inbox.build.py", "exec"), namespace)
        self.assertEqual(namespace["SPEC"], self.spec)
        self.assertIsNone(namespace["_td"])
        self.assertNotIn("RACK", namespace)
        rack = namespace["build"](FakeOp(None, "baseCOMP", "project1"), namespace["SPEC"])
        self.assertEqual(rack.name, "fx_rack")

    def test_the_module_texts_inside_the_network_are_the_files_that_are_tested(self):
        _, rack = self.build()
        self.assertEqual(rack.children["td_osc"].text, (PYTHON_DIR / "fx_td_osc.py").read_text())
        self.assertEqual(rack.children["osc_in_callbacks"].text, (PYTHON_DIR / "fx_td_osc_in_callbacks.py").read_text())
        self.assertEqual(json.loads(rack.children["td_contract"].text), self.spec["contract"])
        self.assertEqual(rack.children["osc_in"].builtin["port"].val, 7000)
        self.assertEqual(rack.children["osc_out"].builtin["port"].val, 7001)
        self.assertEqual(rack.children["osc_in"].builtin["callbacks"].val, rack.path + "/osc_in_callbacks")


class RecordingBackend:
    def __init__(self):
        self.params, self.sent = {}, []

    def get(self, comp, par):
        return self.params.get((comp, par))

    def set(self, comp, par, value):
        self.params[(comp, par)] = value

    def send(self, address, args):
        self.sent.append((address, list(args)))


class OscModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = generate()
        cls.contract = json.loads((cls.directory / "closed-inbox.network.json").read_text())["contract"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def link(self):
        backend = RecordingBackend()
        return osc_lib.RackLink(self.contract, backend), backend

    def first_range(self):
        node = self.contract["nodes"][0]
        return node, next(c for c in node["controls"] if c["kind"] == "range" and c["path"].startswith("params."))

    def test_range_reports_are_scaled_onto_the_parameter_range_and_step(self):
        link, backend = self.link()
        node, control = self.first_range()
        self.assertTrue(link.receive(control["address"], [1.0]))
        self.assertEqual(backend.params[(node["comp"], control["par"])], control["max"])
        self.assertTrue(link.receive(control["address"], [0.0]))
        self.assertEqual(backend.params[(node["comp"], control["par"])], control["min"])
        link.receive(control["address"], [5.0])
        self.assertEqual(backend.params[(node["comp"], control["par"])], control["max"], "out of range is clamped")

    def test_integer_parameters_receive_integers(self):
        link, backend = self.link()
        node = self.contract["nodes"][0]
        control = next(c for c in node["controls"] if c.get("integer"))
        link.receive(control["address"], [0.5])
        value = backend.params[(node["comp"], control["par"])]
        self.assertIsInstance(value, int)

    def test_unknown_addresses_and_ill_typed_arguments_are_ignored(self):
        link, backend = self.link()
        node, control = self.first_range()
        for address, args in [("/fx/bogus", [1]), ("/fx/node/ghost/intensity", [0.5]), (control["address"], ["loud"]), (control["address"], []),
                              (control["address"], [None]), (control["address"], [float("nan")]), (control["address"], [True]),
                              (5, [1]), ("/fx/count", ["many"]), ("/fx/selected", [3]), ("/fx/transport/play", [])]:
            self.assertFalse(link.receive(address, args), (address, args))
        self.assertEqual(backend.params, {})

    def test_toggles_follow_the_report(self):
        link, backend = self.link()
        node = self.contract["nodes"][0]
        control = next(c for c in node["controls"] if c["path"] == "bypass")
        link.receive(control["address"], [1])
        self.assertEqual(backend.params[(node["comp"], "Bypass")], 1)
        link.receive(control["address"], [0])
        self.assertEqual(backend.params[(node["comp"], "Bypass")], 0)

    def test_status_reports_the_link_and_a_mismatch(self):
        link, backend = self.link()
        status = ("", self.contract["rack"]["status"]["par"])
        self.assertTrue(link.receive("/fx/count", [len(self.contract["nodes"])]))
        self.assertEqual(backend.params[status], "linked: %d processors" % len(self.contract["nodes"]))
        link.receive("/fx/count", [len(self.contract["nodes"]) + 2])
        self.assertTrue(backend.params[status].startswith("mismatch: rack has"))
        link, backend = self.link()
        node = self.contract["nodes"][0]
        link.receive(node["typeAddress"], ["not-" + node["type"]])
        self.assertTrue(backend.params[status].startswith("mismatch: " + node["id"]))

    def test_a_sent_edit_does_not_touch_the_rules_here_the_rack_decides_and_reports(self):
        link, backend = self.link()
        rules = []
        backend.rule = lambda comp, kind, path, payload: rules.append((comp, kind, path, payload))
        node, control = self.first_range()
        value = control["min"] + 0.25 * (control["max"] - control["min"])
        self.assertIsNotNone(link.par_changed(node["comp"], control["par"], value))
        self.assertEqual(rules, [], "no local release: while the workbench records, the same edit keeps the lane and adds an event")
        self.assertTrue(link.receive(control["rules"]["released"], []))   # what the rack reports for a hand edit
        self.assertEqual(rules, [(node["comp"], "released", control["path"], None)])
        link.receive(control["address"], [0.9])                            # a value report never changes rules
        self.assertEqual(len(rules), 1)

    def test_edits_become_control_messages_and_repeats_are_not_resent(self):
        link, backend = self.link()
        node, control = self.first_range()
        value = control["min"] + 0.25 * (control["max"] - control["min"])
        sent = link.par_changed(node["comp"], control["par"], value)
        self.assertEqual(sent, (control["address"], [0.25]))
        self.assertIsNone(link.par_changed(node["comp"], control["par"], value))
        self.assertIsNotNone(link.par_changed(node["comp"], control["par"], control["max"]))
        self.assertEqual([s[0] for s in backend.sent], [control["address"]] * 2)

    def test_what_was_just_received_is_not_sent_back(self):
        link, backend = self.link()
        node, control = self.first_range()
        link.receive(control["address"], [0.5])
        written = backend.params[(node["comp"], control["par"])]
        self.assertIsNone(link.par_changed(node["comp"], control["par"], written))
        self.assertEqual(backend.sent, [])

    def test_switch_selection_transport_and_pulses(self):
        link, backend = self.link()
        node = self.contract["nodes"][0]
        self.assertEqual(link.par_changed(node["comp"], "Solo", 1), (next(c["address"] for c in node["controls"] if c["path"] == "solo"), [1]))
        self.assertEqual(link.par_changed("", "Selected", node["id"]), ("/fx/selected", [node["id"]]))
        self.assertIsNone(link.par_changed("", "Selected", ""))
        self.assertEqual(link.par_changed("", "Play", 1), ("/fx/transport/play", [1]))
        self.assertEqual(link.pulse("", "Next"), ("/fx/select/next", []))
        self.assertEqual(link.pulse("", "Prev"), ("/fx/select/prev", []))
        self.assertIsNone(link.pulse("", "Selected"))
        self.assertIsNone(link.par_changed(node["comp"], "Nodeid", "x"), "read-only identity parameters are not controls")
        link.receive("/fx/selected", [node["id"]])
        self.assertIsNone(link.par_changed("", "Selected", node["id"]), "the rack's own report is not an edit")

    def test_sync_forgets_what_was_exchanged_and_asks_the_rack_again(self):
        link, backend = self.link()
        node, control = self.first_range()
        link.par_changed(node["comp"], control["par"], control["max"])
        link.pulse("", "Sync")
        self.assertEqual(backend.sent[-1], ("/fx/sync", []))
        self.assertIsNotNone(link.par_changed(node["comp"], control["par"], control["max"]), "after a resync the same value is news again")

    def test_time_and_selection_reports(self):
        link, backend = self.link()
        link.receive("/fx/time", [3.25])
        self.assertEqual(backend.params[("", "Time")], 3.25)
        self.assertTrue(link.receive("/fx/selected/type", ["queue"]))
        self.assertTrue(link.receive("/fx/transport/loop", [1]))
        self.assertEqual(backend.params[("", "Loop")], 1)

    def test_a_foreign_contract_is_refused(self):
        with self.assertRaises(ValueError):
            osc_lib.RackLink({"schema": "other"}, RecordingBackend())

    def test_scaling_keeps_the_rack_position_and_rounds_only_integers(self):
        control = {"min": 0.04, "max": 2, "step": 0.01}
        value = osc_lib.norm_to_value(control, 0.226)
        self.assertAlmostEqual(value, 0.04 + 0.226 * 1.96, places=9)
        self.assertEqual(osc_lib.norm_to_value({"min": 1, "max": 8, "step": 1, "integer": True}, 0.5), 5)
        self.assertEqual(osc_lib.norm_to_value(control, 7), 2)

    def test_applying_a_report_as_a_parameter_write_sends_nothing_back(self):
        """TouchDesigner fires its parameter callback on every write; the module must not answer its own report."""
        class Echoing(RecordingBackend):
            link = None

            def set(self, comp, par, value):
                changed = self.params.get((comp, par)) != value
                super().set(comp, par, value)
                if changed:
                    self.link.par_changed(comp, par, value)

        backend = Echoing()
        link = osc_lib.RackLink(self.contract, backend)
        backend.link = link
        for node in self.contract["nodes"]:
            for control in node["controls"]:
                if control["kind"] == "range":
                    for norm in (0.0, 0.226, 0.5, 0.7771, 1.0):
                        link.receive(control["address"], [round(norm, 4)])
                else:
                    link.receive(control["address"], [1])
        self.assertEqual(backend.sent, [])

    def test_the_python_text_runs_on_the_python_touchdesigner_ships(self):
        import ast
        for path in sorted(PYTHON_DIR.glob("*.py")):
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 11))


class TouchDesignerBackendTests(unittest.TestCase):
    def test_osc_values_are_sent_as_positional_arguments_not_as_one_list(self):
        """OSC Out DAT's sendOSC(address, *values, ...) takes the values positionally."""
        sent = []

        class Out:
            def sendOSC(self, address, *values, **options):
                sent.append((address, values))

        class Rack:
            path = "/project1/fx_rack"

            def op(self, name):
                return Out() if name == "osc_out" else None

        backend = osc_lib.TDBackend(Rack())
        backend.send("/fx/node/a/intensity", [0.25])
        backend.send("/fx/sync", [])
        backend.send("/fx/selected", ["a"])
        self.assertEqual(sent, [("/fx/node/a/intensity", (0.25,)), ("/fx/sync", ()), ("/fx/selected", ("a",))])


class GoldenAddressTests(unittest.TestCase):
    def test_ids_with_reserved_characters_round_trip_through_the_generated_contract(self):
        patch = json.loads(FIXTURE.read_text())
        renames = {"inbox": "a/b", "recurrence": "fx 1", "closed": "ünï"}
        text = FIXTURE.read_text()
        for old, new in renames.items():
            text = text.replace('"%s"' % old, json.dumps(new, ensure_ascii=False))
        patch = json.loads(text)
        directory = Path(tempfile.mkdtemp(prefix="td-net-"))
        try:
            patch_path = directory / "odd.json"
            patch_path.write_text(json.dumps(patch), encoding="utf-8")
            out = generate(patch_path)
            contract = json.loads((out / "closed-inbox.network.json").read_text())["contract"]
            backend = RecordingBackend()
            link = osc_lib.RackLink(contract, backend)
            for node_id, encoded in {"a/b": "a%2Fb", "fx 1": "fx%201", "ünï": "%C3%BCn%C3%AF"}.items():
                node = next(n for n in contract["nodes"] if n["id"] == node_id)
                self.assertEqual(node["typeAddress"], "/fx/node/%s/type" % encoded)
                self.assertTrue(link.receive("/fx/node/%s/wet" % encoded, [0.25]))
                self.assertEqual(backend.params[(node["comp"], "Wet")], 0.25)
            for bad in ["/fx/node/a/b/wet", "/fx/node/%zz/wet", "/fx/node/%C3/wet", "/fx/node/ünï/wet"]:
                self.assertFalse(link.receive(bad, [0.5]), bad)
            shutil.rmtree(out, ignore_errors=True)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
