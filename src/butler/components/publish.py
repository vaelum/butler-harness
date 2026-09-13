"""The `publish` component: export a private Forgejo branch to a public mirror.

Copybara rebuilds the destination's history from the source's, so the mirror is
*generated* — never committed to directly. What a project declares is which two
repositories are involved and which extra paths stay private; everything about
how Copybara is driven lives here.

That consolidation is the point. Before this component every exporting project
carried its own ~200-line `push-public.sh` and hand-written `copy.bara.sky`,
which between them stated the exclude list **twice** — once as a glob for
Copybara, once as a regex for the tag-equivalence check — under a comment
reading "keep the two in sync". That is the most likely way a private file
eventually reaches a public repository. Here the globs are the single source and
the regex is derived from them, so the two cannot drift.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import proc, ui
from ..command import Node, arg
from ..config import PublishConfig
from ..context import Ctx
from ..errors import ButlerError

# Pinned deliberately: an export is reproducible only if the tool is. Bump both
# together — the jar is refused unless it hashes to exactly this.
COPYBARA_VERSION = "v20260907"
COPYBARA_SHA256 = "4499e9cd0c07cfc9b82c3af644b7b95c1787eea980f99c1515515774c1307d64"
COPYBARA_URL = ("https://github.com/google/copybara/releases/download/"
                f"{COPYBARA_VERSION}/copybara_deploy.jar")

# Copybara's exit code for "nothing new to export". Not a failure.
NO_OP = 4

# Never exported, from any project. A project's own `exclude` adds to this; it
# cannot remove from it, which is why the dangerous entries live here.
#
#   *-PLAN.md        infrastructure plans: hosts, addresses, credential paths
#   .forgejo/**      CI: self-hosted runner labels, secret names, internal URLs
#   copy.bara.sky    the export machinery, where a project still has one
#   push-public.sh   likewise
#
# `butler/butler.toml` is deliberately NOT here: how a project is built is part
# of what a published project tells you, and chords has shipped it from the
# start.
BASELINE_EXCLUDE = (
    "*-PLAN.md",
    "**/*-PLAN.md",
    ".forgejo/**",
    "copy.bara.sky",
    "push-public.sh",
)

# The private pin a generated shim carries, and what it becomes publicly. A
# clone of the mirror cannot reach Forgejo over SSH, so a shim left pointing
# there makes the published repository unbuildable by anyone but its author.
# Deliberately free of quote characters: this goes inside a Starlark string in
# the generated config, and a " or ' here would terminate it early.
HARNESS_PRIVATE_RE = r"(git\+ssh://\S+/butler\.git)"
# Both files that can carry the pin: a consuming project's shim, and — in the
# harness itself — the module that writes that shim. Excluding either would be
# worse than publishing it unrewritten: a mirror missing `new.py` is a harness
# whose `butler new` fails on import.
HARNESS_PIN_PATHS = ("butler.py", "src/butler/new.py", "*.md", "**/*.md")

# Both spellings, always. Copybara's "**/*.md" does not match a root-level
# README.md — which is why every hand-written config in this fleet listed
# "*-PLAN.md" and "**/*-PLAN.md" side by side. Getting this wrong is silent:
# the rewrite simply does not happen, and the link ships.
MD_PATHS = ("*.md", "**/*.md")


def _cfg(ctx: Ctx) -> PublishConfig:
    if ctx.cfg.publish is None:
        raise ButlerError(
            "this project has no [publish] configuration",
            hint="Add a [publish] section to butler/butler.toml naming the\n"
                 "Forgejo repo, the GitHub mirror and the author identity.")
    return ctx.cfg.publish


# --------------------------------------------------------------------------- #
# the exclude list, stated once
# --------------------------------------------------------------------------- #

def excludes(cfg: PublishConfig) -> list[str]:
    """The full exclude list: the baseline, then the project's own additions."""
    seen, out = set(), []
    for pattern in (*BASELINE_EXCLUDE, *cfg.exclude):
        if pattern not in seen:
            seen.add(pattern)
            out.append(pattern)
    return out


def glob_to_regex(pattern: str) -> str:
    """One Copybara glob as an anchored regex over repository-relative paths.

    This exists so the exclude list can be written once. The tag-equivalence
    check in `publish_tag` needs to ask "did anything *not* excluded change
    between these two commits", which is a question about paths, not about
    Copybara — and answering it from a second, hand-maintained regex is how the
    two drift apart.

    Only the subset of glob syntax the exclude lists actually use is handled,
    and anything outside it raises rather than silently matching too little.
    """
    if "{" in pattern or "}" in pattern:
        raise ButlerError(f"[publish] exclude pattern {pattern!r} uses brace expansion",
                          hint="Write the alternatives as separate patterns.")

    # "dir/**" — the directory and everything under it.
    if pattern.endswith("/**"):
        return "^" + re.escape(pattern[:-3]) + "/"

    # "**/name" — at any depth, including the root.
    anchor = "^"
    if pattern.startswith("**/"):
        pattern, anchor = pattern[3:], "(^|/)"

    if "**" in pattern:
        raise ButlerError(f"[publish] exclude pattern {pattern!r} is not supported",
                          hint="Use 'dir/**', '**/name' or a plain path.")

    # A single * stays inside one path segment, as it does for Copybara.
    body = "".join("[^/]*" if part == "*" else re.escape(part)
                   for part in re.split(r"(\*)", pattern))
    return anchor + body + "$"


def excluded_re(cfg: PublishConfig) -> re.Pattern[str]:
    return re.compile("|".join(glob_to_regex(p) for p in excludes(cfg)))


# --------------------------------------------------------------------------- #
# the generated Copybara config
# --------------------------------------------------------------------------- #

def workflow(cfg: PublishConfig, *, destination: str) -> str:
    """The copy.bara.sky this export runs with.

    Generated into a scratch directory rather than committed, so there is no
    file in the repository for the exclude list to have to exclude, and no
    chance of the checked-in config and the one actually used disagreeing.
    """
    exclude = ",\n        ".join(f'"{p}"' for p in excludes(cfg))
    transforms = [
        # A commit message may carry private notes after a line starting with
        # "PRIVATE:"; that line and everything after it is dropped publicly.
        '    metadata.scrubber("(?s)\\nPRIVATE:.*", replacement = ""),',
    ]
    if cfg.rewrite_harness:
        public = f"git+https://github.com/{cfg.harness_github}.git"
        pin_paths = ", ".join(f'"{p}"' for p in HARNESS_PIN_PATHS)
        md_paths = ", ".join(f'"{p}"' for p in MD_PATHS)
        # The harness is the one repository whose mirror is renamed, so a link
        # to it cannot be mapped path-for-path by the generic rule below. This
        # rule runs first, and only when a project says where the harness lives.
        harness_link = ""
        if cfg.harness_forgejo:
            harness_link = f"""        core.replace(
            before = "https://{cfg.host}/{cfg.harness_forgejo}",
            after = "https://github.com/{cfg.harness_github}",
            paths = glob([{md_paths}]),
        ),
"""
        transforms.append(f'''    # The shim's pin points at Forgejo, which nobody outside can reach; the
    # published copy must point at the published harness or the mirror is not
    # buildable by anyone but its author.
    core.transform([
        core.replace(
            before = "${{url}}",
            after = "{public}",
            regex_groups = {{"url": r"{HARNESS_PRIVATE_RE}"}},
            paths = glob([{pin_paths}]),
        ),
{harness_link}        # Any other Forgejo web link becomes its GitHub equivalent. The harness
        # rule above runs first because that one repo's mirror is renamed; the
        # rest map path-for-path. A link a reader cannot open is worse than no
        # link, and it advertises the private layout besides.
        core.replace(
            before = "https://{cfg.host}/${{repo}}",
            after = "https://github.com/${{repo}}",
            regex_groups = {{"repo": r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"}},
            paths = glob([{md_paths}]),
        ),
    ],
        # A regex_groups replace has no automatic inverse, and Copybara refuses
        # to load a transform it cannot reverse unless told the reversal is
        # deliberately empty. Nothing here is ever run backwards: the export is
        # one-way, private -> public.
        reversal = [],
        noop_behavior = "IGNORE_NOOP"),''')

    return f'''# Generated by `butler publish` — do not edit, and do not commit.
# Source of truth: the [publish] section of butler/butler.toml.

core.workflow(
    name = "export",
    origin = git.origin(url = "{cfg.origin_url}", ref = "{cfg.branch}"),
    destination = git.destination(
        url = "{destination}",
        fetch = "{cfg.branch}",
        push = "{cfg.branch}",
    ),
    origin_files = glob(["**"], exclude = [
        {exclude},
    ]),
    # overwrite, not pass_thru: every public commit carries this one identity
    # whatever the private commit said, so the exported history cannot acquire
    # a second author from a stray git config.
    authoring = authoring.overwrite("{cfg.author}"),
    mode = "ITERATIVE",
    transformations = [
{chr(10).join(transforms)}
    ],
)
'''


# --------------------------------------------------------------------------- #
# the tool
# --------------------------------------------------------------------------- #

def _java() -> str:
    """The JVM to run Copybara with. Copybara needs a recent one; the pinned
    path is tried first so a machine with several JDKs behaves predictably."""
    pinned = os.environ.get("COPYBARA_JAVA", "/usr/lib/jvm/java-25-openjdk/bin/java")
    if Path(pinned).is_file():
        return pinned
    found = shutil.which("java")
    if found is None:
        raise ButlerError("no java found, and Copybara needs one (25+)",
                          hint="Install a JDK, or set COPYBARA_JAVA to its java binary.")
    return found


def jar_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "copybara" / f"copybara-{COPYBARA_VERSION}.jar"


def ensure_jar(ctx: Ctx) -> Path:
    """Fetch the pinned jar once, refusing it unless it hashes as expected.

    Downloaded to a .part file and moved into place only after the check, so an
    interrupted download cannot be mistaken for a good jar on the next run.
    """
    jar = jar_path()
    if jar.is_file():
        return jar
    if ctx.would(f"download Copybara {COPYBARA_VERSION} to {jar}"):
        return jar
    jar.parent.mkdir(parents=True, exist_ok=True)
    part = jar.with_suffix(".part")
    ui.plain(f"Fetching Copybara {COPYBARA_VERSION} …")
    try:
        with urllib.request.urlopen(COPYBARA_URL) as r, part.open("wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise ButlerError(f"could not download Copybara: {e}") from e
    digest = hashlib.sha256(part.read_bytes()).hexdigest()
    if digest != COPYBARA_SHA256:
        part.unlink(missing_ok=True)
        raise ButlerError(
            f"copybara_deploy.jar {COPYBARA_VERSION} failed its sha256 check",
            hint=f"expected {COPYBARA_SHA256}\ngot      {digest}")
    part.rename(jar)
    return jar


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #

def require_pushed(ctx: Ctx, cfg: PublishConfig) -> None:
    """Copybara reads the branch from Forgejo, not from this working copy, so
    unpushed local work would be silently left out of the export rather than
    failing. Refuse unless the two agree."""
    if ctx.dry_run:
        return
    proc.run(["git", "fetch", "-q", "origin", cfg.branch], cwd=ctx.root, echo=False)
    local = proc.capture(["git", "rev-parse", cfg.branch], cwd=ctx.root)
    remote = proc.capture(["git", "rev-parse", f"origin/{cfg.branch}"], cwd=ctx.root)
    if not local.ok or not remote.ok:
        raise ButlerError(f"cannot resolve '{cfg.branch}' locally and on origin",
                          hint=f"Does this project have a '{cfg.branch}' branch?")
    if local.out.strip() != remote.out.strip():
        raise ButlerError(
            f"local '{cfg.branch}' differs from origin/{cfg.branch}",
            hint=f"Copybara exports what is on Forgejo. Push first:\n"
                 f"  git push origin {cfg.branch}")


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #

@dataclass
class _Run:
    cfg: PublishConfig
    destination: str
    rehearsing: bool


def _destination(ctx: Ctx, cfg: PublishConfig, args) -> _Run:
    """Where this run writes. A rehearsal targets a scratch bare repo, so the
    whole export can be inspected before anything reaches GitHub."""
    override = os.environ.get("PUBLIC_URL")
    if getattr(args, "rehearse", False):
        scratch = Path(tempfile.mkdtemp(prefix=f"butler-publish-{ctx.name}-"))
        bare = scratch / "mirror.git"
        proc.check(["git", "init", "--quiet", "--bare", "-b", cfg.branch, str(bare)],
                   cwd=ctx.root, echo=False, what="create the rehearsal repo")
        return _Run(cfg, str(bare), True)
    if override:
        return _Run(cfg, override, True)
    return _Run(cfg, cfg.destination_url, False)


def check(ctx: Ctx, args) -> int:
    """Say what would ship and what would not, without touching the network."""
    cfg = _cfg(ctx)
    rx = excluded_re(cfg)
    listing = proc.capture(["git", "ls-tree", "-r", "--name-only", cfg.branch], cwd=ctx.root)
    if not listing.ok:
        raise ButlerError(f"cannot list '{cfg.branch}'",
                          hint=f"Does this project have a '{cfg.branch}' branch?")
    files = [f for f in listing.out.splitlines() if f]
    held = [f for f in files if rx.search(f)]
    shipped = [f for f in files if not rx.search(f)]

    ui.plain(ui.bold(f"{cfg.forgejo} ({cfg.branch}) -> github.com/{cfg.github} "
                     f"[{cfg.visibility}]"))
    ui.plain()
    ui.plain(ui.bold(f"  would publish ({len(shipped)} files)"))
    for f in shipped:
        ui.plain(f"    {f}")
    ui.plain()
    ui.plain(ui.bold(f"  held back ({len(held)} files)"))
    for f in held:
        ui.plain(ui.dim(f"    {f}"))
    if not held:
        ui.plain(ui.dim("    (nothing)"))
    ui.plain()
    ui.plain(ui.bold("  exclude patterns"))
    for pattern in excludes(cfg):
        ui.plain(ui.dim(f"    {pattern}"))
    # A public export that ships no exclusions at all is usually a project that
    # has not thought about it yet, rather than one with nothing to hide.
    if cfg.is_public and not held:
        ui.warn("note:", "this is a public export and nothing is being held back.")
    return 0


def publish(ctx: Ctx, args) -> int:
    cfg = _cfg(ctx)
    run = _destination(ctx, cfg, args)
    require_pushed(ctx, cfg)
    jar, java = ensure_jar(ctx), _java()

    scratch = Path(tempfile.mkdtemp(prefix=f"butler-publish-cfg-{ctx.name}-"))
    sky = scratch / "copy.bara.sky"
    sky.write_text(workflow(cfg, destination=run.destination))
    if ctx.verbose:
        ui.plain(ui.dim(sky.read_text()))

    cmd = [java, "-jar", str(jar), "migrate", str(sky), "export",
           "--git-destination-url", run.destination,
           "--git-committer-name", cfg.committer.split("<")[0].strip(),
           "--git-committer-email", cfg.committer.split("<")[1].rstrip(">").strip()]
    # Copybara has no prior state in an empty destination, so it needs to be
    # told where to start rather than inferring it from the last export. A
    # rehearsal always targets a repo created empty moments ago, so it always
    # needs this — asking for --rehearse --init would be a papercut.
    if getattr(args, "init", False) or run.rehearsing:
        cmd += ["--init-history", "--force"]

    rc = ctx.run(cmd, cwd=ctx.root)
    if rc == NO_OP:
        ui.ok(f"public {cfg.branch} is already up to date")
    elif rc != 0:
        return rc

    if run.rehearsing:
        ui.plain()
        ui.ok("rehearsal:", f"exported to {run.destination}")
        listing = proc.capture(
            ["git", "ls-tree", "-r", "--name-only", cfg.branch, ], cwd=run.destination)
        if listing.ok:
            ui.plain(ui.bold("  the exported tree"))
            for f in listing.out.splitlines():
                ui.plain(f"    {f}")
        ui.note("Nothing was pushed to GitHub. Read the tree above before a real run.")
        return 0

    tag = getattr(args, "tag", None)
    if tag:
        publish_tag(ctx, cfg, tag)
    return 0


def publish_tag(ctx: Ctx, cfg: PublishConfig, tag: str) -> None:
    """Publish one private tag on the public commit that corresponds to it.

    Copybara gives every exported commit a `GitOrigin-RevId` trailer naming the
    private commit it came from, which is the only way back: public SHAs are
    rebuilt and share nothing with the private ones.
    """
    root, url = ctx.root, cfg.destination_url

    if not proc.capture(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                        cwd=root).ok:
        raise ButlerError(f"tag '{tag}' does not exist")
    reachable = proc.run(["git", "merge-base", "--is-ancestor",
                          f"{tag}^{{commit}}", f"refs/heads/{cfg.branch}"],
                         cwd=root, echo=False) == 0
    if not reachable:
        raise ButlerError(f"tag '{tag}' is not reachable from '{cfg.branch}'",
                          hint="Only what is on the publication branch can be published.")

    proc.check(["git", "fetch", "-q", url, cfg.branch], cwd=root, echo=False,
               what="fetch the public branch")
    head = proc.capture(["git", "rev-parse", "FETCH_HEAD"], cwd=root).out.strip()

    # Read the trailer out of the raw message: Copybara appends it to the last
    # paragraph, and when that paragraph already has a "Word: ..." line git no
    # longer treats it as a trailer at all.
    public_for: dict[str, str] = {}
    log = proc.capture(["git", "log", "-z", "--format=%H%n%B", head], cwd=root)
    for entry in log.out.split("\0"):
        lines = entry.strip("\n").split("\n")
        if not lines or not lines[0]:
            continue
        for line in lines[1:]:
            if line.startswith("GitOrigin-RevId: "):
                public_for[line.removeprefix("GitOrigin-RevId: ").strip()] = lines[0]

    # The tagged commit itself may never have been exported — it might only have
    # touched excluded files — so walk back to the newest ancestor that was.
    walk = proc.capture(["git", "rev-list", "--first-parent", f"{tag}^{{commit}}"], cwd=root)
    target = source = None
    for commit in walk.out.split():
        if commit in public_for:
            target, source = public_for[commit], commit
            break
    if target is None:
        raise ButlerError(f"no exported commit found for tag '{tag}'",
                          hint="Has this branch been exported yet?")

    # That is only the *same release* if nothing but excluded files changed in
    # between. A tag on a merged-in side branch fails this: Copybara follows
    # first parents, so the tag's exact tree never exists publicly.
    changed = proc.capture(["git", "diff", "--name-only", source, f"{tag}^{{commit}}"],
                           cwd=root).out.split()
    rx = excluded_re(cfg)
    if any(not rx.search(f) for f in changed):
        short = proc.capture(["git", "rev-parse", "--short", source], cwd=root).out.strip()
        ui.warn("warning:", f"'{tag}' has no exact public equivalent "
                            f"(nearest export is {short}); not publishing it")
        return

    # A published tag is never moved by accident. One already on the right
    # commit is left alone, so a run that failed after pushing it can simply be
    # repeated.
    listing = proc.capture(["git", "ls-remote", url, f"refs/tags/{tag}",
                            f"refs/tags/{tag}^{{}}"], cwd=root)
    published = ""
    for line in listing.out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/tags/{tag}^{{}}":
            published = sha.strip()
        elif ref.strip() == f"refs/tags/{tag}" and not published:
            published = sha.strip()
    if published:
        if published != target:
            raise ButlerError(
                f"tag '{tag}' already exists publicly, on another commit ({published[:12]})")
        ui.ok(f"{tag} is already published")
        return

    ctx.check(["git", "push", url, f"{target}:refs/tags/{tag}"])
    ui.ok(f"published {tag}", f"-> {target[:12]}")


# --------------------------------------------------------------------------- #

def node(cfg: PublishConfig) -> Node:
    publish_args = [
        arg("tag", nargs="?", help="also publish this tag (must be on the branch)"),
        arg("--init", action="store_true",
            help="first export into an EMPTY mirror; run once"),
        arg("--rehearse", action="store_true",
            help="export into a scratch repo and print the tree; pushes nothing"),
    ]
    return Node(
        "publish", f"export {cfg.branch} to github.com/{cfg.github} ({cfg.visibility})",
        func=publish, args=publish_args,
        children=[
            Node("check", "list what would and would not be published", func=check),
        ],
        epilog="The mirror is generated: never commit to it directly.",
    )
