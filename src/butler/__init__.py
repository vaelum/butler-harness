"""butler — the shared harness behind each project's butler.py.

Everything a project's butler_tasks.py needs is importable from here:

    from butler import task, arg, ButlerError, ui
"""

from __future__ import annotations

from . import proc, ui
from .command import Arg, arg
from .context import Ctx
from .errors import ButlerError, ConfigError, MissingToolError
from .tasks import task

__version__ = "0.8.1"

__all__ = [
    "task", "arg", "Arg", "Ctx",
    "ButlerError", "ConfigError", "MissingToolError",
    "proc", "ui", "__version__",
]
