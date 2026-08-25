"""Terminal styling that disappears when it should.

Colour is worth having on the one line that carries the verdict, and actively
harmful everywhere it leaks: into pipes, into CI logs, into a file someone
redirected. The rules here are the ones users already expect from other tools,
so nobody has to learn ours:

  NO_COLOR set (any value)   never colour            https://no-color.org
  FORCE_COLOR set            always colour           (CI that renders ANSI)
  stdout is not a TTY        never colour            piped, redirected, captured
  TERM=dumb                  never colour

Nothing here changes what is written -- only how it looks. Every word in the
output means the same thing with colour stripped, because for most readers it
will be.
"""

from __future__ import annotations

import os
import sys

_RESET = "\033[0m"
_CODES = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}


def colour_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def style(text: str, *names: str, stream=None) -> str:
    """`text` wrapped in `names`, or unchanged when colour is off."""
    if not names or not colour_enabled(stream):
        return text
    prefix = "".join(_CODES[n] for n in names if n in _CODES)
    return f"{prefix}{text}{_RESET}" if prefix else text


#: How each verdict should read at a glance. A proof and a refutation are the
#: two things worth spotting without reading, and "inconclusive" must not look
#: like either -- it is the one people most often mistake for a pass.
VERDICT_STYLE = {
    "verified": ("green", "bold"),
    "counterexample": ("red", "bold"),
    "unwind_limit": ("yellow",),
    "timeout": ("yellow",),
    "unknown": ("yellow",),
    "parse_error": ("yellow",),
    "tool_error": ("yellow",),
}


def verdict(name: str, stream=None) -> str:
    return style(name, *VERDICT_STYLE.get(name.lower().split()[0], ()), stream=stream)
