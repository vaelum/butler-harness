"""`butler.py doctor` — say what's installed before a build discovers it isn't.

Every hand-rolled butler re-discovered cmake / the Android SDK / adb inside the
command that needed it, so a missing NDK surfaced as a confusing CMake error ten
minutes into a build. This reports the whole picture up front, and unlike every
other command it keeps going after the first failure.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import proc, ui
from .command import Node
from .context import Ctx
from .errors import ButlerError


@dataclass
class Check:
    label: str
    found: str | None
    detail: str = ""
    required: bool = True


def _tool(label: str, name: str, *, version_flag: bool = True,
          required: bool = True) -> Check:
    path = shutil.which(name)
    if not path:
        return Check(label, None, required=required)
    detail = ""
    if version_flag:
        ver = proc.tool_version(path)
        detail = ".".join(str(v) for v in ver) if ver else ""
    return Check(label, path, detail, required)


def _general() -> list[Check]:
    return [
        _tool("git", "git"),
        _tool("docker", "docker"),
        _tool("rsync", "rsync", version_flag=False, required=False),
        _tool("ssh", "ssh", version_flag=False, required=False),
    ]


def _tauri(ctx: Ctx) -> list[Check]:
    app = ctx.cfg.app
    if app is None:
        return []
    checks = [
        _tool("cargo", "cargo"),
        _tool("node", "node"),
        _tool("npm", "npm", version_flag=False),
    ]
    cargo = shutil.which("cargo")
    if cargo:
        r = proc.capture([cargo, "tauri", "--version"])
        checks.append(Check("cargo-tauri", "installed" if r.ok else None,
                            r.out.strip() if r.ok else ""))
    checks.append(Check("app directory", str(app.dir) if app.dir.is_dir() else None,
                        ctx.disp(app.dir)))
    return checks


def _android(ctx: Ctx) -> list[Check]:
    app = ctx.cfg.app
    if app is None or app.android is None:
        return []
    from .components import android as andro

    sdk = andro.find_sdk()
    ndk = andro.find_ndk(sdk, app.android.ndk_version)
    java = andro.find_java_home()
    checks = [
        Check("Android SDK", str(sdk) if sdk else None),
        Check("Android NDK", str(ndk) if ndk else None,
              "pinned" if ndk and app.android.ndk_version
              and ndk.name == app.android.ndk_version else ""),
        Check("JAVA_HOME", str(java) if java else None),
    ]
    if sdk:
        tc = andro.Toolchain(sdk=sdk, ndk=ndk or sdk, java_home=java)
        bt = tc.build_tools
        checks.append(Check("build-tools", str(bt) if bt else None,
                            bt.name if bt else "needed to sign release APKs"))
        adb = proc.which("adb", [sdk / "platform-tools"])
        checks.append(Check("adb", adb, required=False))
        if adb:
            ready, problems = andro.adb_devices(adb, tc.env)
            devices = ", ".join(s for s, _ in ready)
            checks.append(Check("attached device", devices or None,
                                "; ".join(problems), required=False))
    ks = andro.keystore_for(ctx, app, app.android)
    checks.append(Check("signing keystore", str(ks.jks) if ks.jks.is_file() else None,
                        "in-repo — move it to a vault" if ks.in_repo else "",
                        required=False))
    return checks


def _build(ctx: Ctx) -> list[Check]:
    cfg = ctx.cfg.build
    if cfg is None:
        return []
    from .components import cmake as cmake_component

    need = ".".join(str(v) for v in cfg.cmake_min)
    try:
        path, ver = cmake_component.find_cmake(cfg.cmake_min)
        found, detail = str(path), ".".join(str(v) for v in ver) if ver else ""
        if ver and ver < cfg.cmake_min:
            detail += f" — older than the required {need}"
    except ButlerError:
        found, detail = None, f"needs >= {need}"
    checks = [
        Check("cmake", found, detail),
        _tool("ninja", "ninja", required=False),
        Check("presets", ", ".join(cfg.presets),
              "CMakePresets.json"
              if (cfg.dir / "CMakePresets.json").is_file()
              else "no CMakePresets.json — the presets above will not resolve"),
    ]
    if cfg.docker:
        # One image per preset is allowed, so report each one that a preset can
        # actually use rather than a single path that may not exist.
        for preset in cfg.presets:
            if not cfg.docker.supports(preset):
                continue
            env = cfg.docker.for_preset(preset, ctx.root)
            note = env.tag + (" (always)" if preset in cfg.docker.always else "")
            checks.append(Check(f"buildenv[{preset}]",
                                str(env.dockerfile) if env.dockerfile.is_file() else None,
                                note, required=False))
    return checks


def _check_tools(ctx: Ctx) -> list[Check]:
    cfg = ctx.cfg.check
    if cfg is None:
        return []
    checks = []
    if cfg.lint is not None:
        config = ctx.cfg.root / cfg.lint.config
        checks.append(Check("megalinter config", str(config) if config.is_file() else None,
                            cfg.lint.config))
    if cfg.tidy is not None:
        driver = ctx.cfg.root / cfg.tidy.driver
        checks.append(Check("clang-tidy driver", str(driver) if driver.is_file() else None,
                            cfg.tidy.driver))
        checks.append(_tool("clang-tidy", "clang-tidy", required=False))
    return checks


def _server(ctx: Ctx) -> list[Check]:
    cfg = ctx.cfg.server
    if cfg is None:
        return []
    checks = [Check("server directory", str(cfg.dir) if cfg.dir.is_dir() else None,
                    ctx.disp(cfg.dir))]
    if cfg.venv:
        py = cfg.venv / "bin" / "python"
        checks.append(Check("server venv", str(py) if py.exists() else None,
                            "" if py.exists()
                            else f"cd {ctx.disp(cfg.dir)} && python -m venv "
                                 f"{cfg.venv.name} && {cfg.venv.name}/bin/pip install -e '.[dev]'",
                            required=False))
    if cfg.deploy:
        checks.append(Check("deploy target", cfg.deploy.host, cfg.deploy.dir,
                            required=False))
    return checks


def run(ctx: Ctx, args) -> int:
    groups = [
        ("general", _general()),
        ("tauri", _tauri(ctx)),
        ("android", _android(ctx)),
        ("server", _server(ctx)),
        ("build", _build(ctx)),
        ("check", _check_tools(ctx)),
    ]
    missing_required = 0
    for title, checks in groups:
        if not checks:
            continue
        ui.plain()
        ui.plain(ui.bold(title))
        for c in checks:
            if c.found:
                mark, tail = "  ok  ", c.found + (f"  ({c.detail})" if c.detail else "")
                ui.plain(f"  {ui.dim(mark)}{c.label:<18} {tail}")
            else:
                missing_required += int(c.required)
                label = "MISSING" if c.required else "absent "
                ui.plain(f"  {label} {c.label:<18} {ui.dim(c.detail)}")
    ui.plain()
    if missing_required:
        ui.warn(f"{missing_required} required item(s) missing.",
                "Builds that need them will fail.")
        return 1
    ui.ok("all required tooling present.")
    return 0


def node() -> Node:
    return Node("doctor", "report which build tools and SDKs are usable", func=run)
