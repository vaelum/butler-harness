"""Running a build inside a container image the project ships.

Split out from `cmake` because nothing here is CMake-specific: build an image
from a Dockerfile in the repo, then run a shell script inside it against the
mounted working tree, as the invoking user. It is the reproducible path CI takes
and the one that needs nothing on the host but Docker.
"""

from __future__ import annotations

import os
import uuid

from .. import proc
from ..config import ResolvedBuildenv
from ..context import Ctx
from ..errors import ButlerError


def ensure_image(ctx: Ctx, cfg: ResolvedBuildenv) -> str:
    """Build (or refresh) the buildenv image and return its tag.

    USER_ID/GROUP_ID are build args rather than a plain `--user` because these
    images create a matching user at build time; everything the build writes
    into the mounted tree then belongs to the person who ran it, instead of
    root-owned artifacts that the next host build cannot overwrite.
    """
    if not cfg.dockerfile.is_file():
        raise ButlerError(f"Dockerfile not found: {ctx.disp(cfg.dockerfile)}",
                          hint="Check [build.docker] dockerfile in butler.toml.")
    proc.check(["docker", "build", "-t", cfg.tag, "-f", cfg.dockerfile,
                "--build-arg", f"USER_ID={os.getuid()}",
                "--build-arg", f"GROUP_ID={os.getgid()}",
                cfg.context],
               cwd=ctx.root, dry_run=ctx.dry_run, what="docker build (buildenv)")
    return cfg.tag


def run_in(ctx: Ctx, cfg: ResolvedBuildenv, image: str, script: str, *,
           extra_args: list[str] | None = None, name: str = "buildenv") -> None:
    """Run `script` under `sh -c` inside the image, with the tree mounted.

    The container is named uniquely and `--rm`'d: several builds (a local one
    and a CI one, or two presets) must be able to run at once without the second
    failing on a name clash or reaping the first.
    """
    container = f"{name}-{uuid.uuid4().hex[:8]}"
    cmd = [
        "docker", "run", "--rm",
        "--name", container,
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{ctx.root}:{cfg.workdir}",
        "-w", cfg.workdir,
        *(extra_args or []),
        image, "bash", "-c", script,
    ]
    proc.check(cmd, cwd=ctx.root, dry_run=ctx.dry_run, what="containerised build")
