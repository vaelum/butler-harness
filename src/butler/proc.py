"""Running external commands.

One implementation of the `run()` that appears three times in the existing
butlers, plus the capture/check variants the other two grew separately
(`run_command`, `_run_rc`, ad-hoc `subprocess.run(..., capture_output=True)`).

Conventions kept from the originals because they are right:

  * a missing executable is 127 (shell convention), not a traceback;
  * Ctrl-C is 130, and it stops the current command rather than unwinding
    through a half-finished multi-step task;
  * `extra_env` MERGES into the inherited environment. Every caller wants that
    — replacing it breaks PATH, HOME, SSH_AUTH_SOCK and the terminal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import ButlerError
from . import ui

Command = Sequence[str | Path]


def printable(cmd: Command) -> str:
    return " ".join(str(c) for c in cmd)


def _argv(cmd: Command) -> list[str]:
    return [str(c) for c in cmd]


def _env_for(extra: dict[str, str] | None) -> dict[str, str] | None:
    return {**os.environ, **extra} if extra else None


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def combined(self) -> str:
        return (self.out or "") + (self.err or "")


def run(
    cmd: Command,
    cwd: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
    echo: bool = True,
    note: str = "",
    dry_run: bool = False,
) -> int:
    """Echo and run a command, streaming its output. Returns the exit code."""
    if echo:
        ui.cmd(printable(cmd), str(cwd) if cwd else None, note=note)
    if dry_run:
        return 0
    try:
        return subprocess.run(_argv(cmd), cwd=str(cwd) if cwd else None,
                              env=_env_for(env)).returncode
    except FileNotFoundError as e:
        ui.err(str(e))
        return 127
    except KeyboardInterrupt:
        return 130


def check(
    cmd: Command,
    cwd: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
    echo: bool = True,
    note: str = "",
    dry_run: bool = False,
    what: str | None = None,
) -> None:
    """Like `run`, but a non-zero exit raises. Use this for the steps in a
    sequence where carrying on would be nonsense (configure before build, rsync
    before restart)."""
    rc = run(cmd, cwd, env=env, echo=echo, note=note, dry_run=dry_run)
    if rc != 0:
        label = what or printable(cmd)
        raise ButlerError(f"{label} failed with exit code {rc}", code=rc)


def capture(
    cmd: Command,
    cwd: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
    echo: bool = False,
) -> Result:
    """Run and capture. Never raises on a non-zero exit — callers that capture
    are usually probing (is a device attached? does this DB exist?) and want to
    interpret the failure themselves."""
    if echo:
        ui.cmd(printable(cmd), str(cwd) if cwd else None)
    try:
        p = subprocess.run(_argv(cmd), cwd=str(cwd) if cwd else None,
                           env=_env_for(env), capture_output=True, text=True)
    except FileNotFoundError as e:
        return Result(127, "", str(e))
    except KeyboardInterrupt:
        return Result(130, "", "interrupted")
    return Result(p.returncode, p.stdout or "", p.stderr or "")


def feed(
    cmd: Command,
    stdin_text: str,
    cwd: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
    echo: bool = True,
    dry_run: bool = False,
) -> int:
    """Run a command with `stdin_text` on stdin, streaming its output.

    The reason this exists: a remote login shell is whatever the user set (fish,
    here), so `ssh host '<sh script>'` is parsed by fish and fails on anything
    POSIX-specific. Feeding the script to `sh -s` on stdin means the login shell
    only ever sees the two words `sh -s`.
    """
    if echo:
        ui.cmd(printable(cmd), note="stdin script")
        for line in stdin_text.strip().splitlines():
            ui.plain(ui.dim("  | " + line))
        ui.plain()
    if dry_run:
        return 0
    try:
        return subprocess.run(_argv(cmd), cwd=str(cwd) if cwd else None,
                              env=_env_for(env), input=stdin_text, text=True).returncode
    except FileNotFoundError as e:
        ui.err(str(e))
        return 127
    except KeyboardInterrupt:
        return 130


def popen(
    cmd: Command,
    cwd: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
    note: str = "background",
) -> subprocess.Popen:
    """Start a long-lived child (a dev window, a throwaway backend) and return
    it. The caller is responsible for `terminate_child` in a finally — a
    backgrounded process must never outlive the command that started it."""
    ui.cmd(printable(cmd), str(cwd) if cwd else None, note=note)
    try:
        return subprocess.Popen(_argv(cmd), cwd=str(cwd) if cwd else None,
                                env=_env_for(env))
    except FileNotFoundError as e:
        raise ButlerError(str(e), code=127) from e


def terminate_child(proc: subprocess.Popen | None, timeout: float = 10) -> None:
    """SIGTERM, then SIGKILL if it won't go. Safe to call on an already-dead or
    never-started process."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


def which(tool: str, extra_path: Sequence[str | Path] = ()) -> str | None:
    """Find `tool`, looking in `extra_path` first. The extra entries matter for
    SDK-provided tools (adb under platform-tools, keytool under JAVA_HOME) that
    are usually NOT on PATH."""
    import shutil

    for d in extra_path:
        cand = Path(d) / tool
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which(tool)


def tool_version(path: str | Path) -> tuple[int, ...] | None:
    """`<tool> --version` parsed into a version tuple, or None if it won't run
    or won't say. Used to pick between several installed copies of a tool."""
    import re

    try:
        out = subprocess.check_output([str(path), "--version"],
                                      stderr=subprocess.STDOUT).decode("utf-8", "ignore")
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


def confirm(question: str, *, assume_yes: bool = False, expect: str | None = None) -> bool:
    """Ask before something irreversible.

    `expect` demands an exact word ("wipe", "reset") rather than y/N — reserve
    it for operations that destroy data. Non-interactive stdin answers no, which
    is the safe default for a CI or piped run.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        ui.warn("aborted:", "not a terminal, and this needs confirmation "
                            "(pass --yes to proceed unattended).")
        return False
    prompt = f"Type '{expect}' to confirm: " if expect else f"{question} [y/N] "
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == expect if expect else answer.lower() in ("y", "yes")
