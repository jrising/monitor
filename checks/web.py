"""Websites and anything listening on a port (databases, ssh, mail...)."""
import socket
import time

import requests

from checks import check, fail, ok, warn


@check("http", required=["url"], example="""
  - id: my-site
    name: My website
    group: web
    priority: true
    url: https://example.org
    check: {type: http, url: "https://example.org"}
    schedule: every 15m
""")
def http(spec):
    """Fetch a URL. Red on errors or 4xx/5xx. Options: contains (text that must appear),
    expect_status, slow_ms (yellow if slower), method, headers, timeout."""
    t0 = time.time()
    r = requests.request(spec.get("method", "GET"), spec["url"], timeout=spec.get("timeout", 10),
                         headers=spec.get("headers"))
    ms = round((time.time() - t0) * 1000)
    stats = {"http": r.status_code, "latency": f"{ms} ms"}
    expect = spec.get("expect_status")
    good = (r.status_code == expect) if expect else r.status_code < 400
    if not good:
        return fail(f"HTTP {r.status_code}", stats)
    if spec.get("contains") and spec["contains"] not in r.text:
        return fail(f"response lacks {spec['contains']!r}", stats)
    if spec.get("slow_ms") and ms > spec["slow_ms"]:
        return warn(f"slow ({ms} ms)", stats)
    return ok(stats)


@check("tcp", required=["host", "port"], example="""
  - id: my-db
    name: Database
    group: web
    check: {type: tcp, host: mysql.example.org, port: 3306}
    schedule: every 15m
""")
def tcp(spec):
    """Can we open a connection to host:port? (databases, ssh, anything with a port)"""
    t0 = time.time()
    with socket.create_connection((spec["host"], int(spec["port"])), timeout=spec.get("timeout", 5)):
        pass
    return ok({"latency": f"{round((time.time() - t0) * 1000)} ms"})
