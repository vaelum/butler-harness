"""Reading API tokens out of the machine's secrets file.

A token is not project configuration: it belongs to the machine and to the
service that issued it, so it is never in butler.toml and never in a repository.
This module is the one place that knows where to look for one.

The file is a list of `KEY=value` lines, mode 0600, and it is *parsed* rather
than sourced — sourcing would execute whatever else ended up in it. Values never
reach the terminal: callers are told which name was used, not what it held, so a
wrong token surfaces as the service's own 401.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Where to look when the environment has no token. SECRETS_FILE overrides it,
# which is how the tests run without touching the real one.
DEFAULT_FILE = "~/.secrets"

# `export KEY=value`, `KEY="value"`, `KEY='value'`. Anything else on the line —
# a comment, a blank — simply does not match.
_LINE = re.compile(r"""^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)\s*$""")


def path() -> Path:
    return Path(os.environ.get("SECRETS_FILE", DEFAULT_FILE)).expanduser()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read(file: Path | None = None) -> dict[str, str]:
    """Every assignment in the secrets file, last one winning. Missing or
    unreadable file: no secrets, not an error — the caller decides whether the
    one it wanted was required."""
    file = file or path()
    try:
        text = file.read_text()
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            found[m["key"]] = _unquote(m["value"])
    return found


def lookup(names: list[str], file: Path | None = None) -> tuple[str, str] | None:
    """The first of `names` that has a value, as (name, value).

    The environment is consulted first and in full: an explicitly exported token
    always beats the file, which is how a one-off run overrides the machine's
    default without editing it.
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return name, value
    secrets = read(file)
    for name in names:
        value = secrets.get(name)
        if value:
            return name, value
    return None


def host_suffix(host: str) -> str:
    """`git.example.org` -> `GIT_EXAMPLE_ORG`, so a per-host token can be named
    for the host it belongs to. A credential is only ever sent to the service it
    was issued for, and the name is what says which that is."""
    return re.sub(r"[^A-Za-z0-9]", "_", host).upper()


def forgejo_names(host: str) -> list[str]:
    """The names a Forgejo token may go by, most specific last.

    The read-only one is preferred over the general one: everything the harness
    does with this token *reads* — it fetches a release someone else's CI built
    — so it has no business holding a writable one when a narrower token exists.
    """
    return ["FORGEJO_TOKEN_READONLY", f"FORGEJO_TOKEN_FOR_{host_suffix(host)}",
            "FORGEJO_TOKEN"]
