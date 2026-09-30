"""What an item's files mean together: open, answered, approved, stale, waiting.

`check`, `status`, `list`, `gate` and the service all read state through here,
so "is this approval current?" has one answer everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import answers as answers_mod
from .folder import Item
from .types import ID_RE, plan_hash, plan_snapshot, step_hash, step_snapshot

DONE_STEP = ("done", "dropped")


# ---- dependencies ------------------------------------------------------------- #

def satisfied(ref: str, item: Item, ids: dict | None = None) -> bool:
    """Is what `ref` names finished: a step done, a phase's steps all done or
    dropped, a decision resolved, a plan in done/? Without `ids` (the folder's
    items by id), a reference into another item counts as finished: it cannot
    be looked at, and should not hold anything up on a page."""
    if "/" in ref or ID_RE.match(ref):
        if ids is None:
            return True
        if "/" not in ref:
            target = ids.get(ref)
            return target is not None and target.folder == "done"
        head, local = ref.split("/", 1)
        target_item = ids.get(head)
    else:
        target_item, local = item, ref
    if target_item is None:
        return False
    kind = target_item.locals().get(local)
    node = target_item.find(local)
    if node is None:
        return False
    if kind == "step":
        return node.get("state") == "done"
    if kind == "phase":
        return all(s.get("state") in DONE_STEP for s in node.get("step", []))
    if kind == "decision":
        return bool(node.get("resolved"))
    return True


def waiting_on(step: dict, item: Item, ids: dict | None = None) -> list[str]:
    """What in the step's `needs` is not finished yet."""
    return [r for r in step.get("needs", []) or [] if isinstance(r, str) and not satisfied(r, item, ids)]


# ---- decisions ---------------------------------------------------------------- #

def decision_state(dec: dict, ans: dict, local: str) -> str:
    """resolved | answered | open."""
    if dec.get("resolved"):
        return "resolved"
    if answers_mod.answered(ans.get("decisions", {}).get(local)):
        return "answered"
    return "open"


def decision_local(item: Item, dec: dict) -> str:
    return item.id if item.kind == "decision" else str(dec.get("id", ""))


# ---- approvals ---------------------------------------------------------------- #

@dataclass
class Approval:
    status: str                 # none | approved | disapproved | stale
    verdict: str = ""           # what the user said, even when stale
    at: str = ""
    note: str = ""
    sha256: str = ""
    current: str = ""           # the hash of the step as it is now
    snapshot: object = None     # what was approved
    now: object = None          # what it is now

    @property
    def ok(self) -> bool:
        return self.status == "approved"

    def line(self, user: str) -> str:
        if self.status == "approved":
            return (f"approved in the page by {user} at {self.at}, "
                    f"unchanged since (sha256 {self.current[:12]}…)")
        if self.status == "disapproved":
            note = f": {self.note}" if self.note else ""
            return f"disapproved by {user} at {self.at}{note}"
        if self.status == "stale":
            return (f"{self.verdict} by {user} at {self.at}, but changed since "
                    f"(answered on sha256 {self.sha256[:12]}…, now {self.current[:12]}…): ask again")
        return f"not approved yet: {user} has not answered"


def _approval(entry: dict | None, current: str, now: object) -> Approval:
    if not entry or entry.get("verdict") not in ("approved", "disapproved"):
        return Approval("none", current=current, now=now)
    a = Approval(status=entry["verdict"], verdict=entry["verdict"], at=str(entry.get("at", "")),
                 note=str(entry.get("note") or ""), sha256=str(entry.get("sha256", "")),
                 current=current, snapshot=entry.get("snapshot"), now=now)
    if a.sha256 != current:
        a.status = "stale"
    return a


def step_approval(step: dict, ans: dict) -> Approval:
    return _approval(ans.get("approvals", {}).get(step.get("id")), step_hash(step), step_snapshot(step))


def plan_approval(item: Item, ans: dict) -> Approval:
    return _approval(ans.get("plan_approval"), plan_hash(item.data), plan_snapshot(item.data))


# ---- summaries ---------------------------------------------------------------- #

@dataclass
class Summary:
    item: Item
    answers: dict
    steps_total: int = 0
    steps_done: int = 0
    by_owner: dict = field(default_factory=dict)        # owner -> [done, total]
    for_user: list = field(default_factory=list)       # (what, local, text)
    for_agent: list = field(default_factory=list)
    next_step: dict | None = None
    open_steps: list = field(default_factory=list)     # every step not done or dropped

    def as_json(self) -> dict:
        it = self.item
        return {"id": it.id, "kind": it.kind, "title": it.title, "folder": it.folder, "file": it.rel,
                "summary": it.data.get("summary", ""),
                "steps": {"done": self.steps_done, "total": self.steps_total, "by": self.by_owner},
                "for_user": [dict(zip(("what", "local", "text"), x)) for x in self.for_user],
                "for_agent": [dict(zip(("what", "local", "text"), x)) for x in self.for_agent],
                "next": self.next_step.get("id") if self.next_step else None,
                "open": self.open_steps}


def plan_approval_due(item: Item, ans: dict, required: bool) -> bool:
    """Does a draft plan wait for the user's approval of its phases and steps?"""
    if not required or item.kind != "plan" or item.folder != "draft" or not item.phases():
        return False
    if any(not d.get("resolved") for d in item.decisions()):
        return False
    return not plan_approval(item, ans).ok


def summarize(item: Item, ans: dict, *, plan_approval_required: bool = True,
              ids: dict | None = None) -> Summary:
    s = Summary(item=item, answers=ans)
    for dec in item.decisions():
        local = decision_local(item, dec)
        st = decision_state(dec, ans, local)
        if st == "open":
            s.for_user.append(("decision", local, dec.get("question", "")))
        elif st == "answered":
            s.for_agent.append(("answer", local, dec.get("question", "")))

    ticks = ans.get("steps", {})
    for step in item.steps():
        sid, owner, state = step.get("id", ""), step.get("by", "agent"), step.get("state", "open")
        done_pair = s.by_owner.setdefault(owner, [0, 0])
        s.steps_total += 1
        done_pair[1] += 1
        if state in DONE_STEP:
            s.steps_done += 1
            done_pair[0] += 1
        elif s.next_step is None and state in ("open", "active"):
            s.next_step = step
        ticked = ticks.get(sid, {}).get("state") == "done"
        if owner == "user" and ticked and state != "done":
            s.for_agent.append(("tick", sid, f"{step.get('title', '')}: done in the page, not yet in the file"))
        if state not in DONE_STEP:
            waits = waiting_on(step, item, ids)
            approval = step_approval(step, ans).status if step.get("gate") else None
            s.open_steps.append({"id": sid, "title": step.get("title", ""), "by": owner, "state": state,
                                 "gate": bool(step.get("gate")), "approval": approval,
                                 "ready": not waits, "waiting_on": waits,
                                 "ticked": owner == "user" and ticked})
            # The user's own steps that can be done now are theirs to do.
            if owner == "user" and not waits and not ticked:
                s.for_user.append(("step", sid, step.get("title", "")))
        if step.get("gate") and state not in DONE_STEP:
            a = step_approval(step, ans)
            if a.status == "none":
                s.for_user.append(("approval", sid, step.get("title", "")))
            elif a.status == "stale":
                s.for_user.append(("approval", sid, f"{step.get('title', '')} (changed since your "
                                                    f"{a.verdict} answer)"))
            elif a.status == "disapproved":
                s.for_agent.append(("disapproved", sid, f"{step.get('title', '')}: drop it or rewrite it"
                                    + (f"; note: {a.note}" if a.note else "")))

    if plan_approval_due(item, ans, plan_approval_required):
        pa = plan_approval(item, ans)
        text = "the plan's phases and steps" + (" (changed since your answer)" if pa.status == "stale" else "")
        s.for_user.append(("plan-approval", "", text))
    elif item.kind == "plan" and item.folder != "draft" and plan_approval_required:
        pa = plan_approval(item, ans)
        if pa.status == "stale":
            s.for_user.append(("plan-approval", "", "the steps changed since you approved the plan; "
                                                    "look again (the agent is not blocked)"))

    for c in ans.get("comments", []):
        if not c.get("resolved"):
            s.for_agent.append(("comment", str(c.get("id", "")),
                                (f"on {c['on']}: " if c.get("on") else "") + str(c.get("text", ""))))

    if item.kind == "review":
        for f in item.findings():
            if f.get("state", "open") == "open":
                s.for_agent.append(("finding", f.get("id", ""), f.get("title", "")))
    return s
