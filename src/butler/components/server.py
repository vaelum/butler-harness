"""The `server` component: a FastAPI service, run locally or in Docker Compose."""

from __future__ import annotations

import sys

from .. import ui
from ..command import Node, arg
from ..config import ServerConfig
from ..context import Ctx
from ..errors import ButlerError
from . import compose, deploy as deploy_mod


def _server(ctx: Ctx) -> ServerConfig:
    if ctx.cfg.server is None:
        raise ButlerError("this project has no [server] configuration")
    return ctx.cfg.server


def python_for(cfg: ServerConfig) -> str:
    """The project venv's interpreter if it exists, else the current one.

    butler deliberately does NOT create the venv: the dependency set is the
    project's business, and silently building one hides a missing install until
    something imports differently in CI.
    """
    if cfg.venv:
        candidate = cfg.venv / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return sys.executable


def run(ctx: Ctx, args) -> int:
    """Local dev without Docker: uvicorn --reload against the working tree."""
    cfg = _server(ctx)
    return ctx.run([python_for(cfg), "-m", "uvicorn", cfg.module,
                    "--reload", "--host", "127.0.0.1", "--port", str(args.port or cfg.port)],
                   cwd=cfg.dir)


def dev(ctx: Ctx, args) -> int:
    cfg = _server(ctx)
    return compose.up(ctx, cfg.dir, detach=args.detach, file=cfg.compose_file)


def down(ctx: Ctx, args) -> int:
    cfg = _server(ctx)
    return compose.down(ctx, cfg.dir, file=cfg.compose_file)


def logs(ctx: Ctx, args) -> int:
    cfg = _server(ctx)
    return compose.logs(ctx, cfg.dir, cfg.service, follow=not args.no_follow,
                        file=cfg.compose_file)


def test(ctx: Ctx, args) -> int:
    cfg = _server(ctx)
    return ctx.run([python_for(cfg), *cfg.test, *args.extra], cwd=cfg.dir)


def deploy(ctx: Ctx, args) -> int:
    return deploy_mod.deploy(ctx, _server(ctx))


def reset(ctx: Ctx, args) -> int:
    """Wipe local server storage: the Compose volumes and the sqlite files.

    Irreversible, and it un-claims the instance, so it demands the word rather
    than a y/N — a mistyped `y` should not be able to do this.
    """
    cfg = _server(ctx)
    if not ctx.confirm("", expect="wipe"):
        ui.plain("aborted")
        return 1

    compose.down(ctx, cfg.dir, volumes=True, file=cfg.compose_file)
    data_dir = cfg.dir / cfg.data_dir
    files = sorted(data_dir.glob(f"{cfg.db_file}*")) if cfg.db_file and data_dir.is_dir() else []
    if ctx.would(f"delete {len(files)} db file(s) under {ctx.disp(data_dir)}"):
        return 0
    removed = []
    for f in files:
        f.unlink()
        removed.append(f.name)
    ui.ok("cleared", f"— local db files removed: {', '.join(removed) or 'none'}")
    return 0


def _script_handler(script):
    def handler(ctx: Ctx, args) -> int:
        return ctx.run(script.cmd, cwd=_server(ctx).dir)

    return handler


def node(cfg: ServerConfig) -> Node:
    children = [
        Node("run", "local uvicorn --reload (no Docker)", func=run,
             args=[arg("--port", type=int, help=f"override the port (default {cfg.port})")]),
        Node("test", "run the server test suite", func=test,
             args=[arg("extra", nargs="*", help="extra arguments passed to the test command")]),
    ]
    if cfg.compose:
        port = f" (host port {cfg.dev_port})" if cfg.dev_port else ""
        children[1:1] = [
            Node("dev", f"Docker Compose stack{port}", func=dev,
                 args=[arg("-d", "--detach", action="store_true",
                           help="start in the background and return")]),
            Node("down", "stop the Docker Compose stack", func=down),
            Node("logs", "follow the server logs", func=logs,
                 args=[arg("--no-follow", action="store_true", help="print and exit")]),
        ]
    if cfg.deploy:
        children.append(Node("deploy", "back up, rsync and restart on the deploy host",
                             func=deploy))
    if cfg.compose and cfg.db_file:
        children.append(Node("reset", "WIPE all local data (Compose volumes + sqlite)",
                             func=reset))

    node = Node("server", "backend service", children=children)
    # Declared scripts are added last and REPLACE a same-named built-in: a
    # project that says how to do something has said so on purpose.
    for script in cfg.scripts:
        node.add(Node(script.name, script.help, func=_script_handler(script)))
    return node
