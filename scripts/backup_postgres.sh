#!/usr/bin/env bash
# Standard PostgreSQL deployment. Existing SQLite archives use backup.sh.
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DEST="${1:-$ROOT/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || { echo "Invalid BACKUP_KEEP_DAYS" >&2; exit 1; }
PYTHON="${SPOOLDESK_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || { echo "Python 3.12+ is required; set SPOOLDESK_PYTHON to its path." >&2; exit 1; }
"$PYTHON" scripts/postgres_backup.py backup --destination "$DEST"
# Copy only complete verified archives, never credentials or partial files.
if [ -n "${BACKUP_OFFSITE_DIR:-}" ]; then
    mkdir -p "$BACKUP_OFFSITE_DIR"
    for source in "$DEST"/spooldesk-pg-*.zip; do
        [ -f "$source" ] || continue
        target="$BACKUP_OFFSITE_DIR/$(basename "$source")"
        if [ -e "$target" ]; then
            cmp -s "$source" "$target" || { echo "Offsite checksum mismatch" >&2; exit 1; }
            continue
        fi
        partial="$(mktemp "$BACKUP_OFFSITE_DIR/.spooldesk-copy-XXXXXX")"
        trap 'rm -f -- "$partial"' EXIT
        cat "$source" > "$partial"
        cmp -s "$source" "$partial"
        ln -- "$partial" "$target"
        rm -- "$partial"
        trap - EXIT
    done
fi
if [ "$KEEP_DAYS" -gt 0 ]; then
    find "$DEST" -maxdepth 1 -type f -name 'spooldesk-pg-*.zip' -mtime "+$KEEP_DAYS" -delete
fi
date -u +%Y-%m-%dT%H:%M:%SZ > "$DEST/.last-success"
