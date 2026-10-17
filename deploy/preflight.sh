#!/usr/bin/env bash
# Read-only checks before the FIRST deploy on a server that already runs other apps.
# Changes nothing: it only looks. Run it from the unpacked package, before moving it into place:
#   tar -xzf /tmp/lead-puller.tgz -C /tmp && bash /tmp/lead-puller/deploy/preflight.sh [leads.yourdomain.com]
# Optional argument: the hostname you plan to give the app, to check no other Nginx site already uses it.
set -uo pipefail

TARGET="${TARGET:-$HOME/lead-puller}"
PORT="${HOST_PORT:-8090}"
DOMAIN="${1:-}"
problems=0
ok()   { echo "  OK    $*"; }
info() { echo "  INFO  $*"; }
bad()  { echo "  STOP  $*"; problems=$((problems + 1)); }

echo "== Docker"
if docker compose version >/dev/null 2>&1; then
  ok "$(docker compose version | head -n1)"
else
  bad "'docker compose' (v2) not available for user $(whoami). Run as the user that runs AnavMail, or root."
fi
if docker ps >/dev/null 2>&1; then
  ok "user $(whoami) can talk to Docker"
  echo "  Running containers (these must still be running after the deploy):"
  docker ps --format '    {{.Names}}  |  {{.Status}}  |  {{.Ports}}'
  if docker compose ls -a 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx leadpuller; then
    info "a compose project named 'leadpuller' already exists (fine if this is a re-deploy of this app)"
  else
    ok "no existing compose project named 'leadpuller'"
  fi
else
  bad "user $(whoami) cannot use Docker (permission denied?)"
fi

echo "== Port $PORT (Lead Puller listens on 127.0.0.1:$PORT only)"
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$PORT\$"; then
  bad "port $PORT is already in use. Pick a free one and set HOST_PORT=<port> in .env:"
  ss -ltn | awk 'NR==1 || $4 ~ /[:.]'"$PORT"'$/'
else
  ok "port $PORT is free"
fi
echo "  Ports already in use on this server, for reference:"
ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | sed -E 's/.*[:.]//' | sort -n -u | tr '\n' ' '; echo

echo "== Folder $TARGET"
if [ -e "$TARGET" ]; then
  bad "$TARGET already exists - check what it is before deploying there (or set TARGET=/other/path)"
else
  ok "$TARGET does not exist yet"
fi

echo "== Memory and disk"
free -h | sed 's/^/  /'
avail_mb="$(free -m | awk '/^Mem:/ {print $7}')"
if [ "${avail_mb:-0}" -lt 1024 ]; then
  bad "only ${avail_mb} MB memory available; Lead Puller is capped at 768 MB (normally uses ~100 MB)"
else
  ok "${avail_mb} MB available; Lead Puller is capped at 768 MB"
fi
df -h "$HOME" /var/lib/docker 2>/dev/null | sed 's/^/  /'
free_gb="$(df -BG --output=avail "$HOME" 2>/dev/null | tail -n1 | tr -dc '0-9')"
if [ "${free_gb:-0}" -lt 3 ]; then
  bad "less than 3 GB free disk; the image build needs ~0.5 GB plus room for data and backups"
else
  ok "${free_gb} GB free disk"
fi

echo "== Web server (only matters for the HTTPS step)"
for s in nginx caddy apache2; do
  if systemctl is-active --quiet "$s" 2>/dev/null; then info "$s is running"; fi
done
if [ -n "$DOMAIN" ] && [ -d /etc/nginx ]; then
  if grep -rqs -E "server_name[^;]*[[:space:]]$DOMAIN([[:space:];]|\$)" /etc/nginx/; then
    bad "$DOMAIN is already used by an existing Nginx site:"
    grep -rls -E "server_name[^;]*[[:space:]]$DOMAIN([[:space:];]|\$)" /etc/nginx/ | sed 's/^/    /'
  else
    ok "no Nginx site uses $DOMAIN yet"
  fi
fi

echo
if [ "$problems" -eq 0 ]; then
  echo "Preflight passed: deploying Lead Puller won't collide with what's on this server."
else
  echo "Preflight found $problems problem(s) marked STOP. Fix them before deploying."
  exit 1
fi
