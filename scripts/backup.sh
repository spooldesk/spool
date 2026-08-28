#!/usr/bin/env bash
# One archive holding both halves of the record.
#
# A helpdesk's data is a database and a directory of attachments, and they
# are useless apart: a restored database whose attachments are missing is a
# support history full of dead paperclips, and attachments with no database
# are a directory of files nobody can name. So they are backed up together,
# in one file, at one instant.
#
# The database is copied with SQLite's own online-backup API rather than by
# copying the file. In WAL mode — which this runs in, so the dashboard can
# read while the bot writes — the file on disk is not a complete database on
# its own, and `cp support.db` at the wrong moment produces an archive that
# restores into a missing hour.
#
# Usage:
#   bash scripts/backup.sh [destination-directory]
#
# Restore:
#   docker compose down
#   tar xzf spooldesk-YYYY-MM-DD-HHMM.tar.gz -C /tmp/restore
#   docker compose cp /tmp/restore/support.db dashboard:/app/data/support.db
#   docker compose cp /tmp/restore/attachments dashboard:/app/data/
#   docker compose up -d
#
# Restoring a WAL database means the -wal and -shm files must NOT come back
# with it; this archive deliberately contains neither, because the snapshot
# inside it is already complete.

set -euo pipefail

DEST="${1:-./backups}"
STAMP="$(date +%Y-%m-%d-%H%M)"
ARCHIVE="$DEST/spooldesk-$STAMP.tar.gz"

cd "$(dirname "$0")/.."

if ! docker compose ps --status running --services 2>/dev/null | grep -qx dashboard; then
	echo "The dashboard container is not running; nothing to back up from." >&2
	echo "Start it first, or restore from an older archive." >&2
	exit 1
fi

mkdir -p "$DEST"

# Everything happens inside the container, in one shot, so the database
# snapshot and the attachment listing describe the same moment.
docker compose exec -T dashboard sh -c '
set -e
python - <<"PY"
import sqlite3, os

src_path = os.environ.get("DATABASE_URL", "").split("///")[-1] or "/app/data/support.db"
src = sqlite3.connect(src_path)
dst = sqlite3.connect("/tmp/support.db")
# Copies a consistent view even while the bot is writing. This is the whole
# reason the script is not two lines of cp.
src.backup(dst)
dst.close()
src.close()
PY
if [ -d /app/data/attachments ]; then
	tar czf - -C /tmp support.db -C /app/data attachments
else
	tar czf - -C /tmp support.db
fi
rm -f /tmp/support.db
' > "$ARCHIVE"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo "Wrote $ARCHIVE ($SIZE)"
echo
echo "This is not a backup until it is somewhere else. Copy it off this"
echo "machine — the disk holding the only copy is the disk that fails."
