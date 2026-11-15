"""Fail when a test or tool launches a browser without going through the silent helper.

The silence rule is absolute (no run may produce audible sound), so there is no allowlist beyond the helper pair and
this lint's own files: every launch is ``silent_launch`` (Python, ``tools/silent_browser.py``) or ``silentLaunch``
(Node, ``tools/silent-browser.mjs``).
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}
EXEMPT = {'tools/silent_browser.py', 'tools/silent-browser.mjs', 'tests/test_silent_browser.py',
          'tests/test_silent_browser_lint.py', 'tests/silent-browser.test.mjs'}
def files(suffixes):
    for path in sorted(ROOT.rglob('*')):
        parts = set(path.relative_to(ROOT).parts)
        if parts & SKIP_DIRS or not path.is_file() or path.suffix not in suffixes:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative not in EXEMPT:
            yield path, relative


ENGINE_NAMES = {'chromium', 'webkit', 'firefox', '_electron', 'electron', 'browserType', 'browser_type', 'engine'}
PLAYWRIGHT_ENGINES = {'chromium', 'webkit', 'firefox'}
# Owners that are application objects, not browser types (a driver file may drive an app that has its own launch()).
SAFE_OWNERS = {'self', 'cls', 'this', 'lab', 'e', 'live', 'studio', 'player', 'runtime', 'controller', 'deck', 'app', 'window', 'document', 'console'}
PY_LAUNCHERS = {'launch', 'launch_persistent_context', 'launch_server', 'connect_over_cdp'}
NAMES = 'launch|launchPersistentContext|launchServer|connectOverCDP'
# obj.launch(, obj\n  .launch(, obj['launch'](, and a launch pulled out by destructuring or bound for later.
JS_CALL = re.compile(rf"(?P<owner>[\w$.\]\[]*?[\w$\]])\s*\.\s*(?:{NAMES})\b|(?P<owner2>[\w$]+)\s*\[\s*['\"](?:{NAMES})['\"]\s*\]")
JS_DESTRUCTURED = re.compile(rf"\{{[^}}]*\b(?:{NAMES})\b[^}}]*\}}\s*=\s*[\w$.\]\[]*?(?:chromium|webkit|firefox|browserType|engine)\b")
# A Playwright engine under another name: const runner = chromium; const {chromium: runner} = require('playwright');
JS_ALIAS_ASSIGN = re.compile(r"([\w$]+)\s*=\s*(?:[\w$.()'\"\[\]]*?\.)?(?:chromium|webkit|firefox)\b(?!\s*\.\s*launch)")
JS_ALIAS_DESTRUCTURE = re.compile(r"\b(?:chromium|webkit|firefox)\s*:\s*([\w$]+)")
JS_ALIAS_IMPORT = re.compile(r"\b(?:chromium|webkit|firefox)\s+as\s+([\w$]+)")  # import {chromium as chrome} from 'playwright'
# Names such as ``engine`` are Playwright browser types only in a file that deals with a browser driver; application code
# has its own ``engine.launch(...)``.
DRIVER_FILE = re.compile(r"playwright|puppeteer|chromium|firefox|browserType|browser_type|\bwebkit\s*[.,)]", re.I)
JS_COMPUTED = re.compile(r"([\w$]+)\s*\[\s*[^\]'\"\s][^\]]*\]\s*\(")  # engine[method]()
JS_PUPPETEER = re.compile(r"\bpuppeteer\b\s*\.\s*launch|(?:require\(|from\s+)['\"]puppeteer")


def python_launches(source: str, name: str):
    """Playwright launches in Python: any ``.launch`` style call in a browser-driver file unless its owner is an app object,
    plus ``getattr(engine, 'launch')`` and an alias of an engine."""
    tree = ast.parse(source, name)
    driver = bool(DRIVER_FILE.search(source))
    aliases = set(ENGINE_NAMES if driver else PLAYWRIGHT_ENGINES)
    changed = True
    while changed:  # chained aliases: runner = bt; bt = pw.chromium
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Attribute, ast.Name)):
                label = node.value.attr if isinstance(node.value, ast.Attribute) else node.value.id
                if label in PLAYWRIGHT_ENGINES or label in aliases:
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id not in aliases:
                            aliases.add(target.id); changed = True
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute) and node.value.attr in PY_LAUNCHERS:
            owner = node.value.value  # a bound launch method kept for later: launch = pw.chromium.launch
            label = owner.attr if isinstance(owner, ast.Attribute) else owner.id if isinstance(owner, ast.Name) else ''
            if label in aliases or (driver and label not in SAFE_OWNERS):
                found.append(node.lineno)
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == 'getattr' and len(node.args) >= 2:
            first = node.args[0]
            label = first.attr if isinstance(first, ast.Attribute) else first.id if isinstance(first, ast.Name) else ''
            second = node.args[1]
            dynamic = not (isinstance(second, ast.Constant) and isinstance(second.value, str))
            if label in aliases and (dynamic or second.value in PY_LAUNCHERS):
                found.append(node.lineno)
            continue
        if not (isinstance(func, ast.Attribute) and func.attr in PY_LAUNCHERS):
            continue
        owner = func.value
        label = owner.attr if isinstance(owner, ast.Attribute) else owner.id if isinstance(owner, ast.Name) else ''
        if label in aliases or isinstance(owner, (ast.Call, ast.Subscript)) or (driver and label not in SAFE_OWNERS):
            found.append(node.lineno)
    return sorted(found)


def _blank_comments(source: str) -> str:
    """Blank out comments but keep every newline, so a commented launch is ignored and line numbers stay true.
    String literals are walked, so ``" // "`` inside a string never hides the code after it."""
    out, i, n, quote = [], 0, len(source), None
    while i < n:
        c, pair = source[i], source[i:i + 2]
        if quote is None:
            if pair == '//':
                j = source.find('\n', i)
                j = n if j < 0 else j
                out.append(' ' * (j - i)); i = j
                continue
            if pair == '/*':
                j = source.find('*/', i + 2)
                j = n if j < 0 else j + 2
                out.append(re.sub(r'[^\n]', ' ', source[i:j])); i = j
                continue
            if c in '"\'`':
                quote = c
            out.append(c)
        else:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(source[i + 1]); i += 2
                continue
            if c == quote or (c == '\n' and quote in '"\''):
                quote = None
        i += 1
    return ''.join(out)


def node_launches(source: str):
    code = _blank_comments(source)
    driver = bool(DRIVER_FILE.search(code))
    names = set(ENGINE_NAMES if driver else PLAYWRIGHT_ENGINES)
    changed = True
    while changed:  # chained aliases: const bt = playwright.webkit; const runner = bt;
        before = len(names)
        pattern = re.compile(r"([\w$]+)\s*=\s*(?:[\w$.()'\"\[\]]*?\.)?(?:" + "|".join(sorted(re.escape(n) for n in names)) + r")\b(?!\s*\.\s*launch)")
        names |= {m.group(1) for m in pattern.finditer(code)}
        changed = len(names) != before
    names |= {m.group(1) for m in JS_ALIAS_DESTRUCTURE.finditer(code)} | {m.group(1) for m in JS_ALIAS_IMPORT.finditer(code)}
    found = set()
    for match in JS_CALL.finditer(code):
        owner = match.group('owner') or match.group('owner2')
        tail = re.search(r"([\w$]+)$", owner)
        tail_name = tail.group(1) if tail else ''
        if (driver and owner.endswith(']')) or tail_name in names or (driver and tail_name not in SAFE_OWNERS):
            found.add(code.count('\n', 0, match.start() + len(match.group(0)) - len(re.search(rf"(?:{NAMES})\b.*$", match.group(0), re.S).group(0))) + 1)
    for match in JS_COMPUTED.finditer(code):  # chromium[method]()
        if match.group(1) in names:
            found.add(code.count('\n', 0, match.start()) + 1)
    for match in (JS_DESTRUCTURED.finditer(code) if driver else []):
        found.add(code.count('\n', 0, match.start()) + 1)
    puppeteer = JS_PUPPETEER.search(code)
    if puppeteer:
        found.add(code.count('\n', 0, puppeteer.start()) + 1)
    return sorted(found)


class SilentLaunchLintTests(unittest.TestCase):
    def test_no_python_file_launches_a_browser_around_the_helper(self):
        bare = []
        for path, relative in files({'.py'}):
            try:
                tree_source = path.read_text(encoding='utf-8')
                bare += [f'{relative}:{line}' for line in python_launches(tree_source, relative)]
            except SyntaxError:
                continue
        self.assertEqual(bare, [], 'launch through tools.silent_browser.silent_launch so the run is silent')

    def test_no_node_file_launches_a_browser_around_the_helper(self):
        bare = [f'{relative}:{line}' for path, relative in files({'.js', '.mjs', '.cjs'})
                for line in node_launches(path.read_text(encoding='utf-8', errors='replace'))]
        self.assertEqual(bare, [], 'launch through tools/silent-browser.mjs silentLaunch so the run is silent')

    def test_the_lint_sees_a_bare_launch_and_accepts_the_helper(self):
        self.assertEqual(python_launches('runner = pw.chromium\nrunner.launch()\n', 'x.py'), [2])
        self.assertEqual(python_launches('getattr(pw, name).launch()\n', 'x.py'), [1])
        self.assertEqual(node_launches('await chromium\n  .launch({headless:true});\n'), [2])
        self.assertEqual(node_launches('const runner = chromium;\nawait runner.launch();\n'), [2])
        self.assertEqual(node_launches("const {chromium: runner} = require('playwright');\nawait runner.launch();\n"), [2])
        self.assertEqual(node_launches("await playwright[kind].launch();\n"), [1])
        self.assertEqual(node_launches("await chromium['launch']();\n"), [1])
        self.assertEqual(node_launches("const {launch} = chromium;\nawait launch();\n"), [1])
        self.assertEqual(node_launches("import { chromium as chrome } from 'playwright';\nawait chrome.launch();\n"), [2])
        self.assertEqual(node_launches("const bt = playwright.webkit; const runner = bt; await runner.launch();\n"), [1])
        self.assertEqual(node_launches("const method = 'launch'; await chromium[method]();\n"), [1])
        self.assertEqual(node_launches("const pw = require('playwright'); await thing.launch();\n"), [1])
        self.assertEqual(python_launches("import playwright\nlaunch = getattr(pw.chromium, 'launch')\n", 'x.py'), [2])
        self.assertEqual(python_launches("import playwright\nbt = pw.chromium\nrunner = bt\nrunner.launch()\n", 'x.py'), [4])
        self.assertEqual(python_launches("import playwright\nlaunch = pw.chromium.launch\nlaunch()\n", 'x.py'), [2])
        self.assertEqual(node_launches('const note = " // "; webkit.launch();\n'), [1])
        self.assertEqual(node_launches('const url = "http://x"; // chromium.launch()\n'), [])
        self.assertEqual(node_launches('/* webkit.launch() */ const x = 1;\n'), [])
        self.assertEqual(node_launches('await engine.launch(id);\n'), [])
        self.assertEqual(python_launches('p.chromium.launch()\n', 'x.py'), [1])
        self.assertEqual(python_launches('pw.webkit.launch_persistent_context("/p")\n', 'x.py'), [1])
        self.assertEqual(python_launches('silent_launch(p.chromium)\n', 'x.py'), [])
        self.assertEqual(node_launches('browser = await webkit.launch({headless:true});\n'), [1])
        self.assertEqual(node_launches('await browserType.launchPersistentContext(dir, {});\n'), [1])
        self.assertEqual(node_launches('browser = await silentLaunch(webkit, {headless:true});\n'), [])
        self.assertEqual(node_launches('await player.launch(id);\n'), [])
        self.assertEqual(node_launches('// webkit.launch({headless:true})\n'), [])

    def test_the_helper_pair_is_the_only_launcher(self):
        self.assertEqual(len(python_launches((ROOT / 'tools/silent_browser.py').read_text(encoding='utf-8'), 'silent_browser.py')), 2)
        self.assertEqual(len(re.findall(r'engine\.launch(?:PersistentContext)?\(', (ROOT / 'tools/silent-browser.mjs').read_text(encoding='utf-8'))), 2)


if __name__ == '__main__':
    unittest.main()
