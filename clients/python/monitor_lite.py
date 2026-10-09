"""
monitor_lite: progress reporting that prints to the terminal, with the same calls as the Monitor
client. One file, standard library only: copy it into code you share.

    from monitor_lite import panel

    run = panel("laptop-calibration")
    run.stage("loading data")
    for i, r in enumerate(regions, 1):
        fit(r)
        run.progress(i / len(regions))
    run.done()

If the full client (monitor_client, from github.com/jrising/monitor) is installed, panel() hands over
to it, so the same code reports to your dashboard on your machines and prints progress everywhere else.

Output goes to stderr: on a terminal, a progress bar redrawn in place; in a log file, a line per stage
and every 10%.
"""
from __future__ import annotations

import contextlib
import shutil
import sys
import time
import traceback
from typing import Any, Optional

STATUS_ALIASES = {
    "green": "green", "ok": "green", "good": "green", "up": "green", "running": "green", "done": "green",
    "yellow": "yellow", "checking": "yellow", "warn": "yellow", "warning": "yellow", "pending": "yellow",
    "red": "red", "error": "red", "stopped": "red", "down": "red", "failed": "red", "fail": "red",
    "grey": "grey", "gray": "grey", "unknown": "grey",
}


def panel(panel_id: str, *, name: Optional[str] = None, group: Optional[str] = None,
          priority: Optional[bool] = None, stale_after: Optional[int | str] = None,
          url: Optional[str] = None, catch_errors: bool = True):
    """A progress reporter. Uses the Monitor client when it's installed, otherwise prints."""
    try:
        import monitor_client
    except ImportError:
        monitor_client = None
    if monitor_client is not None and getattr(monitor_client, "MONITOR_CLIENT_API", 0) >= 2:
        return monitor_client.panel(panel_id, name=name, group=group, priority=priority,
                                    stale_after=stale_after, url=url, catch_errors=catch_errors)
    return Panel(panel_id, name=name)


def _fmt(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def _isatty(stream) -> bool:
    try:
        return stream.isatty()
    except Exception:
        return False


_open_line: list = []  # the panel whose progress bar is on the current terminal line, if any


def _end_line() -> None:
    if _open_line:
        p = _open_line.pop()
        p._out.write("\n")
        p._out.flush()


class Panel:
    """Prints a panel's updates. Accepts everything the Monitor client's Panel does."""

    def __init__(self, panel_id: str, *, name: Optional[str] = None, stream=None, **_definition: Any):
        self.id = panel_id
        self.label = name or panel_id
        self._out = stream if stream is not None else sys.stderr
        self._tty = _isatty(self._out)
        self._t0 = time.monotonic()
        self._stage: Optional[str] = None
        self._progress: Optional[float] = None
        self._stats: dict[str, Any] = {}
        self._drawn = 0.0
        self._decile = -1
        _install_hook()

    # ---- output
    def _line(self, text: str) -> None:
        _end_line()
        self._out.write(f"[{self.label}] {text}\n")
        self._out.flush()

    def _stats_text(self) -> str:
        if not self._stats:
            return ""
        items = list(self._stats.items())[:4]
        return " (" + ", ".join(f"{k}={v}" for k, v in items) + ")"

    def _bar(self, force: bool) -> None:
        frac = self._progress or 0.0
        elapsed = time.monotonic() - self._t0
        timing = _fmt(elapsed)
        if 0.02 < frac < 1:
            timing += f", ~{_fmt(elapsed * (1 - frac) / frac)} left"
        stage = f"  {self._stage}" if self._stage else ""
        if self._tty:
            now = time.monotonic()
            if not force and now - self._drawn < 0.1:
                return
            self._drawn = now
            width = 20
            filled = int(round(frac * width))
            text = (f"[{self.label}] {frac * 100:3.0f}% |{'█' * filled}{' ' * (width - filled)}| "
                    f"{timing}{stage}{self._stats_text()}")
            cols = shutil.get_terminal_size((100, 20)).columns
            if _open_line and _open_line[0] is not self:
                _end_line()
            self._out.write("\r" + text[:max(20, cols - 1)] + "\x1b[K")
            self._out.flush()
            if not _open_line:
                _open_line.append(self)
        else:
            decile = int(frac * 10 + 1e-9)
            if force or decile > self._decile:
                self._decile = decile
                self._line(f"{frac * 100:.0f}%, {timing}{stage}{self._stats_text()}")

    # ---- the client interface
    def update(self, status: Optional[str] = None, *, stage: Optional[str] = None,
               error: Optional[str] = None, progress: Optional[float] = None,
               clear: Optional[list[str]] = None, **stats: Any) -> None:
        status = STATUS_ALIASES.get(str(status).lower(), status) if status is not None else None
        for k in clear or []:
            if k == "stage": self._stage = None
            elif k == "progress": self._progress = None; self._decile = -1
            elif k == "stats": self._stats = {}
        if stats:
            self._stats.update(stats)
            self._stats = {k: v for k, v in self._stats.items() if v is not None}
        new_stage = stage is not None and stage != self._stage
        if stage is not None:
            self._stage = stage
        if status == "red":
            self._line(f"ERROR: {error or stage or 'failed'}")
            return
        if error is not None:
            self._line(f"error: {error}")
        if progress is not None:
            self._progress = max(0.0, min(1.0, float(progress)))
            # done() is the update that sets progress 1 and clears the error; progress(1.0) alone isn't
            if self._progress >= 1.0 and "error" in (clear or []):
                self._line(f"{stage or 'done'} after {_fmt(time.monotonic() - self._t0)}{self._stats_text()}")
                return
        if status == "yellow" and new_stage:
            self._line(f"warning: {stage}")
        elif progress is not None:
            self._bar(force=new_stage or self._progress >= 1.0)
        elif new_stage:
            self._line(stage)

    def ok(self, stage: Optional[str] = None, **stats):
        return self.update("green", stage=stage, clear=["error"], **stats)

    def stage(self, msg: str, **stats):
        """Report the current step; the job is running fine."""
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
        """with panel.track("fitting model"): ...  -> reports the error if it raises."""
        self.stage(stage)
        try:
            yield self
        except BaseException as e:
            msg = traceback.format_exception_only(type(e), e)[-1].strip()
            self.error(f"{stage}: {msg}"[:500])
            raise
        else:
            self.ok(done_msg or f"{stage} ✓")

    def heartbeat(self, interval: float = 60, on_poll=None) -> "Panel":
        return self

    def stop_heartbeat(self) -> None:
        pass

    def flush(self, timeout: float = 10.0) -> bool:
        return True


_hooked = False


def _install_hook() -> None:
    """End a half-drawn progress line before Python prints a traceback or exits."""
    global _hooked
    if _hooked:
        return
    _hooked = True
    previous = sys.excepthook

    def hook(*exc):
        _end_line()
        previous(*exc)
    sys.excepthook = hook
    import atexit
    atexit.register(_end_line)
