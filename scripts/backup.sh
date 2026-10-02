#!/usr/bin/env bash
# Consistent SQLite + files backup. Briefly stop app writers, restart exactly
# those that were running, verify, then publish. See docs/BACKUP_RESTORE.md.
# Usage: bash scripts/backup.sh [destination-directory]
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Resolve configuration before creating a backup directory or touching services.
# Compose applies shell/.env/COMPOSE_ENV_FILES precedence; never source a dotenv
# file as shell code. These config commands do not require the Docker daemon.
# Reject a PostgreSQL declaration even when the base Compose file would force
# SQLite: that mismatch must not produce a backup of the wrong database.
DECLARED_SCHEME="$(docker compose config --environment | awk '
    /^DATABASE_URL=/ { sub(/^[^=]*=/, ""); split($0, parts, ":"); print parts[1] }
')"
case "$DECLARED_SCHEME" in
    ""|sqlite|sqlite+aiosqlite) ;;
    *) echo "SQLite backup refused: DATABASE_URL declares an unsupported database. See docs/BACKUP_RESTORE.md." >&2; exit 1 ;;
esac
# The service configuration can override the interpolation environment (for
# example, the PostgreSQL overlay). Inspect its DATABASE_URL values as well.
# Only schemes leave this filter; connection credentials are never printed.
EFFECTIVE_SCHEMES="$(docker compose config | awk '
    $1 == "DATABASE_URL:" {
        value = $2; sub(/^[\042\047]/, "", value); split(value, parts, ":"); print parts[1]
    }
')"
if [ -z "$EFFECTIVE_SCHEMES" ]; then
    echo "SQLite backup refused: no effective service DATABASE_URL could be resolved." >&2
    exit 1
fi
while IFS= read -r scheme; do
    case "$scheme" in
        sqlite|sqlite+aiosqlite) ;;
        *) echo "SQLite backup refused: a service uses an unsupported database. See docs/BACKUP_RESTORE.md." >&2; exit 1 ;;
    esac
done <<< "$EFFECTIVE_SCHEMES"

DEST="${1:-$ROOT/backups}"
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd)"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
if ! [[ "$KEEP_DAYS" =~ ^[0-9]+$ ]]; then
    echo "BACKUP_KEEP_DAYS must be a nonnegative integer." >&2
    exit 1
fi
command -v flock >/dev/null || { echo "Install util-linux (flock) to prevent overlapping backups." >&2; exit 1; }
exec 9>"$DEST/.backup.lock"
flock -n 9 || { echo "Another backup already holds the lock." >&2; exit 1; }

STAMP="$(date -u +%Y-%m-%d-%H%M%S)-$$"
ARCHIVE="$DEST/spooldesk-$STAMP.zip"
PART="$ARCHIVE.part"
RUNNING=()
OFFSITE_PART=""
SERVICES="$(docker compose ps --status running --services)"
while IFS= read -r service; do
    case "$service" in bot|dashboard) RUNNING+=("$service");; esac
done <<< "$SERVICES"

resume_services() {
    if [ "${#RUNNING[@]}" -gt 0 ]; then
        docker compose start "${RUNNING[@]}" >&2 || return 1
        RUNNING=()
    fi
}
finish() {
    status="$1"
    trap - EXIT
    resume_services || status=1
    [ ! -f "$PART" ] || rm -f -- "$PART"
    [ -z "$OFFSITE_PART" ] || rm -f -- "$OFFSITE_PART"
    exit "$status"
}
trap 'finish $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${#RUNNING[@]}" -gt 0 ]; then
    docker compose stop "${RUNNING[@]}" >&2
fi

# The one-off container works with stopped services; unlike compose exec/cp
# after down, it does not depend on a container that was just removed. The
# script is mounted read-only because the production image excludes tooling.
docker compose run --rm --no-deps -T --entrypoint python \
    --volume "$ROOT/scripts/backup_restore.py:/tmp/backup_restore.py:ro" dashboard -c '
import os, pathlib, shutil, sys, tempfile
sys.path.insert(0, "/tmp")
from backup_restore import create_backup
url = os.environ.get("DATABASE_URL", "")
if not url.startswith(("sqlite:///", "sqlite+aiosqlite:///")):
    raise SystemExit("This wrapper supports SQLite only. See the PostgreSQL restore runbook.")
database = pathlib.Path(url.split("///", 1)[1].split("?", 1)[0])
attachments = pathlib.Path(os.environ.get("ATTACHMENTS_DIR") or "/app/data/attachments")
attachments.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="spooldesk-export-") as temp:
    output = pathlib.Path(temp) / "archive.zip"
    create_backup(database, attachments, output, offline=True)
    with output.open("rb") as source:
        shutil.copyfileobj(source, sys.stdout.buffer)
' > "$PART"

# Validate the bytes received by the host, rather than just the source copy.
# Input streaming keeps the host file private even when its UID differs from
# the non-root container UID.
docker compose run --rm --no-deps -T --entrypoint python \
    --volume "$ROOT/scripts/backup_restore.py:/tmp/backup_restore.py:ro" dashboard -c '
import pathlib, shutil, sys, tempfile
sys.path.insert(0, "/tmp")
from backup_restore import verify_archive
with tempfile.TemporaryDirectory(prefix="spooldesk-check-") as temp:
    path = pathlib.Path(temp) / "archive.zip"
    with path.open("wb") as dest:
        shutil.copyfileobj(sys.stdin.buffer, dest)
    verify_archive(path)
' < "$PART" >&2
ln -- "$PART" "$ARCHIVE"
rm -- "$PART"
resume_services

# Optional separately mounted storage. It must actually be on another host or
# storage system; naming a second directory on this disk is not disaster recovery.
if [ -n "${BACKUP_OFFSITE_DIR:-}" ]; then
    mkdir -p "$BACKUP_OFFSITE_DIR"
    OFFSITE="$BACKUP_OFFSITE_DIR/$(basename "$ARCHIVE")"
    # Claim an empty partial before copying, so the exit trap only removes
    # a file this invocation created. Never remove somebody else's partial.
    (set -o noclobber; : > "$OFFSITE.part")
    OFFSITE_PART="$OFFSITE.part"
    cat "$ARCHIVE" > "$OFFSITE_PART"
    cmp -s "$ARCHIVE" "$OFFSITE.part"
    ln -- "$OFFSITE.part" "$OFFSITE"
    rm -- "$OFFSITE.part"
    OFFSITE_PART=""
fi

# Retention runs only after the complete configured pipeline succeeds. Legacy
# tar.gz archives are intentionally untouched by the new archive tool.
if [ "$KEEP_DAYS" -gt 0 ]; then
    find "$DEST" -maxdepth 1 -type f -name 'spooldesk-*.zip' -mtime "+$KEEP_DAYS" -delete
fi
date -u +%Y-%m-%dT%H:%M:%SZ > "$DEST/.last-success"
echo "Verified backup: $ARCHIVE"
if [ -z "${BACKUP_OFFSITE_DIR:-}" ]; then
    echo "Offsite copy is not configured. Copy this private archive to independent storage."
fi
