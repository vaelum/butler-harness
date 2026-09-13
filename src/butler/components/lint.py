"""The `check` component: MegaLinter over the tree, clang-tidy over the sources.

Both run against the whole project rather than a diff, and both are local-first:
MegaLinter is driven as a container directly, so no node/npx is needed on the
host, and clang-tidy consumes the compile database a normal configured build
already produces.
"""

from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace

from .. import paths, proc, ui
from ..command import Node, arg
from ..config import CheckConfig, LintConfig, TidyConfig
from ..context import Ctx
from ..errors import ButlerError
from . import cmake as cmake_component
from . import docker_build


def _check(ctx: Ctx) -> CheckConfig:
    if ctx.cfg.check is None:
        raise ButlerError("this project has no [check] configuration")
    return ctx.cfg.check


# --------------------------------------------------------------------------- #
# MegaLinter
# --------------------------------------------------------------------------- #

def _reports_dir(ctx: Ctx, cfg: LintConfig):
    """The reports directory, pre-created as the invoking user — and emptied
    first when the project asks for it.

    A run of the raw image without `--user` (mega-linter-runner does this)
    leaves a root-owned directory behind that the non-root container below then
    cannot write into. Failing here names the problem and the fix; failing
    inside the container is an opaque PermissionError halfway through a linter.

    Clearing matters for a different reason: MegaLinter reuses the directory in
    place, and the project-scope scanners walk the workspace including it — so
    last run's SARIF is read back and re-counted, and findings that were fixed
    keep resurfacing. `keep` preserves another stage's artifact living there.
    """
    reports = ctx.root / cfg.reports
    if reports.is_dir() and not os.access(reports, os.W_OK):
        raise ButlerError(
            f"{ctx.disp(reports)} is not writable",
            hint="Root-owned from a run without --user? Remove it and retry:\n"
                 f"  sudo rm -rf {reports}")
    what = ("empty" if cfg.clear and reports.is_dir() else "create")
    if not ctx.would(f"{what} {ctx.disp(reports)}"):
        if cfg.clear:
            paths.clear_dir(reports, keep=cfg.keep)
        reports.mkdir(parents=True, exist_ok=True)
    return reports


def lint(ctx: Ctx, args) -> int:
    cfg = _check(ctx).lint
    if cfg is None:
        raise ButlerError("this project has no [check.lint] configuration")
    if not (ctx.root / cfg.config).is_file():
        raise ButlerError(f"MegaLinter config not found: {cfg.config}",
                          hint="Check [check.lint] config in butler.toml.")

    reports = _reports_dir(ctx, cfg)

    if cfg.dockerfile is not None:
        if not cfg.dockerfile.is_file():
            raise ButlerError(f"Dockerfile not found: {ctx.disp(cfg.dockerfile)}",
                              hint="Check [check.lint] dockerfile in butler.toml.")
        # A thin local wrapper over the pinned upstream image (it fixes tool
        # permissions for non-root runs). Cached after the first build; only the
        # FROM pull is slow, and linter versions move only when that tag is
        # bumped deliberately.
        proc.check(["docker", "build", "-t", cfg.image, "-f", cfg.dockerfile,
                    cfg.context or cfg.dockerfile.parent],
                   cwd=ctx.root, dry_run=ctx.dry_run, what="docker build (lint image)")

    cmd = [
        "docker", "run", "--rm",
        "--name", f"{ctx.name}-lint-{uuid.uuid4().hex[:8]}",
        # Match the build containers: run as the host user, so the reports and
        # any --fix rewrites stay owned by whoever ran this. HOME must then
        # point somewhere writable for the linters' caches (semgrep rules,
        # trivy DB, npm, ...).
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp",
        # ruff ignores HOME and defaults its cache to <workspace>/.ruff_cache,
        # i.e. into the mounted repo — which pollutes the tree and then fails
        # when a previous root-owned cache is there. Send it somewhere ephemeral.
        "-e", "RUFF_CACHE_DIR=/tmp/.ruff_cache",
        "-v", f"{ctx.root}:{cfg.workspace}:rw",
        "-e", f"DEFAULT_WORKSPACE={cfg.workspace}",
        # MegaLinter only looks for .mega-linter.yml at the workspace root, so a
        # config kept elsewhere is passed explicitly.
        "-e", f"MEGALINTER_CONFIG={cfg.config}",
    ]
    if cfg.reports != "megalinter-reports":
        # Land the reporter output (megalinter.log, SARIF, linters_logs/…) where
        # the project keeps it, rather than in MegaLinter's default directory at
        # the workspace root.
        cmd += ["-e", f"REPORT_OUTPUT_FOLDER={cfg.workspace}/{cfg.reports}"]
    # Overlay an empty tmpfs on each heavy tree so *every* linter, however it
    # enumerates files, sees them empty. Only ones that exist, so linting never
    # creates phantom directories in the working tree.
    for tree in cfg.hide:
        if (ctx.root / tree).is_dir():
            cmd += ["--tmpfs", f"{cfg.workspace}/{tree}"]
    if args.fix:
        cmd += ["-e", "APPLY_FIXES=all"]
    if args.linters:
        cmd += ["-e", f"ENABLE_LINTERS={args.linters}"]
    cmd.append(cfg.image)

    rc = ctx.run(cmd)
    ui.plain()
    (ui.ok if rc == 0 else ui.warn)("lint", f"— reports in {ctx.disp(reports)}")
    return rc


# --------------------------------------------------------------------------- #
# clang-tidy
# --------------------------------------------------------------------------- #

def _build_args(ctx: Ctx, cfg: TidyConfig, args) -> SimpleNamespace:
    """A `build` invocation shaped like the one that produces the compile DB.

    Build options are forwarded from this command's own flags: tidying a tree
    configured with a feature off, using a database that had it on, reports on
    code the build never compiled.
    """
    build = ctx.cfg.build
    assert build is not None
    return SimpleNamespace(
        preset=cfg.preset or build.default_preset,
        test=False, test_filter=None, docker=False,
        **{o.dest: getattr(args, o.dest, o.default) for o in build.options},
    )


def tidy(ctx: Ctx, args) -> int:
    cfg = _check(ctx).tidy
    if cfg is None:
        raise ButlerError("this project has no [check.tidy] configuration")
    build = ctx.cfg.build
    if build is None:
        raise ButlerError("[check.tidy] needs a [build] section",
                          hint="clang-tidy reads the compile database a "
                               "configured CMake build writes.")

    build_args = _build_args(ctx, cfg, args)
    # Build first (no tests) to generate or refresh the compile database.
    cmake_component.build(ctx, build_args)

    preset = build_args.preset
    driver = ctx.root / cfg.driver
    if not driver.is_file():
        raise ButlerError(f"clang-tidy driver not found: {cfg.driver}",
                          hint="Check [check.tidy] driver in butler.toml.")

    db_rel = f"{build.build_dir.format(preset=preset)}/{cfg.db}"
    if cfg.docker:
        # In the buildenv, because the database references that container's
        # toolchain and headers: the host's clang-tidy would be reading include
        # paths that exist only inside the image.
        buildenv = cmake_component.buildenv_for(ctx, build, preset)
        script = " ".join(["python3", cfg.driver, db_rel, *cfg.args, *args.extra])
        docker_build.run_in(ctx, buildenv, buildenv.tag, script,
                            name=f"{ctx.name}-tidy")
        return 0

    db = build.preset_dir(preset) / cfg.db
    if not ctx.dry_run and not db.is_file():
        raise ButlerError(f"no compile database at {ctx.disp(db)}",
                          hint="Set CMAKE_EXPORT_COMPILE_COMMANDS=ON for this preset.")
    return ctx.run([sys.executable, driver, db, *cfg.args, *args.extra])


# --------------------------------------------------------------------------- #
# the node
# --------------------------------------------------------------------------- #

def _stage(name: str, run, ctx: Ctx) -> int:
    """One checker, whose failure is a result rather than an abort.

    A red stage must not stop the stages after it, or the report that folds them
    together — a findings report is most useful exactly when there are findings.
    """
    try:
        return run()
    except ButlerError as e:
        ui.warn(f"{name}:", e.message)
        return e.code or 1


def run_all(ctx: Ctx, args) -> int:
    """Bare `check`: every configured checker, then the report. Worst code wins."""
    cfg = _check(ctx)
    rc = 0
    if cfg.lint is not None:
        rc = max(rc, _stage("lint", lambda: lint(
            ctx, SimpleNamespace(fix=False, linters=None)), ctx))
    if cfg.tidy is not None:
        extras = {o.dest: getattr(args, o.dest, o.default)
                  for o in (ctx.cfg.build.options if ctx.cfg.build else [])}
        rc = max(rc, _stage("clang-tidy", lambda: tidy(
            ctx, SimpleNamespace(extra=[], **extras)), ctx))
    if cfg.report:
        report(ctx, args)
    if rc:
        ui.warn("check", "finished with findings — see the report above.")
    return rc


def report(ctx: Ctx, args) -> int:
    """Rebuild the combined report from whatever stage artifacts are current.

    Each stage refreshes only its own artifact, so this folds in the latest of
    each — which is what makes a lint-only run still show the last tidy results.
    """
    cfg = _check(ctx)
    if not cfg.report:
        raise ButlerError("this project has no [check] report command")
    cmd = list(cfg.report)
    if cmd[0] == "python":
        # The harness runs in its own virtualenv; "python" here means the
        # interpreter butler is running under, not whatever is on PATH.
        cmd[0] = sys.executable
    return ctx.run(cmd)


def node(cfg: CheckConfig, build) -> Node:
    children = []
    if cfg.lint is not None:
        children.append(Node(
            "lint", "MegaLinter over the whole tree (Docker)", func=lint,
            args=[
                arg("--fix", action="store_true",
                    help="let fix-capable linters rewrite the working tree"),
                arg("--linters",
                    help="comma-separated MegaLinter keys to run only a subset "
                         "(e.g. SPELL_CSPELL)"),
            ]))
    if cfg.tidy is not None:
        where = " in the buildenv container" if cfg.tidy.docker else ""
        options = cmake_component.option_args(build) if build else []
        children.append(Node(
            "tidy", f"build, then run clang-tidy over the sources{where}", func=tidy,
            args=[*options,
                  arg("extra", nargs="*",
                      help="extra arguments passed to the clang-tidy driver")]))
    if cfg.report:
        children.append(Node("report", "rebuild the combined report from the "
                                       "current stage results", func=report))
    return Node("check", "static analysis", func=run_all, children=children)
