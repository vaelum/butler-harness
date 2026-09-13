import json
import tomllib
import zipfile
from pathlib import Path

import pytest

from butler import config
from butler.components import extension
from butler.context import Ctx
from butler.errors import ButlerError, ConfigError

CHROME_MANIFEST = {
    "manifest_version": 3,
    "name": "Demo Importer",
    "version": "1.0.0",
    "permissions": ["storage", "scripting"],
    "background": {"service_worker": "background.js"},
    "options_page": "options.html",
}

TOML = """
[project]
name = "chords"
[extension]
dir = "extension"
[extension.firefox]
id = "chords-importer@vaelum.de"
min_version = "115.0"
"""


def make(tmp_path: Path, text: str = TOML) -> Ctx:
    ext = tmp_path / "extension"
    (ext / "assets").mkdir(parents=True)
    (ext / "manifest.json").write_text(json.dumps(CHROME_MANIFEST, indent=2))
    (ext / "background.js").write_text("// listeners at top level\n")
    (ext / "assets" / "icon.png").write_bytes(b"png")
    cfg = config.parse(tomllib.loads(text), tmp_path)
    return Ctx(cfg=cfg)


class Args:
    label = "20260802-120000"


def test_packages_both_bundles(tmp_path):
    ctx = make(tmp_path)
    assert extension.package(ctx, Args()) == 0
    names = sorted(p.name for p in (tmp_path / "dist").iterdir())
    assert names == ["chords-extension-chrome-20260802-120000.zip",
                     "chords-extension-firefox-20260802-120000.zip"]


def test_artifact_name_defaults_to_the_project_name(tmp_path):
    ctx = make(tmp_path)
    assert ctx.cfg.extension.artifact == "chords-extension"


def test_chrome_bundle_carries_the_manifest_unchanged(tmp_path):
    ctx = make(tmp_path)
    extension.package(ctx, Args())
    z = zipfile.ZipFile(tmp_path / "dist" / "chords-extension-chrome-20260802-120000.zip")
    assert json.loads(z.read("manifest.json")) == CHROME_MANIFEST
    assert sorted(z.namelist()) == ["assets/icon.png", "background.js", "manifest.json"]


def test_firefox_bundle_swaps_only_what_firefox_needs(tmp_path):
    ctx = make(tmp_path)
    extension.package(ctx, Args())
    z = zipfile.ZipFile(tmp_path / "dist" / "chords-extension-firefox-20260802-120000.zip")
    m = json.loads(z.read("manifest.json"))

    # An event page, not a service worker.
    assert m["background"] == {"scripts": ["background.js"]}
    assert m["browser_specific_settings"]["gecko"] == {
        "id": "chords-importer@vaelum.de", "strict_min_version": "115.0"}
    # Everything else is identical — the point of deriving rather than forking.
    for key in ("manifest_version", "name", "version", "permissions", "options_page"):
        assert m[key] == CHROME_MANIFEST[key]
    # The same background.js ships in both.
    assert z.read("background.js") == b"// listeners at top level\n"


def test_the_source_manifest_is_never_mutated(tmp_path):
    ctx = make(tmp_path)
    extension.package(ctx, Args())
    on_disk = json.loads((tmp_path / "extension" / "manifest.json").read_text())
    assert on_disk == CHROME_MANIFEST


def test_without_a_firefox_section_only_chrome_is_built(tmp_path):
    ctx = make(tmp_path, '[project]\nname="chords"\n[extension]\n')
    extension.package(ctx, Args())
    assert [p.name for p in (tmp_path / "dist").iterdir()] == [
        "chords-extension-chrome-20260802-120000.zip"]


def test_firefox_section_requires_an_id(tmp_path):
    with pytest.raises(ConfigError, match="missing the required key 'id'"):
        make(tmp_path, '[project]\nname="c"\n[extension]\n[extension.firefox]\n')


def test_zips_are_reproducible(tmp_path):
    """Sorted entries, so two runs of the same source are byte-identical."""
    ctx = make(tmp_path)
    extension.package(ctx, Args())
    first = (tmp_path / "dist" / "chords-extension-chrome-20260802-120000.zip").read_bytes()
    extension.package(ctx, Args())
    second = (tmp_path / "dist" / "chords-extension-chrome-20260802-120000.zip").read_bytes()
    assert first == second


def test_missing_manifest_is_a_clean_error(tmp_path):
    ctx = make(tmp_path)
    (tmp_path / "extension" / "manifest.json").unlink()
    with pytest.raises(ButlerError, match="no manifest.json"):
        extension.package(ctx, Args())


def test_dry_run_writes_nothing(tmp_path):
    ctx = make(tmp_path)
    ctx.dry_run = True
    assert extension.package(ctx, Args()) == 0
    assert not (tmp_path / "dist").exists()


def test_label_defaults_to_the_release_label(tmp_path):
    ctx = make(tmp_path)

    class NoLabel:
        label = None

    extension.package(ctx, NoLabel())
    names = sorted(p.name for p in (tmp_path / "dist").iterdir())
    # No git tag here, so the label is the UTC stamp: <base>-<flavour>-YYYYmmdd-HHMMSS.zip
    assert [n[:-len("-20260802-120000.zip")] for n in names] == [
        "chords-extension-chrome", "chords-extension-firefox"]
