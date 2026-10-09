#!/usr/bin/env python3
"""
Report this machine's status to the monitor: up/down, CPU %, RAM %, local IP.

Run it from cron every 5 minutes. If the machine is off (or offline), it stops
reporting and the panel turns red after STALE_AFTER.

    */5 * * * *  /usr/bin/python3 ~/projects/monitor/tools/host_status.py homebox

Needs: the Python client installed (pip install --user -e ~/projects/monitor/clients/python)
and a saved login (monitor-agent login https://monitor.existencia.org mon_xxx) using a
token whose scope covers the panel id, e.g. a token named "homebox".
Linux only (reads /proc). Standard library only.
"""
import os
import socket
import sys
import time

from monitor_client import Monitor

STALE_AFTER = "15m"      # red if no report this long (3 missed cron runs)
WARN_CPU = 90            # yellow above these
WARN_RAM = 90


def cpu_percent(sample=1.0):
    """Whole-machine CPU use over `sample` seconds, from /proc/stat."""
    def read():
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        return idle, sum(vals)
    i1, t1 = read()
    time.sleep(sample)
    i2, t2 = read()
    return round(100 * (1 - (i2 - i1) / max(t2 - t1, 1)), 1)


def ram_percent():
    """Memory in use (excluding reclaimable cache), from /proc/meminfo."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
    return round(100 * (1 - info["MemAvailable"] / info["MemTotal"]), 1)


def local_ip():
    """The address this machine uses to reach the internet (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def uptime():
    with open("/proc/uptime") as f:
        sec = int(float(f.read().split()[0]))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h" if d else f"{h}h {rem // 60}m"


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else socket.gethostname()
    cpu, ram, ip = cpu_percent(), ram_percent(), local_ip()
    stats = {"cpu": f"{cpu}%", "ram": f"{ram}%", "ip": ip or "none", "up": uptime(),
             "load": " ".join(f"{x:.2f}" for x in os.getloadavg())}

    mon = Monitor()
    panel = mon.panel(f"{host}-status", name=host, group="machines",
                      priority=True, stale_after=STALE_AFTER)
    stage = f"up {stats['up']} · {ip or 'no network'}"
    if ip is None:
        panel.warn("no network address", **stats)
    elif cpu > WARN_CPU or ram > WARN_RAM:
        panel.warn(f"high load: CPU {cpu}%, RAM {ram}%", **stats)
    else:
        panel.ok(stage, **stats)
    mon.flush()


if __name__ == "__main__":
    main()
