"""The silent launch helper mutes at the source: flags, media elements and the AudioContext destination.

Every fixture here is silent content (zero-sample WAV, a zero-amplitude oscillator), so a regression in the helper
fails an assertion and never makes a sound. Browsers launch only through ``silent_launch``.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from tools.silent_browser import (INIT_SCRIPT_PATH, MUTE_ARGS, assert_no_closed_shadow_roots, init_script,
                                  silence_browser, silence_context, silent_args, silent_launch,
                                  silent_launch_persistent_context)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def browser_options() -> dict:
    """A pinned system Chrome when one is given or present, else Playwright's own Chromium (what CI installs)."""
    configured = os.environ.get('PORTVS_BROWSER_EXECUTABLE') or os.environ.get('SYSTEM_CHROME')
    if configured:
        return {'executable_path': configured}
    return {'executable_path': DEFAULT_CHROME} if Path(DEFAULT_CHROME).is_file() else {}


def silent_wav_uri() -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
        stream.writeframes(b'\x00\x00' * 4000)
    return 'data:audio/wav;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def page_uri(body: str) -> str:
    html = f'<!doctype html><meta charset="utf-8"><body>{body}</body>'
    # Pre-flight (AV #186): a closed declarative shadow root would hide media no page script can reach, so the
    # page is refused here, before any browser launches, instead of going audible.
    assert_no_closed_shadow_roots(html, 'page_uri fixture')
    return 'data:text/html;base64,' + base64.b64encode(html.encode('utf-8')).decode('ascii')


# Chromium-only flags: other engines' launchers do not take them.
CHROMIUM_FLAGS = ['--no-sandbox', '--autoplay-policy=no-user-gesture-required']


class FakeEngine:
    def __init__(self, name: str):
        self.name, self.launched = name, None

    def launch(self, **kwargs):
        self.launched = kwargs
        return FakeBrowser()

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.launched = {'user_data_dir': user_data_dir, **kwargs}
        context = FakeContext()
        context.pages.append(FakePage(context))  # a persistent context opens its first page itself
        return context


class FakePage:
    def __init__(self, context):
        self.context, self.closed, self.evaluated = context, False, []

    def evaluate(self, script):
        self.evaluated.append(script)

    def close(self, **kwargs):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.scripts, self.closed, self.pages = [], False, []

    def add_init_script(self, **kwargs):
        self.scripts.append(kwargs)

    def new_page(self):
        self.pages.append(FakePage(self)); return self.pages[-1]

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []

    def new_context(self, **kwargs):
        context = FakeContext(); self.contexts.append(context); return context

    def new_page(self, **kwargs):  # Playwright's own: replaced by the helper
        raise AssertionError('the original new_page must never run: it makes its context after the fact')


class SilentLaunchContractTests(unittest.TestCase):
    def test_chromium_gets_mute_audio_merged_with_the_callers_arguments(self):
        engine = FakeEngine('chromium')
        silent_launch(engine, headless=True, args=['--no-sandbox', '--autoplay-policy=no-user-gesture-required'])
        self.assertEqual(engine.launched['args'], ['--no-sandbox', '--autoplay-policy=no-user-gesture-required', *MUTE_ARGS])
        self.assertTrue(engine.launched['headless'])

    def test_mute_flag_is_added_once_and_a_missing_args_list_still_gets_it(self):
        self.assertEqual(silent_args('chromium', ['--mute-audio']), ['--mute-audio'])
        self.assertEqual(silent_args('chromium'), ['--mute-audio'])

    def test_webkit_and_firefox_have_no_flag_and_rely_on_the_init_script(self):
        self.assertEqual(silent_args('webkit', ['--x']), ['--x'])
        self.assertEqual(silent_args('firefox'), [])

    def test_firefox_launch_forwards_caller_prefs_and_injects_no_engine_pref(self):
        # AV #186: Playwright cannot set the float pref media.volume_scale through firefox_user_prefs — a number
        # breaks every Firefox launch (NS_ERROR_UNEXPECTED from setIntPref; verified live at Playwright 1.57.0 /
        # Firefox 144.0.2) and a string is ignored by the float getter — so no pref is injected, ever.
        engine = FakeEngine('firefox')
        silent_launch(engine, headless=True, firefox_user_prefs={'layout.css.devPixelsPerPx': '2.0'})
        self.assertEqual(engine.launched['firefox_user_prefs'], {'layout.css.devPixelsPerPx': '2.0'})
        self.assertNotIn('media.volume_scale', engine.launched.get('firefox_user_prefs', {}))
        engine = FakeEngine('firefox')
        silent_launch(engine, headless=True)
        self.assertNotIn('firefox_user_prefs', engine.launched)
        engine = FakeEngine('firefox')
        silent_launch_persistent_context(engine, '/profile', firefox_user_prefs={'foo': 1})
        self.assertEqual(engine.launched['firefox_user_prefs'], {'foo': 1})
        self.assertNotIn('media.volume_scale', engine.launched['firefox_user_prefs'])

    def test_every_context_and_page_gets_the_init_script(self):
        browser = silent_launch(FakeEngine('webkit'))
        context = browser.new_context()
        page = browser.new_page()
        self.assertEqual(context.scripts, [{'path': str(INIT_SCRIPT_PATH)}])
        self.assertEqual(page.context.scripts, [{'path': str(INIT_SCRIPT_PATH)}])
        self.assertEqual(len(browser.contexts), 2)

    def test_new_page_makes_its_silenced_context_first_and_closes_it_with_the_page(self):
        browser = silent_launch(FakeEngine('webkit'))
        page = browser.new_page(viewport={'width': 1})
        context = page.context
        self.assertEqual(context.scripts, [{'path': str(INIT_SCRIPT_PATH)}])
        page.close()
        self.assertTrue(page.closed and context.closed)

    def test_wrapping_twice_does_not_inject_twice(self):
        browser = silent_launch(FakeEngine('webkit'))
        self.assertIs(silence_browser(browser), browser)
        self.assertEqual(len(browser.new_context().scripts), 1)

    def test_persistent_contexts_are_silenced_too(self):
        engine = FakeEngine('chromium')
        context = silent_launch_persistent_context(engine, '/profile', args=['--no-sandbox'])
        self.assertEqual(engine.launched['args'], ['--no-sandbox', '--mute-audio'])
        self.assertEqual(len(context.scripts), 1)
        self.assertEqual(context.pages[0].evaluated, [init_script()], 'the page already open gets the script run in it')

    def test_init_script_is_nonempty_and_has_no_device_output_path(self):
        script = init_script()
        self.assertIn('__SILENT_BROWSER__', script)
        self.assertNotIn('setSinkId', script)


class NodeHelperTests(unittest.TestCase):
    def test_node_helper_applies_the_same_mute_as_the_python_helper(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'node is required for the Node twin of the helper')
        completed = subprocess.run([node, '--test', str(REPO / 'tests/silent-browser.test.mjs')], check=False,
                                   capture_output=True, text=True, cwd=REPO)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class ClosedShadowRootPreflightTests(unittest.TestCase):
    """AV #186: a closed declarative shadow root hides media no page script can reach, so the page is refused
    before any browser launches, naming the page. Open roots keep passing — the init script pins them."""

    def test_closed_roots_are_refused_naming_the_page(self):
        for html in ('<template shadowrootmode="closed"><audio src="x"></audio></template>',
                     "<template shadowrootmode='closed'><audio src='x'></audio></template>",
                     '<template shadowrootmode=closed><audio src="x"></audio></template>',
                     '<template  shadowrootmode  =  "closed"  ><audio src="x"></audio></template>',
                     '<TEMPLATE SHADOWROOTMODE="CLOSED"><audio src="x"></audio></TEMPLATE>'):
            with self.assertRaisesRegex(ValueError, 'closed declarative shadow root'):
                assert_no_closed_shadow_roots(html, 'web/fx/index.html')
            with self.assertRaisesRegex(ValueError, 'web/fx/index.html'):
                assert_no_closed_shadow_roots(html, 'web/fx/index.html')

    def test_open_roots_and_plain_pages_pass(self):
        for html in ('<template shadowrootmode="open"><audio src="x"></audio></template>',
                     "<template shadowrootmode='open'><audio src='x'></audio></template>",
                     '<audio src="x"></audio>', '', '<template><audio src="x"></audio></template>'):
            assert_no_closed_shadow_roots(html, 'web/fx/index.html')

    def test_every_page_the_suite_loads_is_preflighted(self):
        with self.assertRaisesRegex(ValueError, 'page_uri fixture'):
            page_uri('<template shadowrootmode="closed"><audio src="x"></audio></template>')

    def test_no_html_page_in_the_repo_uses_a_closed_declarative_shadow_root(self):
        skipped = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}
        pages = [p for p in sorted(REPO.rglob('*.html'))
                 if not (set(p.relative_to(REPO).parts) & skipped)]
        self.assertGreater(len(pages), 0, 'the scan must actually walk the tree')
        for page in pages:
            assert_no_closed_shadow_roots(page.read_text(encoding='utf-8'),
                                           str(page.relative_to(REPO)))


PAGE = page_uri(f'''
<audio id="parsed" autoplay loop src="{silent_wav_uri()}"></audio>
<video id="parsed_video" loop src="{silent_wav_uri()}"></video>
<div id="later"></div><iframe id="frame" srcdoc="<audio id='inner' loop src='{silent_wav_uri()}'></audio>"></iframe>''')


class SilentBrowserPageTests(unittest.TestCase):
    engine_name = 'chromium'

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get('SILENT_BROWSER_REQUIRE_PAGE') == '1':
                raise
            raise unittest.SkipTest('playwright is not installed here; CI installs it and sets SILENT_BROWSER_REQUIRE_PAGE=1')
        cls.pw = sync_playwright().start()
        engine = getattr(cls.pw, cls.engine_name)
        try:
            options = browser_options() if cls.engine_name == 'chromium' else {}
            cls.browser = silent_launch(engine, headless=True, args=CHROMIUM_FLAGS if cls.engine_name == 'chromium' else [],
                                        **options)
        except Exception as error:
            cls.pw.stop()
            missing = "Executable doesn't exist" in str(error)
            if cls.engine_name == 'chromium' or not missing or os.environ.get('SILENT_BROWSER_REQUIRE_WEBKIT') == '1':
                raise  # a job that installs WebKit sets the variable: a missing WebKit there is a failure, never a skip
            raise unittest.SkipTest(f'{cls.engine_name} is not installed here: {str(error).splitlines()[0]}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.goto(PAGE)

    def native(self, expression):
        return self.page.evaluate(f'(() => {{ const el = {expression}; return window.__SILENT_BROWSER__.native(el); }})()')

    def test_documents_written_into_a_blank_page_are_silenced_too(self):
        # set_content and add_script_tag write into the initial blank document, which only a context-level script covers.
        blank = self.browser.new_page()
        self.addCleanup(blank.close)
        self.assertTrue(blank.evaluate('typeof window.__SILENT_BROWSER__ === "object"'))
        blank.set_content('<audio id="a"></audio>')
        self.assertTrue(blank.evaluate('window.__SILENT_BROWSER__.native(document.querySelector("#a")).muted === true'))

    def test_media_routed_through_the_source_node_constructor_is_released_like_the_factory(self):
        result = self.page.evaluate("""() => {
          const Context = window.AudioContext || window.webkitAudioContext;
          const ctx = new Context(), el = document.querySelector('#parsed_video');
          const node = new MediaElementAudioSourceNode(ctx, {mediaElement: el});
          node.connect(ctx.destination);
          el.muted = false; el.volume = 0.25;
          const out = {captured: window.__SILENT_BROWSER__.captured(el), native: window.__SILENT_BROWSER__.native(el),
                       isNode: node instanceof MediaElementAudioSourceNode, sinkGain: window.__SILENT_BROWSER__.sinkInfo(ctx).gain};
          ctx.close();
          return out;
        }""")
        self.assertTrue(result['captured'] and result['isNode'])
        self.assertEqual(result['native'], {'muted': False, 'volume': 0.25})
        self.assertEqual(result['sinkGain'], 0)

    def test_media_inside_nested_declarative_shadow_roots_is_pinned(self):
        result = self.page.evaluate("""async () => {
          if (typeof Element.prototype.setHTMLUnsafe !== 'function') return {skipped: true};
          const host = document.createElement('div'); document.body.append(host);
          const src = document.querySelector('#parsed').src;
          host.setHTMLUnsafe(`<div><template shadowrootmode="open"><div><template shadowrootmode="open"><audio id="deep" loop src="${src}"></audio></template></div></template></div>`);
          await new Promise(resolve => setTimeout(resolve, 0));
          const inner = host.firstElementChild.shadowRoot.firstElementChild.shadowRoot.querySelector('#deep');
          return {native: window.__SILENT_BROWSER__.native(inner)};
        }""")
        if result.get('skipped'):
            self.skipTest('declarative shadow DOM parsing (setHTMLUnsafe) is not exposed by this engine')
        self.assertEqual(result['native'], {'muted': True, 'volume': 0})

    def test_the_first_page_of_a_persistent_context_is_silenced_before_the_caller_writes_into_it(self):
        engine = getattr(self.pw, self.engine_name)
        options = (browser_options() if self.engine_name == 'chromium' else {})
        with tempfile.TemporaryDirectory() as profile:
            context = silent_launch_persistent_context(engine, profile, headless=True, args=['--no-sandbox'] if self.engine_name == 'chromium' else [], **options)
            try:
                first = context.pages[0]
                self.assertTrue(first.evaluate('typeof window.__SILENT_BROWSER__ === "object"'))
                first.set_content('<audio id="a" autoplay loop></audio>')
                self.assertEqual(first.evaluate('window.__SILENT_BROWSER__.native(document.querySelector("#a"))'), {'muted': True, 'volume': 0})
            finally:
                context.close()

    def test_init_script_ran_in_the_page_and_in_a_nested_frame(self):
        self.assertTrue(self.page.evaluate('typeof window.__SILENT_BROWSER__ === "object"'))
        frame = next(f for f in self.page.frames if f != self.page.main_frame)
        self.assertTrue(frame.evaluate('typeof window.__SILENT_BROWSER__ === "object"'))

    def test_parsed_autoplay_and_declared_elements_are_pinned_muted_at_volume_zero(self):
        for selector in ('#parsed', '#parsed_video'):
            self.assertEqual(self.native(f'document.querySelector("{selector}")'), {'muted': True, 'volume': 0}, selector)

    def test_elements_that_ask_for_sound_and_play_stay_pinned(self):
        result = self.page.evaluate('''async () => {
          const wav = document.querySelector('#parsed').src;
          const made = document.createElement('audio'); made.src = wav;
          const constructed = new Audio(wav);
          document.querySelector('#later').innerHTML = `<video id="injected" src="${wav}"></video>`;
          const injected = document.querySelector('#injected');
          const all = {made, constructed, injected, parsed: document.querySelector('#parsed')};
          const out = {};
          for (const [name, el] of Object.entries(all)) {
            el.muted = false; el.volume = 0.5;
            await el.play().catch(() => {});
            out[name] = {native: window.__SILENT_BROWSER__.native(el), asked: {muted: el.muted, volume: el.volume}};
          }
          return out;
        }''')
        self.assertEqual(set(result), {'made', 'constructed', 'injected', 'parsed'})
        for name, state in result.items():
            self.assertEqual(state['native'], {'muted': True, 'volume': 0}, name)
            self.assertEqual(state['asked'], {'muted': False, 'volume': 0.5}, f'{name}: the app still reads back what it set')

    def test_volume_setter_keeps_the_native_range_check(self):
        raised = self.page.evaluate('''() => { try { document.querySelector('#parsed').volume = 3; return false; } catch (e) { return e.name; } }''')
        self.assertEqual(raised, 'IndexSizeError')
        self.assertEqual(self.native('document.querySelector("#parsed")'), {'muted': True, 'volume': 0})

    def test_realtime_audio_context_destination_goes_through_a_gain_zero_sink(self):
        result = self.page.evaluate('''() => {
          const Context = window.AudioContext || window.webkitAudioContext;
          const ctx = new Context();
          const osc = ctx.createOscillator(), level = ctx.createGain(); level.gain.value = 0;
          osc.connect(level);
          const returned = level.connect(ctx.destination);
          const sink = window.__SILENT_BROWSER__.sinkInfo(ctx);
          const out = {chained: returned === ctx.destination, hasSink: !!sink, sinkGain: sink && sink.gain,
                       sinkIsGain: sink.isGain, sinkContext: sink && sink.owner};
          level.disconnect(ctx.destination);
          ctx.close();
          return out;
        }''')
        self.assertEqual(result, {'chained': True, 'hasSink': True, 'sinkGain': 0, 'sinkIsGain': True, 'sinkContext': True})

    def test_media_element_routed_into_web_audio_is_released_from_the_pin_and_silenced_in_the_graph(self):
        result = self.page.evaluate('''() => {
          const Context = window.AudioContext || window.webkitAudioContext;
          const ctx = new Context(), el = document.querySelector('#parsed_video');
          const source = ctx.createMediaElementSource(el);
          source.connect(ctx.destination);
          el.muted = false; el.volume = 0.25;
          const out = {captured: window.__SILENT_BROWSER__.captured(el), native: window.__SILENT_BROWSER__.native(el),
                       sinkGain: window.__SILENT_BROWSER__.sinkInfo(ctx).gain};
          ctx.close();
          return out;
        }''')
        self.assertTrue(result['captured'])
        self.assertEqual(result['native'], {'muted': False, 'volume': 0.25})
        self.assertEqual(result['sinkGain'], 0)

    def test_offline_rendering_keeps_its_real_destination(self):
        result = self.page.evaluate('''async () => {
          const offline = new OfflineAudioContext(1, 4000, 8000);
          const osc = offline.createOscillator(); osc.frequency.value = 440;
          osc.connect(offline.destination); osc.start();
          const buffer = await offline.startRendering();
          const data = buffer.getChannelData(0);
          let peak = 0; for (const value of data) peak = Math.max(peak, Math.abs(value));
          return {sink: window.__SILENT_BROWSER__.sinkInfo(offline), peak};
        }''')
        self.assertIsNone(result['sink'])
        self.assertGreater(result['peak'], 0.1, 'offline rendering is a buffer, not the device, and must still carry signal')

    def test_speech_synthesis_utterances_are_spoken_at_volume_zero(self):
        result = self.page.evaluate('''() => {
          if (!window.speechSynthesis) return {skipped: true};
          const utterance = new SpeechSynthesisUtterance(''); utterance.volume = 1;
          speechSynthesis.cancel(); speechSynthesis.speak(utterance); speechSynthesis.cancel();
          const ssml = new SpeechSynthesisUtterance('<speak><prosody volume="x-loud">hello</prosody></speak>'); ssml.volume = 1;
          speechSynthesis.speak(ssml); speechSynthesis.cancel();
          return {volume: utterance.volume, ssmlVolume: ssml.volume, ssmlText: ssml.text};
        }''')
        if result.get('skipped'):
            self.skipTest('speechSynthesis is not exposed by this engine')
        self.assertEqual(result['volume'], 0)
        self.assertEqual((result['ssmlVolume'], result['ssmlText']), (0, ''), 'markup carries its own volume, so it is spoken as nothing')

    def test_realtime_connections_reach_the_native_connect_only_through_the_gain_zero_sink(self):
        # A spy installed first sits underneath the silent init script, so it sees what the native connect is given.
        spy = """(() => { const calls = []; window.__connectCalls = calls; const native = AudioNode.prototype.connect;
          AudioNode.prototype.connect = function (target, ...rest) {
            calls.push({from: this.constructor.name, to: target && target.constructor && target.constructor.name});
            return native.call(this, target, ...rest);
          }; })();"""
        engine = getattr(self.pw, self.engine_name)
        options = browser_options() if self.engine_name == 'chromium' else {}
        raw = engine.launch(headless=True, args=silent_args(self.engine_name, ['--no-sandbox'] if self.engine_name == 'chromium' else []), **options)
        self.addCleanup(raw.close)
        context = raw.new_context()
        # One script, so the order is not left to Playwright: the spy is evaluated first, then the silent init wraps it.
        context.add_init_script(spy + '\n' + init_script())
        page = context.new_page()
        page.goto('data:text/html,<title>x</title>')
        seen = page.evaluate("""() => {
          const Context = window.AudioContext || window.webkitAudioContext;
          const ctx = new Context(), osc = ctx.createOscillator(), gain = ctx.createGain();
          osc.connect(gain); gain.connect(ctx.destination);
          const out = {calls: window.__connectCalls.slice(), sinkGain: window.__SILENT_BROWSER__.sinkInfo(ctx).gain};
          ctx.close();
          return out;
        }""")
        to_destination = [call for call in seen['calls'] if call['to'] == 'AudioDestinationNode']
        self.assertEqual(to_destination, [{'from': 'GainNode', 'to': 'AudioDestinationNode'}], 'only the sink itself reaches the device node')
        self.assertEqual(seen['sinkGain'], 0)
        self.assertIn({'from': 'GainNode', 'to': 'GainNode'}, seen['calls'], 'the app node was routed into the sink')

    def test_pinned_elements_announce_volume_changes_keep_the_range_check_and_shadow_roots_are_covered(self):
        result = self.page.evaluate("""async () => {
          const el = document.querySelector('#parsed');
          const events = []; el.addEventListener('volumechange', () => events.push([el.muted, el.volume]));
          el.volume = 0.5; el.volume = 0.5; el.muted = true; el.muted = true;
          let thrown = null; try { el.volume = 3; } catch (error) { thrown = error.name; }
          const afterThrow = {asked: el.volume, native: window.__SILENT_BROWSER__.native(el)};
          const host = document.createElement('div'); document.body.append(host);
          const open = host.attachShadow({mode: 'open'});
          open.innerHTML = `<audio id="in_shadow" loop src="${el.src}"></audio>`;
          await new Promise(resolve => setTimeout(resolve, 0));
          const closedHost = document.createElement('div'); document.body.append(closedHost);
          const closed = closedHost.attachShadow({mode: 'closed'});
          closed.innerHTML = `<video id="in_closed" loop src="${el.src}"></video>`;
          await new Promise(resolve => setTimeout(resolve, 0));
          return {events, thrown, afterThrow, shadowNative: window.__SILENT_BROWSER__.native(open.querySelector('#in_shadow')),
                  closedNative: window.__SILENT_BROWSER__.native(closed.querySelector('#in_closed'))};
        }""")
        self.assertEqual(result['events'], [[False, 0.5], [True, 0.5]], 'one event per change the app made, none for a repeated value')
        self.assertEqual(result['thrown'], 'IndexSizeError')
        self.assertEqual(result['afterThrow'], {'asked': 0.5, 'native': {'muted': True, 'volume': 0}})
        self.assertEqual(result['shadowNative'], {'muted': True, 'volume': 0})
        self.assertEqual(result['closedNative'], {'muted': True, 'volume': 0})


class SilentBrowserWebKitTests(SilentBrowserPageTests):
    engine_name = 'webkit'


if __name__ == '__main__':
    unittest.main()
