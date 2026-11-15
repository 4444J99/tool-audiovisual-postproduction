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
LAUNCHERS = {'launch', 'launch_persistent_context', 'launch_server', 'connect_over_cdp',
             'launchPersistentContext', 'launchServer', 'connectOverCDP'}
ENGINES = {'chromium', 'webkit', 'firefox', 'browser_type', 'engine', 'browserType'}
JS_CALL = re.compile(r'(?<![\w$])(?P<owner>[\w$.]*?)\b(?:launch|launchPersistentContext|launchServer|connectOverCDP)\s*\(')
JS_OWNER = re.compile(r'(?:^|\.)(?:chromium|webkit|firefox|electron|_electron|browserType|browser_type|engine)$')
JS_DIRECT = re.compile(r'\b(?:chromium|webkit|firefox)\s*\.\s*launch\b|\bpuppeteer\b\s*\.\s*launch|(?:require\(|from\s+)[\'"]puppeteer')


def files(suffixes):
    for path in sorted(ROOT.rglob('*')):
        parts = set(path.relative_to(ROOT).parts)
        if parts & SKIP_DIRS or not path.is_file() or path.suffix not in suffixes:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative not in EXEMPT:
            yield path, relative


def python_launches(source: str, name: str):
    found = []
    for node in ast.walk(ast.parse(source, name)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LAUNCHERS):
            continue
        owner = node.func.value
        label = owner.attr if isinstance(owner, ast.Attribute) else owner.id if isinstance(owner, ast.Name) else ''
        if label in ENGINES:
            found.append(node.lineno)
    return found


def node_launches(source: str):
    found = []
    for number, line in enumerate(source.splitlines(), 1):
        if re.match(r'\s*(?://|\*|/\*)', line):
            continue
        hit = any(match.group('owner').rstrip('.') and JS_OWNER.search(match.group('owner').rstrip('.'))
                  for match in JS_CALL.finditer(line))
        if hit or JS_DIRECT.search(line):
            found.append(number)
    return found


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
