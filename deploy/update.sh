#!/usr/bin/env bash
# Pull the latest code from GitHub and restart the app. Run on the server:
#   ~/monitor.yourdomain.org/deploy/update.sh
# Your feed list, database and settings (data/, monitor.env) are untouched.
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --ff-only
venv/bin/pip install --quiet -r requirements.txt
venv/bin/python server.py validate || echo "warning: the feed list has an error; fix it in the editor"

if [ -f /etc/systemd/system/monitor.service ]; then
  sudo systemctl restart monitor          # VPS install
else
  mkdir -p tmp && touch tmp/restart.txt   # DreamHost / Passenger
fi
echo "Updated to $(git log -1 --format='%h %s')"
