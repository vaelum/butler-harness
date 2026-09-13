"""Getting the source tree complete before anything compiles it.

Submodules and `git_deps` are declared once in `[project]` and refreshed by
every command that builds — a CMake build, a Tauri desktop build, an Android
build. A dependency that a build expects and doesn't find fails a long way from
its cause: deep inside CMake, or inside a Rust build script that shells out to
CMake, with a message about a missing directory rather than a missing checkout.
"""

from __future__ import annotations

from . import vcs
from .context import Ctx


def prepare(ctx: Ctx) -> None:
    """Refresh whatever the project declares. A no-op when it declares nothing,
    which is why it is safe to call from every build path."""
    project = ctx.cfg.project
    if project.git_deps:
        vcs.ensure_git_deps(ctx.root, project.git_deps, dry_run=ctx.dry_run)
    if project.submodules:
        vcs.update_submodules(ctx.root, dry_run=ctx.dry_run)
