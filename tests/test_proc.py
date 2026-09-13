import sys
from pathlib import Path

import pytest

from butler import paths, proc, vcs
from butler.errors import ButlerError


def test_missing_executable_is_127_not_a_traceback():
    assert proc.run(["definitely-not-a-real-binary"], echo=False) == 127


def test_check_raises_with_the_exit_code():
    with pytest.raises(ButlerError) as e:
        proc.check([sys.executable, "-c", "raise SystemExit(3)"], echo=False)
    assert e.value.code == 3


def test_dry_run_does_not_execute(tmp_path):
    marker = tmp_path / "ran"
    rc = proc.run([sys.executable, "-c", f"open({str(marker)!r},'w')"],
                  echo=False, dry_run=True)
    assert rc == 0 and not marker.exists()


def test_extra_env_merges_rather_than_replaces(monkeypatch):
    monkeypatch.setenv("KEEP_ME", "yes")
    r = proc.capture([sys.executable, "-c",
                      "import os;print(os.environ.get('KEEP_ME'), os.environ.get('ADDED'))"],
                     env={"ADDED": "1"})
    assert r.out.strip() == "yes 1"


def test_capture_reports_failure_without_raising():
    r = proc.capture([sys.executable, "-c", "import sys;sys.stderr.write('nope');raise SystemExit(2)"])
    assert r.rc == 2 and not r.ok and "nope" in r.combined


def test_tool_version_parses_a_real_tool():
    version = proc.tool_version(sys.executable)
    assert version is not None and version[:2] == sys.version_info[:2]


def test_tool_version_is_none_for_a_missing_tool():
    assert proc.tool_version("definitely-not-a-real-binary") is None


def test_which_checks_extra_paths_first(tmp_path):
    tool = tmp_path / "faketool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    assert proc.which("faketool", [tmp_path]) == str(tool)
    assert proc.which("faketool") is None


def test_confirm_is_no_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert proc.confirm("really?") is False
    assert proc.confirm("really?", assume_yes=True) is True


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def test_disp_is_relative_inside_and_absolute_outside(tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    assert paths.disp(root / "app" / "x.txt", root) == "app/x.txt"
    outside = tmp_path / "vault" / "k.jks"
    assert paths.disp(outside, root) == str(outside)


def test_collect_only_takes_installers(tmp_path):
    src, dest = tmp_path / "bundle", tmp_path / "dist"
    (src / "deb").mkdir(parents=True)
    for name in ("app.deb", "app.AppImage", "app.txt", "libfoo.so"):
        (src / "deb" / name).write_bytes(b"x")
    got = paths.collect_artifacts([src], dest, tmp_path)
    assert sorted(p.name for p in got) == ["app.AppImage", "app.deb"]


def test_collect_tolerates_a_missing_source(tmp_path):
    assert paths.collect_artifacts([tmp_path / "nope"], tmp_path / "dist", tmp_path) == []


def test_clear_dir_can_keep_entries(tmp_path):
    d = tmp_path / "results"
    (d / "sub").mkdir(parents=True)
    (d / "keep.json").write_text("{}")
    (d / "drop.log").write_text("x")
    paths.clear_dir(d, keep=["keep.json"])
    assert [p.name for p in d.iterdir()] == ["keep.json"]


# --------------------------------------------------------------------------- #
# vcs
# --------------------------------------------------------------------------- #

def test_release_label_falls_back_to_a_utc_stamp(tmp_path):
    label = vcs.release_label(tmp_path)   # not a git repo
    assert len(label) == 15 and label[8] == "-"


def test_update_submodules_is_a_no_op_outside_git(tmp_path):
    vcs.update_submodules(tmp_path)   # must not raise
