"""The one way the harness reports failure.

Every one of the five hand-rolled butlers called `sys.exit()` from deep inside
helper functions, or returned bare exit codes that callers had to remember to
propagate. Neither composes: a helper that
exits can't be reused by a task that wants to recover, and a helper that returns
a code silently succeeds when a caller forgets to check it.

Here, helpers raise. `cli.main` is the only place that turns a failure into an
exit code, so a task wrapping a built-in can always catch and carry on.
"""

from __future__ import annotations


class ButlerError(Exception):
    """A failure with a message meant for the user, not a traceback.

    `hint` is printed as an indented follow-up line — use it for the command
    that fixes the problem, which is almost always what the user wants next.
    """

    def __init__(self, message: str, *, hint: str | None = None, code: int = 1):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


class ConfigError(ButlerError):
    """butler.toml is missing, malformed, or says something impossible."""


class MissingToolError(ButlerError):
    """A required external tool (cmake, adb, docker, keytool, ...) isn't usable.

    Separate from ButlerError so `doctor` can collect these without the run
    aborting at the first one.
    """

    def __init__(self, tool: str, *, hint: str | None = None):
        super().__init__(f"{tool} not found", hint=hint, code=127)
        self.tool = tool
