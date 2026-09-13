"""git_deps: the checkout follows butler.toml, not the remote it was cloned with."""

from __future__ import annotations

import subprocess
from pathlib import Path

from butler import vcs


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _bare_repo_with_commit(root: Path, name: str, marker: str) -> Path:
    """A bare repo whose `main` holds one file, so two of them are telling
    apart by content and not just by path."""
    work = root / f"{name}-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.invalid", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "marker").write_text(marker)
    _git("add", "marker", cwd=work)
    _git("commit", "-q", "-m", marker, cwd=work)
    bare = root / f"{name}.git"
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=root)
    return bare


def test_existing_checkout_is_repointed_to_the_toml_url(tmp_path, capsys):
    old = _bare_repo_with_commit(tmp_path, "old", "old host")
    new = _bare_repo_with_commit(tmp_path, "new", "new host")
    project = tmp_path / "project"
    project.mkdir()
    # The checkout was made when the dependency lived at `old` …
    _git("clone", "-q", str(old), "deps/lib", cwd=project)
    # … and butler.toml has since moved it to `new`.
    vcs.ensure_git_deps(project, [{"path": "deps/lib", "url": str(new), "ref": "origin/main"}])

    dest = project / "deps" / "lib"
    assert _git("remote", "get-url", "origin", cwd=dest) == str(new)
    assert (dest / "marker").read_text() == "new host"
    assert "Repointing deps/lib origin" in capsys.readouterr().out


def test_matching_origin_is_left_alone(tmp_path, capsys):
    repo = _bare_repo_with_commit(tmp_path, "only", "the one")
    project = tmp_path / "project"
    project.mkdir()
    _git("clone", "-q", str(repo), "deps/lib", cwd=project)
    vcs.ensure_git_deps(project, [{"path": "deps/lib", "url": str(repo), "ref": "origin/main"}])

    assert "Repointing" not in capsys.readouterr().out
    assert (project / "deps" / "lib" / "marker").read_text() == "the one"
