"""The publish component: config, the single exclude list, and the generated
Copybara workflow.

The regex tests carry the most weight. `glob_to_regex` exists so a project can
state its exclude list once, and the tag-equivalence check can ask the same
question Copybara asks — a mismatch there is how a private file reaches a
public repository.
"""

import tomllib

import pytest

from butler import config
from butler.components import publish
from butler.errors import ButlerError, ConfigError


def parse(text: str):
    from pathlib import Path
    return config.parse(tomllib.loads(text), Path("/proj"))


BASE = '''
[project]
name = "demo"

[publish]
forgejo = "acme/demo"
host = "git.example.org"
author = "dev <dev@example.org>"
'''


# ---- config --------------------------------------------------------------- #

def test_minimal_publish_section():
    cfg = parse(BASE).publish
    assert cfg is not None
    assert cfg.forgejo == "acme/demo"
    # The mirror defaults to the same path; only a renaming project says it twice.
    assert cfg.github == "acme/demo"
    assert cfg.visibility == "private"
    assert cfg.branch == "main"
    # One identity by default — the committer follows the author.
    assert cfg.committer == cfg.author == "dev <dev@example.org>"


def test_urls_are_derived_not_configured():
    cfg = parse(BASE).publish
    assert cfg.origin_url == "ssh://git@git.example.org:2222/acme/demo.git"
    assert cfg.destination_url == "git@github.com:acme/demo.git"


def test_mirror_may_be_renamed():
    cfg = parse(BASE + 'github = "acme/demo-harness"\n').publish
    assert cfg.destination_url == "git@github.com:acme/demo-harness.git"


def test_author_is_required():
    with pytest.raises(ConfigError, match="missing the required key 'author'"):
        parse('[project]\nname="d"\n[publish]\nforgejo = "a/b"\nhost = "h"\n')


def test_visibility_is_checked():
    with pytest.raises(ConfigError, match="must be .public. or .private."):
        parse(BASE + 'visibility = "internal"\n')


def test_repo_paths_must_be_owner_name():
    with pytest.raises(ConfigError, match="must be .owner/name."):
        parse('[project]\nname="d"\n[publish]\nforgejo = "demo"\nhost = "h"\nauthor = "a <a@b.c>"\n')


def test_unknown_key_is_rejected():
    # The section reads as "[.publish]" — top-level tables are named relative to
    # an unnamed root. Pre-existing across every section; pinned here as-is.
    with pytest.raises(ConfigError, match=r"has unknown key\(s\): exlcude"):
        parse(BASE + 'exlcude = ["x"]\n')


# ---- the exclude list ----------------------------------------------------- #

def test_project_excludes_add_to_the_baseline_and_cannot_remove_from_it():
    cfg = parse(BASE + 'exclude = ["CHANGELOG.md"]\n').publish
    got = publish.excludes(cfg)
    for required in publish.BASELINE_EXCLUDE:
        assert required in got
    assert "CHANGELOG.md" in got


def test_the_baseline_holds_back_ci_plans_and_the_machinery():
    rx = publish.excluded_re(parse(BASE).publish)
    for held in ("DEPLOY-PLAN.md", "docs/SERVER-PLAN.md",
                 ".forgejo/workflows/ci.yml", "copy.bara.sky", "push-public.sh"):
        assert rx.search(held), f"{held} should be excluded"
    for shipped in ("README.md", "src/main.py", "docs/plan.md", "plans/x.md"):
        assert not rx.search(shipped), f"{shipped} should be published"


@pytest.mark.parametrize("pattern,hit,miss", [
    ("*-PLAN.md", "X-PLAN.md", "PLAN.md.bak"),
    ("**/*-PLAN.md", "a/b/X-PLAN.md", "a/b/plan.md"),
    ("proposals/**", "proposals/a/b.md", "proposals.md"),
    ("copy.bara.sky", "copy.bara.sky", "src/copy.bara.sky"),
    ("server/docker/**", "server/docker/Dockerfile", "server/dockerfile"),
])
def test_glob_to_regex_matches_what_copybara_would(pattern, hit, miss):
    import re
    rx = re.compile(publish.glob_to_regex(pattern))
    assert rx.search(hit)
    assert not rx.search(miss)


def test_a_dot_is_not_a_wildcard():
    # re.escape's job, but worth pinning: "copy.bara.sky" must not match
    # "copyXbaraYsky", which a naive translation would allow.
    import re
    rx = re.compile(publish.glob_to_regex("copy.bara.sky"))
    assert not rx.search("copyXbaraYsky")


def test_unsupported_glob_syntax_raises_rather_than_matching_too_little():
    # Silently matching nothing is how a private path ships.
    with pytest.raises(ButlerError, match="brace expansion"):
        publish.glob_to_regex("{a,b}.md")
    with pytest.raises(ButlerError, match="not supported"):
        publish.glob_to_regex("a/**/b.md")


# ---- the generated workflow ----------------------------------------------- #

def test_workflow_names_both_repos_and_pins_the_identity():
    cfg = parse(BASE).publish
    sky = publish.workflow(cfg, destination=cfg.destination_url)
    assert 'url = "ssh://git@git.example.org:2222/acme/demo.git"' in sky
    assert 'url = "git@github.com:acme/demo.git"' in sky
    assert 'authoring.overwrite("dev <dev@example.org>")' in sky
    assert 'metadata.scrubber' in sky


def test_workflow_carries_every_exclude():
    cfg = parse(BASE + 'exclude = ["secrets/**"]\n').publish
    sky = publish.workflow(cfg, destination="x")
    for pattern in publish.excludes(cfg):
        assert f'"{pattern}"' in sky


def test_the_harness_pin_is_repointed_at_the_public_mirror():
    # A shim left pointing at Forgejo makes the published repo unbuildable by
    # anyone but its author.
    cfg = parse(BASE).publish
    sky = publish.workflow(cfg, destination="x")
    assert "github.com/vaelum/butler-harness.git" in sky  # the default mirror
    assert '"butler.py"' in sky and '"src/butler/new.py"' in sky


def test_the_rewrite_covers_the_harness_own_scaffolder():
    # src/butler/new.py writes the pin into every shim it generates. Excluding
    # it would publish a harness whose `butler new` fails on import, so it is
    # rewritten in place like the shim.
    sky = publish.workflow(parse(BASE).publish, destination="x")
    assert "src/butler/new.py" in sky


def test_generated_starlark_has_no_string_terminating_quotes():
    # The pin regex is embedded in a Starlark string. A " or ' inside it ends
    # that string early and the whole config fails to parse — which only shows
    # up when an export is attempted.
    assert '"' not in publish.HARNESS_PRIVATE_RE
    assert "'" not in publish.HARNESS_PRIVATE_RE
    sky = publish.workflow(parse(BASE).publish, destination="x")
    for line in sky.splitlines():
        assert line.count('"') % 2 == 0, f"unbalanced quotes: {line}"


def test_the_pin_regex_matches_a_real_shim_line():
    import re
    rx = re.compile(publish.HARNESS_PRIVATE_RE)
    line = 'HARNESS_URL = "git+ssh://git@git.example.org:2222/acme/butler.git"'
    m = rx.search(line)
    assert m and m.group(1) == "git+ssh://git@git.example.org:2222/acme/butler.git"


def test_the_transform_declares_an_empty_reversal():
    # Copybara refuses to load a core.transform it cannot reverse, and a
    # regex_groups replace has no automatic inverse. Caught only by actually
    # running an export, so it is pinned here.
    sky = publish.workflow(parse(BASE).publish, destination="x")
    assert "reversal = []" in sky


def test_rehearsal_implies_init(monkeypatch, tmp_path):
    # A rehearsal always targets a repo created empty seconds earlier, so it
    # always needs --init-history; requiring --rehearse --init would be noise.
    import butler.components.publish as pub
    seen = {}

    class FakeCtx:
        name, root, dry_run, verbose = "demo", tmp_path, False, False

        def would(self, _): return False

        def run(self, cmd, cwd=None):
            seen["cmd"] = cmd
            return pub.NO_OP

    cfg = parse(BASE).publish
    monkeypatch.setattr(pub, "_cfg", lambda ctx: cfg)
    monkeypatch.setattr(pub, "require_pushed", lambda ctx, c: None)
    monkeypatch.setattr(pub, "ensure_jar", lambda ctx: tmp_path / "c.jar")
    monkeypatch.setattr(pub, "_java", lambda: "/usr/bin/java")
    monkeypatch.setattr(pub.proc, "check", lambda *a, **k: None)
    monkeypatch.setattr(pub.proc, "capture",
                        lambda *a, **k: pub.proc.Result(0, "", ""))

    pub.publish(FakeCtx(), type("A", (), {"rehearse": True, "init": False, "tag": None})())
    assert "--init-history" in seen["cmd"]


def test_markdown_globs_carry_both_spellings():
    # Copybara's "**/*.md" does not match a root-level README.md. Every
    # hand-written config in this fleet listed "*-PLAN.md" and "**/*-PLAN.md"
    # side by side for exactly this reason, and getting it wrong is silent —
    # the rewrite does not happen and the private link ships.
    sky = publish.workflow(parse(BASE).publish, destination="x")
    for block in sky.split("core.replace")[1:]:
        if '"**/*.md"' in block:
            assert '"*.md"' in block, "**/*.md without *.md misses root files"


def test_host_is_required_not_defaulted():
    # Defaulting it would bake one person's server into a published, reusable
    # harness — and into the docs and tests of every copy.
    with pytest.raises(ConfigError, match="missing the required key 'host'"):
        parse('[project]\nname="d"\n[publish]\nforgejo="a/b"\nauthor="a <a@b.c>"\n')


def test_the_harness_link_rule_appears_only_when_asked_for():
    # The harness is the one repo whose mirror is renamed, so a link to it
    # cannot be mapped path-for-path by the generic rule.
    plain = publish.workflow(parse(BASE).publish, destination="x")
    assert "https://git.example.org/acme/harness" not in plain

    named = publish.workflow(
        parse(BASE + 'harness_forgejo = "acme/harness"\n').publish, destination="x")
    assert 'before = "https://git.example.org/acme/harness"' in named
    assert 'after = "https://github.com/vaelum/butler-harness"' in named


def test_generic_forgejo_links_map_path_for_path():
    sky = publish.workflow(parse(BASE).publish, destination="x")
    assert 'before = "https://git.example.org/${repo}"' in sky
    assert 'after = "https://github.com/${repo}"' in sky


def test_tag_is_a_flag_because_publish_is_a_branch_node():
    # `publish` carries a `check` child, so a positional tag would be read as a
    # subcommand name and any real tag rejected as an invalid choice.
    n = publish.node(parse(BASE).publish)
    assert [c.name for c in n.children] == ["check"]
    flags = [f for a in n.args for f in a.flags]
    assert "--tag" in flags
    assert not any(f for f in flags if not f.startswith("-"))


def test_the_publish_table_is_stripped_from_the_exported_config():
    # [publish] is export machinery and it names the private host. The rest of
    # butler.toml describes how the project is built and is worth publishing,
    # so the table goes rather than the file.
    sky = publish.workflow(parse(BASE).publish, destination="x")
    assert 'paths = glob(["butler/butler.toml"])' in sky
    assert "multiline = True" in sky
    # Starlark rejects \[ in a plain string, so the pattern must be a raw
    # string; and Copybara matches with RE2, which has no lookahead.
    assert 'r"\\n\\[publish' in sky
    assert "(?=" not in sky, "RE2 has no lookahead"


def test_the_strip_pattern_removes_the_table_and_nothing_else():
    import re
    pat = publish.PUBLISH_TABLE_RE
    toml = ('[project]\nname = "x"\n\n[publish]\nforgejo = "a/b"\n'
            'host = "h"\n\n[app]\nkind = "tauri"\n')
    out = re.sub(pat, "\n", toml)
    assert "[publish]" not in out and "host" not in out
    assert '[project]' in out and '[app]' in out and 'kind = "tauri"' in out


def test_the_rewrite_can_be_turned_off():
    cfg = parse(BASE + "rewrite_harness = false\n").publish
    assert "butler-harness" not in publish.workflow(cfg, destination="x")


def test_generated_config_says_not_to_commit_it():
    sky = publish.workflow(parse(BASE).publish, destination="x")
    assert "do not commit" in sky.lower()
