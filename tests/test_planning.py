"""planning: the types, check's rules, and the commands that read files."""

import datetime as dt
import json
import subprocess
import threading
import tomllib
from pathlib import Path

import pytest

from butler import cli
from butler.planning import answers, check, convert, folder, guide, state, types

REPO = Path(__file__).resolve().parent.parent
TODAY = dt.date.today().isoformat()


def make_project(tmp_path: Path) -> Path:
    (tmp_path / "butler").mkdir(parents=True)
    (tmp_path / "butler" / "butler.toml").write_text('[project]\nname = "demo"\n')
    return tmp_path


def run(root: Path, *argv: str) -> int:
    return cli.main(["--no-color", "planning", *argv], root=root)


@pytest.fixture
def proj(tmp_path):
    root = make_project(tmp_path)
    assert run(root, "init") == 0
    return root


def plan_text(**over) -> str:
    """A small valid plan; keyword arguments replace whole lines by key."""
    base = f'''kind = "plan"
id = "p-4k7x2m"
title = "Monitoring"
summary = "A check per service."
created = {TODAY}
updated = {TODAY}
why = "Nothing notices when a container stops."

[[decision]]
id = "alerts"
question = "Where should alerts go?"
recommend = "ntfy"
because = "It runs already."

  [[decision.option]]
  id = "ntfy"
  label = "ntfy"

  [[decision.option]]
  id = "mail"
  label = "Mail"

[[phase]]
id = "p1"
title = "Checks"

  [[phase.step]]
  id = "s1-1"
  title = "A check per service"
  by = "agent"

  [[phase.step]]
  id = "s1-2"
  title = "Deploy"
  by = "agent"
  gate = true
  needs = ["alerts", "s1-1"]

  [[phase.step]]
  id = "s1-3"
  title = "Confirm the phone rings"
  by = "user"
  needs = ["s1-2"]
'''
    for key, value in over.items():
        lines = base.splitlines()
        base = "\n".join(value if line.strip().startswith(key.replace("_", "-") + " =")
                         or line.strip().startswith(key + " =") else line for line in lines) + "\n"
    return base


def put(root: Path, rel: str, text: str) -> Path:
    p = root / "planning" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def report(root: Path):
    return check.run(folder.read(root / "planning", root), git=False)


def messages(rep, level="error"):
    return [f.message for f in rep.findings if f.level == level]


# --------------------------------------------------------------------------- #
# types, schema, guide
# --------------------------------------------------------------------------- #

def test_ids_are_well_formed_and_unique():
    ids = {types.new_id("plan") for _ in range(200)}
    assert len(ids) == 200
    assert all(types.ID_RE.match(i) and i.startswith("p-") for i in ids)
    assert types.new_id("note").startswith("n-")


def test_unknown_keys_and_wrong_types_are_errors():
    raw = tomllib.loads(plan_text().replace('by = "agent"', 'by = "robot"', 1) + '\ncolour = "blue"\n')
    loaded = types.load(raw)
    text = " ".join(str(p) for p in loaded.problems)
    assert "unknown key 'colour'" in text
    assert "'robot'" in text


def test_step_hash_ignores_state_and_defaults():
    step = {"id": "s1", "title": "Deploy", "by": "agent", "gate": True}
    assert types.step_hash(step) == types.step_hash({**step, "state": "done", "commit": "abc"})
    assert types.step_hash(step) != types.step_hash({**step, "title": "Deploy elsewhere"})
    plain = {"id": "s2", "title": "x", "by": "agent"}
    assert types.step_hash(plain) == types.step_hash({**plain, "gate": False, "state": "open"})


def test_the_guide_reference_is_generated_from_the_types():
    assert guide.PATH.read_text() == guide.rendered(guide.PATH.read_text()), \
        "GUIDE.md is out of date: python -m butler.planning.guide"


def test_json_schema_covers_every_kind():
    schema = types.json_schema()
    kinds = {b["properties"]["kind"]["const"] for b in schema["oneOf"]}
    assert kinds == set(types.KINDS)
    json.dumps(schema)


# --------------------------------------------------------------------------- #
# the commands on a fresh folder
# --------------------------------------------------------------------------- #

def test_init_new_and_check(proj, capsys):
    root = proj / "planning"
    assert (root / "planning.toml").is_file()
    assert (root / ".schema" / "v1.json").is_file()
    for kind in types.KINDS:
        assert run(proj, "new", kind, f"a-{kind}") == 0
    rep = report(proj)
    assert rep.errors == 0, messages(rep)
    kinds = sorted(i.kind for i in folder.read(root).items)
    assert kinds == sorted(types.KINDS)
    first = (root / "draft" / "a-plan.toml").read_text().splitlines()[0]
    assert first == "#:schema ../.schema/v1.json"


def test_new_refuses_a_wrong_folder(proj):
    assert run(proj, "new", "note", "x", "--folder", "todo") != 0


def test_this_repositorys_plan_checks_clean(tmp_path):
    # As committed, answers included: a gated step that is active needs the
    # approval in answers/, and the committed plan must pass its own check.
    import shutil
    shutil.copytree(REPO / "planning", tmp_path / "planning", ignore=shutil.ignore_patterns(".*.lock"))
    rep = check.run(folder.read(tmp_path / "planning", tmp_path), git=False)
    assert rep.errors == 0, messages(rep)


# --------------------------------------------------------------------------- #
# check: one failing fixture per rule
# --------------------------------------------------------------------------- #

def test_toml_error_names_the_line(proj):
    put(proj, "draft/broken.toml", 'kind = "plan"\nid = "p-4k7x2m"\ntitle = "x\n')
    rep = report(proj)
    f = next(f for f in rep.findings if "not valid TOML" in f.message)
    assert f.line == 3


def test_three_quotes_inside_prose_get_a_hint(proj):
    put(proj, "draft/q.toml", "kind = 'plan'\nwhy = '''\nsay ''' here\n'''\n")
    assert any("three single quotes" in m for m in messages(report(proj)))


def test_future_dates_are_errors(proj):
    future = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    put(proj, "draft/x.toml", plan_text(updated=f"updated = {future}"))
    assert any("later than today" in m for m in messages(report(proj)))


def test_bad_and_mismatched_ids(proj):
    put(proj, "draft/a.toml", plan_text(id='id = "plan-1"'))
    put(proj, "draft/b.toml", plan_text(id='id = "n-4k7x2m"'))
    errs = " ".join(messages(report(proj)))
    assert "is not <letter>-<six base32" in errs
    assert "is a note's id" in errs


def test_a_copied_file_is_caught(proj):
    put(proj, "draft/a.toml", plan_text())
    put(proj, "draft/b.toml", plan_text())
    assert any("is also the id of" in m for m in messages(report(proj)))


def test_duplicate_local_ids(proj):
    put(proj, "draft/a.toml", plan_text().replace('id = "s1-3"', 'id = "s1-1"'))
    assert any("local id 's1-1'" in m for m in messages(report(proj)))


def test_references_must_resolve_and_not_circle(proj):
    text = plan_text().replace('needs = ["s1-2"]', 'needs = ["nowhere"]')
    put(proj, "draft/a.toml", text)
    assert any("'nowhere', which does not exist" in m for m in messages(report(proj)))
    text = plan_text().replace('title = "A check per service"\n  by = "agent"',
                               'title = "A check per service"\n  by = "agent"\n  needs = ["s1-3"]')
    put(proj, "draft/a.toml", text)
    assert any("in a circle" in m for m in messages(report(proj)))


def test_decision_rules(proj):
    text = plan_text().replace('recommend = "ntfy"', 'recommend = "pigeon"')
    put(proj, "draft/a.toml", text)
    assert any("recommends 'pigeon'" in m for m in messages(report(proj)))
    text = plan_text().replace('because = "It runs already."', 'resolved = ["ntfy"]')
    put(proj, "draft/a.toml", text)
    errs = " ".join(messages(report(proj)))
    assert "recommend needs a because" in errs and "resolved needs an outcome" in errs


def test_folder_rules(proj):
    put(proj, "todo/a.toml", plan_text())
    errs = " ".join(messages(report(proj)))
    assert "has open decisions: alerts" in errs
    assert "has not been approved" in errs
    put(proj, "todo/a.toml", "")
    (proj / "planning/todo/a.toml").unlink()
    put(proj, "done/a.toml", plan_text())
    errs = " ".join(messages(report(proj)))
    assert "steps not done or dropped" in errs and "decisions not resolved" in errs


def test_a_note_outside_notes_and_a_blocked_step_without_reason(proj):
    put(proj, "draft/n.toml", f'kind = "note"\nid = "n-4k7x2m"\ntitle = "n"\nsummary = "s"\n'
                              f'created = {TODAY}\nupdated = {TODAY}\n\n[[section]]\ntitle = "t"\nbody = "b"\n')
    put(proj, "draft/a.toml", plan_text().replace('title = "A check per service"\n  by = "agent"',
                                                  'title = "A check per service"\n  by = "agent"\n  state = "blocked"'))
    errs = " ".join(messages(report(proj)))
    assert "a note belongs in notes" in errs
    assert "s1-1 is blocked but says no reason" in errs


def test_a_done_step_needs_what_it_needs_done(proj):
    put(proj, "draft/a.toml", plan_text().replace('title = "Confirm the phone rings"\n  by = "user"',
                                                  'title = "Confirm the phone rings"\n  by = "user"\n  state = "done"'))
    assert any("s1-3 is done, but what it needs is not: s1-2" in m for m in messages(report(proj)))


def test_markdown_plans_and_stray_files_are_warnings(proj):
    put(proj, "done/old.md", "# Old\n- [x] thing\n")
    put(proj, "draft/form.html", "<html></html>")
    (proj / "planning" / "junk.txt").write_text("x")
    rep = report(proj)
    warns = " ".join(messages(rep, "warn"))
    assert "Markdown plan" in warns and "forms prototype" in warns and "does not belong" in warns
    assert rep.errors == 0


def test_orphan_answers_are_errors(proj):
    put(proj, "answers/p-zzzzzz.json", "{}")
    assert any("no item has that id" in m for m in messages(report(proj)))


def test_an_id_changed_since_head_is_an_error(proj):
    put(proj, "draft/a.toml", plan_text())
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    subprocess.run(["git", "-C", str(proj), "add", "."], check=True)
    subprocess.run(["git", "-C", str(proj), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                   check=True)
    put(proj, "draft/a.toml", plan_text(id='id = "p-5k7x2m"'))
    rep = check.run(folder.read(proj / "planning", proj), git=True)
    assert any("id changed from p-4k7x2m" in m for m in messages(rep))


# --------------------------------------------------------------------------- #
# answers, approvals, gate
# --------------------------------------------------------------------------- #

def approve(root: Path, item: str, step_id: str, verdict="approved", note=""):
    f = folder.read(root / "planning", root)
    it = f.get(item)
    step = next(s for s in it.steps() if s["id"] == step_id)
    answers.update(f.answers_path(item), item, lambda d: d.setdefault("approvals", {}).__setitem__(
        step_id, {"verdict": verdict, "note": note, "at": answers.now(), "sha256": types.step_hash(step),
                  "snapshot": types.jsonable(types.step_snapshot(step))}))


def test_a_gated_step_done_without_approval_fails_check(proj):
    done = plan_text().replace('gate = true', 'gate = true\n  state = "done"')
    done = done.replace('title = "A check per service"\n  by = "agent"',
                        'title = "A check per service"\n  by = "agent"\n  state = "done"')
    done = done.replace('because = "It runs already."', 'because = "x"\nresolved = ["ntfy"]\noutcome = "o"')
    put(proj, "draft/a.toml", done)
    assert any("gated and done, but has no current approval" in m for m in messages(report(proj)))
    approve(proj, "p-4k7x2m", "s1-2")
    assert not any("no current approval" in m for m in messages(report(proj)))


def test_gate_opens_on_approval_and_closes_when_the_step_changes(proj, capsys):
    put(proj, "draft/a.toml", plan_text())
    assert run(proj, "gate", "p-4k7x2m", "s1-2") == 1
    approve(proj, "p-4k7x2m", "s1-2")
    assert run(proj, "gate", "p-4k7x2m", "s1-2") == 0
    assert "unchanged since" in capsys.readouterr().out
    # Ticking does not make it stale …
    put(proj, "draft/a.toml", plan_text().replace('gate = true', 'gate = true\n  state = "active"'))
    assert run(proj, "gate", "p-4k7x2m", "s1-2") == 0
    # … rewording does.
    put(proj, "draft/a.toml", plan_text().replace('title = "Deploy"', 'title = "Deploy to production"'))
    assert run(proj, "gate", "p-4k7x2m", "s1-2") == 1
    assert "changed since" in capsys.readouterr().out
    # A step without a gate needs nothing.
    assert run(proj, "gate", "p-4k7x2m", "s1-1") == 0


def test_a_disapproved_step_must_be_dropped_or_rewritten(proj):
    put(proj, "draft/a.toml", plan_text())
    approve(proj, "p-4k7x2m", "s1-2", verdict="disapproved", note="not on Friday")
    assert any("was disapproved" in m for m in messages(report(proj)))
    dropped = plan_text().replace('gate = true', 'gate = true\n  state = "dropped"\n  reason = "not on Friday"')
    put(proj, "draft/a.toml", dropped)
    assert not any("was disapproved" in m for m in messages(report(proj)))


def test_answers_merge_one_entry_at_a_time(tmp_path):
    path = tmp_path / "answers" / "p-4k7x2m.json"

    def worker(i):
        answers.update(path, "p-4k7x2m", lambda d: d["decisions"].__setitem__(f"q{i}", {"selected": ["a"]}))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(answers.read(path)["decisions"]) == 20
    # Committed like any other file, not left owner-only by mkstemp.
    assert path.stat().st_mode & 0o777 == 0o644


def test_status_list_and_resolve(proj, capsys):
    put(proj, "draft/a.toml", plan_text())
    path = proj / "planning" / "answers" / "p-4k7x2m.json"
    answers.update(path, "p-4k7x2m", lambda d: (
        d["decisions"].__setitem__("alerts", {"selected": ["mail"], "text": "", "at": "t"}),
        d["comments"].append({"id": "c1", "on": "p1", "text": "back up first", "at": "t"})))
    capsys.readouterr()
    assert run(proj, "status") == 0
    out = capsys.readouterr().out
    assert "answer alerts" in out and "→ Mail" in out
    assert "comment c1" in out
    assert run(proj, "list", "decisions", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["state"] == "answered" and rows[0]["recommend"] == ["ntfy"]
    assert run(proj, "resolve", "p-4k7x2m", "c1", "--note", "added s1-0") == 0
    assert answers.read(path)["comments"][0]["resolved"]["note"] == "added s1-0"


def test_move_checks_the_target_folder(proj):
    put(proj, "draft/a.toml", plan_text())
    assert run(proj, "move", "p-4k7x2m", "todo") != 0
    resolved = plan_text().replace('because = "It runs already."',
                                   'because = "x"\nresolved = ["ntfy"]\noutcome = "amos: ntfy"')
    put(proj, "draft/a.toml", resolved)
    f = folder.read(proj / "planning", proj)
    it = f.get("p-4k7x2m")
    answers.update(f.answers_path(it.id), it.id, lambda d: d.__setitem__("plan_approval", {
        "verdict": "approved", "at": "t", "sha256": types.plan_hash(it.data)}))
    assert run(proj, "move", "p-4k7x2m", "todo") == 0
    assert (proj / "planning" / "todo" / "a.toml").is_file()
    assert report(proj).errors == 0


def test_summary_waits_on_the_user_for_a_draft_plan_approval(proj):
    resolved = plan_text().replace('because = "It runs already."',
                                   'because = "x"\nresolved = ["ntfy"]\noutcome = "o"')
    put(proj, "draft/a.toml", resolved)
    f = folder.read(proj / "planning", proj)
    s = state.summarize(f.get("p-4k7x2m"), {}, plan_approval_required=True)
    assert ("plan-approval", "", "the plan's phases and steps") in s.for_user


# --------------------------------------------------------------------------- #
# convert
# --------------------------------------------------------------------------- #

MD_PLAN = """# Settle the tailnet

Why this matters: keys expire.

## Steps

- [x] **Record what the node shows.** Done 2026-09-29.
- [ ] **Remove the old services.** (amos) In the console.
  Three of them.
- [-] **Ask about the unknown device.** Dropped.

## Related

Plan 12.
"""

FORM = """<!doctype html><html><body data-plan="24-x"><main>
<h1>Settle the choices</h1><p class="lead">lead</p><p>Why: the pages drift.</p>
<section class="card" data-title="repositories.md" data-decision><h3>x</h3>
<p class="summary">It drifts.</p>
<fieldset data-q="repos" data-label="How should it be kept?"><legend>q</legend>
<label class="opt"><input type="radio" name="n" value="generate" data-label="Generate it"><span>Generate<span class="hint">a task</span></span></label>
<label class="opt"><input type="radio" name="n" value="hand" data-label="By hand"><span>Hand</span></label>
<textarea></textarea></fieldset><details><summary>Details</summary><div class="body"><p>Counts <code>25</code>.</p></div></details></section>
<section class="card" data-title="Fix the dates"><h3>y</h3><details><summary>Details</summary><div class="body"><p>Eleven pages.</p></div></details></section>
</main></body></html>"""


def test_convert_a_markdown_plan(proj):
    src = put(proj, "done/22-tailnet.md", MD_PLAN)
    assert run(proj, "convert", str(src)) == 0
    target = proj / "planning" / "done" / "22-tailnet.toml"
    data = tomllib.loads(target.read_text())
    steps = data["phase"][0]["step"]
    assert [s["state"] if "state" in s else "open" for s in steps] == ["done", "open", "dropped"]
    assert steps[1]["by"] == "user" and "Three of them" in steps[1]["details"]
    assert data["section"][0]["title"] == "Related"
    rep = report(proj)
    assert not [f for f in rep.findings if f.file.endswith("22-tailnet.toml") and f.level == "error"
                and "not valid" in f.message], messages(rep)


def test_convert_a_form_carries_its_answers(proj):
    src = put(proj, "draft/24-x.html", FORM)
    put(proj, "draft/24-x.answers.json", json.dumps({"saved_at": "2026-09-30T08:24:11Z", "answers": {
        "repos": {"selected": ["generate"], "text": "", "labels": ["Generate it"]}}}))
    assert run(proj, "convert", str(src)) == 0
    f = folder.read(proj / "planning", proj)
    it = next(i for i in f.items if i.name == "24-x")
    dec = it.decisions()[0]
    assert dec["id"] == "repos" and [o["id"] for o in dec["option"]] == ["generate", "hand"]
    assert "`25`" in dec["context"]
    assert it.steps()[0]["title"] == "Fix the dates"
    a = f.answers(it.id)["decisions"]["repos"]
    assert a["selected"] == ["generate"] and "24-x.answers.json" in a["source"]
    assert "Why: the pages drift." in it.data["why"]


MD_NESTED = """# Node setup

**Status:** in progress since 2026-09-12.

The server path needs a guided install.

## Phases

The order matters.

### N0 — one data root

- [x] Pin the UID and GID: `useradd --system`. Pick a
  number that is free on every distribution.
- [ ] Move config and state under one root.

### N1 — ship the topology

- [ ] **Split the compose file.** Into examples.

## Notes

```
### not a heading
```
"""


def test_convert_reads_subsections_as_phases_and_plain_bullets_as_titles(proj):
    src = put(proj, "in-progress/node-setup.md", MD_NESTED)
    assert run(proj, "convert", str(src)) == 0
    data = tomllib.loads((proj / "planning" / "in-progress" / "node-setup.toml").read_text())
    assert [p["title"] for p in data["phase"]] == ["N0 — one data root", "N1 — ship the topology"]
    first = data["phase"][0]["step"][0]
    assert first["title"] == "Pin the UID and GID: `useradd --system`" and first["state"] == "done"
    assert "free on every distribution" in first["details"]
    assert data["phase"][1]["step"][0]["title"] == "Split the compose file"
    assert data["summary"] == "The server path needs a guided install."
    assert [s["title"] for s in data["section"]] == ["Phases", "Notes"]
    assert "### not a heading" in data["section"][1]["body"]


MD_PROSE = """# Linux

Why it matters.

# Part I — the client

What this part covers.

## Phases

The order matters, and this sentence must survive.

- [x] **Ship the tarball, with a title the Markdown
  wrapped.** Built in CI.
- [ ] Bridge these out:
  - `put`, with a nested item
    that wraps
  - `get`

  ```sh
  make tarball
  ```

Two alternatives were considered and are worse.

## Updates

No updater.
"""


def test_convert_keeps_the_prose_around_checkboxes_and_the_shape_of_a_step(proj):
    src = put(proj, "done/linux.md", MD_PROSE)
    assert run(proj, "convert", str(src)) == 0
    data = tomllib.loads((proj / "planning" / "done" / "linux.toml").read_text())
    titles = [x["title"] for x in data["section"]]
    assert titles == ["Part I — the client", "Part I — the client: Phases", "Part I — the client: Updates"]
    assert data["section"][0]["body"].strip() == "What this part covers."
    phases_text = data["section"][1]["body"]
    assert "this sentence must survive" in phases_text
    assert "Two alternatives were considered and are worse." in phases_text
    assert "→ step s1-1" in phases_text and "→ step s1-2" in phases_text
    first, second = data["phase"][0]["step"]
    assert first["title"] == "Ship the tarball, with a title the Markdown wrapped"
    assert first["details"].strip() == "Built in CI."
    assert second["title"] == "Bridge these out:"
    assert "- `put`, with a nested item\n  that wraps\n- `get`" in second["details"]
    assert "```sh\nmake tarball\n```" in second["details"]


def test_convert_keeps_a_subfolder_under_its_state_folder(proj):
    src = put(proj, "done/followups/01-sweep.md", MD_PLAN)
    assert run(proj, "convert", str(src)) == 0
    assert (proj / "planning" / "done" / "followups" / "01-sweep.toml").is_file()


def test_convert_closes_a_form_from_done(proj):
    src = put(proj, "done/24-x.html", FORM)
    put(proj, "done/24-x.answers.json", json.dumps({"saved_at": "2026-09-30T08:24:11Z", "answers": {
        "repos": {"selected": ["generate"], "text": "", "labels": ["Generate it"]}}}))
    assert run(proj, "convert", str(src)) == 0
    it = next(i for i in folder.read(proj / "planning", proj).items if i.name == "24-x")
    dec = it.decisions()[0]
    assert dec["resolved"] == ["generate"] and str(dec["resolved_on"]) == "2026-09-30"
    assert "24-x.answers.json" in dec["outcome"]
    assert it.steps()[0]["state"] == "done"
    rep = report(proj)
    assert not [f for f in rep.findings if f.file.endswith("24-x.toml") and f.level == "error"], messages(rep)


def test_only_a_resolved_decision_may_have_more_than_four_options(proj):
    opts = "".join(f'\n  [[option]]\n  id = "o{i}"\n  label = "Option {i}"\n' for i in range(6))
    base = ('kind = "decision"\nid = "d-5k2m7x"\ntitle = "t"\nsummary = "s"\ncreated = 2026-09-30\n'
            'updated = 2026-09-30\nquestion = "Which one?"\n')
    put(proj, "draft/many.toml", base + opts)
    assert any("has 6 options" in m for m in messages(report(proj)))
    resolved = base.replace('question = "Which one?"\n', 'question = "Which one?"\nresolved = ["o3"]\n'
                            'outcome = "amos, 2026-09-30: o3."\nresolved_on = 2026-09-30\n')
    (proj / "planning" / "draft" / "many.toml").unlink()
    put(proj, "done/many.toml", resolved + opts)
    assert not any("options" in m for m in messages(report(proj)))


def test_the_users_ready_steps_wait_on_them(proj):
    done = plan_text().replace('because = "It runs already."', 'because = "x"\nresolved = ["ntfy"]\noutcome = "o"')
    for sid in ("s1-1", "s1-2"):
        done = done.replace(f'id = "{sid}"', f'id = "{sid}"\n  state = "done"', 1)
    put(proj, "draft/a.toml", done)
    f = folder.read(proj / "planning", proj)
    s = state.summarize(f.get("p-4k7x2m"), {}, ids=f.by_id())
    assert ("step", "s1-3", "Confirm the phone rings") in s.for_user
    s1_3 = next(x for x in s.open_steps if x["id"] == "s1-3")
    assert s1_3["ready"] and s1_3["by"] == "user"
    # Not yet ready: it waits on s1-2.
    put(proj, "draft/a.toml", plan_text())
    f = folder.read(proj / "planning", proj)
    s = state.summarize(f.get("p-4k7x2m"), {}, ids=f.by_id())
    assert not any(w == "step" for w, _, _ in s.for_user)
    assert next(x for x in s.open_steps if x["id"] == "s1-3")["waiting_on"] == ["s1-2"]
