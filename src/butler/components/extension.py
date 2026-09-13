"""The `extension` component: zip a browser extension for Chrome and Firefox.

One source tree, two bundles. The only differences between them are the two
things Firefox needs and Chrome rejects, so rather than maintaining a second
manifest that will drift, the Firefox manifest is derived from the Chrome one
at package time:

  * `background` — Firefox runs an event page (`scripts`), not a Chrome service
    worker. The same background.js loads unchanged, provided it registers its
    listeners at top level and persists state, which it must already do to
    survive a service-worker restart.
  * `browser_specific_settings.gecko` — a stable add-on id, so storage survives
    updates and Mozilla can sign it later, plus a minimum Firefox version.

Everything else (permissions, icons, action, options_page) is identical, and is
therefore never duplicated.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Callable

from .. import ui, vcs
from ..command import Node, arg
from ..config import ExtensionConfig
from ..context import Ctx
from ..errors import ButlerError

Transform = Callable[[dict], dict]


def firefox_manifest(chrome: dict, addon_id: str, min_version: str) -> dict:
    m = json.loads(json.dumps(chrome))  # deep copy — never mutate the source
    m["background"] = {"scripts": ["background.js"]}
    m["browser_specific_settings"] = {
        "gecko": {"id": addon_id, "strict_min_version": min_version}
    }
    return m


def write_zip(ctx: Ctx, cfg: ExtensionConfig, out: Path,
              transform: Transform | None = None) -> None:
    """Zip the extension tree into `out`.

    Entries are added in sorted order so two runs of the same source produce
    byte-identical archives — a diffable release artifact is worth the one line.
    """
    manifest_src = cfg.dir / "manifest.json"
    if not manifest_src.is_file():
        raise ButlerError(f"no manifest.json in {ctx.disp(cfg.dir)}")

    rewritten = None
    if transform is not None:
        try:
            rewritten = json.dumps(transform(json.loads(manifest_src.read_text())),
                                   indent=2) + "\n"
        except ValueError as e:
            raise ButlerError(f"{ctx.disp(manifest_src)} is not valid JSON: {e}") from e

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(cfg.dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(cfg.dir).as_posix()
            if rewritten is not None and path == manifest_src:
                z.writestr(arcname, rewritten)
            else:
                z.write(path, arcname)
    ui.ok("wrote", f"{ctx.disp(out)} ({out.stat().st_size:,} bytes)")


def package(ctx: Ctx, args) -> int:
    cfg = ctx.cfg.extension
    if cfg is None:
        raise ButlerError("this project has no [extension] configuration")
    if not cfg.dir.is_dir():
        raise ButlerError(f"no extension directory at {ctx.disp(cfg.dir)}")

    # CI passes --label explicitly (mirroring the APK step) rather than relying
    # on git describe inside a shallow checkout.
    label = args.label or vcs.release_label(ctx.root)
    if ctx.would(f"package {ctx.disp(cfg.dir)} as {cfg.artifact}-*-{label}.zip"):
        return 0

    ctx.dist.mkdir(parents=True, exist_ok=True)
    write_zip(ctx, cfg, ctx.dist / f"{cfg.artifact}-chrome-{label}.zip")
    if cfg.firefox:
        write_zip(
            ctx, cfg, ctx.dist / f"{cfg.artifact}-firefox-{label}.zip",
            transform=lambda m: firefox_manifest(m, cfg.firefox.id,
                                                 cfg.firefox.min_version))
    return 0


def node(cfg: ExtensionConfig) -> Node:
    return Node("extension", "browser extension", children=[
        Node("package", "zip the Chrome and Firefox bundles into dist/",
             func=package,
             args=[arg("--label", help="release label for the zip names "
                                       "(default: the git tag on HEAD, else a UTC datetime)")]),
    ])
