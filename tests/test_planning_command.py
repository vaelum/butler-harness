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


# --------------------------------------------------------------------------- #
# planning install
# --------------------------------------------------------------------------- #

FAKE_MAIN = '''\
"""A waterrise checkout's installer, as `planning install` runs it: it records
its arguments and puts a launcher that passes the pin check where the real one
would."""
import os
import sys
from pathlib import Path


def main():
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "installed.txt").write_text(" ".join(sys.argv[1:]))
    bin_ = home / ".local" / "bin"
    bin_.mkdir(parents=True, exist_ok=True)
    exe = bin_ / "waterrise"
    exe.write_text("#!/bin/sh\\n"
                   "if [ \\"$2\\" = install ] && [ \\"$3\\" = --check ]; then "
                   "echo 'waterrise PIN satisfies PIN' >&2; exit 0; fi\\nexit 0\\n".replace("PIN", "%s"))
    exe.chmod(0o755)
    return 0


if __name__ == "__main__":
    sys.exit(main())
''' % (pinned(),)


def git(*args, cwd):
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """A repository standing in for github.com/vaelum/waterrise: one commit, holding a
    fake package whose installer is what the real one's is to the harness; tagged
    at the pin unless a test retags it. The cache is the test's own."""
    repo = tmp_path / "upstream"
    (repo / "waterrise").mkdir(parents=True)
    (repo / "waterrise" / "__init__.py").write_text("")
    (repo / "waterrise" / "__main__.py").write_text(FAKE_MAIN)
    git("init", "--quiet", "--initial-branch=main", cwd=repo)
    git("add", ".", cwd=repo)
    git("commit", "--quiet", "-m", "waterrise", cwd=repo)
    git("tag", f"v{pinned()}", cwd=repo)
    monkeypatch.setattr(planning, "GIT", str(repo))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return repo


def installed(args_file_home: str) -> str | None:
    path = Path(args_file_home) / "installed.txt"
    return path.read_text() if path.exists() else None


def test_install_clones_the_pin_and_runs_its_installer(project, isolated, upstream, execs, capsys):
    assert cli.main(["planning", "install"], root=project) == 0
    clone = Path(os.environ["XDG_CACHE_HOME"]) / "butler" / "waterrise"
    assert (clone / ".git").is_dir()
    import subprocess
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True).stdout
    tagged = subprocess.run(["git", "rev-parse", f"v{pinned()}^{{commit}}"], cwd=upstream,
                            capture_output=True, text=True).stdout
    assert head == tagged
    assert installed(os.environ["HOME"]) == "install"
    assert f"waterrise {pinned()} satisfies {pinned()}" in capsys.readouterr().out
    assert execs == []


def test_install_passes_boot_on(project, isolated, upstream, execs):
    assert cli.main(["planning", "install", "--boot"], root=project) == 0
    assert installed(os.environ["HOME"]) == "install --boot"


def test_a_second_install_fetches_the_clone_it_has(project, isolated, upstream, execs):
    assert cli.main(["planning", "install"], root=project) == 0
    clone = Path(os.environ["XDG_CACHE_HOME"]) / "butler" / "waterrise"
    (clone / "still-here").write_text("")
    assert cli.main(["planning", "install"], root=project) == 0
    assert (clone / "still-here").exists()


def test_install_refuses_a_pin_the_mirror_has_no_tag_for(project, isolated, upstream, execs, capsys):
    git("tag", "-d", f"v{pinned()}", cwd=upstream)
    git("tag", "v1999.1.1", cwd=upstream)
    assert cli.main(["planning", "install"], root=project) == 1
    assert f"has no tag v{pinned()}" in capsys.readouterr().err
    assert installed(os.environ["HOME"]) is None


def test_install_dry_run_changes_nothing(project, isolated, upstream, execs, capsys):
    assert cli.main(["-n", "planning", "install"], root=project) == 0
    out = capsys.readouterr().out
    assert "git clone" in out and "-m waterrise install" in out
    assert not (Path(os.environ["XDG_CACHE_HOME"]) / "butler").exists()
    assert installed(os.environ["HOME"]) is None


def test_install_help_is_the_harness_own(project, isolated, execs, capsys):
    assert cli.main(["planning", "install", "--help"], root=project) == 0
    out = capsys.readouterr().out
    assert "--boot" in out and pinned() in out
    assert execs == []


def test_install_leaves_waterrises_own_switches_to_waterrise(project, isolated, execs, capsys):
    assert cli.main(["planning", "install", "--uninstall"], root=project) == 2
    assert "unrecognized arguments: --uninstall" in capsys.readouterr().err
    assert execs == []


def test_the_hints_name_planning_install(project, isolated, execs, capsys):
    assert cli.main(["planning", "check"], root=project) == 1
    assert "python butler.py planning install" in capsys.readouterr().err
    found, said = planning.status()
    assert found is None and "python butler.py planning install" in said
