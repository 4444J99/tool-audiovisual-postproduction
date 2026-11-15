"""Launch every Playwright browser through here so no run can make a sound.

Owner rule: no test or tool may produce audible sound on the host. The mute sits at the source, in the one place
every launch passes through, and ``tests/test_silent_browser_lint.py`` fails when a launch bypasses it:

* Chromium gets ``--mute-audio`` merged into the caller's own arguments.
* Every engine gets ``silent-init.js`` in every context (``new_context`` and ``new_page``, which makes its own
  context), so media elements are pinned muted with volume 0 and every realtime AudioContext destination connection
  goes through a gain-0 sink. The script is engine-agnostic: WebKit has no mute flag, so the script is its mute.

Nothing here touches the system volume or the output device.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
INIT_SCRIPT_PATH = HERE / 'silent-init.js'
MUTE_ARGS = ('--mute-audio',)


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
    kwargs['args'] = silent_args(engine.name, kwargs.get('args'))
    return silence_context(engine.launch_persistent_context(user_data_dir, **kwargs))
