from pathlib import Path

import pytest

from butler.components import android


# --------------------------------------------------------------------------- #
# artifact naming
# --------------------------------------------------------------------------- #

def test_signed_name_drops_the_tauri_boilerplate():
    assert android.signed_apk_name(
        "app-universal-release-unsigned.apk", "acme", "20260802-101500"
    ) == "acme-20260802-101500.apk"


def test_signed_name_keeps_the_abi_marker_so_splits_never_collide():
    names = {
        android.signed_apk_name(f"app-{a}-release-unsigned.apk", "acme", "v1.2")
        for a in ("arm64", "x86_64")
    }
    assert names == {"acme-arm64-v1.2.apk", "acme-x86_64-v1.2.apk"}


# --------------------------------------------------------------------------- #
# ABI matching
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,expected", [
    ("acme-arm64-v1.apk", "arm64"),
    ("acme-x86_64-v1.apk", "x86_64"),
    ("acme-arm-v1.apk", "arm"),
    ("acme-x86-v1.apk", "x86"),
    ("acme-20260802.apk", None),          # universal — no arch token
])
def test_apk_arch(name, expected):
    assert android.apk_arch(Path(name)) == expected


def test_apk_arch_does_not_let_x86_shadow_x86_64():
    # Longest-first matching: the bug this guards against ships an APK the
    # device cannot run.
    assert android.apk_arch(Path("a-x86_64-v1.apk")) == "x86_64"
    assert android.apk_arch(Path("a-arm64-v1.apk")) == "arm64"


def test_pick_apk_prefers_the_split_for_the_device_abi():
    candidates = [Path("a-x86-v1.apk"), Path("a-arm64-v1.apk"), Path("a-v1.apk")]
    assert android.pick_apk(candidates, "arm64-v8a") == Path("a-arm64-v1.apk")
    assert android.pick_apk(candidates, "armeabi-v7a") == Path("a-v1.apk")  # no arm split


def test_pick_apk_falls_back_to_universal_then_newest():
    assert android.pick_apk([Path("a-x86-v1.apk"), Path("a-v1.apk")], "arm64-v8a") \
        == Path("a-v1.apk")
    assert android.pick_apk([Path("a-x86-v1.apk")], "arm64-v8a") == Path("a-x86-v1.apk")


def test_pick_apk_with_an_unknown_abi():
    assert android.pick_apk([Path("a-arm64-v1.apk")], "riscv64") == Path("a-arm64-v1.apk")


# --------------------------------------------------------------------------- #
# installable filtering
# --------------------------------------------------------------------------- #

def test_installable_drops_the_signing_intermediates(tmp_path):
    made = []
    for name in ("app-release-unsigned.apk", "app-release-aligned.apk",
                 "demo-v1.apk", "demo.aab"):
        p = tmp_path / name
        p.write_bytes(b"x")
        made.append(p)
    assert android.installable_apks(made) == [tmp_path / "demo-v1.apk"]


def test_installable_orders_newest_first(tmp_path):
    import os
    import time

    old, new = tmp_path / "a-v1.apk", tmp_path / "b-v2.apk"
    for p in (old, new):
        p.write_bytes(b"x")
    os.utime(old, (time.time() - 100, time.time() - 100))
    assert android.installable_apks([old, new]) == [new, old]


# --------------------------------------------------------------------------- #
# adb device parsing
# --------------------------------------------------------------------------- #

DEVICES = """List of devices attached
R58M12ABCD             device usb:1-3 product:x model:SM_G991B
emulator-5554          offline
1234567890             unauthorized
"""


def test_adb_devices_separates_ready_from_broken(monkeypatch):
    from butler import proc

    monkeypatch.setattr(proc, "capture",
                        lambda *a, **k: proc.Result(0, DEVICES, ""))
    ready, problems = android.adb_devices("adb", {})
    assert [s for s, _ in ready] == ["R58M12ABCD"]
    assert len(problems) == 2 and "unauthorized" in problems[1]


def test_pick_device_refuses_to_guess_between_several(monkeypatch):
    from butler import proc
    from butler.errors import ButlerError

    two = "List\na device\nb device\n"
    monkeypatch.setattr(proc, "capture", lambda *a, **k: proc.Result(0, two, ""))
    with pytest.raises(ButlerError, match="several devices"):
        android.pick_device("adb", {})


def test_pick_device_explains_an_empty_list(monkeypatch):
    from butler import proc
    from butler.errors import ButlerError

    monkeypatch.setattr(proc, "capture", lambda *a, **k: proc.Result(0, "List\n", ""))
    with pytest.raises(ButlerError, match="no Android device is ready"):
        android.pick_device("adb", {})


# --------------------------------------------------------------------------- #
# keystore location
# --------------------------------------------------------------------------- #

def _ctx(tmp_path):
    import tomllib

    from butler import config
    from butler.context import Ctx

    cfg = config.parse(tomllib.loads(
        '[project]\nname="demo"\n[app]\n[app.android]\n'), tmp_path)
    return Ctx(cfg=cfg)


def test_keystore_prefers_the_vault_outside_the_repo(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "demo").mkdir(parents=True)
    monkeypatch.setenv("ANDROID_KEYSTORE_VAULT", str(vault))
    ctx = _ctx(tmp_path)
    ks = android.keystore_for(ctx, ctx.cfg.app, ctx.cfg.app.android)
    assert ks.jks == vault / "demo" / "demo.jks"
    assert ks.in_repo is False


def test_keystore_falls_back_into_the_repo_only_when_one_is_there(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_KEYSTORE_VAULT", str(tmp_path / "nonexistent"))
    (tmp_path / "app" / ".android").mkdir(parents=True)
    ctx = _ctx(tmp_path)
    ks = android.keystore_for(ctx, ctx.cfg.app, ctx.cfg.app.android)
    assert ks.in_repo is True
    assert ks.jks == tmp_path / "app" / ".android" / "demo.jks"


def test_read_props_needs_all_four_fields(tmp_path):
    ks = android.Keystore(tmp_path, tmp_path / "k.jks", tmp_path / "p.properties", False)
    from butler.config import AndroidConfig

    # An incomplete keystore.properties means "unsigned", not "crash": shipping
    # an unsigned release APK is a legitimate outcome.
    ks.props.write_text("storeFile=/k.jks\nstorePassword=s\n")
    c = AndroidConfig(key_name="demo", dname="", env_prefix="DEMO")
    assert android.read_props(c, ks) is None

    ks.props.write_text("storeFile=/k.jks\nstorePassword=s\nkeyAlias=a\nkeyPassword=p\n")
    assert android.read_props(c, ks)["keyAlias"] == "a"


def test_env_vars_override_the_properties_file(tmp_path, monkeypatch):
    from butler.config import AndroidConfig

    ks = android.Keystore(tmp_path, tmp_path / "k.jks", tmp_path / "p.properties", False)
    ks.props.write_text("storeFile=/k.jks\nstorePassword=s\nkeyAlias=a\nkeyPassword=p\n")
    monkeypatch.setenv("DEMO_ANDROID_KEY_ALIAS", "ci-alias")
    c = AndroidConfig(key_name="demo", dname="", env_prefix="DEMO")
    assert android.read_props(c, ks)["keyAlias"] == "ci-alias"


def test_the_keystore_password_never_reaches_the_command_line(tmp_path, monkeypatch, capsys):
    """butler echoes every command it runs, into terminals and CI logs, and argv
    is world-readable from /proc while apksigner runs. apksigner reads env: for
    exactly this reason."""
    from types import SimpleNamespace

    from butler.config import AndroidConfig, Config, ProjectConfig
    from butler.context import Ctx

    ks = android.Keystore(tmp_path, tmp_path / "k.jks", tmp_path / "p.properties", False)
    ks.props.write_text("storeFile=/k.jks\nstorePassword=s3cret\n"
                        "keyAlias=a\nkeyPassword=k3ypass\n")
    cfg = AndroidConfig(key_name="demo", dname="", env_prefix="DEMO")
    outputs = tmp_path / "outputs" / "apk" / "release"
    outputs.mkdir(parents=True)
    (outputs / "app-release-unsigned.apk").write_bytes(b"x")

    bt = tmp_path / "build-tools"
    bt.mkdir()
    tc = SimpleNamespace(build_tools=bt, env={})
    ctx = Ctx(cfg=Config(root=tmp_path, project=ProjectConfig(name="demo", dist=Path("dist"))))

    seen = {}

    def fake_run(self, cmd, cwd=None, **kw):
        seen[Path(str(cmd[0])).name] = (cmd, kw.get("env") or {})
        return 0

    monkeypatch.setattr(Ctx, "run", fake_run)
    android.sign_release_apks(ctx, cfg, ks, tc, tmp_path / "outputs")

    argv, env = seen["apksigner"]
    printable = " ".join(str(c) for c in argv)
    assert "s3cret" not in printable and "k3ypass" not in printable
    assert "env:BUTLER_KS_PASS" in printable
    assert env["BUTLER_KS_PASS"] == "s3cret" and env["BUTLER_KEY_PASS"] == "k3ypass"


# --------------------------------------------------------------------------- #
# installing never deletes data on its own, and --dry-run installs nothing
# --------------------------------------------------------------------------- #

def _install_ctx(tmp_path, *, dry_run=False, assume_yes=False):
    from butler.config import Config, ProjectConfig
    from butler.context import Ctx

    return Ctx(cfg=Config(root=tmp_path, project=ProjectConfig(name="demo", dist=Path("dist"))),
               dry_run=dry_run, assume_yes=assume_yes)


def _fake_adb(monkeypatch, tmp_path, install_output):
    """A device that answers `install -r` with `install_output`; returns the
    list of adb commands run, so a test can say what never happened."""
    from types import SimpleNamespace

    from butler import proc

    tc = SimpleNamespace(env={}, tool=lambda name: name)
    monkeypatch.setattr(android, "toolchain", lambda cfg, **k: tc)
    monkeypatch.setattr(android, "pick_device", lambda adb, env, device=None: "SERIAL")
    monkeypatch.setattr(android, "app_identifier", lambda app: "com.example.demo")
    ran = []

    def capture(cmd, *a, **k):
        cmd = [str(c) for c in cmd]
        ran.append(cmd)
        if "install" in cmd:
            return proc.Result(1, "", install_output)
        return proc.Result(0, "", "")

    monkeypatch.setattr(proc, "capture", capture)
    apk = tmp_path / "demo-v1.apk"
    apk.write_bytes(b"x")
    return ran, apk


@pytest.mark.parametrize("assume_yes", [False, True])
def test_a_key_mismatch_is_refused_and_never_uninstalls(monkeypatch, tmp_path, assume_yes):
    """Until 0.8.5 this was a y/N prompt that --yes answered by itself: an
    unattended install of a release signed with the wrong key wiped the app's
    data on the device."""
    from butler.errors import ButlerError

    ran, apk = _fake_adb(monkeypatch, tmp_path,
                         "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]")
    with pytest.raises(ButlerError, match="different key") as err:
        android.install_apk(_install_ctx(tmp_path, assume_yes=assume_yes), None, None, [apk])
    assert "--reinstall" in err.value.hint
    assert not any("uninstall" in c for c in ran), "nothing may be uninstalled"


def test_reinstall_is_the_one_way_to_uninstall(monkeypatch, tmp_path):
    from butler.errors import ButlerError

    ran, apk = _fake_adb(monkeypatch, tmp_path, "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]")
    with pytest.raises(ButlerError, match="adb install failed"):
        android.install_apk(_install_ctx(tmp_path), None, None, [apk], reinstall=True)
    assert any("uninstall" in c for c in ran)


def test_install_under_dry_run_runs_no_adb(monkeypatch, tmp_path):
    """proc.capture ignores --dry-run, so before 0.8.5 `-n` really installed."""
    ran, apk = _fake_adb(monkeypatch, tmp_path, "")
    assert android.install_apk(_install_ctx(tmp_path, dry_run=True), None, None, [apk]) == 0
    assert ran == []


def test_build_under_dry_run_does_not_copy_apks_it_never_made(monkeypatch, tmp_path):
    """The dry run "signs" nothing, then copied the APK it never made and died
    with FileNotFoundError (0.8.4 and earlier)."""
    from types import SimpleNamespace

    from butler import sources
    from butler.config import AndroidConfig

    outputs = tmp_path / "gen" / "app" / "build" / "outputs"
    (outputs / "apk" / "universal" / "release").mkdir(parents=True)
    (outputs / "apk" / "universal" / "release" / "app-universal-release-unsigned.apk").write_bytes(b"x")
    cfg = AndroidConfig(key_name="demo", dname="", env_prefix="DEMO")
    for k, v in {"KEYSTORE": "/k.jks", "KS_PASS": "s", "KEY_ALIAS": "a", "KEY_PASS": "p"}.items():
        monkeypatch.setenv(f"DEMO_ANDROID_{k}", v)
    app = SimpleNamespace(dir=tmp_path)
    monkeypatch.setattr(android, "_cfg", lambda ctx: (app, cfg))
    monkeypatch.setattr(sources, "prepare", lambda ctx: None)
    monkeypatch.setattr(android, "toolchain", lambda c, **k: SimpleNamespace(env={}, build_tools=tmp_path))
    monkeypatch.setattr(android, "prepare_gen", lambda *a: None)
    monkeypatch.setattr(android, "gen_dir", lambda a: tmp_path / "gen")
    monkeypatch.setattr(android, "keystore_for", lambda ctx, a, c: android.Keystore(
        tmp_path, tmp_path / "k.jks", tmp_path / "none.properties", False))
    args = SimpleNamespace(debug=False, no_sign=False, universal=False, split_abi=False, install=False)
    ctx = _install_ctx(tmp_path, dry_run=True)
    assert android.build(ctx, args) == 0
    assert not (tmp_path / "dist").exists()
    assert (outputs / "apk" / "universal" / "release" / "app-universal-release-unsigned.apk").exists()

