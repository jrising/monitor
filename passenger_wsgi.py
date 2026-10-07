"""
Entry point for DreamHost shared hosting (Passenger, WSGI).

Passenger looks for this file in the site's directory (e.g. ~/monitor.example.org/). It switches to the
virtualenv's Python, then wraps the FastAPI (ASGI) app so Passenger's WSGI server can run it.
Checks are run by a DreamHost cron job calling `python server.py tick` (shared hosting doesn't
allow a long-running scheduler process).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INTERP = os.path.join(HERE, "venv", "bin", "python3")
if os.path.exists(INTERP) and os.path.realpath(sys.executable) != os.path.realpath(INTERP):
    os.execl(INTERP, INTERP, *sys.argv)

sys.path.insert(0, HERE)
os.environ.setdefault("MONITOR_SCHEDULER", "external")

from a2wsgi import ASGIMiddleware  # noqa: E402

from server import app, init  # noqa: E402

init()
application = ASGIMiddleware(app)
