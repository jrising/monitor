"""
Process and path checks, run on your machines by the agent (monitor_agent.py) and on the monitor's own
host by the server (checks/machine.py). Read-only: nothing beyond `ps` is ever run.
Standard library only (uses psutil if installed, otherwise `ps`).

To add a machine-side check: write it here, add it to LOCAL_CHECKS, register it in checks/machine.py
with where="both" (or "machine"), and update the clones on your machines.
"""
from __future__ import annotations

import fnmatch
import os
import stat as stat_mod
import subprocess
import time
from pathlib import Path
from typing import Optional


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
