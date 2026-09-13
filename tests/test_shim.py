"""The generated bootstrap shim.

The regression these guard against was a genuine infinite loop: the shim is
called butler.py and lives in the project root, and `python -m butler` puts that
root on sys.path ahead of site-packages. So `import butler` found the SHIM
rather than the installed package, which re-exec'd the shim, forever. Every
project using the bootstrap path (rather than BUTLER_HARNESS_PATH) hung.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from butler.new import main as new_main

MARKER = "STUB-HARNESS-RAN"


def scaffold(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    new_main([str(root), "--name", "proj"])
    return root


def stub_venv(cache_home: Path, ref: str, butler_body: str) -> Path:
    """A venv-shaped directory whose `butler` console script is a stub, but
    whose `python` is the REAL interpreter.

    That last part is what gives these tests teeth: if the shim falls back to
    `python -m butler` from the project directory, a real interpreter re-imports
    butler.py and the loop reappears. A stub python would hide it.
    """
    venv = cache_home / "butler" / "venvs" / ref
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python"
    # Echo the argv it was handed, then become the real interpreter.
    py.write_text(f'#!/bin/sh\necho "ARGV: $@" >&2\nexec {sys.executable} "$@"\n')
    py.chmod(0o755)
    entry = bindir / "butler"
    entry.write_text(butler_body)
    entry.chmod(0o755)
    return venv


def run_shim(root: Path, cache_home: Path, *args: str, cwd: Path | None = None):
    env = {**os.environ, "XDG_CACHE_HOME": str(cache_home)}
    env.pop("BUTLER_HARNESS_PATH", None)
    # The harness must not be importable by accident, or the fallback test
    # can't tell whether -P did its job.
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, str(root / "butler.py"), *args],
                          cwd=str(cwd or root), capture_output=True, text=True,
                          env=env, timeout=60)


def ref_of(root: Path) -> str:
    for line in (root / "butler.py").read_text().splitlines():
        if line.startswith("HARNESS_REF"):
            return line.split('"')[1]
    raise AssertionError("no HARNESS_REF in the generated shim")


def test_hands_off_to_the_console_script_not_back_to_itself(tmp_path):
    """If this regresses, the shim re-execs itself and the test times out."""
    root = scaffold(tmp_path)
    cache = tmp_path / "cache"
    stub_venv(cache, ref_of(root),
              f'#!/bin/sh\necho "{MARKER} $@"\n')

    r = run_shim(root, cache, "app", "dev")
    assert MARKER in r.stdout, r.stderr
    # The arguments are forwarded verbatim.
    assert r.stdout.strip().endswith("app dev")


def test_project_root_is_exported_so_cwd_does_not_matter(tmp_path):
    root = scaffold(tmp_path)
    cache = tmp_path / "cache"
    stub_venv(cache, ref_of(root),
              '#!/bin/sh\necho "ROOT=$BUTLER_PROJECT_ROOT"\n')

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = run_shim(root, cache, "doctor", cwd=elsewhere)
    assert f"ROOT={root}" in r.stdout, r.stderr


def test_falls_back_to_dash_P_when_there_is_no_console_script(tmp_path):
    """-P keeps the project root off sys.path, closing the same hole.

    Asserted on the argv the shim hands over, not on the outcome: whether
    `butler` is importable in that interpreter differs between a developer
    machine and CI (where the test venv has it installed), and the mechanism is
    what matters. The stub's python is still the real interpreter, so dropping
    -P would re-enter butler.py and hang rather than fail.
    """
    root = scaffold(tmp_path)
    cache = tmp_path / "cache"
    venv = stub_venv(cache, ref_of(root), "unused")
    (venv / "bin" / "butler").unlink()

    r = run_shim(root, cache, "doctor")
    assert "ARGV: -P -m butler doctor" in r.stderr, (r.stdout, r.stderr)


def test_harness_path_bypasses_the_venv_entirely(tmp_path):
    """The development route: run a working tree, no install, no cache."""
    root = scaffold(tmp_path)
    env = {**os.environ,
           "BUTLER_HARNESS_PATH": str(Path(__file__).resolve().parents[1]),
           "XDG_CACHE_HOME": str(tmp_path / "unused-cache")}
    r = subprocess.run([sys.executable, str(root / "butler.py"), "--help"],
                       cwd=str(root), capture_output=True, text=True, env=env,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    assert "proj butler" in r.stdout
    assert not (tmp_path / "unused-cache").exists()


def test_bootstrap_is_skipped_when_the_venv_is_already_there(tmp_path):
    root = scaffold(tmp_path)
    cache = tmp_path / "cache"
    stub_venv(cache, ref_of(root), f'#!/bin/sh\necho "{MARKER}"\n')

    r = run_shim(root, cache, "doctor")
    assert "preparing the harness" not in r.stderr
    assert MARKER in r.stdout
