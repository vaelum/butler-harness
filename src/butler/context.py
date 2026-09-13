"""The object every action receives.

Ctx is the seam between the harness and a project: it carries the resolved
config and the run helpers, so a task in butler_tasks.py can do everything a
built-in component does without importing half the package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import paths, proc, ui
from .config import Config

Command = Sequence[str | Path]


@dataclass
class Ctx:
    cfg: Config
    dry_run: bool = False
    verbose: bool = False
    assume_yes: bool = False
    # Set by the CLI before dispatch, so a task can reach flags it didn't
    # declare (mostly useful for `wraps=True` tasks).
    args: object | None = field(default=None, repr=False)

    # ---- shortcuts into the config ---------------------------------------- #

    @property
    def root(self) -> Path:
        return self.cfg.root

    @property
    def name(self) -> str:
        return self.cfg.project.name

    @property
    def dist(self) -> Path:
        return self.cfg.dist_dir

    # ---- running things --------------------------------------------------- #

    def run(self, cmd: Command, cwd: str | Path | None = None, **kw) -> int:
        return proc.run(cmd, cwd if cwd is not None else self.root,
                        dry_run=self.dry_run, **kw)

    def check(self, cmd: Command, cwd: str | Path | None = None, **kw) -> None:
        proc.check(cmd, cwd if cwd is not None else self.root,
                   dry_run=self.dry_run, **kw)

    def capture(self, cmd: Command, cwd: str | Path | None = None, **kw) -> proc.Result:
        return proc.capture(cmd, cwd if cwd is not None else self.root, **kw)

    def popen(self, cmd: Command, cwd: str | Path | None = None, **kw):
        return proc.popen(cmd, cwd if cwd is not None else self.root, **kw)

    def confirm(self, question: str, *, expect: str | None = None) -> bool:
        return proc.confirm(question, assume_yes=self.assume_yes, expect=expect)

    def would(self, description: str) -> bool:
        """Guard a filesystem mutation that isn't a subprocess call.

        --dry-run is handled inside `run`, so anything butler does *itself* —
        deleting a database, writing a .desktop file, copying icons into the
        gen tree — needs this or the flag would be a lie.
        """
        if self.dry_run:
            ui.plain(ui.dim(f"[dry-run] would {description}"))
        return self.dry_run

    # ---- paths ------------------------------------------------------------ #

    def disp(self, path: Path | str) -> str:
        return paths.disp(path, self.root)

    def collect(self, roots, dest: Path | None = None) -> list[Path]:
        return paths.collect_artifacts(roots, dest or self.dist, self.root)
