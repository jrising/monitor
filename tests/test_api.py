"""The HTTP API: login, the feed editor, tokens and their limits, and the agent round trip."""
import pytest
from fastapi.testclient import TestClient

import server

FEEDS = """panels:
  - id: laptop-train
    name: train.py
    group: laptop
    on: laptop
    check: {type: process, name: pytest}
    schedule: every 15m
  - id: finance-total
    name: Portfolio
    group: finance
  - id: graph
    name: CPU graph
    priority: true
    embed: https://example.org/graph
    height: 180
"""


@pytest.fixture(scope="module")
def admin():
    c = TestClient(server.app)
    c.__enter__()
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert c.post("/api/login", json={"password": "test-password"}).status_code == 200
    c.headers["X-Monitor"] = "1"
    yield c
    c.__exit__(None, None, None)


@pytest.fixture(scope="module")
def laptop_token(admin):
    r = admin.post("/api/tokens", json={"name": "laptop"})
    assert r.status_code == 200 and r.json()["scope"] == "laptop-*"
    return r.json()["token"]


def bearer(tok):
    c = TestClient(server.app)
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


def test_requires_login():
    assert TestClient(server.app).get("/api/panels").status_code == 401


def test_cookie_writes_need_header(admin):
    r = admin.put("/api/config", json={"text": "panels:"}, headers={"X-Monitor": ""})
    assert r.status_code == 403


def test_feed_editor_validation(admin):
    bad = admin.put("/api/config", json={"text": "panels:\n  - {id: x, check: {type: shell, command: id}}"})
    assert bad.status_code == 422 and "check.type" in bad.json()["detail"]
    bad = admin.put("/api/config", json={"text": "panels:\n  - {id: x, on: laptop, check: {type: http, url: 'https://a'}}"})
    assert bad.status_code == 422
    r = admin.put("/api/config", json={"text": FEEDS})
    assert r.status_code == 200, r.text
    cfg = admin.get("/api/config").json()
    assert {c["name"] for c in cfg["checks"]} >= {"http", "json", "csv"}
    graph = admin.get("/api/panels/graph").json()
    assert graph["display"] == {"embed": "https://example.org/graph", "height": 180}


def test_token_scope(admin, laptop_token):
    t = bearer(laptop_token)
    assert t.put("/api/panels/laptop-job", json={"status": "ok", "stats": {"n": 3}}).status_code == 200
    assert t.put("/api/panels/other-job", json={"status": "ok"}).status_code == 403
    assert t.get("/api/panels/finance-total").status_code == 404
    assert t.get("/api/config").status_code == 403
    assert t.put("/api/panels/laptop-job", json={"check": {"type": "http"}}).json().get("check") is None
    ids = {p["id"] for p in t.get("/api/panels").json()["panels"]}
    assert ids == {"laptop-job", "laptop-train"}


def test_agent_round_trip(admin, laptop_token):
    t = bearer(laptop_token)
    assert admin.get("/api/panels/laptop-train").json()["status"] == "grey"  # agent never seen
    work = t.get("/api/agent").json()
    assert work["name"] == "laptop" and [c["id"] for c in work["checks"] if c["run"]] == ["laptop-train"]
    import monitor_client
    res = monitor_client.run_local_check(work["checks"][0]["check"])
    assert t.post("/api/agent/results", json={"results": [{"id": "laptop-train", **res}]}).json()["recorded"] == ["laptop-train"]
    assert not any(c["run"] for c in t.get("/api/agent").json()["checks"])  # not due again yet
    assert admin.get("/api/panels/laptop-train").json()["status"] == "green"
    # a click on the light asks the agent to re-run it
    assert admin.post("/api/panels/laptop-train/poll").json()["mode"] == "requested"
    assert [c["id"] for c in t.get("/api/agent").json()["checks"] if c["run"]] == ["laptop-train"]


def test_revoke(admin):
    tok = admin.post("/api/tokens", json={"name": "temp"}).json()["token"]
    assert bearer(tok).get("/api/agent").status_code == 200
    admin.delete("/api/tokens/temp")
    assert bearer(tok).get("/api/agent").status_code == 401


def test_run_stages(admin, laptop_token, monkeypatch):
    """`monitor-client run --stage`: several commands reporting as stages of one panel."""
    import sys

    import monitor_client
    t = bearer(laptop_token)

    def via_testclient(self, method, path, body=None, raise_errors=False):
        r = t.request(method, path, json=body)
        return r.json() if r.status_code < 400 else None
    monkeypatch.setattr(monitor_client.Monitor, "_request", via_testclient)
    run = lambda *a: monitor_client._cli(["run", "laptop-sync", *a])  # noqa: E731
    ok_cmd = ["--", sys.executable, "-c", "pass"]
    bad_cmd = ["--", sys.executable, "-c", "import sys; sys.exit('disk full')"]
    panel = lambda: admin.get("/api/panels/laptop-sync").json()  # noqa: E731

    assert run("--stage", "fetch", "--step", "1/3", *ok_cmd) == 0
    p = panel()
    assert p["status"] == "green" and p["stage"] == "fetch (1/3) done" and abs(p["progress"] - 1 / 3) < 1e-6
    assert "fetch" in p["stats"]

    assert run("--stage", "build", "--step", "2/3", *bad_cmd) == 1
    p = panel()
    assert p["status"] == "red" and p["error"] == "build: disk full"

    # a later stage that succeeds (e.g. chained with ;) must not turn the failed run green
    assert run("--stage", "upload", "--step", "3/3", *ok_cmd) == 0
    p = panel()
    assert p["status"] == "red" and "earlier stage failed" in p["stage"]
    assert set(p["stats"]) == {"fetch", "build", "upload"}

    # the next run's first stage starts fresh
    for i, s in enumerate(["fetch", "build", "upload"], 1):
        assert run("--stage", s, "--step", f"{i}/3", *ok_cmd) == 0
    p = panel()
    assert p["status"] == "green" and p["error"] is None and p["progress"] == 1.0
    assert p["stage"].startswith("all 3 stages ok")
