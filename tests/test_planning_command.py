"""`butler.py planning …` runs the installed waterrise: find, check, exec.

No waterrise is needed: a fake one on a temp PATH answers `install --check`,
and the exec is recorded instead of run.
"""

import os
import sys
from pathlib import Path

import pytest

from butler import cli, planning
from butler.waterrise_pin import pinned


@pytest.fixture
def project(tmp_path):
    (tmp_path / "proj" / "butler").mkdir(parents=True)
    (tmp_path / "proj" / "butler" / "butler.toml").write_text(
        '[project]\nname = "demo"\n[planning]\ndir = "plans"\n')
    return tmp_path / "proj"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No real waterrise anywhere: an empty HOME, PATH only with the fakes."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("WATERRISE", raising=False)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_


def fake(bin_: Path, *, check_rc: int = 0, said: str = "") -> Path:
    exe = bin_ / "waterrise"
    said = said or f"waterrise {pinned()} satisfies {pinned()}"
    exe.write_text(f"""#!/bin/sh
if [ "$2" = install ] && [ "$3" = --check ]; then echo '{said}' >&2; exit {check_rc}; fi
exit 0
""")
    exe.chmod(0o755)
    return exe


@pytest.fixture
def execs(monkeypatch):
    seen = []

    def record(path, argv, env):
        seen.append((argv, env))
        return 0
    monkeypatch.setattr(os, "execvpe", record)
    return seen


def test_it_runs_the_installed_waterrise_with_the_same_arguments(project, isolated, execs):
    exe = fake(isolated)
    assert cli.main(["-n", "planning", "status", "p-abc123", "--json"], root=project) == 0
    (argv, env), = execs
    assert argv == [str(exe), "-n", "status", "p-abc123", "--json"]
    assert env["WATERRISE_PROJECT"] == str(project)
    assert env["WATERRISE_PLANNING_DIR"] == "plans"
    assert env["WATERRISE_PROJECT_NAME"] == "demo"
    assert env["WATERRISE_PROG"] == "python butler.py planning"


def test_help_goes_to_waterrise(project, isolated, execs):
    fake(isolated)
    assert cli.main(["planning", "--help"], root=project) == 0
    assert execs[0][0][1:] == ["--help"]


def test_without_waterrise_one_message_says_how_to_install_it(project, isolated, execs, capsys):
    assert cli.main(["planning", "check"], root=project) == 1
    err = capsys.readouterr().err
    assert f"waterrise is not installed; this harness needs {pinned()} or later" in err
    assert "python3 -m waterrise install" in err
    assert execs == []


def test_a_waterrise_the_check_refuses_is_not_run(project, isolated, execs, capsys):
    fake(isolated, check_rc=1, said=f"error: waterrise 2026.1.1 is installed, and this needs {pinned()} or later")
    assert cli.main(["planning", "check"], root=project) == 1
    err = capsys.readouterr().err
    assert f"waterrise 2026.1.1 is installed, and this needs {pinned()} or later" in err
    assert "git pull && python3 -m waterrise install" in err
    assert execs == []


def test_WATERRISE_names_an_executable_or_a_checkout(project, isolated, tmp_path, monkeypatch, execs):
    other = fake(tmp_path)
    monkeypatch.setenv("WATERRISE", str(other))
    assert planning.find() == [str(other)]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setenv("WATERRISE", str(checkout))
    assert planning.find()[:2] == [sys.executable, "-P"] and planning.find()[-1] == str(checkout)


def test_the_launcher_is_found_without_PATH(project, isolated, execs):
    launcher = Path(os.environ["HOME"]) / ".local" / "bin"
    launcher.mkdir(parents=True)
    exe = fake(launcher)
    assert planning.find() == [str(exe)]


def test_the_pin_is_a_calendar_version():
    import re
    assert re.fullmatch(r"\d{4}\.([1-9]|1[0-2])\.[1-9]\d*", pinned())
