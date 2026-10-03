"""`butler.py planning …`: the command tree, in every project (like doctor).

Everything an agent needs — check, status, list, show, gate — reads files and
works without the service. Only serve, service and forget talk to it.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
from pathlib import Path

from .. import ui
from ..command import Node, arg
from ..context import Ctx
from ..errors import ButlerError
from . import answers as answers_mod
from . import check as check_mod
from . import client
from . import convert as convert_mod
from . import folder as folder_mod
from .state import (DONE_STEP, decision_local, decision_state, plan_approval, step_approval,
                    summarize)
from .types import (CONFIG_FILE, FORMAT, ID_RE, KINDS, NOTES_FOLDER, SCHEMA_FOLDER, STATE_FOLDERS,
                    json_schema, jsonable, new_id)

HERE = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
LIST_WHAT = ("items", "decisions", "steps", "approvals", "comments", "findings")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def planning_dir(ctx: Ctx) -> Path:
    return ctx.root / ctx.cfg.planning.dir


def _folder(ctx: Ctx) -> folder_mod.Folder:
    return folder_mod.read(planning_dir(ctx), ctx.root)


def _typed(ctx: Ctx) -> folder_mod.Folder:
    f = _folder(ctx)
    if not f.exists:
        raise ButlerError(f"no planning folder at {ctx.disp(f.root)}",
                          hint="python butler.py planning init")
    return f


def _item(f: folder_mod.Folder, item_id: str) -> folder_mod.Item:
    it = f.get(item_id)
    if it is None:
        near = [i.id for i in f.items if i.id and (item_id in i.id or item_id in i.name)]
        raise ButlerError(f"no item with the id {item_id}",
                          hint=("did you mean: " + ", ".join(near)) if near else
                          "python butler.py planning list")
    return it


def _schema_ref(item_path: Path, root: Path) -> str:
    return os.path.relpath(root / SCHEMA_FOLDER / f"v{FORMAT}.json", item_path.parent)


def _print_json(obj) -> None:
    ui.plain(json.dumps(jsonable(obj), indent=2, ensure_ascii=False, default=str))


def _git_user(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "config", "user.name"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _tracked(repo: Path, path: Path) -> bool:
    try:
        return subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", str(path)],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# --------------------------------------------------------------------------- #
# init, new, schema, guide
# --------------------------------------------------------------------------- #

def cmd_init(ctx: Ctx, args) -> int:
    root = planning_dir(ctx)
    todo = []
    if not (root / CONFIG_FILE).exists():
        todo.append(CONFIG_FILE)
    for d in (*STATE_FOLDERS, NOTES_FOLDER):
        if not (root / d).is_dir():
            todo.append(d + "/")
    if not (root / SCHEMA_FOLDER / f"v{FORMAT}.json").exists():
        todo.append(f"{SCHEMA_FOLDER}/v{FORMAT}.json")
    if not todo:
        ui.ok("already a typed planning folder", ctx.disp(root))
        return 0
    if ctx.would(f"create {', '.join(todo)} in {ctx.disp(root)}"):
        return 0
    root.mkdir(parents=True, exist_ok=True)
    if not (root / CONFIG_FILE).exists():
        user = _git_user(ctx.root)
        lines = ["# This folder's settings: its presence makes it a typed planning folder.",
                 "# The rules: python butler.py planning guide", "", f"format = {FORMAT}"]
        if user:
            lines.append(f"user = {json.dumps(user)}")
        lines.append("plan_approval = true")
        (root / CONFIG_FILE).write_text("\n".join(lines) + "\n")
    for d in (*STATE_FOLDERS, NOTES_FOLDER):
        (root / d).mkdir(exist_ok=True)
        if not any((root / d).iterdir()):
            (root / d / ".gitkeep").touch()
    ignore = root / ".gitignore"
    if not ignore.exists():
        # The answers files are committed; the locks beside them are not.
        ignore.write_text("answers/.*.lock\n")
    _write_schema(root)
    ui.ok("typed planning folder", f"{ctx.disp(root)}: {', '.join(todo)}")
    ui.note("next: python butler.py planning new plan <name>")
    return 0


def _write_schema(root: Path) -> Path:
    path = root / SCHEMA_FOLDER / f"v{FORMAT}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_schema(), indent=2) + "\n")
    return path


def cmd_schema(ctx: Ctx, args) -> int:
    root = planning_dir(ctx)
    if ctx.would(f"write {ctx.disp(root / SCHEMA_FOLDER)}/v{FORMAT}.json"):
        return 0
    ui.ok("wrote", ctx.disp(_write_schema(root)))
    return 0


def cmd_guide(ctx: Ctx, args) -> int:
    ui.plain((HERE / "GUIDE.md").read_text().rstrip())
    return 0


def cmd_new(ctx: Ctx, args) -> int:
    f = _folder(ctx)
    if not f.typed:
        raise ButlerError(f"{ctx.disp(f.root)} is not a typed planning folder",
                          hint="python butler.py planning init")
    if not NAME_RE.match(args.name):
        raise ButlerError(f"name {args.name!r}: use lowercase letters, digits and dashes",
                          hint="the name is the file's; the id is made for you")
    folder = args.folder or (NOTES_FOLDER if args.kind == "note" else "draft")
    allowed = check_mod.ALLOWED[args.kind]
    if folder not in allowed:
        raise ButlerError(f"a {args.kind} lives in {', '.join(allowed)}, not {folder}/")
    path = f.root / folder / f"{args.name}.toml"
    if path.exists():
        raise ButlerError(f"{ctx.disp(path)} exists")
    taken = {i.id for i in f.items} | set(f.answer_files)
    item_id = new_id(args.kind, taken)
    title = args.title or args.name.replace("-", " ").capitalize()
    text = (HERE / "templates" / f"{args.kind}.toml").read_text()
    text = (text.replace("{id}", item_id).replace("{title}", title.replace('"', '\\"'))
            .replace("{today}", _dt.date.today().isoformat())
            .replace("{schema}", _schema_ref(path, f.root)))
    if ctx.would(f"write {ctx.disp(path)} ({item_id})"):
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    ui.ok(item_id, ctx.disp(path))
    ui.note("fill it in, then: python butler.py planning check")
    return 0


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def cmd_check(ctx: Ctx, args) -> int:
    f = _folder(ctx)
    report = check_mod.run(f, git=not args.no_git)
    if args.json:
        _print_json(report.as_json())
    else:
        shown = [x for x in report.findings if x.level != "info" or args.verbose_info]
        last = None
        for x in shown:
            if x.file != last:
                ui.plain(ui.bold(x.file or ctx.disp(f.root)))
                last = x.file
            where = f"line {x.line}: " if x.line else ""
            text = f"  {x.level:5}  {where}{x.message}"
            if x.level == "error":
                ui.plain(ui._wrap("31", text))
            elif x.level == "warn":
                ui.plain(ui._wrap("33", text))
            else:
                ui.plain(ui.dim(text))
        infos = len(report.of("info"))
        if infos and not args.verbose_info:
            ui.plain(ui.dim(f"({infos} note{'s' if infos != 1 else ''} on what waits on whom: "
                            "planning status, or check --all)"))
        label = "planning ok" if not report.errors else "planning check failed"
        counts = f"{report.errors} error(s), {report.warnings} warning(s) — {report.closing()}"
        (ui.ok if not report.errors else ui.warn)(label, counts)
    if report.errors or (args.strict and report.warnings):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# status, show, list
# --------------------------------------------------------------------------- #

def _status_lines(f: folder_mod.Folder, it: folder_mod.Item) -> list[str]:
    ans = f.answers(it.id)
    s = summarize(it, ans, plan_approval_required=f.plan_approval, ids=f.by_id())
    lines = [f"{it.id}  {it.folder}/  {it.title}"]
    for what, local, text in s.for_user:
        lines.append(f"  for {f.user}:  {what} {local}  {text}".rstrip())
    for what, local, text in s.for_agent:
        extra = ""
        if what == "answer":
            a = ans.get("decisions", {}).get(local, {})
            dec = next((d for d in it.decisions() if decision_local(it, d) == local), {})
            labels = [o.get("label") for o in dec.get("option", []) if o.get("id") in (a.get("selected") or [])]
            parts = labels + ([f"own answer: {a['text'].strip()}"] if (a.get("text") or "").strip() else [])
            extra = " → " + " | ".join(parts)
        lines.append(f"  for the agent:  {what} {local}  {text}{extra}".rstrip())
    if s.steps_total:
        owners = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(s.by_owner.items()))
        nxt = f"; next: {s.next_step['id']} ({s.next_step.get('by')}) {s.next_step.get('title', '')}" \
            if s.next_step else ""
        lines.append(f"  steps {s.steps_done}/{s.steps_total} done ({owners}){nxt}")
    return lines


def cmd_status(ctx: Ctx, args) -> int:
    f = _typed(ctx)
    items = [_item(f, args.id)] if args.id else [i for i in f.items if i.parsed and i.id]
    if args.json:
        _print_json([summarize(i, f.answers(i.id), plan_approval_required=f.plan_approval, ids=f.by_id()).as_json()
                     | {"answers": f.answers(i.id)} for i in items])
        return 0
    if not args.id:
        items = [i for i in items if i.folder != "done" or
                 summarize(i, f.answers(i.id), plan_approval_required=f.plan_approval, ids=f.by_id()).for_agent]
    if not items:
        ui.ok("nothing open", ctx.disp(f.root))
    for it in items:
        for i, line in enumerate(_status_lines(f, it)):
            ui.plain(ui.bold(line) if i == 0 else line)
    broken = [i.rel for i in f.items if not i.parsed or not i.id]
    if broken:
        ui.warn("cannot read", ", ".join(broken) + " — planning check says why")
    return 0


def cmd_show(ctx: Ctx, args) -> int:
    f = _typed(ctx)
    it = _item(f, args.id)
    if args.json:
        _print_json({"file": it.rel, "folder": it.folder, "data": it.data, "answers": f.answers(it.id)})
        return 0
    ans = f.answers(it.id)
    d = it.data
    out = [ui.bold(f"{d.get('title')}"), f"{it.kind} {it.id} · {it.folder}/ · {it.rel}"]
    if d.get("summary"):
        out += ["", d["summary"]]
    for key in ("why", "subject", "method"):
        if d.get(key):
            out += ["", ui.bold(key.capitalize()), d[key].strip()]
    scope = d.get("scope") or {}
    for key in ("in", "out"):
        if scope.get(key):
            out += ["", ui.bold(f"Scope, {key}")] + [f"  - {x}" for x in scope[key]]
    if d.get("acceptance"):
        out += ["", ui.bold("Acceptance")] + [f"  - {x}" for x in d["acceptance"]]
    for s in it.sections():
        out += ["", ui.bold(f"§ {s.get('title')}") + (f"  [{s['id']}]" if s.get("id") else ""),
                s.get("body", "").strip()]
    for dec in it.decisions():
        local = decision_local(it, dec)
        st = decision_state(dec, ans, local)
        out += ["", ui.bold(f"? {dec.get('question')}") + f"  [{local}, {st}]"]
        rec = dec.get("recommend")
        recs = rec if isinstance(rec, list) else [rec]
        chosen = dec.get("resolved") or (ans.get("decisions", {}).get(local, {}).get("selected") or [])
        for o in dec.get("option", []):
            mark = "x" if o.get("id") in chosen else " "
            star = " ★" if o.get("id") in recs else ""
            out.append(f"  [{mark}] {o.get('id')}: {o.get('label')}{star}")
        if dec.get("because"):
            out.append(f"      recommended because: {dec['because']}")
        if dec.get("outcome"):
            out.append(f"      outcome: {dec['outcome']}")
    marks = {"open": "[ ]", "active": "[~]", "done": "[x]", "blocked": "[!]", "dropped": "[-]"}
    for ph in it.phases():
        out += ["", ui.bold(f"{ph.get('id')}: {ph.get('title')}")]
        for s in ph.get("step", []):
            gate = ""
            if s.get("gate"):
                gate = f"  gate: {step_approval(s, ans).status}"
            out.append(f"  {marks.get(s.get('state', 'open'), '[?]')} {s.get('id')} ({s.get('by')}) "
                       f"{s.get('title')}{gate}")
            if s.get("reason"):
                out.append(f"      {s.get('state')}: {s['reason']}")
    for fnd in it.findings():
        out.append(f"  {fnd.get('id')} [{fnd.get('severity')}, {fnd.get('state', 'open')}] {fnd.get('title')}")
    for c in ans.get("comments", []):
        state = "handled" if c.get("resolved") else "open"
        out.append(f"  comment {c.get('id')} ({state}) on {c.get('on') or 'the item'}: {c.get('text')}")
    ui.plain("\n".join(out))
    return 0


def _folders_for(ctx: Ctx, everywhere: bool) -> list[tuple[str, folder_mod.Folder]]:
    if not everywhere:
        return [(ctx.name, _typed(ctx))]
    out = []
    for r in client.registry():
        planning = Path(r.get("planning") or Path(r["path"]) / "planning")
        if planning.is_dir():
            out.append((r.get("slug") or r.get("name") or r["path"], folder_mod.read(planning, Path(r["path"]))))
    if not out:
        raise ButlerError("no repository is registered", hint="python butler.py planning serve (in each)")
    return out


def cmd_list(ctx: Ctx, args) -> int:
    rows: list[dict] = []
    for repo, f in _folders_for(ctx, args.everywhere):
        for it in f.items:
            if not it.parsed or not it.id:
                continue
            ans = f.answers(it.id)
            base = {"repo": repo, "item": it.id, "folder": it.folder}
            if args.what == "items":
                s = summarize(it, ans, plan_approval_required=f.plan_approval, ids=f.by_id())
                if not args.all and it.folder == "done":
                    continue
                rows.append(base | {"kind": it.kind, "title": it.title, "done": s.steps_done,
                                    "total": s.steps_total, "for_user": len(s.for_user),
                                    "for_agent": len(s.for_agent)})
            elif args.what == "decisions":
                for dec in it.decisions():
                    local = decision_local(it, dec)
                    st = decision_state(dec, ans, local)
                    if st == "resolved" and not args.all:
                        continue
                    a = ans.get("decisions", {}).get(local, {})
                    rec = dec.get("recommend")
                    rows.append(base | {"local": local, "state": st, "question": dec.get("question"),
                                        "options": [{"id": o.get("id"), "label": o.get("label")}
                                                    for o in dec.get("option", [])],
                                        "recommend": rec if isinstance(rec, list) else ([rec] if rec else []),
                                        "because": dec.get("because"), "answer": a or None,
                                        "resolved": dec.get("resolved"), "outcome": dec.get("outcome")})
            elif args.what in ("steps", "approvals"):
                for s in it.steps():
                    state = s.get("state", "open")
                    if args.by and s.get("by") != args.by:
                        continue
                    if args.what == "approvals":
                        if not s.get("gate"):
                            continue
                        a = step_approval(s, ans)
                        if not args.all and (state in DONE_STEP or a.ok):
                            continue
                        rows.append(base | {"local": s.get("id"), "state": state, "title": s.get("title"),
                                            "approval": a.status, "line": a.line(f.user)})
                    else:
                        if not args.all and state in DONE_STEP:
                            continue
                        rows.append(base | {"local": s.get("id"), "state": state, "by": s.get("by"),
                                            "gate": bool(s.get("gate")), "title": s.get("title")})
            elif args.what == "comments":
                for c in ans.get("comments", []):
                    if c.get("resolved") and not args.all:
                        continue
                    rows.append(base | {"local": c.get("id"), "on": c.get("on"), "text": c.get("text"),
                                        "resolved": c.get("resolved")})
            elif args.what == "findings":
                for fnd in it.findings():
                    if fnd.get("state", "open") != "open" and not args.all:
                        continue
                    rows.append(base | {"local": fnd.get("id"), "state": fnd.get("state", "open"),
                                        "severity": fnd.get("severity"), "title": fnd.get("title")})
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        ui.ok(f"no {'' if args.all else 'open '}{args.what}")
        return 0
    prefix = (lambda r: f"{r['repo']}:") if args.everywhere else (lambda r: "")
    for r in rows:
        ref = prefix(r) + r["item"] + (f"/{r['local']}" if r.get("local") else "")
        if args.what == "items":
            waits = []
            if r["for_user"]:
                waits.append(f"{r['for_user']} for the user")
            if r["for_agent"]:
                waits.append(f"{r['for_agent']} for the agent")
            prog = f"{r['done']}/{r['total']}" if r["total"] else "-"
            ui.plain(f"{ref:24} {r['folder']:12} {r['kind']:9} {prog:>6}  {r['title']}"
                     + (ui.dim("  (" + ", ".join(waits) + ")") if waits else ""))
        elif args.what == "decisions":
            ui.plain(ui.bold(f"{ref}  [{r['state']}]  {r['question']}"))
            chosen = r["resolved"] or ((r["answer"] or {}).get("selected") or [])
            for o in r["options"]:
                star = " ★" if o["id"] in r["recommend"] else ""
                mark = "x" if o["id"] in chosen else " "
                ui.plain(f"    [{mark}] {o['id']}: {o['label']}{star}")
            if r["because"] and r["state"] != "resolved":
                ui.plain(ui.dim(f"        recommended because: {r['because']}"))
            if r["answer"] and (r["answer"].get("text") or "").strip():
                ui.plain(f"        own answer: {r['answer']['text'].strip()}")
            if r["outcome"]:
                ui.plain(ui.dim(f"        outcome: {r['outcome']}"))
        elif args.what == "steps":
            gate = " gate" if r["gate"] else ""
            ui.plain(f"{ref:28} {r['state']:8} {r['by']:5}{gate:5}  {r['title']}")
        elif args.what == "approvals":
            ui.plain(f"{ref:28} {r['approval']:11} {r['title']}")
        elif args.what == "comments":
            state = "handled" if r["resolved"] else "open"
            ui.plain(f"{ref:28} {state:8} on {r['on'] or 'the item'}: {r['text']}")
        elif args.what == "findings":
            ui.plain(f"{ref:28} {r['severity']:8} {r['state']:9} {r['title']}")
    return 0


# --------------------------------------------------------------------------- #
# move, gate, resolve, convert
# --------------------------------------------------------------------------- #

def cmd_move(ctx: Ctx, args) -> int:
    f = _typed(ctx)
    it = _item(f, args.id)
    if args.folder == it.folder:
        ui.ok(f"{it.id} is already in {it.folder}/")
        return 0
    why = check_mod.folder_rules(it, args.folder, f.answers(it.id), plan_approval_required=f.plan_approval)
    if why:
        raise ButlerError(f"{it.id} cannot move to {args.folder}/: " + "; ".join(why),
                          hint="python butler.py planning status " + it.id)
    inner = it.rel.split("/", 1)[1]
    target = f.root / args.folder / inner
    if target.exists():
        raise ButlerError(f"{ctx.disp(target)} exists")
    if ctx.would(f"move {ctx.disp(it.path)} to {ctx.disp(target)}"):
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if _tracked(ctx.root, it.path):
        ctx.check(["git", "mv", str(it.path), str(target)])
    else:
        it.path.rename(target)
    ui.ok(f"{it.id} → {args.folder}/", ctx.disp(target))
    return 0


def cmd_gate(ctx: Ctx, args) -> int:
    f = _typed(ctx)
    it = _item(f, args.id)
    ans = f.answers(it.id)
    if args.step:
        step = next((s for s in it.steps() if s.get("id") == args.step), None)
        if step is None:
            raise ButlerError(f"{it.id} has no step {args.step}")
        if not step.get("gate"):
            ui.ok(f"{it.id}/{args.step}", "has no gate; it needs no approval")
            return 0
        a = step_approval(step, ans)
        what = f"{it.id}/{args.step}"
    else:
        if it.kind != "plan":
            raise ButlerError(f"{it.id} is a {it.kind}; only a plan is approved as a whole")
        a = plan_approval(it, ans)
        what = f"{it.id} (the plan)"
    line = f"{what}: {a.line(f.user)}"
    if a.ok:
        ui.ok("gate open", line)
        return 0
    ui.warn("gate closed", line)
    ui.note("stop here and ask; the approval is given in the page (python butler.py planning serve)")
    return 1


def cmd_resolve(ctx: Ctx, args) -> int:
    f = _typed(ctx)
    it = _item(f, args.id)
    ans = f.answers(it.id)
    if not any(c.get("id") == args.comment for c in ans.get("comments", [])):
        raise ButlerError(f"{it.id} has no comment {args.comment}", hint=f"python butler.py planning status {it.id}")
    if ctx.would(f"mark comment {args.comment} of {it.id} as handled"):
        return 0

    def change(d: dict) -> None:
        for c in d.get("comments", []):
            if c.get("id") == args.comment:
                c["resolved"] = {"at": answers_mod.now(), "note": args.note}
    answers_mod.update(f.answers_path(it.id), it.id, change)
    ui.ok(f"{it.id} {args.comment}", "handled")
    return 0


def _state_and_subdir(src: Path, root: Path) -> tuple[str, str]:
    """The state folder a source sits in, and the subfolders below it: done/coverage/x.md → done, coverage."""
    try:
        parts = src.parent.relative_to(root).parts
    except ValueError:
        return "draft", ""
    if parts and parts[0] in STATE_FOLDERS:
        return parts[0], "/".join(parts[1:])
    return "draft", ""


def cmd_convert(ctx: Ctx, args) -> int:
    f = _folder(ctx)
    if not f.typed:
        raise ButlerError(f"{ctx.disp(f.root)} is not a typed planning folder", hint="python butler.py planning init")
    src = Path(args.file).expanduser()
    src = (src if src.is_absolute() else Path.cwd() / src).resolve()
    if not src.is_file():
        raise ButlerError(f"no such file: {args.file}")
    try:
        rel_src = src.relative_to(f.root).as_posix()
    except ValueError:
        rel_src = str(src)
    name = src.stem
    if src.suffix == ".md":
        draft = convert_mod.from_markdown(src.read_text(encoding="utf-8"), name)
    elif src.suffix == ".html":
        answers_file = src.with_name(f"{name}.answers.json")
        raw = None
        if answers_file.is_file():
            try:
                raw = json.loads(answers_file.read_text())
            except ValueError:
                ui.warn("ignored", f"{ctx.disp(answers_file)} is not JSON")
        draft = convert_mod.from_form(src.read_text(encoding="utf-8"), name,
                                      raw, answers_file.name if raw else "",
                                      done_by=f.user if "done" in _state_and_subdir(src, f.root)[0] else "")
    else:
        raise ButlerError("convert reads a Markdown plan (.md) or a prototype form (.html)")
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "converted"
    folder, subdir = _state_and_subdir(src, f.root)
    target = f.root / folder / subdir / f"{slug}.toml"
    if target.exists():
        raise ButlerError(f"{ctx.disp(target)} exists")
    item_id = new_id("plan", {i.id for i in f.items} | set(f.answer_files))
    text = draft.render(item_id, _dt.date.today(), rel_src, _schema_ref(target, f.root))
    if ctx.would(f"write {ctx.disp(target)}" + (f" and answers/{item_id}.json" if draft.answers else "")):
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if draft.answers:
        def change(d: dict) -> None:
            d.setdefault("decisions", {}).update(draft.answers)
        answers_mod.update(f.answers_path(item_id), item_id, change)
    ui.ok(item_id, f"{ctx.disp(target)} from {ctx.disp(src)}")
    if draft.answers:
        ui.ok("answers carried over", f"{len(draft.answers)} into answers/{item_id}.json, each marked with its source")
    ui.note("a skeleton: read it against the source, fill in recommendations, owners and gates, run "
            "planning check, then delete the source")
    return 0


# --------------------------------------------------------------------------- #
# serve, service, forget
# --------------------------------------------------------------------------- #

def cmd_serve(ctx: Ctx, args) -> int:
    root = planning_dir(ctx)
    if not root.is_dir():
        raise ButlerError(f"no planning folder at {ctx.disp(root)}", hint="python butler.py planning init")
    if ctx.would(f"make sure the planning service runs, and register {ctx.root}"):
        return 0
    running, what = client.ensure(args.port)
    entry = client.register(running, ctx.root, root, ctx.name)
    url = f"http://127.0.0.1:{running.port}/r/{entry['slug']}/"
    if what != "running":
        ui.ok(f"planning service {what}", f"port {running.port}, {running.version}")
    ui.ok(ctx.name, url)
    f = folder_mod.read(root, ctx.root)
    waiting = [(it, summarize(it, f.answers(it.id), plan_approval_required=f.plan_approval, ids=f.by_id()))
               for it in f.items if it.parsed and it.id]
    for it, s in waiting:
        if s.for_user:
            ui.plain(f"  {len(s.for_user)} waiting on {f.user}: "
                     f"http://127.0.0.1:{running.port}/r/{entry['slug']}/i/{it.id}  {it.title}")
    if args.open:
        import webbrowser

        webbrowser.open(url)
    return 0


def cmd_service(ctx: Ctx, args) -> int:
    action = args.action or "status"
    running = client.find(args.port)
    if action == "status":
        if not running:
            ui.note("the planning service is not running (python butler.py planning serve starts it)")
            return 1
        info = client.service_info() or {}
        ui.ok("planning service", f"http://127.0.0.1:{running.port}/  {running.version}, api {running.api}, "
                                  f"pid {running.health.get('pid')}, since {info.get('started', '?')}")
        for r in client.registry():
            gone = "" if Path(r["path"]).exists() else "  (missing)"
            ui.plain(f"  {r.get('slug'):28} {r['path']}{gone}")
        return 0
    if action == "logs":
        log = client.state_dir() / "service.log"
        if not log.exists():
            ui.note("no log yet")
            return 0
        ui.plain("\n".join(log.read_text(errors="replace").splitlines()[-args.lines:]))
        return 0
    if action == "stop":
        if not running:
            ui.note("the planning service is not running")
            return 0
        if ctx.would(f"stop the planning service on port {running.port}"):
            return 0
        client.stop(running)
        ui.ok("stopped", f"port {running.port}")
        return 0
    if action == "restart":
        if ctx.would("restart the planning service"):
            return 0
        port = running.port if running else args.port
        if running:
            client.stop(running)
        running, _ = client.ensure(port)
        ui.ok("restarted", f"http://127.0.0.1:{running.port}/  {running.version}")
        return 0
    raise ButlerError(f"unknown action {action}")


def cmd_forget(ctx: Ctx, args) -> int:
    repo = Path(args.path).resolve() if args.path else ctx.root
    running = client.find()
    if not running:
        raise ButlerError("the planning service is not running")
    if ctx.would(f"unregister {repo}"):
        return 0
    if client.forget(running, repo):
        ui.ok("forgotten", str(repo))
        return 0
    raise ButlerError(f"{repo} is not registered", hint="python butler.py planning service")


# --------------------------------------------------------------------------- #
# the tree
# --------------------------------------------------------------------------- #

def node() -> Node:
    json_flag = arg("--json", action="store_true", help="machine-readable output")
    return Node("planning", "typed plans: check, status, decisions, approvals, and the local pages", children=[
        Node("init", "make planning/ a typed planning folder (planning.toml, the folders, the schema)",
             func=cmd_init),
        Node("new", "a new item with a fresh id, from its kind's template",
             args=[arg("kind", choices=KINDS, help="plan, decision, review or note"),
                   arg("name", help="the file's name: lowercase words with dashes"),
                   arg("--title", help="the item's title (default: from the name)"),
                   arg("--folder", choices=(*STATE_FOLDERS, NOTES_FOLDER),
                       help="where (default: draft/, notes/ for a note)")],
             func=cmd_new),
        Node("check", "validate the whole planning folder (exit 1 on an error)",
             args=[arg("--strict", action="store_true", help="count warnings as errors"),
                   arg("--all", dest="verbose_info", action="store_true",
                       help="also print the notes on what waits on whom"),
                   arg("--no-git", action="store_true", help="skip comparing ids with HEAD"),
                   json_flag],
             func=cmd_check),
        Node("status", "what waits on the user and on the agent, item by item",
             args=[arg("id", nargs="?", help="one item"), json_flag], func=cmd_status),
        Node("list", "one line per decision, step, approval, comment, finding or item",
             args=[arg("what", nargs="?", default="items", choices=LIST_WHAT),
                   arg("--all", action="store_true", help="include what is finished"),
                   arg("--by", choices=("agent", "user"), help="steps of one owner"),
                   arg("--everywhere", action="store_true", help="every registered repository"),
                   json_flag],
             func=cmd_list),
        Node("show", "an item as text", args=[arg("id"), json_flag], func=cmd_show),
        Node("move", "move an item to another folder, after checking that folder's rules",
             args=[arg("id"), arg("folder", choices=(*STATE_FOLDERS, NOTES_FOLDER))], func=cmd_move),
        Node("gate", "exit 0 only if the step (or the plan) is approved and unchanged since",
             args=[arg("id"), arg("step", nargs="?", help="the gated step; without it, the plan")],
             func=cmd_gate),
        Node("resolve", "mark a user's comment as handled",
             args=[arg("id"), arg("comment", help="c1, c2, …"),
                   arg("--note", required=True, help="what was done about it")],
             func=cmd_resolve),
        Node("convert", "a typed skeleton from a Markdown plan or a prototype HTML form (with its answers)",
             args=[arg("file")], func=cmd_convert),
        Node("schema", "write the JSON Schema editors validate against", func=cmd_schema),
        Node("guide", "print the agent guide of this harness version", func=cmd_guide),
        Node("serve", "start the local service if needed, register this repository, print its URL",
             args=[arg("--port", type=int, help=f"only when starting it (default {client.DEFAULT_PORT}, "
                                                 f"or ${client.PORT_ENV})"),
                   arg("--open", action="store_true", help="open the page in a browser")],
             func=cmd_serve),
        Node("service", "the service itself: status, stop, restart, logs",
             args=[arg("action", nargs="?", choices=("status", "stop", "restart", "logs")),
                   arg("--port", type=int, help="where to look, or start"),
                   arg("--lines", type=int, default=40, help="log lines to show")],
             func=cmd_service),
        Node("forget", "unregister a repository (default: this one)",
             args=[arg("path", nargs="?")], func=cmd_forget),
    ])
