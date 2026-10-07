"""The DreamHost CGI launcher: render it like setup.sh does and run it the way Apache would."""
import os
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent


def run_cgi(tmp_path, uri, method="GET", headers=None):
    script = tmp_path / "monitor.cgi"
    script.write_text((APP / "deploy/dreamhost/monitor.cgi.template").read_text()
                      .replace("@PYTHON@", sys.executable).replace("@APP@", str(APP)))
    script.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "GATEWAY_INTERFACE": "CGI/1.1", "REQUEST_METHOD": method,
           "REQUEST_URI": uri, "SERVER_NAME": "monitor.test", "SERVER_PORT": "443", "HTTPS": "on",
           "SERVER_PROTOCOL": "HTTP/1.1", "QUERY_STRING": "",
           "SCRIPT_NAME": "/monitor.cgi",  # what Apache sets after the rewrite; the launcher undoes it
           "MONITOR_DATA": os.environ["MONITOR_DATA"], "MONITOR_PASSWORD": "test-password",
           **(headers or {})}
    return subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=60)


def test_cgi_health(tmp_path):
    r = run_cgi(tmp_path, "/api/health")
    assert "200 OK" in r.stdout and '"ok":true' in r.stdout, r.stdout + r.stderr


def test_cgi_serves_dashboard_and_requires_auth(tmp_path):
    assert "<title>Monitor</title>" in run_cgi(tmp_path, "/").stdout
    assert "401" in run_cgi(tmp_path, "/api/panels").stdout.splitlines()[0]
