"""`python -m butler` — what the bootstrap shim execs into."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]

    # `new` scaffolds a project and therefore must work where no butler.toml
    # exists yet — it can't go through the normal config-loading path.
    if argv and argv[0] == "new":
        from .new import main as new_main

        return new_main(argv[1:])

    from .cli import main as cli_main

    # The shim exports the directory it lives in, so `python /elsewhere/butler.py`
    # works from any cwd. Without it we fall back to walking up from cwd.
    env_root = os.environ.get("BUTLER_PROJECT_ROOT")
    root = Path(env_root) if env_root else None
    return cli_main(argv, root=root)


if __name__ == "__main__":
    raise SystemExit(main())
