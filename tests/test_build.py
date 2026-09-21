"""The `build` and `check` components.

The commands here are long and order-sensitive (configure, then build, then a
ctest whose JUnit path CI reads), and they wrap services that must come down
again. So most of these tests assert on what --dry-run *echoes*: that is the
same string the shell would get.
"""

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from butler import cli, config
from butler.components import cmake as cmake_component
from butler.components import lint as lint_component
from butler.context import Ctx
from butler.errors import ButlerError, ConfigError

TOML = """
[project]
name = "demo"
submodules = true

[build]
kind = "cmake"
presets = ["release", "debug"]
build_dir = "builds/{preset}"
cmake_min = "3.22"
jobs = 4

[[build.option]]
name = "storage"
cmake = ["DEMO_WITH_SFTP", "DEMO_WITH_S3"]
default = true
help = "the SFTP/S3 clients"

[build.docker]
dockerfile = "building/buildenv.Dockerfile"
tag = "demo-buildenv"
workdir = "/demo"

[build.test_env]
up = ["bash", "test/start-test-env.sh"]
down = ["docker", "compose", "-f", "test/docker-compose.yml", "down"]
env = { DEMO_TEST_INTEGRATION = "1" }
needs = "storage"
docker_args = ["--network", "host"]

[check.lint]
config = "test/linting/.mega-linter.yml"
image = "demo-lint-env"
dockerfile = "building/lint/megalinter.Dockerfile"
hide = ["deps", "builds"]

[check.tidy]
driver = "test/clang-tidy.py"
preset = "release"
"""


def parse(text: str = TOML, root: Path = Path("/proj")) -> config.Config:
    return config.parse(tomllib.loads(text), root)


def make_ctx(tmp_path: Path) -> Ctx:
    (tmp_path / "building" / "lint").mkdir(parents=True)
    (tmp_path / "building" / "buildenv.Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "building" / "lint" / "megalinter.Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "test" / "linting").mkdir(parents=True)
    (tmp_path / "test" / "linting" / ".mega-linter.yml").write_text("{}\n")
    (tmp_path / "test" / "clang-tidy.py").write_text("")
    return Ctx(cfg=parse(root=tmp_path), dry_run=True, assume_yes=True)


def build_args(**kw) -> SimpleNamespace:
    base = dict(preset=None, test=False, test_filter=None, docker=False, opt_storage=True)
    return SimpleNamespace(**{**base, **kw})


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def test_build_parses():
    cfg = parse().build
    assert cfg is not None
    assert cfg.default_preset == "release"
    assert cfg.preset_dir("debug") == Path("/proj/builds/debug")
    assert cfg.cmake_min == (3, 22) and cfg.jobs == 4
    assert cfg.docker is not None
    env = cfg.docker.for_preset("release", Path("/proj"))
    assert env.context == Path("/proj/building") and env.tag == "demo-buildenv"


def test_option_flag_polarity_follows_the_default():
    on = config.BuildOption(name="storage", cmake=["A"], default=True)
    off = config.BuildOption(name="lto", cmake=["B"], default=False)
    assert on.flag == "--no-storage" and off.flag == "--lto"
    assert on.defines(True) == ["-DA=ON"] and on.defines(False) == ["-DA=OFF"]


def test_build_dir_must_be_per_preset():
    with pytest.raises(ConfigError, match="build_dir must contain"):
        parse('[project]\nname="d"\n[build]\nbuild_dir = "build"\n')


def test_test_env_needs_an_option_that_exists():
    with pytest.raises(ConfigError, match="not a declared"):
        parse('[project]\nname="d"\n[build]\n[build.test_env]\n'
              'up = ["true"]\nneeds = "storage"\n')


def test_check_must_declare_something():
    with pytest.raises(ConfigError, match="neither"):
        parse('[project]\nname="d"\n[check]\n')


def test_unsupported_build_kind_is_named():
    with pytest.raises(ConfigError, match="kind 'meson' is not supported"):
        parse('[project]\nname="d"\n[build]\nkind = "meson"\n')


# --------------------------------------------------------------------------- #
# the CLI surface
# --------------------------------------------------------------------------- #

def test_tree_and_flags():
    cfg = parse()
    parser = cli.build_parser(cli.build_tree(cfg, []), "demo")
    args = parser.parse_args(["build", "debug", "--test", "--no-storage"])
    assert (args.preset, args.test, args.opt_storage) == ("debug", True, False)
    assert parser.parse_args(["build"]).preset is None
    assert parser.parse_args(["check", "lint", "--fix"]).fix is True


def test_an_undeclared_preset_is_rejected_by_the_parser():
    parser = cli.build_parser(cli.build_tree(parse(), []), "demo")
    with pytest.raises(SystemExit):
        parser.parse_args(["build", "coverage"])


def test_the_old_cli_shape_is_mapped_to_the_new_one():
    # `butler.py release --build --test --docker` was vl's CLI before M4.
    cfg = parse()
    roots = cli.build_tree(cfg, [])
    assert cli.suggest("release", roots, cfg) == "did you mean:  butler.py build release"
    assert cli.suggest("lint", roots, cfg) == "did you mean:  butler.py check lint"


# --------------------------------------------------------------------------- #
# what actually gets run
# --------------------------------------------------------------------------- #

def echoed(capsys) -> str:
    return capsys.readouterr().out


@pytest.fixture(autouse=True)
def fake_cmake(monkeypatch):
    """A fixed cmake, so these tests describe butler rather than this machine."""
    monkeypatch.setattr(cmake_component, "find_cmake",
                        lambda minimum: (Path("/usr/bin/cmake"), (3, 30, 0)))
    monkeypatch.setattr(cmake_component, "sibling", lambda path, name: f"/usr/bin/{name}")


def test_host_build_configures_then_builds_with_the_options(tmp_path, capsys):
    cmake_component.build(make_ctx(tmp_path), build_args(preset="debug"))
    out = echoed(capsys)
    assert "/usr/bin/cmake --preset debug -DDEMO_WITH_SFTP=ON -DDEMO_WITH_S3=ON" in out
    assert "/usr/bin/cmake --build --preset debug --parallel 4" in out
    assert "ctest" not in out


def test_options_are_forwarded_on_every_configure(tmp_path, capsys):
    # Not only the first: a build dir configured the other way must be corrected.
    cmake_component.build(make_ctx(tmp_path), build_args(opt_storage=False))
    assert "-DDEMO_WITH_SFTP=OFF -DDEMO_WITH_S3=OFF" in echoed(capsys)


def test_test_run_brings_services_up_and_down_around_ctest(tmp_path, capsys):
    cmake_component.build(make_ctx(tmp_path), build_args(test=True, test_filter="crypto"))
    out = echoed(capsys)
    up = out.index("bash test/start-test-env.sh")
    ctest = out.index("/usr/bin/ctest --preset release -j 4 --output-junit")
    assert up < ctest, "services must be up before the tests run"
    assert "-R crypto" in out
    assert str(tmp_path / "builds/release/test-results.xml") in out


def test_services_are_skipped_when_their_option_is_off(tmp_path, capsys):
    # The clients that talk to them were compiled out, so there is nothing to
    # integration-test and nothing to serve.
    cmake_component.build(make_ctx(tmp_path), build_args(test=True, opt_storage=False))
    out = echoed(capsys)
    assert "start-test-env" not in out and "/usr/bin/ctest" in out


def test_docker_build_mounts_the_tree_and_runs_one_script(tmp_path, capsys):
    cmake_component.build(make_ctx(tmp_path), build_args(docker=True, test=True))
    out = echoed(capsys)
    assert f"docker build -t demo-buildenv -f {tmp_path}/building/buildenv.Dockerfile" in out
    assert f"-v {tmp_path}:/demo -w /demo" in out
    assert "--network host -e DEMO_TEST_INTEGRATION=1" in out
    # The JUnit path is ABSOLUTE inside the container, and this assertion is the
    # point of the test rather than a detail of it. `ctest --preset` runs in the
    # preset's binary directory, so a relative --output-junit is resolved
    # against THAT: asking for `builds/release/test-results.xml` wrote the
    # report to `builds/release/builds/release/test-results.xml` and every CI
    # job that uploads it found nothing. ctest creates the directories and exits
    # 0, so nothing failed -- it just silently stopped producing test results.
    assert ("cmake --preset release -DDEMO_WITH_SFTP=ON -DDEMO_WITH_S3=ON && "
            "cmake --build --preset release --parallel 4 && "
            "mkdir -p $(dirname /demo/builds/release/test-results.xml) && "
            "ctest --preset release -j 4 "
            "--output-junit /demo/builds/release/test-results.xml") in out


def test_docker_junit_path_is_absolute(tmp_path, capsys):
    """A relative one lands under the binary dir, where nothing looks for it.

    Kept separate from the assertion above so the reason survives a rewrite of
    that command string: what matters is not the exact path but that it starts
    at the container's workdir.
    """
    cmake_component.build(make_ctx(tmp_path), build_args(docker=True, test=True))
    out = echoed(capsys)
    junit = out.split("--output-junit ", 1)[1].split()[0]
    assert junit.startswith("/demo/"), junit


def test_docker_needs_the_section(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    ctx.cfg.build.docker = None
    with pytest.raises(Exception, match="needs a \\[build.docker\\] section"):
        cmake_component.build(ctx, build_args(docker=True))


def docker_help(**docker_keys) -> tuple[str, str]:
    """The --docker help line and the build epilog, for a [build.docker] shape."""
    extra = "".join(f"{k} = {v}\n" for k, v in docker_keys.items())
    cfg = parse(TOML.replace("workdir = \"/demo\"\n", f"workdir = \"/demo\"\n{extra}")).build
    node = cmake_component.node(cfg)
    flag = next(a for a in node.args if a.flags and a.flags[0] == "--docker")
    return flag.kwargs["help"], node.epilog or ""


def test_docker_help_says_which_presets_the_flag_can_change():
    help_, epilog = docker_help(presets='["release", "debug"]',
                                always='["debug"]')
    assert "changes anything only for: release" in help_
    assert "with or without the flag: debug" in epilog
    assert "cannot share a build directory" in epilog


def test_docker_help_admits_when_the_flag_is_a_no_op():
    # Every preset with a buildenv is also forced into it: there is no
    # invocation the flag changes, and the help should say so rather than
    # advertising presets it "supports".
    help_, epilog = docker_help(presets='["debug"]', always='["debug"]')
    assert help_.startswith("no-op here")
    assert "never changes a build here" in epilog
    # The shared-build-directory warning is about host-vs-container builds of
    # the same preset, which cannot happen here.
    assert "cannot share a build directory" not in epilog


def test_an_unknown_preset_names_the_ones_that_exist(tmp_path):
    # Reachable from butler_tasks.py, which doesn't go through argparse's
    # choices — so the error has to be useful on its own.
    with pytest.raises(ButlerError, match="unknown preset 'coverage'") as e:
        cmake_component.build(make_ctx(tmp_path), build_args(preset="coverage"))
    assert "release, debug" in e.value.hint


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def test_lint_hides_only_the_heavy_trees_that_exist(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    (tmp_path / "deps").mkdir()
    lint_component.lint(ctx, SimpleNamespace(fix=True, linters="PYTHON_RUFF"))
    out = echoed(capsys)
    assert "--tmpfs /tmp/lint/deps" in out
    assert "builds" not in out.split("--tmpfs /tmp/lint/deps")[1].split("demo-lint-env")[0]
    assert "-e MEGALINTER_CONFIG=test/linting/.mega-linter.yml" in out
    assert "-e APPLY_FIXES=all" in out and "-e ENABLE_LINTERS=PYTHON_RUFF" in out
    assert f"-v {tmp_path}:/tmp/lint:rw" in out


def test_tidy_builds_first_then_runs_the_driver(tmp_path, capsys):
    lint_component.tidy(make_ctx(tmp_path),
                        SimpleNamespace(extra=[], opt_storage=False))
    out = echoed(capsys)
    build_at = out.index("/usr/bin/cmake --build --preset release")
    driver_at = out.index("test/clang-tidy.py")
    assert build_at < driver_at, "the compile database has to exist first"
    # The database must describe the same configuration clang-tidy reports on.
    assert "-DDEMO_WITH_SFTP=OFF" in out[:build_at]
    assert str(tmp_path / "builds/release/compile_commands.json") in out


def test_bare_check_runs_every_configured_checker(tmp_path, capsys):
    lint_component.run_all(make_ctx(tmp_path), SimpleNamespace())
    out = echoed(capsys)
    assert "demo-lint-env" in out and "test/clang-tidy.py" in out


# --------------------------------------------------------------------------- #
# per-preset buildenvs, prepare scripts, multi-command services
# --------------------------------------------------------------------------- #

YEET_SHAPED = """
[project]
name = "demo"

[build]
presets   = ["release", "cachyos"]
build_dir = "build/builds/{preset}"
prepare   = "build/{preset}/prepare.sh"

[build.docker]
# One image per preset, and cachyos has no host build at all.
dockerfile = "build/{preset}/buildenv.Dockerfile"
tag        = "demo-{preset}-env"
workdir    = "/demo"
presets    = ["cachyos"]
always     = ["cachyos"]

[build.test_env]
up = [
  ["docker", "compose", "-f", "deps/vl/test/docker-compose.yml", "up", "-d"],
  ["docker", "compose", "-f", "deps/vl/test/docker-compose.yml", "run", "--rm", "createbuckets"],
]
down     = [["docker", "compose", "-f", "deps/vl/test/docker-compose.yml", "down", "-v"]]
requires = ["deps/vl/test/docker-compose.yml"]
docker_args = ["--network", "host", "--device", "/dev/fuse"]
"""


def yeet_ctx(tmp_path: Path, *, with_compose: bool = True) -> Ctx:
    prep = tmp_path / "build" / "cachyos"
    prep.mkdir(parents=True, exist_ok=True)
    (prep / "buildenv.Dockerfile").write_text("FROM scratch\n")
    (prep / "prepare.sh").write_text("#!/bin/sh\n")
    (prep / "prepare.sh").chmod(0o755)
    if with_compose:
        compose = tmp_path / "deps" / "vl" / "test"
        compose.mkdir(parents=True, exist_ok=True)
        (compose / "docker-compose.yml").write_text("{}\n")
    return Ctx(cfg=parse(YEET_SHAPED, tmp_path), dry_run=True, assume_yes=True)


def test_a_preset_can_be_container_only(tmp_path, capsys):
    # No --docker flag: the image IS this preset's toolchain.
    cmake_component.build(yeet_ctx(tmp_path), build_args(preset="cachyos"))
    out = echoed(capsys)
    assert "docker build -t demo-cachyos-env" in out
    assert f"-v {tmp_path}:/demo -w /demo" in out
    assert "/usr/bin/cmake --preset" not in out, "must not also build on the host"


def test_the_other_presets_still_build_on_the_host(tmp_path, capsys):
    cmake_component.build(yeet_ctx(tmp_path), build_args(preset="release"))
    out = echoed(capsys)
    assert "/usr/bin/cmake --preset release" in out and "docker build" not in out


def test_docker_is_refused_for_a_preset_with_no_image(tmp_path):
    with pytest.raises(ButlerError, match="no buildenv image"):
        cmake_component.build(yeet_ctx(tmp_path), build_args(preset="release", docker=True))


def test_the_prepare_script_runs_for_a_host_build_that_has_one(tmp_path, capsys):
    # It makes THIS MACHINE able to build the preset — typically installing
    # system packages with sudo.
    prep = tmp_path / "build" / "release"
    prep.mkdir(parents=True, exist_ok=True)
    (prep / "prepare.sh").write_text("#!/bin/sh\n")
    (prep / "prepare.sh").chmod(0o755)
    cmake_component.build(yeet_ctx(tmp_path), build_args(preset="release"))
    assert "build/release/prepare.sh" in echoed(capsys)


def test_a_container_build_never_runs_the_prepare_script(tmp_path, capsys):
    # The image already carries the toolchain, and there is nobody there to
    # answer the sudo prompt.
    cmake_component.build(yeet_ctx(tmp_path), build_args(preset="cachyos"))
    assert "prepare.sh" not in echoed(capsys)


def test_services_can_take_several_commands(tmp_path, capsys):
    cmake_component.build(yeet_ctx(tmp_path), build_args(preset="cachyos", test=True))
    out = echoed(capsys)
    up = out.index("docker compose -f deps/vl/test/docker-compose.yml up -d")
    barrier = out.index("run --rm createbuckets")
    run = out.index("docker run --rm --name demo-cachyos")
    assert up < barrier < run, "the readiness barrier belongs before the tests"
    assert "--network host --device /dev/fuse" in out


def test_a_missing_prerequisite_is_named_before_anything_starts(tmp_path):
    # The compose file comes from a dependency, so it is absent until that
    # dependency is cloned — and starting the tests anyway fails opaquely.
    ctx = yeet_ctx(tmp_path, with_compose=False)
    with pytest.raises(ButlerError, match="deps/vl/test/docker-compose.yml"):
        cmake_component.build(ctx, build_args(preset="cachyos", test=True))


def test_a_build_without_tests_starts_no_services(tmp_path, capsys):
    cmake_component.build(yeet_ctx(tmp_path), build_args(preset="cachyos"))
    out = echoed(capsys)
    assert "docker-compose.yml" not in out
    assert "--device /dev/fuse" not in out, "the FUSE flags exist for the tests"


def test_undeclared_presets_in_the_docker_section_are_rejected():
    with pytest.raises(ConfigError, match="names undeclared preset"):
        parse('[project]\nname="d"\n[build]\npresets=["release"]\n'
              '[build.docker]\ndockerfile="D"\npresets=["cachyos"]\n')


# --------------------------------------------------------------------------- #
# check: clearing results, tidy in the container, the report stage
# --------------------------------------------------------------------------- #

CHECK_SHAPED = YEET_SHAPED + """
[check]
report = ["python", "test/check/check-report.py"]

[check.lint]
config  = "test/check/.mega-linter.yml"
image   = "demo-lint-env"
reports = "test/check/results"
clear   = true
keep    = ["clang-tidy.json"]

[check.tidy]
driver = "test/check/clang-tidy.py"
preset = "cachyos"
docker = true
args   = ["--json-out", "test/check/results/clang-tidy.json"]
"""


def check_ctx(tmp_path: Path, *, dry_run: bool = True) -> Ctx:
    yeet_ctx(tmp_path)                       # buildenv + prepare + compose
    check = tmp_path / "test" / "check"
    check.mkdir(parents=True, exist_ok=True)
    (check / ".mega-linter.yml").write_text("{}\n")
    (check / "clang-tidy.py").write_text("")
    (check / "check-report.py").write_text("")
    return Ctx(cfg=parse(CHECK_SHAPED, tmp_path), dry_run=dry_run, assume_yes=True)


def test_stale_results_are_cleared_but_the_other_stage_survives(tmp_path):
    ctx = check_ctx(tmp_path, dry_run=False)
    results = tmp_path / "test" / "check" / "results"
    results.mkdir(parents=True)
    (results / "results_sarif.sarif").write_text("stale")
    (results / "linters_logs").mkdir()
    (results / "clang-tidy.json").write_text("tidy findings")

    lint_component._reports_dir(ctx, ctx.cfg.check.lint)

    # A stale SARIF is re-read by the project-scope scanners and re-counted, so
    # it has to go; the tidy stage's artifact is not MegaLinter's to delete.
    assert sorted(p.name for p in results.iterdir()) == ["clang-tidy.json"]
    assert (results / "clang-tidy.json").read_text() == "tidy findings"


def test_dry_run_clears_nothing(tmp_path):
    ctx = check_ctx(tmp_path)
    results = tmp_path / "test" / "check" / "results"
    results.mkdir(parents=True)
    (results / "results_sarif.sarif").write_text("stale")
    lint_component._reports_dir(ctx, ctx.cfg.check.lint)
    assert (results / "results_sarif.sarif").exists()


def test_reports_go_where_the_project_keeps_them(tmp_path, capsys):
    lint_component.lint(check_ctx(tmp_path), SimpleNamespace(fix=False, linters=None))
    out = echoed(capsys)
    assert "-e REPORT_OUTPUT_FOLDER=/tmp/lint/test/check/results" in out


def test_tidy_runs_in_the_buildenv_when_asked(tmp_path, capsys):
    lint_component.tidy(check_ctx(tmp_path), SimpleNamespace(extra=[]))
    out = echoed(capsys)
    # The compile database references the container's toolchain and headers, so
    # the driver has to run where those exist.
    assert "docker run --rm --name demo-tidy" in out
    assert ("bash -c python3 test/check/clang-tidy.py "
            "build/builds/cachyos/compile_commands.json "
            "--json-out test/check/results/clang-tidy.json") in out
    assert "/usr/bin/python" not in out, "must not run on the host"


def test_the_report_runs_even_when_a_stage_fails(tmp_path, capsys, monkeypatch):
    ctx = check_ctx(tmp_path)
    monkeypatch.setattr(lint_component, "lint",
                        lambda c, a: (_ for _ in ()).throw(ButlerError("lint found things", code=3)))
    rc = lint_component.run_all(ctx, SimpleNamespace())
    out = echoed(capsys)
    assert rc == 3, "the failure still fails the command"
    assert "check-report.py" in out, "a findings report is most useful when there are findings"
