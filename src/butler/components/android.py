"""Android: toolchain discovery, the regenerable gen/ tree, signing, sideloading.

This is the block that had drifted furthest between the four projects that
carry it. Where they disagreed, the better implementation won:

  * cleartext HTTP — a network_security_config.xml rather than the Gradle
    `manifestPlaceholders["usesCleartextTraffic"]` some of them used. A
    network security config takes precedence on API 24+ AND keeps the system
    trust anchors, so ordinary HTTPS still works; the manifest placeholder is an
    all-or-nothing switch on a lower-precedence mechanism.
  * stale outputs — only one of them cleared app/build/outputs first. Gradle
    leaves an APK behind for every variant ever built and the signing step globs
    the whole tree, so without the wipe an old universal APK gets re-signed with
    today's label and shipped as if it were this build. Always wipe.
  * ABI splits — only one of them built an APK per ABI and matched the device's
    ro.product.cpu.abi when installing. Each APK carries the whole native tree,
    so a universal one is ~4x what any device can use. Available everywhere now,
    off by default.

The generated gen/android tree stays REGENERABLE: nothing here edits it in a way
that must survive `cargo tauri android init`. Icons and the network config are
re-applied before every build (idempotent), and release APKs are signed as a
post-build step rather than by patching generated Gradle.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .. import paths, proc, sources, ui, vcs
from ..command import Node, arg
from ..config import AndroidConfig, TauriConfig
from ..context import Ctx
from ..errors import ButlerError, MissingToolError

# A --split-per-abi build names its APKs after the RUST arch, which is not what
# a device reports for ro.product.cpu.abi — hence the translation. Longest-first
# so "x86" never shadows "x86_64".
APK_ARCHES = ("arm64", "x86_64", "arm", "x86")
ABI_TO_APK_ARCH = {
    "arm64-v8a": "arm64",
    "armeabi-v7a": "arm",
    "armeabi": "arm",
    "x86_64": "x86_64",
    "x86": "x86",
}

_SDK_CANDIDATES = (
    "~/Android/Sdk",              # Linux (Android Studio)
    "~/Library/Android/sdk",      # macOS
    "~/AppData/Local/Android/Sdk",  # Windows
    "/opt/android-sdk",           # Arch package
)
_JDK_CANDIDATES = (
    "/usr/lib/jvm/java-17-openjdk",
    "/usr/lib/jvm/default",
    "~/Android/jdk",
)


# --------------------------------------------------------------------------- #
# toolchain
# --------------------------------------------------------------------------- #

@dataclass
class Toolchain:
    sdk: Path
    ndk: Path
    java_home: Path | None

    @property
    def env(self) -> dict[str, str]:
        """The overrides tauri-cli, Gradle and any build.rs need.

        All three NDK aliases are set: tauri-cli reads NDK_HOME, cargo-ndk and
        assorted build scripts read ANDROID_NDK_HOME or ANDROID_NDK_ROOT, and
        which one a given crate picked is not worth tracking.
        """
        e = {
            "ANDROID_HOME": str(self.sdk),
            "ANDROID_SDK_ROOT": str(self.sdk),
            "NDK_HOME": str(self.ndk),
            "ANDROID_NDK_HOME": str(self.ndk),
            "ANDROID_NDK_ROOT": str(self.ndk),
        }
        if self.java_home:
            e["JAVA_HOME"] = str(self.java_home)
        # Make the SDK's platform-tools (adb) reachable for `android dev`.
        e["PATH"] = os.pathsep.join(
            p for p in (str(self.sdk / "platform-tools"), os.environ.get("PATH", "")) if p)
        return e

    @property
    def build_tools(self) -> Path | None:
        """Newest build-tools/<ver> (holds zipalign and apksigner)."""
        bt = self.sdk / "build-tools"
        if not bt.is_dir():
            return None
        versions = sorted((p for p in bt.iterdir() if p.is_dir()), key=lambda p: p.name)
        return versions[-1] if versions else None

    def tool(self, name: str) -> str:
        found = proc.which(name, [self.sdk / "platform-tools",
                                  *( [self.java_home / "bin"] if self.java_home else [] ),
                                  *( [self.build_tools] if self.build_tools else [] )])
        if not found:
            raise MissingToolError(name, hint="Install the Android SDK's platform-tools/"
                                              "build-tools, or put it on PATH.")
        return found


def find_sdk() -> Path | None:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(var)
        if val and Path(val).is_dir():
            return Path(val)
    for c in _SDK_CANDIDATES:
        p = Path(c).expanduser()
        if p.is_dir():
            return p
    return None


def find_ndk(sdk: Path | None, pinned: str | None) -> Path | None:
    """Prefer the pinned version (so a local build matches CI), else the newest
    installed, else the Arch AUR path."""
    for var in ("NDK_HOME", "ANDROID_NDK_HOME", "ANDROID_NDK_ROOT"):
        val = os.environ.get(var)
        if val and Path(val).is_dir():
            return Path(val)
    candidates: list[Path] = []
    if sdk:
        ndk_root = sdk / "ndk"
        if pinned and (ndk_root / pinned).is_dir():
            return ndk_root / pinned
        if ndk_root.is_dir():
            candidates += sorted(p for p in ndk_root.iterdir() if p.is_dir())
    if Path("/opt/android-ndk").is_dir():
        candidates.append(Path("/opt/android-ndk"))
    return candidates[-1] if candidates else None


def find_java_home() -> Path | None:
    val = os.environ.get("JAVA_HOME")
    if val and Path(val).is_dir():
        return Path(val)
    for c in _JDK_CANDIDATES:
        p = Path(c).expanduser()
        if p.is_dir():
            return p
    # Derive it from the java on PATH (…/bin/java -> …).
    java = shutil.which("java")
    if java:
        home = Path(java).resolve().parent.parent
        if home.is_dir():
            return home
    return None


def toolchain(cfg: AndroidConfig, *, required: bool = True) -> Toolchain | None:
    """Resolve the Android toolchain, or explain precisely what is missing.

    The originals discovered these separately in four places and each failed
    differently — one exited, one printed a warning and carried on into a
    confusing CMake error. One message, listing everything that's absent.
    """
    sdk = find_sdk()
    ndk = find_ndk(sdk, cfg.ndk_version)
    java_home = find_java_home()

    missing = []
    if not sdk:
        missing.append("Android SDK  (set ANDROID_HOME, or install to ~/Android/Sdk)")
    if not ndk:
        missing.append("Android NDK  (set NDK_HOME, or install one via the SDK manager)")
    if not java_home:
        missing.append("JDK 17       (set JAVA_HOME, or put java on PATH)")
    if missing:
        if not required:
            return None
        raise ButlerError(
            "the Android build prerequisites are missing",
            hint="\n".join(f"  - {m}" for m in missing))

    assert sdk and ndk
    return Toolchain(sdk=sdk, ndk=ndk, java_home=java_home)


# --------------------------------------------------------------------------- #
# signing material
# --------------------------------------------------------------------------- #

@dataclass
class Keystore:
    dir: Path
    jks: Path
    props: Path
    in_repo: bool


def keystore_for(ctx: Ctx, app: TauriConfig, cfg: AndroidConfig) -> Keystore:
    """Where this app's signing material lives.

    A signing key must never be committed, so the default is a vault OUTSIDE
    any repo with one subfolder per app — one vault signs every app, and a
    fresh clone of a project contains nothing secret. $ANDROID_KEYSTORE_VAULT
    overrides the location. The in-repo fallback exists only so a machine
    without a vault can still produce a (gitignored) key and build; it warns,
    because a key under the project tree is one `git add -A` from being public.
    """
    env_vault = os.environ.get("ANDROID_KEYSTORE_VAULT")
    vault = Path(env_vault).expanduser() if env_vault else cfg.vault.expanduser()
    vault_dir = vault / cfg.key_name
    repo_dir = app.dir / ".android"

    if not vault_dir.is_dir() and repo_dir.is_dir():
        ui.note(f"using the in-repo keystore at {ctx.disp(repo_dir)}. Move it to a "
                f"vault outside the repo ({vault}) when you get a chance.")
        return Keystore(repo_dir, repo_dir / f"{cfg.key_name}.jks",
                        repo_dir / "keystore.properties", in_repo=True)
    return Keystore(vault_dir, vault_dir / f"{cfg.key_name}.jks",
                    vault_dir / "keystore.properties", in_repo=False)


def read_props(cfg: AndroidConfig, ks: Keystore) -> dict[str, str] | None:
    """Signing config from keystore.properties; the four env vars override.

    Returns None — not an error — when anything is missing: an unsigned release
    build is a legitimate outcome (CI ships unsigned APKs), it just can't be
    sideloaded, and the caller says so.
    """
    props: dict[str, str] = {}
    if ks.props.is_file():
        for line in ks.props.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
    p = cfg.env_prefix
    overrides = {
        "storeFile": os.environ.get(f"{p}_ANDROID_KEYSTORE"),
        "storePassword": os.environ.get(f"{p}_ANDROID_KS_PASS"),
        "keyAlias": os.environ.get(f"{p}_ANDROID_KEY_ALIAS"),
        "keyPassword": os.environ.get(f"{p}_ANDROID_KEY_PASS"),
    }
    props.update({k: v for k, v in overrides.items() if v})
    required = ("storeFile", "storePassword", "keyAlias", "keyPassword")
    return props if all(props.get(k) for k in required) else None


def keygen(ctx: Ctx, app: TauriConfig, cfg: AndroidConfig, password: str | None) -> int:
    """Create a self-signed keystore for sideloadable release APKs.

    Self-signed is fine for sideloading; it is not a Play Store upload key.
    """
    import secrets

    tc = toolchain(cfg)
    assert tc
    ks = keystore_for(ctx, app, cfg)
    if ks.jks.exists():
        ui.warn("keystore already exists:", f"{ctx.disp(ks.jks)} (delete it to regenerate)")
        return 1

    password = password or secrets.token_urlsafe(18)
    alias = cfg.key_name
    if ctx.would(f"create {ctx.disp(ks.jks)} + keystore.properties"):
        return 0
    ks.dir.mkdir(parents=True, exist_ok=True)
    ctx.check([tc.tool("keytool"), "-genkeypair", "-v",
               "-keystore", ks.jks, "-alias", alias,
               "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
               "-storepass", password, "-keypass", password,
               "-dname", cfg.dname],
              cwd=app.dir, env=tc.env, what="keytool -genkeypair")
    ks.props.write_text(
        f"storeFile={ks.jks}\nstorePassword={password}\n"
        f"keyAlias={alias}\nkeyPassword={password}\n")
    ks.props.chmod(0o600)  # it holds the store password

    ui.plain()
    ui.ok("wrote", f"{ctx.disp(ks.jks)} + keystore.properties (mode 600)")
    ui.plain("Back this keystore up — the SAME key is required to ship in-place upgrades.")
    return 0


def signed_apk_name(unsigned: str, key_name: str, label: str) -> str:
    """`app-universal-release-unsigned.apk` -> `<key_name>-<label>.apk`, keeping
    any architecture marker so a split build's APKs never collide."""
    stem = unsigned.replace("-release-unsigned.apk", "")
    parts = [p for p in stem.split("-") if p not in ("app", "universal")]
    suffix = ("-" + "-".join(parts)) if parts else ""
    return f"{key_name}{suffix}-{label}.apk"


def sign_release_apks(ctx: Ctx, cfg: AndroidConfig, ks: Keystore, tc: Toolchain,
                      outputs: Path) -> list[Path]:
    """zipalign + apksigner every *-release-unsigned.apk under `outputs`.

    A post-build step on purpose: signing via generated Gradle would be lost the
    next time `cargo tauri android init` regenerates the project.
    """
    props = read_props(cfg, ks)
    if not props:
        ui.plain()
        ui.note("no keystore — the release APK is UNSIGNED and cannot be sideloaded.\n"
                "      Run 'butler.py app android keygen', or build with --debug for an "
                "auto-signed APK.")
        return []
    bt = tc.build_tools
    if not bt:
        ui.err("build-tools not found under the SDK; cannot sign.")
        return []

    label = vcs.release_label(ctx.root)
    signed: list[Path] = []
    for unsigned in sorted(outputs.rglob("*-release-unsigned.apk")):
        aligned = unsigned.with_name(unsigned.name.replace("-unsigned", "-aligned"))
        out = unsigned.with_name(signed_apk_name(unsigned.name, cfg.key_name, label))
        if ctx.run([bt / "zipalign", "-p", "-f", "4", unsigned, aligned], env=tc.env) != 0:
            continue
        # The passwords go through the ENVIRONMENT, not `pass:` on the command
        # line. butler echoes every command it runs, and these scripts have
        # always been run in terminals and CI logs — an argv-borne keystore
        # password is printed there, and is readable from /proc while apksigner
        # runs. apksigner reads env: and file: for exactly this reason.
        rc = ctx.run([bt / "apksigner", "sign",
                      "--ks", props["storeFile"],
                      "--ks-key-alias", props["keyAlias"],
                      "--ks-pass", "env:BUTLER_KS_PASS",
                      "--key-pass", "env:BUTLER_KEY_PASS",
                      "--out", out, aligned],
                     env={**tc.env,
                          "BUTLER_KS_PASS": props["storePassword"],
                          "BUTLER_KEY_PASS": props["keyPassword"]})
        aligned.unlink(missing_ok=True)
        if rc == 0:
            signed.append(out)
    return signed


# --------------------------------------------------------------------------- #
# the regenerable gen/android tree
# --------------------------------------------------------------------------- #

def gen_dir(app: TauriConfig) -> Path:
    return app.tauri_dir / "gen" / "android"


def sync_icons(ctx: Ctx, app: TauriConfig) -> None:
    """Install the app's launcher icons over the ones `android init` scaffolds.

    `cargo tauri android init` seeds gen/android/.../res from its own template,
    which carries the stock Tauri artwork, and never re-reads
    src-tauri/icons/android afterwards — so without this the APK ships the wrong
    icon. Re-applied before every build, so it survives regeneration.
    """
    src = app.tauri_dir / "icons" / "android"
    res = gen_dir(app) / "app" / "src" / "main" / "res"
    if not src.is_dir() or not res.is_dir():
        return
    if ctx.would("sync the launcher icons into gen/android"):
        return
    copied = 0
    for f in sorted(src.rglob("*")):
        if f.is_file():
            out = res / f.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            copied += 1
    # The template's adaptive-icon halves are dead once mipmap-anydpi-v26 points
    # at @mipmap/ic_launcher_foreground + @color/ic_launcher_background.
    for stale in (res / "drawable" / "ic_launcher_background.xml",
                  res / "drawable-v24" / "ic_launcher_foreground.xml"):
        stale.unlink(missing_ok=True)
    if copied:
        ui.ok("synced", f"{copied} launcher-icon file(s) into gen/android/")


_NETWORK_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<!-- Managed by butler (android.sync_network_config): allow the app to reach a
     self-hosted plain-HTTP backend on the LAN. -->
<network-security-config>
    <base-config cleartextTrafficPermitted="true">
        <trust-anchors>
            <certificates src="system" />
        </trust-anchors>
    </base-config>
</network-security-config>
"""


def sync_network_config(ctx: Ctx, app: TauriConfig) -> None:
    """Permit cleartext HTTP so the app can reach a self-hosted LAN backend.

    Android 9+ blocks cleartext, and Tauri's manifest sets
    usesCleartextTraffic=false for release. A network-security-config takes
    precedence on API 24+ and keeps the system trust anchors, so HTTPS is
    unaffected — add <domain> entries here to narrow it to specific hosts.
    """
    main = gen_dir(app) / "app" / "src" / "main"
    if not main.is_dir():
        return
    if ctx.would("write network_security_config.xml and wire it into the manifest"):
        return
    xml_dir = main / "res" / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    (xml_dir / "network_security_config.xml").write_text(_NETWORK_CONFIG)

    manifest = main / "AndroidManifest.xml"
    if not manifest.is_file():
        return
    text = manifest.read_text()
    if "android:networkSecurityConfig" in text:
        return  # already wired
    anchor = 'android:usesCleartextTraffic="${usesCleartextTraffic}">'
    if anchor not in text:
        ui.warn("warning:", "could not wire networkSecurityConfig into "
                            "AndroidManifest.xml (the usesCleartextTraffic anchor moved); "
                            "cleartext may be blocked in release.")
        return
    manifest.write_text(text.replace(
        anchor,
        'android:usesCleartextTraffic="${usesCleartextTraffic}"\n'
        '        android:networkSecurityConfig="@xml/network_security_config">',
        1))
    ui.ok("applied", "Android network security config (cleartext HTTP permitted).")


def ensure_initialised(ctx: Ctx, app: TauriConfig, tc: Toolchain) -> None:
    if not gen_dir(app).exists():
        ui.plain("Initializing the Android project (one-time)...")
        ctx.check(["cargo", "tauri", "android", "init"], cwd=app.dir, env=tc.env,
                  what="cargo tauri android init")


def prepare_gen(ctx: Ctx, app: TauriConfig, cfg: AndroidConfig, tc: Toolchain) -> None:
    ensure_initialised(ctx, app, tc)
    sync_icons(ctx, app)
    if cfg.cleartext:
        sync_network_config(ctx, app)


# --------------------------------------------------------------------------- #
# sideloading (adb)
# --------------------------------------------------------------------------- #

def adb_devices(adb: str, env: dict[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(ready, problems): serials of attached ready devices, plus the raw lines
    for unusable ones (unauthorized/offline) so we can say what to fix."""
    r = proc.capture([adb, "devices", "-l"], env=env)
    ready, problems = [], []
    for line in r.out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            ready.append((parts[0], " ".join(parts[2:])))
        elif parts:
            problems.append(line)
    return ready, problems


def pick_device(adb: str, env: dict[str, str], device: str | None = None) -> str:
    """Resolve the target serial: explicit --device, $ANDROID_SERIAL, or the
    only attached device."""
    ready, problems = adb_devices(adb, env)
    device = device or os.environ.get("ANDROID_SERIAL")
    if device:
        if any(s == device for s, _ in ready):
            return device
        raise ButlerError(
            f"device '{device}' is not attached or not ready",
            hint="\n".join(f"available: {s}  {d}" for s, d in ready) or "no devices are ready")
    if not ready:
        raise ButlerError(
            "no Android device is ready for adb",
            hint="\n".join(problems + [
                "Connect the phone over USB, enable Developer options -> USB debugging,",
                "and accept the 'Allow USB debugging' prompt on the device."]))
    if len(ready) > 1:
        raise ButlerError(
            "several devices are attached; pick one with --device SERIAL",
            hint="\n".join(f"{s}  {d}" for s, d in ready))
    serial, desc = ready[0]
    ui.ok("device:", f"{serial}  {desc}")
    return serial


def installable_apks(candidates) -> list[Path]:
    """Only APKs adb can actually install — drop the unsigned/aligned
    intermediates the signing step leaves behind. Newest first."""
    ok = [Path(p) for p in candidates
          if str(p).endswith(".apk")
          and "-unsigned" not in Path(p).name and "-aligned" not in Path(p).name]
    return sorted((p for p in ok if p.is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def apk_arch(path: Path) -> str | None:
    """The arch a split APK targets, or None for a universal one. Matched on
    token boundaries so 'x86' does not also match 'x86_64'."""
    for a in APK_ARCHES:
        if re.search(rf"[-_]{re.escape(a)}[-_.]", path.name):
            return a
    return None


def pick_apk(candidates: list[Path], abi: str) -> Path:
    """The split built for this device's ABI, else a universal one, else the
    newest. A split build leaves five APKs behind and only two will run on any
    given device — the matching split is ~4x smaller, so prefer it."""
    want = ABI_TO_APK_ARCH.get(abi)
    if want:
        for p in candidates:
            if apk_arch(p) == want:
                return p
    for p in candidates:
        if apk_arch(p) is None:
            return p
    return candidates[0]


def app_identifier(app: TauriConfig) -> str:
    """The package name, from tauri.conf.json."""
    import json

    conf = app.tauri_dir / "tauri.conf.json"
    try:
        ident = json.loads(conf.read_text()).get("identifier")
    except (OSError, ValueError):
        ident = None
    if not ident:
        raise ButlerError(f"no 'identifier' in {conf}",
                          hint="adb needs the package name to launch/uninstall the app.")
    return ident


def install_apk(ctx: Ctx, app: TauriConfig, cfg: AndroidConfig, apks,
                *, device=None, reinstall=False, logcat=False) -> int:
    """Sideload an APK onto an attached phone and launch it."""
    tc = toolchain(cfg)
    assert tc
    candidates = installable_apks(apks)
    if not candidates:
        raise ButlerError(
            "no installable APK found",
            hint="Build one first:  butler.py app android build --debug --install")

    adb = tc.tool("adb")
    env = tc.env
    serial = pick_device(adb, env, device)
    app_id = app_identifier(app)
    abi = proc.capture([adb, "-s", serial, "shell", "getprop", "ro.product.cpu.abi"],
                       env=env).out.strip()
    apk = pick_apk(candidates, abi)
    if abi and apk_arch(apk) is None and len(candidates) > 1:
        ui.note(f"no APK was built for this device's ABI ({abi}); installing the universal one.")

    ui.plain(f"installing {ui.bold(ctx.disp(apk))} ({apk.stat().st_size:,} bytes"
             f"{', ' + abi if abi else ''})")

    def do_install() -> proc.Result:
        ui.cmd(f"adb -s {serial} install -r {ctx.disp(apk)}")
        return proc.capture([adb, "-s", serial, "install", "-r", str(apk)], env=env)

    def uninstall() -> None:
        proc.capture([adb, "-s", serial, "uninstall", app_id], env=env)

    if reinstall:
        uninstall()
    r = do_install()
    if r.combined.strip():
        ui.plain(r.combined.strip())

    if r.rc != 0 and not reinstall and (
            "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in r.combined
            or "signatures do not match" in r.combined):
        # Debug and release APKs carry different signing keys, so swapping
        # between them needs a clean install — which wipes the app's on-device
        # data. Ask rather than doing that silently.
        ui.plain()
        ui.plain(f"The installed {app_id} is signed with a different key (debug vs release).")
        ui.plain("Uninstalling it first " +
                 ui.bold("wipes the app's local data on the device") +
                 " (server-synced data survives).")
        if not ctx.confirm("Continue?"):
            ui.plain("Aborted. Re-run with --reinstall to skip this prompt.")
            return 1
        uninstall()
        r = do_install()
        ui.plain(r.combined.strip())

    if r.rc != 0:
        raise ButlerError("adb install failed", code=r.rc)

    ui.ok("installed", f"— launching {app_id}...")
    proc.capture([adb, "-s", serial, "shell", "monkey", "-p", app_id,
                  "-c", "android.intent.category.LAUNCHER", "1"], env=env)

    if logcat:
        _tail_logcat(adb, serial, app_id, env)
    return 0


def _tail_logcat(adb: str, serial: str, app_id: str, env: dict[str, str]) -> None:
    pid = ""
    for _ in range(20):  # the process needs a moment to appear
        pids = proc.capture([adb, "-s", serial, "shell", "pidof", app_id], env=env).out.split()
        pid = pids[0] if pids else ""
        if pid:
            break
        time.sleep(0.5)
    if not pid:
        ui.note(f"{app_id} is not running; tailing the whole log instead.")
    ui.plain(f"\ntailing logcat{f' for pid {pid}' if pid else ''} (Ctrl-C to stop)...")
    proc.run([adb, "-s", serial, "logcat"] + (["--pid", pid] if pid else []),
             env=env, echo=False)


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #

def _resolve_split(args, cfg: AndroidConfig) -> bool:
    if getattr(args, "universal", False):
        return False
    if getattr(args, "split_abi", False):
        return True
    return cfg.split_abi


def build(ctx: Ctx, args) -> int:
    app, cfg = _cfg(ctx)
    sources.prepare(ctx)
    tc = toolchain(cfg)
    assert tc
    prepare_gen(ctx, app, cfg, tc)

    outputs = gen_dir(app) / "app" / "build" / "outputs"
    # Regenerable, and stale variants here would be re-signed and shipped.
    if outputs.is_dir() and not ctx.would(f"clear {ctx.disp(outputs)}"):
        shutil.rmtree(outputs)

    cmd = ["cargo", "tauri", "android", "build", "--apk"]
    if args.debug:
        cmd.append("--debug")
    if _resolve_split(args, cfg):
        cmd.append("--split-per-abi")
    ctx.check(cmd, cwd=app.dir, env=tc.env, what="cargo tauri android build")

    # Debug APKs are auto-signed with the Android debug key and are instantly
    # sideloadable; release APKs come out unsigned.
    if args.debug or args.no_sign:
        built = [p for p in ctx.collect([outputs]) if p.suffix == ".apk"]
    else:
        ks = keystore_for(ctx, app, cfg)
        signed = sign_release_apks(ctx, cfg, ks, tc, outputs)
        if signed:
            ctx.dist.mkdir(parents=True, exist_ok=True)
            built = []
            for p in signed:
                dest = ctx.dist / p.name
                shutil.copy2(p, dest)
                built.append(dest)
            paths.report_copied(built, ctx.root, "signed & copied to dist/")
        else:
            built = [p for p in ctx.collect([outputs]) if p.suffix == ".apk"]

    if getattr(args, "install", False):
        return install_apk(ctx, app, cfg, built, device=args.device,
                           reinstall=args.reinstall, logcat=args.logcat)
    return 0


def dev(ctx: Ctx, args) -> int:
    app, cfg = _cfg(ctx)
    tc = toolchain(cfg)
    assert tc
    prepare_gen(ctx, app, cfg, tc)
    return ctx.run(["cargo", "tauri", "android", "dev"], cwd=app.dir, env=tc.env)


def init(ctx: Ctx, args) -> int:
    app, cfg = _cfg(ctx)
    tc = toolchain(cfg)
    assert tc
    ctx.check(["cargo", "tauri", "android", "init"], cwd=app.dir, env=tc.env,
              what="cargo tauri android init")
    sync_icons(ctx, app)
    if cfg.cleartext:
        sync_network_config(ctx, app)
    return 0


def install(ctx: Ctx, args) -> int:
    app, cfg = _cfg(ctx)
    return install_apk(ctx, app, cfg, sorted(ctx.dist.glob("*.apk")),
                       device=args.device, reinstall=args.reinstall, logcat=args.logcat)


def _cfg(ctx: Ctx) -> tuple[TauriConfig, AndroidConfig]:
    app = ctx.cfg.app
    if app is None or app.android is None:
        raise ButlerError("this project has no [app.android] configuration")
    return app, app.android


_INSTALL_ARGS = [
    arg("--device", help="adb serial (default: the only attached device, or $ANDROID_SERIAL)"),
    arg("--reinstall", action="store_true",
        help="uninstall first — a clean install; WIPES the app's on-device data"),
    arg("--logcat", action="store_true", help="tail the app's logcat after launching"),
]


def node(cfg: AndroidConfig) -> Node:
    return Node(
        name="android",
        help="Android build, signing and sideloading (needs the SDK/NDK)",
        children=[
            Node("init", "scaffold gen/android (one-time; re-runnable)", func=init),
            Node("dev", "run on a device/emulator with hot reload", func=dev),
            Node("build", "build an APK", func=build, args=[
                arg("--debug", action="store_true",
                    help="debug profile (auto-signed, instantly sideloadable)"),
                arg("--no-sign", action="store_true",
                    help="skip signing the release APK (leaves it unsigned)"),
                arg("--split-abi", action="store_true",
                    help="one APK per ABI (~4x smaller each)"
                         + (" [default]" if cfg.split_abi else "")),
                arg("--universal", action="store_true",
                    help="one APK carrying every ABI"
                         + ("" if cfg.split_abi else " [default]")),
                arg("--install", action="store_true",
                    help="sideload the freshly built APK and launch it"),
                *_INSTALL_ARGS,
            ]),
            Node("keygen", "create the release signing keystore (one-time)", func=keygen_action,
                 args=[arg("--password", help="keystore password (default: random)")]),
            Node("install", "sideload the newest APK in dist/", func=install,
                 args=_INSTALL_ARGS),
        ],
    )


def keygen_action(ctx: Ctx, args) -> int:
    app, cfg = _cfg(ctx)
    return keygen(ctx, app, cfg, args.password)
