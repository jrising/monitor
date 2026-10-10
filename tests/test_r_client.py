"""The R client (monitor_client.R), run with Rscript against a live server. Skipped without R."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
RSCRIPT = shutil.which("Rscript")
pytestmark = pytest.mark.skipif(
    not RSCRIPT or subprocess.run([RSCRIPT or "Rscript", "-e", "library(curl); library(jsonlite)"],
                                  capture_output=True).returncode != 0,
    reason="needs Rscript with the curl and jsonlite packages")


def rscript(live, mode, *extra, client="clients/R/monitor_client.R", **env_extra):
    env = {**os.environ, "MONITOR_URL": live["url"], "MONITOR_TOKEN": live["token"],
           "MONITOR_CLIENT_CONFIG": "/nonexistent", **env_extra}
    return subprocess.run([RSCRIPT, str(APP / "tests/r_client_check.R"), str(APP / client), mode, *extra],
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


def test_r_short_form_defaults(live):
    """monitor_panel(id) alone: group from the id, uncaught errors turn unfinished panels red."""
    r = rscript(live, "short")
    assert r.returncode == 1, r.stdout + r.stderr
    admin = live["admin"]
    assert admin.get("/api/panels/laptop-r-short").json()["status"] == "green"
    p = admin.get("/api/panels/laptop-r-short-crash").json()
    assert p["status"] == "red" and p["error"] == "singular matrix" and p["group"] == "laptop", p


def test_r_lite_hands_over_to_full_client(live):
    r = rscript(live, "lite", client="clients/R/monitor_lite.R",
                MONITOR_R_CLIENT=str(APP / "clients/R/monitor_client.R"))
    assert r.returncode == 0 and "R LITE OK" in r.stdout, r.stdout + r.stderr


def test_r_lite_alone_prints(tmp_path):
    """Without the full client (or a login), monitor_lite.R prints progress and needs no packages."""
    script = tmp_path / "job.R"
    script.write_text(
        f'source("{APP / "clients/R/monitor_lite.R"}")\n'
        'run <- monitor_panel("laptop-x")\n'
        'run$stage("loading data")\n'
        'for (i in 1:20) run$progress(i / 20, n = i)\n'
        'run$warn("slow")\n'
        'run$track("writing", NULL)\n'
        'run$done()\n'
        'r2 <- monitor_panel("laptop-y"); r2$error("diverged")\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONITOR_")}
    r = subprocess.run([RSCRIPT, str(script)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    lines = r.stderr.splitlines()
    assert lines[0] == "[laptop-x] loading data"
    assert "[laptop-x] 50%" in r.stderr and "(n=10)" in r.stderr
    assert "[laptop-x] warning: slow" in lines and "[laptop-x] writing \u2713" in lines
    assert any(x.startswith("[laptop-x] done after") for x in lines)
    assert lines[-1] == "[laptop-y] ERROR: diverged"


def test_r_first_update_arrives_without_a_later_call(live):
    """R has no background thread: a stage() followed by a long computation must still show up."""
    r = rscript(live, "prompt")
    assert r.returncode == 0 and "R PROMPT OK" in r.stdout, r.stdout + r.stderr
