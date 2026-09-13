"""The escape hatch: project-defined tasks in butler_tasks.py.

Config covers what several projects share. This covers what exactly one project
does — a bespoke dev container, chords' OpenRouter key management, a
throwaway pre-authenticated test environment. Those
never belong in the harness, but they do belong under the same `butler.py` and
the same `--help`.

Two forms:

    @task("server.claim-token", help="print the first-run claim token")
    def claim_token(ctx):
        ...

    @task("app.android.build", wraps=True)
    def before_apk(ctx, args, inner):
        ctx.check(["scripts/pre-apk.sh"])
        return inner()

Handlers are introspected by parameter NAME, so declare only what you use:
`ctx` is always first; add `args` for the parsed flags, and `inner` (wrapping
tasks only) for the built-in you replaced.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .command import Arg
from .errors import ButlerError


@dataclass
class Task:
    path: list[str]
    func: Callable
    help: str = ""
    args: list[Arg] = field(default_factory=list)
    wraps: bool = False


_registry: list[Task] = []


def task(name: str, *, help: str = "", args: list[Arg] | None = None,
         wraps: bool = False):
    """Register a task at a dotted command path ('app.android.build').

    Without `wraps`, an existing built-in at that path is replaced outright.
    With it, the built-in is passed to the handler as `inner`.
    """
    path = [p for p in name.split(".") if p]
    if not path:
        raise ButlerError(f"@task name {name!r} is empty")

    def decorate(func: Callable) -> Callable:
        _registry.append(Task(path=path, func=func, help=help or (func.__doc__ or "").strip().split("\n")[0],
                              args=list(args or []), wraps=wraps))
        return func

    return decorate


def registered() -> list[Task]:
    return list(_registry)


def clear() -> None:
    """Drop every registered task (tests, and re-loading a different project)."""
    _registry.clear()


def load(root: Path) -> list[Task]:
    """Import this project's butler_tasks.py, if it has one.

    The project root goes on sys.path so the tasks module can import
    project-local helpers (`from scripts.release import notes`) the same way any
    script in the repo would — but APPENDED, never prepended. A project keeps
    its config in a directory called `butler/`, and prepending the root would
    let that directory shadow this very package, so `from butler import task`
    inside the tasks file would import the project's config folder instead.
    """
    from .config import TASKS_NAME, tasks_path

    path = tasks_path(root)
    if path is None:
        return []

    if str(root) not in sys.path:
        sys.path.append(str(root))

    spec = importlib.util.spec_from_file_location("butler_tasks", path)
    if spec is None or spec.loader is None:
        raise ButlerError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["butler_tasks"] = module
    try:
        spec.loader.exec_module(module)
    except ButlerError:
        raise
    except Exception as e:
        # A broken tasks file must not look like a broken harness.
        raise ButlerError(f"{TASKS_NAME} raised {type(e).__name__}: {e}",
                          hint=f"Fix {path}, or move it aside to run without it.") from e
    return registered()


def invoke(func: Callable, ctx, args: Any, inner: Callable | None = None) -> int:
    """Call a handler, passing only the parameters it actually declares."""
    params = inspect.signature(func).parameters
    kwargs: dict[str, Any] = {}
    if "args" in params:
        kwargs["args"] = args
    if inner is not None and "inner" in params:
        kwargs["inner"] = inner
    result = func(ctx, **kwargs)
    return 0 if result is None else int(result)
