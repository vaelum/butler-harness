"""A planning folder, read from disk: its settings, its items, and its answers.

Reading never raises on bad content. Everything wrong with a file is collected
as a Problem on it, so `check` can report all of them at once and the service
can still show the rest of a folder while one file is half-written.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import answers as answers_mod
from .types import (ANSWERS_FOLDER, CONFIG_FILE, NOTES_FOLDER, SCHEMA_FOLDER, STATE_FOLDERS,
                    Problem, load, load_config)

_TOML_LINE = re.compile(r"\(at line (\d+), column (\d+)\)")


@dataclass
class Item:
    path: Path
    rel: str                      # relative to the planning folder
    folder: str                   # draft | todo | in-progress | done | notes
    text: str = ""
    data: dict = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    parsed: bool = False

    @property
    def id(self) -> str:
        v = self.data.get("id")
        return v if isinstance(v, str) else ""

    @property
    def kind(self) -> str:
        v = self.data.get("kind")
        return v if isinstance(v, str) else ""

    @property
    def title(self) -> str:
        v = self.data.get("title")
        return v if isinstance(v, str) else self.path.stem

    @property
    def name(self) -> str:
        """The file's name inside its folder, without `.toml`: for humans only."""
        return self.rel.split("/", 1)[1][:-5] if "/" in self.rel else self.rel[:-5]

    # ---- the parts ----------------------------------------------------------- #

    def decisions(self) -> list[dict]:
        """Every decision in the item. A standalone decision is its own only
        decision, with the item's id as its local id."""
        if self.kind == "decision":
            return [self.data]
        return [d for d in self.data.get("decision", []) if isinstance(d, dict)]

    def phases(self) -> list[dict]:
        return [p for p in self.data.get("phase", []) if isinstance(p, dict)]

    def steps(self) -> list[dict]:
        return [s for p in self.phases() for s in p.get("step", []) if isinstance(s, dict)]

    def findings(self) -> list[dict]:
        return [f for f in self.data.get("finding", []) if isinstance(f, dict)]

    def sections(self) -> list[dict]:
        return [s for s in self.data.get("section", []) if isinstance(s, dict)]

    def locals(self) -> dict[str, str]:
        """local id -> what it names (decision, phase, step, finding, section)."""
        out: dict[str, str] = {}
        for d in self.decisions():
            if isinstance(d.get("id"), str):
                out[d["id"]] = "decision"
        for p in self.phases():
            if isinstance(p.get("id"), str):
                out[p["id"]] = "phase"
        for s in self.steps():
            if isinstance(s.get("id"), str):
                out[s["id"]] = "step"
        for f in self.findings():
            if isinstance(f.get("id"), str):
                out[f["id"]] = "finding"
        for s in self.sections():
            if isinstance(s.get("id"), str):
                out[s["id"]] = "section"
        return out

    def find(self, local: str) -> dict | None:
        for group in (self.decisions(), self.phases(), self.steps(), self.findings(), self.sections()):
            for x in group:
                if x.get("id") == local:
                    return x
        return None

    def line_of(self, problem: Problem) -> int | None:
        """Best-effort line for a problem: its own, the line of the local id it
        names, or the line of its top-level key."""
        if problem.line:
            return problem.line
        lines = self.text.splitlines()
        if problem.local:
            pat = re.compile(r'^\s*id\s*=\s*"' + re.escape(problem.local) + r'"')
            for i, line in enumerate(lines, 1):
                if pat.match(line):
                    return i
        if problem.where:
            key = re.split(r"[.\[]", problem.where)[0]
            pat = re.compile(r"^\s*\[{0,2}" + re.escape(key) + r"\b")
            for i, line in enumerate(lines, 1):
                if pat.match(line):
                    return i
        return None


@dataclass
class Folder:
    root: Path                     # the planning folder
    repo: Path                     # the repository it belongs to
    config: dict | None = None     # planning.toml, loaded; None if absent
    config_problems: list[Problem] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    legacy: list[str] = field(default_factory=list)    # Markdown plans, relative
    forms: list[str] = field(default_factory=list)     # the prototype's HTML forms
    stray: list[str] = field(default_factory=list)
    answer_files: dict[str, Path] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    @property
    def typed(self) -> bool:
        return self.config is not None

    @property
    def user(self) -> str:
        return (self.config or {}).get("user") or "the user"

    @property
    def plan_approval(self) -> bool:
        return bool((self.config or {}).get("plan_approval", True))

    def by_id(self) -> dict[str, Item]:
        out: dict[str, Item] = {}
        for it in self.items:
            if it.id and it.id not in out:
                out[it.id] = it
        return out

    def get(self, item_id: str) -> Item | None:
        return self.by_id().get(item_id)

    def answers_path(self, item_id: str) -> Path:
        return self.root / ANSWERS_FOLDER / f"{item_id}.json"

    def answers(self, item_id: str) -> dict:
        return answers_mod.read(self.answers_path(item_id), item_id)


def _parse_item(path: Path, root: Path, folder: str) -> Item:
    rel = path.relative_to(root).as_posix()
    it = Item(path=path, rel=rel, folder=folder)
    try:
        it.text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        it.problems.append(Problem("error", f"cannot read: {e}"))
        return it
    try:
        raw = tomllib.loads(it.text)
    except tomllib.TOMLDecodeError as e:
        msg = str(e)
        m = _TOML_LINE.search(msg)
        line = int(m.group(1)) if m else None
        hint = ""
        if "'''" in it.text:
            hint = " (a prose field in ''' … ''' cannot itself contain three single quotes)"
        it.problems.append(Problem("error", f"not valid TOML: {_TOML_LINE.sub('', msg).strip()}{hint}",
                                   line=line))
        return it
    loaded = load(raw)
    it.data, it.parsed = loaded.data, True
    it.problems.extend(loaded.problems)
    return it


def read(root: Path, repo: Path | None = None) -> Folder:
    """Everything under `root` (a planning folder). Missing is not an error here."""
    root = root.resolve()
    f = Folder(root=root, repo=(repo or root.parent).resolve())
    if not root.is_dir():
        return f

    cfg = root / CONFIG_FILE
    if cfg.is_file():
        try:
            loaded = load_config(tomllib.loads(cfg.read_text(encoding="utf-8")))
            f.config = loaded.data
            f.config_problems = loaded.problems
        except (OSError, tomllib.TOMLDecodeError) as e:
            f.config = {}
            f.config_problems = [Problem("error", f"{CONFIG_FILE} is not valid TOML: {e}")]

    for entry in sorted(root.iterdir()):
        name = entry.name
        if name in (CONFIG_FILE, "README.md", SCHEMA_FOLDER) or name.startswith("."):
            continue
        if name == ANSWERS_FOLDER and entry.is_dir():
            for a in sorted(entry.glob("*.json")):
                f.answer_files[a.stem] = a
            continue
        if entry.is_dir() and name in (*STATE_FOLDERS, NOTES_FOLDER):
            for p in sorted(entry.rglob("*")):
                if not p.is_file() or p.name.startswith("."):
                    continue
                rel = p.relative_to(root).as_posix()
                if p.suffix == ".toml":
                    f.items.append(_parse_item(p, root, name))
                elif p.suffix == ".md":
                    f.legacy.append(rel)
                elif p.suffix == ".html" or p.name.endswith(".answers.json"):
                    f.forms.append(rel)
                else:
                    f.stray.append(rel)
            continue
        f.stray.append(name + ("/" if entry.is_dir() else ""))
    return f
