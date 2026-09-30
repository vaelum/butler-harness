"""planning: the service's API and rules, and the detached lifecycle."""

import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from butler.planning import answers, client, folder, service
from tests.test_planning import make_project, plan_text, put, run


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(client.PORT_ENV, raising=False)
    return client.state_dir()


@pytest.fixture
def repo(tmp_path):
    root = make_project(tmp_path / "repo")
    assert run(root, "init") == 0
    put(root, "draft/a.toml", plan_text())
    return root


@pytest.fixture
def server(state, repo):
    srv = service.make_server(0, state)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    status, entry = client.call(port, "POST", "/api/repos",
                                {"path": str(repo), "planning": str(repo / "planning"), "name": "demo"}, auth=True)
    assert status == 200 and entry["slug"] == "demo"
    yield port
    srv.shutdown()
    srv.server_close()


def req(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    for k, v in (headers if headers is not None else {client.HEADER: "1"}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and resp.headers.get_content_type() == "application/json"
                                 else raw), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), e.headers


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #

def test_pages_and_item(server):
    status, body, headers = req(server, "GET", "/r/demo/i/p-4k7x2m")
    assert status == 200 and b"/static/" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    status, item, headers = req(server, "GET", "/api/r/demo/item/p-4k7x2m")
    assert status == 200 and item["data"]["title"] == "Monitoring"
    assert item["summary"]["for_user"][0]["local"] == "alerts"
    status, _, _ = req(server, "GET", "/api/r/demo/item/p-4k7x2m", headers={"If-None-Match": headers["ETag"]})
    assert status == 304
    status, repo_view, _ = req(server, "GET", "/api/r/demo")
    assert repo_view["items"][0]["id"] == "p-4k7x2m" and repo_view["errors"] == 0
    status, all_, _ = req(server, "GET", "/api/repos")
    assert all_["repos"][0]["waiting"][0]["id"] == "p-4k7x2m"


def test_answer_tick_comment_and_approve(server, repo):
    assert req(server, "PUT", "/api/r/demo/decision/p-4k7x2m/alerts", {"selected": ["mail"], "text": "no"})[0] == 200
    assert req(server, "PUT", "/api/r/demo/decision/p-4k7x2m/alerts", {"selected": ["pigeon"]})[0] == 400
    assert req(server, "PUT", "/api/r/demo/step/p-4k7x2m/s1-3", {"state": "done"})[0] == 200
    assert req(server, "PUT", "/api/r/demo/step/p-4k7x2m/s1-1", {"state": "done"})[0] == 403, "agent steps are not ticked"
    status, body, _ = req(server, "POST", "/api/r/demo/comment/p-4k7x2m", {"on": "p1", "text": "back up first"})
    assert status == 200 and body["id"] == "c1"
    assert req(server, "PUT", "/api/r/demo/approval/p-4k7x2m/s1-1", {"verdict": "approved"})[0] == 400, "no gate"
    assert req(server, "PUT", "/api/r/demo/approval/p-4k7x2m/s1-2", {"verdict": "approved", "note": "go"})[0] == 200
    ans = answers.read(repo / "planning" / "answers" / "p-4k7x2m.json")
    assert ans["decisions"]["alerts"]["selected"] == ["mail"]
    assert ans["steps"]["s1-3"]["state"] == "done"
    assert ans["comments"][0]["on"] == "p1"
    assert ans["approvals"]["s1-2"]["verdict"] == "approved"
    # What the page wrote is what gate reads.
    assert run(repo, "gate", "p-4k7x2m", "s1-2") == 0
    # The item file itself was never written.
    assert (repo / "planning" / "draft" / "a.toml").read_text() == plan_text()


def test_a_resolved_decision_is_locked(server, repo):
    put(repo, "draft/a.toml", plan_text().replace('because = "It runs already."',
                                                  'because = "x"\nresolved = ["ntfy"]\noutcome = "o"'))
    assert req(server, "PUT", "/api/r/demo/decision/p-4k7x2m/alerts", {"selected": ["mail"]})[0] == 409


def test_what_is_refused(server, repo):
    # A write without the header, from another origin, or with a wrong Host.
    assert req(server, "PUT", "/api/r/demo/step/p-4k7x2m/s1-3", {"state": "done"}, headers={})[0] == 403
    assert req(server, "PUT", "/api/r/demo/step/p-4k7x2m/s1-3", {"state": "done"},
               headers={client.HEADER: "1", "Origin": "https://evil.example"})[0] == 403
    assert req(server, "GET", "/api/repos", headers={"Host": "evil.example"})[0] == 403
    # The control API without the token.
    assert req(server, "POST", "/api/repos", {"path": "/", "planning": "/etc"})[0] == 403
    assert req(server, "POST", "/api/shutdown", {})[0] == 403
    assert req(server, "DELETE", "/api/repos/demo")[0] == 403, "an existing repository needs the token"
    # Out of the planning folder, by path or by symlink.
    (repo / "secret.txt").write_text("x")
    (repo / "planning" / "link").symlink_to(repo / "secret.txt")
    assert req(server, "GET", "/r/demo/f/link")[0] == 404
    url = urllib.request.Request(f"http://127.0.0.1:{server}/r/demo/f/%2e%2e/secret.txt")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url, timeout=5)
    # A file under planning/ is served, sandboxed.
    status, _, headers = req(server, "GET", "/r/demo/f/draft/a.toml")
    assert status == 200 and headers["Content-Security-Policy"] == "sandbox"


def test_registering_twice_keeps_one_entry_and_worktrees_get_their_own_slug(server, repo, tmp_path):
    status, again = client.call(server, "POST", "/api/repos",
                                {"path": str(repo), "planning": str(repo / "planning"), "name": "demo"}, auth=True)
    assert again["slug"] == "demo"
    other = make_project(tmp_path / "wt")
    (other / "planning").mkdir()
    status, entry = client.call(server, "POST", "/api/repos",
                                {"path": str(other), "planning": str(other / "planning"), "name": "demo"}, auth=True)
    assert entry["slug"] == "demo-wt"
    assert len(client.registry()) == 2


# --------------------------------------------------------------------------- #
# the detached lifecycle
# --------------------------------------------------------------------------- #

@pytest.fixture
def detached(state):
    yield
    info = client.service_info()
    if info:
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_two_serves_at_once_start_one_service(detached, repo, monkeypatch):
    monkeypatch.setenv(client.PORT_ENV, "0")
    results = []

    def go():
        results.append(client.ensure(None))

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pids = {r.health["pid"] for r, _ in results}
    assert len(pids) == 1
    assert sorted(w for _, w in results) == ["running", "running", "started"]


def test_serve_registers_and_a_killed_service_comes_back(detached, repo, monkeypatch, capsys):
    monkeypatch.setenv(client.PORT_ENV, "0")
    assert run(repo, "serve") == 0
    assert "/r/demo/" in capsys.readouterr().out
    first = client.service_info()
    os.kill(first["pid"], signal.SIGKILL)
    time.sleep(0.2)
    assert run(repo, "serve") == 0
    second = client.service_info()
    assert second["pid"] != first["pid"]
    assert [r["slug"] for r in client.registry()] == ["demo"], "the registry survived"


def test_an_older_service_is_replaced(detached, repo, monkeypatch):
    monkeypatch.setenv(client.PORT_ENV, "0")
    running, _ = client.ensure(None)
    monkeypatch.setattr(client, "API", client.API + 1)
    newer, what = client.ensure(None)
    assert what.startswith("replaced")
    assert newer.health["pid"] != running.health["pid"]


def test_service_stop(detached, repo, monkeypatch):
    monkeypatch.setenv(client.PORT_ENV, "0")
    running, _ = client.ensure(None)
    client.stop(running)
    assert client.find() is None
    assert client.service_info() is None


def test_events_say_change_when_a_file_changes(server, repo):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request("GET", "/api/events?scope=repo&slug=demo", headers={"Host": f"127.0.0.1:{server}"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")

    def next_event():
        name = None
        while True:
            line = resp.fp.readline().decode()
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line == "\n" and name:
                return name

    assert next_event() == "hello"
    time.sleep(0.2)
    put(repo, "draft/a.toml", plan_text().replace('title = "Monitoring"', 'title = "Monitoring, renamed"'))
    assert next_event() == "change"
    conn.close()
