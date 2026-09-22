"""The `release` component: cut a version and take it all the way out.

Every release in this fleet went the same way by hand, and the steps are the
same everywhere because the shape of the setup is: the work happens on a
development branch, the publication branch gains exactly **one** commit per
release, a `v*` tag on it starts the Forgejo build that produces the installers
and publishes the Forgejo release, and only then does the mirror get the export,
the public tag and a copy of that release.

Written out, that is:

    git checkout main && git merge --squash dev && git commit
    git tag -a v1.2.3 && git push origin main v1.2.3
    …watch the build…
    butler publish --tag v1.2.3

Each of those is easy; the order is what is easy to get wrong, and the two
places it goes wrong are both expensive. Exporting before the build finishes
puts a public tag on the mirror whose release never arrives. Tagging a version
whose CHANGELOG entry was forgotten produces a release with no notes — which is
exactly how the two `v2026.9.2` re-tags in chords' history happened.

So this component is the order, the checks between the steps, and one thing the
hand-run sequence could not do safely: re-cutting the same version after a
failed build (`--retag`). A failed build has published nothing, so the version
number is still free — the squash commit is rebuilt from the fixed work branch,
the tag moves onto it, and both are force-pushed. The publication branch still
gains exactly one commit for the release, which is the property that makes its
history readable at all.

What it will not do is move a tag whose release exists. That release names a
commit; moving the tag would leave it describing one nobody can check out.
"""

from __future__ import annotations

import re
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

from .. import release as forgejo, ui, vcs
from ..command import Node, arg
from ..config import ReleaseConfig
from ..context import Ctx
from ..errors import ButlerError
from . import publish as publish_component

# In order. `--from` names one of these and the ones before it are skipped,
# which is how a run that died in the export is finished without touching the
# branch it already merged.
STEPS = ("prepare", "merge", "tag", "push", "wait", "land", "export")

# `version = "1.2.3"`, `"version": "1.2.3"`, `__version__ = "1.2.3"`. Enough to
# recognise the declaration in a pyproject.toml, a Cargo.toml, a package.json or
# a module — and specific enough that a version mentioned in prose nearby does
# not count as one.
#
# The key must start its line, which is what keeps `rust-version = "1.77"` and
# a dependency's inline `{ version = "2" }` out: those are versions of something
# else, and this is the pattern the release WRITES through. The value is
# captured, so the declaration can be read as well as matched.
VERSION_DECL = re.compile(
    r"""^(?P<head>[ \t]*(?:__version__|["']?version["']?)[ \t]*[:=][ \t]*["']?)"""
    r"""(?P<version>[^"'\s,}\]]+)""",
    re.M)


def _cfg(ctx: Ctx) -> ReleaseConfig:
    if ctx.cfg.release is None:
        raise ButlerError(
            "this project has no [release] configuration",
            hint="Add a [release] section to butler/butler.toml naming the\n"
                 "work branch and the branch releases are merged into.")
    return ctx.cfg.release


# --------------------------------------------------------------------------- #
# what a release is, before anything moves
# --------------------------------------------------------------------------- #

@dataclass
class Plan:
    """Everything the run needs, resolved before the first mutation."""
    version: str
    tag: str
    subject: str
    notes: str
    # The commit the publication branch had BEFORE this release's squash
    # commit — the point a re-cut rewinds to, so the branch ends up one commit
    # ahead of it again rather than two.
    base: str
    retag: bool
    # The line holding "## [Unreleased]", when the notes came from there and
    # the heading is to be renamed to this version before the squash. None when
    # the CHANGELOG already names the version.
    promote: int | None = None
    # The version_files still declaring some other version, which the cut
    # writes this one into. Empty when they already agree with the tag.
    bump: tuple[str, ...] = ()


def _split_version(cfg: ReleaseConfig, given: str) -> tuple[str, str]:
    """`1.2.3` and `v1.2.3` both name the same release; take either."""
    version = given.strip()
    if cfg.tag_prefix and version.startswith(cfg.tag_prefix):
        version = version[len(cfg.tag_prefix):]
    if not version:
        raise ButlerError(f"{given!r} is not a version")
    return version, cfg.tag_for(version)


@dataclass
class Notes:
    """The release notes, and where they came from."""
    text: str
    # Set when they came from an "## [Unreleased]" heading on this line, which
    # the release renames to the version it is cutting.
    promote: int | None = None


# "## [Unreleased]", however it is capitalised and bracketed. Keep a Changelog
# calls it that, and every changelog in this fleet follows that format.
UNRELEASED = re.compile(r"^##\s*\[?unreleased\]?\s*$", re.I)


def _section(lines: list[str], heading: re.Pattern[str]) -> tuple[int, str] | None:
    """The first section whose heading matches, as (line index, body)."""
    for i, line in enumerate(lines):
        if not heading.match(line):
            continue
        body = []
        for later in lines[i + 1:]:
            if later.startswith("## "):
                break
            body.append(later)
        return i, "\n".join(body).strip("\n")
    return None


def read_notes(root: Path, cfg: ReleaseConfig, version: str) -> Notes:
    """The CHANGELOG section for `version`, verbatim — or the Unreleased one.

    Read here rather than left to CI because this is the last moment it can be
    fixed cheaply. A missing entry discovered by the build has already cost a
    tag, a push and however long the build ran.

    Writing the version into the heading is the release's job, not the author's:
    while the work is on the work branch nobody knows which version it will go
    out as, so it accumulates under "## [Unreleased]" and the cut renames that
    heading to the number it is cutting. Refusing a release over a heading the
    release itself is about to write would be a chore invented by the tool.
    """
    if cfg.changelog is None:
        return Notes("")
    path = root / cfg.changelog
    if not path.is_file():
        raise ButlerError(f"no {cfg.changelog} in this project",
                          hint="Write one, or set [release] changelog = \"\" to\n"
                               "stop butler looking for release notes.")
    lines = path.read_text().splitlines()
    # "## [1.2.3]" or "## 1.2.3 — title", up to the next heading of that level.
    named = _section(lines, re.compile(rf"^##\s*\[?{re.escape(version)}\]?(\D|$)"))
    if named and named[1].strip():
        return Notes(named[1] + "\n")

    # Only when the version has no section at all. One that is there but empty
    # is a section somebody started and left; promoting Unreleased on top of it
    # would put the same heading in the file twice.
    if named is None:
        unreleased = _section(lines, UNRELEASED)
        if unreleased and unreleased[1].strip():
            return Notes(unreleased[1] + "\n", promote=unreleased[0])

    raise ButlerError(
        f"{cfg.changelog} has no entry for {version}",
        hint=f"Add a '## [{version}]' section, or write the notes under\n"
             "'## [Unreleased]' and the cut will rename that heading.\n"
             "They become the release notes, and a release without them is\n"
             "one nobody can read.")


def declared_version(text: str) -> re.Match[str] | None:
    """The project's own version declaration: the first one in the file.

    First, because that is the project's — a Cargo.toml's `[package] version`
    comes before its dependencies', a package.json's before everything it
    depends on. Anything further down belongs to something else.
    """
    return VERSION_DECL.search(text)


def stale_version_files(root: Path, cfg: ReleaseConfig, version: str) -> list[str]:
    """The version_files that do not declare `version` yet, for the cut to write.

    chords shipped `v2026.9.2` and then had to ship it again because the app's
    version had stayed behind; the tag is the only thing that says which
    version a build is, and it is the one thing no build reads. Refusing over
    it made the author do by hand what the version number on the command line
    already said, so the release writes it instead — and a file it cannot find
    a declaration in is still a refusal, because guessing where the version
    goes in a file butler does not understand is how the wrong line gets
    rewritten.
    """
    stale = []
    for name in cfg.version_files:
        path = root / name
        if not path.is_file():
            raise ButlerError(f"[release] version_files names {name}, which does not exist")
        found = declared_version(path.read_text())
        if found is None:
            raise ButlerError(
                f"no version declaration in {name}",
                hint="[release] version_files names the files whose version the\n"
                     "cut writes. butler looks for a line whose key is exactly\n"
                     "'version' (or '__version__'), and there is none here.")
        if found["version"] != version:
            stale.append(name)
    return stale


def bump_version_file(path: Path, version: str) -> bool:
    """Write `version` into the file's own version declaration.

    Only that one declaration, and only its value: everything around it — the
    quoting, the spacing, the rest of the file — is left exactly as it was,
    because this runs over files whose format butler does not otherwise parse.
    """
    text = path.read_text()
    found = declared_version(text)
    if found is None:
        raise ButlerError(f"no version declaration in {path.name} any more")
    if found["version"] == version:
        return False
    path.write_text(text[:found.start("version")] + version + text[found.end("version"):])
    return True


# --------------------------------------------------------------------------- #
# git state
# --------------------------------------------------------------------------- #

def _sha(ctx: Ctx, rev: str) -> str | None:
    r = ctx.capture(["git", "rev-parse", "-q", "--verify", f"{rev}^{{commit}}"])
    return r.out.strip() if r.ok and r.out.strip() else None


def _remote_tag(ctx: Ctx, tag: str) -> str | None:
    """The commit `tag` names on origin, following an annotated tag to it."""
    listing = ctx.capture(["git", "ls-remote", "origin",
                           f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
    found = ""
    for line in listing.out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/tags/{tag}^{{}}":
            found = sha.strip()
        elif ref.strip() == f"refs/tags/{tag}" and not found:
            found = sha.strip()
    return found or None


def _current_branch(ctx: Ctx) -> str:
    return ctx.capture(["git", "symbolic-ref", "--quiet", "--short", "HEAD"]).out.strip()


def preflight(ctx: Ctx, cfg: ReleaseConfig, args, version: str, tag: str) -> Plan:
    """Everything that can be refused is refused here, before anything moves.

    Ordering matters in one direction only: nothing in this function writes, so
    a run that stops in it has changed nothing anywhere.
    """
    root = ctx.root
    if not (root / ".git").is_dir():
        raise ButlerError("this project is not a git checkout")
    vcs.require_clean(root, hint=f"A release is cut from what is on origin. "
                                 f"Commit them on\n'{cfg.source}' and push, or "
                                 f"stash them.")

    on = _current_branch(ctx)
    if on == cfg.into:
        raise ButlerError(
            f"this checkout is on '{cfg.into}', the branch the release writes",
            hint=f"A release is cut from the work branch:\n  git checkout {cfg.source}")

    notes = read_notes(root, cfg, version)
    bump = tuple(stale_version_files(root, cfg, version))
    if (notes.promote is not None or bump) and on != cfg.source:
        writes = []
        if notes.promote is not None:
            writes.append(f"the notes are under '## [Unreleased]'")
        if bump:
            writes.append(f"{', '.join(bump)} still declare(s) another version")
        raise ButlerError(
            f"{'; and '.join(writes)} — and this checkout is on '{on}'",
            hint=f"Writing those is a commit on the work branch:\n"
                 f"  git checkout {cfg.source}")
    subject = cfg.subject(ctx.name, version)

    # Branches only. `--tags` would try to update every local tag from origin
    # and fail the whole run on "would clobber existing tag" — which is exactly
    # the state a re-cut is in, its tag moved locally and not yet pushed. What
    # origin has under a tag is read with ls-remote, where it cannot be
    # confused with what this checkout has.
    ctx.check(["git", "fetch", "--quiet", "--no-tags", "origin"], echo=False,
              what="fetch origin")
    source = _sha(ctx, cfg.source)
    if source is None:
        raise ButlerError(f"there is no '{cfg.source}' branch",
                          hint="[release] 'source' is the branch the work happens on.")
    remote_source = _sha(ctx, f"origin/{cfg.source}")
    if remote_source is None:
        raise ButlerError(f"'{cfg.source}' has never been pushed",
                          hint=f"  git push -u origin {cfg.source}")
    # The build runs on what Forgejo has, and the export reads the same. Local
    # work left behind here would be missing from the release without anything
    # failing — the worst shape a mistake can take.
    if source != remote_source:
        raise ButlerError(
            f"local '{cfg.source}' differs from origin/{cfg.source}",
            hint=f"The build and the export both read Forgejo. Push first:\n"
                 f"  git push origin {cfg.source}")

    origin_into = _sha(ctx, f"origin/{cfg.into}") or _sha(ctx, cfg.into)
    if origin_into is None:
        raise ButlerError(f"there is no '{cfg.into}' branch, locally or on origin",
                          hint="Create it before the first release:\n"
                               f"  git branch {cfg.into} {cfg.source}")

    # A release is going to be published on that forge, so ask now whether it
    # can be. The alternative is finding out from the build, after the tag.
    pub = ctx.cfg.publish
    if pub is not None and pub.release and not ctx.dry_run:
        if not forgejo.releases_enabled(pub.host, pub.forgejo):
            raise ButlerError(
                f"{pub.host}/{pub.forgejo} has Releases turned off",
                hint="Its CI publishes a release for the tag, and every releases\n"
                     "endpoint on that repository answers 404 while the unit is\n"
                     "disabled. Turn it on in the repository settings, or set\n"
                     "[publish] release = false if this project has no release\n"
                     "to copy.")

    local_tag, remote = _sha(ctx, tag), _remote_tag(ctx, tag)
    existing = local_tag or remote
    # A run resuming after the tag step is *expected* to find its tag: that is
    # what finishing a release whose build has since gone green looks like.
    # Only a run that would create one is refused for finding it there.
    resuming = args.from_step in ("push", "wait", "land", "export")
    if existing and not args.retag and not resuming:
        raise ButlerError(
            f"{tag} already exists",
            hint="If its build failed, nothing has been published yet and the\n"
                 f"same version can be re-cut:\n  butler.py release "
                 f"{version} --retag\nOtherwise cut the next version.")
    if args.retag and not existing:
        ui.warn("note:", f"--retag was given but {tag} does not exist yet; "
                         "cutting it normally.")
    retag = bool(existing) and args.retag

    # Where the release commit goes on top of. The publication branch is
    # written by releases only, so anything else sitting on it is a mistake
    # worth stopping for — with one exception that is not a mistake at all: an
    # earlier attempt at THIS release, which every run of this component makes
    # locally before it is pushed.
    base = origin_into
    if retag:
        _refuse_a_published_retag(ctx, cfg, tag)
        # What ORIGIN has under the tag, preferentially: that is the release
        # whose build failed, the one the branch may be carrying. The local tag
        # may already have been moved by an earlier, half-finished re-cut, and
        # reading that one would measure this run against itself.
        release_commit = remote or local_tag
        parent = _sha(ctx, f"{release_commit}^")
        if parent is None:
            raise ButlerError(f"the commit {tag} names has no parent to rewind to")
        # Two shapes are both fine, because the branch may or may not have
        # landed before the build failed: it is either still under the release
        # (the normal case now that the tag is pushed first) or on it (a run
        # told not to wait, or one from before that order changed).
        if origin_into == release_commit:
            base = parent
        elif origin_into == parent:
            base = origin_into
        else:
            raise ButlerError(
                f"'{cfg.into}' is not where {tag} was cut from, so re-cutting "
                f"it would rewrite commits butler did not make",
                hint=f"origin/{cfg.into} is at {origin_into[:12]}; {tag} names "
                     f"{release_commit[:12]} on {parent[:12]}.\nSort that out by hand.")

    local_into = _sha(ctx, cfg.into)
    if local_into is not None and local_into not in (base, origin_into):
        # An earlier attempt's release commit for this version is expected:
        # `merge` rebuilds it in place. Anything else is someone's own work.
        if not _is_attempt(ctx, local_into, base, subject):
            raise ButlerError(
                f"local '{cfg.into}' is not what origin has",
                hint=f"  local  {local_into[:12]}\n  origin {origin_into[:12]}\n"
                     f"'{cfg.into}' is written by releases only. Reconcile it "
                     f"first:\n  git checkout {cfg.into} && "
                     f"git reset --hard origin/{cfg.into}")

    return Plan(version=version, tag=tag, subject=subject, notes=notes.text,
                base=base, retag=retag, promote=notes.promote, bump=bump)


def _is_attempt(ctx: Ctx, commit: str, base: str, subject: str) -> bool:
    """Whether `commit` is this release's commit from an earlier, unfinished
    run of this command — one commit, on `base`, with the release subject."""
    got = ctx.capture(["git", "show", "-s", "--format=%P%n%s", commit]).out.splitlines()
    return len(got) >= 2 and got[0].split() == [base] and got[1].strip() == subject


def _refuse_a_published_retag(ctx: Ctx, cfg: ReleaseConfig, tag: str) -> None:
    """A re-cut is safe only while nothing has been published under that tag.

    Both places are checked, because either one alone can be the published one:
    the Forgejo release exists as soon as the build finishes, and the mirror's
    tag exists as soon as an export ran.
    """
    pub = ctx.cfg.publish
    if pub is None or ctx.dry_run:
        return
    if forgejo.published(pub.host, pub.forgejo, tag):
        raise ButlerError(
            f"{tag} already has a release on {pub.host}/{pub.forgejo}",
            hint="Its build finished, so the version is spent. Moving the tag\n"
                 "would leave that release describing a commit nobody can\n"
                 "check out. Cut the next version instead.")
    if _mirror_has_tag(ctx, pub, tag):
        raise ButlerError(
            f"{tag} is already on github.com/{pub.github}",
            hint="It has been exported, so it is public. Cut the next version.")


def _mirror_has_tag(ctx: Ctx, pub, tag: str) -> bool:
    return bool(ctx.capture(["git", "ls-remote", pub.destination_url,
                             f"refs/tags/{tag}"]).out.strip())


# --------------------------------------------------------------------------- #
# the steps
# --------------------------------------------------------------------------- #

def prepare_subject(plan: Plan, cfg: ReleaseConfig) -> str:
    """What the work branch's preparation commit is called."""
    if plan.promote is not None and plan.bump:
        return f"{plan.version}: the version, and the notes"
    if plan.bump:
        return f"version: {plan.version}"
    return f"changelog: {plan.version}"


def prepare(ctx: Ctx, cfg: ReleaseConfig, plan: Plan) -> None:
    """Write the version into the work branch: the notes' heading, the version
    files, or both — as one commit, pushed.

    Committed rather than left in the working tree because the release commit
    is a copy of that branch's *tree*: an uncommitted edit would ship a
    changelog still reading "Unreleased" in the release describing it, and a
    version file still naming the version before this one. The build reads both
    from Forgejo, which is why this pushes.

    It is the one step in the cycle that writes to the work branch, so it runs
    after every refusal in the preflight and not before.
    """
    if plan.promote is None and not plan.bump:
        return
    paths = list(plan.bump)
    what = []
    if plan.promote is not None:
        what.append(f"'## [Unreleased]' -> '## [{plan.version}]' in {cfg.changelog}")
    if plan.bump:
        what.append(f"version {plan.version} in {', '.join(plan.bump)}")
    ui.plain(ui.bold("the work branch: " + "; ".join(what)))
    if ctx.would(f"write those, commit them on {cfg.source} and push"):
        return

    if plan.promote is not None and cfg.changelog is not None:
        path = ctx.root / cfg.changelog
        lines = path.read_text().splitlines(keepends=True)
        if not UNRELEASED.match(lines[plan.promote].rstrip("\n")):
            # The file changed under us between the preflight and here.
            raise ButlerError(f"{cfg.changelog} line {plan.promote + 1} is no longer "
                              f"the Unreleased heading")
        lines[plan.promote] = f"## [{plan.version}]\n"
        path.write_text("".join(lines))
        paths.append(cfg.changelog)

    for name in plan.bump:
        bump_version_file(ctx.root / name, plan.version)

    ctx.check(["git", "commit", "--quiet", "-m", prepare_subject(plan, cfg),
               "--", *paths], what="commit the version and the notes")
    ctx.check(["git", "push", "--quiet", "origin", cfg.source],
              what=f"push {cfg.source}")
    ui.ok(f"{cfg.source}", f"says {plan.version} now: {', '.join(paths)}")


def merge(ctx: Ctx, cfg: ReleaseConfig, plan: Plan) -> str:
    """Give the publication branch the work branch's tree, as one commit.

    Not `git merge --squash`, for two reasons. The publication branch is often
    *unrelated* to the work branch — butler's own `main` is an orphan, because
    the published history was started fresh rather than carrying the private
    one — and merge refuses that outright. And a merge is more than is wanted
    anyway: what a release puts on this branch is the source tree as released,
    whole. That is a tree copy, and `commit-tree` says so exactly.

    Working in plumbing has a second payoff: nothing is checked out, so the
    working tree is never touched, there is no branch to switch back from, and
    a run that dies here leaves the checkout exactly as it found it.

    Returns the commit the tag goes on.
    """
    ui.plain(ui.bold(f"{cfg.source} -> {cfg.into}, as one commit"))
    tree = ctx.capture(["git", "rev-parse", f"{cfg.source}^{{tree}}"]).out.strip()
    if not tree:
        raise ButlerError(f"cannot read the tree of '{cfg.source}'")

    # A resumed run finds its own commit already sitting there: same tree, same
    # subject, same parent. Committing a second one would put two commits on
    # the branch for one release, which is the one thing this must not do.
    head = _sha(ctx, cfg.into)
    if head and _describes(ctx, head, tree, plan):
        ui.ok(f"{cfg.into} already carries {plan.version}", f"({head[:12]})")
        return head

    if ctx.would(f"commit {cfg.source}'s tree on {cfg.into} as {plan.subject!r}"):
        return plan.base
    made = ctx.capture(["git", "commit-tree", tree, "-p", plan.base,
                        "-m", plan.subject])
    if not made.ok or not made.out.strip():
        raise ButlerError(f"could not build the release commit: {made.combined.strip()}")
    commit = made.out.strip()
    # With the old value given, so a branch that moved under us — another
    # release running elsewhere, a stray checkout — fails here rather than
    # having its commit overwritten.
    ref = ["git", "update-ref", "-m", f"release {plan.version}",
           f"refs/heads/{cfg.into}", commit]
    ctx.check([*ref, head] if head else ref, what=f"move {cfg.into}")
    ui.ok(f"{cfg.into}", f"one commit: {plan.subject} ({commit[:12]})")
    return commit


def _describes(ctx: Ctx, commit: str, tree: str, plan: Plan) -> bool:
    """Whether `commit` is already this release's commit, made by this run's
    plan — same content, same subject, same place in the history."""
    got = ctx.capture(["git", "show", "-s", "--format=%T%n%P%n%s", commit]).out.splitlines()
    if len(got) < 3:
        return False
    have_tree, parents, subject = got[0].strip(), got[1].split(), got[2].strip()
    return (have_tree == tree and subject == plan.subject
            and parents == [plan.base])


def tag(ctx: Ctx, cfg: ReleaseConfig, plan: Plan, target: str) -> None:
    """Annotate the release commit. The tag is what starts the build."""
    existing = _sha(ctx, plan.tag)
    if existing and existing == target:
        ui.ok(f"{plan.tag} is already on this commit")
        return
    cmd = ["git", "tag", "-a", plan.tag, "-m", plan.subject]
    if existing:
        if not plan.retag:
            raise ButlerError(f"{plan.tag} names {existing[:12]}, not this release")
        cmd.insert(2, "--force")
    ctx.check([*cmd, target], what=f"tag {plan.tag}")
    ui.ok(f"tagged {plan.tag}", f"-> {target[:12]}")


def push(ctx: Ctx, cfg: ReleaseConfig, plan: Plan) -> None:
    """Send the TAG to Forgejo. That is what starts the build.

    Only the tag. The publication branch stays where it is until the build has
    passed (`land`, below), and the order matters more than it looks:

      * a failed build leaves that branch untouched, so re-cutting the version
        rewinds nothing that was ever pushed — the fixed release commit lands as
        an ordinary fast-forward, and `--retag` never has to force a branch;
      * which means the branch can stay force-push protected on the forge, as a
        publication branch should be. butler 0.8.1 found this the hard way: the
        old order pushed the branch first, its build failed, and the re-cut
        bounced off "branch main is protected from force push" — correctly.

    A tag has nothing to protect: nothing is published under it until its build
    says so, and moving it is the whole point of a re-cut.
    """
    cmd = ["git", "push", "origin", f"refs/tags/{plan.tag}"]
    if plan.retag:
        # A tag has no remote-tracking ref for a lease to compare against, so
        # moving it is a plain force — safe here only because nothing has been
        # published under it, which was checked before anything moved.
        cmd.insert(2, "--force")
    ctx.check(cmd, what=f"push {plan.tag}")
    ui.ok(f"pushed {plan.tag}", "-> origin, which starts the build")


def land(ctx: Ctx, cfg: ReleaseConfig, plan: Plan) -> None:
    """Move the publication branch onto the release, once its build has passed.

    A fast-forward, always: the branch was left alone until now, so the release
    commit sits directly on top of what origin has. If this is ever rejected as
    non-fast-forward, something else moved the branch — which is a thing to look
    at, not to force past.
    """
    cmd = ["git", "push", "origin", f"refs/heads/{cfg.into}"]
    if plan.retag and (_sha(ctx, f"origin/{cfg.into}") or plan.base) != plan.base:
        # The branch already carries the release being re-cut — a run that was
        # told not to wait, or one from before the tag went first. Rewinding it
        # is what --retag is for, and with lease, so a commit someone else put
        # there aborts the push rather than disappearing.
        cmd.insert(2, "--force-with-lease")
        ui.warn("note:", f"{cfg.into} already carries the re-cut release; "
                         "rewinding it (this needs force-push to be allowed).")
    ctx.check(cmd, what=f"push {cfg.into}")
    ui.ok(f"pushed {cfg.into}", f"-> origin ({plan.tag} is built and released)")


def wait(ctx: Ctx, cfg: ReleaseConfig, plan: Plan, args) -> bool:
    """Wait for the build the tag started, and refuse to go on unless it passed.

    Returns whether the build is *known* to have passed. Everything after this
    point publishes, and publishing a release nobody has seen build is the one
    thing this component exists to prevent — so "not waited for" and "passed"
    must not look the same to the caller.
    """
    if not cfg.wait or args.no_wait:
        ui.warn("note:", "not waiting for the build.")
        return False
    pub = ctx.cfg.publish
    if pub is None:
        # Nothing to wait for and nothing to export: the release is whatever
        # this repository's own forge makes of the tag.
        ui.warn("note:", "no [publish] section, so there is no Forgejo host to "
                         "watch the build on.")
        return True
    sha = _sha(ctx, plan.tag) or ""
    forgejo.wait_for_build(ctx, host=pub.host, repo=pub.forgejo, tag=plan.tag,
                           sha=sha, timeout=args.timeout or cfg.timeout,
                           poll=cfg.poll, workflow=cfg.workflow)
    return True


def export(ctx: Ctx, cfg: ReleaseConfig, plan: Plan, args) -> int:
    """Hand over to `publish`: the export, the public tag, the release copy."""
    if ctx.cfg.publish is None:
        ui.warn("note:", "this project has no [publish] section, so nothing is "
                         "exported. The release is out on Forgejo.")
        return 0
    return publish_component.publish(
        ctx, Namespace(tag=plan.tag, init=False, rehearse=False)) or 0


def publish_hint(plan: Plan) -> str:
    return f"butler.py publish --tag {plan.tag}"


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #

def describe(ctx: Ctx, cfg: ReleaseConfig, plan: Plan, args) -> None:
    pub = ctx.cfg.publish
    ui.plain(ui.bold(f"release {plan.version}"))
    if plan.promote is not None or plan.bump:
        written = []
        if plan.promote is not None:
            written.append(f"'## [Unreleased]' -> '## [{plan.version}]'")
        if plan.bump:
            written.append(f"version {plan.version} in {', '.join(plan.bump)}")
        ui.plain(f"  prepare  {'; '.join(written)} — committed on {cfg.source}")
    ui.plain(f"  squash   {cfg.source} -> {cfg.into}"
             + (f"  (rewinding to {plan.base[:12]})" if plan.retag else ""))
    ui.plain(f"  commit   {plan.subject}")
    ui.plain(f"  tag      {plan.tag}" + ("  (moved)" if plan.retag else ""))
    ui.plain(f"  push     origin {plan.tag}" + ("  (forced)" if plan.retag else "")
             + "  — the build runs on it")
    if cfg.wait and not args.no_wait and pub is not None:
        ui.plain(f"  wait     the build on {pub.host}/{pub.forgejo}")
    ui.plain(f"  land     origin {cfg.into} -> the release, once the build passed")
    if pub is not None and not args.no_export:
        what = "export, public tag" + (", release copy" if pub.release else "")
        ui.plain(f"  publish  {what} -> github.com/{pub.github}")
    if plan.notes:
        first = next((line for line in plan.notes.splitlines() if line.strip()), "")
        ui.plain(ui.dim(f"  notes    {len(plan.notes.splitlines())} lines, "
                        f"starting {first.strip()[:48]!r}"))


def cut(ctx: Ctx, args) -> int:
    cfg = _cfg(ctx)
    version, tag_name = _split_version(cfg, args.version)
    plan = preflight(ctx, cfg, args, version, tag_name)

    describe(ctx, cfg, plan, args)
    ui.plain()
    if args.check:
        ui.note("Nothing was changed. Drop --check to cut it.")
        return 0
    if plan.retag and not ctx.confirm(
            f"Re-cut {plan.tag}? This rewrites {cfg.into} on origin"):
        return 1
    if not plan.retag and not ctx.confirm(f"Cut {plan.tag}?"):
        return 1

    steps = STEPS[STEPS.index(args.from_step):] if args.from_step else STEPS

    # None of these check anything out: the branch is written with plumbing,
    # so the working tree this was run from is the working tree it ends in.
    if "prepare" in steps:
        prepare(ctx, cfg, plan)
    target = merge(ctx, cfg, plan) if "merge" in steps else _sha(ctx, cfg.into)
    if not target:
        raise ButlerError(f"there is no local '{cfg.into}' to tag",
                          hint="Run without --from, so the release commit is made.")
    if "tag" in steps:
        tag(ctx, cfg, plan, target)
    if "push" in steps:
        push(ctx, cfg, plan)

    green = wait(ctx, cfg, plan, args) if "wait" in steps else True
    if not green:
        # The tag is out and the build is running; the branch and the mirror
        # wait for it. Stopping here is what keeps a re-cut cheap: nothing that
        # would have to be rewound has been pushed.
        ui.plain()
        ui.ok(f"{plan.tag} is pushed and building.",
              f"Once it is green:  butler.py release {plan.version} --from land")
        return 0
    # Only now: the export reads the branch from Forgejo, so it has to be there
    # before `publish` runs.
    if "land" in steps:
        land(ctx, cfg, plan)
    exported = False
    if "export" in steps and not args.no_export:
        rc = export(ctx, cfg, plan, args)
        if rc != 0:
            return rc
        exported = ctx.cfg.publish is not None
    ui.plain()
    # The last line says what is true, not what was asked for: a run stopped
    # early by --no-export has put a tag on Forgejo and nothing on the mirror,
    # and reading "is out" over that is how the mirror silently falls behind.
    if exported:
        ui.ok(f"{plan.tag} is out.")
    else:
        ui.ok(f"{plan.tag} is on Forgejo.",
              f"The mirror does not have it yet: {publish_hint(plan)}")
    return 0


# --------------------------------------------------------------------------- #

def node(cfg: ReleaseConfig, publish_cfg) -> Node:
    where = f" and export it to github.com/{publish_cfg.github}" if publish_cfg else ""
    return Node(
        "release",
        f"squash {cfg.source} into {cfg.into}, tag it, wait for the build{where}",
        func=cut,
        args=[
            arg("version", help="the version to cut, with or without "
                                f"the '{cfg.tag_prefix}' prefix"),
            arg("--check", action="store_true",
                help="print what would happen and stop; changes nothing"),
            arg("--retag", action="store_true",
                help="re-cut a version whose build failed: rebuild the squash "
                     "commit, move the tag, force-push"),
            arg("--from", dest="from_step", metavar="STEP", choices=STEPS,
                help=f"resume from a step ({', '.join(STEPS)})"),
            arg("--no-wait", action="store_true",
                help="do not wait for the Forgejo build"),
            arg("--no-export", action="store_true",
                help="stop after the build; do not touch the mirror"),
            arg("--timeout", type=int, metavar="SECONDS",
                help="how long to wait for the build (default from butler.toml)"),
        ],
        epilog="A failed build has published nothing: fix it, commit on "
               f"{cfg.source}, and re-cut the same version with --retag.",
    )
