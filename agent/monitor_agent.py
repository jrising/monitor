"""
The command-line tool for your machines: the `monitor-agent` command (also installed as
`monitor-client`, its old name). This is separate from the clients in clients/: they report from
inside a program, while this watches things from the outside.

    monitor-agent login https://monitor.example.org <token>   # once per machine; every client uses it
    monitor-agent run laptop-backup --stale-after 26h -- rsync -a ~/Docs nas:/docs
    monitor-agent run laptop-sync --stage fetch --step 1/2 -- python fetch.py &&
    monitor-agent run laptop-sync --stage load  --step 2/2 -- python load.py
    monitor-agent set laptop-x red --error "disk full"         # one-off update
    monitor-agent list                                         # panels this token can see
    monitor-agent agent [--once]     # run this machine's process/path checks from the feed list
    monitor-agent test process train.py                        # try a check locally

Install (with the Python client, which it uses to send updates):
    python3 -m pip install --user -e ~/projects/monitor/clients/python -e ~/projects/monitor/agent
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import monitor_client
except ImportError:  # not installed: use the copy in the same clone
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "python"))
    import monitor_client
from monitor_client import CONFIG_FILE, Monitor, Panel

from machine_checks import LOCAL_CHECKS, fmt_duration, run_local_check

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
def main(argv: Optional[list[str]] = None) -> int:
    try:
        return _main(argv)
    finally:
        monitor_client.flush_all()


def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="monitor-agent", description="Monitor agent and command-line tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="save the server URL and this machine's token")
    lg.add_argument("url"); lg.add_argument("token")

    r = sub.add_parser("run", help="run a command, reporting green/red when it finishes")
    r.add_argument("panel")
    r.add_argument("--name"); r.add_argument("--group"); r.add_argument("--priority", action="store_true")
    r.add_argument("--stale-after", help="e.g. 26h: go red if this job hasn't reported in that long")
    r.add_argument("--stage", metavar="NAME",
                   help="report this command as one stage of a multi-step job on the same panel")
    r.add_argument("--step", metavar="K/N",
                   help="with --stage: this is stage K of N (shows progress; stage 1 starts a fresh run, "
                        "and later stages don't hide an earlier stage's failure)")

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
        mon = Monitor(args.url, args.token, echo=False)
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

    mon = Monitor(echo=False)
    if not mon.connected:
        msg = "not logged in: run `monitor-agent login URL TOKEN` first (or set MONITOR_URL)"
        if args.cmd != "run":
            print(msg, file=sys.stderr)
            return 2
        print(f"[monitor] {msg}; running the command without reporting", file=sys.stderr)
    if args.cmd == "agent":
        return run_agent(mon, args.once, args.interval)

    if args.cmd == "list":
        for p in mon.panels():
            print(f"{p['status']:6} {p['id']:30} {p.get('stage') or ''} {p.get('error') or ''}")
        return 0

    if args.cmd == "set":
        stats = dict(kv.split("=", 1) for kv in args.stat)
        mon.panel(args.panel, catch_errors=False).update(args.status, stage=args.stage, error=args.error, progress=args.progress, **stats)
        return 0

    if not cmd:
        ap.error("no command given (use: run PANEL -- cmd args...)")
    if args.step and not args.stage:
        ap.error("--step needs --stage NAME")
    k = n = None
    if args.step:
        try:
            k, n = (int(x) for x in args.step.split("/"))
            assert 1 <= k <= n
        except (ValueError, AssertionError):
            ap.error("--step must look like 2/3")
    p = mon.panel(args.panel, name=args.name, group=args.group, priority=args.priority or None,
                  stale_after=args.stale_after, catch_errors=False)
    if args.stage:
        return _run_stage(p, cmd, args.stage, k, n)
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


def _run_stage(p: Panel, cmd: list[str], name: str, k: Optional[int], n: Optional[int]) -> int:
    """One stage of a multi-step job. Each stage's run time is kept as a stat named after it."""
    label = f"{name} ({k}/{n})" if k else name
    key = name if name not in ("status", "stage", "error", "progress", "clear") else f"{name} step"
    first = k is None or k == 1
    last = k is None or k == n
    earlier_failed = False
    if not first:  # don't let a later stage paint over an earlier stage's failure in this run
        info = p.m._request("GET", f"/api/panels/{p.id}") or {}
        earlier_failed = info.get("status") == "red" and not info.get("stale")
    if first:
        p.update("green", stage=f"{label}: running", progress=0.0 if k else None,
                 clear=["error", "stats"] + ([] if k else ["progress"]))
    elif earlier_failed:
        p.update(stage=f"{label}: running, after an earlier stage failed")
    else:
        p.update("green", stage=f"{label}: running", progress=(k - 1) / n)

    t0 = time.time()
    proc = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    sys.stderr.write(proc.stderr)
    took = fmt_duration(time.time() - t0)
    when = time.strftime("%Y-%m-%d %H:%M")
    if proc.returncode != 0:
        err = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
        p.update("red", stage=f"{label} failed at {when}", error=f"{name}: {err}"[:400],
                 **{key: f"failed after {took}"})
    elif earlier_failed:
        p.update(stage=f"{label} ok, but an earlier stage failed", **{key: took})
    elif last:
        done = f"all {n} stages ok" if k else f"{name} ok"
        p.update("green", stage=f"{done} at {when}", progress=1.0 if k else None, **{key: took})
    else:
        p.update("green", stage=f"{label} done", progress=k / n, **{key: took})
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
