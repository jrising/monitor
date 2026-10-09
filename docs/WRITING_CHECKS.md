# Writing a new kind of check

A check type is a Python function in a file in `checks/`. The server loads every file there at
startup, so adding one is: write the file, push to GitHub, run `deploy/update.sh` on the server.
The new type then shows up in the dashboard's feed editor (as a **+ Add** button and in the
reference list), and the editor validates its required fields.

## The smallest example

`checks/weather.py`:

```python
from checks import check, http_get, ok, warn


@check("weather", required=["city"], example="""
  - id: weather-philly
    name: Philadelphia
    group: misc
    check: {type: weather, city: Philadelphia, above: 30}
    schedule: every 1h
""")
def weather(spec):
    """Current temperature for a city (wttr.in). Options: above (°C, turns yellow)."""
    data = http_get(f"https://wttr.in/{spec['city']}?format=j1").json()
    temp = float(data["current_condition"][0]["temp_C"])
    stats = {"temp": f"{temp:.0f}°C"}
    if spec.get("above") is not None and temp > spec["above"]:
        return warn("hot", stats)
    return ok(stats)
```

## The contract

* **Input:** `spec` is the panel's `check:` mapping from the feed list, as a dict. `${SECRET_NAME}`
  strings have already been replaced with values from the server's `monitor.env`, so API keys never
  need to be in the feed list.
* **Output:** a dict with any of `status` (green/yellow/red, or ok/warn/error), `stage` (short
  message), `error` (shown in the red box), `stats` (a dict shown as key/value chips; keep values short
  strings or numbers; `None` values are dropped), `progress` (0–1). Build it with the helpers:
  * `ok(stats, stage=None)`, `warn(stage, stats)`, `fail(error, stats)`
* **Errors:** just raise. Any exception turns the panel red with the exception's message, so you don't
  need try/except for "the API was down".
* **Time:** checks run in a thread pool, with a cron job on DreamHost. Keep each under ~30 s and always
  pass a `timeout` to network calls. `http_get(url, ...)` does both, adds a User-Agent and raises on
  HTTP errors.
* **`@check(...)` arguments:**
  * `name`: the `type:` used in the feed list. Must be unique.
  * `required`: keys the editor insists on before saving.
  * `example`: a feed-list entry (YAML, starting `- id:`). It becomes the editor's template, and the
    test suite checks it's valid.
  * `where`: `"server"` (default) runs on the monitor. `"both"`/`"machine"` are for checks that
    can run on your own machines through the agent; see below.
* **Docstring:** its first sentence is shown in the editor's reference. List the options there.

Shared helpers live in `checks/__init__.py`: `thresholds(value, spec, label)` gives every check the
same `below:`/`above:` behaviour, and `checks/data.py` has `dig(obj, "a.b.0.c")` for JSON paths and
`last_row(csv_text)`.

## Testing a check locally

```bash
pip install -r requirements-dev.txt
python -c "from checks import load_all, REGISTRY; load_all(); print(REGISTRY['weather'].run({'city': 'Philadelphia'}))"
pytest -q          # also verifies every check's example is a valid feed entry
```

Or run the server locally (`uvicorn server:app --port 8600`, with `MONITOR_PASSWORD` set), add the
check in the editor and click its light.

GitHub runs the tests on every push (`.github/workflows/tests.yml`).

## Checks that run on your machines

Checks run by the agent (`on: laptop`) execute inside `agent/machine_checks.py` on that machine, a
standard-library-only file the agent imports. To add one:

1. Write the function in `agent/machine_checks.py` and add it to `LOCAL_CHECKS` there. It must use only
   the standard library, read state rather than change it, and never run commands taken from the spec.
2. Register it in `checks/machine.py` with `where="both"` (or `"machine"` if it makes no sense on the
   monitor's host), calling the function from `machine_checks`.
3. `git pull` on your machines and restart their agents.

The agent only runs check types its own copy of `machine_checks.py` knows. That's deliberate: nothing
the server sends can make your laptop run new code.

## Panels that aren't checks

How a panel looks is controlled by display keys in the feed list, stored in the panel's `display`
field. Currently `embed:` (an https URL shown in an iframe) and `height:`. To add another (say, a
sparkline of a stat), accept the key in `PANEL_KEYS`/`parse_config` in `server.py` and render it in
`tileParts()` in `static/index.html`.
