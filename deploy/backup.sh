#!/usr/bin/env bash
# Online backup of the SQLite database, taken inside the running container (safe while the app runs).
# Writes data/backups/leadpuller-YYYYmmdd-HHMMSS.db and keeps the newest 14.
# Usage: bash deploy/backup.sh   (deploy/update.sh runs it first; nightly cron: see README)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

cid="$($DC ps -q leadpuller 2>/dev/null || true)"
if [ -z "$cid" ] || [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" != "true" ]; then
  echo "Container is not running - nothing to back up (expected on the very first deploy)."
  exit 0
fi

$DC exec -T leadpuller python - <<'PY'
import glob, os, sqlite3, time

src_path = "/data/leadpuller.db"
if not os.path.exists(src_path):
    print("No database yet - nothing to back up.")
    raise SystemExit
os.makedirs("/data/backups", exist_ok=True)
dst_path = time.strftime("/data/backups/leadpuller-%Y%m%d-%H%M%S.db")
src, dst = sqlite3.connect(src_path), sqlite3.connect(dst_path)
src.backup(dst)
dst.close()
src.close()
for old in sorted(glob.glob("/data/backups/leadpuller-*.db"))[:-14]:
    os.remove(old)
print("Backup written: data/backups/" + os.path.basename(dst_path))
PY
