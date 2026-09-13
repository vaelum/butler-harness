"""--dry-run must be honest.

`run` handles it for subprocess calls, but butler also mutates the filesystem
itself — deleting a database, writing a .desktop file, copying icons into the
gen tree. Those need an explicit guard, and these tests are what keeps one from
being forgotten.
"""

import tomllib
from pathlib import Path

import pytest

from butler import config
from butler.errors import ButlerError
from butler.components import server as server_component
from butler.components import tauri
from butler.context import Ctx

TOML = """
[project]
name = "demo"
[app]
[app.install]
name = "Demo"
programs_dir = "%%PROGRAMS%%"
[server]
db_file = "demo.db"
"""


def make_ctx(tmp_path: Path, dry_run: bool) -> Ctx:
    text = TOML.replace("%%PROGRAMS%%", str(tmp_path / "programs"))
    cfg = config.parse(tomllib.loads(text), tmp_path)
    return Ctx(cfg=cfg, dry_run=dry_run, assume_yes=True)


class Args:
    pass


def test_server_reset_keeps_the_database_under_dry_run(tmp_path):
    data = tmp_path / "server" / "data"
    data.mkdir(parents=True)
    for name in ("demo.db", "demo.db-wal", "demo.db-shm"):
        (data / name).write_bytes(b"x")

    assert server_component.reset(make_ctx(tmp_path, dry_run=True), Args()) == 0
    assert len(list(data.iterdir())) == 3, "dry-run must not delete anything"

    server_component.reset(make_ctx(tmp_path, dry_run=False), Args())
    assert list(data.iterdir()) == []


def test_app_install_writes_nothing_under_dry_run(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "Demo.AppImage").write_bytes(b"x")
    icons = tmp_path / "app" / "src-tauri" / "icons"
    icons.mkdir(parents=True)
    (icons / "icon.png").write_bytes(b"x")

    assert tauri.install(make_ctx(tmp_path, dry_run=True), Args()) == 0
    assert not (tmp_path / "programs").exists()


def test_icon_does_not_demand_the_generators_output_under_dry_run(tmp_path, capsys):
    """The generator is what writes app-icon.png. Under --dry-run it did not
    run, so requiring its output reports a problem the flag itself created."""
    text = TOML.replace("%%PROGRAMS%%", str(tmp_path / "programs"))
    text = text.replace("[app]", '[app]\nicon_generator = "gen_icon.py"')
    cfg = config.parse(tomllib.loads(text), tmp_path)
    (tmp_path / "app").mkdir()
    # A generator that succeeds without producing anything, so the only thing
    # under test is what happens to the missing output.
    (tmp_path / "app" / "gen_icon.py").write_text("")

    assert tauri.icon(Ctx(cfg=cfg, dry_run=True), Args()) == 0
    assert "cargo tauri icon" in capsys.readouterr().out

    # Without the flag it is a real, reportable problem again.
    with pytest.raises(ButlerError, match="no icon source"):
        tauri.icon(Ctx(cfg=cfg, dry_run=False), Args())


def test_would_reports_and_gates(tmp_path, capsys):
    dry = make_ctx(tmp_path, dry_run=True)
    assert dry.would("delete everything") is True
    assert "would delete everything" in capsys.readouterr().out
    assert make_ctx(tmp_path, dry_run=False).would("delete everything") is False
