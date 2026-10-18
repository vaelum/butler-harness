"""`butler.py planning …`: the installed waterrise, run for this project.

The planning code is its own project, waterrise, installed once per machine
from its own checkout (`python -m waterrise install`). This command does three
things and nothing else:

  1. find it: `$WATERRISE` (an executable, or a checkout to run with
     `python -m waterrise`), else `waterrise` on PATH, else the launcher
     `~/.local/bin/waterrise`, else the venv's script;
  2. check it: `waterrise install --check <pin>`, the pin being the version
     this harness was tested with (waterrise_pin.py); on a refusal, one
     message that says what is installed, what is needed and how to fix it;
  3. run it: exec, with the same arguments, in this project. The tool's exit
     code, its tty, colours and Ctrl-C are its own.

The environment tells waterrise which project and folder, under which name,
and how to name itself in its messages, so the output reads as it always did.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .command import Node, arg
from .context import Ctx
from .errors import ButlerError
from .waterrise_pin import pinned

PROG = "python butler.py planning"
INSTALL = ("git clone https://github.com/vaelum/waterrise.git && cd waterrise && "
           "python3 -m waterrise install")
# Run a checkout given as $WATERRISE without installing it, as
# BUTLER_HARNESS_PATH does for the harness.
_FROM_CHECKOUT = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                  "from waterrise.__main__ import main; sys.exit(main())")


def find() -> list[str] | None:
    """The command line that runs waterrise, or None if it is not there."""
    given = os.environ.get("WATERRISE")
    if given:
        path = Path(given).expanduser()
        if path.is_dir():
            return [sys.executable, "-P", "-c", _FROM_CHECKOUT, str(path.resolve())]
        return [str(path)] if path.is_file() else None
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    for found in (_which("waterrise"), Path.home() / ".local" / "bin" / "waterrise",
                  data / "waterrise" / "venv" / "bin" / "waterrise"):
        if found and Path(found).is_file():
            return [str(found)]
    return None


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def check(cmd: list[str]) -> tuple[bool, str]:
    """(ok, what the installed waterrise said about the pin)."""
    try:
        out = subprocess.run([*cmd, "--no-color", "install", "--check", pinned()],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{' '.join(cmd)} does not run: {e}"
    said = [line for line in (out.stderr + out.stdout).splitlines() if line.strip()]
    first = said[0].removeprefix("error: ") if said else f"exit {out.returncode}"
    if out.returncode == 0:
        return True, first
    # waterrise before 2026.10 knows no `install`; neither do a few wrong files.
    if "invalid choice: 'install'" in out.stderr:
        first = f"{' '.join(cmd)} is not a waterrise this harness can use"
    return False, first


def environment(ctx: Ctx) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"WATERRISE_PROJECT": str(ctx.root),
                "WATERRISE_PLANNING_DIR": str(ctx.cfg.planning.dir),
                "WATERRISE_PROJECT_NAME": ctx.name,
                "WATERRISE_PROG": PROG})
    return env


def run(ctx: Ctx, args) -> int:
    cmd = find()
    if cmd is None:
        raise ButlerError(f"waterrise is not installed; this harness needs {pinned()} or later",
                          hint=INSTALL)
    ok, said = check(cmd)
    if not ok:
        raise ButlerError(said, hint=f"this harness needs waterrise {pinned()} or later:\n{INSTALL}\n"
                                     "(in a checkout of it: git pull && python3 -m waterrise install)")
    flags = [f for f, on in (("-v", ctx.verbose), ("-n", ctx.dry_run),
                             ("--no-color", getattr(args, "no_color", False)), ("--yes", ctx.assume_yes)) if on]
    argv = [*cmd, *flags, *args.waterrise_args]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(argv[0], argv, environment(ctx))
    return 0  # not reached


def status() -> tuple[str | None, str]:
    """For doctor: (where it is or None, what it says about the pin)."""
    cmd = find()
    if cmd is None:
        return None, f"needs {pinned()} or later: {INSTALL}"
    ok, said = check(cmd)
    return (" ".join(cmd) if ok else None), said


def node() -> Node:
    return Node("planning", "typed plans: check, status, decisions, approvals, and the local pages "
                            "(runs the installed waterrise)",
                args=[arg("waterrise_args", nargs="...", metavar="ARGS",
                          help="waterrise's command and its arguments: python butler.py planning --help")],
                func=run)
