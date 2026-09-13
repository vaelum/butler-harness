import tomllib
from pathlib import Path

import pytest

from butler import cli, command, config, tasks
from butler.command import Node, arg
from butler.errors import ButlerError

TOML = """
[project]
name = "demo"
[app]
[app.android]
[server]
db_file = "demo.db"
[server.deploy]
host = "user@host"
dir = "/srv/demo"
backup_dir = "/backup"
"""


def build(text: str = TOML, project_tasks=None):
    cfg = config.parse(tomllib.loads(text), Path("/proj"))
    roots = cli.build_tree(cfg, project_tasks or [])
    return cfg, roots, cli.build_parser(roots, cfg.project.name)


def test_tree_follows_the_config():
    _, roots, _ = build()
    names = [n.name for n in roots]
    assert names == sorted(names), "components are listed alphabetically"
    assert {"app", "server", "doctor"} <= set(names)

    app = command.find(roots, ["app"])
    assert app and {c.name for c in app.children} >= {"dev", "build", "icon", "android"}
    # No [app.install] section, so no install command.
    assert app.child("install") is None

    android = command.find(roots, ["app", "android"])
    assert android and {c.name for c in android.children} == {
        "init", "dev", "build", "keygen", "install"}


def test_components_absent_from_the_config_have_no_commands():
    _, roots, _ = build('[project]\nname = "demo"\n')
    assert [n.name for n in roots] == ["doctor"]


def test_server_commands_track_the_config():
    _, roots, _ = build()
    server = command.find(roots, ["server"])
    assert server and {c.name for c in server.children} == {
        "run", "dev", "down", "logs", "test", "deploy", "reset"}

    # compose = false drops the Compose-only commands, and reset with them.
    _, roots2, _ = build('[project]\nname="d"\n[server]\ncompose = false\n')
    server2 = command.find(roots2, ["server"])
    assert server2 and {c.name for c in server2.children} == {"run", "test"}


def test_global_flag_before_a_subcommand_survives():
    """Regression: parents= shares action objects, so set_defaults on the root
    used to give every subparser a concrete default that overwrote this."""
    _, _, parser = build()
    before = parser.parse_args(["-n", "app", "build"])
    after = parser.parse_args(["app", "build", "-n"])
    assert getattr(before, "dry_run", False) is True
    assert getattr(after, "dry_run", False) is True
    assert getattr(parser.parse_args(["app", "build"]), "dry_run", False) is False


def test_global_flag_survives_two_levels_of_nesting():
    _, _, parser = build()
    args = parser.parse_args(["-n", "app", "android", "build", "--debug"])
    assert getattr(args, "dry_run", False) is True
    assert args.debug is True


def test_planned_section_becomes_a_command_that_explains_itself(monkeypatch):
    # No section is planned-but-unimplemented today, so this stands one up.
    monkeypatch.setitem(config.PLANNED_SECTIONS, "ios", "M9 — iOS bundles")
    cfg, roots, parser = build('[project]\nname="d"\n[ios]\nteam="ABC"\n')
    node = command.find(roots, ["ios"])
    assert node is not None and node.func is not None
    with pytest.raises(ButlerError, match="not implemented yet"):
        node.func(None, None)


# --------------------------------------------------------------------------- #
# butler_tasks.py overlay
# --------------------------------------------------------------------------- #

def test_task_adds_a_new_command_under_an_existing_component():
    called = []
    t = tasks.Task(path=["server", "claim-token"], func=lambda ctx: called.append(1),
                   help="print the token")
    _, roots, parser = build(project_tasks=[t])
    node = command.find(roots, ["server", "claim-token"])
    assert node is not None and node.help == "print the token"
    args = parser.parse_args(["server", "claim-token"])
    assert args._func is t.func


def test_task_creates_missing_ancestors():
    t = tasks.Task(path=["node", "run"], func=lambda ctx: 0, help="dev node")
    _, roots, parser = build(project_tasks=[t])
    assert command.find(roots, ["node", "run"]) is not None
    assert parser.parse_args(["node", "run"])._func is t.func


def test_task_replaces_a_builtin():
    replacement = lambda ctx: 42  # noqa: E731
    t = tasks.Task(path=["app", "build"], func=replacement)
    _, roots, _ = build(project_tasks=[t])
    assert command.find(roots, ["app", "build"]).func is replacement


def test_wrapping_task_keeps_the_builtin_and_its_flags():
    order = []

    def inner_impl(ctx, args):
        order.append("inner")
        return 7

    roots = [Node("app", children=[Node("build", func=inner_impl,
                                        args=[arg("--debug", action="store_true")])])]

    def outer(ctx, inner):
        order.append("outer")
        return inner()

    cli.overlay(roots, [tasks.Task(path=["app", "build"], func=outer, wraps=True,
                                   args=[arg("--extra", action="store_true")])])
    node = command.find(roots, ["app", "build"])
    assert [a.flags for a in node.args] == [("--debug",), ("--extra",)]
    assert node.func(None, None) == 7
    assert order == ["outer", "inner"]


def test_wrapping_nothing_is_an_error():
    roots = [Node("app", children=[])]
    with pytest.raises(ButlerError, match="has nothing to wrap"):
        cli.overlay(roots, [tasks.Task(path=["app", "nope"], func=lambda ctx: 0,
                                       wraps=True)])


# --------------------------------------------------------------------------- #
# suggestions
# --------------------------------------------------------------------------- #

def test_suggest_close_component_name():
    _, roots, _ = build()
    assert cli.suggest("serve", roots) == "did you mean:  butler.py server"


def test_suggest_finds_an_action_used_as_a_component():
    """The migration aid for a project whose CLI put the action first."""
    _, roots, _ = build()
    assert cli.suggest("deploy", roots) == "did you mean:  butler.py server deploy"


def test_suggest_gives_up_quietly():
    _, roots, _ = build()
    assert cli.suggest("xyzzy", roots) is None


# --------------------------------------------------------------------------- #
# --version
# --------------------------------------------------------------------------- #

def test_version_reports_what_is_running(tmp_path, capsys):
    import butler

    (tmp_path / "butler").mkdir()
    (tmp_path / "butler" / "butler.toml").write_text('[project]\nname="demo"\n')
    (tmp_path / "butler.py").write_text('HARNESS_REF = "v9.9.9"\n')

    assert cli.main(["--version"], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"butler {butler.__version__}" in out
    assert "running from" in out
    # The pin the project asks for is reported alongside it — they can differ.
    assert "project pin   v9.9.9" in out


def test_version_works_outside_a_project(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--version"], root=None) == 0
    assert "butler" in capsys.readouterr().out


def test_version_flags_a_working_tree_override(tmp_path, capsys, monkeypatch):
    (tmp_path / "butler").mkdir()
    (tmp_path / "butler" / "butler.toml").write_text('[project]\nname="demo"\n')
    (tmp_path / "butler.py").write_text('HARNESS_REF = "v1.0.0"\n')
    monkeypatch.setenv("BUTLER_HARNESS_PATH", "/somewhere/butler")

    cli.main(["--version"], root=tmp_path)
    out = capsys.readouterr().out
    assert "working tree" in out and "ignored" in out
