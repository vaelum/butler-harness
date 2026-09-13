"""Copying a release built on Forgejo onto the GitHub mirror.

The installers are built where the code is private: a `v*` tag pushed to Forgejo
runs the project's CI, which publishes a Forgejo release with every artifact
attached. The mirror has no CI of its own — it is generated, and a workflow file
is one of the things the export holds back — so the public release is not built
again, it is *copied*: assets, notes, title and pre-release flag.

Two properties are worth stating, because both are load-bearing and neither is
obvious from the code:

  * everything here is fetched and checked BEFORE the export pushes anything.
    A build that is still running, a draft, an empty release — each stops the
    run with nothing published, rather than leaving a public tag whose release
    never arrives.
  * a run that failed partway can simply be repeated. A GitHub release that
    already exists gets only the assets it is missing.
"""

from __future__ import annotations

import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import proc, secrets, ui
from .context import Ctx
from .errors import ButlerError

# An asset name becomes a local filename and then a GitHub asset name, so it is
# checked rather than trusted: no separators, no leading dot, no surprises.
ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

TIMEOUT = 60


@dataclass
class Release:
    """One Forgejo release, downloaded and ready to be mirrored."""
    tag: str
    title: str
    notes: str
    prerelease: bool
    assets: list[Path] = field(default_factory=list)
    # The directory the assets were downloaded into, owned by this object: the
    # caller closes it in a finally, so a failed run leaves no installers in
    # /tmp for the next person to wonder about.
    _tmp: tempfile.TemporaryDirectory | None = None

    @property
    def names(self) -> list[str]:
        return [a.name for a in self.assets]

    @property
    def directory(self) -> Path:
        return Path(self._tmp.name) if self._tmp else Path(tempfile.gettempdir())

    def cleanup(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


class _DropAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Forgejo redirects an asset download to wherever it keeps its objects —
    S3, here. urllib re-sends every header on a redirect, which would hand the
    Forgejo token to a service it was not issued for (and, in the S3 case, to
    one that logs its requests). Curl drops the header across hosts; so do we.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _host(newurl) != _host(req.full_url):
            # Header capitalisation is urllib's, not ours: it title-cases what
            # add_header stored, so both spellings are removed.
            for key in ("Authorization", "authorization"):
                new.headers.pop(key, None)
                new.unredirected_hdrs.pop(key, None)
        return new


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_DropAuthOnCrossHostRedirect())


def token(host: str) -> str:
    """The Forgejo token to read the release with, from the environment or the
    machine's secrets file."""
    names = secrets.forgejo_names(host)
    found = secrets.lookup(names)
    if found is None:
        raise ButlerError(
            f"no Forgejo token for {host}",
            hint="Copying a release needs a token that can read the repository\n"
                 f"(scope read:repository). Export one of {', '.join(names)},\n"
                 f"or put it in {secrets.path()}.")
    name, value = found
    ui.plain(ui.dim(f"  using {name}"))
    return value


def _get(url: str, tok: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"token {tok}"})
    with _opener().open(req, timeout=TIMEOUT) as r:
        return r.read()


def fetch(ctx: Ctx, *, host: str, repo: str, tag: str) -> Release | None:
    """Download the Forgejo release for `tag`, refusing anything unfinished.

    Returns None under --dry-run, where nothing is downloaded and nothing will
    be published either.
    """
    api = f"https://{host}/api/v1/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    if ctx.would(f"download the Forgejo release for {tag} from {host}/{repo}"):
        return None

    ui.plain(ui.bold(f"the Forgejo release for {tag}"))
    tok = token(host)
    try:
        meta = json.loads(_get(api, tok))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ButlerError(
                f"no Forgejo release for '{tag}' yet",
                hint=f"Is its build still running? https://{host}/{repo}/actions") from e
        raise ButlerError(f"could not read the release for '{tag}': HTTP {e.code}") from e
    except OSError as e:
        raise ButlerError(f"could not reach {host}: {e}") from e

    if meta.get("draft"):
        raise ButlerError(f"the Forgejo release for '{tag}' is still a draft",
                          hint="Its build has not finished uploading.")
    listed = meta.get("assets") or []
    if not listed:
        raise ButlerError(f"the Forgejo release for '{tag}' has no assets")

    tmp = tempfile.TemporaryDirectory(prefix=f"butler-release-{tag}-")
    into = Path(tmp.name)
    rel = Release(tag=tag,
                  title=(meta.get("name") or "").strip() or tag,
                  notes=meta.get("body") or "",
                  prerelease=bool(meta.get("prerelease")),
                  _tmp=tmp)
    for asset in listed:
        name, size = asset.get("name", ""), asset.get("size")
        if not ASSET_NAME.match(name):
            raise ButlerError(f"unexpected asset name {name!r} on the release for '{tag}'")
        dest = into / name
        ui.plain(f"  fetching {name}")
        try:
            dest.write_bytes(_get(asset["browser_download_url"], tok))
        except OSError as e:
            raise ButlerError(f"could not download {name}: {e}") from e
        # A truncated download is a release that installs nothing. The size the
        # release lists is the only thing to check it against.
        got = dest.stat().st_size
        if size is not None and got != size:
            raise ButlerError(f"{name}: downloaded {got} bytes, the release lists {size}")
        rel.assets.append(dest)
    return rel


def mirror(ctx: Ctx, rel: Release, *, github: str) -> None:
    """Publish `rel` on the GitHub mirror, on the tag of the same name.

    `gh` rather than the REST API: it already holds the credential, and an
    upload is a multipart request this harness has no business reimplementing.
    """
    existing = ctx.capture(["gh", "release", "view", rel.tag, "-R", github,
                            "--json", "assets", "-q", ".assets[].name"])
    if existing.ok:
        have = set(existing.out.split())
        missing = [a for a in rel.assets if a.name not in have]
        if not missing:
            ui.ok(f"GitHub release {rel.tag} is already complete")
            return
        ctx.check(["gh", "release", "upload", rel.tag, "-R", github, *missing],
                  what="upload the missing release assets")
        ui.ok(f"uploaded {len(missing)} missing asset(s)", f"-> {github} {rel.tag}")
        return

    notes = rel.directory / "notes.md"
    if not ctx.would(f"write the release notes for {rel.tag}"):
        notes.write_text(rel.notes)
    cmd = ["gh", "release", "create", rel.tag, "-R", github,
           # The tag must already be on the mirror: publishing the release is
           # the last step, after the export pushed it. Without this gh would
           # helpfully create a tag of its own, on whatever the default branch
           # happens to point at.
           "--verify-tag",
           "--title", rel.title, "--notes-file", str(notes)]
    if rel.prerelease:
        cmd.append("--prerelease")
    # gh creates the release as a draft, uploads every asset, then publishes it,
    # so a half-uploaded release is never visible.
    ctx.check([*cmd, *rel.assets], what="create the GitHub release")
    ui.ok(f"published GitHub release {rel.tag}", f"-> {github}")


def require_gh() -> None:
    if proc.which("gh") is None:
        raise ButlerError("copying a release needs the GitHub CLI (gh), and it is not installed",
                          hint="Install gh and run `gh auth login`.")
