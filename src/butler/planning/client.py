"""Find, start, register with, and stop the one planning service per user.

The service's state lives in `$XDG_STATE_HOME/butler/planning/`:

    service.json   pid, port, api level, version, start time, instance id
    repos.json     the registry (written by the service only)
    token          mode 0600: the control API's credential
    service.log    the service's output, rotated at 1 MB
    start.lock     taken while a `serve` decides whether to start one

`ensure()` is what `planning serve` runs: probe, and only if nothing (or an
older service) answers, take the lock, probe again, and start one detached.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..errors import ButlerError

API = 1
DEFAULT_PORT = 8765
SERVICE = "butler-planning"
HEADER = "X-Butler-Planning"
PORT_ENV = "BUTLER_PLANNING_PORT"
LOG_LIMIT = 1024 * 1024


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "butler" / "planning"


def token(create: bool = True) -> str | None:
    path = state_dir() / "token"
    try:
        value = path.read_text().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    return value


def service_info() -> dict | None:
    try:
        data = json.loads((state_dir() / "service.json").read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def registry() -> list[dict]:
    try:
        data = json.loads((state_dir() / "repos.json").read_text())
    except (OSError, ValueError):
        return []
    repos = data.get("repos") if isinstance(data, dict) else None
    return [r for r in repos or [] if isinstance(r, dict) and r.get("path")]


def wanted_port(flag: int | None) -> int:
    if flag is not None:
        return flag
    env = os.environ.get(PORT_ENV)
    if env:
        try:
            return int(env)
        except ValueError:
            raise ButlerError(f"{PORT_ENV}={env!r} is not a port number") from None
    return DEFAULT_PORT


# --------------------------------------------------------------------------- #
# talking to it
# --------------------------------------------------------------------------- #

class Other(Exception):
    """Something that is not the planning service answers on the port."""


def call(port: int, method: str, path: str, body: dict | None = None, *,
         auth: bool = False, timeout: float = 3.0) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    req.add_header("Host", f"127.0.0.1:{port}")
    req.add_header(HEADER, "1")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", f"Bearer {token()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, (json.loads(raw) if raw else None)
    except ValueError:
        return status, raw.decode(errors="replace")


def health(port: int, timeout: float = 0.6) -> dict | None:
    """The service's health on `port`; None if nothing listens. Raises Other
    when something else answers."""
    try:
        status, body = call(port, "GET", "/api/health", timeout=timeout)
    except (ConnectionRefusedError, ConnectionResetError):
        return None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (ConnectionRefusedError, ConnectionResetError)):
            return None
        raise Other(str(e.reason)) from None
    except (TimeoutError, OSError) as e:
        raise Other(str(e)) from None
    if status == 200 and isinstance(body, dict) and body.get("service") == SERVICE:
        return body
    raise Other(f"HTTP {status}")


@dataclass
class Running:
    port: int
    health: dict

    @property
    def api(self) -> int:
        return int(self.health.get("api", 0))

    @property
    def version(self) -> str:
        return str(self.health.get("version", "?"))


def find(port: int | None = None) -> Running | None:
    """The running service: at the port service.json names, else at `port`."""
    tried = set()
    info = service_info()
    for p in ([info.get("port")] if info else []) + [wanted_port(port)]:
        if not isinstance(p, int) or p in tried or p == 0:
            continue
        tried.add(p)
        try:
            h = health(p)
        except Other:
            continue
        if h:
            return Running(p, h)
    return None


# --------------------------------------------------------------------------- #
# starting and stopping
# --------------------------------------------------------------------------- #

_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); "
         "from butler.planning.service import main; main(sys.argv[2:])")


def _src() -> Path:
    import butler
    return Path(butler.__file__).resolve().parent.parent


def _rotate(log: Path) -> None:
    try:
        if log.stat().st_size > LOG_LIMIT:
            os.replace(log, log.with_suffix(".log.1"))
    except FileNotFoundError:
        pass


def _spawn(port: int) -> int:
    """Start the service detached and return its real port."""
    if os.name == "nt":
        raise ButlerError("the planning service runs on Linux and macOS only")
    sdir = state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    token()
    info = sdir / "service.json"
    info.unlink(missing_ok=True)
    log = sdir / "service.log"
    _rotate(log)
    with open(log, "a") as out, open(os.devnull) as devnull:
        # -P and the state directory as cwd: started from a project root,
        # `import butler` would find the project's butler/ config folder
        # instead of this package (the trap the shim's comment describes).
        proc = subprocess.Popen(
            [sys.executable, "-P", "-c", _BOOT, str(_src()), str(port), str(sdir)],
            cwd=sdir, stdin=devnull, stdout=out, stderr=out, start_new_session=True,
            close_fds=True)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log.read_text(errors="replace").splitlines()[-5:]
            raise ButlerError("the planning service exited as it started",
                              hint="\n".join(tail) or f"see {log}")
        data = service_info()
        if data and data.get("pid") == proc.pid and data.get("port"):
            try:
                if health(int(data["port"])):
                    return int(data["port"])
            except Other:
                pass
        time.sleep(0.05)
    raise ButlerError("the planning service did not answer within 5 s", hint=f"see {log}")


def _lock():
    import fcntl

    sdir = state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    fh = open(sdir / "start.lock", "a")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def stop(running: Running, wait: float = 5.0) -> None:
    call(running.port, "POST", "/api/shutdown", {}, auth=True)
    instance = running.health.get("instance")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            answering = health(running.port, timeout=0.3) is not None
        except Other:
            answering = False
        # Stopped means both: no longer answering, and its service.json gone
        # (it removes that last, after closing the socket).
        info = service_info()
        if not answering and not (info and info.get("instance") == instance):
            return
        time.sleep(0.05)
    raise ButlerError(f"the planning service on port {running.port} did not stop")


def ensure(port: int | None = None) -> tuple[Running, str]:
    """A running service at API level API or newer: (it, what happened)."""
    running = find(port)
    if running and running.api >= API:
        return running, "running"
    fh = _lock()
    try:
        running = find(port)
        if running and running.api >= API:
            return running, "running"
        what = "started"
        if running:
            stop(running)
            what = f"replaced {running.version} (api {running.api})"
        want = wanted_port(port)
        if want:
            try:
                if health(want):
                    raise ButlerError(f"a planning service answers on port {want} but not as expected")
            except Other:
                raise ButlerError(f"port {want} is taken by something that is not the planning service",
                                  hint=f"start it on another: {PORT_ENV}=<port> or planning serve --port") \
                    from None
        real = _spawn(want)
        h = health(real)
        return Running(real, h or {}), what
    finally:
        fh.close()


def register(running: Running, repo: Path, planning: Path, name: str) -> dict:
    status, body = call(running.port, "POST", "/api/repos",
                        {"path": str(repo), "planning": str(planning), "name": name}, auth=True)
    if status != 200 or not isinstance(body, dict):
        raise ButlerError(f"the planning service refused to register {repo}: {body}")
    return body


def forget(running: Running, repo: Path) -> bool:
    for entry in registry():
        if Path(entry["path"]) == repo:
            status, _ = call(running.port, "DELETE", f"/api/repos/{entry['slug']}", auth=True)
            return status in (200, 204)
    return False
