"""The planning service: one process per user, every registered repository.

Started detached by `planning serve` (client.py), never by hand:

    python -P -c <boot> <src> <port> <state dir>

Pages (one shell, rendered by app.js):

    /                      every repository, and what waits on the user
    /r/<slug>/             one repository's items by folder
    /r/<slug>/i/<id>       one item, interactive
    /r/<slug>/m/<path>     a Markdown file under planning/, read-only
    /r/<slug>/f/<path>     any file under planning/, as is (sandboxed)

API (JSON):

    GET    /api/health
    GET    /api/events?scope=index|repo&slug=<slug>   Server-Sent Events: `hello`
                                          (the version) and `change`, whenever a
                                          file behind that page changes
    POST   /api/shutdown                          token
    GET    /api/repos
    POST   /api/repos                             token: register {path, planning, name}
    DELETE /api/repos/<slug>                      token, or the header if the path is gone
    GET    /api/r/<slug>                          the repository's items
    GET    /api/r/<slug>/item/<id>                one item, its answers and its state
    GET    /api/r/<slug>/file/<path>              a file's text
    PUT    /api/r/<slug>/decision/<id>/<local>    {selected, text}
    PUT    /api/r/<slug>/step/<id>/<local>        {state: "done" | "open"}  (user steps)
    PUT    /api/r/<slug>/approval/<id>/<local>    {verdict, note}           (gated steps)
    PUT    /api/r/<slug>/approval/<id>            {verdict, note}           (the plan)
    POST   /api/r/<slug>/comment/<id>             {on, text}

Who may do what:

- It binds 127.0.0.1 only, and the Host header must name it (DNS rebinding).
- Every write needs the header X-Butler-Planning and, if the browser sends an
  Origin, this server's own. A web page elsewhere cannot send a custom header
  without a CORS preflight, and none is ever answered.
- The control API (register, forget, shutdown) needs the token from the 0600
  file, so another account on the machine cannot make the service read one of
  this user's directories.
- Pages carry a Content-Security-Policy without inline script; files under /f/
  are sandboxed. Only files under a registered planning folder are served.
- A body is at most 256 KiB; answers are written atomically, one entry at a time.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import answers as answers_mod
from . import check as check_mod
from . import folder as folder_mod
from .client import API, HEADER, SERVICE
from .state import decision_local, plan_approval, step_approval, summarize
from .types import ID_RE, jsonable, plan_hash, plan_snapshot, step_hash, step_snapshot

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = ("app.css", "app.js", "marked.min.js", "marked.LICENSE.md")
MAX_BODY = 256 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _version() -> str:
    import butler

    h = hashlib.sha256()
    for name in ASSETS:
        h.update((STATIC / name).read_bytes())
    return f"{butler.__version__}-{h.hexdigest()[:8]}"


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.repos: list[dict] = []
        try:
            data = json.loads(path.read_text())
            self.repos = [r for r in data.get("repos", []) if isinstance(r, dict) and r.get("path")]
        except (OSError, ValueError, AttributeError):
            self.repos = []

    def save(self) -> None:
        answers_mod.write(self.path, {"repos": self.repos})

    def by_slug(self, slug: str) -> dict | None:
        return next((r for r in self.repos if r.get("slug") == slug), None)

    def register(self, path: str, planning: str, name: str) -> dict:
        repo = Path(path).resolve()
        with self.lock:
            for r in self.repos:
                if Path(r["path"]) == repo:
                    r["planning"] = planning
                    self.save()
                    return r
            base = re.sub(r"[^a-z0-9-]+", "-", (name or repo.name).lower()).strip("-") or "repo"
            taken = {r.get("slug") for r in self.repos}
            slug = base
            if slug in taken:
                slug = f"{base}-{re.sub(r'[^a-z0-9-]+', '-', repo.name.lower()).strip('-')}"
            n = 2
            while slug in taken:
                slug = f"{base}-{n}"
                n += 1
            entry = {"path": str(repo), "planning": planning, "name": name or repo.name, "slug": slug,
                     "common": _git_common(repo), "added": answers_mod.now()}
            self.repos.append(entry)
            self.save()
            return entry

    def forget(self, slug: str) -> bool:
        with self.lock:
            before = len(self.repos)
            self.repos = [r for r in self.repos if r.get("slug") != slug]
            if len(self.repos) != before:
                self.save()
                return True
            return False


def _git_common(repo: Path) -> str:
    """The git common directory, so worktrees group under their main checkout."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
                              "--git-common-dir"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return str(repo)


def _branch(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #

def _folder(entry: dict) -> folder_mod.Folder:
    return folder_mod.read(Path(entry["planning"]), Path(entry["path"]))


def repo_view(entry: dict, *, deep: bool = True) -> dict:
    missing = not Path(entry["planning"]).is_dir()
    out = {"slug": entry["slug"], "name": entry.get("name"), "path": entry["path"],
           "common": entry.get("common"), "missing": missing}
    if missing:
        return out
    f = _folder(entry)
    out["typed"] = f.typed
    out["user"] = f.user
    out["branch"] = _branch(Path(entry["path"]))
    items, waiting = [], []
    for it in f.items:
        if not it.parsed or not it.id:
            items.append({"id": it.id, "file": it.rel, "folder": it.folder, "title": it.title,
                          "kind": it.kind, "broken": True})
            continue
        s = summarize(it, f.answers(it.id), plan_approval_required=f.plan_approval, ids=f.by_id()).as_json()
        items.append(s)
        if s["for_user"]:
            waiting.append({"id": it.id, "title": it.title, "for_user": s["for_user"]})
    out["items"] = items
    out["waiting"] = waiting
    out["legacy"] = f.legacy
    out["forms"] = f.forms
    if deep:
        rep = check_mod.run(f, git=False)
        out["errors"] = rep.errors
        out["warnings"] = rep.warnings
    return out


def item_view(entry: dict, item_id: str) -> dict | None:
    f = _folder(entry)
    it = f.get(item_id)
    if it is None:
        return None
    ans = f.answers(item_id)
    approvals = {}
    for s in it.steps():
        if s.get("gate"):
            a = step_approval(s, ans)
            approvals[s.get("id")] = {k: getattr(a, k) for k in
                                      ("status", "verdict", "at", "note", "sha256", "current", "snapshot",
                                       "now")}
    view = {
        "repo": {"slug": entry["slug"], "name": entry.get("name")},
        "id": it.id, "kind": it.kind, "folder": it.folder, "file": it.rel,
        "data": jsonable(it.data), "answers": ans, "user": f.user,
        "plan_approval_required": f.plan_approval,
        "approvals": jsonable(approvals),
        "summary": summarize(it, ans, plan_approval_required=f.plan_approval, ids=f.by_id()).as_json(),
        "problems": [x.as_json() for x in check_mod.run(f, git=False).findings
                     if x.file in (it.rel, f"answers/{it.id}.json") and x.level != "info"],
    }
    if it.kind == "plan":
        pa = plan_approval(it, ans)
        view["plan_approval"] = jsonable({k: getattr(pa, k) for k in
                                          ("status", "verdict", "at", "note", "sha256", "current",
                                           "snapshot", "now")})
    return view


def _tree_signature(root: Path) -> str:
    """Names, sizes and mtimes of everything under `root`: changes when any
    file is written, added, moved or deleted. Polled, not inotify: stdlib-only,
    and a planning folder is a few dozen small files."""
    h = hashlib.sha256()
    try:
        for p in sorted(root.rglob("*")):
            if p.name.endswith(".lock") or p.name.endswith(".tmp"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p}:{st.st_mtime_ns}:{st.st_size};".encode())
    except OSError:
        h.update(b"missing")
    return h.hexdigest()


def _etag(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        try:
            st = p.stat()
            h.update(f"{p}:{st.st_mtime_ns}:{st.st_size};".encode())
        except OSError:
            h.update(f"{p}:-;".encode())
    return '"' + h.hexdigest()[:20] + '"'


# --------------------------------------------------------------------------- #
# the handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "butler-planning"
    registry: Registry
    port: int
    token: str
    version: str
    instance: str
    shell: bytes

    # ---- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt: str, *args) -> None:
        # Writes and errors only; a line per poll would bury them.
        if self.command in ("PUT", "POST", "DELETE") or (len(args) > 1 and str(args[1])[:1] in "45"):
            sys.stderr.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {self.command} {self.path} "
                             f"{args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: object, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(), extra=extra)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _write_ok(self) -> bool:
        if self.headers.get(HEADER) != "1":
            self._error(403, f"missing {HEADER}")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            self._error(403, "wrong Origin")
            return False
        return True

    def _authorized(self) -> bool:
        given = self.headers.get("Authorization", "")
        return secrets.compare_digest(given, f"Bearer {self.token}")

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._error(400, "bad Content-Length")
            return None
        if length > MAX_BODY:
            self._error(413, "body too large")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            self._error(400, "not JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "not a JSON object")
            return None
        return data

    def _route(self) -> list[str]:
        return [unquote(p) for p in urlsplit(self.path).path.split("/") if p]

    # ---- GET ---------------------------------------------------------------- #

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "wrong Host")
        parts = self._route()
        if not parts or parts[0] == "r" and (len(parts) == 2 or len(parts) >= 3 and parts[2] in ("i", "m")):
            return self._send(200, self.shell, "text/html; charset=utf-8",
                              {"Content-Security-Policy": CSP})
        if parts[0] == "static" and len(parts) == 3 and parts[1] == self.version and parts[2] in ASSETS:
            data = (STATIC / parts[2]).read_bytes()
            ctype = {"js": "text/javascript", "css": "text/css"}.get(parts[2].rsplit(".", 1)[-1],
                                                                     "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return None
        if parts[0] == "r" and len(parts) >= 4 and parts[2] == "f":
            return self._file(parts[1], parts[3:], raw=True)
        if parts[0] != "api":
            return self._error(404, "not found")
        api = parts[1:]
        if api == ["health"]:
            return self._json(200, {"service": SERVICE, "api": API, "version": self.version,
                                    "instance": self.instance, "pid": os.getpid(), "port": self.port})
        if api == ["repos"]:
            return self._json(200, {"repos": [repo_view(r, deep=False) for r in self.registry.repos]})
        if api == ["events"]:
            return self._events()
        if len(api) >= 2 and api[0] == "r":
            entry = self.registry.by_slug(api[1])
            if entry is None:
                return self._error(404, "no such repository")
            if len(api) == 2:
                return self._json(200, repo_view(entry))
            if len(api) == 4 and api[2] == "item":
                if not ID_RE.match(api[3]):
                    return self._error(404, "no such item")
                f = Path(entry["planning"])
                fold = folder_mod.read(f, Path(entry["path"]))
                it = fold.get(api[3])
                if it is None:
                    return self._error(404, "no such item")
                tag = _etag(it.path, fold.answers_path(it.id), f)
                if self.headers.get("If-None-Match") == tag:
                    return self._send(304, extra={"ETag": tag})
                view = item_view(entry, api[3])
                return self._json(200, view, {"ETag": tag})
            if len(api) >= 4 and api[2] == "file":
                return self._file(api[1], api[3:], raw=False)
        return self._error(404, "not found")

    # How often an open page's files are looked at, and how often a quiet
    # stream says it is still there (proxies and browsers drop idle ones).
    EVENT_TICK = 0.5
    EVENT_PING = 15.0

    def _events(self) -> None:
        """A Server-Sent Events stream: `change` whenever a file behind the page
        changes. The page reloads its data on it; `hello` carries the version, so
        a page served by an older service reloads itself after an upgrade."""
        from urllib.parse import parse_qs

        q = parse_qs(urlsplit(self.path).query)
        scope, slug = (q.get("scope") or ["index"])[0], (q.get("slug") or [""])[0]

        def roots() -> list[Path]:
            if scope == "repo":
                entry = self.registry.by_slug(slug)
                return [Path(entry["planning"])] if entry else []
            return [Path(r["planning"]) for r in self.registry.repos]

        def signature() -> str:
            parts = [_tree_signature(r) for r in roots()]
            if scope != "repo":
                parts.append(_etag(self.registry.path))
            return hashlib.sha256("|".join(parts).encode()).hexdigest()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # The stream stays open, so the connection must not be reused afterwards.
        self.close_connection = True
        try:
            self.wfile.write(f"event: hello\ndata: {json.dumps({'version': self.version})}\n\n".encode())
            self.wfile.flush()
            last, quiet = signature(), 0.0
            while not getattr(self.server, "stopping", False):
                time.sleep(self.EVENT_TICK)
                now = signature()
                if now != last:
                    last, quiet = now, 0.0
                    self.wfile.write(b"event: change\ndata: {}\n\n")
                    self.wfile.flush()
                else:
                    quiet += self.EVENT_TICK
                    if quiet >= self.EVENT_PING:
                        quiet = 0.0
                        self.wfile.write(b": still here\n\n")
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _file(self, slug: str, rel: list[str], *, raw: bool) -> None:
        entry = self.registry.by_slug(slug)
        if entry is None:
            return self._error(404, "no such repository")
        root = Path(entry["planning"]).resolve()
        target = (root / "/".join(rel)).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return self._error(404, "no such file")
        data = target.read_bytes()
        if raw:
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or target.suffix in (".md", ".toml", ".json"):
                ctype = ("text/html" if target.suffix == ".html" else "text/plain") + "; charset=utf-8"
            return self._send(200, data, ctype, {"Content-Security-Policy": "sandbox"})
        return self._json(200, {"path": "/".join(rel), "text": data.decode("utf-8", errors="replace")})

    # ---- writes ------------------------------------------------------------- #

    def do_POST(self) -> None:  # noqa: N802
        self._write("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._write("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._write("DELETE")

    def _write(self, method: str) -> None:
        if not self._host_ok():
            return self._error(403, "wrong Host")
        if not self._write_ok():
            return None
        parts = self._route()
        if len(parts) < 2 or parts[0] != "api":
            return self._error(404, "not found")
        api = parts[1:]

        if api == ["shutdown"] and method == "POST":
            if not self._authorized():
                return self._error(403, "the control API needs the token")
            self._json(200, {"stopping": True})
            self.server.stopping = True
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        if api == ["repos"] and method == "POST":
            if not self._authorized():
                return self._error(403, "the control API needs the token")
            body = self._body()
            if body is None:
                return None
            path, planning = body.get("path"), body.get("planning")
            if not isinstance(path, str) or not isinstance(planning, str) or not Path(path).is_absolute():
                return self._error(400, "path and planning must be absolute paths")
            if not Path(planning).resolve().is_relative_to(Path(path).resolve()):
                return self._error(400, "the planning folder must be inside the repository")
            if not Path(path).is_dir():
                return self._error(400, "no such directory")
            return self._json(200, self.registry.register(path, str(Path(planning).resolve()),
                                                          str(body.get("name") or "")))
        if len(api) == 2 and api[0] == "repos" and method == "DELETE":
            entry = self.registry.by_slug(api[1])
            if entry is None:
                return self._error(404, "no such repository")
            if not self._authorized() and Path(entry["path"]).exists():
                return self._error(403, "only a repository that no longer exists can be forgotten from the page")
            self.registry.forget(api[1])
            return self._json(200, {"forgotten": api[1]})

        if len(api) >= 4 and api[0] == "r":
            entry = self.registry.by_slug(api[1])
            if entry is None:
                return self._error(404, "no such repository")
            body = self._body()
            if body is None:
                return None
            return self._item_write(entry, method, api[2], api[3], api[4] if len(api) > 4 else None, body)
        return self._error(404, "not found")

    def _item_write(self, entry: dict, method: str, what: str, item_id: str, local: str | None,
                    body: dict) -> None:
        f = _folder(entry)
        it = f.get(item_id) if ID_RE.match(item_id) else None
        if it is None:
            return self._error(404, "no such item")
        path = f.answers_path(it.id)

        if what == "decision" and method == "PUT" and local:
            dec = next((d for d in it.decisions() if decision_local(it, d) == local), None)
            if dec is None:
                return self._error(404, "no such decision")
            if dec.get("resolved"):
                return self._error(409, "this decision is resolved; comment to reopen it")
            selected = body.get("selected") or []
            text = body.get("text") or ""
            oids = [o.get("id") for o in dec.get("option", [])]
            if not isinstance(selected, list) or any(s not in oids for s in selected):
                return self._error(400, "selected must name options of this decision")
            if len(selected) > 1 and not dec.get("multiple"):
                return self._error(400, "this decision takes one option")
            if not isinstance(text, str):
                return self._error(400, "text must be a string")
            if text and dec.get("own_answer") is False:
                return self._error(400, "this decision takes no own answer")

            def change(d: dict) -> None:
                d.setdefault("decisions", {})[local] = {"selected": selected, "text": text,
                                                        "at": answers_mod.now()}
            answers_mod.update(path, it.id, change)
            return self._json(200, {"saved": True})

        if what == "step" and method == "PUT" and local:
            step = next((s for s in it.steps() if s.get("id") == local), None)
            if step is None:
                return self._error(404, "no such step")
            if step.get("by") != "user":
                return self._error(403, "only the user's own steps are ticked in the page")
            state = body.get("state")
            if state not in ("done", "open"):
                return self._error(400, "state must be done or open")

            def change(d: dict) -> None:
                d.setdefault("steps", {})[local] = {"state": state, "at": answers_mod.now()}
            answers_mod.update(path, it.id, change)
            return self._json(200, {"saved": True})

        if what == "approval" and method == "PUT":
            verdict = body.get("verdict")
            note = body.get("note") or ""
            if verdict not in ("approved", "disapproved", None) or not isinstance(note, str):
                return self._error(400, "verdict must be approved, disapproved or null")
            if local:
                step = next((s for s in it.steps() if s.get("id") == local), None)
                if step is None:
                    return self._error(404, "no such step")
                if not step.get("gate"):
                    return self._error(400, "this step has no gate")
                record = {"verdict": verdict, "note": note, "at": answers_mod.now(),
                          "sha256": step_hash(step), "snapshot": jsonable(step_snapshot(step))}

                def change(d: dict) -> None:
                    if verdict is None:
                        d.setdefault("approvals", {}).pop(local, None)
                    else:
                        d.setdefault("approvals", {})[local] = record
            else:
                if it.kind != "plan":
                    return self._error(400, "only a plan is approved as a whole")
                record = {"verdict": verdict, "note": note, "at": answers_mod.now(),
                          "sha256": plan_hash(it.data), "snapshot": jsonable(plan_snapshot(it.data))}

                def change(d: dict) -> None:
                    if verdict is None:
                        d.pop("plan_approval", None)
                    else:
                        d["plan_approval"] = record
            answers_mod.update(path, it.id, change)
            return self._json(200, {"saved": True})

        if what == "comment" and method == "POST":
            on = body.get("on") or ""
            text = (body.get("text") or "").strip()
            if not isinstance(on, str) or (on and on not in it.locals()):
                return self._error(400, "on must be empty or a local id of this item")
            if not text:
                return self._error(400, "a comment needs text")
            created = {}

            def change(d: dict) -> None:
                cid = answers_mod.next_comment_id(d)
                created["id"] = cid
                d.setdefault("comments", []).append({"id": cid, "on": on, "text": text,
                                                     "at": answers_mod.now()})
            answers_mod.update(path, it.id, change)
            return self._json(200, {"id": created["id"]})

        return self._error(404, "not found")


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #

def make_server(port: int, state: Path) -> ThreadingHTTPServer:
    state.mkdir(parents=True, exist_ok=True)
    from .client import token as _token

    os.environ["XDG_STATE_HOME"] = str(state.parent.parent)
    version = _version()
    shell = (STATIC / "app.html").read_text().replace("{{V}}", version).encode()
    handler = type("H", (Handler,), {
        "registry": Registry(state / "repos.json"), "port": port, "token": _token(),
        "version": version, "instance": secrets.token_hex(8), "shell": shell,
    })
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    handler.port = server.server_address[1]
    return server


def main(argv: list[str]) -> None:
    port, state = int(argv[0]), Path(argv[1])
    server = make_server(port, state)
    h = server.RequestHandlerClass
    info = {"service": SERVICE, "pid": os.getpid(), "port": h.port, "api": API, "version": h.version,
            "instance": h.instance, "started": answers_mod.now()}
    answers_mod.write(state / "service.json", info)
    sys.stderr.write(f"{info['started']} serving on http://127.0.0.1:{h.port}/ "
                     f"(butler {h.version}, pid {os.getpid()})\n")
    sys.stderr.flush()

    def stop(*_):
        server.stopping = True
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            current = json.loads((state / "service.json").read_text())
            if current.get("instance") == h.instance:
                (state / "service.json").unlink()
        except (OSError, ValueError):
            pass
        sys.stderr.write(f"{answers_mod.now()} stopped\n")
