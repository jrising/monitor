"""Client-side batching: merged updates must equal sending each one; loops must not block or flood."""
import random
import threading
import time

import pytest
from fastapi.testclient import TestClient

import monitor_client
import server
from monitor_client import Monitor, merge_updates

STATES = ("status", "stage", "error", "progress", "stats")


def random_update(rng):
    u = {}
    if rng.random() < 0.4: u["status"] = rng.choice(["green", "yellow", "red"])
    if rng.random() < 0.4: u["stage"] = rng.choice(["load", "fit", "write"])
    if rng.random() < 0.3: u["error"] = rng.choice(["boom", "disk full"])
    if rng.random() < 0.4: u["progress"] = rng.choice([0.0, 0.5, 1.0])
    if rng.random() < 0.5: u["stats"] = {rng.choice("abc"): rng.randint(0, 9) for _ in range(rng.randint(1, 2))}
    if rng.random() < 0.15: u["replace_stats"] = True
    if rng.random() < 0.3: u["clear"] = rng.sample(STATES, rng.randint(1, 2))
    return u or {"stage": "x"}


@pytest.fixture(scope="module")
def admin():
    c = TestClient(server.app)
    c.post("/api/login", json={"password": "test-password"})
    c.headers["X-Monitor"] = "1"
    return c


def state(admin, pid):
    p = admin.get(f"/api/panels/{pid}").json()
    return {k: p[k] for k in STATES}


def test_merged_update_equals_sequence(admin):
    """Property test against the real server: one merged PUT == the same updates sent in order."""
    rng = random.Random(1)
    for trial in range(150):
        seq = [random_update(rng) for _ in range(rng.randint(1, 6))]
        start = {"status": "green", "stage": "s0", "error": "e0", "progress": 0.3, "stats": {"a": 1, "z": 9}}
        a, b = f"merge-a-{trial}", f"merge-b-{trial}"
        for pid in (a, b):
            assert admin.put(f"/api/panels/{pid}", json=start).status_code == 200
        for u in seq:
            assert admin.put(f"/api/panels/{a}", json=u).status_code == 200
        merged = None
        for u in seq:
            merged = merge_updates(merged, u)
        assert admin.put(f"/api/panels/{b}", json=merged).status_code == 200
        assert state(admin, a) == state(admin, b), (seq, merged)


class Recorder(Monitor):
    """A Monitor whose 'network' records requests and takes `delay` seconds each."""

    def __init__(self, delay=0.0, **kw):
        super().__init__(url="http://test", token="t", quiet=True, **kw)
        self.sent, self.delay = [], delay

    def _request(self, method, path, body=None, raise_errors=False):
        time.sleep(self.delay)
        self.sent.append((time.monotonic(), path, body))
        return {}


def test_tight_loop_is_fast_and_throttled():
    mon = Recorder(delay=0.3, min_interval=0.25)  # a slow server
    run = mon.panel("laptop-loop")
    t0 = time.perf_counter()
    n = 0
    while time.perf_counter() - t0 < 1.2:
        n += 1
        run.progress(min(n / 1e6, 0.99), stage=f"iteration {n}", n=n)
    per_call = (time.perf_counter() - t0) / n
    assert n > 10_000 and per_call < 100e-6, f"{n} calls, {per_call * 1e6:.1f} µs per call"
    assert len(mon.sent) <= 6, len(mon.sent)            # ~1 per 0.3 s, not one per call
    run.done(n=n)
    assert mon.flush(timeout=5)
    last = mon.sent[-1][2]
    assert last["progress"] == 1.0 and last["stats"]["n"] == n and last["stage"] == "done"


def test_important_updates_are_not_held_back():
    mon = Recorder(min_interval=60)
    run = mon.panel("laptop-x")
    run.stage("start")                     # first update: sent at once
    for i in range(100):
        run.progress(i / 100)              # routine: held for up to 60 s
    run.error("solver diverged")           # status change + error: sent at once
    deadline = time.monotonic() + 3
    while len(mon.sent) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(mon.sent) == 2
    assert mon.sent[1][2]["status"] == "red" and mon.sent[1][2]["progress"] == 0.99
    for _ in range(50):
        run.error("solver diverged")       # the same error again isn't news: throttled
    time.sleep(0.2)
    assert len(mon.sent) == 2


def test_pending_updates_sent_at_exit(tmp_path):
    """A script that ends right after a throttled update still delivers it (atexit flush)."""
    import json
    import subprocess
    import sys
    log = tmp_path / "sent.jsonl"
    script = tmp_path / "job.py"
    script.write_text(
        "import json, sys, time\n"
        f"sys.path.insert(0, {str(monitor_client.Path(monitor_client.__file__).parent)!r})\n"
        "import monitor_client\n"
        "class R(monitor_client.Monitor):\n"
        "    def _request(self, m, p, body=None, raise_errors=False):\n"
        "        time.sleep(0.2)\n"
        f"        open({str(log)!r}, 'a').write(json.dumps(body) + '\\n'); return {{}}\n"
        "mon = R(url='http://t', min_interval=60)\n"
        "run = mon.panel('laptop-job')\n"
        "run.stage('a')\n"
        "for i in range(1000): run.progress(i / 1000, n=i)\n")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    sent = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(sent) == 2 and sent[-1]["stats"]["n"] == 999, out.stdout + out.stderr


def test_background_false_is_synchronous():
    mon = Recorder(background=False)
    run = mon.panel("laptop-sync")
    for i in range(3):
        run.progress(i / 3)
    assert len(mon.sent) == 3
