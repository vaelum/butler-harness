# Planning with butler: the guide for agents

This is the rulebook for `planning/` in any project on the butler harness. It
is printed by `python butler.py planning guide` and ships with the harness, so
it always matches the version the project pins. Read it before writing or
changing anything in a planning folder. The reference tables at the end are
generated from the harness's own type definitions; nothing else is
authoritative.

## 1. The folder

```
planning/
  planning.toml   format = 1, user, plan_approval; its presence makes the folder typed
  draft/          items waiting on the user
  todo/           agreed and ready
  in-progress/    being worked on
  done/           finished, kept as the record
  notes/          context several items share; notes have no state
  answers/        <id>.json: the user's answers and approvals — never yours to write
  .schema/        the JSON Schema editors validate against (`planning schema`)
  .gitignore      keeps the answers' lock files out of git (`planning init` writes it)
```

- **The folder is the state.** Move an item with
  `python butler.py planning move <id> <folder>`, which checks the target
  folder's rules first and then runs `git mv`.
- **Each item is one TOML file** with a `kind`: `plan`, `decision`, `review`
  or `note`. The file name is a readable slug and may change.
- **The `id` is the identity** and never changes: `p-3p758x` is the kind's
  letter and six base32 characters. `planning new` makes it; never invent,
  edit or reuse one. The answers file, the page's URL and every reference
  hang on it, which is why a move or a rename breaks nothing.
- **Two writers, never the same file.** You write the item. The planning
  service writes `answers/<id>.json` when the user clicks in the page. The only
  commands of yours that touch an answers file are `planning resolve` and
  `planning convert`.
- Markdown plans and the old HTML forms are shown read-only and reported by
  `check` as warnings. `planning convert <file>` makes a typed skeleton.

## 2. Which kind

| you have | write a |
|---|---|
| work with steps, or questions that lead to work | `plan` |
| one question for the user that no plan owns, or a settled choice that must not be reopened (`final = true`) | `decision` |
| findings of a review or an audit, to be triaged and handed to plans | `review` |
| context several items read first: ground rules, a data model, research | `note` |

`python butler.py planning new plan <name>` (or `decision`, `review`,
`note`) writes the item from its template with a fresh id. Fill it in, then
run `planning check`.

## 3. Writing TOML that stays valid

- Put every top-level key (`why`, `acceptance`, `needs`, …) **before** the
  first `[table]` or `[[array]]` header; after a header, keys belong to it.
- `[[phase.step]]` attaches to the `[[phase]]` above it, and
  `[[decision.option]]` to the `[[decision]]` above it. Indent them by two
  spaces so the nesting shows.
- **Prose** fields (`why`, `context`, `details`, `body`, `subject`, `method`,
  `evidence`) hold Markdown. Write them as multi-line literal strings between
  three single quotes on each side: nothing inside needs escaping, but the text
  itself must not contain three single quotes in a row.
- Dates are bare TOML dates (`created = 2026-09-30`), not strings.
- Unknown keys are errors. `check` names the key and the line.
- Never a credential value in an item. Name the variable or the file.

## 4. Writing decisions

A decision is a question only the user can answer. Everything the user needs
to answer it is in the decision itself.

- One question each, ending in a question mark.
- `context`: the facts, what each choice costs, and where they come from, as
  checked on `as_of`. The user answers from this, not from a chat.
- Two to four options. Each stands on its own and says what picking it
  means (`hint`). No straw men, no "other" (the user can always write an own
  answer unless `own_answer = false`).
- One `recommend`, and a `because` that gives the reason in one sentence.
  With `multiple = true`, `recommend` may be a list.
- Ask decisions through the page, not in chat: after writing them run
  `python butler.py planning serve` and give the user the URL it prints.
- **Acting on an answer.** `planning status` (or `planning list decisions`)
  shows what the user picked or wrote. When you have changed the plan to
  follow it, record it in the decision:

  ```toml
  resolved = ["random"]          # option ids, or ["own"]
  outcome = "amos, 2026-09-30: as recommended. <what changed because of it>"
  resolved_on = 2026-09-30
  ```

  A resolved decision is locked in the page. Never delete a decision, and
  never resolve one the user has not answered.

## 5. Writing phases and steps

After the decisions and the scope are settled, a plan gets fixed phases of
steps. The user approves the plan as a whole before it leaves `draft/` (when
`plan_approval = true`).

- **A step is one checkable outcome**, owned by one side: `by = "agent"` or
  `by = "user"`. Split a step that both sides act on.
- A user step says exactly what to do, where, and what to report back.
- `done_when` says how to tell it is done; `needs` names the decisions, steps
  or phases that come first (`needs = ["alerts", "s1-1", "p1"]`, or
  `p-4k7x2m/s2-1` in another item).
- States: `open`, `active`, `done`, `blocked`, `dropped`. `blocked` and
  `dropped` need a `reason`. `done` takes the `commit`.
- **Gates.** A step gets `gate = true` when it is hard to undo or visible
  outside the machine: a deploy, a push to a main branch, a release or tag,
  deleting data, repositories or accounts, rotating or revoking credentials,
  sending a message or an email, spending money, changing a live host's
  configuration. When unsure, gate it.

## 6. The four modes

| mode | when | the user, in the page | you |
|---|---|---|---|
| **Decide** | a draft with open decisions | picks an option or writes an own answer | write the decisions (section 4), run `serve`, give the URL; act on answers, record them |
| **Extend** | any time | comments on the item, a section, a phase, a decision, a step or a finding | read comments in `status`, change the item, then `planning resolve <id> <c…> --note "what you did"`; when something new comes up mid-work, add a decision and put it in the `needs` of the steps it holds up |
| **Track** | todo and in-progress | watches progress live; ticks their own steps | set `state = "active"` when you start a step and `"done"` with `commit` when it is, append a `[[log]]` entry, `planning move` the item, `planning check` after every edit |
| **Gate** | before a gated step; before a plan leaves draft | approves or disapproves, with a note; sees what changed since an earlier answer | run `planning gate` right before the action (section 7) |

A user step the user ticked in the page shows as "done in the page, not yet in
the file" until you set its `state = "done"`.

The top of every item's page is the user's to-do list, built from the files:
the decisions to answer, the gates to approve, and the user's own steps whose
`needs` are all finished. It is only as good as the `needs` you write: a user
step without them shows as ready at once, and one that waits on a step you
forgot to set to `done` never shows. `planning status` prints the same list.

## 7. Approvals

- **Right before** a gated step's action, run
  `python butler.py planning gate <id> <step>` and quote its line. Exit 0
  means approved by the user in the page, and the step unchanged since. That
  line is what an action evaluator (a permission classifier, a hook, a person
  reading the transcript) can check.
- Exit 1 means not answered, disapproved (with the user's note), or stale.
  **Stop** and tell the user; give them the URL from `planning serve`. Never
  work around a closed gate.
- An approval covers the step's text: every field but `state`, `commit` and
  `reason`. Editing a gated step after its approval makes the approval stale.
  Record progress on it in the `[[log]]`, not in the step.
- A disapproved step is dropped (`state = "dropped"`, `reason` = the user's
  note) or rewritten, which asks the user again.
- `planning gate <id>` without a step checks the plan's own approval, which
  `planning move <id> todo` also requires.
- **Never** write anything under `planning/answers/`, and never call the
  planning service's API yourself (`curl`, scripts). An approval you wrote is
  worthless, and it shows in the transcript and in git.

## 8. Keeping an item current

During implementation, after each step:

1. set the step's `state` (and `commit`);
2. append a `[[log]]` entry (`on`, `text`, `step`, `commit`);
3. set the item's `updated` to today;
4. run `python butler.py planning check` and fix what it says;
5. when a phase's work changes the plan, say so in a decision or a comment
   answer, not silently.

`planning status` is where to look first when you pick an item up: it lists
what waits on the user and what waits on you.

## 9. Commands

| command | does |
|---|---|
| `planning guide` | this text |
| `planning init` | make `planning/` typed |
| `planning new KIND NAME` | a new item with a fresh id |
| `planning check [--strict] [--json]` | validate the whole folder; exit 1 on an error |
| `planning status [ID] [--json]` | what waits on the user and on you |
| `planning list [items\|decisions\|steps\|approvals\|comments\|findings] [--all] [--by agent\|user] [--everywhere] [--json]` | one line each; open ones unless `--all` |
| `planning show ID` | an item as text |
| `planning move ID FOLDER` | move it, after checking the folder's rules |
| `planning gate ID [STEP]` | exit 0 only if approved and unchanged |
| `planning resolve ID COMMENT --note TEXT` | mark a comment handled |
| `planning convert FILE` | a skeleton from a Markdown plan or an old HTML form |
| `planning serve [--open]` | start or join the local service, register this repository, print the URL |
| `planning service [status\|stop\|restart\|logs]` | the service itself |

The service is one process per user for every repository, on
`127.0.0.1:8765` (`BUTLER_PLANNING_PORT` changes it). Its state is in
`$XDG_STATE_HOME/butler/planning/`.

## 10. Before handing a draft to the user

- [ ] `planning check` passes.
- [ ] Every decision has context, two to four options, a recommendation and a because.
- [ ] Every step has an owner; user steps say exactly what to do.
- [ ] Risky steps are gated.
- [ ] `planning serve` has run, and the user has the URL.

## Reference

<!-- reference:start -->
<!-- generated from butler/planning/types.py by `planning.types.reference()`; do not edit by hand -->

#### `planning.toml`

| key | type | required | meaning |
|---|---|---|---|
| `format` | integer | yes | the format version: 1 |
| `user` | string |  | how the pages and `status` name the user (default: git's user.name) |
| `plan_approval` | boolean | default `true` | a plan needs the user's approval to leave draft/ |

#### Keys every item has

| key | type | required | meaning |
|---|---|---|---|
| `kind` | `"plan"` \| `"decision"` \| `"review"` \| `"note"` | yes | what the file is; must match the id's letter |
| `id` | string | yes | `<letter>-<six base32>`, made by `planning new`; never changes |
| `title` | string | yes | the item's name |
| `summary` | string | yes | one or two sentences, shown on cards |
| `created` | date | yes | the day it was written |
| `updated` | date | yes | the day it last changed; not later than today |
| `context` | list of reference |  | items to read first, usually notes |
| `related` | list of reference |  | items worth knowing about |
| `tags` | list of string |  | free words |

#### `kind = "plan"`

| key | type | required | meaning |
|---|---|---|---|
| `why` | prose (Markdown) | yes | why the work is worth doing |
| `scope` | `[scope]` |  | the `[scope]` table |
| `acceptance` | list of string |  | what done means for the whole plan |
| `needs` | list of reference |  | plans that must be done first |
| `touches` | list of string |  | paths or areas it changes |
| `section` | `[[section]]` |  | design notes that belong to this plan only |
| `decision` | `[[decision]]` |  | questions for the user |
| `phase` | `[[phase]]` |  | the stages of the work, each with its steps |
| `risk` | `[[risk]]` |  | what could go wrong |
| `log` | `[[log]]` |  | progress, oldest first; the agent appends |

#### `kind = "decision"`

| key | type | required | meaning |
|---|---|---|---|
| `context` | prose (Markdown) |  | what the user needs to know to answer |
| `question` | string | yes | one question, ending in a question mark |
| `as_of` | date |  | when the facts in `context` were checked |
| `multiple` | boolean | default `false` | more than one option may be chosen |
| `option` | `[[option]]` | yes | two to four options |
| `recommend` | string or list of strings |  | the recommended option id (a list if `multiple`) |
| `because` | string |  | why that option; required with `recommend` |
| `own_answer` | boolean | default `true` | the user may write their own answer |
| `resolved` | list of string |  | option ids, or ["own"]: set by the agent once it has acted on the answer; a decision without it is open |
| `outcome` | string |  | required with `resolved`: what was decided, by whom, and what changed |
| `resolved_on` | date |  | the day it was resolved |
| `final` | boolean | default `false` | settled, and not to be reopened |

#### `kind = "review"`

| key | type | required | meaning |
|---|---|---|---|
| `subject` | prose (Markdown) | yes | what was reviewed, and at which commit |
| `method` | prose (Markdown) |  | how, and how far the findings were checked |
| `finding` | `[[finding]]` |  | the findings |

#### `kind = "note"`

| key | type | required | meaning |
|---|---|---|---|
| `section` | `[[section]]` | yes | the text, in sections |

#### `[[decision]]` in a plan

| key | type | required | meaning |
|---|---|---|---|
| `id` | string | yes | local id, never renamed or reused |
| `question` | string | yes | one question, ending in a question mark |
| `context` | prose (Markdown) |  | what the user needs to know to answer |
| `as_of` | date |  | when the facts in `context` were checked |
| `multiple` | boolean | default `false` | more than one option may be chosen |
| `option` | `[[option]]` | yes | two to four options |
| `recommend` | string or list of strings |  | the recommended option id (a list if `multiple`) |
| `because` | string |  | why that option; required with `recommend` |
| `own_answer` | boolean | default `true` | the user may write their own answer |
| `resolved` | list of string |  | option ids, or ["own"]: set by the agent once it has acted on the answer; a decision without it is open |
| `outcome` | string |  | required with `resolved`: what was decided, by whom, and what changed |
| `resolved_on` | date |  | the day it was resolved |

#### `[[…option]]`

| key | type | required | meaning |
|---|---|---|---|
| `id` | string | yes | local id of the option |
| `label` | string | yes | what the user reads and picks |
| `hint` | string |  | one line under the label: the consequence of picking it |

#### `[[phase]]`

| key | type | required | meaning |
|---|---|---|---|
| `id` | string | yes | local id |
| `title` | string | yes | what this stage of the work is |
| `goal` | string |  | what is true when the phase is done |
| `step` | `[[step]]` | yes | the steps, in order |

#### `[[phase.step]]`

| key | type | required | meaning |
|---|---|---|---|
| `id` | string | yes | local id, never renamed or reused |
| `title` | string | yes | one checkable outcome |
| `by` | `"agent"` \| `"user"` | yes | who does it |
| `state` | `"open"` \| `"active"` \| `"done"` \| `"blocked"` \| `"dropped"` | default `"open"` | where it is |
| `gate` | boolean | default `false` | the user must approve this step before it starts |
| `needs` | list of reference |  | decisions, steps or phases that come first |
| `details` | prose (Markdown) |  | how, exactly |
| `done_when` | string |  | how to tell it is done |
| `commit` | string |  | the commit that did it |
| `reason` | string |  | required when blocked or dropped |

#### `[scope]`

| key | type | required | meaning |
|---|---|---|---|
| `in` | list of string |  | what the plan does |
| `out` | list of string |  | what it deliberately does not |

#### `[[section]]`

| key | type | required | meaning |
|---|---|---|---|
| `id` | string |  | local id, so the section can be commented on |
| `title` | string | yes | the heading |
| `body` | prose (Markdown) | yes | the text |

#### `[[risk]]`

| key | type | required | meaning |
|---|---|---|---|
| `what` | string | yes | what could go wrong |
| `mitigation` | string |  | what keeps it small |

#### `[[log]]`

| key | type | required | meaning |
|---|---|---|---|
| `on` | date | yes | the day |
| `text` | string | yes | what happened |
| `step` | reference |  | the step it is about |
| `commit` | string |  | the commit |

#### `[[finding]]`

| key | type | required | meaning |
|---|---|---|---|
| `id` | string | yes | local id |
| `title` | string | yes | the finding in one line |
| `severity` | `"critical"` \| `"high"` \| `"medium"` \| `"low"` \| `"info"` | yes | how bad |
| `area` | string |  | where: a host, a path, a component |
| `details` | prose (Markdown) |  | what exactly |
| `evidence` | prose (Markdown) |  | how it was seen: a command and its output, a file and line |
| `state` | `"open"` \| `"confirmed"` \| `"rejected"` \| `"fixed"` \| `"moved"` | default `"open"` | where its triage is |
| `to` | reference |  | the plan (or plan/step) that took it; required when moved |
| `reason` | string |  | why it was rejected |
<!-- reference:end -->
