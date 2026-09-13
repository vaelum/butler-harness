"""`server deploy` — the ssh/rsync sequence, and the things projects differ on.

Nothing here talks to a host: `proc.feed` (the ssh channel) and `proc.run` (the
rsync) are recorded, so what the tests assert on is the script butler WOULD run
and the order it would run it in. That order is the whole design — stop before
snapshot, snapshot before overwrite, preflight before either — so it is worth
pinning down.
"""

import tomllib
from pathlib import Path

import pytest

from butler import config, proc
from butler.components import deploy
from butler.context import Ctx
from butler.errors import ButlerError, ConfigError

BASE = """
[project]
name = "demo"
[server]
dir = "server"
[server.deploy]
host = "user@host"
dir = "~/demo"
backup_dir = "~/backups"
"""


def parse(text: str = BASE, root: Path = Path("/proj")):
    return config.parse(tomllib.loads(text), root)


class Recorder:
    """Stands in for ssh and rsync, and remembers what it was asked to do."""

    def __init__(self, monkeypatch, *, first_deploy: bool = False):
        self.steps: list[tuple[str, str]] = []
        self.first = first_deploy
        monkeypatch.setattr(proc, "feed", self.feed)
        monkeypatch.setattr(proc, "run", self.run)

    def feed(self, cmd, stdin_text, cwd=None, **kw):
        self.steps.append(("ssh", stdin_text))
        # The data-dir probe: 1 means "absent", i.e. a first deploy.
        if "test -d" in stdin_text:
            return 1 if self.first else 0
        return 0

    def run(self, cmd, cwd=None, **kw):
        self.steps.append(("rsync", " ".join(str(c) for c in cmd)))
        return 0

    @property
    def kinds(self) -> list[str]:
        """One word per step, in order — the sequence under test."""
        out = []
        for kind, text in self.steps:
            if kind == "rsync":
                out.append("rsync")
            elif "test -d" in text:
                out.append("probe")
            elif "command -v zip" in text and "compose" not in text:
                out.append("preflight")
            elif "mkdir -p" in text and "zip -rq" not in text and "compose" not in text:
                out.append("mkdir-data")
            elif "compose build" in text:
                out.append("build")
            elif "down" in text:
                out.append("stop")
            elif "zip -rq" in text or "cp -a" in text:
                out.append("backup")
            elif "up -d" in text:
                out.append("start")
            else:
                out.append("?")
        return out

    def script(self, step: str) -> str:
        """What the named step actually sent — the ssh script, or the rsync argv."""
        return next(text for kind, (_, text) in zip(self.kinds, self.steps)
                    if kind == step)


def run_deploy(monkeypatch, text: str = BASE, *, first: bool = False,
               root: Path = Path("/proj")) -> Recorder:
    rec = Recorder(monkeypatch, first_deploy=first)
    cfg = parse(text, root)
    assert deploy.deploy(Ctx(cfg=cfg), cfg.server) == 0
    return rec


# --------------------------------------------------------------------------- #
# the sequence
# --------------------------------------------------------------------------- #

def test_the_default_order_stops_before_it_overwrites(monkeypatch):
    rec = run_deploy(monkeypatch)
    assert rec.kinds == ["probe", "stop", "backup", "rsync", "mkdir-data", "start"]
    # The backup tool is checked while the service is still up.
    assert "command -v zip" in rec.script("stop")


def test_build_first_builds_while_the_old_stack_is_still_serving(monkeypatch):
    rec = run_deploy(monkeypatch, BASE + "build_first = true\n")
    assert rec.kinds == ["probe", "rsync", "mkdir-data", "preflight", "build",
                         "stop", "backup", "start"]
    # Already built above, so the swap must not build again — that would put the
    # slow part back inside the downtime window.
    assert "--build" not in rec.script("start")


def test_a_first_deploy_skips_the_backup_and_its_preflight(monkeypatch):
    rec = run_deploy(monkeypatch, first=True)
    assert rec.kinds == ["probe", "stop", "rsync", "mkdir-data", "start"]
    assert "command -v zip" not in rec.script("stop")


def test_an_unreachable_host_is_not_a_first_deploy(monkeypatch):
    """rc 1 is "no data dir"; anything above it is ssh failing, and treating
    that as a clean first deploy would silently skip the backup."""
    rec = Recorder(monkeypatch)
    monkeypatch.setattr(proc, "feed", lambda *a, **kw: 255)
    cfg = parse()
    with pytest.raises(ButlerError, match="could not reach the deploy host"):
        deploy.deploy(Ctx(cfg=cfg), cfg.server)


# --------------------------------------------------------------------------- #
# host_file — the ssh target kept out of a public repo
# --------------------------------------------------------------------------- #

HOST_FILE = BASE.replace('host = "user@host"', 'host_file = ".deploy-target"')


def test_the_host_can_come_from_a_gitignored_file(tmp_path, monkeypatch):
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / ".deploy-target").write_text("deploy@example.com\n")
    rec = run_deploy(monkeypatch, HOST_FILE, root=tmp_path)
    assert "deploy@example.com:~/demo/" in rec.script("rsync")
    # Local-only, and never the host's business.
    assert "--exclude=.deploy-target" in rec.script("rsync")


def test_a_missing_host_file_says_how_to_make_one(tmp_path):
    cfg = parse(HOST_FILE, tmp_path)
    with pytest.raises(ButlerError, match="not found") as e:
        _ = cfg.server.deploy.ssh_host
    assert "echo 'user@example.com' >" in e.value.hint


def test_an_empty_host_file_is_an_error(tmp_path):
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / ".deploy-target").write_text("   \n")
    cfg = parse(HOST_FILE, tmp_path)
    with pytest.raises(ButlerError, match="is empty"):
        _ = cfg.server.deploy.ssh_host


def test_the_host_is_not_resolved_at_parse_time(tmp_path):
    """--help and doctor must work in a clone that has no deploy target yet."""
    assert parse(HOST_FILE, tmp_path).server.deploy.host_file.name == ".deploy-target"


def test_host_and_host_file_are_exclusive():
    with pytest.raises(ConfigError, match="exactly one of"):
        parse(BASE + 'host_file = ".deploy-target"\n')
    with pytest.raises(ConfigError, match="exactly one of"):
        parse(BASE.replace('host = "user@host"\n', ""))


# --------------------------------------------------------------------------- #
# layouts that differ per project
# --------------------------------------------------------------------------- #

def test_a_compose_file_off_the_root_is_used_by_every_step(monkeypatch):
    rec = run_deploy(monkeypatch, BASE + 'compose_file = "docker/docker-compose.yml"\n')
    for step in ("stop", "start"):
        assert "docker compose -f docker/docker-compose.yml" in rec.script(step)
    # And the "is there anything to stop?" test looks for that file, not the
    # four default names.
    assert "[ -f ~/demo/docker/docker-compose.yml ]" in rec.script("stop")


def test_compose_file_falls_through_from_server_to_deploy():
    text = BASE.replace("[server]\n", '[server]\ncompose_file = "docker/compose.yml"\n')
    assert parse(text).server.deploy.compose_file == "docker/compose.yml"


def test_a_data_dir_outside_the_deploy_dir_is_probed_and_zipped_where_it_is(monkeypatch):
    rec = run_deploy(monkeypatch, BASE + 'data_dir = "~/.demo-data"\n')
    assert "test -d ~/.demo-data" in rec.script("probe")
    assert "data=~/.demo-data" in rec.script("backup")
    # Zipped from the parent, so the archive still unpacks over the original.
    assert 'cd "$(dirname "$data")" && zip -rq' in rec.script("backup")
    assert "mkdir -p ~/.demo-data" in rec.script("mkdir-data")


def test_a_relative_data_dir_stays_inside_the_deploy_dir(monkeypatch):
    rec = run_deploy(monkeypatch)
    assert "test -d ~/demo/data" in rec.script("probe")


def test_build_env_forwards_only_what_is_set_locally(monkeypatch):
    monkeypatch.setenv("APT_MIRROR", "mirror.example.com")
    monkeypatch.delenv("UNSET_KNOB", raising=False)
    rec = run_deploy(monkeypatch,
                     BASE + 'build_first = true\nbuild_env = ["APT_MIRROR", "UNSET_KNOB"]\n')
    assert "APT_MIRROR=mirror.example.com docker compose build" in rec.script("build")
    assert "UNSET_KNOB" not in rec.script("build")


def test_build_env_must_be_names():
    with pytest.raises(ConfigError, match="build_env"):
        parse(BASE + "build_env = [3]\n")
