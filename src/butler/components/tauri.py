"""The `app` component: a Tauri desktop app, plus the Android subtree."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import paths, sources, ui
from ..command import Node, arg
from ..config import TauriConfig
from ..context import Ctx
from ..errors import ButlerError
from . import android


def _app(ctx: Ctx) -> TauriConfig:
    if ctx.cfg.app is None:
        raise ButlerError("this project has no [app] configuration")
    return ctx.cfg.app


def _linux_bundle_env() -> dict[str, str] | None:
    """AppImage's linuxdeploy is itself an AppImage and needs FUSE; this
    fallback extracts and runs instead, which is what makes the bundle step work
    on headless, containerised or hardened Linux hosts."""
    if not sys.platform.startswith("linux"):
        return None
    return {"APPIMAGE_EXTRACT_AND_RUN": "1", "NO_STRIP": "1"}


def dev(ctx: Ctx, args) -> int:
    """Open the desktop dev window.

    When the project configures a dev identifier, the window runs under its own
    bundle identity: a distinct identifier means its own WebView data directory
    and its own keychain namespace, so a dev window can never collide with an
    installed release build or read its credentials.
    """
    app = _app(ctx)
    cmd = ["cargo", "tauri", "dev"]
    override = {}
    if app.dev_identifier:
        override["identifier"] = app.dev_identifier
    if app.dev_product_name:
        override["productName"] = app.dev_product_name
    if override:
        cmd += ["--config", json.dumps(override)]
    return ctx.run(cmd, cwd=app.dir)


def build(ctx: Ctx, args) -> int:
    app = _app(ctx)
    sources.prepare(ctx)
    cmd = ["cargo", "tauri", "build"]
    if args.debug:
        cmd.append("--debug")
    if args.no_bundle:
        cmd.append("--no-bundle")
    elif args.bundles:
        cmd += ["--bundles", *args.bundles]
    rc = ctx.run(cmd, cwd=app.dir, env=_linux_bundle_env())
    if rc == 0 and not args.no_bundle and not ctx.dry_run:
        profile = "debug" if args.debug else "release"
        ctx.collect([app.tauri_dir / "target" / profile / "bundle"])
    return rc


def icon(ctx: Ctx, args) -> int:
    """Regenerate the icon set. A project with generated source art names the
    generator in butler.toml; it runs first and must leave app-icon.png behind."""
    app = _app(ctx)
    if app.icon_generator:
        ctx.check([sys.executable, app.icon_generator], cwd=app.dir,
                  what=app.icon_generator)
    # The master art is often shared with a web frontend and lives outside the
    # app directory, hence icon_source; a generator writes app-icon.png instead.
    src = app.icon_source or (app.dir / "app-icon.png")
    # Under --dry-run the generator did not actually run, so its output is
    # legitimately absent — demanding it here would report a problem that only
    # exists because of the flag.
    generated = app.icon_generator and ctx.dry_run
    if not src.is_file() and not generated:
        raise ButlerError(
            f"no icon source at {ctx.disp(src)}",
            hint="Set [app] icon_source or icon_generator, or put app-icon.png "
                 "in the app directory.")
    return ctx.run(["cargo", "tauri", "icon", str(src)], cwd=app.dir)


def install(ctx: Ctx, args) -> int:
    """Install the built Linux AppImage for everyday desktop use:
      1. copy the newest dist/*.AppImage to <programs_dir>/<Name>.AppImage,
      2. copy the app icon next to it as <programs_dir>/<Name>.png,
      3. write a .desktop launcher pointing at both with absolute paths.

    The installed files use stable, version-less names, so installing a newer
    build over the old one leaves the launcher valid.
    """
    app = _app(ctx)
    cfg = app.install
    if cfg is None:
        raise ButlerError("this project has no [app.install] configuration")
    if not sys.platform.startswith("linux"):
        raise ButlerError("`app install` is Linux-only (AppImage + .desktop)")

    src = paths.newest([*ctx.dist.glob("*.AppImage"), *ctx.dist.glob("*.appimage")])
    if src is None:
        raise ButlerError("no AppImage in dist/",
                          hint="Build one first:  butler.py app build --bundles appimage")

    icon_src = app.tauri_dir / "icons" / "icon.png"
    if not icon_src.is_file():
        raise ButlerError(f"app icon missing: {ctx.disp(icon_src)}",
                          hint="Run:  butler.py app icon")

    applications = Path.home() / ".local" / "share" / "applications"
    if ctx.would(f"install {ctx.disp(src)} into {cfg.programs_dir} "
                 f"and write {cfg.name}.desktop"):
        return 0
    cfg.programs_dir.mkdir(parents=True, exist_ok=True)
    applications.mkdir(parents=True, exist_ok=True)

    appimage_dst = cfg.programs_dir / f"{cfg.name}.AppImage"
    icon_dst = cfg.programs_dir / f"{cfg.name}.png"
    desktop_dst = applications / f"{cfg.name}.desktop"

    import shutil

    shutil.copy2(src, appimage_dst)
    appimage_dst.chmod(0o755)  # an AppImage must be executable to launch
    shutil.copy2(icon_src, icon_dst)
    desktop_dst.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        f"Name={cfg.name}\n"
        + (f"Comment={cfg.comment}\n" if cfg.comment else "")
        + f"Exec={appimage_dst}\n"
        f"Icon={icon_dst}\n"
        "Terminal=false\n"
        f"Categories={';'.join(cfg.categories)};\n"
        f"StartupWMClass={cfg.startup_wm_class}\n"
    )

    ui.plain()
    ui.ok("installed")
    ui.plain(f"  app    {appimage_dst}  (from {ctx.disp(src)})")
    ui.plain(f"  icon   {icon_dst}")
    ui.plain(f"  launch {desktop_dst}")
    return 0


def node(cfg: TauriConfig) -> Node:
    children = [
        Node("dev", "desktop dev window", func=dev),
        Node("build", "desktop bundles for this OS", func=build, args=[
            arg("--debug", action="store_true", help="debug profile"),
            arg("--no-bundle", action="store_true", help="compile only, skip packaging"),
            arg("--bundles", nargs="*", metavar="KIND",
                help="e.g. deb appimage rpm nsis msi dmg"),
        ]),
        Node("icon", "regenerate the icon set", func=icon),
    ]
    if cfg.install:
        children.append(
            Node("install", "install the built AppImage + icon + .desktop launcher (Linux)",
                 func=install))
    if cfg.android:
        children.append(android.node(cfg.android))
    return Node("app", "Tauri app", children=children)
