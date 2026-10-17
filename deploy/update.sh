#!/usr/bin/env bash
# Manual deploy on the server: back up the database, update the code, rebuild, health-check.
#   bash deploy/update.sh                        pull the latest commit of the deploy branch (git checkout only)
#   bash deploy/update.sh /tmp/lead-puller.zip   deploy an uploaded package instead (.zip, .tgz or .tar.gz)
# The server's .env and data/ are never touched.
set -euo pipefail

BRANCH="${DEPLOY_BRANCH:-main}"

die() { echo "ERROR: $*" >&2; exit 1; }

main() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  [ -f .env ] || die "$PWD/.env is missing - create it first: cp .env.example .env && nano .env"

  local dc
  if docker compose version >/dev/null 2>&1; then dc="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then dc="docker-compose"
  else die "Docker Compose is not installed"; fi

  local port
  port="$(grep -E '^HOST_PORT=' .env | tail -n1 | cut -d= -f2- | tr -d ' "\r' || true)"
  port="${port:-8090}"

  echo "==> 1/4 Backing up the database"
  bash deploy/backup.sh

  echo "==> 2/4 Updating the code"
  if [ $# -ge 1 ]; then from_package "$1"; else from_git; fi

  echo "==> 3/4 Rebuilding and restarting the container"
  $dc up -d --build

  echo "==> 4/4 Waiting for the health check on 127.0.0.1:$port"
  local out
  for _ in $(seq 1 30); do
    if out="$(curl -fsS "http://127.0.0.1:$port/healthz" 2>/dev/null)"; then
      echo "Deployed. $out"
      return 0
    fi
    sleep 2
  done
  $dc logs --tail 80 leadpuller
  die "The app did not become healthy - see the logs above."
}

from_git() {
  [ -d .git ] || die "Not a git checkout - pass a package path instead: bash deploy/update.sh /tmp/lead-puller.zip"
  git diff --quiet && git diff --cached --quiet \
    || die "Tracked files were edited on the server. Run 'git status', undo those edits, then retry."
  local before
  before="$(git rev-parse --short HEAD)"
  git fetch --quiet origin "$BRANCH"
  git checkout --quiet "$BRANCH"
  git merge --ff-only --quiet "origin/$BRANCH"
  echo "$before -> $(git log -1 --format='%h %s (%an, %ar)')"
}

from_package() {
  local pkg stage src
  pkg="$(readlink -f "$1")"
  [ -f "$pkg" ] || die "Package not found: $1"
  stage="$(mktemp -d)"
  case "$pkg" in
    *.zip)
      if command -v unzip >/dev/null 2>&1; then unzip -q "$pkg" -d "$stage"
      else python3 -m zipfile -e "$pkg" "$stage"; fi ;;
    *.tgz|*.tar.gz) tar -xzf "$pkg" -C "$stage" ;;
    *) rm -rf "$stage"; die "Unsupported package type (use .zip, .tgz or .tar.gz): $1" ;;
  esac
  # The project folder inside the package may have any name (lead-puller/, seamless-puller/, or none).
  src="$(find "$stage" -maxdepth 3 -path '*/app/main.py' -print -quit)"
  [ -n "$src" ] || { rm -rf "$stage"; die "Package has no app/main.py"; }
  src="$(dirname "$(dirname "$src")")"
  rm -rf "$src/.env" "$src/data"
  rm -rf app tests
  cp -a "$src/." .
  rm -rf "$stage"
  echo "Code replaced from $pkg"
}

# Keep the call and the exit on one line: git may rewrite this file while it runs.
main "$@"; exit $?
