from pathlib import Path

import pytest

from butler import config
from butler.errors import ConfigError


def parse(text: str, root: Path = Path("/proj")):
    import tomllib

    return config.parse(tomllib.loads(text), root)


def test_minimal():
    cfg = parse('[project]\nname = "demo"\n')
    assert cfg.project.name == "demo"
    assert cfg.dist_dir == Path("/proj/dist")
    assert cfg.app is None and cfg.server is None


def test_project_section_is_required():
    with pytest.raises(ConfigError, match="no \\[project\\] section"):
        parse("")


def test_unknown_key_is_an_error_not_a_silent_no_op():
    # The whole point of strict tables: a typo must not quietly do nothing.
    with pytest.raises(ConfigError, match="unknown key\\(s\\): dsit"):
        parse('[project]\nname = "demo"\ndsit = "out"\n')


def test_unknown_nested_key():
    with pytest.raises(ConfigError, match="app.android.*cleartxt"):
        parse('[project]\nname="d"\n[app]\n[app.android]\ncleartxt = true\n')


def test_wrong_type_says_which_type():
    with pytest.raises(ConfigError, match="'port' must be int"):
        parse('[project]\nname="d"\n[server]\nport = "8000"\n')


def test_app_defaults_derive_from_project_name():
    cfg = parse('[project]\nname = "demo"\n[app]\n[app.android]\n')
    assert cfg.app is not None and cfg.app.android is not None
    a = cfg.app.android
    assert a.key_name == "demo"
    assert a.env_prefix == "DEMO"
    assert a.dname == "CN=demo, OU=demo, O=demo, C=DE"
    assert cfg.app.dir == Path("/proj/app")
    assert cfg.app.tauri_dir == Path("/proj/app/src-tauri")


def test_planned_sections_are_recognised_not_rejected(monkeypatch):
    # Nothing is planned-but-unimplemented today; the mechanism is what's tested.
    monkeypatch.setitem(config.PLANNED_SECTIONS, "ios", "M9 — iOS bundles")
    cfg = parse('[project]\nname="d"\n[ios]\nteam = "ABC"\n')
    assert cfg.planned == {"ios": "M9 — iOS bundles"}


def test_unsupported_kind_is_named():
    with pytest.raises(ConfigError, match="kind 'flutter' is not supported"):
        parse('[project]\nname="d"\n[app]\nkind = "flutter"\n')


def test_deploy_backup_requires_a_backup_dir():
    with pytest.raises(ConfigError, match="needs 'backup_dir'"):
        parse('[project]\nname="d"\n[server]\n[server.deploy]\n'
              'host = "h"\ndir = "/srv/d"\n')


def test_sqlite_backup_requires_db_file():
    with pytest.raises(ConfigError, match="needs 'db_file'"):
        parse('[project]\nname="d"\n[server]\n[server.deploy]\n'
              'host = "h"\ndir = "/srv/d"\nbackup = "sqlite-file"\n'
              'backup_dir = "/backup"\n')


def test_db_file_falls_through_from_server_to_deploy():
    cfg = parse('[project]\nname="d"\n[server]\ndb_file = "d.db"\n'
                '[server.deploy]\nhost="h"\ndir="/srv/d"\n'
                'backup = "sqlite-file"\nbackup_dir = "/backup"\n')
    assert cfg.server and cfg.server.deploy
    assert cfg.server.deploy.db_file == "d.db"


def test_unknown_backup_strategy_lists_the_valid_ones():
    with pytest.raises(ConfigError, match="zip-data-dir, sqlite-file, none"):
        parse('[project]\nname="d"\n[server]\n[server.deploy]\n'
              'host="h"\ndir="/srv/d"\nbackup = "tarball"\nbackup_dir="/b"\n')


def test_find_root_walks_up(tmp_path):
    (tmp_path / "butler").mkdir()
    (tmp_path / "butler" / "butler.toml").write_text('[project]\nname="d"\n')
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert config.find_root(deep) == tmp_path


def test_find_root_reports_where_it_looked(tmp_path):
    with pytest.raises(ConfigError, match="no butler/butler.toml found"):
        config.find_root(tmp_path)
