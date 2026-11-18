"""Launch every Playwright browser through here so no run can make a sound.

Owner rule: no test or tool may produce audible sound on the host. The mute sits at the source, in the one place
every launch passes through, and ``tests/test_silent_browser_lint.py`` fails when a launch bypasses it:

* Chromium gets ``--mute-audio`` merged into the caller's own arguments.
* Every engine gets ``silent-init.js`` in every context (``new_context`` and ``new_page``, which makes its own
  context), so media elements are pinned muted with volume 0 and every realtime AudioContext destination connection
  goes through a gain-0 sink. The script is engine-agnostic: WebKit has no mute flag, so the script is its mute.
* Firefox has no engine mute either, and none can be injected: Playwright cannot set the float pref
  ``media.volume_scale`` through ``firefox_user_prefs`` — a number breaks every Firefox launch (juggler calls
  ``setIntPref`` for any number, the pref is a float, ``NS_ERROR_UNEXPECTED``; verified live at Playwright
  1.57.0 / Firefox 144.0.2, and the same signature at 1.62.1 / Firefox 153 in timed-text #89) and a string
  is stored as a string and ignored by the float getter. Caller prefs are forwarded unchanged and no engine
  pref is injected; Firefox relies on the init script alone.
* WebKit cannot be muted at the engine either, and the init script cannot reach media parsed inside a closed
  declarative shadow root, so such a page is refused before any browser launches: call
  ``assert_no_closed_shadow_roots`` on a check page's HTML (the test harness does this for every page it
  loads, and a repo-wide test scans every ``*.html`` file) and the run fails loudly, naming the page.

Nothing here touches the system volume or the output device.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
INIT_SCRIPT_PATH = HERE / 'silent-init.js'
MUTE_ARGS = ('--mute-audio',)

# A closed declarative shadow root (<template shadowrootmode="closed">, quoting and
# whitespace variants, any case) hides media no page script can reach. The init script
# cannot pin it, so the page is refused at pre-flight instead of going audible.
CLOSED_SHADOW_ROOT_RE = re.compile(
    r"""shadowrootmode\s*=\s*(?:"closed"|'closed'|closed(?=[\s/>]))""", re.IGNORECASE)


def assert_no_closed_shadow_roots(html: str, source: str = 'page') -> None:
    """Refuse a check page whose HTML parses media into a closed declarative shadow root.

    The init script cannot reach inside such a root, so letting the page load would silently
    violate the silence rule. Raises ``ValueError`` naming the page; open roots pass.
    """
    if CLOSED_SHADOW_ROOT_RE.search(html or ''):
        raise ValueError(
            f'{source}: closed declarative shadow root (shadowrootmode="closed") — '
            'media inside it cannot be pinned, refusing the page')


def init_script() -> str:
    return INIT_SCRIPT_PATH.read_text(encoding='utf-8')


def silent_args(engine_name: str, args: Any = None) -> list[str]:
    """The caller's arguments plus the engine's mute flag, never replacing what the caller asked for."""
    merged = list(args or [])
    if engine_name == 'chromium':
        merged.extend(flag for flag in MUTE_ARGS if flag not in merged)
    return merged


def silence_context(context: Any) -> Any:
    context.add_init_script(path=str(INIT_SCRIPT_PATH))
    return context


def silence_browser(browser: Any) -> Any:
    """Make ``new_context`` and ``new_page`` on this browser hand out silenced contexts."""
    if getattr(browser, '_silent_browser', False):
        return browser
    new_context = browser.new_context

    def silent_new_context(*args: Any, **kwargs: Any) -> Any:
        return silence_context(new_context(*args, **kwargs))

    def silent_new_page(*args: Any, **kwargs: Any) -> Any:
        # Playwright's own new_page makes a context after the fact, so a script added to it would miss the blank
        # document that set_content and add_script_tag write into. Make the silenced context first, then the page,
        # and close the context with the page the way Playwright's owned context does.
        context = silent_new_context(*args, **kwargs)
        page = context.new_page()
        page_close = page.close

        def close_with_context(**close_kwargs: Any) -> None:
            try:
                page_close(**close_kwargs)
            finally:
                context.close()

        page.close = close_with_context
        return page

    browser.new_context, browser.new_page = silent_new_context, silent_new_page
    browser._silent_browser = True
    return browser


def silent_launch(engine: Any, **kwargs: Any) -> Any:
    """``playwright.<engine>.launch(**kwargs)`` with the mute applied at the source."""
    kwargs['args'] = silent_args(engine.name, kwargs.get('args'))
    return silence_browser(engine.launch(**kwargs))


def silent_launch_persistent_context(engine: Any, user_data_dir: str, **kwargs: Any) -> Any:
    """A persistent context is created with its first page already open, so that page's document is silenced by
    running the init script in it now; every later document and navigation gets it from the context."""
    kwargs['args'] = silent_args(engine.name, kwargs.get('args'))
    context = silence_context(engine.launch_persistent_context(user_data_dir, **kwargs))
    script = init_script()
    for page in context.pages:
        page.evaluate(script)
    return context
