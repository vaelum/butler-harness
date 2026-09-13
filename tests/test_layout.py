"""Where butler.toml and butler_tasks.py live.

Default is a butler/ subdirectory, so a project root gains one entry (the shim)
rather than three. The flat layout is still read so a project can move at its
own pace.
"""

import sys

import pytest

from butler import config, tasks
from butler.errors import ConfigError

TOML = '[project]\nname = "demo"\n'


def nested(tmp_path):
    (tmp_path / "butler").mkdir()
    (tmp_path / "butler" / "butler.toml").write_text(TOML)
    return tmp_path


def flat(tmp_path):
    (tmp_path / "butler.toml").write_text(TOML)
    return tmp_path


def test_nested_is_found(tmp_path):
    root = nested(tmp_path)
    assert config.config_path(root) == root / "butler" / "butler.toml"
    assert config.load(root).project.name == "demo"


def test_flat_still_works(tmp_path):
    root = flat(tmp_path)
    assert config.config_path(root) == root / "butler.toml"
    assert config.load(root).project.name == "demo"


def test_nested_wins_over_flat(tmp_path):
    root = nested(tmp_path)
    (root / "butler.toml").write_text('[project]\nname = "stale"\n')
    assert config.load(root).project.name == "demo"


def test_find_root_from_a_subdirectory(tmp_path):
    root = nested(tmp_path)
    deep = root / "server" / "app"
    deep.mkdir(parents=True)
    assert config.find_root(deep) == root


def test_find_root_from_inside_the_butler_directory(tmp_path):
    """The regression this layout invites: butler/butler.toml looks exactly like
    a flat project rooted at butler/. It must resolve to the project above."""
    root = nested(tmp_path)
    assert config.find_root(root / "butler") == root


def test_find_root_error_names_the_expected_location(tmp_path):
    with pytest.raises(ConfigError, match="no butler/butler.toml found"):
        config.find_root(tmp_path)


def test_tasks_are_loaded_from_the_butler_directory(tmp_path):
    root = nested(tmp_path)
    (root / "butler" / "butler_tasks.py").write_text(
        "from butler import task\n\n\n"
        "@task('smoke', help='from the nested layout')\n"
        "def smoke(ctx):\n"
        "    return 0\n")
    tasks.clear()
    loaded = tasks.load(root)
    assert [t.path for t in loaded] == [["smoke"]]
    assert loaded[0].help == "from the nested layout"


def test_tasks_from_the_flat_layout_still_load(tmp_path):
    root = flat(tmp_path)
    (root / "butler_tasks.py").write_text(
        "from butler import task\n\n\n@task('smoke')\ndef smoke(ctx):\n    return 0\n")
    tasks.clear()
    assert [t.path for t in tasks.load(root)] == [["smoke"]]


def test_project_root_is_appended_to_sys_path_not_prepended(tmp_path, monkeypatch):
    """A directory called butler/ at the project root would shadow this very
    package if the root were prepended — `from butler import task` inside the
    tasks file would import the project's config folder instead."""
    root = nested(tmp_path)
    (root / "butler" / "butler_tasks.py").write_text(
        "from butler import task\n\n\n@task('smoke')\ndef smoke(ctx):\n    return 0\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    tasks.clear()
    tasks.load(root)   # would raise ImportError if the folder shadowed the package
    assert sys.path[-1] == str(root)


def test_no_tasks_file_is_fine(tmp_path):
    tasks.clear()
    assert tasks.load(nested(tmp_path)) == []
