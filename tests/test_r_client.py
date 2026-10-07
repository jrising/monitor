"""The R client (monitor_client.R), run with Rscript against a live server. Skipped without R."""
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

import server

APP = Path(__file__).resolve().parent.parent
RSCRIPT = shutil.which("Rscript")
pytestmark = pytest.mark.skipif(
    not RSCRIPT or subprocess.run([RSCRIPT or "Rscript", "-e", "library(curl); library(jsonlite)"],
                                  capture_output=True).returncode != 0,
    reason="needs Rscript with the curl and jsonlite packages")


@pytest.fixture(scope="module")
def live():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    admin = TestClient(server.app)
    admin.post("/api/login", json={"password": "test-password"})
    admin.headers["X-Monitor"] = "1"
    admin.delete("/api/tokens/laptop")
    tok = admin.post("/api/tokens", json={"name": "laptop"}).json()["token"]
    yield {"url": f"http://127.0.0.1:{port}", "token": tok, "admin": admin}
    srv.should_exit = True


def rscript(live, mode, *extra):
    env = {**os.environ, "MONITOR_URL": live["url"], "MONITOR_TOKEN": live["token"],
           "MONITOR_CLIENT_CONFIG": "/nonexistent"}
    return subprocess.run([RSCRIPT, str(APP / "tests/r_client_check.R"), str(APP / "monitor_client.R"), mode, *extra],
                          env=env, capture_output=True, text=True, timeout=120)


def test_r_client(live):
    r = rscript(live, "basic")
    assert r.returncode == 0 and "R CLIENT OK" in r.stdout, r.stdout + r.stderr


def test_r_uncaught_error_turns_panel_red(live):
    r = rscript(live, "crash")
    assert r.returncode == 1, r.stdout + r.stderr
    p = live["admin"].get("/api/panels/laptop-r-cron").json()
    assert p["status"] == "red" and p["error"] == "input file missing: data.csv", p


def test_r_error_inside_track_keeps_step_name(live):
    r = rscript(live, "crash-in-track")
    assert r.returncode == 1, r.stdout + r.stderr
    p = live["admin"].get("/api/panels/laptop-r-cron2").json()
    assert p["status"] == "red" and p["error"] == "downloading: timeout from server", p


def test_r_merge_matches_sending_in_order(live, tmp_path):
    """Same property test as for Python: R's merged update == the updates applied one by one."""
    import json
    import random

    from test_client_throttle import STATES, random_update
    rng = random.Random(7)
    seqs = [[random_update(rng) for _ in range(rng.randint(1, 6))] for _ in range(150)]
    f = tmp_path / "seqs.json"
    f.write_text(json.dumps(seqs))
    r = rscript(live, "merge", str(f))
    assert r.returncode == 0, r.stderr
    merged = json.loads(r.stdout)
    admin = live["admin"]
    start = {"status": "green", "stage": "s0", "error": "e0", "progress": 0.3, "stats": {"a": 1, "z": 9}}
    for i, (seq, m) in enumerate(zip(seqs, merged)):
        a, b = f"rmerge-a-{i}", f"rmerge-b-{i}"
        for pid in (a, b):
            admin.put(f"/api/panels/{pid}", json=start)
        for u in seq:
            admin.put(f"/api/panels/{a}", json=u)
        assert admin.put(f"/api/panels/{b}", json=m).status_code == 200, m
        sa, sb = (admin.get(f"/api/panels/{x}").json() for x in (a, b))
        assert {k: sa[k] for k in STATES} == {k: sb[k] for k in STATES}, (seq, m)


def test_r_tight_loop(live):
    r = rscript(live, "loop")
    assert r.returncode == 0 and "R LOOP OK" in r.stdout, r.stdout + r.stderr
    print(r.stdout)


def test_r_pending_sent_at_exit(live):
    r = rscript(live, "exit")
    assert r.returncode == 0, r.stderr
    p = live["admin"].get("/api/panels/laptop-r-exit").json()
    assert p["stats"].get("n") == 500 and p["progress"] == 0.5, p
