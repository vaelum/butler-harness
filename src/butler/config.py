"""butler.toml — parsing and validation.

In a config-first design the dominant failure mode is a typo'd key silently
doing nothing, so every table is consumed strictly: anything left over after the
known keys are read is an error naming the offending key. That is the whole
reason for the `Table` wrapper rather than plain dict access.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from .errors import ButlerError, ConfigError

CONFIG_NAME = "butler.toml"
TASKS_NAME = "butler_tasks.py"
# Both live in a butler/ subdirectory by default, so a project root gains one
# entry (the butler.py shim) rather than three. The flat layout is still read,
# so a project can move at its own pace.
CONFIG_DIR = "butler"

# Recognised but not yet implemented; named here so a project that declares one
# gets "not implemented yet" instead of "unknown key". Empty today — [build] and
# [check] landed in M6 — but the mechanism stays: it is how the next planned
# section announces itself.
PLANNED_SECTIONS: dict[str, str] = {}


class Table:
    """A TOML table that must be fully consumed."""

    def __init__(self, data: Any, where: str):
        if not isinstance(data, dict):
            raise ConfigError(f"[{where}] must be a table, got {type(data).__name__}")
        self._d = dict(data)
        self._where = where

    def _pop(self, key: str, default: Any, required: bool) -> Any:
        if key in self._d:
            return self._d.pop(key)
        if required:
            raise ConfigError(f"[{self._where}] is missing the required key '{key}'")
        return default

    def _typed(self, key: str, type_: type, default: Any, required: bool) -> Any:
        val = self._pop(key, default, required)
        if val is not None and not isinstance(val, type_):
            raise ConfigError(
                f"[{self._where}] '{key}' must be {type_.__name__}, "
                f"got {type(val).__name__}")
        return val

    def str_(self, key, default=None, *, required=False) -> Any:
        return self._typed(key, str, default, required)

    def int_(self, key, default=None, *, required=False) -> Any:
        return self._typed(key, int, default, required)

    def bool_(self, key, default=None, *, required=False) -> Any:
        return self._typed(key, bool, default, required)

    def list_(self, key, default=None, *, required=False) -> list:
        val = self._typed(key, list, None, required)
        return list(val) if val is not None else list(default or [])

    def table(self, key: str) -> Table | None:
        val = self._pop(key, None, False)
        return Table(val, f"{self._where}.{key}") if val is not None else None

    def open_table(self, key: str) -> dict:
        """A table whose KEYS are user-chosen (script names), so it can't be
        consumed strictly — only its values are validated."""
        val = self._pop(key, None, False)
        if val is None:
            return {}
        if not isinstance(val, dict):
            raise ConfigError(f"[{self._where}.{key}] must be a table")
        return dict(val)

    def done(self) -> None:
        if self._d:
            keys = ", ".join(sorted(self._d))
            raise ConfigError(f"[{self._where}] has unknown key(s): {keys}")


# --------------------------------------------------------------------------- #
# component configs
# --------------------------------------------------------------------------- #

@dataclass
class InstallConfig:
    """`app install`: the Linux AppImage + icon + .desktop launcher."""
    name: str
    programs_dir: Path
    comment: str = ""
    categories: list[str] = field(default_factory=lambda: ["Utility"])
    wm_class: str | None = None

    @property
    def startup_wm_class(self) -> str:
        return self.wm_class or self.name


@dataclass
class AndroidConfig:
    key_name: str
    dname: str
    # A vault OUTSIDE the repo holds the signing keys, one subfolder per app, so
    # a keystore is never in a project tree and one vault signs several apps.
    # $ANDROID_KEYSTORE_VAULT wins; this is the fallback.
    vault: Path = Path("~/Documents/important")
    # Permit plain-HTTP to a self-hosted LAN backend via a network security
    # config (see components/android.py for why not usesCleartextTraffic).
    cleartext: bool = False
    # One APK per ABI instead of one universal APK carrying all of them.
    split_abi: bool = False
    ndk_version: str | None = None
    # Prefix for the four signing-override env vars, e.g. MYAPP ->
    # MYAPP_ANDROID_KEYSTORE / _KS_PASS / _KEY_ALIAS / _KEY_PASS.
    env_prefix: str = ""


@dataclass
class TauriConfig:
    dir: Path
    android: AndroidConfig | None = None
    install: InstallConfig | None = None
    # `cargo tauri dev` under a distinct identity, so a dev window can't collide
    # with an installed release build's WebView storage / keychain entries.
    dev_identifier: str | None = None
    dev_product_name: str | None = None
    icon_generator: str | None = None   # script run before `cargo tauri icon`
    # Master art for `cargo tauri icon`, relative to the PROJECT root — it is
    # often shared with the web frontend and so lives outside the app dir.
    icon_source: Path | None = None

    @property
    def tauri_dir(self) -> Path:
        return self.dir / "src-tauri"


@dataclass
class DeployConfig:
    dir: str
    host: str | None = None
    # The ssh target read from a file at deploy time instead of from this
    # config. butler.toml is committed, and a server address is exactly the
    # thing a public repo must not carry — so the file is gitignored, and
    # `host_file` names it. Relative to the server directory.
    host_file: Path | None = None
    backup_dir: str | None = None
    # How to snapshot before overwriting: zip the whole data dir (a real restore
    # point), copy a single sqlite file, or nothing.
    backup: str = "zip-data-dir"
    # Relative to `dir`, or absolute / ~-rooted when the data lives outside the
    # deploy directory (a bind mount the compose file points at).
    data_dir: str = "data"
    db_file: str | None = None
    # Compose file relative to `dir`, when it isn't at the deploy dir's root.
    compose_file: str | None = None
    # Build the new image BEFORE stopping the old stack, so downtime is the swap
    # rather than the build. Only correct when the image COPYs its source in:
    # with a bind-mounted source tree the running container would pick up the
    # new code at rsync time, ahead of the swap.
    build_first: bool = False
    # Local environment variables forwarded to the remote build, for build-time
    # knobs like an apt mirror. Not for secrets — the script is echoed.
    build_env: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: [
        "data/", ".env", ".venv/", "__pycache__/", "*.egg-info/", ".pytest_cache/",
    ])
    # After a FIRST deploy, poll the logs for this prefix and print what follows
    # (the one-time claim token these servers emit when unclaimed).
    post_deploy_watch: str | None = None

    @cached_property
    def ssh_host(self) -> str:
        """The ssh target: `host`, or the contents of `host_file`.

        Resolved here rather than at parse time on purpose — `--help`, `doctor`
        and every non-deploy command must work in a fresh clone that has no
        deploy target yet.
        """
        if self.host:
            return self.host
        assert self.host_file is not None
        if not self.host_file.is_file():
            raise ButlerError(
                f"{self.host_file} not found",
                hint="Create it with the deploy target, e.g.:\n"
                     f"  echo 'user@example.com' > {self.host_file}")
        host = self.host_file.read_text().strip()
        if not host:
            raise ButlerError(f"{self.host_file} is empty")
        return host


@dataclass
class ScriptAction:
    """A command the project already has a script for.

    Not every server is compose-and-rsync. Some drive their own
    `scripts/build.sh`, and re-expressing that in config would be a rewrite
    rather than a migration — so butler delegates and just owns the CLI.
    """
    name: str
    cmd: list[str]
    help: str


@dataclass
class ServerConfig:
    dir: Path
    module: str = "app.main:app"
    port: int = 8000
    dev_port: int | None = None      # host port the compose stack publishes
    service: str = "server"          # compose service name
    compose: bool = True
    # The sqlite file inside the data dir, if the server has one. Used by
    # `server reset` locally and as the default for a sqlite-file deploy backup.
    db_file: str | None = None
    data_dir: str = "data"
    # Compose file relative to `dir`, when it isn't at the server dir's root.
    compose_file: str | None = None
    venv: Path | None = None
    test: list[str] = field(default_factory=lambda: ["-m", "pytest"])
    deploy: DeployConfig | None = None
    # Extra (or overriding) actions that shell out to the project's own scripts.
    scripts: list[ScriptAction] = field(default_factory=list)


@dataclass
class FirefoxConfig:
    # A stable add-on id, so storage survives updates and Mozilla can sign it
    # later. Email-style ids are valid AMO ids.
    id: str
    min_version: str = "115.0"


@dataclass
class ExtensionConfig:
    dir: Path
    artifact: str                    # base name of the produced zips
    firefox: FirefoxConfig | None = None


@dataclass
class BuildOption:
    """A project-level build switch, exposed as a CLI flag and CMake variables.

    `-DVL_WITH_SFTP=ON -DVL_WITH_S3=ON` is not something the harness can know
    about, but "one flag flipping a named set of cache variables" is a shape it
    can. The values are forwarded on EVERY configure, not just the first, so a
    build directory previously configured the other way is corrected rather than
    silently reused.
    """
    name: str
    cmake: list[str]
    default: bool = True
    help: str = ""

    @property
    def flag(self) -> str:
        """`--no-storage` for a default-on option, `--storage` for a default-off
        one — so the flag always reads as the thing it changes."""
        return f"--no-{self.name}" if self.default else f"--{self.name}"

    @property
    def dest(self) -> str:
        return f"opt_{self.name.replace('-', '_')}"

    def defines(self, enabled: bool) -> list[str]:
        return [f"-D{var}={'ON' if enabled else 'OFF'}" for var in self.cmake]


@dataclass
class TestEnvConfig:
    """Services the test suite talks to, brought up around a test run.

    `needs` names a BuildOption: the integration tests only exist when the
    clients that talk to these services were compiled in, so with the option off
    there is nothing to serve and nothing is started.
    """
    # Each is a list of commands: standing services up is often two steps (start
    # the stack, then run the job that creates the bucket as a readiness barrier).
    up: list[list[str]]
    down: list[list[str]] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    needs: str | None = None
    # Host paths that must exist before a test run, checked up front. A compose
    # file supplied by a dependency, or /dev/fuse for a suite that mounts: both
    # fail deep and confusingly when absent, and clearly when checked here.
    requires: list[str] = field(default_factory=list)
    # Extra `docker run` arguments for a containerised test run — host
    # networking to reach services published on 127.0.0.1, a device and the
    # capabilities a FUSE mount needs, and so on.
    docker_args: list[str] = field(default_factory=list)


@dataclass
class BuildenvConfig:
    """A Docker image carrying the toolchain, so a build needs only Docker.

    `dockerfile`, `context` and `tag` may contain `{preset}`: a project can ship
    one image per preset (a distro image per target) rather than one for all.
    """
    dockerfile: str
    tag: str
    context: str | None = None
    # Where the project tree is mounted inside the container. A host build and a
    # container build therefore cannot share a build directory — the CMake cache
    # records absolute paths — which is why `--docker` is a CI/clean-tree path.
    workdir: str = "/src"
    # Presets that can build in the container; empty means all of them.
    presets: list[str] = field(default_factory=list)
    # Presets that build in the container even without `--docker`, because the
    # image IS their toolchain and a host build of them was never meaningful.
    always: list[str] = field(default_factory=list)

    def for_preset(self, preset: str, root: Path) -> "ResolvedBuildenv":
        dockerfile = root / self.dockerfile.format(preset=preset)
        return ResolvedBuildenv(
            dockerfile=dockerfile,
            context=(root / self.context.format(preset=preset)) if self.context
                    else dockerfile.parent,
            tag=self.tag.format(preset=preset),
            workdir=self.workdir,
        )

    def supports(self, preset: str) -> bool:
        return not self.presets or preset in self.presets


@dataclass
class ResolvedBuildenv:
    """A BuildenvConfig with `{preset}` filled in — what the component runs."""
    dockerfile: Path
    context: Path
    tag: str
    workdir: str


@dataclass
class BuildConfig:
    dir: Path
    presets: list[str]
    build_dir: str = "builds/{preset}"
    cmake_min: tuple[int, ...] = (3, 22)
    jobs: int = 8
    options: list[BuildOption] = field(default_factory=list)
    docker: BuildenvConfig | None = None
    test_env: TestEnvConfig | None = None
    # A per-preset script run before configuring, if it exists — where a target
    # fetches or generates what CMake then expects to find. May contain
    # `{preset}`; a missing file is not an error, since most presets have none.
    prepare: str | None = None

    @property
    def default_preset(self) -> str:
        return self.presets[0]

    def preset_dir(self, preset: str) -> Path:
        return self.dir / self.build_dir.format(preset=preset)

    def option(self, name: str) -> BuildOption | None:
        return next((o for o in self.options if o.name == name), None)


@dataclass
class LintConfig:
    """MegaLinter, driven as a container directly — no node/npx on the host."""
    config: str                      # relative to the root: it IS a container path
    image: str
    dockerfile: Path | None = None   # a thin local wrapper over the pinned image
    context: Path | None = None
    reports: str = "megalinter-reports"
    # Empty the reports directory before a run. MegaLinter reuses it in place,
    # and the project-scope scanners walk the workspace INCLUDING that directory
    # — so last run's SARIF is re-read and re-counted, and findings that were
    # actually fixed keep resurfacing.
    clear: bool = False
    # Entries `clear` preserves: another stage's artifact living in the same
    # directory, which a lint-only run must not delete.
    keep: list[str] = field(default_factory=list)
    # Heavy trees an empty tmpfs is overlaid on. MegaLinter's own file filtering
    # does not protect against project-scope scanners (trufflehog, checkov,
    # jscpd) that walk the workspace themselves.
    hide: list[str] = field(default_factory=list)
    workspace: str = "/tmp/lint"


@dataclass
class TidyConfig:
    """clang-tidy over the compile database a configured build leaves behind."""
    driver: str
    preset: str | None = None
    db: str = "compile_commands.json"
    # Run the driver inside the buildenv container rather than on the host.
    # clang-tidy needs the exact toolchain and headers the compile database
    # references; when the database was produced in a container, the host's
    # clang-tidy is reading paths that only exist inside it.
    docker: bool = False
    args: list[str] = field(default_factory=list)


@dataclass
class CheckConfig:
    lint: LintConfig | None = None
    tidy: TidyConfig | None = None
    # Run after the stages, always — it folds whatever artifacts are current
    # into one report, which is most useful exactly when a stage found things.
    report: list[str] = field(default_factory=list)


@dataclass
class PublishConfig:
    """How this project is exported to a public mirror.

    The whole point of this section is that a project should state only what is
    *true of this project* — which repositories, which branch, which extra
    paths stay private — and nothing about how Copybara is driven. Everything
    else (the pinned jar, the identity pinning, the baseline exclude list, the
    commit scrubber) is the harness's business and lives in one place.
    """
    forgejo: str                 # "vaelum/chords" — the private source
    github: str                  # "vaelum/chords" — the generated mirror
    visibility: str              # "public" | "private"; documents intent
    branch: str                  # the publication branch
    author: str                  # every exported commit is authored by this
    committer: str               # ...and committed by this
    exclude: list[str]           # project-specific additions to the baseline
    release: bool                # a tag's CI-built release is copied to the mirror
    host: str                    # Forgejo host; stated per project, never defaulted
    port: int                    # Forgejo SSH port
    rewrite_harness: bool        # repoint butler.py's pin at the public harness
    harness_github: str          # where the public harness lives publicly
    harness_forgejo: str | None  # ...and privately, when a doc links to it

    @property
    def origin_url(self) -> str:
        return f"ssh://git@{self.host}:{self.port}/{self.forgejo}.git"

    @property
    def destination_url(self) -> str:
        return f"git@github.com:{self.github}.git"

    @property
    def is_public(self) -> bool:
        return self.visibility == "public"


@dataclass
class ProjectConfig:
    name: str
    dist: Path
    git_deps: list[dict] = field(default_factory=list)
    submodules: bool = False


@dataclass
class Config:
    root: Path
    project: ProjectConfig
    app: TauriConfig | None = None
    server: ServerConfig | None = None
    extension: ExtensionConfig | None = None
    build: BuildConfig | None = None
    check: CheckConfig | None = None
    publish: PublishConfig | None = None
    # Sections that parsed as "planned but unimplemented"; the CLI turns each
    # into a command that says so rather than pretending it doesn't exist.
    planned: dict[str, str] = field(default_factory=dict)

    @property
    def dist_dir(self) -> Path:
        return self.root / self.project.dist


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def config_path(root: Path) -> Path | None:
    """This project's butler.toml — nested first, then the flat fallback."""
    for candidate in (root / CONFIG_DIR / CONFIG_NAME, root / CONFIG_NAME):
        if candidate.is_file():
            return candidate
    return None


def tasks_path(root: Path) -> Path | None:
    """This project's butler_tasks.py, beside whichever butler.toml was found."""
    for candidate in (root / CONFIG_DIR / TASKS_NAME, root / TASKS_NAME):
        if candidate.is_file():
            return candidate
    return None


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` looking for a project.

    Walking up (rather than demanding cwd == project root) is what makes
    `python ../../butler.py app dev` and running from a subdirectory work.
    """
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        if (d / CONFIG_DIR / CONFIG_NAME).is_file():
            return d
        # The flat layout, except when `d` IS the butler/ directory of the
        # project above — running from inside butler/ must find the project,
        # not mistake that directory for the root.
        if (d / CONFIG_NAME).is_file() and d.name != CONFIG_DIR:
            return d
    raise ConfigError(
        f"no {CONFIG_DIR}/{CONFIG_NAME} found in {here} or any parent directory",
        hint="Create one with:  butler new .")


def load(root: Path) -> Config:
    path = config_path(root)
    if path is None:
        raise ConfigError(f"no {CONFIG_DIR}/{CONFIG_NAME} under {root}")
    try:
        raw = tomllib.loads(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e
    return parse(raw, root)


def parse(raw: dict, root: Path) -> Config:
    top = Table(raw, "")

    project = _project(top.table("project"), root)
    app = _tauri(top.table("app"), root, project.name)
    server = _server(top.table("server"), root)
    extension = _extension(top.table("extension"), root, project.name)
    build = _build(top.table("build"), root, project.name)
    check = _check(top.table("check"), root, project.name)
    publish = _publish(top.table("publish"), project.name)

    planned = {}
    for name, milestone in PLANNED_SECTIONS.items():
        if top.table(name) is not None:
            planned[name] = milestone
    top.done()

    return Config(root=root, project=project, app=app, server=server,
                  extension=extension, build=build, check=check, publish=publish,
                  planned=planned)


def _publish(t: Table | None, project_name: str) -> PublishConfig | None:
    if t is None:
        return None
    forgejo = t.str_("forgejo", required=True)
    # The mirror defaults to the same path: a project that renames on the way
    # out is the exception, and saying so twice for the common case is noise.
    github = t.str_("github", forgejo)
    visibility = t.str_("visibility", "private")
    if visibility not in ("public", "private"):
        raise ConfigError(
            f"[publish] 'visibility' must be \"public\" or \"private\", got {visibility!r}")
    for key, value in (("forgejo", forgejo), ("github", github)):
        if value.count("/") != 1 or value.startswith("/") or value.endswith("/"):
            raise ConfigError(
                f"[publish] '{key}' must be \"owner/name\", got {value!r}")
    author = t.str_("author", required=True)
    cfg = PublishConfig(
        forgejo=forgejo,
        github=github,
        visibility=visibility,
        branch=t.str_("branch", "main"),
        author=author,
        # One identity by default. push-public.sh had to pin the committer
        # separately or it became whoever ran the export; here the default
        # simply follows the author, and a project that wants them to differ
        # says so.
        committer=t.str_("committer", author),
        exclude=[str(x) for x in t.list_("exclude", [])],
        # Off by default: most mirrors are plain source, and a project whose
        # tags carry no CI-built release would otherwise fail every `--tag` run
        # looking for one. The projects that do build installers say so.
        release=t.bool_("release", False),
        # Required, deliberately. Defaulting this would bake one person's
        # server into a harness that is published and meant to be reusable —
        # and would put that hostname in the docs and tests of every copy.
        host=t.str_("host", required=True),
        port=t.int_("port", 2222),
        rewrite_harness=t.bool_("rewrite_harness", True),
        harness_github=t.str_("harness_github", "vaelum/butler-harness"),
        harness_forgejo=t.str_("harness_forgejo", None),
    )
    t.done()
    return cfg


def _version(text: str | None, where: str, default: tuple[int, ...]) -> tuple[int, ...]:
    if text is None:
        return default
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        raise ConfigError(f"[{where}] '{text}' is not a version like '3.22'") from None


def _build(t: Table | None, root: Path, project_name: str) -> BuildConfig | None:
    if t is None:
        return None
    kind = t.str_("kind", "cmake")
    if kind != "cmake":
        raise ConfigError(f"[build] kind '{kind}' is not supported (only 'cmake' so far)")
    directory = root / t.str_("dir", ".")
    options = _build_options(t.list_("option"))
    docker = _buildenv(t.table("docker"), root, project_name)
    test_env = _test_env(t.table("test_env"), options)
    presets = t.list_("presets", ["release", "debug"])
    if not presets or not all(isinstance(p, str) for p in presets):
        raise ConfigError("[build] presets must be a non-empty array of preset names")
    cfg = BuildConfig(
        dir=directory,
        presets=list(presets),
        build_dir=t.str_("build_dir", "builds/{preset}"),
        cmake_min=_version(t.str_("cmake_min"), "build.cmake_min", (3, 22)),
        jobs=t.int_("jobs", 8),
        options=options,
        docker=docker,
        test_env=test_env,
        prepare=t.str_("prepare"),
    )
    t.done()
    if "{preset}" not in cfg.build_dir:
        raise ConfigError("[build] build_dir must contain '{preset}' — one build "
                          "directory per preset is what keeps a debug build from "
                          "sharing a CMake cache with a release build")
    for key, names in (("presets", docker.presets if docker else []),
                       ("always", docker.always if docker else [])):
        unknown = [p for p in names if p not in cfg.presets]
        if unknown:
            raise ConfigError(f"[build.docker] {key} names undeclared preset(s): "
                              f"{', '.join(unknown)}")
    if docker:
        outside = [p for p in docker.always if not docker.supports(p)]
        if outside:
            raise ConfigError(f"[build.docker] always lists {', '.join(outside)}, "
                              f"which 'presets' excludes from container builds")
    return cfg


def _build_options(items: list) -> list[BuildOption]:
    out = []
    for i, item in enumerate(items):
        t = Table(item, f"build.option[{i}]")
        name = t.str_("name", required=True)
        cmake = t.list_("cmake", required=True)
        opt = BuildOption(
            name=name,
            cmake=list(cmake),
            default=t.bool_("default", True),
            help=t.str_("help", ""),
        )
        t.done()
        if not opt.cmake or not all(isinstance(v, str) for v in opt.cmake):
            raise ConfigError(f"[build.option.{name}] cmake must be a non-empty "
                              f"array of CMake variable names")
        out.append(opt)
    names = [o.name for o in out]
    if len(set(names)) != len(names):
        raise ConfigError("[build.option] two options share a name")
    return out


def _buildenv(t: Table | None, root: Path, project_name: str) -> BuildenvConfig | None:
    if t is None:
        return None
    cfg = BuildenvConfig(
        dockerfile=t.str_("dockerfile", required=True),
        # The build context defaults to the Dockerfile's directory, which is
        # what every one of these images actually wants — not the repo root,
        # whose gigabytes of build output would be sent to the daemon.
        context=t.str_("context"),
        tag=t.str_("tag", f"{project_name}-buildenv"),
        workdir=t.str_("workdir", "/src"),
        presets=t.list_("presets", []),
        always=t.list_("always", []),
    )
    t.done()
    return cfg


def _commands(value: list, where: str) -> list[list[str]]:
    """One command, or several. `["a", "b"]` and `[["a"], ["b"]]` both parse."""
    if not value:
        return []
    if all(isinstance(item, str) for item in value):
        return [list(value)]
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, list) or not item or not all(isinstance(c, str) for c in item):
            raise ConfigError(f"[{where}] must be an array of strings, or an "
                              f"array of such arrays (entry {i} is neither)")
        out.append(list(item))
    return out


def _test_env(t: Table | None, options: list[BuildOption]) -> TestEnvConfig | None:
    if t is None:
        return None
    up = _commands(t.list_("up", required=True), "build.test_env.up")
    env = t.open_table("env")
    cfg = TestEnvConfig(
        up=up,
        down=_commands(t.list_("down", []), "build.test_env.down"),
        env={str(k): str(v) for k, v in env.items()},
        needs=t.str_("needs"),
        requires=t.list_("requires", []),
        docker_args=t.list_("docker_args", []),
    )
    t.done()
    if not cfg.up:
        raise ConfigError("[build.test_env] up must be a non-empty command")
    if cfg.needs and not any(o.name == cfg.needs for o in options):
        raise ConfigError(f"[build.test_env] needs = '{cfg.needs}', which is not "
                          f"a declared [[build.option]]")
    for key, value in (("requires", cfg.requires), ("docker_args", cfg.docker_args)):
        if not all(isinstance(item, str) for item in value):
            raise ConfigError(f"[build.test_env] {key} must be an array of strings")
    return cfg


def _check(t: Table | None, root: Path, project_name: str) -> CheckConfig | None:
    if t is None:
        return None
    lint_t, tidy_t = t.table("lint"), t.table("tidy")
    cfg = CheckConfig(
        lint=_lint(lint_t, root, project_name),
        tidy=_tidy(tidy_t),
        report=t.list_("report", []),
    )
    t.done()
    if not all(isinstance(c, str) for c in cfg.report):
        raise ConfigError("[check] report must be an array of strings (a command)")
    if cfg.lint is None and cfg.tidy is None:
        raise ConfigError("[check] declares neither [check.lint] nor [check.tidy]")
    return cfg


def _lint(t: Table | None, root: Path, project_name: str) -> LintConfig | None:
    if t is None:
        return None
    dockerfile = t.str_("dockerfile")
    cfg = LintConfig(
        config=t.str_("config", ".mega-linter.yml"),
        image=t.str_("image", f"{project_name}-lint-env"),
        dockerfile=(root / dockerfile) if dockerfile else None,
        context=(root / c) if (c := t.str_("context")) else None,
        reports=t.str_("reports", "megalinter-reports"),
        clear=t.bool_("clear", False),
        keep=t.list_("keep", []),
        hide=t.list_("hide", []),
        workspace=t.str_("workspace", "/tmp/lint"),
    )
    t.done()
    if cfg.context is None and cfg.dockerfile is not None:
        cfg.context = cfg.dockerfile.parent
    if cfg.context is not None and cfg.dockerfile is None:
        raise ConfigError("[check.lint] context is only meaningful with a dockerfile")
    if not all(isinstance(h, str) for h in cfg.hide):
        raise ConfigError("[check.lint] hide must be an array of paths")
    return cfg


def _tidy(t: Table | None) -> TidyConfig | None:
    if t is None:
        return None
    cfg = TidyConfig(
        driver=t.str_("driver", required=True),
        preset=t.str_("preset"),
        db=t.str_("db", "compile_commands.json"),
        docker=t.bool_("docker", False),
        args=t.list_("args", []),
    )
    t.done()
    if not all(isinstance(a, str) for a in cfg.args):
        raise ConfigError("[check.tidy] args must be an array of strings")
    return cfg


def _extension(t: Table | None, root: Path, project_name: str) -> ExtensionConfig | None:
    if t is None:
        return None
    firefox = t.table("firefox")
    cfg = ExtensionConfig(
        dir=root / t.str_("dir", "extension"),
        artifact=t.str_("artifact", f"{project_name}-extension"),
        firefox=FirefoxConfig(
            id=firefox.str_("id", required=True),
            min_version=firefox.str_("min_version", "115.0"),
        ) if firefox else None,
    )
    if firefox:
        firefox.done()
    t.done()
    return cfg


def _project(t: Table | None, root: Path) -> ProjectConfig:
    if t is None:
        raise ConfigError("butler.toml has no [project] section",
                          hint='Minimum:  [project]\n          name = "myproject"')
    cfg = ProjectConfig(
        name=t.str_("name", required=True),
        dist=Path(t.str_("dist", "dist")),
        submodules=t.bool_("submodules", False),
        git_deps=_git_deps(t.list_("git_deps")),
    )
    t.done()
    return cfg


def _git_deps(items: list) -> list[dict]:
    out = []
    for i, item in enumerate(items):
        d = Table(item, f"project.git_deps[{i}]")
        out.append({
            "path": d.str_("path", required=True),
            "url": d.str_("url", required=True),
            "ref": d.str_("ref", "origin/main"),
        })
        d.done()
    return out


def _tauri(t: Table | None, root: Path, project_name: str) -> TauriConfig | None:
    if t is None:
        return None
    kind = t.str_("kind", "tauri")
    if kind != "tauri":
        raise ConfigError(f"[app] kind '{kind}' is not supported (only 'tauri' so far)")
    android = _android(t.table("android"), project_name)
    install = _install(t.table("install"))
    cfg = TauriConfig(
        dir=root / t.str_("dir", "app"),
        android=android,
        install=install,
        dev_identifier=t.str_("dev_identifier"),
        dev_product_name=t.str_("dev_product_name"),
        icon_generator=t.str_("icon_generator"),
        icon_source=(root / src) if (src := t.str_("icon_source")) else None,
    )
    t.done()
    return cfg


def _android(t: Table | None, project_name: str) -> AndroidConfig | None:
    if t is None:
        return None
    key_name = t.str_("key_name", project_name)
    cfg = AndroidConfig(
        key_name=key_name,
        dname=t.str_("dname", f"CN={key_name}, OU={key_name}, O={key_name}, C=DE"),
        vault=Path(t.str_("vault", "~/Documents/important")),
        cleartext=t.bool_("cleartext", False),
        split_abi=t.bool_("split_abi", False),
        ndk_version=t.str_("ndk_version"),
        env_prefix=t.str_("env_prefix", project_name.upper()),
    )
    t.done()
    return cfg


def _install(t: Table | None) -> InstallConfig | None:
    if t is None:
        return None
    cfg = InstallConfig(
        name=t.str_("name", required=True),
        programs_dir=Path(t.str_("programs_dir", required=True)).expanduser(),
        comment=t.str_("comment", ""),
        categories=t.list_("categories", ["Utility"]),
        wm_class=t.str_("wm_class"),
    )
    t.done()
    return cfg


def _server(t: Table | None, root: Path) -> ServerConfig | None:
    if t is None:
        return None
    kind = t.str_("kind", "fastapi")
    if kind != "fastapi":
        raise ConfigError(f"[server] kind '{kind}' is not supported (only 'fastapi' so far)")
    db_file = t.str_("db_file")
    data_dir = t.str_("data_dir", "data")
    compose_file = t.str_("compose_file")
    scripts = _scripts(t.open_table("scripts"))
    venv = t.str_("venv", ".venv")
    directory = root / t.str_("dir", "server")
    deploy = _deploy(t.table("deploy"), db_file, data_dir, compose_file, directory)
    cfg = ServerConfig(
        dir=directory,
        module=t.str_("module", "app.main:app"),
        port=t.int_("port", 8000),
        dev_port=t.int_("dev_port"),
        service=t.str_("service", "server"),
        compose=t.bool_("compose", True),
        db_file=db_file,
        data_dir=data_dir,
        compose_file=compose_file,
        venv=(directory / venv) if venv else None,
        test=t.list_("test", ["-m", "pytest"]),
        deploy=deploy,
        scripts=scripts,
    )
    t.done()
    return cfg


def _scripts(raw: dict) -> list[ScriptAction]:
    """`[server.scripts]` — either `name = [argv…]` or a table with cmd/help."""
    out = []
    for name, value in raw.items():
        where = f"server.scripts.{name}"
        if isinstance(value, list):
            cmd, help_ = value, None
        elif isinstance(value, dict):
            tbl = Table(value, where)
            cmd = tbl.list_("cmd", required=True)
            help_ = tbl.str_("help")
            tbl.done()
        else:
            raise ConfigError(f"[{where}] must be an array of strings, or a table "
                              f"with 'cmd' (and optionally 'help')")
        if not cmd or not all(isinstance(c, str) for c in cmd):
            raise ConfigError(f"[{where}] cmd must be a non-empty array of strings")
        out.append(ScriptAction(name=name, cmd=list(cmd),
                                help=help_ or f"run {' '.join(cmd)}"))
    return out


def _deploy(t: Table | None, db_file: str | None, data_dir: str,
            compose_file: str | None, server_dir: Path) -> DeployConfig | None:
    if t is None:
        return None
    backup = t.str_("backup", "zip-data-dir")
    if backup not in ("zip-data-dir", "sqlite-file", "none"):
        raise ConfigError(
            f"[server.deploy] backup '{backup}' is not one of: "
            "zip-data-dir, sqlite-file, none")
    host = t.str_("host")
    host_file = t.str_("host_file")
    cfg = DeployConfig(
        dir=t.str_("dir", required=True),
        host=host,
        host_file=(server_dir / host_file) if host_file else None,
        backup_dir=t.str_("backup_dir"),
        backup=backup,
        data_dir=t.str_("data_dir", data_dir),
        db_file=t.str_("db_file", db_file),
        compose_file=t.str_("compose_file", compose_file),
        build_first=t.bool_("build_first", False),
        build_env=t.list_("build_env", []),
        exclude=t.list_("exclude", DeployConfig.__dataclass_fields__["exclude"].default_factory()),
        post_deploy_watch=t.str_("post_deploy_watch"),
    )
    t.done()
    if bool(host) == bool(host_file):
        raise ConfigError(
            "[server.deploy] needs exactly one of 'host' (the ssh target) or "
            "'host_file' (a gitignored file holding it)")
    if not all(isinstance(name, str) for name in cfg.build_env):
        raise ConfigError("[server.deploy] build_env must be an array of variable names")
    if cfg.backup == "sqlite-file" and not cfg.db_file:
        raise ConfigError("[server.deploy] backup = 'sqlite-file' needs 'db_file'")
    if cfg.backup != "none" and not cfg.backup_dir:
        raise ConfigError(f"[server.deploy] backup = '{cfg.backup}' needs 'backup_dir'")
    return cfg
