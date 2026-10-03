"""`planning convert`: a typed skeleton from a file of the old kind.

Two sources:

- a Markdown plan: the first `#` heading is the title, the text before the
  next heading is `why`, each `##` section with checkboxes becomes a phase of
  steps (`[x]` done), and every other section a `[[section]]`;
- one of the forms prototype's HTML forms: each card's questions become
  decisions with the form's own question and option ids, cards without
  questions become agent steps, and `<form>.answers.json` beside it is carried
  into `answers/<id>.json`, each entry marked with its source and `saved_at`.

The result is a skeleton: it passes `planning check`'s schema, and an agent
reads it against the source before the source is deleted.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from . import answers as answers_mod
from .types import LOCAL_RE

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


# --------------------------------------------------------------------------- #
# TOML writing (the stdlib has no writer; these are the only shapes we need)
# --------------------------------------------------------------------------- #

def q(s: str) -> str:
    """A basic TOML string."""
    return json.dumps(str(s), ensure_ascii=False)


def prose(s: str) -> str:
    """A multi-line literal string, or a basic one if the text contains '''."""
    s = s.strip("\n")
    if "'''" in s:
        return q(s)
    return "'''\n" + s + "\n'''"


def arr(xs: list[str]) -> str:
    return "[" + ", ".join(q(x) for x in xs) + "]"


def _local(text: str, taken: set[str], prefix: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24].strip("-") or prefix
    if not LOCAL_RE.match(base):
        base = prefix
    cand, n = base, 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    taken.add(cand)
    return cand


@dataclass
class Draft:
    title: str
    summary: str = ""
    why: str = ""
    sections: list[tuple[str, str]] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    phases: list[dict] = field(default_factory=list)      # {id, title, steps: [dict]}
    answers: dict = field(default_factory=dict)            # local -> entry

    def render(self, item_id: str, today: _dt.date, source: str, schema: str | None) -> str:
        out = []
        if schema:
            out.append(f"#:schema {schema}")
        summary = self.summary or _first_sentence(self.why) or self.title
        out += ['kind = "plan"', f'id = "{item_id}"', f"title = {q(self.title)}",
                f"summary = {q(summary[:300])}", f"created = {today}", f"updated = {today}",
                f"why = {prose(self.why or 'Converted from ' + source + '; say why here.')}", ""]
        for title, body in self.sections:
            out += ["[[section]]", f"title = {q(title)}", f"body = {prose(body)}", ""]
        for d in self.decisions:
            out += ["[[decision]]", f'id = "{d["id"]}"', f"question = {q(d['question'])}"]
            if d.get("context"):
                out.append(f"context = {prose(d['context'])}")
            if d.get("multiple"):
                out.append("multiple = true")
            if d.get("resolved"):
                out += [f"resolved = {arr(d['resolved'])}", f"outcome = {q(d['outcome'])}",
                        f"resolved_on = {d['resolved_on']}"]
            out.append("")
            for o in d["options"]:
                out += ["  [[decision.option]]", f'  id = "{o["id"]}"', f"  label = {q(o['label'])}"]
                if o.get("hint"):
                    out.append(f"  hint = {q(o['hint'])}")
                out.append("")
        for ph in self.phases:
            out += ["[[phase]]", f'id = "{ph["id"]}"', f"title = {q(ph['title'])}", ""]
            for s in ph["steps"]:
                out += ["  [[phase.step]]", f'  id = "{s["id"]}"', f"  title = {q(s['title'])}",
                        f'  by = "{s["by"]}"']
                if s.get("state", "open") != "open":
                    out.append(f'  state = "{s["state"]}"')
                if s.get("reason"):
                    out.append(f"  reason = {q(s['reason'])}")
                if s.get("details"):
                    out.append(f"  details = {prose(s['details'])}")
                out.append("")
        out += ["[[log]]", f"on = {today}", f"text = {q('Converted from ' + source + ' by planning convert.')}", ""]
        return "\n".join(out)


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:300]


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #

_BOX = re.compile(r"^(\s*)[-*] \[([ xX~!-])\] (.*)$")


_STATUS = re.compile(r"^\W*status\b", re.I)


def _summary(why: str) -> str:
    """The first sentence of `why`, past a leading "Status: …" paragraph."""
    paras = [p for p in re.split(r"\n\s*\n", why) if p.strip()]
    while len(paras) > 1 and _STATUS.match(paras[0]):
        paras = paras[1:]
    return _first_sentence(paras[0]) if paras else ""


def _split_title(text: str) -> tuple[str, str]:
    """A plain bullet's first sentence as the step's title, the rest as its details."""
    flat = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", flat)
    head = m.group(1) if m else flat
    if len(head) > 120:
        head = head[:120].rsplit(" ", 1)[0] + "…"
        return head, flat
    return head.rstrip("."), flat[len(head):].strip()


def _steps(body: list[str], n_phase: int) -> list[dict]:
    steps: list[dict] = []
    step = None
    for line in body:
        m = _BOX.match(line)
        if m and not m.group(1):
            mark, rest = m.group(2).lower(), m.group(3).strip()
            bold = re.match(r"\*\*(.+?)\*\*\s*(.*)", rest)
            by = "user" if re.search(r"\((amos|user)\b", rest) else "agent"
            state = {"x": "done", "~": "active", "!": "blocked", "-": "dropped"}.get(mark, "open")
            step = {"id": f"s{n_phase}-{len(steps) + 1}", "by": by, "state": state,
                    "title": bold.group(1).rstrip(".") if bold else "", "details": bold.group(2) if bold else rest,
                    "plain": not bold}
            if state in ("blocked", "dropped"):
                step["reason"] = "as the Markdown plan this was converted from says"
            steps.append(step)
        elif step is not None and (line.startswith("  ") or not line.strip()):
            step["details"] = (step["details"] + "\n" + line.strip()).strip()
        elif line.strip():
            step = None
    for st in steps:
        if st.pop("plain"):
            st["title"], st["details"] = _split_title(st["details"])
    return steps


def _blocks(lines: list[str], marker: str) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = [("", [])]
    fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith(marker):
            blocks.append((line[len(marker):].strip(), []))
        else:
            blocks[-1][1].append(line)
    return blocks


def from_markdown(text: str, name: str) -> Draft:
    lines = text.splitlines()
    title = name
    body_start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title, body_start = line[2:].strip(), i + 1
            break
    blocks = _blocks(lines[body_start:], "## ")
    why = "\n".join(blocks[0][1]).strip()
    d = Draft(title=title, why=why, summary=_summary(why))
    has_box = lambda body: any(_BOX.match(line) for line in body)

    def add_section(heading: str, body: list[str]) -> None:
        if "\n".join(body).strip():
            d.sections.append((heading or "Notes", "\n".join(body).strip()))

    for heading, body in blocks[1:]:
        if not has_box(body):
            add_section(heading, body)
            continue
        subs = _blocks(body, "### ")
        if len(subs) == 1:
            subs = [(heading, body)]
        else:
            add_section(heading, subs[0][1])
            subs = [(sub, sb) for sub, sb in subs[1:]]
        for sub, sb in subs:
            if not has_box(sb):
                add_section(f"{heading}: {sub}" if sub != heading else heading, sb)
                continue
            n_phase = len(d.phases) + 1
            d.phases.append({"id": f"p{n_phase}", "title": sub, "steps": _steps(sb, n_phase)})
    return d


# --------------------------------------------------------------------------- #
# the prototype's HTML forms
# --------------------------------------------------------------------------- #

@dataclass
class Node:
    tag: str
    attrs: dict
    kids: list = field(default_factory=list)

    def find_all(self, pred) -> list["Node"]:
        out = []
        for k in self.kids:
            if isinstance(k, Node):
                if pred(k):
                    out.append(k)
                out.extend(k.find_all(pred))
        return out

    def first(self, pred) -> "Node | None":
        found = self.find_all(pred)
        return found[0] if found else None

    def cls(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].kids.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def _md(node, skip=lambda n: False) -> str:
    """A node's content as Markdown-ish text: code, links, emphasis, lists."""
    if isinstance(node, str):
        return node
    if skip(node) or node.tag in ("script", "style", "input", "textarea", "button", "summary"):
        return ""
    inner = "".join(_md(k, skip) for k in node.kids)
    t = node.tag
    if t == "code":
        return f"`{inner}`"
    if t in ("strong", "b"):
        return f"**{inner}**"
    if t in ("em", "i"):
        return f"*{inner}*"
    if t == "a" and node.attrs.get("href"):
        return f"[{inner}]({node.attrs['href']})"
    if t == "li":
        return f"\n- {inner.strip()}"
    if t in ("p", "div", "ul", "ol", "table", "details", "h2", "h3", "pre", "section", "fieldset"):
        return f"\n\n{inner.strip()}\n\n"
    if t == "br":
        return "\n"
    return inner


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _text(node) -> str:
    return " ".join(_md(node).split())


def from_form(html: str, name: str, answers_raw: dict | None, source: str,
              done_by: str = "") -> Draft:
    p = _Tree()
    p.feed(html)
    root = p.root
    h1 = root.first(lambda n: n.tag == "h1")
    main = root.first(lambda n: n.tag == "main") or root
    d = Draft(title=_text(h1) if h1 else name)

    why_parts = []
    for k in main.kids:
        if isinstance(k, Node) and k.tag == "p" and not ({"lead"} & k.cls()):
            why_parts.append(_md(k))
    d.why = _clean("\n\n".join(why_parts))
    for sec in main.find_all(lambda n: n.tag == "h2"):
        pass  # headings only group the cards; the cards carry the content

    taken: set[str] = set()
    steps: list[dict] = []
    for card in main.find_all(lambda n: n.tag == "section" and "card" in n.cls()):
        title = card.attrs.get("data-title") or _text(card.first(lambda n: n.tag == "h3") or Node("x", {}))
        summary = card.first(lambda n: n.tag == "p" and "summary" in n.cls())
        details = card.first(lambda n: n.tag == "details")
        context = _clean((_md(summary) if summary else "") + "\n\n" + (_md(details) if details else ""))
        fieldsets = card.find_all(lambda n: n.tag == "fieldset" and "data-q" in n.attrs)
        if not fieldsets:
            sid = f"s1-{len(steps) + 1}"
            taken.add(sid)
            steps.append({"id": sid, "title": title, "by": "agent", "state": "open", "details": context})
            continue
        for fs in fieldsets:
            qid = fs.attrs["data-q"]
            local = qid if LOCAL_RE.match(qid) and qid not in taken else _local(qid, taken, "decision")
            taken.add(local)
            opts = []
            multiple = False
            for lab in fs.find_all(lambda n: n.tag == "label"):
                inp = lab.first(lambda n: n.tag == "input")
                if inp is None:
                    continue
                multiple = multiple or inp.attrs.get("type") == "checkbox"
                hint = lab.first(lambda n: n.tag == "span" and "hint" in n.cls())
                opts.append({"id": inp.attrs.get("value", ""), "label": inp.attrs.get("data-label") or _text(lab),
                             "hint": _text(hint) if hint else ""})
            question = fs.attrs.get("data-label") or title
            ctx = context if len(fieldsets) == 1 else f"From the card \"{title}\".\n\n{context}"
            d.decisions.append({"id": local, "question": question, "context": ctx, "options": opts,
                                "multiple": multiple, "form_q": qid})

    if steps:
        d.phases.append({"id": "p1", "title": "Steps for the agent", "steps": steps})

    if answers_raw:
        saved_at = answers_raw.get("saved_at", "")
        for dec in d.decisions:
            a = (answers_raw.get("answers") or {}).get(dec["form_q"])
            if not a or not (a.get("selected") or (a.get("text") or "").strip()):
                continue
            d.answers[dec["id"]] = {"selected": list(a.get("selected") or []), "text": a.get("text") or "",
                                    "at": saved_at or answers_mod.now(), "source": f"{source} (saved_at {saved_at})"}
    if done_by:
        _close(d, done_by, source)
    return d


def _close(d: Draft, who: str, source: str) -> None:
    """A form from done/ was acted on: its answered decisions are resolved and its steps done."""
    for dec in d.decisions:
        a = d.answers.get(dec["id"])
        if not a:
            continue
        on = str(a["at"])[:10]
        dec["resolved"] = a["selected"] or ["own"]
        dec["resolved_on"] = on
        dec["outcome"] = f"{who}, {on}: answered in the form {source}; the plan it belonged to is done."
    for ph in d.phases:
        for st in ph["steps"]:
            if st["state"] == "open":
                st["state"] = "done"
