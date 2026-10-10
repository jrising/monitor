"""
Monitor client for Python: report a job's progress to the Monitor dashboard.
Standard library only. The interface every language's client shares is in clients/README.md.

    from monitor_client import panel

    run = panel("laptop-calibration")      # group "laptop", from the id
    run.stage("loading data")
    for i, r in enumerate(regions, 1):
        fit(r)
        run.progress(i / len(regions))
    run.done()

Optional extras: panel(..., name=, group=, priority=True, stale_after="2h"), run.progress(f, stage=,
n_results=12) (other keyword arguments become stats), run.warn(), run.error(), `with run.track("step"):`.

Logging in once per machine (`monitor-agent login URL TOKEN`) is all the setup there is. Without a
login, or with MONITOR_URL=off, nothing is sent and progress is printed instead (see monitor_lite.py,
the copy-anywhere version of that). On a terminal, progress is printed as well as sent.

An uncaught exception turns the job's panels red (catch_errors=False to leave it alone).
Updates are sent from a background thread and throttled, so calling run.progress() on every
iteration of a fast loop is fine: routine updates go out at most every few seconds (latest values
win), while a status change, an error, completion or a panel's first update go out at once. Anything
still pending is sent when the program exits (or call flush_all()).

Network failures never raise: monitoring must not crash the thing being monitored.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import monitor_lite  # prints progress; installed alongside this file
except ImportError:
    monitor_lite = None

MONITOR_CLIENT_API = 2  # monitor_lite hands over to this module only if it's at least this version

STATUS_ALIASES = {
    "green": "green", "ok": "green", "good": "green", "up": "green", "running": "green", "done": "green",
    "yellow": "yellow", "checking": "yellow", "warn": "yellow", "warning": "yellow", "pending": "yellow",
    "red": "red", "error": "red", "stopped": "red", "down": "red", "failed": "red", "fail": "red",
    "grey": "grey", "gray": "grey", "unknown": "grey",
}
STATE_FIELDS = ("status", "stage", "error", "progress", "stats")
DEFAULT_MIN_INTERVAL = float(os.environ.get("MONITOR_MIN_INTERVAL", "5"))

CONFIG_FILE = Path(os.environ.get("MONITOR_CLIENT_CONFIG", Path.home() / ".config" / "monitor" / "client.json"))


def _saved_login() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _effects(u: dict) -> dict:
    """What one update does on the server, per field: ("set", v) / ("clear",) for plain fields,
    ("merge", d) / ("replace", d) for stats. (The server applies `clear` after the other fields.)"""
    eff = {}
    for k, v in u.items():
        if k in ("clear", "replace_stats"):
            continue
        if k == "stats":
            eff[k] = ("replace" if u.get("replace_stats") else "merge", dict(v))
        else:
            eff[k] = ("set", v)
    for k in u.get("clear") or []:
        if k in STATE_FIELDS:
            eff[k] = ("replace", {}) if k == "stats" else ("clear",)
    return eff


def merge_updates(old: Optional[dict], new: dict) -> dict:
    """Combine two panel updates into one with the same effect as sending them in order."""
    eff = _effects(old or {})
    for k, e in _effects(new).items():
        if k == "stats" and e[0] == "merge" and k in eff:  # merge on top of an earlier merge/replace
            eff[k] = (eff[k][0], {**eff[k][1], **e[1]})
        else:
            eff[k] = e
    body: dict[str, Any] = {}
    clear = []
    for k, e in eff.items():
        if e[0] == "set":
            body[k] = e[1]
        elif e[0] == "clear" or (e[0] == "replace" and not e[1]):
            clear.append(k)
        else:
            body[k] = e[1]
            if e[0] == "replace":
                body["replace_stats"] = True
    if clear:
        body["clear"] = sorted(clear)
    return body


class Monitor:
    """Connection to the dashboard. Panel updates go through a background sender that throttles
    routine updates to one per `min_interval` seconds per panel (default 5, or MONITOR_MIN_INTERVAL)
    and sends important ones at once. background=False sends every update synchronously instead.

    The server is `url`, else MONITOR_URL, else the saved login. With none of these (or "off"), the
    monitor isn't connected: nothing is sent and panels print their progress instead.
    echo: also print progress while sending (default: when stderr is a terminal)."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None,
                 timeout: float = 10.0, quiet: bool = False, *,
                 min_interval: Optional[float] = None, background: bool = True,
                 echo: Optional[bool] = None):
        saved = _saved_login()
        url = url or os.environ.get("MONITOR_URL") or saved.get("url") or ""
        self.connected = bool(url) and url.lower() not in ("off", "none", "0", "false")
        self.url = url.rstrip("/") if self.connected else None
        self.token = token or os.environ.get("MONITOR_TOKEN") or saved.get("token") or ""
        self.echo = (not self.connected) or (_isatty(sys.stderr) if echo is None else echo)
        self.timeout = timeout
        self.quiet = quiet
        self.min_interval = DEFAULT_MIN_INTERVAL if min_interval is None else float(min_interval)
        self.background = background
        self._cv = threading.Condition()
        self._pending: dict[str, dict] = {}      # panel id -> merged update not yet sent
        self._urgent: set[str] = set()
        self._last: dict[str, dict] = {}         # panel id -> what was last queued (status, error, done, time)
        self._sending = 0
        self._worker: Optional[threading.Thread] = None

    # ---- background sending
    def _submit(self, panel_id: str, body: dict) -> None:
        if not self.connected:
            return
        if "status" in body:
            body["status"] = STATUS_ALIASES.get(str(body["status"]).lower(), body["status"])
        if not self.background:
            self._request("PUT", f"/api/panels/{panel_id}", body)
            return
        with self._cv:
            last = self._last.get(panel_id)
            urgent = (last is None
                      or ("status" in body and body["status"] != last["status"])
                      or (body.get("error") is not None and body["error"] != last["error"])
                      or (body.get("progress") == 1.0 and not last["done"]))
            if last is None:
                last = self._last[panel_id] = {"status": None, "error": None, "done": False, "sent": 0.0}
            if "status" in body:
                last["status"] = body["status"]
            if "error" in body or "error" in (body.get("clear") or []):
                last["error"] = body.get("error")
            if "progress" in body or "progress" in (body.get("clear") or []):
                last["done"] = body.get("progress") == 1.0
            self._pending[panel_id] = merge_updates(self._pending.get(panel_id), body)
            if urgent:
                self._urgent.add(panel_id)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run_sender, daemon=True, name="monitor-sender")
                self._worker.start()
                if self not in _live_monitors:
                    _live_monitors.append(self)
            self._cv.notify_all()

    def _run_sender(self) -> None:
        while True:
            with self._cv:
                while True:
                    now = time.monotonic()
                    due = [pid for pid in self._pending
                           if pid in self._urgent or now - self._last[pid]["sent"] >= self.min_interval]
                    if due:
                        break
                    wait = min((self._last[pid]["sent"] + self.min_interval - now for pid in self._pending),
                               default=None)
                    self._cv.wait(timeout=wait)
                pid = due[0]
                body = self._pending.pop(pid)
                self._urgent.discard(pid)
                self._last[pid]["sent"] = now
                self._sending += 1
            try:
                self._request("PUT", f"/api/panels/{pid}", body)
            except Exception as e:  # never let the sender thread die
                if not self.quiet:
                    print(f"[monitor] sending update for {pid} failed: {e}", file=sys.stderr)
            finally:
                with self._cv:
                    self._sending -= 1
                    self._cv.notify_all()

    def flush(self, timeout: float = 10.0) -> bool:
        """Send everything pending now (ignoring the throttle) and wait for it. True if all sent."""
        deadline = time.monotonic() + timeout
        with self._cv:
            self._urgent.update(self._pending)
            self._cv.notify_all()
            while self._pending or self._sending:
                left = deadline - time.monotonic()
                if left <= 0 or self._worker is None or not self._worker.is_alive():
                    return False
                self._cv.wait(timeout=left)
        return True

    def _request(self, method: str, path: str, body: Optional[dict] = None, raise_errors: bool = False):
        if not self.connected:
            if raise_errors:
                raise RuntimeError("not logged in: run `monitor-agent login URL TOKEN` (or set MONITOR_URL)")
            return None
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("X-Token", self.token)  # some hosts (Apache CGI) strip Authorization
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except (urllib.error.URLError, OSError, ValueError) as e:
            detail = e
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = json.loads(e.read()).get("detail", e)
                except Exception:
                    pass
            if raise_errors:
                raise RuntimeError(f"{method} {path}: {detail}") from None
            if not self.quiet:
                print(f"[monitor] {method} {path} failed: {detail}", file=sys.stderr)
            return None

    def panel(self, panel_id: str, *, name: Optional[str] = None, group: Optional[str] = None,
              priority: Optional[bool] = None, stale_after: Optional[int | str] = None,
              url: Optional[str] = None, catch_errors: bool = True) -> "Panel":
        """Get a panel; it's created on its first update. Definition fields are sent only if given
        (and are ignored for panels defined in the dashboard's feed list, which owns them). Left out,
        the name is the id and the group is the id's first part ("laptop" for "laptop-calibration").
        catch_errors: an uncaught exception turns this panel red (unless it already finished)."""
        p = Panel(self, panel_id, name=name, catch_errors=catch_errors)
        definition = {k: v for k, v in dict(name=name, group=group, priority=priority,
                                             stale_after=stale_after, url=url).items() if v is not None}
        if definition:
            self._submit(panel_id, definition)
        return p

    def panels(self) -> list[dict]:
        r = self._request("GET", "/api/panels")
        return r["panels"] if r else []


class Panel:
    def __init__(self, monitor: Monitor, panel_id: str, *, name: Optional[str] = None,
                 catch_errors: bool = False):
        self.m = monitor
        self.id = panel_id
        self._hb_stop = threading.Event()
        self._echo = monitor_lite.Panel(panel_id, name=name) if monitor.echo and monitor_lite else None
        self._finished = False       # done() called: an exception after that isn't this job's failure
        self._reported = None        # the exception track() already reported
        if catch_errors:
            _watch(self)

    def update(self, status: Optional[str] = None, *, stage: Optional[str] = None,
               error: Optional[str] = None, progress: Optional[float] = None,
               clear: Optional[list[str]] = None, **stats: Any) -> None:
        """status: green/yellow/red (aliases: ok/running, checking/warn, error/stopped).
        progress: 0..1. Extra keyword args become stats, e.g. n_results=120, loss=0.031.
        Returns immediately; the update is sent in the background (throttled, see Monitor)."""
        body: dict[str, Any] = {}
        if status is not None: body["status"] = status
        if stage is not None: body["stage"] = stage
        if error is not None: body["error"] = error
        if progress is not None: body["progress"] = max(0.0, min(1.0, float(progress)))
        if stats: body["stats"] = stats
        if clear: body["clear"] = clear
        if self._echo is not None:
            self._echo.update(status, stage=stage, error=error, progress=progress, clear=clear, **stats)
        if "progress" in body:
            self._finished = body["progress"] == 1.0
        self.m._submit(self.id, body)

    def ok(self, stage: Optional[str] = None, **stats):
        return self.update("green", stage=stage, clear=["error"], **stats)

    def stage(self, msg: str, **stats):
        """Report the current step; the job is running fine (green)."""
        return self.update("green", stage=msg, clear=["error"], **stats)

    def warn(self, msg: str, **stats):
        return self.update("yellow", stage=msg, **stats)

    def progress(self, frac: float, stage: Optional[str] = None, **stats):
        return self.update("green", progress=frac, stage=stage, **stats)

    def stats(self, **stats):
        return self.update(**stats)

    def done(self, msg: str = "done", **stats):
        return self.update("green", stage=msg, progress=1.0, clear=["error"], **stats)

    def error(self, msg: str, **stats):
        return self.update("red", error=msg, **stats)

    def stopped(self, msg: str = "stopped", **stats):
        return self.update("red", error=msg, **stats)

    @contextlib.contextmanager
    def track(self, stage: str, done_msg: Optional[str] = None):
        """with panel.track("fitting model"): ...  -> red with the exception if it raises."""
        self.stage(stage)
        try:
            yield self
        except BaseException as e:
            tb = traceback.format_exception_only(type(e), e)[-1].strip()
            self.error(f"{stage}: {tb}"[:500])
            self._reported = e
            raise
        else:
            self.ok(done_msg or f"{stage} ✓")

    def heartbeat(self, interval: float = 60, on_poll: Optional[Callable[["Panel"], Any]] = None) -> "Panel":
        """Background thread that keeps the panel fresh (pair with stale_after) and answers
        clicks on the dashboard light by calling on_poll(panel)."""
        def loop():
            last_beat = 0.0
            while not self._hb_stop.wait(min(5.0, interval)):
                info = self.m._request("GET", f"/api/panels/{self.id}")
                if info and info.get("poll_requested"):
                    try:
                        (on_poll or (lambda p: p.update(alive=time.strftime("%H:%M:%S"))))(self)
                    except Exception as e:
                        self.error(f"on_poll failed: {e}")
                    last_beat = time.time()
                elif time.time() - last_beat >= interval:
                    self.update(alive=time.strftime("%H:%M:%S"))
                    last_beat = time.time()
        self._hb_stop.clear()
        threading.Thread(target=loop, daemon=True, name=f"monitor-hb-{self.id}").start()
        return self

    def stop_heartbeat(self):
        self._hb_stop.set()

    def flush(self, timeout: float = 10.0) -> bool:
        """Send everything pending now and wait for it (for all panels of this connection)."""
        return self.m.flush(timeout) if self.m.connected else True


def _isatty(stream) -> bool:
    try:
        return stream.isatty()
    except Exception:
        return False


_default_monitor: Optional[Monitor] = None


def panel(panel_id: str, *, name: Optional[str] = None, group: Optional[str] = None,
          priority: Optional[bool] = None, stale_after: Optional[int | str] = None,
          url: Optional[str] = None, catch_errors: bool = True) -> Panel:
    """A panel on the dashboard, using the saved login. See Monitor.panel."""
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = Monitor()
        if not _default_monitor.connected:
            print("[monitor] not logged in on this machine (no MONITOR_URL or ~/.config/monitor/client.json): "
                  "printing progress instead of sending it", file=sys.stderr)
    return _default_monitor.panel(panel_id, name=name, group=group, priority=priority,
                                  stale_after=stale_after, url=url, catch_errors=catch_errors)


# ---- uncaught exceptions turn the job's panels red
_watched: list[Panel] = []


def _watch(p: Panel) -> None:
    if not _watched:
        previous = sys.excepthook

        def hook(etype, e, tb):
            if monitor_lite:
                monitor_lite._end_line()
            for q in list(_watched):
                if q._finished or q._reported is e:
                    continue
                if isinstance(e, KeyboardInterrupt):
                    q.stopped("interrupted")
                else:
                    q.error(traceback.format_exception_only(etype, e)[-1].strip()[:500])
            flush_all()
            previous(etype, e, tb)
        sys.excepthook = hook
    _watched.append(p)


_live_monitors: list[Monitor] = []


def flush_all(timeout: float = 10.0) -> bool:
    """Send everything pending, for every connection, and wait for it. True if all sent."""
    ok = True
    for mon in list(_live_monitors):
        if not mon.flush(timeout=timeout):
            ok = False
            if not mon.quiet:
                print("[monitor] some updates could not be sent before exit", file=sys.stderr)
    return ok


_flush_at_exit = atexit.register(flush_all)
