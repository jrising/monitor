"""
Check plugins.

Every .py file in this folder is loaded at startup. A file adds check types with the @check decorator:

    from checks import check, ok, warn, fail, http_get

    @check("weather", required=["city"], example='''
      - id: weather-philly
        name: Philadelphia weather
        check: {type: weather, city: Philadelphia}
        schedule: every 1h
    ''')
    def weather(spec):
        \"\"\"Current temperature for a city.\"\"\"
        data = http_get(f"https://wttr.in/{spec['city']}?format=j1").json()
        temp = float(data["current_condition"][0]["temp_C"])
        return ok({"temp": f"{temp:.0f}°C"})

`spec` is the panel's `check:` mapping from the feed list. Return a dict made with ok() / warn() / fail()
(or by hand: status, stage, error, stats, progress). Raising an exception is fine too: the panel turns
red with the exception message. The new type then appears in the dashboard's feed editor, with the
example as its "+ Add" template. See docs/WRITING_CHECKS.md.

A plugin file that fails to import doesn't stop the server; the error shows in the feed editor.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import requests


@dataclass
class CheckType:
    name: str
    run: Callable[[dict], dict]
    required: tuple[str, ...]
    where: str  # "server": run by the monitor; "machine": only via an agent (`on:`); "both"
    doc: str
    example: str
    module: str


REGISTRY: dict[str, CheckType] = {}
LOAD_ERRORS: dict[str, str] = {}


def check(name: str, *, required: Iterable[str] = (), where: str = "server", example: str = ""):
    """Register a check type. `required` keys are validated when the feed list is saved."""
    if where not in ("server", "machine", "both"):
        raise ValueError("where must be 'server', 'machine' or 'both'")

    def deco(fn: Callable[[dict], dict]):
        if name in REGISTRY and REGISTRY[name].module != fn.__module__:
            raise ValueError(f"check type {name!r} already defined in {REGISTRY[name].module}")
        REGISTRY[name] = CheckType(name, fn, tuple(required), where, _dedent(fn.__doc__ or ""),
                                   _dedent(example), fn.__module__)
        return fn
    return deco


# ---- result helpers
def ok(stats: Optional[dict[str, Any]] = None, stage: Optional[str] = None, **extra) -> dict:
    return {"status": "green", "stage": stage, "stats": stats or {}, **extra}


def warn(stage: str, stats: Optional[dict[str, Any]] = None, **extra) -> dict:
    return {"status": "yellow", "stage": stage, "stats": stats or {}, **extra}


def fail(error: str, stats: Optional[dict[str, Any]] = None, **extra) -> dict:
    return {"status": "red", "error": error, "stats": stats or {}, **extra}


def http_get(url: str, *, timeout: float = 15, headers: Optional[dict] = None, **kw) -> requests.Response:
    """GET with a timeout and a polite User-Agent; raises on HTTP errors."""
    h = {"User-Agent": "monitor-dashboard/1.0"}
    h.update(headers or {})
    r = requests.get(url, timeout=timeout, headers=h, **kw)
    r.raise_for_status()
    return r


def thresholds(value: float, spec: dict, label: str = "value") -> Optional[dict]:
    """Shared `below:` / `above:` handling: returns a warn() result if crossed, else None."""
    if spec.get("below") is not None and value < float(spec["below"]):
        return warn(f"{label} below {spec['below']}")
    if spec.get("above") is not None and value > float(spec["above"]):
        return warn(f"{label} above {spec['above']}")
    return None


def _dedent(s: str) -> str:
    lines = s.strip("\n").splitlines()
    indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
    return "\n".join(ln[indent:] for ln in lines).strip("\n")


def load_all() -> None:
    """Import every module in this package (idempotent)."""
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        full = f"{__name__}.{mod.name}"
        if full in sys.modules:
            continue
        try:
            importlib.import_module(full)
        except Exception:
            LOAD_ERRORS[mod.name] = traceback.format_exc(limit=4)
            print(f"[monitor] check plugin {mod.name!r} failed to load:\n{LOAD_ERRORS[mod.name]}", file=sys.stderr)
