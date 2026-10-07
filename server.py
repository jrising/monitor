"""
Monitor server: a status dashboard with a push API, scheduled checks, a web feed editor and
per-machine tokens. No check ever runs an arbitrary command.

Settings come from environment variables or a `monitor.env` file next to this script (KEY=VALUE lines):
  MONITOR_PASSWORD   password for the dashboard and editor (required)
  MONITOR_SCHEDULER  "internal" (default: built-in scheduler, needs a long-running process)
                     or "external" (shared hosting: run `python server.py tick` from cron)
  MONITOR_REFRESH    seconds between dashboard refreshes (default 5; use ~30 under CGI)
  MONITOR_AGENT_INTERVAL  seconds between agent check-ins (default 15; use ~60 under CGI)
  MONITOR_TZ         time zone for cron-style schedules, e.g. America/New_York (default: server's)
  MONITOR_TOKEN      optional extra push token with access to every panel (tokens are normally
                     created in the dashboard: Edit feeds → Tokens)
  MONITOR_DATA       folder for the database and feed list (default: data/ next to this file)
  SECRET_*           values that check specs can reference as ${SECRET_NAME} (API keys etc.)

Kinds of check live in checks/ — one file per family; drop a new file there to add one.

Run (VPS / own server):   uvicorn server:app --host 127.0.0.1 --port 8600
Run (DreamHost shared):   CGI, see deploy/dreamhost/ and the README
Due checks from cron:     python server.py tick
"""
from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import datetime as dt
import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load_env_file() -> None:
    f = HERE / "monitor.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

import requests  # noqa: E402
import yaml  # noqa: E402
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import checks  # noqa: E402
from checks import LOAD_ERRORS, REGISTRY  # noqa: E402

checks.load_all()

DATA_DIR = Path(os.environ.get("MONITOR_DATA", HERE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("MONITOR_DB", DATA_DIR / "monitor.db"))
CONFIG_PATH = Path(os.environ.get("MONITOR_CONFIG", DATA_DIR / "monitor.yaml"))
ENV_TOKEN = os.environ.get("MONITOR_TOKEN", "")
PASSWORD = os.environ.get("MONITOR_PASSWORD", "")
SCHED_MODE = os.environ.get("MONITOR_SCHEDULER", "internal").lower()
TZ = None
if os.environ.get("MONITOR_TZ"):
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ["MONITOR_TZ"])
REFRESH_SECONDS = int(os.environ.get("MONITOR_REFRESH", "5"))          # dashboard reload interval
AGENT_INTERVAL = int(os.environ.get("MONITOR_AGENT_INTERVAL", "15"))  # how often agents check in
HISTORY_LEN = 24
SESSION_DAYS = 30
AGENT_OFFLINE_AFTER = 15 * 60  # an agent unseen this long shows its panels as unknown
SLUG = re.compile(r"[A-Za-z0-9._-]+")

# ---------------------------------------------------------------- statuses
# green = good / running fine, yellow = checking / warning, red = stopped / error
STATUS_ALIASES = {
    "green": "green", "ok": "green", "good": "green", "up": "green", "running": "green", "done": "green",
    "yellow": "yellow", "checking": "yellow", "warn": "yellow", "warning": "yellow", "pending": "yellow",
    "red": "red", "error": "red", "stopped": "red", "down": "red", "failed": "red", "fail": "red",
    "grey": "grey", "gray": "grey", "unknown": "grey",
}


def norm_status(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    try:
        return STATUS_ALIASES[str(s).lower()]
    except KeyError:
        raise HTTPException(400, f"unknown status {s!r}; use green/yellow/red (or ok/checking/error)")


# ---------------------------------------------------------------- storage
_lock = threading.RLock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
_db.row_factory = sqlite3.Row
_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS panels (
        id TEXT PRIMARY KEY,
        name TEXT, grp TEXT DEFAULT 'misc', priority INTEGER DEFAULT 0, sort INTEGER DEFAULT 0,
        status TEXT DEFAULT 'grey', stage TEXT, error TEXT, progress REAL, stats TEXT DEFAULT '{}',
        url TEXT, check_spec TEXT, schedule TEXT, stale_after INTEGER,
        poll_requested INTEGER DEFAULT 0, updated_at REAL, checked_at REAL, from_config INTEGER DEFAULT 0,
        agent TEXT
    );
    CREATE TABLE IF NOT EXISTS history (panel_id TEXT, ts REAL, status TEXT, message TEXT);
    CREATE INDEX IF NOT EXISTS history_panel ON history(panel_id, ts);
    CREATE TABLE IF NOT EXISTS tokens (
        name TEXT PRIMARY KEY, hash TEXT UNIQUE, scope TEXT DEFAULT '*', created REAL, last_used REAL
    );
    CREATE TABLE IF NOT EXISTS agents (name TEXT PRIMARY KEY, last_seen REAL);
    CREATE TABLE IF NOT EXISTS login_fails (ip TEXT, ts REAL);
    """
)
_have = {r[1] for r in _db.execute("PRAGMA table_info(panels)")}
for _col in ("agent TEXT", "display TEXT"):  # upgrade databases from older versions
    if _col.split()[0] not in _have:
        _db.execute(f"ALTER TABLE panels ADD COLUMN {_col}")
_db.commit()

STATE_FIELDS = ("status", "stage", "error", "progress", "stats")


def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Locked read: the connection is shared by request handlers and check threads."""
    with _lock:
        return _db.execute(sql, params).fetchall()


def q1(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    rows = q(sql, params)
    return rows[0] if rows else None


def write(sql: str, params: tuple = ()) -> None:
    with _lock:
        _db.execute(sql, params)
        _db.commit()


def _row(panel_id: str) -> Optional[sqlite3.Row]:
    return q1("SELECT * FROM panels WHERE id=?", (panel_id,))


def upsert(panel_id: str, fields: dict[str, Any], merge_stats: bool = True, record: bool = True) -> None:
    """Create or update a panel. Status changes are appended to history."""
    with _lock:
        row = _row(panel_id)
        if row is None:
            _db.execute("INSERT INTO panels (id, name, updated_at) VALUES (?, ?, ?)",
                        (panel_id, fields.get("name") or panel_id, time.time()))
            row = _row(panel_id)
        if "stats" in fields and fields["stats"] is not None and not isinstance(fields["stats"], str):
            stats = json.loads(row["stats"] or "{}") if merge_stats else {}
            stats.update(fields["stats"])
            stats = {k: v for k, v in stats.items() if v is not None}  # send None to delete a stat
            fields["stats"] = json.dumps(stats)
        if any(k in fields for k in STATE_FIELDS):
            fields.setdefault("updated_at", time.time())
            fields.setdefault("poll_requested", 0)  # any update answers a pending poll
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        _db.execute(f"UPDATE panels SET {cols} WHERE id=?", (*fields.values(), panel_id))
        new_status = fields.get("status")
        if new_status and record:
            last = q1("SELECT status FROM history WHERE panel_id=? ORDER BY ts DESC LIMIT 1", (panel_id,))
            msg = fields.get("error") or fields.get("stage") or ""
            if last is None or last["status"] != new_status or msg:
                _db.execute("INSERT INTO history VALUES (?,?,?,?)", (panel_id, time.time(), new_status, msg))
                _db.execute(
                    "DELETE FROM history WHERE panel_id=? AND ts NOT IN "
                    "(SELECT ts FROM history WHERE panel_id=? ORDER BY ts DESC LIMIT ?)",
                    (panel_id, panel_id, HISTORY_LEN))
        _db.commit()


def _agents_seen() -> dict[str, float]:
    return {r["name"]: r["last_seen"] for r in q("SELECT * FROM agents")}


def to_dict(row: sqlite3.Row, full: bool = True, agents: Optional[dict] = None) -> dict:
    d = dict(row)
    d["group"] = d.pop("grp")
    d["priority"] = bool(d["priority"])
    d["poll_requested"] = bool(d["poll_requested"])
    d["from_config"] = bool(d["from_config"])
    d["stats"] = json.loads(d["stats"] or "{}")
    spec = d.pop("check_spec")
    d["check"] = json.loads(spec).get("type") if spec else None
    d["display"] = json.loads(d["display"]) if d.get("display") else {}
    d["stale"] = False
    now = time.time()
    if d["stale_after"] and d["updated_at"] and now - d["updated_at"] > d["stale_after"]:
        d["stale"] = True
        d["status"] = "red"
        d["error"] = f"no update for {_ago(now - d['updated_at'])}"
    elif d["agent"]:
        seen = (agents if agents is not None else _agents_seen()).get(d["agent"])
        if not seen or now - seen > AGENT_OFFLINE_AFTER:
            d["status"] = "grey"
            d["stage"] = f"{d['agent']} agent offline" + (f" (seen {_ago(now - seen)} ago)" if seen else " (never seen)")
            d["error"] = None
    if full:
        d["history"] = [dict(h) for h in q("SELECT ts, status, message FROM history WHERE panel_id=? ORDER BY ts",
                                           (d["id"],))]
    return d


def _ago(sec: float) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= n:
            return f"{int(sec // n)}{unit}"
    return f"{int(sec)}s"


# ---------------------------------------------------------------- running checks
_SECRET = re.compile(r"\$\{(SECRET_[A-Za-z0-9_]+)\}")


def _with_secrets(v):
    """Replace ${SECRET_NAME} in check specs with values from the environment / monitor.env."""
    if isinstance(v, str):
        return _SECRET.sub(lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _with_secrets(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_with_secrets(x) for x in v]
    return v


def machine_types() -> set[str]:
    return {n for n, c in REGISTRY.items() if c.where in ("machine", "both")}


def run_check(panel_id: str) -> None:
    row = _row(panel_id)
    if row is None or not row["check_spec"] or row["agent"]:
        return
    spec = json.loads(row["check_spec"])
    upsert(panel_id, {"status": "yellow", "stage": "checking…", "error": None}, record=False)
    ctype = REGISTRY.get(spec.get("type"))
    try:
        if ctype is None:
            raise ValueError(f"unknown check type {spec.get('type')!r}")
        result = dict(ctype.run(_with_secrets(spec)) or {})
        result["status"] = norm_status(result.get("status") or "grey")
    except HTTPException as e:
        result = {"status": "red", "error": f"check returned a bad result: {e.detail}"}
    except Exception as e:  # any failure of the check itself = red
        result = {"status": "red", "error": f"{type(e).__name__}: {e}"[:500]}
    result = {k: v for k, v in result.items() if k in ("status", "stage", "error", "stats", "progress")}
    if isinstance(result.get("stats"), dict):
        result["stats"] = {k: v for k, v in result["stats"].items() if v is not None}
    result.setdefault("stage", None)
    result.setdefault("error", None)
    result["checked_at"] = time.time()
    upsert(panel_id, result)


# ---------------------------------------------------------------- schedules
_EVERY = re.compile(r"^every\s+(\d+)\s*([smhd])$")


def parse_schedule(sched: str):
    """'every 5m' -> IntervalTrigger; 5-field cron -> CronTrigger. Raises ValueError if invalid."""
    sched = str(sched).strip()
    m = _EVERY.match(sched)
    if m:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return IntervalTrigger(**{unit: int(m.group(1))}, timezone=TZ or dt.timezone.utc)
    if sched.startswith("every"):
        raise ValueError(f"bad schedule {sched!r}: use e.g. 'every 5m', 'every 2h'")
    try:
        return CronTrigger.from_crontab(sched, timezone=TZ) if TZ else CronTrigger.from_crontab(sched)
    except Exception as e:
        raise ValueError(f"bad schedule {sched!r}: {e}") from None


def is_due(row: sqlite3.Row, now: Optional[float] = None) -> bool:
    """Has this panel's schedule come round since its last check?"""
    if not row["check_spec"] or not row["schedule"]:
        return False
    now = now or time.time()
    last = row["checked_at"]
    if last is None:
        return True
    trig = parse_schedule(row["schedule"])
    if isinstance(trig, IntervalTrigger):
        return now - last >= trig.interval.total_seconds() - 30  # slack for cron jitter
    tz = TZ or dt.datetime.now().astimezone().tzinfo
    nxt = trig.get_next_fire_time(None, dt.datetime.fromtimestamp(last + 1, tz))
    return nxt is not None and nxt.timestamp() <= now + 30


scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})


def schedule_panel(panel_id: str) -> None:
    if SCHED_MODE != "internal":
        return
    job_id = f"check:{panel_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    row = _row(panel_id)
    if row is None or not row["check_spec"] or not row["schedule"] or row["agent"]:
        return
    scheduler.add_job(run_check, parse_schedule(row["schedule"]), args=[panel_id], id=job_id,
                      replace_existing=True)


def tick(force: bool = False) -> list[str]:
    """Run every due server-side check (all if force). Called by cron in external mode."""
    due = []
    for r in q("SELECT * FROM panels WHERE check_spec IS NOT NULL AND agent IS NULL"):
        try:
            if force or is_due(r):
                due.append(r["id"])
        except ValueError as e:
            upsert(r["id"], {"status": "red", "error": str(e)})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(run_check, due))
    return due


# ---------------------------------------------------------------- feed list (monitor.yaml)
class ConfigError(ValueError):
    pass


def _seconds(v) -> Optional[int]:
    if v is None or isinstance(v, int):
        return v
    v = str(v).strip()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(v[-1:])
    try:
        return int(v[:-1]) * mult if mult else int(v)
    except ValueError:
        raise ConfigError(f"bad duration {v!r}: use e.g. 90s, 30m, 26h, 2d") from None


PANEL_KEYS = {"id", "name", "group", "priority", "sort", "url", "check", "schedule", "stale_after", "on",
              "embed", "height"}


def parse_config(text: str) -> list[dict]:
    """Validate feed-list text; returns normalized panel definitions or raises ConfigError."""
    try:
        cfg = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML syntax: {e}") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("panels", []), (list, type(None))):
        raise ConfigError("the file must have a top-level `panels:` list")
    out, seen = [], set()
    for i, p in enumerate(cfg.get("panels") or []):
        where = f"panel #{i + 1}"
        if not isinstance(p, dict) or not p.get("id"):
            raise ConfigError(f"{where}: every panel needs an `id`")
        # YAML 1.1 reads a bare `on:` key as the boolean True; treat it as the word it was meant to be
        p = {("on" if k is True else str(k)): v for k, v in p.items()}
        pid = str(p["id"])
        where = f"panel '{pid}'"
        if not SLUG.fullmatch(pid):
            raise ConfigError(f"{where}: id may only contain letters, digits, . _ -")
        if pid in seen:
            raise ConfigError(f"{where}: duplicate id")
        seen.add(pid)
        unknown = set(p) - PANEL_KEYS
        if unknown:
            hint = " (shell checks aren't supported)" if "command" in str(p.get("check")) else ""
            raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}{hint}")
        check = p.get("check")
        if check is not None:
            if not isinstance(check, dict) or check.get("type") not in REGISTRY:
                raise ConfigError(f"{where}: check.type must be one of {', '.join(sorted(REGISTRY))}")
            missing = [k for k in REGISTRY[check["type"]].required if check.get(k) in (None, "")]
            if missing:
                raise ConfigError(f"{where}: {check['type']} check needs {', '.join(missing)}")
        agent = p.get("on")
        if agent is not None:
            agent = str(agent)
            if not check or check["type"] not in machine_types():
                raise ConfigError(f"{where}: `on:` is for checks that run on one of your machines "
                                  f"({', '.join(sorted(machine_types()))})")
            if not SLUG.fullmatch(agent):
                raise ConfigError(f"{where}: `on:` must be a token name")
        if check and REGISTRY[check["type"]].where == "machine" and agent is None:
            raise ConfigError(f"{where}: {check['type']} checks need `on: <machine token name>`")
        display = {}
        if p.get("embed") is not None:
            if not re.match(r"^https://", str(p["embed"])):
                raise ConfigError(f"{where}: embed must be an https:// URL")
            display["embed"] = str(p["embed"])
        if p.get("height") is not None:
            try:
                display["height"] = max(60, min(1200, int(p["height"])))
            except (TypeError, ValueError):
                raise ConfigError(f"{where}: height must be a number of pixels") from None
        if p.get("schedule") is not None:
            if check is None:
                raise ConfigError(f"{where}: a schedule needs a check to run")
            try:
                parse_schedule(p["schedule"])
            except ValueError as e:
                raise ConfigError(f"{where}: {e}") from None
        try:
            stale = _seconds(p.get("stale_after"))
        except ConfigError as e:
            raise ConfigError(f"{where}: {e}") from None
        url = p.get("url")
        if url is not None and not re.match(r"^https?://", str(url)):
            raise ConfigError(f"{where}: url must start with http:// or https://")
        out.append({
            "id": pid, "name": str(p.get("name", pid)), "grp": str(p.get("group", "misc")),
            "priority": int(bool(p.get("priority", False))), "sort": int(p.get("sort", i)),
            "url": url, "check_spec": json.dumps(check) if check else None,
            "schedule": str(p["schedule"]) if p.get("schedule") is not None else None,
            "stale_after": stale, "agent": agent, "from_config": 1,
            "display": json.dumps(display) if display else None,
        })
    return out


def apply_config(panels: list[dict]) -> list[str]:
    """Write definitions to the DB; returns server-side ids whose check changed (to run right away)."""
    changed, seen = [], set()
    for p in panels:
        seen.add(p["id"])
        old = _row(p["id"])
        if (old is None or old["check_spec"] != p["check_spec"] or old["agent"] != p["agent"]) and p["check_spec"]:
            if p["agent"]:
                p = {**p, "checked_at": None}  # agent picks it up on its next poll
            else:
                changed.append(p["id"])
        upsert(p["id"], {k: v for k, v in p.items() if k != "id"})
    with _lock:  # panels removed from the file (but not push-created ones) are dropped
        for r in q("SELECT id FROM panels WHERE from_config=1"):
            if r["id"] not in seen:
                _db.execute("DELETE FROM panels WHERE id=?", (r["id"],))
                _db.execute("DELETE FROM history WHERE panel_id=?", (r["id"],))
                if SCHED_MODE == "internal" and scheduler.get_job(f"check:{r['id']}"):
                    scheduler.remove_job(f"check:{r['id']}")
        _db.commit()
    for p in panels:
        schedule_panel(p["id"])
    return changed


def load_config_file() -> list[str]:
    if not CONFIG_PATH.exists():
        return []
    return apply_config(parse_config(CONFIG_PATH.read_text()))


def _run_soon(ids: list[str]) -> None:
    if SCHED_MODE == "internal":
        for pid in ids:
            threading.Thread(target=run_check, args=(pid,), daemon=True).start()
    elif ids:  # CGI: the process ends with the request, so run them now (checks have timeouts)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(run_check, ids))


_initialized = False


def init() -> None:
    """Load config and start the scheduler (idempotent; also called by the CGI launcher)."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    try:
        load_config_file()
    except ConfigError as e:
        print(f"[monitor] monitor.yaml not loaded: {e}", file=sys.stderr)
    if SCHED_MODE == "internal":
        for r in q("SELECT id FROM panels WHERE check_spec IS NOT NULL AND agent IS NULL"):
            try:
                schedule_panel(r["id"])
            except ValueError as e:
                upsert(r["id"], {"status": "red", "error": str(e)})
            scheduler.add_job(run_check, args=[r["id"]], id=f"startup:{r['id']}")  # check once now
        scheduler.start()


# ---------------------------------------------------------------- auth
# * admin session: the browser, after logging in with MONITOR_PASSWORD. Can do everything.
# * named tokens: one per machine/purpose, created and revoked in the dashboard. A token can report
#   status for panels its scope allows (globs on panel id, e.g. "laptop-*") and run the checks assigned
#   to it with `on: <token name>`. It can never define checks or read other panels.
COOKIE = "monitor_session"
_session_key = hashlib.sha256(f"monitor-session:{PASSWORD}".encode()).digest()
_last_used_written: dict[str, float] = {}


@dataclass
class Who:
    role: str  # "admin" | "token"
    name: str = ""
    scope: list[str] = field(default_factory=lambda: ["*"])

    def allows(self, panel_id: str, agent: Optional[str] = None) -> bool:
        if self.role == "admin":
            return True
        if agent and agent == self.name:
            return True
        return any(fnmatch.fnmatchcase(panel_id, g) for g in self.scope)


def _sign(payload: str) -> str:
    return hmac.new(_session_key, payload.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"admin|{exp}"
    return base64.urlsafe_b64encode(f"{payload}|{_sign(payload)}".encode()).decode()


def valid_session(cookie: Optional[str]) -> bool:
    if not cookie or not PASSWORD:
        return False
    try:
        role, exp, sig = base64.urlsafe_b64decode(cookie.encode()).decode().split("|")
    except Exception:
        return False
    return hmac.compare_digest(sig, _sign(f"{role}|{exp}")) and int(exp) > time.time()


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def who(request: Request) -> Who:
    if not PASSWORD and not ENV_TOKEN and not q1("SELECT 1 FROM tokens"):
        return Who("admin")  # nothing configured at all: open (local testing only)
    if valid_session(request.cookies.get(COOKIE)):
        # cookie-authenticated writes must carry a custom header (blocks cross-site form posts)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-monitor") != "1":
            raise HTTPException(403, "missing X-Monitor header")
        return Who("admin")
    supplied = request.headers.get("x-token") or request.headers.get("authorization", "")
    supplied = supplied[7:].strip() if supplied.lower().startswith("bearer ") else supplied.strip()
    if supplied:
        if ENV_TOKEN and hmac.compare_digest(supplied, ENV_TOKEN):
            return Who("token", "env", ["*"])
        row = q1("SELECT * FROM tokens WHERE hash=?", (_hash_token(supplied),))
        if row:
            now = time.time()
            if now - _last_used_written.get(row["name"], 0) > 60:
                _last_used_written[row["name"]] = now
                write("UPDATE tokens SET last_used=? WHERE name=?", (now, row["name"]))
            return Who("token", row["name"], [s.strip() for s in (row["scope"] or "").split(",") if s.strip()])
    raise HTTPException(401, "log in, or send a valid token")


def admin_only(request: Request) -> Who:
    w = who(request)
    if w.role != "admin":
        raise HTTPException(403, "this needs the dashboard login, not a token")
    return w


def token_only(request: Request) -> Who:
    w = who(request)
    if w.role != "token":
        raise HTTPException(400, "the agent endpoints need a machine token")
    return w


# ---------------------------------------------------------------- API
@contextlib.asynccontextmanager
async def lifespan(_app):
    init()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Monitor", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


class PanelUpdate(BaseModel):
    name: Optional[str] = None
    group: Optional[str] = None
    priority: Optional[bool] = None
    sort: Optional[int] = None
    url: Optional[str] = None
    stale_after: Optional[Union[int, str]] = None
    status: Optional[str] = None
    stage: Optional[str] = None
    error: Optional[str] = None
    progress: Optional[float] = None  # 0..1
    stats: Optional[dict[str, Any]] = None
    replace_stats: bool = False
    clear: Optional[list[str]] = None  # e.g. ["error", "progress"] to blank those fields


class Login(BaseModel):
    password: str


class ConfigText(BaseModel):
    text: str
    dry_run: bool = False


class NewToken(BaseModel):
    name: str
    scope: Optional[str] = None  # globs on panel ids; default "<name>-*"


class AgentResults(BaseModel):
    results: list[dict[str, Any]]


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    if not PASSWORD:
        raise HTTPException(400, "no MONITOR_PASSWORD set on the server")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    now = time.time()
    # kept in the database, not memory: under CGI every request is a new process
    write("DELETE FROM login_fails WHERE ts < ?", (now - 900,))
    if q1("SELECT COUNT(*) AS n FROM login_fails WHERE ip=?", (ip,))["n"] >= 8:
        raise HTTPException(429, "too many attempts; try again in 15 minutes")
    if not hmac.compare_digest(body.password.encode(), PASSWORD.encode()):
        write("INSERT INTO login_fails VALUES (?, ?)", (ip, now))
        time.sleep(1)
        raise HTTPException(401, "wrong password")
    write("DELETE FROM login_fails WHERE ip=?", (ip,))
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, make_session(), max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=https, samesite="strict", path="/")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    try:
        w = who(request)
        role = w.role if w.role == "admin" else None
    except HTTPException:
        role = None
    return {"role": role, "password_set": bool(PASSWORD), "scheduler": SCHED_MODE, "refresh": REFRESH_SECONDS}


@app.get("/api/panels")
def list_panels(w: Who = Depends(who)):
    rows = q("SELECT * FROM panels ORDER BY priority DESC, grp, sort, name")
    agents = _agents_seen()
    return {"now": time.time(),
            "panels": [to_dict(r, agents=agents) for r in rows if w.allows(r["id"], r["agent"])]}


@app.get("/api/panels/{panel_id}")
def get_panel(panel_id: str, w: Who = Depends(who)):
    row = _row(panel_id)
    if row is None or not w.allows(panel_id, row["agent"]):
        raise HTTPException(404, "no such panel")
    return to_dict(row, full=False)


@app.put("/api/panels/{panel_id}")
def update_panel(panel_id: str, u: PanelUpdate, w: Who = Depends(who)):
    if not SLUG.fullmatch(panel_id):
        raise HTTPException(400, "panel id may only contain letters, digits, . _ -")
    row = _row(panel_id)
    if not w.allows(panel_id, row["agent"] if row else None):
        raise HTTPException(403, f"token '{w.name}' may not update '{panel_id}' (its scope: {', '.join(w.scope) or 'none'})")
    data = u.model_dump(exclude_unset=True)
    fields: dict[str, Any] = {}
    if row is None or not row["from_config"]:  # the feed list owns definitions of panels it lists
        for k in ("name", "sort"):
            if k in data:
                fields[k] = data[k]
        if "url" in data:
            if data["url"] and not re.match(r"^https?://", data["url"]):
                raise HTTPException(400, "url must start with http:// or https://")
            fields["url"] = data["url"]
        if "group" in data:
            fields["grp"] = data["group"]
        if "priority" in data:
            fields["priority"] = int(bool(data["priority"]))
        if "stale_after" in data:
            try:
                fields["stale_after"] = _seconds(data["stale_after"])
            except ConfigError as e:
                raise HTTPException(400, str(e))
    for k in ("stage", "error", "progress", "stats"):
        if k in data:
            fields[k] = data[k]
    if "status" in data:
        fields["status"] = norm_status(data["status"])
    for k in data.get("clear") or []:
        if k in STATE_FIELDS:
            fields[k] = "{}" if k == "stats" else None
    upsert(panel_id, fields, merge_stats=not u.replace_stats)
    return to_dict(_row(panel_id), full=False)


@app.post("/api/panels/{panel_id}/poll")
def poll_panel(panel_id: str, w: Who = Depends(who)):
    """Clicking a light. Server-side checks run now; agent checks and push-only panels get a
    poll request that the agent (or a client heartbeat) answers."""
    row = _row(panel_id)
    if row is None or not w.allows(panel_id, row["agent"]):
        raise HTTPException(404, "no such panel")
    if row["check_spec"] and not row["agent"]:
        if SCHED_MODE == "internal":
            upsert(panel_id, {"status": "yellow", "stage": "checking…", "error": None}, record=False)
            threading.Thread(target=run_check, args=(panel_id,), daemon=True).start()
        else:  # shared hosting: don't rely on threads outliving the request
            run_check(panel_id)
        return {"mode": "check"}
    write("UPDATE panels SET poll_requested=1 WHERE id=?", (panel_id,))
    return {"mode": "requested"}


@app.delete("/api/panels/{panel_id}")
def delete_panel(panel_id: str, w: Who = Depends(admin_only)):
    row = _row(panel_id)
    if row is not None and row["from_config"]:
        raise HTTPException(400, "this panel is defined in the feed list; remove it there")
    with _lock:
        _db.execute("DELETE FROM panels WHERE id=?", (panel_id,))
        _db.execute("DELETE FROM history WHERE panel_id=?", (panel_id,))
        _db.commit()
    return {"deleted": panel_id}


# --- agent (runs on your machines; see monitor_client.py agent)
@app.get("/api/agent")
def agent_poll(w: Who = Depends(token_only)):
    """The checks assigned to this token's machine, with which ones to run now."""
    now = time.time()
    with _lock:
        _db.execute("INSERT INTO agents VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
                    (w.name, now))
        _db.commit()
    checks = []
    for r in q("SELECT * FROM panels WHERE agent=? AND check_spec IS NOT NULL", (w.name,)):
        try:
            run = bool(r["poll_requested"]) or r["checked_at"] is None or is_due(r, now)
        except ValueError:
            run = False
        checks.append({"id": r["id"], "check": json.loads(r["check_spec"]), "run": run})
    return {"name": w.name, "checks": checks, "poll_every": AGENT_INTERVAL}


@app.post("/api/agent/results")
def agent_results(body: AgentResults, w: Who = Depends(token_only)):
    done = []
    for res in body.results:
        row = _row(str(res.get("id", "")))
        if row is None or row["agent"] != w.name:
            continue
        fields = {k: res.get(k) for k in ("stage", "error", "stats") if k in res}
        fields["status"] = norm_status(res.get("status") or "grey")
        fields["checked_at"] = time.time()
        upsert(row["id"], fields)
        done.append(row["id"])
    return {"recorded": done}


# --- feed list
@app.get("/api/config")
def get_config(w: Who = Depends(admin_only)):
    text = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else "panels:\n"
    return {"text": text, "path": CONFIG_PATH.name, "scheduler": SCHED_MODE,
            "checks": [{"name": c.name, "doc": c.doc, "example": c.example, "where": c.where,
                        "required": list(c.required)} for c in sorted(REGISTRY.values(), key=lambda c: c.name)],
            "plugin_errors": {k: v.strip().splitlines()[-1] for k, v in LOAD_ERRORS.items()}}


@app.put("/api/config")
def put_config(body: ConfigText, w: Who = Depends(admin_only)):
    try:
        panels = parse_config(body.text)
    except ConfigError as e:
        raise HTTPException(422, str(e))
    known = {r["name"] for r in q("SELECT name FROM tokens")} | ({"env"} if ENV_TOKEN else set())
    unknown_agents = sorted({p["agent"] for p in panels if p["agent"] and p["agent"] not in known})
    note = (f"No token named {', '.join(unknown_agents)} yet — create one under Tokens." if unknown_agents else None)
    if body.dry_run:
        return {"ok": True, "panels": len(panels), "note": note}
    if CONFIG_PATH.exists():
        shutil.copy(CONFIG_PATH, CONFIG_PATH.with_suffix(".yaml.bak"))
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(body.text)
    tmp.replace(CONFIG_PATH)
    changed = apply_config(panels)
    _run_soon(changed)
    return {"ok": True, "panels": len(panels), "checking": changed, "note": note}


@app.post("/api/reload")
def reload_config(w: Who = Depends(admin_only)):
    try:
        changed = load_config_file()
    except ConfigError as e:
        raise HTTPException(422, str(e))
    _run_soon(changed)
    return {"reloaded": True}


# --- tokens
@app.get("/api/tokens")
def list_tokens(w: Who = Depends(admin_only)):
    agents = _agents_seen()
    return {"tokens": [{"name": r["name"], "scope": r["scope"], "created": r["created"],
                        "last_used": r["last_used"], "agent_seen": agents.get(r["name"])}
                       for r in q("SELECT * FROM tokens ORDER BY name")]}


@app.post("/api/tokens")
def create_token(body: NewToken, w: Who = Depends(admin_only)):
    name = body.name.strip()
    if not SLUG.fullmatch(name) or name == "env":
        raise HTTPException(400, "token name: letters, digits, . _ - (e.g. laptop, compute1)")
    if q1("SELECT 1 FROM tokens WHERE name=?", (name,)):
        raise HTTPException(409, f"a token called '{name}' exists; revoke it first to replace it")
    scope = ",".join(s.strip() for s in (body.scope or f"{name}-*").split(",") if s.strip())
    tok = "mon_" + secrets.token_urlsafe(24)
    write("INSERT INTO tokens (name, hash, scope, created) VALUES (?,?,?,?)",
          (name, _hash_token(tok), scope, time.time()))
    return {"name": name, "scope": scope, "token": tok}  # shown once; only the hash is stored


@app.delete("/api/tokens/{name}")
def revoke_token(name: str, w: Who = Depends(admin_only)):
    write("DELETE FROM tokens WHERE name=?", (name,))
    _last_used_written.pop(name, None)
    return {"revoked": name}


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Monitor server utilities")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick", help="run server-side checks that are due (cron, external-scheduler mode)")
    t.add_argument("--all", action="store_true", help="run every check now")
    sub.add_parser("validate", help="check monitor.yaml for errors")
    a = ap.parse_args()
    try:
        if a.cmd == "validate":
            text = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else ""
            print(f"ok: {len(parse_config(text))} panels")
        else:
            load_config_file()  # pick up edits made directly to the file
            ran = tick(force=a.all)
            if ran:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} checked: {', '.join(ran)}")
    except ConfigError as e:
        sys.exit(f"monitor.yaml: {e}")
