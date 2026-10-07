"""
Process and path checks. These usually run on one of your machines through the agent
(`on: laptop` in the feed list), so their code lives in monitor_client.py, the single file the
agent machines have. Without `on:` they look at the monitor's own host.

To add another machine-side check: write it in monitor_client.py, add it to LOCAL_CHECKS there,
register it here with where="both" (or "machine"), and copy the new monitor_client.py to your machines.
"""
from checks import check
from monitor_client import check_path, check_process


@check("process", required=["name"], where="both", example="""
  - id: laptop-train
    name: train.py
    group: laptop
    on: laptop                 # token name of the machine running the agent
    check: {type: process, name: "train.py"}
    schedule: every 15m
""")
def process(spec):
    """Is a process whose command line contains `name` running? Reports how long, CPU and RAM.
    Options: min_count, idle_below (CPU % under which it turns yellow)."""
    return check_process(spec)


@check("path", required=["path"], where="both", example="""
  - id: laptop-outputs
    name: Model outputs
    group: laptop
    on: laptop
    check: {type: path, path: "~/runs/outputs", pattern: "*.nc", max_age: 6h}
    schedule: every 1h
""")
def path(spec):
    """A file: size and age. A directory: file count, total size, newest file.
    Options: pattern, recursive, max_age (red if nothing newer), min_files, min_size."""
    return check_path(spec)
