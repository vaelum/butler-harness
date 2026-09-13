"""Docker Compose helpers.

Thin on purpose — `docker compose` is already a good CLI. What's here is the
handful of invocations every project repeated, and one thing worth centralising:
reading a value back out of a service's logs.
"""

from __future__ import annotations

from pathlib import Path

from .. import proc
from ..context import Ctx


def base(file: str | None = None) -> list[str]:
    """`docker compose`, pointed at a non-default compose file if the project
    keeps one (docker/docker-compose.yml and the like are common enough)."""
    return ["docker", "compose"] + (["-f", file] if file else [])


def up(ctx: Ctx, dir: Path, *, detach: bool = False, build: bool = True,
       file: str | None = None) -> int:
    cmd = base(file) + ["up"]
    if detach:
        cmd.append("-d")
    if build:
        cmd.append("--build")
    return ctx.run(cmd, cwd=dir)


def down(ctx: Ctx, dir: Path, *, volumes: bool = False, file: str | None = None) -> int:
    cmd = base(file) + ["down"]
    if volumes:
        cmd.append("-v")
    return ctx.run(cmd, cwd=dir)


def logs(ctx: Ctx, dir: Path, service: str, *, follow: bool = False,
         file: str | None = None) -> int:
    cmd = base(file) + ["logs"]
    if follow:
        cmd.append("-f")
    return ctx.run(cmd + [service], cwd=dir)


def logs_text(ctx: Ctx, dir: Path, service: str, *, file: str | None = None) -> str:
    return proc.capture(base(file) + ["logs", service], cwd=dir).combined


def last_log_value(ctx: Ctx, dir: Path, service: str, prefix: str) -> str | None:
    """The text after the last occurrence of `prefix` in a service's logs.

    This is how the one-time claim token these servers print on first boot gets
    surfaced without the user scrolling through container output.
    """
    lines = [ln for ln in logs_text(ctx, dir, service).splitlines() if prefix in ln]
    if not lines:
        return None
    return lines[-1].split(prefix, 1)[1].strip()
