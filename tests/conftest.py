import os
import sys
import tempfile
from pathlib import Path

# Configure the server before it is imported: throwaway data folder, a password, cron-style scheduling.
_tmp = tempfile.mkdtemp(prefix="monitor-test-")
os.environ.update({
    "MONITOR_DATA": _tmp,
    "MONITOR_PASSWORD": "test-password",
    "MONITOR_SCHEDULER": "external",
    "SECRET_TEST_KEY": "s3cret",
})
os.environ.pop("MONITOR_TOKEN", None)
os.environ.pop("MONITOR_URL", None)
os.environ["MONITOR_CLIENT_CONFIG"] = os.path.join(_tmp, "no-login.json")  # never your real login
_root = Path(__file__).resolve().parent.parent
for _d in ("", "clients/python", "agent"):
    sys.path.insert(0, str(_root / _d))


import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def live():
    """The server on a real port, with a token for laptop-* panels."""
    import uvicorn
    from fastapi.testclient import TestClient

    import server
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


