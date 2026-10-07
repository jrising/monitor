"""
Monitor client — push updates from scripts, and run standard checks on this machine.
Standard library only (uses psutil if installed, otherwise `ps`). Copy this one file anywhere.

One-time setup on each machine (token from the dashboard: Edit feeds → Tokens):
    python monitor_client.py login https://monitor.example.org <token>

In a Python job:
    from monitor_client import Monitor
    mon = Monitor()                                   # reads the saved login (or MONITOR_URL / MONITOR_TOKEN)
    run = mon.panel("laptop-ssp", name="SSP ensemble", group="laptop", priority=True)
    run.stage("loading data")
    run.progress(0.1, n_results=24)
    with run.track("calibrating"): ...               # red with the exception if it raises
    run.done()

Wrap a command (e.g. in crontab):
    python monitor_client.py run backup --group laptop --stale-after 26h -- rsync -a ~/Docs nas:/docs

Run the feed list's process/path checks assigned to this machine (`on: <token name>`):
    python monitor_client.py agent            # stays running; answers dashboard clicks within ~15 s
    python monitor_client.py agent --once     # one pass, for cron

Network failures never raise: monitoring must not crash the thing being monitored.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fnmatch
import json
import os
import stat as stat_mod
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

CONFIG_FILE = Path(os.environ.get("MONITOR_CLIENT_CONFIG", Path.home() / ".config" / "monitor" / "client.json"))


def _saved_login() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


class Monitor:
    def __init__(self, url: Optional[str] = None, token: Optional[str] = None,
                 timeout: float = 10.0, quiet: bool = False):
        saved = _saved_login()
        self.url = (url or os.environ.get("MONITOR_URL") or saved.get("url") or "http://localhost:8600").rstrip("/")
        self.token = token or os.environ.get("MONITOR_TOKEN") or saved.get("token") or ""
        self.timeout = timeout
        self.quiet = quiet

    def _request(self, method: str, path: str, body: Optional[dict] = None, raise_errors: bool = False):
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
              url: Optional[str] = None) -> "Panel":
        """Get a panel; it's created on its first update. Definition fields are sent only if given
        (and are ignored for panels defined in the dashboard's feed list, which owns them)."""
        p = Panel(self, panel_id)
        definition = {k: v for k, v in dict(name=name, group=group, priority=priority,
                                             stale_after=stale_after, url=url).items() if v is not None}
        if definition:
            self._request("PUT", f"/api/panels/{panel_id}", definition)
        return p

    def panels(self) -> list[dict]:
        r = self._request("GET", "/api/panels")
        return r["panels"] if r else []


class Panel:
    def __init__(self, monitor: Monitor, panel_id: str):
        self.m = monitor
        self.id = panel_id
        self._hb_stop = threading.Event()

    def update(self, status: Optional[str] = None, *, stage: Optional[str] = None,
               error: Optional[str] = None, progress: Optional[float] = None,
               clear: Optional[list[str]] = None, **stats: Any) -> Optional[dict]:
        """status: green/yellow/red (aliases: ok/running, checking/warn, error/stopped).
        progress: 0..1. Extra keyword args become stats, e.g. n_results=120, loss=0.031."""
        body: dict[str, Any] = {}
        if status is not None: body["status"] = status
        if stage is not None: body["stage"] = stage
        if error is not None: body["error"] = error
        if progress is not None: body["progress"] = max(0.0, min(1.0, float(progress)))
        if stats: body["stats"] = stats
        if clear: body["clear"] = clear
        return self.m._request("PUT", f"/api/panels/{self.id}", body)

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


# ======================================================================= standard checks
# Used by the agent on your machines and by the server for its own host. Read-only; no commands
# beyond `ps` are ever run.

def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(sec: float) -> str:
    sec = int(max(0, sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d: return f"{d}d {h}h"
    if h: return f"{h}h {m}m"
    if m: return f"{m}m"
    return f"{sec}s"


def parse_duration(v) -> Optional[float]:
    if v is None or isinstance(v, (int, float)):
        return v
    v = str(v).strip()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(v[-1:])
    return float(v[:-1]) * mult if mult else float(v)


def _parse_etime(s: str) -> int:
    """ps etime: [[dd-]hh:]mm:ss"""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def _list_processes() -> list[dict]:
    """[{pid, age, rss (bytes), cmd}] for all processes, via psutil or ps."""
    try:
        import psutil  # optional
        now, out = time.time(), []
        for p in psutil.process_iter(["pid", "create_time", "memory_info", "cmdline", "name"]):
            info = p.info
            cmd = " ".join(info.get("cmdline") or []) or (info.get("name") or "")
            mem = info.get("memory_info")
            out.append({"pid": info["pid"], "age": now - (info.get("create_time") or now),
                        "rss": mem.rss if mem else 0, "cmd": cmd, "_proc": p})
        return out
    except ImportError:
        pass
    r = subprocess.run(["ps", "-A", "-o", "pid=,etime=,rss=,args="], capture_output=True, text=True, timeout=20)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            out.append({"pid": int(parts[0]), "age": _parse_etime(parts[1]), "rss": int(parts[2]) * 1024,
                        "cmd": parts[3]})
        except ValueError:
            continue
    return out


def _cpu_percent(procs: list[dict], sample: float = 0.5) -> Optional[float]:
    """Current CPU use (sum over procs, % of one core), sampled over `sample` seconds."""
    if procs and "_proc" in procs[0]:
        for p in procs:
            try: p["_proc"].cpu_percent(None)
            except Exception: pass
        time.sleep(sample)
        total = 0.0
        for p in procs:
            try: total += p["_proc"].cpu_percent(None)
            except Exception: pass
        return total
    if Path("/proc/self/stat").exists():  # Linux
        def ticks(pid):
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                return int(fields[11]) + int(fields[12])  # utime + stime
            except (OSError, IndexError, ValueError):
                return None
        before = {p["pid"]: ticks(p["pid"]) for p in procs}
        time.sleep(sample)
        hz = os.sysconf("SC_CLK_TCK")
        used = sum((ticks(pid) or 0) - b for pid, b in before.items() if b is not None)
        return max(0.0, used / hz / sample * 100)
    # macOS/BSD: ps's %cpu is already a recent average
    r = subprocess.run(["ps", "-o", "pcpu=", "-p", ",".join(str(p["pid"]) for p in procs)],
                       capture_output=True, text=True, timeout=10)
    try:
        return sum(float(x) for x in r.stdout.split())
    except ValueError:
        return None


def check_process(spec: dict) -> dict:
    """Is a process whose command line contains `name` running? Reports how long it has been
    running, CPU and RAM. Options: min_count (default 1), idle_below (CPU % -> yellow)."""
    name = spec["name"]
    me = os.getpid()
    procs = [p for p in _list_processes()
             if name in p["cmd"] and p["pid"] != me and not p["cmd"].startswith("ps -A")]
    need = int(spec.get("min_count", 1))
    if len(procs) < need:
        msg = "not running" if not procs else f"only {len(procs)} running (want {need})"
        return {"status": "red", "error": msg, "stats": {"procs": len(procs)}}
    cpu = _cpu_percent(procs)
    stats = {"running": fmt_duration(max(p["age"] for p in procs)),
             "cpu": f"{cpu:.0f}%" if cpu is not None else None,
             "ram": fmt_bytes(sum(p["rss"] for p in procs))}
    if len(procs) > 1:
        stats["procs"] = len(procs)
    idle = spec.get("idle_below")
    if idle is not None and cpu is not None and cpu < float(idle):
        return {"status": "yellow", "stage": f"idle? cpu {cpu:.0f}%", "stats": stats}
    return {"status": "green", "stage": None, "stats": stats}


def check_path(spec: dict) -> dict:
    """A file: its size and age. A directory: number of files, total size and newest file.
    Options: pattern ("*.nc"), recursive (default true), max_age ("26h": red if nothing newer),
    min_files / min_size (yellow if below)."""
    p = Path(os.path.expanduser(str(spec["path"])))
    try:
        st = p.stat()
    except FileNotFoundError:
        return {"status": "red", "error": f"not found: {spec['path']}"}
    except PermissionError:
        return {"status": "red", "error": f"permission denied: {spec['path']}"}
    now = time.time()
    max_age = parse_duration(spec.get("max_age"))
    if stat_mod.S_ISREG(st.st_mode):
        stats = {"size": fmt_bytes(st.st_size), "modified": fmt_duration(now - st.st_mtime) + " ago"}
        if max_age and now - st.st_mtime > max_age:
            return {"status": "red", "error": f"not modified for {fmt_duration(now - st.st_mtime)}", "stats": stats}
        min_size = spec.get("min_size")
        if min_size is not None and st.st_size < float(min_size):
            return {"status": "yellow", "stage": "smaller than expected", "stats": stats}
        return {"status": "green", "stage": None, "stats": stats}

    pattern = spec.get("pattern")
    recursive = spec.get("recursive", True)
    count = total = 0
    newest = 0.0
    deadline = now + float(spec.get("timeout", 20))
    truncated = False
    for root, dirs, files in os.walk(p):
        if not recursive:
            dirs.clear()
        for f in files:
            if pattern and not fnmatch.fnmatch(f, pattern):
                continue
            try:
                fst = os.stat(os.path.join(root, f))
            except OSError:
                continue
            count += 1
            total += fst.st_size
            newest = max(newest, fst.st_mtime)
        if time.time() > deadline:
            truncated = True
            break
    stats = {"files": f"{count:,}{'+' if truncated else ''}", "size": fmt_bytes(total)}
    if newest:
        stats["newest"] = fmt_duration(now - newest) + " ago"
    if max_age and (not newest or now - newest > max_age):
        return {"status": "red", "error": "no new files for " + (fmt_duration(now - newest) if newest else "ever"),
                "stats": stats}
    min_files = spec.get("min_files")
    if min_files is not None and count < int(min_files):
        return {"status": "yellow", "stage": f"only {count} files", "stats": stats}
    return {"status": "green", "stage": "scan stopped early (large tree)" if truncated else None, "stats": stats}


LOCAL_CHECKS = {"process": check_process, "path": check_path}


def run_local_check(spec: dict) -> dict:
    try:
        res = LOCAL_CHECKS[spec["type"]](spec)
    except KeyError:
        res = {"status": "red", "error": f"unknown check type {spec.get('type')!r}"}
    except Exception as e:
        res = {"status": "red", "error": f"{type(e).__name__}: {e}"[:400]}
    res.setdefault("error", None)
    return res


# ======================================================================= agent
_server_interval: Optional[float] = None


def agent_pass(mon: Monitor) -> tuple[str, list[str]]:
    """Ask the server which of this machine's checks are due, run them, report back."""
    global _server_interval
    info = mon._request("GET", "/api/agent", raise_errors=True)
    _server_interval = info.get("poll_every")
    due = [c for c in info["checks"] if c["run"]]
    if due:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda c: {"id": c["id"], **run_local_check(c["check"])}, due))
        mon._request("POST", "/api/agent/results", {"results": results}, raise_errors=True)
    return info["name"], [c["id"] for c in due]


def run_agent(mon: Monitor, once: bool, interval: Optional[float]) -> int:
    if once:
        try:
            name, ran = agent_pass(mon)
        except RuntimeError as e:
            print(f"[monitor] {e}", file=sys.stderr)
            return 1
        if ran:
            print(f"{time.strftime('%H:%M:%S')} {name}: checked {', '.join(ran)}")
        return 0
    print(f"monitor agent polling {mon.url} (Ctrl-C to stop)")
    while True:
        try:
            name, ran = agent_pass(mon)
            if ran:
                print(f"{time.strftime('%H:%M:%S')} {name}: checked {', '.join(ran)}", flush=True)
        except RuntimeError as e:
            print(f"[monitor] {e}", file=sys.stderr, flush=True)
        except Exception as e:  # keep the agent alive whatever happens
            print(f"[monitor] agent error: {e}", file=sys.stderr, flush=True)
        time.sleep(interval or _server_interval or 15)  # the server suggests an interval


# ======================================================================= CLI
def _cli(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Monitor client")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="save the server URL and this machine's token")
    lg.add_argument("url"); lg.add_argument("token")

    r = sub.add_parser("run", help="run a command, reporting green/red when it finishes")
    r.add_argument("panel")
    r.add_argument("--name"); r.add_argument("--group"); r.add_argument("--priority", action="store_true")
    r.add_argument("--stale-after", help="e.g. 26h: go red if this job hasn't reported in that long")

    s = sub.add_parser("set", help="push a one-off update")
    s.add_argument("panel"); s.add_argument("status", nargs="?")
    s.add_argument("--stage"); s.add_argument("--error"); s.add_argument("--progress", type=float)
    s.add_argument("--stat", action="append", default=[], metavar="KEY=VALUE")

    a = sub.add_parser("agent", help="run this machine's process/path checks from the feed list")
    a.add_argument("--once", action="store_true", help="one pass and exit (for cron)")
    a.add_argument("--interval", type=float, help="seconds between polls (default: what the server suggests)")

    t = sub.add_parser("test", help="try a check locally, e.g. test process train.py / test path ~/data")
    t.add_argument("type", choices=sorted(LOCAL_CHECKS)); t.add_argument("target")

    sub.add_parser("list", help="print the panels this token can see")

    argv = list(sys.argv[1:] if argv is None else argv)
    cmd: list[str] = []
    if "--" in argv:  # everything after -- is the wrapped command
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    args = ap.parse_args(argv)

    if args.cmd == "login":
        mon = Monitor(args.url, args.token)
        try:
            info = mon._request("GET", "/api/agent", raise_errors=True)
        except RuntimeError as e:
            print(f"Couldn't connect: {e}", file=sys.stderr)
            return 1
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({"url": mon.url, "token": mon.token}))
        os.chmod(CONFIG_FILE, 0o600)
        print(f"Connected as '{info['name']}'. Saved to {CONFIG_FILE}")
        return 0

    if args.cmd == "test":
        spec = {"type": args.type, ("name" if args.type == "process" else "path"): args.target}
        print(json.dumps(run_local_check(spec), indent=2))
        return 0

    mon = Monitor()
    if args.cmd == "agent":
        return run_agent(mon, args.once, args.interval)

    if args.cmd == "list":
        for p in mon.panels():
            print(f"{p['status']:6} {p['id']:30} {p.get('stage') or ''} {p.get('error') or ''}")
        return 0

    if args.cmd == "set":
        stats = dict(kv.split("=", 1) for kv in args.stat)
        mon.panel(args.panel).update(args.status, stage=args.stage, error=args.error, progress=args.progress, **stats)
        return 0

    if not cmd:
        ap.error("no command given (use: run PANEL -- cmd args...)")
    p = mon.panel(args.panel, name=args.name, group=args.group, priority=args.priority or None,
                  stale_after=args.stale_after)
    p.update("green", stage=f"running: {' '.join(cmd)[:120]}", clear=["error", "progress"])
    t0 = time.time()
    proc = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    sys.stderr.write(proc.stderr)
    took = fmt_duration(time.time() - t0)
    if proc.returncode == 0:
        p.update("green", stage=f"ok at {time.strftime('%Y-%m-%d %H:%M')}", clear=["error"], took=took)
    else:
        last = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
        p.update("red", stage=f"failed at {time.strftime('%Y-%m-%d %H:%M')}", error=last[:400],
                 took=took, exit=proc.returncode)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(_cli())
