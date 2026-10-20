#!/usr/bin/env bash
# Deploy / update Lead Puller 2.x on the server.
#
#   cd ~/lead-puller && bash deploy/deploy.sh            # pull main, back up, rebuild, restart, check
#   bash deploy/deploy.sh --no-pull                      # same, but deploy the code already in the folder
#
# What it does, in order (stops at the first problem, before touching the running app where it can):
#   1. checks .env, Docker and the git checkout
#   2. backs up the database to ~/leadpuller-backup (safe while the app is running; keeps the last 20):
#      data/leadpuller2.db, or data/leadpuller.db while 2.0 has never run here (the last 1.x state)
#   3. git pull --ff-only origin main
#   4. first start of 2.0 only (it then imports data/leadpuller.db, read-only): shows what 1.x already
#      spent this month, which 2.0 does not count, and offers to lower MONTHLY_CREDIT_BUDGET for this
#      month and to clear SEAMLESS_API_KEY (keys live on the Accounts page). .env is backed up first.
#   5. frees the port: an older Lead Puller container started under another compose project name
#      (e.g. "lead-puller" from the folder name) is removed -- only if it uses this folder's data/.
#      Anything else on the port stops the deploy.
#   6. docker compose -p leadpuller up -d --build   (always the same project name, so updates replace
#      the running container instead of starting a second one)
#   7. waits until the app answers, then prints the last log lines (and, on 2.0's first start, what it imported)
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

# Python in a throwaway container (the data files are owned by root). The script comes on stdin.
pyrun() { docker run -i --rm "$@" python:3.12-slim python -; }

# Value of KEY in .env (last one wins, inline comment and quotes removed).
env_get() {
  { grep -E "^$1=" .env || true; } | tail -n 1 | cut -d= -f2- |
    sed -E "s/[[:space:]]+#.*\$//; s/^[[:space:]]+//; s/[[:space:]]+\$//; s/^[\"'](.*)[\"']\$/\1/"
}

# Set KEY=VALUE in .env, with an optional comment line above it. Keeps the file's owner and mode.
env_set() {
  local tmp
  tmp="$(mktemp)"
  awk -v k="$1" -v v="$2" -v c="${3:-}" '
    index($0, "# deploy.sh (" k "):") == 1 { next }
    index($0, k "=") == 1 { if (!done) { if (c != "") print "# deploy.sh (" k "): " c; print k "=" v; done = 1 }; next }
    { print }
    END { if (!done) { if (c != "") print "# deploy.sh (" k "): " c; print k "=" v } }' .env > "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}

# Yes/no question, default yes. Without a terminal the answer is no.
ask() {
  local a
  if [ ! -t 0 ]; then printf '%s [Y/n] -> no (no terminal)\n' "$1"; return 1; fi
  read -r -p "$1 [Y/n] " a || return 1
  case "$a" in ""|y|Y|yes|Yes|YES) return 0 ;; *) return 1 ;; esac
}

# ---- 1. checks ---------------------------------------------------------------
say "Checking $APP_DIR"
[ -f docker-compose.yml ] || fail "no docker-compose.yml here"
[ -f .env ] || fail ".env is missing (cp .env.example .env and fill it in)"
[ -n "$(env_get APP_PASSWORD)" ] || fail ".env has no APP_PASSWORD (2.0 logs in with APP_PASSWORD)"
docker info >/dev/null 2>&1 || fail "can't talk to Docker (is this user in the docker group?)"
PORT="$(grep -oE '127\.0\.0\.1:[0-9]+' docker-compose.yml | head -1 | cut -d: -f2)"
[ -n "$PORT" ] || fail "couldn't read the host port from docker-compose.yml"
if [ "$PULL" = 1 ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no
  fail "the files above were changed on the server; commit/undo them first (or use --no-pull)"
fi
PREV="$(git rev-parse --short HEAD)"
# Run with --no-pull right after a manual "git pull": go back to where that pull started.
if [ "$PULL" = 0 ] && git rev-parse -q --verify ORIG_HEAD >/dev/null &&
   [ "$(git rev-parse ORIG_HEAD)" != "$(git rev-parse HEAD)" ] && git merge-base --is-ancestor ORIG_HEAD HEAD; then
  PREV="$(git rev-parse --short ORIG_HEAD)"
fi

# ---- 2. backup ---------------------------------------------------------------
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -f data/leadpuller2.db ]; then SRC_DB=leadpuller2.db; NAME="leadpuller2-$STAMP-$PREV.db"
elif [ -f data/leadpuller.db ]; then SRC_DB=leadpuller.db; NAME="leadpuller-$STAMP-$PREV.db"
else SRC_DB=""; NAME=""; fi
if [ -n "$SRC_DB" ]; then
  say "Backing up data/$SRC_DB to $BACKUP_DIR/$NAME"
  # SQLite's backup API is safe while the app is writing (it includes the WAL).
  pyrun -v "$APP_DIR/data:/data" -v "$BACKUP_DIR:/backup" \
        -e SRC="$SRC_DB" -e DST="$NAME" -e OWNER="$(id -u):$(id -g)" <<'PY' || fail "database backup failed; nothing was changed"
import os, sqlite3
src = sqlite3.connect("file:/data/%s?mode=ro" % os.environ["SRC"], uri=True)
dst = sqlite3.connect("/backup/" + os.environ["DST"])
src.backup(dst)
dst.close()
src.close()
uid, gid = (int(x) for x in os.environ["OWNER"].split(":"))
os.chown("/backup/" + os.environ["DST"], uid, gid)
PY
  for pattern in "leadpuller-*.db" "leadpuller2-*.db"; do
    find "$BACKUP_DIR" -maxdepth 1 -name "$pattern" -printf '%T@ %p\n' | sort -rn |
      tail -n +$((KEEP_BACKUPS + 1)) | cut -d' ' -f2- | xargs -r rm -f
  done
else
  say "No database yet (first deploy): nothing to back up"
fi

# ---- 3. code -----------------------------------------------------------------
if [ "$PULL" = 1 ]; then
  say "Pulling main"
  git pull --ff-only origin main || fail "git pull failed; the running app was not touched"
fi
NOW="$(git rev-parse --short HEAD)"
say "Deploying $NOW (was $PREV)"
[ -f app/importer.py ] || fail "this folder doesn't hold Lead Puller 2.x code (no app/importer.py); this script is for 2.x"

# ---- 4. first start of 2.0 ---------------------------------------------------
FIRST_V2=0
if [ ! -f data/leadpuller2.db ] && [ -f data/leadpuller.db ]; then
  FIRST_V2=1
  say "First start of 2.0: it will import data/leadpuller.db (read-only; 1.x's file is never changed)"
  echo "Make sure no pull is running in the old version right now."
  SPENT="$(pyrun -v "$APP_DIR/data:/data" <<'PY'
import calendar, sqlite3, time
t = time.gmtime()
start = calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0))
con = sqlite3.connect("file:/data/leadpuller.db?mode=ro", uri=True)
try:
    print(int(con.execute("SELECT COALESCE(SUM(credits),0) FROM ledger WHERE at>=?", (start,)).fetchone()[0]))
except sqlite3.Error:
    print(0)
PY
)" || fail "couldn't read this month's spending from data/leadpuller.db; nothing was changed"
  BUDGET="$(env_get MONTHLY_CREDIT_BUDGET)"
  BUDGET="${BUDGET:-9500}"
  case "$BUDGET" in *[!0-9]*) fail "MONTHLY_CREDIT_BUDGET in .env is not a whole number: $BUDGET" ;; esac
  echo "1.x already spent $SPENT credits this month. 2.0 starts the month at 0, it doesn't see those."
  ENV_BAK="$BACKUP_DIR/env-$STAMP.bak"
  backup_env() { [ -f "$ENV_BAK" ] || { cp .env "$ENV_BAK"; chmod 600 "$ENV_BAK"; echo "(.env backed up to $ENV_BAK)"; }; }
  if [ "$BUDGET" -gt 0 ] && [ "$SPENT" -gt 0 ]; then
    NEW=$((BUDGET - SPENT))
    [ "$NEW" -ge 1 ] || NEW=1   # 0 would mean "no limit"
    if ask "Set MONTHLY_CREDIT_BUDGET from $BUDGET to $NEW for the rest of $(date +%B)?"; then
      [ -w .env ] || fail ".env is not writable by $(id -un)"
      backup_env
      env_set MONTHLY_CREDIT_BUDGET "$NEW" "lowered from $BUDGET for $(date +%Y-%m) (1.x had spent $SPENT); set back to $BUDGET on the 1st"
      echo "Done. Set it back to $BUDGET on the 1st of next month, then run: docker compose -p $PROJECT up -d"
    else
      echo "MONTHLY_CREDIT_BUDGET left at $BUDGET."
    fi
  fi
  if [ -n "$(env_get SEAMLESS_API_KEY)" ]; then
    echo "SEAMLESS_API_KEY is set in .env. In 2.0 Seamless keys belong on the Accounts page;"
    echo "a key left in .env is added back as an account every time the app starts."
    if ask "Clear SEAMLESS_API_KEY in .env?"; then
      [ -w .env ] || fail ".env is not writable by $(id -un)"
      backup_env
      env_set SEAMLESS_API_KEY ""
      echo "Cleared. The key from the old database still shows on the Accounts page as \"Account from .env\" until you delete it there."
    fi
  fi
  ask "Start Lead Puller 2.0 now?" ||
    { say "Nothing was started; the old version is still running. When ready: bash deploy/deploy.sh --no-pull"; exit 0; }
fi

# ---- 5. free the port --------------------------------------------------------
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

# ---- 6. build and start ------------------------------------------------------
say "Building and starting (project '$PROJECT')"
docker compose -p "$PROJECT" up -d --build --remove-orphans

# ---- 7. check ----------------------------------------------------------------
say "Waiting for the app on 127.0.0.1:$PORT"
for i in $(seq 1 90); do
  if HEALTH="$(curl -fsS "http://127.0.0.1:$PORT/healthz" 2>/dev/null)"; then
    docker compose -p "$PROJECT" ps
    say "Last log lines"
    docker compose -p "$PROJECT" logs --tail 15
    if [ "$FIRST_V2" = 1 ]; then
      say "Imported from the old database"
      pyrun -v "$APP_DIR/data:/data" <<'PY' || true
import json, sqlite3
con = sqlite3.connect("file:/data/leadpuller2.db?mode=ro", uri=True)
row = con.execute("SELECT v FROM meta WHERE k='old_import'").fetchone()
m = json.loads(row[0]) if row else {}
if m.get("error"):
    print("IMPORT FAILED: %s -- don't run any pull; ask for help." % m["error"])
elif not m.get("found"):
    print("WARNING: the old database was not found, nothing was imported -- don't run any pull; ask for help.")
else:
    print("%(people)s people, %(emails)s downloaded emails, %(do_not_pull)s do-not-pull entries, %(accounts)s Seamless accounts" % m)
    if not m.get("people") and not m.get("emails"):
        print("WARNING: no people or emails were imported -- check this before running any pull.")
PY
      echo "Next: on the Accounts page add your new Seamless key, then delete \"Account from .env\" (the old key)."
    fi
    say "DEPLOYED $NOW ($HEALTH). Backup of the previous database: ${NAME:-none}"
    exit 0
  fi
  sleep 2
done
docker compose -p "$PROJECT" ps || true
docker compose -p "$PROJECT" logs --tail 60 || true
cat >&2 <<EOF

DEPLOY FAILED: the app did not answer within 3 minutes (logs above).
To go back to the previous version ($PREV):
  git reset --hard $PREV && docker compose -p $PROJECT up -d --build --remove-orphans
The 1.x database (data/leadpuller.db) is never changed by 2.0, so going back to 1.x needs no restore.
EOF
if [ "$SRC_DB" = leadpuller2.db ]; then
  cat >&2 <<EOF
Only if 2.0's own database is damaged, restore the backup taken just now:
  docker compose -p $PROJECT down && sudo cp "$BACKUP_DIR/$NAME" data/leadpuller2.db && sudo rm -f data/leadpuller2.db-wal data/leadpuller2.db-shm
  bash deploy/deploy.sh --no-pull
EOF
fi
exit 1
