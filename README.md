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
static/index.html     the dashboard and feed editor
passenger_wsgi.py     entry point on DreamHost shared hosting
deploy/               update script, systemd units, macOS launchd file for the agent
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

Shared plans don't allow long-running processes, so the app runs under DreamHost's Passenger and a
DreamHost cron job runs the scheduled checks (`MONITOR_SCHEDULER=external`).

1. **Panel → Websites:** add a subdomain (e.g. `monitor.yourdomain.org`), turn on **Passenger** and the
   free **Let's Encrypt** certificate.
2. **SSH in once to install** (you won't need SSH for day-to-day use):
   ```bash
   cd ~/monitor.yourdomain.org
   git init && git remote add origin https://github.com/YOURNAME/monitor.git
   git fetch && git checkout -f -t origin/main        # alongside DreamHost's public/ folder
   python3 -m venv venv && venv/bin/pip install -r requirements.txt
   cp monitor.env.example monitor.env && nano monitor.env   # set MONITOR_PASSWORD
   mkdir -p tmp && touch tmp/restart.txt
   ```
   A private repo needs a deploy key or a token for `git fetch`; GitHub's "deploy keys" page shows
   how. Python 3.10+ is needed; if `python3 --version` is older, install a newer one with DreamHost's
   custom-Python guide and use it to make the venv.
3. **Panel → Advanced → Cron Jobs:** every 5 minutes (or as often as allowed), with email off:
   ```bash
   cd ~/monitor.yourdomain.org && venv/bin/python server.py tick
   ```
   Each tick runs only the checks whose schedule has come round; schedules shorter than the cron
   interval are rounded up.
4. Open `https://monitor.yourdomain.org`, log in, click **Edit feeds**.

**Updating later:** `ssh` in and run `~/monitor.yourdomain.org/deploy/update.sh`. It pulls, installs
any new requirements, checks the feed list still loads and restarts the app.

## Deploying on a VPS or your own server

Clone it to `/opt/monitor`, make the venv and `monitor.env` as above, set
`MONITOR_SCHEDULER=internal` (the built-in scheduler), and install `deploy/monitor.service`. Put HTTPS
in front, e.g. Caddy: `monitor.yourdomain.org { reverse_proxy 127.0.0.1:8600 }`.

## Monitoring a machine (laptop, compute server)

**Once per machine:**

1. In the dashboard: **Edit feeds → Tokens**, name it after the machine (`laptop`) and click
   **New token**. By default it may update panels whose id starts `laptop-`. The dashboard shows a
   ready-to-paste command; the token isn't shown again.
2. On the machine, put `monitor_client.py` somewhere (e.g. `~/bin/`) and run that command:
   ```bash
   python3 ~/bin/monitor_client.py login https://monitor.yourdomain.org mon_xxxxxxxx
   ```
   This saves the URL and token to `~/.config/monitor/client.json`, readable only by you.

**Then, for a Python job,** nothing else needs setting up. The panel appears on its first update:

```python
import sys; sys.path.insert(0, "/Users/james/bin")    # wherever monitor_client.py lives
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

**For a command or cron job:** wrap it, and it reports success, failure and the last error line:

```bash
0 2 * * *  python3 ~/bin/monitor_client.py run laptop-backup --stale-after 26h -- rsync -a ~/Docs nas:/docs
```

**For process and path checks** (no changes to the program being watched), add them in the feed editor
with `on: laptop` (**+ process**, **+ path** templates), then run the agent on that machine:

```bash
python3 ~/bin/monitor_client.py agent            # keep running (see deploy/ for launchd/systemd files)
python3 ~/bin/monitor_client.py agent --once     # or one pass from cron every few minutes
python3 ~/bin/monitor_client.py test process train.py    # try a check locally first
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

R, for example: `httr::PUT(paste0(url, "/api/panels/laptop-fit"), add_headers(Authorization = paste("Bearer", tok)), body = list(status = "ok", stats = list(n = 12)), encode = "json")`.
