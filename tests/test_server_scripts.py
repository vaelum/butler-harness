"""[server.scripts] — delegating an action to a script the project already has.

Discovered at M2: chords' server is driven by scripts/build.sh, deploy.sh and
backup.sh, not by compose-and-rsync. Re-expressing those in config would be a
rewrite rather than a migration.
"""

import tomllib
from pathlib import Path

import pytest

from butler import cli, command, config
from butler.errors import ConfigError

SCRIPTED = """
[project]
name = "chords"
[server]
dir = "server"
compose = false
[server.scripts]
dev    = ["bash", "scripts/build.sh", "dev"]
build  = ["bash", "scripts/build.sh"]
backup = { cmd = ["bash", "scripts/backup.sh"], help = "back up the local data dir" }
"""


def parse(text: str = SCRIPTED):
    return config.parse(tomllib.loads(text), Path("/proj"))


def test_scripts_become_commands():
    cfg = parse()
    roots = cli.build_tree(cfg, [])
    server = command.find(roots, ["server"])
    assert {c.name for c in server.children} == {"run", "test", "dev", "build", "backup"}


def test_help_is_derived_or_given():
    cfg = parse()
    roots = cli.build_tree(cfg, [])
    assert command.find(roots, ["server", "dev"]).help == "run bash scripts/build.sh dev"
    assert command.find(roots, ["server", "backup"]).help == "back up the local data dir"


def test_a_script_replaces_a_same_named_builtin():
    # compose = true would give `dev` a Compose implementation; an explicit
    # script wins, because the project said how to do it on purpose.
    cfg = parse(SCRIPTED.replace("compose = false", "compose = true"))
    roots = cli.build_tree(cfg, [])
    dev = command.find(roots, ["server", "dev"])
    assert dev.help == "run bash scripts/build.sh dev"
    # ...and the other Compose commands are still there.
    assert command.find(roots, ["server", "down"]) is not None


def test_script_runs_in_the_server_directory(tmp_path):
    (tmp_path / "server").mkdir()
    cfg = config.parse(tomllib.loads(
        '[project]\nname="c"\n[server]\ncompose = false\n'
        '[server.scripts]\nwhere = ["pwd"]\n'), tmp_path)
    roots = cli.build_tree(cfg, [])
    from butler.context import Ctx

    ctx = Ctx(cfg=cfg, dry_run=True)
    assert command.find(roots, ["server", "where"]).func(ctx, None) == 0


def test_bad_script_value_is_rejected():
    with pytest.raises(ConfigError, match="must be an array of strings"):
        parse('[project]\nname="c"\n[server]\n[server.scripts]\ndev = "bash x.sh"\n')


def test_empty_script_is_rejected():
    with pytest.raises(ConfigError, match="non-empty array"):
        parse('[project]\nname="c"\n[server]\n[server.scripts]\ndev = []\n')


def test_script_table_needs_cmd():
    with pytest.raises(ConfigError, match="missing the required key 'cmd'"):
        parse('[project]\nname="c"\n[server]\n[server.scripts]\n'
              'dev = { help = "no command" }\n')


def test_a_task_can_still_wrap_a_scripted_action():
    """chords wraps `server deploy` to ship the API key before deploying."""
    from butler import tasks

    cfg = parse(SCRIPTED.replace(
        "backup = ", "deploy = [\"bash\", \"scripts/deploy.sh\"]\nbackup = "))
    order = []

    def outer(ctx, inner):
        order.append("key")
        return inner()

    roots = cli.build_tree(cfg, [tasks.Task(path=["server", "deploy"], func=outer,
                                            wraps=True)])
    from butler.context import Ctx

    ctx = Ctx(cfg=cfg, dry_run=True)
    assert command.find(roots, ["server", "deploy"]).func(ctx, None) == 0
    assert order == ["key"]
