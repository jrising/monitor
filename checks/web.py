"""Websites and anything listening on a port (databases, ssh, mail...)."""
import re
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
    """Fetch a URL. Red on errors or 4xx/5xx. Options: contains (text that must appear), lacks (text
    that must not), expect_status, slow_ms (yellow if slower), method, headers, timeout, retries
    (default 1: a failure is re-checked once, 5 s later, before the panel turns red)."""
    tries = 1 + int(spec.get("retries", 1))
    for attempt in range(tries):
        try:
            result = _http_once(spec)
        except requests.RequestException as e:
            result = fail(_describe(e, spec))
        if result["status"] != "red" or attempt == tries - 1:
            return result
        time.sleep(5)


def _describe(e: Exception, spec: dict) -> str:
    """A short, readable reason instead of urllib3's nested exception text."""
    text = str(e)
    if isinstance(e, requests.exceptions.SSLError):
        m = re.search(r"certificate verify failed: ([^(')\]]+)", text)
        return "SSL certificate problem" + (f": {m.group(1).strip()}" if m else "")
    if isinstance(e, requests.exceptions.Timeout):
        return f"no response within {spec.get('timeout', 15)} s"
    if isinstance(e, requests.exceptions.ConnectionError):
        if re.search(r"Name or service not known|nodename nor servname|Failed to resolve|getaddrinfo failed", text):
            return "DNS lookup failed (domain doesn't resolve)"
        if "Connection refused" in text:
            return "connection refused"
        if re.search(r"Connection reset|RemoteDisconnected|aborted", text):
            return "connection dropped by the server"
        return "couldn't connect"
    return f"{type(e).__name__}: {text}"[:300]


def _http_once(spec):
    t0 = time.time()
    r = requests.request(spec.get("method", "GET"), spec["url"], timeout=spec.get("timeout", 15),
                         headers=spec.get("headers"))
    ms = round((time.time() - t0) * 1000)
    stats = {"http": r.status_code, "latency": f"{ms} ms"}
    expect = spec.get("expect_status")
    good = (r.status_code == expect) if expect else r.status_code < 400
    if not good:
        return fail(f"HTTP {r.status_code}", stats)
    if spec.get("contains") and spec["contains"] not in r.text:
        return fail(f"response lacks {spec['contains']!r}", stats)
    if spec.get("lacks") and spec["lacks"] in r.text:
        return fail(f"response contains {spec['lacks']!r}", stats)
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
    """Can we open a connection to host:port? (databases, ssh, anything with a port)
    Options: timeout, retries (default 1, as for http)."""
    tries = 1 + int(spec.get("retries", 1))
    for attempt in range(tries):
        t0 = time.time()
        try:
            with socket.create_connection((spec["host"], int(spec["port"])), timeout=spec.get("timeout", 5)):
                pass
            return ok({"latency": f"{round((time.time() - t0) * 1000)} ms"})
        except OSError as e:
            if attempt == tries - 1:
                return fail(f"{type(e).__name__}: {e}"[:300])
            time.sleep(5)
