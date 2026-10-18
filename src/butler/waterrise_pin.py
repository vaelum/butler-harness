"""Which waterrise this harness runs: the version it was tested with.

waterrise is installed once per machine from its own checkout; the harness
only names the version it needs here, and `planning` asks the installed one
whether it serves it (`waterrise install --check`). Bumping it is a one-line
commit, tested like any other change.
"""

VERSION = "2026.10.1"


def pinned() -> str:
    return VERSION
