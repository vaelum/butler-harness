#!/usr/bin/env python3
"""butler's own butler — the harness driving itself.

    python3 butler.py release 0.8.1
    python3 butler.py publish check
    python3 butler.py --help

Every other project in the fleet has a *bootstrap shim* here: ~90 lines that
install a pinned release of the harness into a cached virtualenv and hand off to
it. This file is not that, and must not become it. The harness pinning itself
would mean a release of butler is cut by whatever older butler the pin names —
so a broken release step could not be fixed by the very commit that fixes it,
and a new one could never be used to ship itself.

So this runs `src/` directly, the way `BUTLER_HARNESS_PATH` does for everyone
else. The code that cuts the release is the code being released, which is the
only arrangement in which `release --retag` means anything here: fix the bug,
re-cut the same version, and the fixed code is what runs.

Stdlib only, like the shim, and for the same reason: a fresh clone can release
the harness with nothing installed.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    # This file is called butler.py and sits next to a butler/ directory
    # (butler.toml lives there). Both shadow the real package: `import butler`
    # from this directory finds THIS FILE. So the script's own directory comes
    # off sys.path before src/ goes on — the same hole the generated shim
    # closes with `python -P`.
    for entry in ("", str(ROOT), str(Path.cwd())):
        while entry in sys.path:
            sys.path.remove(entry)
    sys.path.insert(0, str(ROOT / "src"))

    # The project root travels in the environment, as it does from a generated
    # shim, so `python3 /fast/projects/butler/butler.py release …` works from
    # wherever you happen to be standing.
    os.environ["BUTLER_PROJECT_ROOT"] = str(ROOT)

    from butler.__main__ import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
