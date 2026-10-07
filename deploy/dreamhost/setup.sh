#!/usr/bin/env bash
# Install (or repair) the monitor on DreamHost shared hosting, running as a CGI script.
#
#   git clone https://github.com/YOU/monitor.git ~/monitor-app      # the app: NOT inside a web folder
#   bash ~/monitor-app/deploy/dreamhost/setup.sh monitor.yourdomain.org
#
# Safe to re-run any time; it checks everything and fixes what it can.
set -uo pipefail
APP="$(cd "$(dirname "$0")/../.." && pwd)"
DOMAIN="${1:-}"
[ -n "$DOMAIN" ] || { echo "usage: bash deploy/dreamhost/setup.sh monitor.yourdomain.org"; exit 2; }
WEB="${WEB_DIR:-$HOME/$DOMAIN}"
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFIX\033[0m   %s\n' "$*"; PROBLEMS=$((PROBLEMS+1)); }
note() { printf '  \033[33mnote\033[0m  %s\n' "$*"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$*"; exit 1; }
PROBLEMS=0
echo "Monitor setup: app in $APP, site $DOMAIN served from $WEB"

# --- 1. folders: the app must not be web-readable --------------------------------------------
[ -d "$WEB" ] || die "$WEB doesn't exist. Add $DOMAIN under Websites in the DreamHost panel first (or set WEB_DIR=...)."
case "$APP/" in "$WEB/"*) die "the app is inside the web folder, where anyone could download monitor.env and the database.
          Move it out:  mv $APP ~/monitor-app  (then run this script from there)";; esac
if [ -e "$WEB/server.py" ] || [ -e "$WEB/monitor.env" ] || [ -d "$WEB/data" ]; then
  die "$WEB still contains app files (server.py / monitor.env / data). They are publicly downloadable.
          Move anything you need out of $WEB, delete the rest, then re-run this script."
fi
ok "app folder is outside the web folder"

# --- 2. Python 3.10+ and the virtualenv -----------------------------------------------------
if [ ! -x "$APP/venv/bin/python3" ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3 $HOME/opt/python-3*/bin/python3; do
    p=$(command -v "$c" 2>/dev/null) || continue
    "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null && { PY="$p"; break; }
  done
  [ -n "$PY" ] || die "no Python 3.10+ found. Install one (DreamHost: 'Installing a custom version of Python 3'), then re-run."
  "$PY" -m venv "$APP/venv" || die "couldn't create the virtualenv with $PY"
  ok "created venv with $("$PY" --version)"
fi
"$APP/venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP/requirements.txt" \
  && ok "packages installed ($("$APP/venv/bin/python3" --version))" || bad "pip install failed (see above)"

# --- 3. settings -----------------------------------------------------------------------------
ENVF="$APP/monitor.env"
if [ ! -f "$ENVF" ]; then
  cp "$APP/monitor.env.example" "$ENVF"
  PW=$("$APP/venv/bin/python3" -c 'import secrets; print(secrets.token_urlsafe(12))')
  sed -i "s|^MONITOR_PASSWORD=.*|MONITOR_PASSWORD=$PW|" "$ENVF"
  ok "created monitor.env with a new password:  $PW   (change it any time in $ENVF)"
fi
setenv() {  # set KEY=VALUE in monitor.env unless the key already has a value
  grep -Eq "^$1=.+" "$ENVF" || { sed -i "/^$1=/d" "$ENVF"; echo "$1=$2" >> "$ENVF"; note "set $1=$2"; }
}
setenv MONITOR_SCHEDULER external
setenv MONITOR_REFRESH 30
setenv MONITOR_AGENT_INTERVAL 60
grep -Eq '^MONITOR_PASSWORD=.+' "$ENVF" && ok "monitor.env has a password" || bad "set MONITOR_PASSWORD= in $ENVF"
chmod 600 "$ENVF"; mkdir -p "$APP/data"; chmod 700 "$APP/data"

# --- 4. the web folder: only the launcher and .htaccess ------------------------------------
for f in index.html index.htm index.php; do
  [ -f "$WEB/$f" ] && mv "$WEB/$f" "$APP/data/$f.dreamhost-placeholder" && ok "removed DreamHost's placeholder $f"
done
PYBIN="$APP/venv/bin/python3"
sed -e "s|@PYTHON@|$PYBIN|g" -e "s|@APP@|$APP|g" "$APP/deploy/dreamhost/monitor.cgi.template" > "$WEB/monitor.cgi"
cp "$APP/deploy/dreamhost/htaccess.template" "$WEB/.htaccess"
chmod 755 "$WEB" "$WEB/monitor.cgi"; chmod 644 "$WEB/.htaccess"
ok "wrote $WEB/monitor.cgi and .htaccess"
others=$(ls -A "$WEB" | grep -Ev '^(monitor\.cgi|\.htaccess|\.well-known|\.dh-diag|favicon\.ico)$' || true)
[ -n "$others" ] && note "other files in $WEB (not served, since every URL goes to the app): $(echo $others)"

# --- 5. does it work? ------------------------------------------------------------------------
out=$(cd "$WEB" && env -i HOME="$HOME" PATH="$PATH" GATEWAY_INTERFACE=CGI/1.1 REQUEST_METHOD=GET \
      REQUEST_URI=/api/health SERVER_NAME="$DOMAIN" SERVER_PORT=443 HTTPS=on SERVER_PROTOCOL=HTTP/1.1 \
      ./monitor.cgi 2>&1)
case "$out" in
  *"200 OK"*'"ok":true'*) ok "monitor.cgi runs and answers /api/health" ;;
  *) bad "monitor.cgi doesn't run:"; echo "$out" | tail -8 | sed 's/^/          /' ;;
esac
"$APP/venv/bin/python3" "$APP/server.py" validate >/dev/null 2>&1 || note "the feed list has an error; fix it in the editor"
if command -v curl >/dev/null; then
  code=$(curl -s -o /tmp/monitor-setup.$$ -w '%{http_code}' "https://$DOMAIN/api/health")
  if [ "$code" = 200 ] && grep -q '"ok"' /tmp/monitor-setup.$$; then ok "https://$DOMAIN is live"
  else
    bad "https://$DOMAIN/api/health returned HTTP $code"
    [ "$code" = 000 ] && echo "          → HTTPS isn't working yet: turn on the Let's Encrypt certificate for $DOMAIN in the panel."
    log=$(ls -t "$HOME/logs/$DOMAIN"/https/error.log "$HOME/logs/$DOMAIN"/http/error.log 2>/dev/null | head -1)
    [ -n "$log" ] && { echo "          Recent server errors ($log):"; tail -5 "$log" | sed 's/^/            /'; }
  fi
  rm -f /tmp/monitor-setup.$$
fi

echo
echo "Scheduled checks need one cron job (Panel → Advanced → Cron Jobs, every 5 minutes, email off):"
echo "    $APP/venv/bin/python3 $APP/server.py tick"
echo
[ "$PROBLEMS" -eq 0 ] && echo "All good. Open https://$DOMAIN" || echo "$PROBLEMS thing(s) to fix above."
