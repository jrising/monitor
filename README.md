# Monitor

A small self-hosted status dashboard. Priority panels show as blocks in a grid, everything else in a
scrolling list. Each panel has a traffic light you can click to check it now, plus stats, a stage
message, a progress bar and errors.

Panels get their status three ways:

1. **Checks the monitor runs itself:** websites, ports/databases, JSON APIs, published spreadsheets,
   stock prices.
2. **Checks an agent runs on your machines:** is a process running (for how long, CPU, RAM), and
   how big or how fresh is a file or folder.
3. **Updates your scripts push:** from Python or R with a client (`clients/`), or by wrapping any
   command.

Nothing ever runs an arbitrary shell command. New kinds of check are small Python files in `checks/`
(see [docs/WRITING_CHECKS.md](docs/WRITING_CHECKS.md)).

```
server.py             web app, API, scheduler; `python server.py tick` for cron
checks/               check types, one file per family; add files here to extend
clients/              report progress from inside a program: one folder per language (python/, R/),
                      each with a full client and a print-only drop-in (see clients/README.md)
agent/                for your machines: the agent (process/path checks) and the command-line tool
static/index.html     the dashboard and feed editor
deploy/               DreamHost setup, update script, systemd/launchd files for the agent
notify.py             sending alert emails
tools/                one-off helpers, e.g. importing monitors from UptimeRobot
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

1. Clone the repo and install the Python client and the command-line tool into the Python your scripts
   use:
   ```bash
   git clone https://github.com/YOURNAME/monitor.git ~/projects/monitor
   python3 -m pip install --user -e ~/projects/monitor/clients/python -e ~/projects/monitor/agent
   ```
   Both are standard library only, and *editable* installs: Python imports them straight from the
   clone, so a `git pull` updates them everywhere. Repeat for each Python you run jobs with (a conda
   env, a project venv, the one cron uses). For R, nothing to install beyond the `curl` and `jsonlite`
   packages; add `MONITOR_R_CLIENT=~/projects/monitor/clients/R/monitor_client.R` to `~/.Renviron` if
   you use `monitor_lite.R` in shared code (below).
2. In the dashboard: **Edit feeds → Tokens**, name it after the machine (`laptop`) and click
   **New token**. By default it may update panels whose id starts `laptop-`. The dashboard shows a
   ready-to-paste command; the token isn't shown again.
3. Run that command on the machine (if your shell can't find `monitor-agent`, use
   `python3 -m monitor_agent`):
   ```bash
   monitor-agent login https://monitor.yourdomain.org mon_xxxxxxxx
   ```
   This saves the URL and token to `~/.config/monitor/client.json`, readable only by you, so scripts
   never contain the token. All the clients use it.

### From inside a program: the clients

**Python:**

```python
from monitor_client import panel

run = panel("laptop-ssp")
run.stage("loading data")
for i, s in enumerate(scenarios, 1):
    solve(s)
    run.progress(i / len(scenarios))
run.done()
```

**R:**

```r
source("~/projects/monitor/clients/R/monitor_client.R")

run <- monitor_panel("laptop-calibration")
run$stage("loading data")
for (i in seq_along(regions)) {
  fit(regions[i])
  run$progress(i / length(regions))
}
run$done()
```

That's all a job needs. The panel appears on its first update, named after its id, in the group given
by the id's first part (`laptop`). If the script dies with an uncaught error, the panel turns red with
the error message. When you want more:

* `panel("laptop-ssp", name = "SSP ensemble", group = "models", priority = TRUE, stale_after = "2h")`:
  a display name, another group, a block in the grid instead of a row, and red if the job stops
  reporting for 2 hours (killed, laptop asleep).
* `run.progress(i / n, stage = f"scenario {i}/{n}", n_results = i)`: a stage message with the
  progress; any other named argument becomes a stat on the panel. `done()` takes stats too.
* `run.warn("slow")` (yellow), `run.error("diverged")` (red), `run.stats(loss = 0.03)`.
* `with run.track("writing netCDF"): …` (R: `run$track("writing outputs", write_outputs())`): a named
  step that turns red with that step's name and error if it fails.

Network problems are printed and ignored, so monitoring never crashes the job. Calls are cheap enough
for tight loops (a few µs in Python, 20–30 µs in R): routine updates are combined and sent at most every
5 seconds per panel, while a status change, an error, completion and a panel's first update go out at
once, and anything pending is sent when the script ends. Details for each language are at the top of
its client file, and [clients/README.md](clients/README.md) is the interface the clients share (and a
new language's client should follow).

**Sharing code that uses the monitor.** Each client has a print-only twin, `monitor_lite.py` /
`monitor_lite.R`: the same calls, no dependencies, and it prints a progress bar instead (on a terminal;
a line per stage and every 10 % in a log). Copy it into the code you share and use it in place of the
client:

```python
from monitor_lite import panel              # R: source("monitor_lite.R")
```

On your machines it hands over to the full client (Python: when `monitor_client` is installed; R: when
`MONITOR_R_CLIENT` is set), so the job reports to the dashboard there and prints progress for everyone
else, without changing the code. The full client itself prints instead of sending when the machine
has no login (or with `MONITOR_URL=off`), and prints as well as sends when you run a job in a terminal
or an interactive R session.

### From the outside: the agent and command-line tool

`monitor-agent` (from `agent/`; also installed under its old name, `monitor-client`) watches things
that don't report for themselves.

**Wrap a command or cron job,** and it reports success, failure and the last error line:

```bash
0 2 * * *  monitor-agent run laptop-backup --stale-after 26h -- rsync -a ~/Docs nas:/docs
```

`--stale-after` turns the panel red if the job doesn't report at all in that long (laptop asleep,
cron broken).

**For a job made of several commands,** give each one `--stage` (and `--step K/N` for a progress bar)
and the same panel name, so they show as stages of one job rather than separate panels:

```bash
0 2 * * *  cd ~/jobs && monitor-agent run laptop-sync --stage fetch     --step 1/3 -- python fetch.py \
                     && monitor-agent run laptop-sync --stage transform --step 2/3 -- python transform.py \
                     && monitor-agent run laptop-sync --stage upload    --step 3/3 -- python upload.py
```

The panel shows the running stage, a progress bar and each stage's run time, and reads "all 3 stages
ok at …" when the last one finishes. If a stage fails, the panel goes red with that stage's error.
Stage 1 always starts a fresh run, and later stages never turn a failed run green, so a failure stays
visible until the next run even if you chain with `;` instead of `&&`. The stages can also be
separate cron entries or scripts, as long as they run in order.

**Process and path checks** (no changes to the program being watched): add them in the feed editor
with `on: laptop` (**+ process**, **+ path** templates), then run the agent on that machine:

```bash
monitor-agent agent                  # keep running (see deploy/ for launchd/systemd files)
monitor-agent agent --once           # or one pass from cron every few minutes
monitor-agent test process train.py  # try a check locally first
```

The agent only runs the built-in process/path checks with the settings the feed list gives them. It
can't be told to run commands, and it reads but never changes anything.

Also: `monitor-agent set laptop-x red --error "disk full"` for a one-off update, and `monitor-agent
list` for the panels this machine's token can see.

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

## Alert emails

Add an `alerts:` section to the feed list (**+ alerts** in the editor) and tell the server how to send
mail in `monitor.env`:

```yaml
alerts:
  email: you@example.org          # or a list
  panels: all                     # all, priority, or none; per panel `alert: true/false` overrides
  after: 0m                       # only alert once something has been red this long
  recovery: true                  # also email when it's green again
  # repeat: 24h                   # remind while still red
```

```bash
# monitor.env. On DreamHost, create a mailbox such as monitor@yourdomain.org for this.
MONITOR_SMTP_HOST=smtp.dreamhost.com
MONITOR_SMTP_PORT=587
MONITOR_SMTP_USER=monitor@yourdomain.org
MONITOR_SMTP_PASSWORD=...
```

Then **Send test email** in the editor's Alerts box. You get one email when a panel goes red and one when
it's green again, not one per check. Whatever happens in the same check run arrives as a single email.
Red means anything: a failed check, an error a script pushed, or a job that stopped reporting
(`stale_after`). Yellow and grey (agent offline, e.g. laptop asleep) don't alert. Website and port
checks re-try once, 5 s later, before turning red, so a single dropped request doesn't email you.

Alerts are worked out after each scheduled check run: on DreamHost that's the cron job (every 5
minutes), elsewhere every minute. If sending fails (wrong password, say), the Alerts box shows the
error and the emails are retried on the next run.

**Who watches the monitor?** If DreamHost, or the cron job, stops, nothing here can tell you. Keep one
free external check on the monitor itself, e.g. an UptimeRobot keyword monitor on
`https://monitor.yourdomain.org/api/health` looking for `"checks_running":true` (false when scheduled
checks haven't run for 20 minutes). The dashboard also shows a warning when that happens.

## Moving monitors over from UptimeRobot

```bash
python3 tools/import_uptimerobot.py --api-key <read-only API key> > uptimerobot.yaml
```

prints feed-list entries for all your UptimeRobot monitors: website and keyword monitors become
`http` checks (`contains:` / `lacks:`), port monitors become `tcp` checks, intervals become schedules,
heartbeats become pushed panels with `stale_after`. Things that don't translate directly (ping,
heartbeats, basic auth) get a `NOTE` comment. Paste the entries under `panels:` in the editor, check,
save. Use a read-only key (UptimeRobot: Integrations & API → API). The tool tries UptimeRobot's v2 API,
then v3; if neither works, save the monitor list as JSON and use `--from-json monitors.json`.

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

From Python or R, use the clients (above) rather than calling the API directly.
