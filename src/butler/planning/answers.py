"""The answers file: what the user decided, ticked, approved and said.

`planning/answers/<id>.json`, one per item, keyed by the item's id so that a
move or a rename of the item moves nothing here. The service is its writer: it
changes one entry per request, under a lock, and replaces the file atomically,
so two tabs (or two repositories) never overwrite each other. The two butler
commands that also write it — `resolve` and `convert` — go through `update`
too. Agents read it through `status` and `gate`, never by hand.

    {
      "format": 1,
      "item": "p-3p758x",
      "decisions": {"<local>": {"selected": [...], "text": "", "at": "…", "source"?: "…"}},
      "steps":     {"<local>": {"state": "done" | "open", "at": "…"}},
      "approvals": {"<local>": {"verdict": "approved" | "disapproved", "note": "",
                                "at": "…", "sha256": "…", "snapshot": {...}}},
      "plan_approval": {"verdict": …, "note": "", "at": "…", "sha256": "…", "snapshot": [...]},
      "comments":  [{"id": "c1", "on": "<local or ''>", "text": "…", "at": "…",
                     "resolved"?: {"at": "…", "note": "…"}}]
    }
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .types import FORMAT

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def empty(item: str) -> dict:
    return {"format": FORMAT, "item": item, "decisions": {}, "steps": {}, "approvals": {},
            "comments": []}


def read(path: Path, item: str = "") -> dict:
    """The answers at `path`, or an empty set. A file that is not JSON reads as
    empty with an `_error` key, which `check` reports."""
    base = empty(item)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return base
    except (OSError, ValueError) as e:
        base["_error"] = str(e)
        return base
    if not isinstance(data, dict):
        base["_error"] = "not a JSON object"
        return base
    for key, value in base.items():
        data.setdefault(key, value)
    return data


@contextmanager
def _locked(path: Path):
    key = str(path.resolve())
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        # And across processes: a `planning resolve` in a terminal while the
        # service writes the same file. POSIX only, like the service itself.
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            yield
            return
        with open(path.parent / f".{path.name}.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def write(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # mkstemp makes the file 0600; answers are committed like any other file.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(path: Path, item: str, change: Callable[[dict], None]) -> dict:
    """Read, apply `change` to one part, write — all under the file's lock."""
    with _locked(path):
        data = read(path, item)
        data.pop("_error", None)
        data["item"] = item
        change(data)
        write(path, data)
        return data


def next_comment_id(data: dict) -> str:
    n = 0
    for c in data.get("comments", []):
        cid = str(c.get("id", ""))
        if cid.startswith("c") and cid[1:].isdigit():
            n = max(n, int(cid[1:]))
    return f"c{n + 1}"


def answered(entry: dict | None) -> bool:
    return bool(entry) and (bool(entry.get("selected")) or bool((entry.get("text") or "").strip()))
