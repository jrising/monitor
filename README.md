# Monitor

A small self-hosted status dashboard. Priority panels show as blocks in a grid, everything else in a
scrolling list. Each panel has a traffic light you can click to check it now, plus stats, a stage
message, a progress bar and errors.

Panels get their status three ways:

1. **Checks the monitor runs itself:** websites, ports/databases, JSON APIs, published spreadsheets,
   stock prices.
2. **Checks an agent runs on your machines:** is a process running (for how long, CPU, RAM), and
   how big or how fresh is a file or folder.
3. **Updates your scripts push:** from Python with `monitor_client.py`, or by wrapping any command.

Nothing ever runs an arbitrary shell command. New kinds of check are small Python files in `checks/`
(see [docs/WRITING_CHECKS.md](docs/WRITING_CHECKS.md)).

```
server.py             web app, API, scheduler; `python server.py tick` for cron
checks/               check types, one file per family; add files here to extend
monitor_client.py     for your machines: push API, agent, CLI (standard library only)
monitor_client.R      the push API for R scripts (source() it)
static/index.html     the dashboard and feed editor
deploy/               DreamHost setup, update script, systemd/launchd files for the agent
tests/                pytest suite (runs on GitHub on every push)
data/                 your feed list and database (not in git)
monitor.env           your password and settings (not in git; see monitor.env.example)
```

## Lights

| light  | meaning |
|--------|---------|
| green  | good / running fine |
| yellow | checking, or a warning (slow site, threshold crossed, idle process) |
| red    | stopped / error, or nothing reported within the panel's `stale_after` |
| grey   | no data yet, or the machine's agent is offline |

Clicking a light runs that panel's check now. For an agent check, the agent picks the click up
within about 15 seconds.

## Getting it onto GitHub

```bash
cd monitor
git remote add origin git@github.com:YOURNAME/monitor.git   # make an empty private repo first
git push -u origin main
```

From then on, edit and push, then run `deploy/update.sh` on the server (below). `data/` and
`monitor.env` are git-ignored, so updates never touch your feeds, history or password.

## Deploying on DreamHost shared hosting

DreamHost shared plans run Python web apps as CGI scripts (Passenger is no longer supported, and
long-running processes aren't allowed). So:

* the app lives in a folder of its own, **outside** the web folder (`~/monitor-app`);
* the web folder (`~/monitor.yourdomain.org`) holds only `monitor.cgi`, a small launcher, and an
  `.htaccess` that sends every URL to it;
* a DreamHost cron job runs the scheduled checks.

Each request starts Python afresh (about a second), which is fine for one person; the dashboard
refreshes every 30 s and agents check in every 60 s in this setup.

1. **Panel → Websites:** add the subdomain (e.g. `monitor.yourdomain.org`) as an ordinary website, and
   turn on its free **Let's Encrypt** certificate.
2. **SSH in once:**
   ```bash
   git clone https://github.com/YOURNAME/monitor.git ~/monitor-app
   bash ~/monitor-app/deploy/dreamhost/setup.sh monitor.yourdomain.org
   ```
   If the panel's web directory for the site isn't `~/monitor.yourdomain.org`, the script finds a
   folder with the site's name elsewhere in your home (e.g. `~/projects/monitor.yourdomain.org`), or
   you can give it as a second argument.
   The script creates the venv (needs Python 3.10+; it finds one or tells you how to get one), creates
   `monitor.env` with a random password it prints once, writes the two web files, removes DreamHost's
   "almost here" page, and tests the app locally and live. It's safe to re-run at any time, and it's
   the first thing to try if something isn't working.
3. **Panel → Advanced → Cron Jobs:** add the command the script prints, every 5 minutes, email off:
   ```bash
   /home/YOU/monitor-app/venv/bin/python3 /home/YOU/monitor-app/server.py tick
   ```
   Each tick runs only the checks whose schedule has come round; schedules shorter than 5 minutes are
   rounded up.
4. Open `https://monitor.yourdomain.org`, log in, click **Edit feeds**.

**Updating later:** `bash ~/monitor-app/deploy/update.sh`. It pulls, installs any new requirements
and checks the feed list still loads. Under CGI there's nothing to restart: the next request runs the
new code.

**Never put the app itself in the web folder.** Anything there can be downloaded, including
`monitor.env` (your password) and the database. `setup.sh` refuses to run if it finds app files there.

## Deploying on a VPS or your own server

Clone it to `/opt/monitor`, make the venv and `monitor.env` as above, set
`MONITOR_SCHEDULER=internal` (the built-in scheduler), and install `deploy/monitor.service`. Put HTTPS
in front, e.g. Caddy: `monitor.yourdomain.org { reverse_proxy 127.0.0.1:8600 }`.

## Monitoring a machine (laptop, compute server)

**Once per machine:**

1. Clone the repo and install the client into the Python your scripts use:
   ```bash
   git clone https://github.com/YOURNAME/monitor.git ~/projects/monitor
   python3 -m pip install --user -e ~/projects/monitor
   ```
   This installs only `monitor_client.py` (standard library, no dependencies) as an *editable*
   install: Python imports it straight from the clone, so a `git pull` updates it everywhere, and
   there's no `sys.path` editing in your scripts. Repeat the `pip install -e` for each Python you run
   jobs with (a conda env, a project venv, the one cron uses). On a machine where you can't install
   anything, copying the single file next to your script also works.
2. In the dashboard: **Edit feeds → Tokens**, name it after the machine (`laptop`) and click
   **New token**. By default it may update panels whose id starts `laptop-`. The dashboard shows a
   ready-to-paste command; the token isn't shown again.
3. Run that command on the machine (`monitor-client` was installed in step 1; if your shell can't
   find it, use `python3 -m monitor_client` instead):
   ```bash
   monitor-client login https://monitor.yourdomain.org mon_xxxxxxxx
   ```
   This saves the URL and token to `~/.config/monitor/client.json`, readable only by you, so scripts
   never contain the token.

**Then, for a Python job,** nothing else needs setting up. The panel appears on its first update:

```python
from monitor_client import Monitor

mon = Monitor()                                         # uses the saved login
run = mon.panel("laptop-ssp", name="SSP ensemble", group="laptop", priority=True, stale_after="2h")
run.stage("loading data")
for i, s in enumerate(scenarios, 1):
    solve(s)
    run.progress(i / len(scenarios), stage=f"scenario {i}/{len(scenarios)}", n_results=i)
run.done(n_results=len(scenarios))

with run.track("writing netCDF"):       # red with the exception message if this raises
    write_outputs()
```

Other calls: `run.warn("slow")` (yellow), `run.error("diverged")` (red), `run.stats(loss=0.03)`.
Network problems are printed and ignored, so monitoring never crashes the job.

**Calling it inside loops is fine.** Updates are sent from a background thread, so calls return
immediately (about 7 µs each, however slow the server). Routine updates (progress, stage, stats) are
combined and sent at most every 5 seconds per panel, latest values winning; a status change, a new
error, completion (`done()`, progress 1) and a panel's first update are sent at once. Anything still
pending is sent when the script exits, or call `mon.flush()`. Change the interval with
`Monitor(min_interval=…)` or `MONITOR_MIN_INTERVAL`; `Monitor(background=False)` sends every call
synchronously.

**For a command or cron job:** wrap it, and it reports success, failure and the last error line:

```bash
0 2 * * *  python3 -m monitor_client run laptop-backup --stale-after 26h -- rsync -a ~/Docs nas:/docs
```

`--stale-after` turns the panel red if the job doesn't report at all in that long (laptop asleep,
cron broken).

**For a job made of several commands,** give each one `--stage` (and `--step K/N` for a progress bar)
and the same panel name, so they show as stages of one job rather than separate panels:

```bash
0 2 * * *  cd ~/jobs && monitor-client run laptop-sync --stage fetch     --step 1/3 -- python fetch.py \
                     && monitor-client run laptop-sync --stage transform --step 2/3 -- python transform.py \
                     && monitor-client run laptop-sync --stage upload    --step 3/3 -- python upload.py
```

The panel shows the running stage, a progress bar and each stage's run time, and reads "all 3 stages
ok at …" when the last one finishes. If a stage fails, the panel goes red with that stage's error.
Stage 1 always starts a fresh run, and later stages never turn a failed run green, so a failure stays
visible until the next run even if you chain with `;` instead of `&&`. The stages can also be
separate cron entries or scripts, as long as they run in order.

**From R:** `source()` the R client from the clone. It uses the same saved login (from
`monitor-client login`) and needs the `curl` and `jsonlite` packages.

```r
source("~/projects/monitor/monitor_client.R")

run <- monitor_panel("laptop-calibration", name = "IAM calibration", group = "laptop",
                     priority = TRUE, stale_after = "2h")
run$catch_errors()            # in Rscript/cron: any uncaught error turns the panel red
run$stage("loading data")
for (i in seq_along(regions)) {
  fit(regions[i])
  run$progress(i / length(regions), stage = paste("region", i), n_results = i)
}
run$track("writing outputs", write_outputs())   # red with the error message if it fails
run$done(n_results = length(regions))
```

The same calls as in Python: `run$ok()`, `run$warn("slow")`, `run$error("diverged")`,
`run$stats(loss = 0.03)`, `run$update(...)`. As in Python, network problems only give a warning, and
updates are throttled the same way, so `run$progress()` can go in a loop (about 20–30 µs a call). R
has no threads, so a send doesn't wait for the server: it completes during your later calls.
`run$done()`, `run$error()`, `run$flush()` and the end of the R session wait (up to 5 s) until
everything is delivered. Set the interval with `monitor_connect(min_interval = …)`, e.g.
`monitor_panel("laptop-x", monitor = monitor_connect(min_interval = 1))`.

**For process and path checks** (no changes to the program being watched), add them in the feed editor
with `on: laptop` (**+ process**, **+ path** templates), then run the agent on that machine:

```bash
monitor-client agent                  # keep running (see deploy/ for launchd/systemd files)
monitor-client agent --once           # or one pass from cron every few minutes
monitor-client test process train.py  # try a check locally first
```

The agent only runs the built-in process/path checks with the settings the feed list gives them. It
can't be told to run commands, and it reads but never changes anything.

## The feed editor

The feed list is YAML, edited in the browser. **+ Add** buttons insert a template for every check type,
and the side panel lists each type's options. **Check** validates, **Save** validates, keeps the
previous version (`data/monitor.yaml.bak`) and runs new checks immediately. Useful keys:

* `priority: true`: a block in the grid instead of a row in the list.
* `schedule: every 15m`, `every 2h`, or cron like `"0 9 * * 1-5"` (in `MONITOR_TZ`).
* `stale_after: 26h`: red if nothing has reported in that long.
* `on: laptop`: run this process/path check on the machine whose token is `laptop`.
* `embed: https://…` and `height: 240`: show a web page in the panel (a Grafana graph, a status page).
  The site must allow being framed.
* `${SECRET_NAME}` inside a check: replaced by `SECRET_NAME=…` from `monitor.env`, so API keys stay out
  of the feed list.

## Security

* **Password** (`MONITOR_PASSWORD`): you, in the browser. Signed HttpOnly cookie for 30 days.
  Required for seeing everything, editing feeds and managing tokens. Login is rate-limited.
* **Machine tokens:** one per machine, revocable in the editor. A token can update and read only panels
  in its scope (`laptop-*`) plus checks assigned to it with `on:`. It can't see other panels, read the
  feed list or define checks. Only a hash is stored on the server.
* There are no shell checks. New check code arrives only through git, by you.
* Serve over HTTPS only. Browser writes need a custom header (stops other sites submitting forms as
  you), and the page can't be framed by other sites.

## API

Scripts authenticate with `Authorization: Bearer <token>`.

| method | path | who | |
|---|---|---|---|
| GET | `/api/panels`, `/api/panels/{id}` | login, token (its scope) | panels |
| PUT | `/api/panels/{id}` | login, token (its scope) | report `status, stage, error, progress, stats{}, clear[]`; for panels not in the feed list also `name, group, priority, url, stale_after` |
| POST | `/api/panels/{id}/poll` | login, token | what clicking a light does |
| GET / POST | `/api/agent`, `/api/agent/results` | token | agent: fetch due checks, report results |
| GET / PUT | `/api/config` | login | feed list (`{"text": …, "dry_run": true}` to validate) |
| GET / POST / DELETE | `/api/tokens` | login | machine tokens |
| GET | `/api/health` | anyone | liveness |

For R, use `monitor_client.R` (above) rather than calling the API directly.
