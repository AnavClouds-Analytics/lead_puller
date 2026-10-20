#!/usr/bin/env bash
# Deploy / update Lead Puller on the server.
#
#   cd ~/lead-puller && bash deploy/deploy.sh            # pull main, back up, rebuild, restart, check
#   bash deploy/deploy.sh --no-pull                      # same, but deploy the code already in the folder
#
# What it does, in order (stops at the first problem, before touching the running app where it can):
#   1. checks .env, Docker and the git checkout
#   2. backs up data/leadpuller.db to ~/leadpuller-backup (safe while the app is running; keeps the last 20)
#   3. git pull --ff-only origin main
#   4. frees the port: an older Lead Puller container started under another compose project name
#      (e.g. "lead-puller" from the folder name) is removed -- only if it uses this folder's data/.
#      Anything else on the port stops the deploy.
#   5. docker compose -p leadpuller up -d --build   (always the same project name, so updates replace
#      the running container instead of starting a second one)
#   6. waits until the app answers, then prints the last log lines
set -euo pipefail

PROJECT=leadpuller
BACKUP_DIR="${BACKUP_DIR:-$HOME/leadpuller-backup}"
KEEP_BACKUPS=20
PULL=1
[ "${1:-}" = "--no-pull" ] && PULL=0

cd "$(dirname "$0")/.."
APP_DIR="$(pwd -P)"
say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\nDEPLOY STOPPED: %s\n' "$*" >&2; exit 1; }

# ---- 1. checks ---------------------------------------------------------------
say "Checking $APP_DIR"
[ -f docker-compose.yml ] || fail "no docker-compose.yml here"
[ -f .env ] || fail ".env is missing (cp .env.example .env and fill it in)"
grep -qE '^APP_PASSWORD=.+' .env || fail ".env has no APP_PASSWORD -- v1.2 logs in with APP_PASSWORD (ADMIN_USERS is not used)"
grep -qE '^SEAMLESS_API_KEY=.+' .env || fail ".env has no SEAMLESS_API_KEY"
docker info >/dev/null 2>&1 || fail "can't talk to Docker (is this user in the docker group?)"
PORT="$(grep -oE '127\.0\.0\.1:[0-9]+' docker-compose.yml | head -1 | cut -d: -f2)"
[ -n "$PORT" ] || fail "couldn't read the host port from docker-compose.yml"
if [ "$PULL" = 1 ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no
  fail "the files above were changed on the server; commit/undo them first (or use --no-pull)"
fi
PREV="$(git rev-parse --short HEAD)"

# ---- 2. backup ---------------------------------------------------------------
mkdir -p "$BACKUP_DIR"
if [ -f data/leadpuller.db ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  NAME="leadpuller-$STAMP-$PREV.db"
  say "Backing up the database to $BACKUP_DIR/$NAME"
  # Runs in a throwaway container: the db file is owned by root, and SQLite's backup API is safe
  # while the app is writing (it includes the WAL).
  docker run --rm -v "$APP_DIR/data:/data:ro" -v "$BACKUP_DIR:/backup" python:3.12-slim python -c "
import sqlite3, os
src = sqlite3.connect('file:/data/leadpuller.db?mode=ro', uri=True)
dst = sqlite3.connect('/backup/$NAME')
src.backup(dst); dst.close(); src.close()
os.chown('/backup/$NAME', $(id -u), $(id -g))
" || fail "database backup failed; nothing was changed"
  ls -1t "$BACKUP_DIR"/leadpuller-*.db 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
else
  say "No data/leadpuller.db yet (first deploy): nothing to back up"
fi

# ---- 3. code -----------------------------------------------------------------
if [ "$PULL" = 1 ]; then
  say "Pulling main"
  git pull --ff-only origin main || fail "git pull failed; the running app was not touched"
fi
NOW="$(git rev-parse --short HEAD)"
say "Deploying $NOW (was $PREV)"

# ---- 4. free the port --------------------------------------------------------
# Leftover compose projects started from this folder under another name (e.g. a failed "lead-puller" run).
for proj in $(docker ps -a --filter "label=com.docker.compose.project.working_dir=$APP_DIR" \
                --format '{{.Label "com.docker.compose.project"}}' | sort -u); do
  if [ "$proj" != "$PROJECT" ]; then
    say "Removing old compose project '$proj' from this folder (data/ is kept)"
    docker compose -p "$proj" down --remove-orphans
  fi
done
# Whatever still holds the port.
for id in $(docker ps -q --filter "publish=$PORT"); do
  name="$(docker inspect -f '{{.Name}}' "$id" | sed 's#^/##')"
  proj="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id")"
  svc="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id")"
  [ "$proj" = "$PROJECT" ] && continue          # ours: compose replaces it below
  if [ "$svc" != "leadpuller" ]; then
    fail "port $PORT is used by another container: $name (project '$proj'). Change the port in docker-compose.yml or stop that app."
  fi
  src="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$id")"
  if [ "$(cd "$src" 2>/dev/null && pwd -P)" != "$APP_DIR/data" ]; then
    fail "an older Lead Puller ($name) holds port $PORT and keeps its data in '$src', not $APP_DIR/data. Copy that data here first, then re-run."
  fi
  say "Stopping older Lead Puller container $name (project '$proj', same data folder)"
  docker rm -f "$id" >/dev/null
done
if command -v ss >/dev/null && ss -ltnH "sport = :$PORT" | grep -q . && [ -z "$(docker ps -q --filter "publish=$PORT")" ]; then
  ss -ltnp "sport = :$PORT" || true
  fail "port $PORT is used by a program outside Docker (above)"
fi

# ---- 5. build and start ------------------------------------------------------
say "Building and starting (project '$PROJECT')"
docker compose -p "$PROJECT" up -d --build --remove-orphans

# ---- 6. check ----------------------------------------------------------------
say "Waiting for the app on 127.0.0.1:$PORT"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/auth/status" >/dev/null 2>&1; then
    docker compose -p "$PROJECT" ps
    say "Last log lines"
    docker compose -p "$PROJECT" logs --tail 15
    say "DEPLOYED $NOW. Backup of the previous database: ${NAME:-none}"
    exit 0
  fi
  sleep 2
done
docker compose -p "$PROJECT" ps || true
docker compose -p "$PROJECT" logs --tail 60 || true
cat >&2 <<EOF

DEPLOY FAILED: the app did not answer within 60 s (logs above).
To go back to the previous version ($PREV):
  git reset --hard $PREV && bash deploy/deploy.sh --no-pull
and, only if the database is damaged, restore it:
  docker compose -p $PROJECT down && sudo cp "$BACKUP_DIR/${NAME:-<backup>.db}" data/leadpuller.db && sudo rm -f data/leadpuller.db-wal data/leadpuller.db-shm
  bash deploy/deploy.sh --no-pull
EOF
exit 1
