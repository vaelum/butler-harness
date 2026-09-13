"""Paths, and the artifacts that land in them."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from . import ui

# Every installable the toolchains here can produce. Used to sweep a build's
# output tree into dist/ without knowing which bundler ran.
INSTALLER_SUFFIXES = (
    ".deb", ".appimage", ".rpm",      # linux
    ".msi", ".exe",                   # windows
    ".dmg", ".app.tar.gz",            # macOS
    ".apk", ".aab",                   # android
)


def disp(path: Path | str, root: Path) -> str:
    """Path for display: repo-relative inside the tree, absolute outside it.

    The absolute case is not a fallback — the Android keystore vault genuinely
    lives outside the repo, and printing a `../../..` chain for it helps nobody.
    """
    p = Path(path)
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p)


def collect_artifacts(
    roots: Iterable[Path],
    dest: Path,
    project_root: Path,
    *,
    suffixes: tuple[str, ...] = INSTALLER_SUFFIXES,
) -> list[Path]:
    """Copy every installer found under `roots` into `dest`. Returns what landed."""
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and any(p.name.lower().endswith(s) for s in suffixes):
                out = dest / p.name
                shutil.copy2(p, out)
                copied.append(out)
    report_copied(copied, project_root)
    return copied


def report_copied(copied: list[Path], project_root: Path, label: str = "copied to dist/") -> None:
    if copied:
        ui.plain()
        ui.ok(label)
        for d in copied:
            ui.plain(f"  {disp(d, project_root)}  ({d.stat().st_size:,} bytes)")
    else:
        ui.plain()
        ui.note("no installer artifacts found to copy into dist/.")


def newest(paths: Iterable[Path]) -> Path | None:
    """The most recently modified of `paths`, or None if there are none."""
    items = [p for p in paths if p.is_file()]
    return max(items, key=lambda p: p.stat().st_mtime) if items else None


def clear_dir(path: Path, keep: Iterable[str] = ()) -> None:
    """Empty a regenerable directory in place, optionally keeping named entries.

    In place rather than rmtree+mkdir because these are often bind-mount or
    tmpfs targets, and replacing the directory itself breaks the mount.
    """
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        return
    keep = set(keep)
    for entry in path.iterdir():
        if entry.name in keep:
            continue
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
