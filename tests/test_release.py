"""Copying a Forgejo release onto the GitHub mirror.

Nothing here talks to a network: the HTTP layer is replaced, and `gh` is
recorded rather than run. What the tests pin is the part that is easy to get
wrong and impossible to notice afterwards — a truncated asset, a draft release
mirrored as finished, the token following a redirect to object storage, and a
re-run uploading what is already there.
"""

import json
import urllib.request
from pathlib import Path

import pytest

from butler import release
from butler.errors import ButlerError


class FakeCtx:
    """Enough Ctx for the release path: it records commands instead of running
    them, and is never in dry-run unless a test says so."""

    def __init__(self, tmp_path, *, view_ok=False, view_out="", dry_run=False):
        self.root, self.dry_run, self.verbose = tmp_path, dry_run, False
        self.cmds: list[list[str]] = []
        self._view_ok, self._view_out = view_ok, view_out

    def would(self, description):
        return self.dry_run

    def capture(self, cmd, cwd=None, **kw):
        from butler import proc
        self.cmds.append([str(c) for c in cmd])
        return proc.Result(0 if self._view_ok else 1, self._view_out, "")

    def check(self, cmd, cwd=None, **kw):
        self.cmds.append([str(c) for c in cmd])


def api(**over):
    meta = {
        "name": "chords 1.0",
        "body": "the notes\n",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "app.AppImage", "size": 3,
             "browser_download_url": "https://git.example.org/dl/app.AppImage"},
        ],
    }
    meta.update(over)
    return meta


def serve(monkeypatch, meta, payload=b"abc"):
    """Answer the metadata request with `meta` and every download with
    `payload`, remembering the token each request carried."""
    seen = {"tokens": []}

    def _get(url, tok):
        seen["tokens"].append(tok)
        seen.setdefault("urls", []).append(url)
        if "/api/v1/" in url:
            return json.dumps(meta).encode()
        return payload

    monkeypatch.setattr(release, "_get", _get)
    return seen


# ---- fetching -------------------------------------------------------------- #

def test_fetch_downloads_every_asset_and_the_notes(tmp_path, monkeypatch):
    serve(monkeypatch, api())
    monkeypatch.setattr(release, "token", lambda host: "t")
    rel = release.fetch(FakeCtx(tmp_path), host="git.example.org",
                        repo="acme/demo", tag="v1.0")
    assert rel.names == ["app.AppImage"]
    assert rel.title == "chords 1.0" and rel.notes == "the notes\n"
    assert rel.assets[0].read_bytes() == b"abc"
    rel.cleanup()


def test_a_release_without_a_title_falls_back_to_the_tag(tmp_path, monkeypatch):
    serve(monkeypatch, api(name=""))
    monkeypatch.setattr(release, "token", lambda host: "t")
    rel = release.fetch(FakeCtx(tmp_path), host="h", repo="a/b", tag="v1.0")
    assert rel.title == "v1.0"
    rel.cleanup()


def test_a_draft_release_is_refused(tmp_path, monkeypatch):
    # Its build has not finished uploading, so mirroring it would publish an
    # incomplete set of installers.
    serve(monkeypatch, api(draft=True))
    monkeypatch.setattr(release, "token", lambda host: "t")
    with pytest.raises(ButlerError, match="still a draft"):
        release.fetch(FakeCtx(tmp_path), host="h", repo="a/b", tag="v1.0")


def test_a_release_with_no_assets_is_refused(tmp_path, monkeypatch):
    serve(monkeypatch, api(assets=[]))
    monkeypatch.setattr(release, "token", lambda host: "t")
    with pytest.raises(ButlerError, match="no assets"):
        release.fetch(FakeCtx(tmp_path), host="h", repo="a/b", tag="v1.0")


def test_a_truncated_download_is_refused(tmp_path, monkeypatch):
    # A short asset is a release that installs nothing, and the size the
    # release lists is the only thing to catch it with.
    serve(monkeypatch, api(), payload=b"a")
    monkeypatch.setattr(release, "token", lambda host: "t")
    with pytest.raises(ButlerError, match="downloaded 1 bytes"):
        release.fetch(FakeCtx(tmp_path), host="h", repo="a/b", tag="v1.0")


@pytest.mark.parametrize("name", ["../../etc/passwd", "a/b.zip", ".hidden", "with space"])
def test_an_asset_name_becomes_a_path_so_it_is_checked(tmp_path, monkeypatch, name):
    serve(monkeypatch, api(assets=[{"name": name, "size": 3,
                                    "browser_download_url": "https://h/dl"}]))
    monkeypatch.setattr(release, "token", lambda host: "t")
    with pytest.raises(ButlerError, match="unexpected asset name"):
        release.fetch(FakeCtx(tmp_path), host="h", repo="a/b", tag="v1.0")


def test_dry_run_downloads_nothing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry run must not fetch")

    monkeypatch.setattr(release, "_get", boom)
    monkeypatch.setattr(release, "token", boom)
    assert release.fetch(FakeCtx(tmp_path, dry_run=True),
                         host="h", repo="a/b", tag="v1.0") is None


# ---- the token ------------------------------------------------------------- #

def test_a_missing_token_names_what_to_set(tmp_path, monkeypatch):
    monkeypatch.setattr(release.secrets, "lookup", lambda names, file=None: None)
    with pytest.raises(ButlerError, match="no Forgejo token for git.example.org"):
        release.token("git.example.org")


def test_the_token_value_is_never_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(release.secrets, "lookup",
                        lambda names, file=None: ("FORGEJO_TOKEN_READONLY", "s3cret"))
    assert release.token("h") == "s3cret"
    out = capsys.readouterr()
    assert "FORGEJO_TOKEN_READONLY" in out.out
    assert "s3cret" not in out.out + out.err


def test_the_token_does_not_follow_a_redirect_off_the_forgejo_host():
    # Forgejo redirects an asset download to its object storage. urllib re-sends
    # every header across a redirect, which would hand a token to a service it
    # was not issued for; curl drops it, and so must this.
    handler = release._DropAuthOnCrossHostRedirect()
    req = urllib.request.Request("https://git.example.org/dl/app",
                                 headers={"Authorization": "token s3cret"})

    class FP:
        def read(self, *a):
            return b""

    new = handler.redirect_request(req, FP(), 302, "Found", {},
                                   "https://objects.example.net/bucket/app")
    assert not any(k.lower() == "authorization" for k in new.headers)

    same = handler.redirect_request(req, FP(), 302, "Found", {},
                                    "https://git.example.org/attachments/app")
    assert any(k.lower() == "authorization" for k in same.headers)


# ---- mirroring ------------------------------------------------------------- #

def made(tmp_path, **over) -> release.Release:
    asset = tmp_path / "app.AppImage"
    asset.write_bytes(b"abc")
    kw = dict(tag="v1.0", title="t", notes="n", prerelease=False, assets=[asset])
    kw.update(over)
    return release.Release(**kw)


def test_a_new_release_is_created_with_its_notes_and_assets(tmp_path):
    ctx = FakeCtx(tmp_path)
    rel = made(tmp_path)
    release.mirror(ctx, rel, github="acme/demo")
    create = [c for c in ctx.cmds if c[:3] == ["gh", "release", "create"]][0]
    assert "acme/demo" in create and "--notes-file" in create
    # gh would otherwise create a tag of its own, on whatever the default
    # branch points at; the tag must be the one the export just published.
    assert "--verify-tag" in create
    assert str(rel.assets[0]) in create
    assert "--prerelease" not in create


def test_a_prerelease_tag_stays_a_prerelease(tmp_path):
    ctx = FakeCtx(tmp_path)
    release.mirror(ctx, made(tmp_path, prerelease=True), github="acme/demo")
    create = [c for c in ctx.cmds if c[:3] == ["gh", "release", "create"]][0]
    assert "--prerelease" in create


def test_a_rerun_uploads_only_the_missing_assets(tmp_path):
    # A run that failed after creating the release is repeated, not undone.
    second = tmp_path / "app.exe"
    second.write_bytes(b"xyz")
    rel = made(tmp_path)
    rel.assets.append(second)
    ctx = FakeCtx(tmp_path, view_ok=True, view_out="app.AppImage\n")
    release.mirror(ctx, rel, github="acme/demo")
    upload = [c for c in ctx.cmds if c[:3] == ["gh", "release", "upload"]][0]
    assert str(second) in upload
    assert str(rel.assets[0]) not in upload


def test_a_complete_release_is_left_alone(tmp_path):
    ctx = FakeCtx(tmp_path, view_ok=True, view_out="app.AppImage\n")
    release.mirror(ctx, made(tmp_path), github="acme/demo")
    assert not any(c[:3] == ["gh", "release", "upload"] for c in ctx.cmds)
    assert not any(c[:3] == ["gh", "release", "create"] for c in ctx.cmds)


def test_gh_is_required_before_anything_is_pushed(monkeypatch):
    monkeypatch.setattr(release.proc, "which", lambda tool, extra_path=(): None)
    with pytest.raises(ButlerError, match="GitHub CLI"):
        release.require_gh()
