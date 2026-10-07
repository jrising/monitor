"""Numbers from other services: JSON APIs and spreadsheets."""
import csv
import io
import re
from typing import Any

from checks import check, fail, http_get, ok, thresholds


def dig(obj: Any, path: str) -> Any:
    """Follow a dotted path into JSON: "data.items.0.price", "results.-1.value"."""
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            if part not in obj:
                raise KeyError(f"no field {part!r} (have: {', '.join(list(obj)[:8])})")
            obj = obj[part]
        else:
            raise KeyError(f"can't look up {part!r} in a {type(obj).__name__}")
    return obj


def _num(v: Any):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[,$%\s£€]", "", str(v)))
    except ValueError:
        return None


@check("json", required=["url", "fields"], example="""
  - id: api-queue
    name: Job queue
    group: servers
    check:
      type: json
      url: "https://api.example.org/status"
      headers: {Authorization: "Bearer ${SECRET_EXAMPLE_KEY}"}   # secrets come from monitor.env
      fields: {queued: "queue.length", workers: "workers.active"}
      above: 100                  # yellow if the first field goes above this
    schedule: every 15m
""")
def json_api(spec):
    """Query a JSON API and show fields from the response. `fields` maps a stat name to a dotted
    path (e.g. data.items.0.price). Options: headers, params, below / above (applied to the first
    field), status_field (a path whose value ok/up/green... sets the light)."""
    data = http_get(spec["url"], headers=spec.get("headers"), params=spec.get("params")).json()
    stats = {name: dig(data, path) for name, path in spec["fields"].items()}
    if spec.get("status_field"):
        state = str(dig(data, spec["status_field"])).lower()
        if state not in ("ok", "up", "green", "healthy", "true", "running", "pass"):
            return fail(f"status: {state}", stats)
    first_name, first_val = next(iter(stats.items()))
    n = _num(first_val)
    if n is not None:
        crossed = thresholds(n, spec, first_name)
        if crossed:
            crossed["stats"] = stats
            return crossed
    return ok(stats)


def last_row(text: str, columns=None, skip_blank: bool = True) -> dict:
    """The last (non-empty) row of CSV text as {header: value}."""
    rows = [r for r in csv.reader(io.StringIO(text))]
    if not rows:
        raise ValueError("empty CSV")
    header, body = rows[0], rows[1:]
    if skip_blank:
        body = [r for r in body if any(c.strip() for c in r)]
    if not body:
        raise ValueError("CSV has a header but no rows")
    row = dict(zip(header, body[-1]))
    if columns:
        missing = [c for c in columns if c not in row]
        if missing:
            raise KeyError(f"no column {missing[0]!r} (have: {', '.join(header[:8])})")
        row = {c: row[c] for c in columns}
    row["rows"] = len(body)
    return row


@check("csv", required=["url"], example="""
  - id: sheet-budget
    name: Grant spending
    group: finance
    check:
      type: csv
      # Google Sheets: File → Share → Publish to web → choose the sheet → CSV, paste the link
      url: "https://docs.google.com/spreadsheets/d/e/XXXX/pub?gid=0&single=true&output=csv"
      columns: [Date, Spent, Remaining]
      value_column: Remaining
      below: 5000                 # yellow if Remaining drops below this
    schedule: every 6h
""")
def csv_last_row(spec):
    """The last row of a CSV file on the web (e.g. a Google Sheet published as CSV), shown as stats.
    Options: columns (which to show; default all, up to 6), value_column + below / above."""
    row = last_row(http_get(spec["url"]).text, spec.get("columns"))
    rows = row.pop("rows")
    stats = dict(list(row.items())[:6])
    stats["rows"] = rows
    col = spec.get("value_column")
    if col:
        n = _num(row.get(col))
        if n is None:
            return fail(f"{col}: {row.get(col)!r} isn't a number", stats)
        crossed = thresholds(n, spec, col)
        if crossed:
            crossed["stats"] = stats
            return crossed
    return ok(stats)
