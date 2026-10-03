"""The kinds of item a planning folder holds, and the one strict loader for them.

Everything else that knows the format is derived from the declarations here:
`planning check` validates with `load`, `planning schema` writes
`json_schema()`, and the reference tables in GUIDE.md are `reference()`. A test
fails when the guide and these declarations disagree, so there is one place to
change the format and nowhere for a second copy to drift.

The loader is strict for the same reason butler.toml's is: a typo'd key that
silently does nothing is the failure a config-first format invites.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

FORMAT = 1

KINDS = ("plan", "decision", "review", "note")
LETTER = {"plan": "p", "decision": "d", "review": "r", "note": "n"}
KIND_OF_LETTER = {v: k for k, v in LETTER.items()}

STATE_FOLDERS = ("draft", "todo", "in-progress", "done")
NOTES_FOLDER = "notes"
ANSWERS_FOLDER = "answers"
SCHEMA_FOLDER = ".schema"
CONFIG_FILE = "planning.toml"

# Crockford's base32 in lowercase: no i, l, o or u, so an id read aloud or
# typed from a screenshot cannot be mistaken for another.
ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
ID_RE = re.compile(r"^([pdrn])-([0-9a-hjkmnp-tv-z]{6})$")
LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

OWNERS = ("agent", "user")
STEP_STATES = ("open", "active", "done", "blocked", "dropped")
SEVERITIES = ("critical", "high", "medium", "low", "info")
FINDING_STATES = ("open", "confirmed", "rejected", "fixed", "moved")

# Fields of a step that change as work happens. An approval covers the rest,
# so ticking a step never makes its approval stale, and rewording it always does.
STEP_MUTABLE = ("state", "commit", "reason")


def new_id(kind: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A fresh id for `kind`, not in `taken`."""
    while True:
        cand = LETTER[kind] + "-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(6))
        if cand not in taken:
            return cand


# --------------------------------------------------------------------------- #
# declarations
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class T:
    """A field type.

    `name` is one of: str, prose, bool, date, ref, strs (a string or a list of
    strings), enum, list (of `of`), table (one `spec`), tables (an array of
    `spec`).
    """
    name: str
    of: "T | None" = None
    values: tuple[str, ...] = ()
    spec: "Spec | None" = None


STR, PROSE, BOOL, DATE, REF = T("str"), T("prose"), T("bool"), T("date"), T("ref")
STRS = T("strs")


def enum(*values: str) -> T:
    return T("enum", values=values)


def list_of(t: T) -> T:
    return T("list", of=t)


def table(spec: "Spec") -> T:
    return T("table", spec=spec)


def tables(spec: "Spec") -> T:
    return T("tables", spec=spec)


@dataclass(frozen=True)
class F:
    type: T
    doc: str
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class Spec:
    name: str
    fields: dict[str, F]


OPTION = Spec("option", {
    "id": F(STR, "local id of the option", True),
    "label": F(STR, "what the user reads and picks", True),
    "hint": F(STR, "one line under the label: the consequence of picking it"),
})

_DECISION_BODY = {
    "question": F(STR, "one question, ending in a question mark", True),
    "context": F(PROSE, "what the user needs to know to answer"),
    "as_of": F(DATE, "when the facts in `context` were checked"),
    "multiple": F(BOOL, "more than one option may be chosen", default=False),
    "option": F(tables(OPTION), "two to four options (a resolved decision may keep more)", True),
    "recommend": F(STRS, "the recommended option id (a list if `multiple`)"),
    "because": F(STR, "why that option; required with `recommend`"),
    "own_answer": F(BOOL, "the user may write their own answer", default=True),
    "resolved": F(list_of(STR), "option ids, or [\"own\"]: set by the agent once it has acted on the answer; "
                  "a decision without it is open"),
    "outcome": F(STR, "required with `resolved`: what was decided, by whom, and what changed"),
    "resolved_on": F(DATE, "the day it was resolved"),
}

DECISION = Spec("decision", {"id": F(STR, "local id, never renamed or reused", True), **_DECISION_BODY})

STEP = Spec("step", {
    "id": F(STR, "local id, never renamed or reused", True),
    "title": F(STR, "one checkable outcome", True),
    "by": F(enum(*OWNERS), "who does it", True),
    "state": F(enum(*STEP_STATES), "where it is", default="open"),
    "gate": F(BOOL, "the user must approve this step before it starts", default=False),
    "needs": F(list_of(REF), "decisions, steps or phases that come first"),
    "details": F(PROSE, "how, exactly"),
    "done_when": F(STR, "how to tell it is done"),
    "commit": F(STR, "the commit that did it"),
    "reason": F(STR, "required when blocked or dropped"),
})

PHASE = Spec("phase", {
    "id": F(STR, "local id", True),
    "title": F(STR, "what this stage of the work is", True),
    "goal": F(STR, "what is true when the phase is done"),
    "step": F(tables(STEP), "the steps, in order", True),
})

SECTION = Spec("section", {
    "id": F(STR, "local id, so the section can be commented on"),
    "title": F(STR, "the heading", True),
    "body": F(PROSE, "the text", True),
})

RISK = Spec("risk", {
    "what": F(STR, "what could go wrong", True),
    "mitigation": F(STR, "what keeps it small"),
})

LOG = Spec("log", {
    "on": F(DATE, "the day", True),
    "text": F(STR, "what happened", True),
    "step": F(REF, "the step it is about"),
    "commit": F(STR, "the commit"),
})

SCOPE = Spec("scope", {
    "in": F(list_of(STR), "what the plan does"),
    "out": F(list_of(STR), "what it deliberately does not"),
})

FINDING = Spec("finding", {
    "id": F(STR, "local id", True),
    "title": F(STR, "the finding in one line", True),
    "severity": F(enum(*SEVERITIES), "how bad", True),
    "area": F(STR, "where: a host, a path, a component"),
    "details": F(PROSE, "what exactly"),
    "evidence": F(PROSE, "how it was seen: a command and its output, a file and line"),
    "state": F(enum(*FINDING_STATES), "where its triage is", default="open"),
    "to": F(REF, "the plan (or plan/step) that took it; required when moved"),
    "reason": F(STR, "why it was rejected"),
})

COMMON = {
    "kind": F(enum(*KINDS), "what the file is; must match the id's letter", True),
    "id": F(STR, "`<letter>-<six base32>`, made by `planning new`; never changes", True),
    "title": F(STR, "the item's name", True),
    "summary": F(STR, "one or two sentences, shown on cards", True),
    "created": F(DATE, "the day it was written", True),
    "updated": F(DATE, "the day it last changed; not later than today", True),
    "context": F(list_of(REF), "items to read first, usually notes"),
    "related": F(list_of(REF), "items worth knowing about"),
    "tags": F(list_of(STR), "free words"),
}

KIND_SPECS: dict[str, Spec] = {
    "plan": Spec("plan", {
        **COMMON,
        "why": F(PROSE, "why the work is worth doing", True),
        "scope": F(table(SCOPE), "the `[scope]` table"),
        "acceptance": F(list_of(STR), "what done means for the whole plan"),
        "needs": F(list_of(REF), "plans that must be done first"),
        "touches": F(list_of(STR), "paths or areas it changes"),
        "section": F(tables(SECTION), "design notes that belong to this plan only"),
        "decision": F(tables(DECISION), "questions for the user"),
        "phase": F(tables(PHASE), "the stages of the work, each with its steps"),
        "risk": F(tables(RISK), "what could go wrong"),
        "log": F(tables(LOG), "progress, oldest first; the agent appends"),
    }),
    "decision": Spec("decision", {
        **COMMON,
        **{k: v for k, v in _DECISION_BODY.items()},
        "final": F(BOOL, "settled, and not to be reopened", default=False),
    }),
    "review": Spec("review", {
        **COMMON,
        "subject": F(PROSE, "what was reviewed, and at which commit", True),
        "method": F(PROSE, "how, and how far the findings were checked"),
        "finding": F(tables(FINDING), "the findings"),
    }),
    "note": Spec("note", {
        **COMMON,
        "section": F(tables(SECTION), "the text, in sections", True),
    }),
}

# The table a spec's name lives under when it is an array inside a kind: a
# standalone decision's options are `[[option]]`, a plan's `[[decision.option]]`.
CONFIG_SPEC = Spec("planning.toml", {
    "format": F(T("int"), "the format version: 1", True),
    "user": F(STR, "how the pages and `status` name the user (default: git's user.name)"),
    "plan_approval": F(BOOL, "a plan needs the user's approval to leave draft/", default=True),
})


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

@dataclass
class Problem:
    level: str            # "error", "warn" or "info"
    message: str
    where: str = ""       # a dotted path inside the item, or ""
    local: str = ""       # a local id to find the line by, or ""
    line: int | None = None

    def __str__(self) -> str:
        at = f" ({self.where})" if self.where else ""
        return f"{self.message}{at}"


@dataclass
class Loaded:
    data: dict
    problems: list[Problem] = field(default_factory=list)


def _is_date(v: Any) -> bool:
    return isinstance(v, _dt.date) and not isinstance(v, _dt.datetime)


def _check(t: T, v: Any, where: str, out: list[Problem], local: str) -> Any:
    def bad(msg: str) -> None:
        out.append(Problem("error", msg, where, local))

    n = t.name
    if n in ("str", "prose", "ref"):
        if not isinstance(v, str):
            bad(f"must be a string, got {type(v).__name__}")
    elif n == "int":
        if not isinstance(v, int) or isinstance(v, bool):
            bad(f"must be an integer, got {type(v).__name__}")
    elif n == "bool":
        if not isinstance(v, bool):
            bad(f"must be true or false, got {type(v).__name__}")
    elif n == "date":
        if not _is_date(v):
            bad(f"must be a date (2026-09-30, unquoted), got {type(v).__name__}")
    elif n == "strs":
        if isinstance(v, str):
            pass
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            pass
        else:
            bad("must be a string or a list of strings")
    elif n == "enum":
        if v not in t.values:
            bad(f"must be one of {', '.join(repr(x) for x in t.values)}, got {v!r}")
    elif n == "list":
        if not isinstance(v, list):
            bad(f"must be a list, got {type(v).__name__}")
        else:
            for i, x in enumerate(v):
                _check(t.of, x, f"{where}[{i}]", out, local)
    elif n == "table":
        if not isinstance(v, dict):
            bad(f"must be a table, got {type(v).__name__}")
        else:
            return _load_spec(t.spec, v, where, out, local)
    elif n == "tables":
        if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
            bad(f"must be an array of tables ([[{where.split('.')[-1]}]])")
        else:
            return [_load_spec(t.spec, x, f"{where}[{i}]", out,
                               x.get("id") if isinstance(x.get("id"), str) else local)
                    for i, x in enumerate(v)]
    return v


def _load_spec(spec: Spec, raw: dict, where: str, out: list[Problem], local: str = "") -> dict:
    data: dict = {}
    for key, value in raw.items():
        f = spec.fields.get(key)
        path = f"{where}.{key}" if where else key
        if f is None:
            known = ", ".join(spec.fields)
            out.append(Problem("error", f"unknown key '{key}' in {spec.name} (known: {known})",
                               path, local))
            continue
        data[key] = _check(f.type, value, path, out, local)
    for key, f in spec.fields.items():
        if key not in data:
            if f.required:
                path = f"{where}.{key}" if where else key
                out.append(Problem("error", f"{spec.name} is missing the required key '{key}'",
                                   path, local))
            elif f.default is not None:
                data[key] = f.default
    return data


def load(raw: dict) -> Loaded:
    """Validate one item's parsed TOML against its kind. Never raises."""
    out: list[Problem] = []
    kind = raw.get("kind")
    if kind not in KIND_SPECS:
        out.append(Problem("error", f"'kind' must be one of {', '.join(KINDS)}, got {kind!r}", "kind"))
        return Loaded(dict(raw), out)
    return Loaded(_load_spec(KIND_SPECS[kind], raw, "", out), out)


def load_config(raw: dict) -> Loaded:
    out: list[Problem] = []
    return Loaded(_load_spec(CONFIG_SPEC, raw, "", out), out)


# --------------------------------------------------------------------------- #
# canonical forms and hashes
# --------------------------------------------------------------------------- #

def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, (_dt.date, _dt.datetime, _dt.time)):
        return v.isoformat()
    return v


def jsonable(v: Any) -> Any:
    """TOML's dates as ISO strings, so an item can go out as JSON."""
    return _jsonable(v)


def _digest(obj: Any) -> str:
    text = json.dumps(_jsonable(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def step_snapshot(step: dict) -> dict:
    """What an approval of `step` covers: every field but the mutable ones.

    Defaults are filled in, so writing `gate = true` out where it was implied
    (or leaving `state` off) changes nothing.
    """
    full = {k: f.default for k, f in STEP.fields.items() if f.default is not None}
    full.update(step)
    return {k: v for k, v in sorted(full.items()) if k not in STEP_MUTABLE}


def step_hash(step: dict) -> str:
    return _digest(step_snapshot(step))


def plan_snapshot(plan: dict) -> list:
    """What a plan approval covers: its phases and steps, without their states."""
    return [{"id": ph.get("id"), "title": ph.get("title"), "goal": ph.get("goal"),
             "step": [step_snapshot(s) for s in ph.get("step", [])]}
            for ph in plan.get("phase", [])]


def plan_hash(plan: dict) -> str:
    return _digest(plan_snapshot(plan))


# --------------------------------------------------------------------------- #
# derived: JSON Schema, and the guide's reference tables
# --------------------------------------------------------------------------- #

def _schema_of(t: T) -> dict:
    n = t.name
    if n in ("str", "prose", "ref"):
        return {"type": "string"}
    if n == "int":
        return {"type": "integer"}
    if n == "bool":
        return {"type": "boolean"}
    if n == "date":
        return {"type": "string", "format": "date"}
    if n == "strs":
        return {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
    if n == "enum":
        return {"enum": list(t.values)}
    if n == "list":
        return {"type": "array", "items": _schema_of(t.of)}
    if n == "table":
        return _spec_schema(t.spec)
    if n == "tables":
        return {"type": "array", "items": _spec_schema(t.spec)}
    raise ValueError(n)


def _spec_schema(spec: Spec) -> dict:
    props = {}
    for k, f in spec.fields.items():
        s = _schema_of(f.type)
        s["description"] = f.doc
        if f.default is not None:
            s["default"] = f.default
        props[k] = s
    out = {"type": "object", "properties": props, "additionalProperties": False}
    req = [k for k, f in spec.fields.items() if f.required]
    if req:
        out["required"] = req
    return out


def json_schema() -> dict:
    """One schema for every kind, chosen by `kind`: what `planning schema` writes."""
    branches = []
    for kind, spec in KIND_SPECS.items():
        s = _spec_schema(spec)
        s["properties"]["kind"] = {"const": kind, "description": spec.fields["kind"].doc}
        s["properties"]["id"] = {"type": "string", "pattern": f"^{LETTER[kind]}-[0-9a-hjkmnp-tv-z]{{6}}$",
                                 "description": spec.fields["id"].doc}
        branches.append(s)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"butler planning item, format {FORMAT}",
        "description": "Written by `python butler.py planning schema`; generated from butler's planning/types.py.",
        "oneOf": branches,
    }


def _type_doc(t: T) -> str:
    n = t.name
    if n == "enum":
        return " \\| ".join(f"`\"{v}\"`" for v in t.values)
    if n == "list":
        return f"list of {_type_doc(t.of)}"
    if n == "strs":
        return "string or list of strings"
    if n == "ref":
        return "reference"
    if n in ("table", "tables"):
        return f"`[{'[' if n == 'tables' else ''}{t.spec.name}]{']' if n == 'tables' else ''}`"
    return {"str": "string", "prose": "prose (Markdown)", "bool": "boolean", "date": "date",
            "int": "integer"}[n]


def _spec_table(spec: Spec, heading: str) -> list[str]:
    lines = [f"#### {heading}", "", "| key | type | required | meaning |", "|---|---|---|---|"]
    for k, f in spec.fields.items():
        req = "yes" if f.required else (f"default `{json.dumps(f.default)}`" if f.default is not None else "")
        lines.append(f"| `{k}` | {_type_doc(f.type)} | {req} | {f.doc} |")
    return lines + [""]


def reference() -> str:
    """The guide's reference section, generated from the declarations above."""
    parts = ["<!-- generated from butler/planning/types.py by `planning.types.reference()`; "
             "do not edit by hand -->", ""]
    parts += _spec_table(CONFIG_SPEC, "`planning.toml`")
    parts += _spec_table(Spec("every item", COMMON), "Keys every item has")
    for kind, spec in KIND_SPECS.items():
        # A kind's own keys, and any common key it redefines (a standalone
        # decision's `context` is prose, not a list of references).
        own = Spec(kind, {k: v for k, v in spec.fields.items() if COMMON.get(k) != v})
        parts += _spec_table(own, f"`kind = \"{kind}\"`")
    for sub, heading in ((DECISION, "`[[decision]]` in a plan"), (OPTION, "`[[…option]]`"),
                         (PHASE, "`[[phase]]`"), (STEP, "`[[phase.step]]`"), (SCOPE, "`[scope]`"),
                         (SECTION, "`[[section]]`"), (RISK, "`[[risk]]`"), (LOG, "`[[log]]`"),
                         (FINDING, "`[[finding]]`")):
        parts += _spec_table(sub, heading)
    return "\n".join(parts).rstrip() + "\n"
