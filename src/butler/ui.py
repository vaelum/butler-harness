"""Terminal output.

The visual language is lifted from the newer of the hand-rolled butlers, which is
the one worth keeping: a bold-blue echoed command with a dim cwd, then green for
what happened, yellow for "you probably want to know", red for failure. The
older ones printed undecorated `Running: ...` lines; those become
`cmd()` calls here so every project reads the same.

Colour is disabled when stdout isn't a tty, when NO_COLOR is set (the informal
standard), or via --no-color.
"""

from __future__ import annotations

import os
import sys

_RESET = "\033[0m"
_BOLD_BLUE = "\033[1;34m"
_BOLD_GREEN = "\033[1;32m"
_BOLD_YELLOW = "\033[1;33m"
_BOLD_RED = "\033[1;31m"
_DIM = "\033[2m"
_BOLD = "\033[1m"

_color: bool | None = None


def init(no_color: bool = False) -> None:
    """Decide once, at startup, whether anything is coloured."""
    global _color
    _color = not (
        no_color
        or os.environ.get("NO_COLOR") is not None
        or not sys.stdout.isatty()
    )


def _enabled() -> bool:
    if _color is None:
        init()
    return bool(_color)


def _wrap(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if _enabled() else text


def bold(text: str) -> str:
    return _wrap(_BOLD, text)


def dim(text: str) -> str:
    return _wrap(_DIM, text)


def cmd(printable: str, cwd: str | None = None, *, note: str = "") -> None:
    """Echo a command about to run. Blank line either side: build output from
    the child process runs straight into this otherwise, and the boundary
    between "what butler asked for" and "what the tool said" is the single most
    useful thing in a long log."""
    suffix = ""
    if cwd:
        suffix = "  " + dim(f"(cwd={cwd}{', ' + note if note else ''})")
    elif note:
        suffix = "  " + dim(f"({note})")
    print(f"\n{_wrap(_BOLD_BLUE, '$ ' + printable)}{suffix}\n", flush=True)


def ok(label: str, rest: str = "") -> None:
    print(f"{_wrap(_BOLD_GREEN, label)}{' ' + rest if rest else ''}")


def warn(label: str, rest: str = "") -> None:
    print(f"{_wrap(_BOLD_YELLOW, label)}{' ' + rest if rest else ''}")


def err(message: str, hint: str | None = None) -> None:
    print(f"{_wrap(_BOLD_RED, 'error:')} {message}", file=sys.stderr)
    if hint:
        for line in hint.splitlines():
            print(f"       {line}", file=sys.stderr)


def note(message: str) -> None:
    warn("note:", message)


def plain(message: str = "") -> None:
    print(message)
