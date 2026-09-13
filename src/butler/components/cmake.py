"""The `build` component: CMake presets, on the host or in a buildenv container.

The two C++ projects this replaces (`vl`, `yeet`) each grew their own copy of
"find a new-enough cmake, configure, build, ctest with a JUnit report, and
optionally do all of it inside Docker". The shapes matched; the details had
drifted. Where they disagreed the better one wins, and the differences that were
genuinely per-project (which presets exist, where build directories go, which
services the integration tests need) moved into butler.toml.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from .. import proc, sources, ui
from ..command import Node, arg
from ..config import BuildConfig, ResolvedBuildenv, TestEnvConfig
from ..context import Ctx
from ..errors import ButlerError
from . import docker_build

RESULTS_NAME = "test-results.xml"


def _build(ctx: Ctx) -> BuildConfig:
    if ctx.cfg.build is None:
        raise ButlerError("this project has no [build] configuration")
    return ctx.cfg.build


# --------------------------------------------------------------------------- #
# finding cmake
# --------------------------------------------------------------------------- #

def _candidates() -> list[Path]:
    """Every cmake on PATH in order, then the common pip/local install prefixes.

    Those last two are not decoration: a distro cmake older than the project
    requires is frequently shadowing a new enough one installed with pip, and
    trusting whatever `cmake` resolves to first is how that becomes a confusing
    configure error instead of a clear one.
    """
    out = [Path(d) / "cmake" for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    out += [Path.home() / ".local/bin/cmake", Path("/usr/local/bin/cmake")]
    return out


def find_cmake(minimum: tuple[int, ...]) -> tuple[Path, tuple[int, ...] | None]:
    """The first cmake meeting `minimum`, else the newest one found.

    Returning the too-old one rather than failing is deliberate — the configure
    error it produces names the actual unmet requirement, and a project may well
    have raised cmake_min ahead of what it truly needs.
    """
    best: tuple[Path, tuple[int, ...]] | None = None
    for cand in _candidates():
        if not (cand.is_file() and os.access(cand, os.X_OK)):
            continue
        ver = proc.tool_version(cand)
        if ver is None:
            continue
        if ver >= minimum:
            return cand, ver
        if best is None or ver > best[1]:
            best = (cand, ver)
    need = ".".join(str(v) for v in minimum)
    if best:
        ui.warn("warning:", f"no cmake >= {need} found; using {best[0]} "
                            f"({'.'.join(str(v) for v in best[1])}). "
                            f"Configure will likely fail.")
        return best
    raise ButlerError(f"no cmake found on PATH; this build needs cmake >= {need}",
                      hint="Install it, or `pip install cmake` for a local copy.")


def sibling(tool_path: Path, name: str) -> str:
    """`ctest` from beside the cmake we picked, so the pair always matches."""
    cand = tool_path.parent / name
    return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else name


# --------------------------------------------------------------------------- #
# options and the test environment
# --------------------------------------------------------------------------- #

def defines_for(cfg: BuildConfig, args) -> list[str]:
    """`-D…=ON/OFF` for every declared option, on every configure."""
    out = []
    for opt in cfg.options:
        enabled = getattr(args, opt.dest, opt.default)
        out.extend(opt.defines(enabled))
    return out


def test_env_active(cfg: BuildConfig, args) -> TestEnvConfig | None:
    """The test services, if this run is one that needs them.

    A build that isn't running tests needs nothing brought up — including the
    container flags that exist for the tests' sake.
    """
    env = cfg.test_env
    if env is None or not getattr(args, "test", False):
        return None
    if env.needs is not None:
        opt = cfg.option(env.needs)
        assert opt is not None  # config.py rejects an unknown name at parse time
        if not getattr(args, opt.dest, opt.default):
            return None
    return env


def require_paths(ctx: Ctx, env: TestEnvConfig) -> None:
    """Fail up front on anything the test run needs and the host hasn't got.

    Both real cases fail deep and unrecognisably otherwise: a compose file that
    a dependency supplies (so it isn't there until that dependency is cloned),
    and /dev/fuse (whose absence silently drops the container's FUSE flags and
    turns into dozens of opaque permission-denied mount failures).
    """
    missing = [p for p in env.requires if not Path(p if p.startswith("/") else ctx.root / p).exists()]
    if missing:
        raise ButlerError(
            "the test environment needs " + ", ".join(missing) + ", which "
            + ("is" if len(missing) == 1 else "are") + " missing",
            hint="A path under the project appears once its dependency is "
                 "checked out — build once first. /dev/fuse needs `modprobe fuse`.")


@contextmanager
def services(ctx: Ctx, env: TestEnvConfig | None):
    """Bring the test services up, and always take them back down.

    Teardown is best-effort and deliberately does NOT mask the test result: a
    compose file that fails to come down must not turn a red test suite green,
    or a green one red.
    """
    if env is None:
        yield
        return
    require_paths(ctx, env)
    ui.plain()
    ui.ok("starting", "the test services")
    for cmd in env.up:
        proc.check(cmd, cwd=ctx.root, dry_run=ctx.dry_run,
                   what="test environment startup")
    try:
        yield
    finally:
        if not ctx.dry_run:
            for cmd in env.down:
                proc.run(cmd, cwd=ctx.root)


# --------------------------------------------------------------------------- #
# building
# --------------------------------------------------------------------------- #

def in_docker(cfg: BuildConfig, preset: str, args) -> bool:
    """Whether this build runs in the container.

    A preset listed in `always` does so with or without the flag: for a target
    whose whole point is a specific distro image, the image IS the toolchain and
    a host build of it was never a meaningful thing to ask for.
    """
    asked = getattr(args, "docker", False)
    if cfg.docker is None:
        if asked:
            raise ButlerError("--docker needs a [build.docker] section",
                              hint="It names the Dockerfile carrying the toolchain.")
        return False
    return asked or preset in cfg.docker.always


def run_prepare(ctx: Ctx, cfg: BuildConfig, preset: str) -> None:
    """A per-preset prepare script, if this preset has one.

    Host builds only: this is what makes THIS MACHINE able to build the preset,
    typically by installing system packages with sudo. A container build has its
    toolchain baked into the image, so running it there would at best be a no-op
    and at worst prompt for a password nobody is there to type.

    Absence is normal, not an error — most presets need nothing, and the ones
    that do declare the same path template as the rest.
    """
    if not cfg.prepare:
        return
    script = ctx.root / cfg.prepare.format(preset=preset)
    if script.is_file():
        proc.check([script], cwd=ctx.root, dry_run=ctx.dry_run,
                   what=f"prepare script for {preset}")


def build(ctx: Ctx, args) -> int:
    cfg = _build(ctx)
    preset = args.preset or cfg.default_preset
    if preset not in cfg.presets:
        raise ButlerError(f"unknown preset '{preset}'",
                          hint=f"this project declares: {', '.join(cfg.presets)}")
    sources.prepare(ctx)
    runner = _build_in_docker if in_docker(cfg, preset, args) else _build_on_host
    runner(ctx, cfg, preset, args)
    ui.plain()
    ui.ok("done", f"— {ctx.name} {preset}")
    return 0


def _results_file(build_dir: str) -> str:
    return f"{build_dir}/{RESULTS_NAME}"


def _build_on_host(ctx: Ctx, cfg: BuildConfig, preset: str, args) -> None:
    run_prepare(ctx, cfg, preset)
    cmake, _ = find_cmake(cfg.cmake_min)
    defines = defines_for(cfg, args)

    proc.check([cmake, "--preset", preset, *defines], cwd=cfg.dir,
               dry_run=ctx.dry_run, what="cmake configure")
    proc.check([cmake, "--build", "--preset", preset, "--parallel", str(cfg.jobs)],
               cwd=cfg.dir, dry_run=ctx.dry_run, what="cmake build")
    if not args.test:
        return

    results = cfg.preset_dir(preset) / RESULTS_NAME
    if not ctx.would(f"create {ctx.disp(results.parent)}"):
        results.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sibling(cmake, "ctest"), "--preset", preset, "-j", str(cfg.jobs),
           "--output-junit", str(results)]
    if args.test_filter:
        cmd += ["-R", args.test_filter]

    env = test_env_active(cfg, args)
    with services(ctx, env):
        proc.check(cmd, cwd=cfg.dir, env=env.env if env else None,
                   dry_run=ctx.dry_run, what="ctest")


def buildenv_for(ctx: Ctx, cfg: BuildConfig, preset: str) -> ResolvedBuildenv:
    if cfg.docker is None:
        raise ButlerError("--docker needs a [build.docker] section",
                          hint="It names the Dockerfile carrying the toolchain.")
    if not cfg.docker.supports(preset):
        raise ButlerError(f"preset '{preset}' has no buildenv image",
                          hint="[build.docker] presets = "
                               f"{', '.join(cfg.docker.presets)}")
    return cfg.docker.for_preset(preset, ctx.root)


def _build_in_docker(ctx: Ctx, cfg: BuildConfig, preset: str, args) -> None:
    buildenv = buildenv_for(ctx, cfg, preset)
    env = test_env_active(cfg, args)
    image = docker_build.ensure_image(ctx, buildenv)

    build_dir = cfg.build_dir.format(preset=preset)
    defines = " ".join(defines_for(cfg, args))
    steps = [f"cmake --preset {preset} {defines}".rstrip(),
             f"cmake --build --preset {preset} --parallel {cfg.jobs}"]
    if args.test:
        results = _results_file(build_dir)
        ctest = (f"ctest --preset {preset} -j {cfg.jobs} --output-junit {results}")
        if args.test_filter:
            ctest += f" -R '{args.test_filter}'"
        steps.append(f"mkdir -p $(dirname {results})")
        steps.append(ctest)

    run_args = list(env.docker_args) if env else []
    for key, value in (env.env if env else {}).items():
        run_args += ["-e", f"{key}={value}"]

    with services(ctx, env):
        docker_build.run_in(ctx, buildenv, image, " && ".join(steps),
                            extra_args=run_args, name=f"{ctx.name}-{preset}")


# --------------------------------------------------------------------------- #
# the node
# --------------------------------------------------------------------------- #

def option_args(cfg: BuildConfig) -> list:
    out = []
    for opt in cfg.options:
        help_ = opt.help or f"{opt.name} ({', '.join(opt.cmake)})"
        out.append(arg(opt.flag, dest=opt.dest, default=opt.default,
                       action="store_false" if opt.default else "store_true",
                       help=("build without " if opt.default else "build with ") + help_))
    return out


def node(cfg: BuildConfig) -> Node:
    args = [
        arg("preset", nargs="?", choices=cfg.presets, default=None,
            help=f"CMake preset to build (default {cfg.default_preset})"),
        arg("--test", action="store_true", help="run the test suite after building"),
        arg("--test-filter", help="only tests matching this regex (ctest -R)"),
        *option_args(cfg),
    ]
    epilog = None
    if cfg.docker:
        # Only the presets that have a buildenv AND are not already forced into
        # it are ones the flag can change anything for. When that set is empty
        # the flag is a no-op for every preset it accepts, and saying so is more
        # use than listing presets it "supports".
        always = [p for p in cfg.docker.presets if p in cfg.docker.always]
        optional = [p for p in cfg.docker.presets if p not in cfg.docker.always]
        if not cfg.docker.presets:
            help_ = ("build inside the buildenv container instead of on the "
                     "host")
        elif optional:
            help_ = ("build inside the buildenv container instead of on the "
                     f"host (changes anything only for: {', '.join(optional)})")
        else:
            help_ = ("no-op here: every preset with a buildenv "
                     f"({', '.join(always)}) already builds in the container")
        args.append(arg("--docker", action="store_true", help=help_))

        lines = []
        if always:
            lines.append("Built in the container with or without the flag: "
                         + ", ".join(always) + " — the image IS that target's\n"
                         "toolchain, so a host build of it is not a thing to ask "
                         "for.")
        if optional:
            lines.append(f"--docker applies to: {', '.join(optional)}. Any other "
                         "preset has no buildenv\nimage and the flag is an error.")
            lines.append("A host build and a --docker build cannot share a build "
                         "directory:\nthe CMake cache records absolute paths, and "
                         f"the container sees the\ntree at {cfg.docker.workdir}. "
                         "Use --docker in CI or a clean checkout.")
        elif cfg.docker.presets:
            lines.append("So passing --docker never changes a build here: for the "
                         "presets above\nit is redundant, and for every other "
                         "preset it is an error (no buildenv\nimage). It exists "
                         "for projects that declare an optional buildenv.")
        epilog = "\n\n".join(lines) or None
    else:
        # Declared so the flag reports why it can't work, rather than argparse
        # calling it an unrecognised argument.
        args.append(arg("--docker", action="store_true",
                        help="(not configured — needs a [build.docker] section)"))
    return Node("build", "configure, build and test with CMake", func=build,
                args=args, epilog=epilog)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
