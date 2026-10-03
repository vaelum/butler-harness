"""`planning check`: everything that can be wrong with a planning folder.

Six groups of rules, in the order the plan (and the guide) lists them:

1. layout        — planning.toml, and nothing in the folder that does not belong
2. each file     — TOML, the kind's keys and types, dates
3. ids           — well formed, unique, unchanged since HEAD
4. references    — every reference resolves, and `needs` has no cycle
5. folder rules  — an item's content fits the folder it is in
6. answers       — the user's answers and approvals agree with the files

It reads files only: no service, so CI can run it.
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
from dataclasses import dataclass, field

from .folder import Folder, Item
from .state import (DONE_STEP, decision_local, decision_state, plan_approval, satisfied,
                    step_approval, summarize)
from .types import FORMAT, ID_RE, KIND_OF_LETTER, LOCAL_RE, NOTES_FOLDER, STATE_FOLDERS, Problem

# kind -> folders it may be in
ALLOWED = {
    "plan": ("draft", "todo", "in-progress", "done"),
    "decision": ("draft", "done"),
    "review": ("draft", "in-progress", "done"),
    "note": (NOTES_FOLDER,),
}


@dataclass
class Finding:
    file: str
    level: str
    message: str
    line: int | None = None

    def as_json(self) -> dict:
        return {"file": self.file, "level": self.level, "message": self.message, "line": self.line}


@dataclass
class Report:
    folder: Folder
    findings: list[Finding] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    waiting_user: int = 0
    waiting_agent: int = 0

    def add(self, file: str, level: str, message: str, line: int | None = None) -> None:
        self.findings.append(Finding(file, level, message, line))

    def of(self, level: str) -> list[Finding]:
        return [f for f in self.findings if f.level == level]

    @property
    def errors(self) -> int:
        return len(self.of("error"))

    @property
    def warnings(self) -> int:
        return len(self.of("warn"))

    def closing(self) -> str:
        parts = " · ".join(f"{k} {self.counts.get(k, 0)}" for k in (*STATE_FOLDERS, NOTES_FOLDER))
        user = self.folder.user
        tail = []
        if self.waiting_user:
            tail.append(f"{self.waiting_user} wait on {user}")
        if self.waiting_agent:
            tail.append(f"{self.waiting_agent} wait on the agent")
        return parts + (" — " + ", ".join(tail) if tail else "")

    def as_json(self) -> dict:
        return {"errors": self.errors, "warnings": self.warnings,
                "findings": [f.as_json() for f in self.findings], "counts": self.counts,
                "waiting": {"user": self.waiting_user, "agent": self.waiting_agent},
                "summary": self.closing()}


def _add(report: Report, item: Item, p: Problem) -> None:
    report.add(item.rel, p.level, str(p), item.line_of(p))


# --------------------------------------------------------------------------- #
# references
# --------------------------------------------------------------------------- #

def _resolve(ref: str, item: Item, ids: dict[str, Item]) -> bool:
    if "/" in ref:
        head, local = ref.split("/", 1)
        other = ids.get(head)
        return other is not None and local in other.locals()
    if ID_RE.match(ref):
        return ref in ids
    return ref in item.locals()


def _refs(item: Item) -> list[tuple[str, str, str]]:
    """(field, ref, local) for every reference in the item."""
    out = []
    for key in ("context", "related", "needs"):
        value = item.data.get(key)
        # A standalone decision's `context` is its prose, not a list of references.
        for r in value if isinstance(value, list) else []:
            if isinstance(r, str):
                out.append((key, r, ""))
    for s in item.steps():
        for r in s.get("needs", []) or []:
            if isinstance(r, str):
                out.append(("needs", r, s.get("id", "")))
    for entry in item.data.get("log", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("step"), str):
            out.append(("log.step", entry["step"], ""))
    for f in item.findings():
        if isinstance(f.get("to"), str):
            out.append(("to", f["to"], f.get("id", "")))
    return out


def _cycle(graph: dict[str, list[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = GREY
        stack.append(n)
        for m in graph.get(n, []):
            if m not in color:
                continue
            if color[m] == GREY:
                return stack[stack.index(m):] + [m]
            if color[m] == WHITE:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        color[n] = BLACK
        return None

    for n in list(graph):
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def _step_graph(item: Item) -> dict[str, list[str]]:
    phases = {p.get("id"): [s.get("id") for s in p.get("step", [])] for p in item.phases()}
    graph: dict[str, list[str]] = {}
    for s in item.steps():
        deps = []
        for r in s.get("needs", []) or []:
            if not isinstance(r, str) or "/" in r or ID_RE.match(r):
                continue
            deps.extend(phases.get(r, [r]))
        graph[s.get("id")] = deps
    return graph


# --------------------------------------------------------------------------- #
# HEAD
# --------------------------------------------------------------------------- #

_ID_LINE = re.compile(r'^id\s*=\s*"([^"]*)"', re.M)


def _ids_at_head(folder: Folder) -> dict[str, str] | None:
    """relative path (inside the planning folder) -> id, as committed at HEAD."""
    try:
        rel = folder.root.relative_to(folder.repo).as_posix()
    except ValueError:
        return None
    try:
        names = subprocess.run(["git", "-C", str(folder.repo), "ls-tree", "-r", "--name-only", "HEAD",
                                "--", rel], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if names.returncode != 0:
        return None
    out = {}
    for name in names.stdout.splitlines():
        if not name.endswith(".toml") or name.endswith("/planning.toml") or name == "planning.toml":
            continue
        shown = subprocess.run(["git", "-C", str(folder.repo), "show", f"HEAD:{name}"],
                               capture_output=True, text=True, timeout=10)
        if shown.returncode != 0:
            continue
        m = _ID_LINE.search(shown.stdout)
        if m:
            out[name[len(rel) + 1:] if rel != "." else name] = m.group(1)
    return out


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #

def folder_rules(item: Item, folder_name: str, ans: dict, *, plan_approval_required: bool) -> list[str]:
    """Why `item` may not be in `folder_name` (empty: it may). Also what
    `planning move` asks before it moves anything."""
    why: list[str] = []
    kind = item.kind
    if kind in ALLOWED and folder_name not in ALLOWED[kind]:
        why.append(f"a {kind} belongs in {', '.join(ALLOWED[kind])}, not {folder_name}/")
        return why
    open_decisions = [decision_local(item, d) for d in item.decisions() if not d.get("resolved")]
    if kind == "plan":
        if folder_name == "todo":
            if open_decisions:
                why.append(f"it has open decisions: {', '.join(open_decisions)}")
            if plan_approval_required and plan_approval(item, ans).status not in ("approved", "stale"):
                why.append("the plan has not been approved (planning.toml: plan_approval = true)")
        elif folder_name == "in-progress":
            waiting = set()
            for s in item.steps():
                waiting.update(r for r in s.get("needs", []) or [] if isinstance(r, str))
            loose = [d for d in open_decisions if d not in waiting]
            if loose:
                why.append(f"open decisions that no step waits on: {', '.join(loose)}")
        elif folder_name == "done":
            unfinished = [s.get("id") for s in item.steps() if s.get("state", "open") not in DONE_STEP]
            if unfinished:
                why.append(f"steps not done or dropped: {', '.join(map(str, unfinished))}")
            if open_decisions:
                why.append(f"decisions not resolved: {', '.join(open_decisions)}")
    elif kind == "decision":
        if folder_name == "draft" and item.data.get("resolved"):
            why.append("it is resolved; a resolved decision is in done/")
        if folder_name == "done" and not item.data.get("resolved"):
            why.append("it is not resolved")
    elif kind == "review":
        if folder_name == "done":
            left = [f.get("id") for f in item.findings() if f.get("state", "open") == "open"]
            if left:
                why.append(f"findings still open: {', '.join(map(str, left))}")
    return why


def _check_item_content(report: Report, item: Item, ids: dict[str, Item], ans: dict, today) -> None:
    rel = item.rel
    data = item.data

    for key in ("created", "updated"):
        v = data.get(key)
        if isinstance(v, _dt.date) and v > today:
            report.add(rel, "error", f"{key} = {v} is later than today ({today})",
                       item.line_of(Problem("error", "", key)))
    if isinstance(data.get("created"), _dt.date) and isinstance(data.get("updated"), _dt.date) \
            and data["updated"] < data["created"]:
        report.add(rel, "warn", "updated is earlier than created")

    # ---- ids ----
    iid = item.id
    m = ID_RE.match(iid) if iid else None
    if iid and not m:
        report.add(rel, "error", f"id {iid!r} is not <letter>-<six base32 characters> "
                                 "(use `planning new` to make one)", 1)
    elif m and item.kind and KIND_OF_LETTER[m.group(1)] != item.kind:
        report.add(rel, "error", f"id {iid!r} is a {KIND_OF_LETTER[m.group(1)]}'s id, but kind = {item.kind!r}")

    seen: dict[str, str] = {}
    groups = [("decision", item.decisions() if item.kind != "decision" else []),
              ("phase", item.phases()), ("step", item.steps()), ("finding", item.findings()),
              ("section", [s for s in item.sections() if "id" in s])]
    for what, group in groups:
        for x in group:
            lid = x.get("id")
            if not isinstance(lid, str):
                continue
            if not LOCAL_RE.match(lid):
                report.add(rel, "error", f"{what} id {lid!r} must be lowercase letters, digits and dashes",
                           item.line_of(Problem("error", "", local=lid)))
            if lid in seen:
                report.add(rel, "error", f"local id {lid!r} is used by a {seen[lid]} and a {what}",
                           item.line_of(Problem("error", "", local=lid)))
            seen[lid] = what

    # ---- references ----
    for key, ref, local in _refs(item):
        if not _resolve(ref, item, ids):
            report.add(rel, "error", f"{key} names {ref!r}, which does not exist",
                       item.line_of(Problem("error", "", key, local)))

    cyc = _cycle(_step_graph(item))
    if cyc:
        report.add(rel, "error", "steps wait on each other in a circle: " + " → ".join(cyc))

    # ---- decisions ----
    for dec in item.decisions():
        local = decision_local(item, dec)
        line = item.line_of(Problem("error", "", local=local)) if item.kind != "decision" else None
        opts = [o for o in dec.get("option", []) if isinstance(o, dict)]
        oids = [o.get("id") for o in opts]
        # The limit keeps an open question answerable. A resolved one is only
        # read, and may keep every option it was answered with (an old form's).
        if len(opts) < 2 or (len(opts) > 4 and not dec.get("resolved")):
            report.add(rel, "error", f"decision {local}: has {len(opts)} options; give two to four", line)
        if len(set(oids)) != len(oids):
            report.add(rel, "error", f"decision {local}: two options share an id", line)
        rec = dec.get("recommend")
        recs = rec if isinstance(rec, list) else ([rec] if rec else [])
        for r in recs:
            if r not in oids:
                report.add(rel, "error", f"decision {local}: recommends {r!r}, which is not an option", line)
        if isinstance(rec, list) and not dec.get("multiple"):
            report.add(rel, "error", f"decision {local}: recommends several options but multiple is false", line)
        if recs and not dec.get("because"):
            report.add(rel, "error", f"decision {local}: recommend needs a because", line)
        res = dec.get("resolved") or []
        for r in res:
            if r != "own" and r not in oids:
                report.add(rel, "error", f"decision {local}: resolved names {r!r}, which is not an option "
                                         "(or \"own\")", line)
        if res and not dec.get("outcome"):
            report.add(rel, "error", f"decision {local}: resolved needs an outcome", line)
        if not res and (dec.get("outcome") or dec.get("resolved_on")):
            report.add(rel, "warn", f"decision {local}: has an outcome but no resolved", line)
        if len([r for r in res if r != "own"]) > 1 and not dec.get("multiple"):
            report.add(rel, "error", f"decision {local}: resolved to several options but multiple is false",
                       line)

    # ---- phases and steps ----
    for ph in item.phases():
        if not ph.get("step"):
            report.add(rel, "warn", f"phase {ph.get('id')} has no steps",
                       item.line_of(Problem("warn", "", local=str(ph.get("id")))))
    for s in item.steps():
        sid = str(s.get("id"))
        line = item.line_of(Problem("error", "", local=sid))
        state = s.get("state", "open")
        if state in ("blocked", "dropped") and not s.get("reason"):
            report.add(rel, "error", f"step {sid} is {state} but says no reason", line)
        if state in ("active", "done"):
            unmet = [r for r in s.get("needs", []) or [] if isinstance(r, str)
                     and _resolve(r, item, ids) and not satisfied(r, item, ids)]
            if unmet:
                report.add(rel, "error", f"step {sid} is {state}, but what it needs is not: "
                                         f"{', '.join(unmet)}", line)

    for f in item.findings():
        fid = str(f.get("id"))
        line = item.line_of(Problem("error", "", local=fid))
        if f.get("state") == "moved" and not f.get("to"):
            report.add(rel, "error", f"finding {fid} is moved but says not where (to)", line)
        if f.get("state") == "rejected" and not f.get("reason"):
            report.add(rel, "warn", f"finding {fid} is rejected but says no reason", line)


def _check_answers(report: Report, item: Item, ans: dict, user: str) -> None:
    rel = f"answers/{item.id}.json"
    if ans.get("_error"):
        report.add(rel, "error", f"cannot read: {ans['_error']}")
        return
    locals_ = item.locals()
    if item.kind == "decision":
        locals_[item.id] = "decision"
    for key in ("decisions", "steps", "approvals"):
        for local in (ans.get(key) or {}):
            if local not in locals_:
                report.add(rel, "warn", f"{key[:-1]} {local!r} is answered, but {item.rel} has no such id")

    for dec in item.decisions():
        local = decision_local(item, dec)
        if decision_state(dec, ans, local) == "answered":
            report.add(item.rel, "info", f"decision {local} is answered; waiting on the agent to act on it")

    ticks = ans.get("steps") or {}
    for s in item.steps():
        sid = str(s.get("id"))
        state = s.get("state", "open")
        line = item.line_of(Problem("error", "", local=sid))
        if s.get("by") == "user" and ticks.get(sid, {}).get("state") == "done" and state != "done":
            report.add(item.rel, "info", f"step {sid} is done in the page, not yet in the file", line)
        if not s.get("gate"):
            continue
        a = step_approval(s, ans)
        if state in ("active", "done") and not a.ok:
            report.add(item.rel, "error",
                       f"step {sid} is gated and {state}, but has no current approval: {a.line(user)}", line)
        elif a.status == "disapproved" and state != "dropped":
            report.add(item.rel, "error", f"step {sid} was disapproved; drop it or rewrite it"
                       + (f" (note: {a.note})" if a.note else ""), line)
        elif a.status == "stale" and state not in DONE_STEP:
            report.add(item.rel, "warn", f"step {sid}: {a.line(user)}", line)

    if item.kind == "plan" and ans.get("plan_approval"):
        pa = plan_approval(item, ans)
        if pa.status == "stale":
            report.add(item.rel, "info", "the phases and steps changed since the plan was approved")

    for c in ans.get("comments", []) or []:
        if not c.get("resolved"):
            report.add(item.rel, "info", f"comment {c.get('id')} is open: {str(c.get('text', ''))[:80]}")


def run(folder: Folder, *, today: _dt.date | None = None, git: bool = True) -> Report:
    today = today or _dt.date.today()
    report = Report(folder=folder)

    # 1. layout
    if not folder.exists:
        report.add("", "error", f"no planning folder at {folder.root}")
        return report
    if not folder.typed:
        report.add("planning.toml", "error", "missing: this folder is not a typed planning folder "
                                             "(`python butler.py planning init` makes one)")
    else:
        for p in folder.config_problems:
            report.add("planning.toml", p.level, str(p))
        if folder.config.get("format") not in (None, FORMAT) and not folder.config_problems:
            report.add("planning.toml", "error", f"format {folder.config.get('format')} is not one this "
                                                 f"harness knows (it knows {FORMAT})")
    for rel in folder.legacy:
        report.add(rel, "warn", "a Markdown plan (shown read-only); `planning convert` makes a typed skeleton")
    for rel in folder.forms:
        report.add(rel, "warn", "a file of the forms prototype (shown read-only); "
                                "`planning convert` reads a form and its answers")
    for rel in folder.stray:
        report.add(rel, "warn", "does not belong in a planning folder")

    for k in (*STATE_FOLDERS, NOTES_FOLDER):
        report.counts[k] = 0

    # 2. each file
    ids: dict[str, Item] = {}
    for it in folder.items:
        report.counts[it.folder] = report.counts.get(it.folder, 0) + 1
        for p in it.problems:
            _add(report, it, p)
        if not it.parsed:
            continue
        if it.id:
            if it.id in ids:
                report.add(it.rel, "error", f"id {it.id} is also the id of {ids[it.id].rel} "
                                            "(a copied file? give one of them a new id)", 1)
            else:
                ids[it.id] = it

    # 3. ids against HEAD
    if git:
        head = _ids_at_head(folder)
        for it in folder.items:
            before = (head or {}).get(it.rel)
            if before and it.id and before != it.id:
                report.add(it.rel, "error", f"id changed from {before} (at HEAD) to {it.id}; "
                                            "an id never changes", 1)

    # 4. to 6.
    plan_needs: dict[str, list[str]] = {}
    for it in folder.items:
        if not it.parsed or not it.kind:
            continue
        ans = folder.answers(it.id) if it.id else {}
        _check_item_content(report, it, ids, ans, today)
        for why in folder_rules(it, it.folder, ans, plan_approval_required=folder.plan_approval):
            report.add(it.rel, "error", f"in {it.folder}/, but {why}")
        if it.id:
            _check_answers(report, it, ans, folder.user)
            s = summarize(it, ans, plan_approval_required=folder.plan_approval, ids=ids)
            report.waiting_user += len(s.for_user)
            report.waiting_agent += len(s.for_agent)
        if it.kind == "plan" and it.id:
            plan_needs[it.id] = [r for r in it.data.get("needs", []) or [] if isinstance(r, str)
                                 and ID_RE.match(r)]

    cyc = _cycle(plan_needs)
    if cyc:
        report.add("", "error", "plans need each other in a circle: " + " → ".join(cyc))

    for iid, path in folder.answer_files.items():
        if iid not in ids:
            report.add(f"answers/{path.name}", "error", f"answers for {iid}, but no item has that id")

    order = {"error": 0, "warn": 1, "info": 2}
    report.findings.sort(key=lambda f: (f.file, order.get(f.level, 3), f.line or 0))
    return report
