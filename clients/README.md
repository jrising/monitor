# Clients

A client lets a program report its own progress to the dashboard. There is one folder per language,
and every language provides the same two files and the same calls, so code moves between them easily
and a new language (Julia next) has a clear target.

```
python/monitor_client.py   python/monitor_lite.py   python/pyproject.toml (pip install -e)
R/monitor_client.R         R/monitor_lite.R
```

Watching things from the *outside* (wrapping a command, process and file checks) is not a client's job:
that's the agent and command-line tool in [`../agent/`](../agent/monitor_agent.py), in Python only.

## The two files

**`monitor_client`** sends updates to the dashboard. Its only setup is the machine's login
(`monitor-agent login URL TOKEN`, saved in `~/.config/monitor/client.json`, or `MONITOR_URL` and
`MONITOR_TOKEN`). With no login, or `MONITOR_URL=off`, it sends nothing and prints progress instead.

**`monitor_lite`** has the same calls but only prints, with no dependencies. It's the file to copy into
code you share. When the full client is available it hands over to it, so the same code reports to the
dashboard on your machines and prints progress everywhere else:

| language | lite uses the full client when |
|----------|-------------------------------|
| Python   | `monitor_client` is importable (installed with `pip install -e clients/python`) |
| R        | `MONITOR_R_CLIENT` names `monitor_client.R` (e.g. in `~/.Renviron`) |

## The interface

The short form is the common case, and needs no options:

```
run = panel("laptop-calibration")        # R: monitor_panel("laptop-calibration")
run.stage("loading data")
for i in 1..n:  work(i);  run.progress(i / n)
run.done()
```

**`panel(id, name?, group?, priority?, stale_after?, url?, catch_errors = true)`**: id is
`letters, digits . _ -`; a machine's token may update ids starting with its own name (`laptop-…`).
Everything else is optional and only sent if given. Left out: the name is the id; the group is the part
of the id before the first `-` (the server applies this when it first sees the panel); not a priority
block; never stale.

| call | status | effect |
|------|--------|--------|
| `stage(msg, **stats)` | green | the current step; clears any error |
| `progress(frac, stage?, **stats)` | green | progress bar, 0..1 |
| `done(msg = "done", **stats)` | green | progress 1, clears the error; sends at once |
| `warn(msg, **stats)` | yellow | |
| `error(msg, **stats)` | red | sends at once |
| `stopped(msg = "stopped")` | red | |
| `ok(stage?, **stats)`, `stats(**stats)` | green / unchanged | |
| `update(status?, stage?, error?, progress?, clear?, **stats)` | | everything else is built on this |
| `track(step, body)` | | runs body as a step; red with `step: error` if it fails, then re-raises |
| `flush()` | | send everything pending now |

Extra named arguments are stats shown on the panel (`n_results = 12`, `loss = 0.03`).

**Behaviour every client must have:**

* **Never fail the program.** Network errors are reported as a warning and otherwise ignored.
* **Cheap in loops.** Routine updates (progress, stage, stats) are merged, latest values winning, and
  sent at most every 5 s per panel (`MONITOR_MIN_INTERVAL`). Sent at once: a panel's first update, a
  status change, a new error, completion. Merging must have the same effect as sending each update in
  order (the tests check this against the server). Anything pending is sent when the program exits.
* **Uncaught errors turn the job red.** With `catch_errors` (the default), an error that ends the program
  turns every panel it created red with the error message, except panels already `done()` and errors
  `track()` already reported with their step name.
* **Console output** (the lite file, and the full client when not logged in, or alongside sending when
  run interactively): on stderr; `[id] message` lines for stages, warnings, errors and completion
  (`done after 4m 12s (n_results=12)`); progress as a bar redrawn in place on a terminal, otherwise a
  line every 10 %.

The API underneath is one call: `PUT /api/panels/{id}` with a JSON body of `status, stage, error,
progress, stats{}, clear[]` (plus `name, group, priority, url, stale_after` to define the panel), and
`Authorization: Bearer <token>` (also sent as `X-Token`, which Apache CGI doesn't strip).
