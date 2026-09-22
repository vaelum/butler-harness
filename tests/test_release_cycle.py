"""Cutting a release: the squash, the tag, the build wait and the re-cut.

The git tests run against real repositories in a tmp directory — a bare one
standing in for Forgejo and a checkout standing in for the machine cutting the
release. That is deliberate: what this component is *for* is the shape of the
history it leaves behind (one commit per release on the publication branch, a
tag on it), and nothing but git will tell you whether that came out right.

The network steps are not exercised here: `--no-wait --no-export` stops the run
at the push, and the build wait is tested on its own against a faked API.
"""

import json
import tomllib
from argparse import Namespace
from pathlib import Path

import pytest

from butler import config, proc, release
from butler.components import release_cycle
from butler.context import Ctx
from butler.errors import ButlerError, ConfigError

TOML = '''
[project]
name = "demo"

[publish]
forgejo = "acme/demo"
host = "git.example.org"
author = "dev <dev@example.org>"
release = true

[release]
%(extra)s
'''


def parse(extra: str = "", root: Path = Path("/proj")):
    return config.parse(tomllib.loads(TOML % {"extra": extra}), root)


# ---- config --------------------------------------------------------------- #

def test_defaults_describe_the_usual_setup():
    cfg = parse().release
    assert cfg is not None
    assert (cfg.source, cfg.into, cfg.tag_prefix) == ("dev", "main", "v")
    assert cfg.changelog == "CHANGELOG.md" and cfg.wait is True
    assert cfg.tag_for("1.2.3") == "v1.2.3"
    assert cfg.subject("demo", "1.2.3") == "demo 1.2.3"


def test_the_merge_target_follows_the_exported_branch():
    # Stating it twice is how the two come to disagree, and a release merged
    # into a branch nobody exports never reaches the mirror.
    cfg = config.parse(tomllib.loads('''
[project]
name = "demo"
[publish]
forgejo = "acme/demo"
host = "h"
author = "a <a@b.c>"
branch = "stable"
[release]
'''), Path("/proj"))
    assert cfg.release.into == "stable"


def test_merging_into_a_branch_that_is_never_exported_is_refused():
    with pytest.raises(ConfigError, match="but .publish. exports"):
        parse('into = "trunk"')


def test_source_and_target_cannot_be_the_same_branch():
    with pytest.raises(ConfigError, match="are both .main."):
        parse('source = "main"')


def test_an_unknown_placeholder_in_the_message_fails_at_config_time():
    # Rather than at the commit, when the branch has already been rewound.
    with pytest.raises(ConfigError, match="unknown placeholder"):
        parse('message = "{proejct} {version}"')


def test_a_custom_message_carries_the_project_and_version():
    cfg = parse('message = "{project} {version} — a shared harness"').release
    assert cfg.subject("butler", "0.8.0") == "butler 0.8.0 — a shared harness"


@pytest.mark.parametrize("extra", ['timeout = 0', 'poll = -1'])
def test_a_nonsense_wait_is_refused(extra):
    with pytest.raises(ConfigError, match="positive number of seconds"):
        parse(extra)


def test_no_release_section_means_no_command():
    cfg = config.parse(tomllib.loads('[project]\nname = "demo"\n'), Path("/proj"))
    assert cfg.release is None


# ---- the version, the notes and the version files -------------------------- #

def test_a_version_is_taken_with_or_without_its_prefix():
    cfg = parse().release
    assert release_cycle._split_version(cfg, "1.2.3") == ("1.2.3", "v1.2.3")
    assert release_cycle._split_version(cfg, "v1.2.3") == ("1.2.3", "v1.2.3")


CHANGELOG = """# Changelog

## [1.2.3]

### Fixed

- the thing

## [1.2.2]

- the earlier thing
"""


def test_the_notes_are_the_changelog_section_for_that_version(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    notes = release_cycle.changelog_notes(tmp_path, parse().release, "1.2.3")
    assert "the thing" in notes
    # Strictly this version's section: the next heading ends it.
    assert "earlier thing" not in notes


def test_a_heading_without_brackets_is_read_too(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("## 2026.9.3 — the one with the fix\n\n- it\n")
    assert "- it" in release_cycle.changelog_notes(tmp_path, parse().release, "2026.9.3")


def test_a_version_with_no_entry_is_refused(tmp_path):
    # The cheapest possible moment to catch it: after the tag it costs a re-cut,
    # and CI reads the same file to write the release notes.
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    with pytest.raises(ButlerError, match="no entry for 9.9.9"):
        release_cycle.changelog_notes(tmp_path, parse().release, "9.9.9")


def test_an_empty_section_counts_as_missing(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("## [1.2.3]\n\n## [1.2.2]\n\n- old\n")
    with pytest.raises(ButlerError, match="no entry"):
        release_cycle.changelog_notes(tmp_path, parse().release, "1.2.3")


def test_notes_can_be_turned_off(tmp_path):
    cfg = parse('changelog = ""').release
    assert release_cycle.changelog_notes(tmp_path, cfg, "1") == ""


@pytest.mark.parametrize("text", [
    'version = "1.2.3"',
    '__version__ = "1.2.3"',
    '  "version": "1.2.3",',
    "version = '1.2.3'",
])
def test_every_usual_spelling_of_a_version_declaration_counts(tmp_path, text):
    (tmp_path / "pyproject.toml").write_text(f"[project]\n{text}\n")
    release_cycle.check_version_files(
        tmp_path, parse('version_files = ["pyproject.toml"]').release, "1.2.3")


def test_a_file_left_at_the_old_version_stops_the_release(tmp_path):
    # The tag is the only thing that says which version this is, and it is the
    # one thing no build reads — so a stale file ships silently.
    (tmp_path / "pyproject.toml").write_text('version = "1.2.2"\n')
    with pytest.raises(ButlerError, match="does not declare version 1.2.3|do\\(es\\) not"):
        release_cycle.check_version_files(
            tmp_path, parse('version_files = ["pyproject.toml"]').release, "1.2.3")


def test_a_version_mentioned_in_prose_is_not_a_declaration(tmp_path):
    (tmp_path / "pyproject.toml").write_text("# upgrade from 1.2.3 before you start\n")
    with pytest.raises(ButlerError):
        release_cycle.check_version_files(
            tmp_path, parse('version_files = ["pyproject.toml"]').release, "1.2.3")


# ---- the cycle, against real repositories ---------------------------------- #

def git(*args, cwd):
    r = proc.capture(["git", *args], cwd=cwd)
    assert r.ok, f"git {' '.join(args)}: {r.combined}"
    return r.out.strip()


@pytest.fixture
def repo(tmp_path):
    """A checkout with `dev` and `main`, pushed to a bare origin."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    proc.capture(["git", "init", "--quiet", "--bare", "-b", "dev", str(origin)],
                 cwd=tmp_path)
    proc.capture(["git", "init", "--quiet", "-b", "dev", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "dev@example.org"), ("user.name", "dev"),
                 ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
        git("config", k, v, cwd=work)
    (work / "CHANGELOG.md").write_text(CHANGELOG)
    (work / "pyproject.toml").write_text('version = "1.2.3"\n')
    git("add", "-A", cwd=work)
    git("commit", "--quiet", "-m", "first", cwd=work)
    git("branch", "main", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "--quiet", "-u", "origin", "dev", "main", cwd=work)
    git("checkout", "--quiet", "dev", cwd=work)
    return work


@pytest.fixture(autouse=True)
def releases_on(monkeypatch):
    """The forge's Releases unit is on, without asking the forge."""
    monkeypatch.setattr(release, "releases_enabled", lambda host, repo: True)


def ctx_for(repo, extra="", **kw):
    cfg = config.parse(tomllib.loads(TOML % {"extra": extra}), repo)
    return Ctx(cfg=cfg, assume_yes=True, **kw)


def args_for(version, **over):
    base = dict(version=version, check=False, retag=False, from_step=None,
                no_wait=True, no_export=True, timeout=None)
    base.update(over)
    return Namespace(**base)


def work(repo, message="more work"):
    """A commit on the work branch, as a working day leaves behind."""
    (repo / "file.txt").write_text(message)
    git("add", "-A", cwd=repo)
    git("commit", "--quiet", "-m", message, cwd=repo)
    git("push", "--quiet", "origin", "dev", cwd=repo)


def test_a_release_adds_exactly_one_commit_to_the_publication_branch(repo):
    work(repo, "one")
    work(repo, "two")
    assert release_cycle.cut(ctx_for(repo), args_for("1.2.3")) == 0

    log = git("log", "--format=%s", "main", cwd=repo).splitlines()
    # Two days of work, one public commit — which is the whole point of
    # squashing: the mirror's history is releases, not keystrokes.
    assert log == ["demo 1.2.3", "first"]
    assert git("rev-parse", "v1.2.3^{commit}", cwd=repo) == \
        git("rev-parse", "main", cwd=repo)


def test_the_tag_goes_first_and_the_branch_waits_for_the_build(repo):
    # The tag is what starts the build; the publication branch does not move
    # until that build has passed. A failed build then leaves nothing pushed
    # that a re-cut would have to rewind — which is also what lets the branch
    # stay force-push protected on the forge.
    work(repo)
    origin = repo.parent / "origin.git"
    before = git("rev-parse", "main", cwd=origin)

    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))   # --no-wait: not green
    assert git("rev-parse", "v1.2.3^{commit}", cwd=origin) == \
        git("rev-parse", "main", cwd=repo)
    assert git("rev-parse", "main", cwd=origin) == before

    # `--from land` is the recovery: the build went green, finish the release.
    release_cycle.cut(ctx_for(repo), args_for("1.2.3", from_step="land"))
    assert git("rev-parse", "main", cwd=origin) == git("rev-parse", "main", cwd=repo)


def test_the_checkout_is_left_on_the_branch_it_started_on(repo):
    # `main` is somewhere this command visits, not somewhere to leave someone
    # standing — the next thing they type is meant for `dev`.
    work(repo)
    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dev"


def test_check_changes_nothing(repo):
    work(repo)
    before = git("rev-parse", "main", cwd=repo)
    assert release_cycle.cut(ctx_for(repo), args_for("1.2.3", check=True)) == 0
    assert git("rev-parse", "main", cwd=repo) == before
    assert not proc.capture(["git", "rev-parse", "-q", "--verify", "v1.2.3"],
                            cwd=repo).ok


def test_unpushed_work_is_refused(repo):
    # The build and the export both read Forgejo, so work left here would be
    # missing from the release without anything failing.
    (repo / "file.txt").write_text("local only")
    git("add", "-A", cwd=repo)
    git("commit", "--quiet", "-m", "local only", cwd=repo)
    with pytest.raises(ButlerError, match="differs from origin/dev"):
        release_cycle.cut(ctx_for(repo), args_for("1.2.3"))


def test_a_dirty_tree_is_refused(repo):
    (repo / "file.txt").write_text("uncommitted")
    with pytest.raises(ButlerError, match="uncommitted changes"):
        release_cycle.cut(ctx_for(repo), args_for("1.2.3"))


def test_a_publication_branch_carrying_local_commits_is_refused(repo):
    # The merge step puts `main` where origin has it. Anything local on top
    # would vanish, so it is refused instead of rewound.
    work(repo)
    git("checkout", "--quiet", "main", cwd=repo)
    (repo / "stray.txt").write_text("by hand")
    git("add", "-A", cwd=repo)
    git("commit", "--quiet", "-m", "committed straight to main", cwd=repo)
    git("checkout", "--quiet", "dev", cwd=repo)
    with pytest.raises(ButlerError, match="not what origin has"):
        release_cycle.cut(ctx_for(repo), args_for("1.2.3"))


def test_a_version_already_tagged_is_refused_and_says_how_to_re_cut(repo):
    work(repo)
    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    with pytest.raises(ButlerError, match="already exists") as e:
        release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    assert "--retag" in (e.value.hint or "")


# ---- re-cutting a version whose build failed -------------------------------- #

@pytest.fixture
def unpublished(monkeypatch):
    """No release exists on Forgejo, and nothing is on the mirror — the state
    a failed build leaves behind."""
    monkeypatch.setattr(release, "published", lambda host, repo, tag: False)
    monkeypatch.setattr(release_cycle, "_mirror_has_tag", lambda ctx, pub, tag: False)


def test_a_re_cut_still_leaves_one_commit_on_the_publication_branch(repo, unpublished):
    work(repo, "the broken one")
    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    first = git("rev-parse", "main", cwd=repo)

    work(repo, "the fix")
    assert release_cycle.cut(ctx_for(repo), args_for("1.2.3", retag=True)) == 0

    log = git("log", "--format=%s", "main", cwd=repo).splitlines()
    assert log == ["demo 1.2.3", "first"]          # still one release commit
    second = git("rev-parse", "main", cwd=repo)
    assert second != first                          # ...but a rebuilt one
    assert git("rev-parse", "v1.2.3^{commit}", cwd=repo) == second
    assert (repo / "file.txt").read_text() == "the fix"


def test_a_re_cut_moves_the_tag_on_origin_and_lands_one_commit(repo, unpublished):
    work(repo, "the broken one")
    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    work(repo, "the fix")
    release_cycle.cut(ctx_for(repo), args_for("1.2.3", retag=True))

    origin = repo.parent / "origin.git"
    # The tag moved; the branch never moved for the failed attempt at all, so
    # nothing on origin had to be rewound.
    assert git("rev-parse", "v1.2.3^{commit}", cwd=origin) == \
        git("rev-parse", "main", cwd=repo)
    assert git("log", "--format=%s", "main", cwd=origin).splitlines() == ["first"]

    # And when its build passes, the release lands as a plain fast-forward.
    release_cycle.cut(ctx_for(repo), args_for("1.2.3", from_step="land"))
    assert git("log", "--format=%s", "main", cwd=origin).splitlines() == \
        ["demo 1.2.3", "first"]


def test_a_re_cut_is_refused_once_the_release_is_published(repo, monkeypatch):
    # The release names a commit. Moving the tag would leave it describing one
    # nobody can check out, and whoever downloaded it holding artifacts built
    # from code that is no longer anywhere.
    work(repo)
    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    monkeypatch.setattr(release, "published", lambda host, repo, tag: True)
    with pytest.raises(ButlerError, match="already has a release"):
        release_cycle.cut(ctx_for(repo), args_for("1.2.3", retag=True))


def test_a_re_cut_is_refused_once_the_tag_is_on_the_mirror(repo, monkeypatch):
    work(repo)
    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    monkeypatch.setattr(release, "published", lambda host, repo, tag: False)
    monkeypatch.setattr(release_cycle, "_mirror_has_tag", lambda ctx, pub, tag: True)
    with pytest.raises(ButlerError, match="already on github.com"):
        release_cycle.cut(ctx_for(repo), args_for("1.2.3", retag=True))


def test_a_re_cut_refuses_a_publication_branch_that_moved_on(repo, unpublished):
    # Rewinding past anything butler did not put there would throw away work.
    work(repo)
    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    git("checkout", "--quiet", "main", cwd=repo)
    (repo / "stray.txt").write_text("by hand")
    git("add", "-A", cwd=repo)
    git("commit", "--quiet", "-m", "straight to main", cwd=repo)
    git("push", "--quiet", "origin", "main", cwd=repo)
    git("checkout", "--quiet", "dev", cwd=repo)
    with pytest.raises(ButlerError, match="would rewrite commits butler did not make"):
        release_cycle.cut(ctx_for(repo), args_for("1.2.3", retag=True))


# ---- waiting for the build -------------------------------------------------- #

def runs(*items):
    return json.dumps({"workflow_runs": list(items)}).encode()


def run_item(**over):
    item = {"id": 1, "prettyref": "v1.2.3", "commit_sha": "abc", "status": "success",
            "workflow_id": "build.yml", "html_url": "https://h/run/1"}
    item.update(over)
    return item


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(release.time, "sleep", lambda s: None)
    monkeypatch.setattr(release, "token", lambda host: "t")


class WaitCtx:
    dry_run = False

    def would(self, description):
        return False


def serve_runs(monkeypatch, *payloads):
    """Answer successive polls with successive payloads, the last repeating."""
    seen = iter(payloads)
    last = {"p": payloads[-1]}

    def _get(url, tok):
        last["p"] = next(seen, last["p"])
        return last["p"]

    monkeypatch.setattr(release, "_get", _get)


def wait(cfg_workflow=None, timeout=60):
    release.wait_for_build(WaitCtx(), host="h", repo="a/b", tag="v1.2.3",
                           sha="abc", timeout=timeout, poll=0,
                           workflow=cfg_workflow)


def test_a_passing_build_lets_the_release_through(monkeypatch, no_sleep):
    serve_runs(monkeypatch, runs(run_item()))
    wait()


def test_it_waits_while_the_build_is_still_going(monkeypatch, no_sleep):
    serve_runs(monkeypatch,
               runs(),                                    # not registered yet
               runs(run_item(status="waiting")),
               runs(run_item(status="running")),
               runs(run_item(status="success")))
    wait()


def test_a_failed_build_stops_the_release_and_points_at_the_run(monkeypatch, no_sleep):
    # Exporting anyway would put a public tag on the mirror whose release
    # never arrives.
    serve_runs(monkeypatch, runs(run_item(status="failure")))
    with pytest.raises(ButlerError, match="build failed") as e:
        wait()
    assert "https://h/run/1" in (e.value.hint or "")
    assert "--retag" in (e.value.hint or "")


def test_one_failed_workflow_fails_the_release(monkeypatch, no_sleep):
    serve_runs(monkeypatch, runs(run_item(), run_item(
        id=2, workflow_id="lint.yml", status="failure")))
    with pytest.raises(ButlerError, match="lint.yml"):
        wait()


def test_a_re_run_replaces_the_failure_it_re_ran(monkeypatch, no_sleep):
    # Re-running a flaked job is how it is dealt with; the old failure must not
    # keep failing the wait forever after.
    serve_runs(monkeypatch, runs(run_item(id=1, status="failure"),
                                 run_item(id=2, status="success")))
    wait()


def test_runs_of_other_tags_and_other_commits_are_ignored(monkeypatch, no_sleep):
    serve_runs(monkeypatch, runs(run_item(id=9, prettyref="dev", status="failure"),
                                 run_item(id=8, commit_sha="other", status="failure"),
                                 run_item()))
    wait()


def test_only_the_named_workflow_is_waited_on(monkeypatch, no_sleep):
    serve_runs(monkeypatch, runs(run_item(), run_item(
        id=2, workflow_id="nightly.yml", status="failure")))
    wait(cfg_workflow="build.yml")


def test_a_build_that_never_finishes_times_out(monkeypatch, no_sleep):
    serve_runs(monkeypatch, runs(run_item(status="running")))
    with pytest.raises(ButlerError, match="has not finished"):
        wait(timeout=0)


def test_dry_run_waits_for_nothing(monkeypatch):
    class Dry(WaitCtx):
        dry_run = True

        def would(self, description):
            return True

    def boom(*a, **kw):
        raise AssertionError("dry-run must not call the API")

    monkeypatch.setattr(release, "_get", boom)
    release.wait_for_build(Dry(), host="h", repo="a/b", tag="v1", sha="s",
                           timeout=1, poll=0)


def test_a_checkout_without_the_publication_branch_creates_it(repo):
    # A fresh clone has `main` only as a remote-tracking ref, and cutting a
    # release is often the first thing that needs it locally.
    work(repo)
    git("branch", "-D", "main", cwd=repo)
    assert release_cycle.cut(ctx_for(repo), args_for("1.2.3")) == 0
    assert git("log", "--format=%s", "main", cwd=repo).splitlines() == \
        ["demo 1.2.3", "first"]


def test_an_orphan_publication_branch_works(repo):
    # butler's own `main` is unrelated to `dev`: the published history was
    # started fresh rather than carrying the private one. `git merge --squash`
    # refuses that outright ("refusing to merge unrelated histories"), which is
    # why the release commit is a tree copy instead.
    git("checkout", "--quiet", "--orphan", "public", cwd=repo)
    git("rm", "-rqf", ".", cwd=repo)
    (repo / "README.md").write_text("the published one\n")
    git("add", "README.md", cwd=repo)
    git("commit", "--quiet", "-m", "demo 1.2.2", cwd=repo)
    git("branch", "-M", "public", "main", cwd=repo)
    git("push", "--quiet", "--force", "origin", "main", cwd=repo)
    git("checkout", "--quiet", "dev", cwd=repo)

    work(repo)
    assert release_cycle.cut(ctx_for(repo), args_for("1.2.3")) == 0
    assert git("log", "--format=%s", "main", cwd=repo).splitlines() == \
        ["demo 1.2.3", "demo 1.2.2"]
    # The release commit carries the work branch's tree, whole.
    assert git("rev-parse", "main^{tree}", cwd=repo) == \
        git("rev-parse", "dev^{tree}", cwd=repo)


def test_the_working_tree_is_never_checked_out(repo):
    # The branch is written with plumbing, so an ignored build directory — or
    # anything else the checkout is sitting on — survives a release untouched.
    work(repo)
    (repo / ".gitignore").write_text("junk/\n")
    git("add", "-A", cwd=repo)
    git("commit", "--quiet", "-m", "ignore junk", cwd=repo)
    git("push", "--quiet", "origin", "dev", cwd=repo)
    (repo / "junk").mkdir()
    (repo / "junk" / "big.bin").write_text("expensive to rebuild")

    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    assert (repo / "junk" / "big.bin").read_text() == "expensive to rebuild"
    assert (repo / "file.txt").exists()       # still dev's tree, not main's
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dev"


def test_cutting_from_the_publication_branch_is_refused(repo):
    # update-ref would move the branch out from under the index, leaving a
    # working tree describing a commit it no longer has.
    work(repo)
    git("checkout", "--quiet", "main", cwd=repo)
    with pytest.raises(ButlerError, match="on 'main', the branch the release writes"):
        release_cycle.cut(ctx_for(repo), args_for("1.2.3"))


def test_a_resumed_run_does_not_add_a_second_commit(repo):
    # Same tree, same subject, same parent: the commit this run would make is
    # already there, and making another would put two on the branch for one
    # release.
    work(repo)
    ctx, args = ctx_for(repo), args_for("1.2.3")
    plan = release_cycle.preflight(ctx, ctx.cfg.release, args, "1.2.3", "v1.2.3")
    first = release_cycle.merge(ctx, ctx.cfg.release, plan)
    again = release_cycle.merge(ctx, ctx.cfg.release, plan)
    assert first == again
    assert git("log", "--format=%s", "main", cwd=repo).splitlines() == \
        ["demo 1.2.3", "first"]


def test_a_tag_moved_locally_does_not_break_the_preflight(repo, unpublished):
    # Mid-re-cut: the tag has moved here and not yet on origin. `git fetch
    # --tags` calls that "would clobber existing tag" and exits 1, which used
    # to fail the run before it could do anything about it.
    work(repo, "the broken one")
    release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    release_cycle.cut(ctx_for(repo), args_for("1.2.3", from_step="land"))
    work(repo, "the fix")
    ctx, args = ctx_for(repo), args_for("1.2.3", retag=True)
    plan = release_cycle.preflight(ctx, ctx.cfg.release, args, "1.2.3", "v1.2.3")
    release_cycle.merge(ctx, ctx.cfg.release, plan)
    release_cycle.tag(ctx, ctx.cfg.release, plan, git("rev-parse", "main", cwd=repo))

    # Local tag now differs from origin's; the preflight must still run.
    again = release_cycle.preflight(ctx, ctx.cfg.release, args, "1.2.3", "v1.2.3")
    assert again.retag is True


def test_releases_turned_off_on_the_forge_stops_the_release(repo, monkeypatch):
    # The CI job publishes the release that gets copied to the mirror, and
    # every releases endpoint answers 404 while the unit is disabled — an
    # answer indistinguishable from a wrong URL. Asking before the tag exists
    # turns three failed builds into one refusal.
    monkeypatch.setattr(release, "releases_enabled", lambda host, repo: False)
    work(repo)
    with pytest.raises(ButlerError, match="has Releases turned off"):
        release_cycle.cut(ctx_for(repo), args_for("1.2.3"))
    assert not proc.capture(["git", "rev-parse", "-q", "--verify", "v1.2.3"],
                            cwd=repo).ok


def test_a_project_without_release_copying_does_not_ask(repo, monkeypatch):
    def boom(host, repo):
        raise AssertionError("release = false must not query the forge")

    monkeypatch.setattr(release, "releases_enabled", boom)
    work(repo)
    cfg_text = TOML.replace("release = true", "release = false")
    import tomllib as t
    cfg = config.parse(t.loads(cfg_text % {"extra": ""}), repo)
    assert release_cycle.cut(Ctx(cfg=cfg, assume_yes=True), args_for("1.2.3")) == 0
