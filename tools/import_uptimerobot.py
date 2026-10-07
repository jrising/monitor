#!/usr/bin/env python3
"""
Turn your UptimeRobot monitors into feed-list entries for the Monitor dashboard.

    python3 tools/import_uptimerobot.py --api-key ur123456-abcdef... > uptimerobot.yaml

Use a *read-only* API key (UptimeRobot: Integrations & API → API → Read-only API key). The output is
YAML to paste under `panels:` in the dashboard's feed editor (Edit feeds); review it first. Anything
that doesn't translate one-to-one is marked with a NOTE comment.

It tries UptimeRobot's v2 API, then v3. If neither works, save the monitor list yourself (e.g. with
curl, following UptimeRobot's API docs) and run:  --from-json monitors.json
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PORTS = {1: 80, 2: 443, 3: 21, 4: 25, 5: 110, 6: 143}          # v2 port sub_type -> port
V2_TYPES = {1: "HTTP", 2: "KEYWORD", 3: "PING", 4: "PORT", 5: "HEARTBEAT"}
V2_HTTP_METHODS = {1: "HEAD", 2: "GET", 3: "POST", 4: "PUT", 5: "PATCH", 6: "DELETE", 7: "OPTIONS"}


# ---------------------------------------------------------------- fetching
def _http(req: urllib.request.Request) -> dict:
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_v2(key: str) -> list[dict]:
    out, offset = [], 0
    while True:
        data = urllib.parse.urlencode({"api_key": key, "format": "json", "limit": 50, "offset": offset,
                                       "custom_http_headers": 1}).encode()
        req = urllib.request.Request("https://api.uptimerobot.com/v2/getMonitors", data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Cache-Control": "no-cache"})
        r = _http(req)
        if r.get("stat") != "ok":
            raise RuntimeError(f"v2 API: {r.get('error') or r}")
        out += r.get("monitors", [])
        pg = r.get("pagination") or {}
        offset += pg.get("limit", 50)
        if offset >= pg.get("total", 0):
            return out


def fetch_v3(key: str) -> list[dict]:
    out, url = [], "https://api.uptimerobot.com/v3/monitors?limit=100"
    while url:
        r = _http(urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}))
        items = r if isinstance(r, list) else r.get("data") or r.get("monitors") or []
        out += items
        nxt = None if isinstance(r, list) else (r.get("nextLink") or r.get("next") or (r.get("links") or {}).get("next"))
        url = urllib.parse.urljoin(url, nxt) if nxt else None
    return out


# ---------------------------------------------------------------- converting
def _get(m: dict, *names, default=None):
    """Field by any of its spellings (v2 snake_case, v3 camelCase)."""
    for n in names:
        if m.get(n) not in (None, ""):
            return m[n]
    return default


def normalize(m: dict) -> dict:
    t = _get(m, "type", "monitorType")
    t = V2_TYPES.get(t, str(t)).upper() if isinstance(t, int) else str(t or "").upper()
    status = _get(m, "status")
    paused = status == 0 or str(status).upper() in ("PAUSED", "0")
    kw_type = _get(m, "keyword_type", "keywordType")
    kw_absent = kw_type == 2 or "NOT" in str(kw_type).upper()
    method = _get(m, "http_method", "httpMethod", "httpMethodType")
    if isinstance(method, int):
        method = V2_HTTP_METHODS.get(method)
    port = _get(m, "port")
    sub = _get(m, "sub_type", "subType")
    if not port and isinstance(sub, int) and sub in PORTS:
        port = PORTS[sub]
    headers = _get(m, "custom_http_headers", "customHttpHeaders")
    if isinstance(headers, list):  # [{"name": ..., "value": ...}] in some versions
        headers = {h.get("name"): h.get("value") for h in headers if isinstance(h, dict)}
    return {
        "name": str(_get(m, "friendly_name", "friendlyName", "name", default="monitor")),
        "url": str(_get(m, "url", default="")),
        "type": t, "paused": paused,
        "interval": int(_get(m, "interval", default=300) or 300),
        "timeout": _get(m, "timeout"),
        "keyword": _get(m, "keyword_value", "keywordValue"), "keyword_absent": kw_absent,
        "port": int(port) if str(port or "").isdigit() else None,
        "method": method, "headers": headers or None,
        "auth": bool(_get(m, "http_username", "httpUsername", "authUsername")),
    }


def slug(name: str, taken: set) -> str:
    base = re.sub(r"^https?://", "", name.lower())
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:40] or "site"
    s, i = base, 2
    while s in taken:
        s, i = f"{base}-{i}", i + 1
    taken.add(s)
    return s


def schedule(seconds: int) -> str:
    minutes = max(5, round(seconds / 60))  # the dashboard's checks run at most every ~5 min on DreamHost
    return f"every {minutes // 60}h" if minutes % 60 == 0 else f"every {minutes}m"


def q(v) -> str:
    return json.dumps(v, ensure_ascii=False)  # a JSON string is valid YAML


def convert(monitors: list[dict], group: str = "web", priority: bool = False) -> str:
    taken: set = set()
    lines, notes = [], 0
    for raw in monitors:
        m = normalize(raw)
        pid = slug(m["name"], taken)
        entry = [f"  - id: {pid}", f"    name: {q(m['name'])}", f"    group: {group}"]
        if priority:
            entry.append("    priority: true")
        comments = []
        url = m["url"]
        if m["type"] in ("HTTP", "KEYWORD", "HTTPS"):
            check = {"type": "http", "url": url}
            if m["type"] == "KEYWORD" and m["keyword"]:
                check["lacks" if m["keyword_absent"] else "contains"] = m["keyword"]
            if m["method"] in ("HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                check["method"] = m["method"]
            if m["headers"]:
                check["headers"] = m["headers"]
                comments.append("NOTE: custom headers copied; move any secrets into monitor.env as ${SECRET_...}")
            if m["timeout"]:
                check["timeout"] = int(m["timeout"])
            if m["auth"]:
                comments.append("NOTE: this monitor used HTTP basic auth, which isn't copied; add an Authorization header")
            if re.match(r"^https?://", url):
                entry.append(f"    url: {q(url)}")
            entry.append("    check: " + _flow(check))
            entry.append(f"    schedule: {schedule(m['interval'])}")
        elif m["type"] == "PORT":
            host = re.sub(r"^\w+://", "", url).split("/")[0].split(":")[0]
            entry.append("    check: " + _flow({"type": "tcp", "host": host, "port": m["port"] or 80}))
            entry.append(f"    schedule: {schedule(m['interval'])}")
        elif m["type"] == "PING":
            host = re.sub(r"^\w+://", "", url).split("/")[0]
            comments.append("NOTE: UptimeRobot pinged this host. Ping isn't possible from shared hosting, so this")
            comments.append("      checks that port 443 answers instead; change the port (e.g. 22, 80) if needed")
            entry.append("    check: " + _flow({"type": "tcp", "host": host, "port": 443}))
            entry.append(f"    schedule: {schedule(m['interval'])}")
        elif m["type"] == "HEARTBEAT":
            entry.append(f"    stale_after: {m['interval'] * 2 // 60}m")
            comments.append("NOTE: was a heartbeat. Make the job report here instead of pinging UptimeRobot, e.g.")
            comments.append(f"      monitor-client run {pid} -- <your command>     (or curl, see README)")
            comments.append(f"      and give its machine's token access to '{pid}' (or rename the id to e.g. laptop-...)")
        else:
            comments.append(f"NOTE: UptimeRobot type {m['type'] or '?'} has no equivalent here yet; left without a check")
        if m["paused"]:
            comments.append("NOTE: paused in UptimeRobot; delete this entry if you don't need it")
        notes += any(c.startswith("NOTE") for c in comments)
        lines += [f"  # {c}" for c in comments] + entry + [""]
    head = [f"# Imported from UptimeRobot: {len(monitors)} monitors"
            + (f", {notes} with NOTEs to review" if notes else "") + ".",
            "# Paste these under `panels:` in Edit feeds, then Check and Save.", ""]
    return "\n".join(head + lines)


def _flow(d: dict) -> str:
    """{type: http, url: "..."} on one line."""
    parts = []
    for k, v in d.items():
        if isinstance(v, dict):
            v = "{" + ", ".join(f"{q(a)}: {q(b)}" for a, b in v.items()) + "}"
        elif isinstance(v, str) and k != "type":
            v = q(v)
        parts.append(f"{k}: {v}")
    return "{" + ", ".join(parts) + "}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--api-key", help="UptimeRobot read-only API key (or env UPTIMEROBOT_API_KEY)")
    ap.add_argument("--from-json", metavar="FILE", help="convert a saved getMonitors/monitors response instead")
    ap.add_argument("--group", default="web", help="group for the panels (default: web)")
    ap.add_argument("--priority", action="store_true", help="make them all priority panels")
    a = ap.parse_args(argv)
    import os
    if a.from_json:
        data = json.load(open(a.from_json))
        monitors = data if isinstance(data, list) else data.get("monitors") or data.get("data") or []
    else:
        key = a.api_key or os.environ.get("UPTIMEROBOT_API_KEY")
        if not key:
            ap.error("give --api-key (a read-only key is enough) or --from-json")
        errors = []
        for fetch in (fetch_v2, fetch_v3):
            try:
                monitors = fetch(key)
                print(f"Fetched {len(monitors)} monitors with the {fetch.__name__[-2:]} API.", file=sys.stderr)
                break
            except (urllib.error.URLError, RuntimeError, ValueError, KeyError) as e:
                detail = e.read().decode(errors="replace")[:200] if isinstance(e, urllib.error.HTTPError) else e
                errors.append(f"{fetch.__name__[-2:]}: {detail}")
        else:
            print("Couldn't read your monitors:\n  " + "\n  ".join(errors)
                  + "\nCheck the key, or save the list as JSON and use --from-json.", file=sys.stderr)
            return 1
    print(convert(monitors, a.group, a.priority))
    return 0


if __name__ == "__main__":
    sys.exit(main())
