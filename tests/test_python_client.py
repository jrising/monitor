"""The Python client's short form, its console output, and monitor_lite, run as real scripts."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
CLIENT_DIR = APP / "clients" / "python"


def run_script(tmp_path, code, *, live=None, pythonpath=(CLIENT_DIR,)):
    script = tmp_path / "job.py"
    script.write_text(textwrap.dedent(code))
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONITOR_")}
    env["MONITOR_CLIENT_CONFIG"] = str(tmp_path / "no-login.json")
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in pythonpath)
    if live:
        env.update(MONITOR_URL=live["url"], MONITOR_TOKEN=live["token"])
    return subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True,
                          timeout=60, cwd=tmp_path)


def test_short_form_reports(tmp_path, live):
    r = run_script(tmp_path, """
        from monitor_client import panel
        run = panel("laptop-py-short")
        run.stage("loading data")
        for i in range(1, 5):
            run.progress(i / 4)
        run.done()
    """, live=live)
    assert r.returncode == 0, r.stderr
    p = live["admin"].get("/api/panels/laptop-py-short").json()
    assert p["status"] == "green" and p["progress"] == 1.0 and p["stage"] == "done"
    assert p["group"] == "laptop" and p["name"] == "laptop-py-short" and not p["priority"]


def test_uncaught_exception_turns_unfinished_panels_red(tmp_path, live):
    r = run_script(tmp_path, """
        from monitor_client import panel
        finished = panel("laptop-py-finished")
        finished.done()
        run = panel("laptop-py-crash")
        run.progress(0.5, stage="fitting")
        stepped = panel("laptop-py-crash-step")
        try:
            with stepped.track("downloading"):
                raise TimeoutError("server slow")
        except TimeoutError:
            pass
        raise ValueError("singular matrix")
    """, live=live)
    assert r.returncode == 1 and "singular matrix" in r.stderr
    admin = live["admin"]
    p = admin.get("/api/panels/laptop-py-crash").json()
    assert p["status"] == "red" and p["error"] == "ValueError: singular matrix", p
    assert admin.get("/api/panels/laptop-py-finished").json()["status"] == "green"
    p = admin.get("/api/panels/laptop-py-crash-step").json()  # a different exception: reported too
    assert p["status"] == "red" and p["error"] == "ValueError: singular matrix", p


def test_error_inside_track_keeps_step_name(tmp_path, live):
    r = run_script(tmp_path, """
        from monitor_client import panel
        run = panel("laptop-py-track")
        with run.track("downloading"):
            raise TimeoutError("server slow")
    """, live=live)
    assert r.returncode == 1
    p = live["admin"].get("/api/panels/laptop-py-track").json()
    assert p["status"] == "red" and p["error"] == "downloading: TimeoutError: server slow", p


def test_not_logged_in_prints_instead(tmp_path):
    r = run_script(tmp_path, """
        import monitor_client
        run = monitor_client.panel("laptop-x")
        assert not run.m.connected
        run.stage("loading data")
        for i in range(1, 21):
            run.progress(i / 20, n=i)
        run.warn("slow")
        with run.track("writing"):
            pass
        run.done()
    """)
    assert r.returncode == 0, r.stderr
    lines = r.stderr.splitlines()
    assert lines[0].startswith("[monitor] not logged in")   # said once, so a missing login is obvious
    assert lines[1] == "[laptop-x] loading data"
    assert "[laptop-x] 50%" in r.stderr and "(n=10)" in r.stderr
    assert "[laptop-x] warning: slow" in lines and "[laptop-x] writing ✓" in lines
    assert lines[-1].startswith("[laptop-x] done after")
    assert sum("[monitor]" in x for x in lines) == 1  # no attempts to reach a server


def test_lite_hands_over_to_the_client(tmp_path, live):
    r = run_script(tmp_path, """
        from monitor_lite import panel
        run = panel("laptop-py-lite")
        run.progress(0.5)
        run.done()
    """, live=live)
    assert r.returncode == 0, r.stderr
    assert live["admin"].get("/api/panels/laptop-py-lite").json()["status"] == "green"


def test_lite_alone(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "monitor_lite.py").write_text((CLIENT_DIR / "monitor_lite.py").read_text())
    r = run_script(tmp_path, """
        from monitor_lite import panel
        run = panel("laptop-x")
        run.stage("loading data")
        for i in range(1, 11):
            run.progress(i / 10)
        run.done(n_results=10)
        panel("laptop-y").error("diverged")
    """, pythonpath=(shared,))
    assert r.returncode == 0, r.stderr
    lines = r.stderr.splitlines()
    assert lines[0] == "[laptop-x] loading data"
    assert any(x.startswith("[laptop-x] done after") and x.endswith("(n_results=10)") for x in lines)
    assert lines[-1] == "[laptop-y] ERROR: diverged"
