#!/usr/bin/env bash
# Put the backup on a clock.
#
# Installs a nightly run of the verified PostgreSQL backup wrapper. Each run briefly
# pauses the app services; choose a suitable maintenance window.
#
# Deliberately host cron rather than a container: scripts/backup_postgres.sh drives
# `docker compose` from outside, and giving a container the Docker socket so
# it can back up its neighbour is a much larger hole than this closes.
#
# Usage:
#   bash scripts/backup_schedule.sh            # install, 03:30 nightly
#   bash scripts/backup_schedule.sh 04:15      # install at another time
#   bash scripts/backup_schedule.sh --remove   # take it back out
#   bash scripts/backup_schedule.sh --show     # what is installed now
#
# The entry is tagged with a marker comment so this script can find and
# replace exactly its own line, and never touches anything else in the
# crontab.

set -euo pipefail

MARKER="# spooldesk-backup"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/logs/backup.log"
# These characters need a different cron/shell quoting strategy. Reject them
# rather than turning a checkout directory name into an executable cron fragment.
case "$ROOT" in
    *\$*|*\`*|*\"*|*\\*|*%*|*$'\n'*|*$'\r'*)
        echo "This checkout path cannot be safely embedded in a cron entry." >&2
        exit 1
        ;;
esac

have_cron() {
	command -v crontab >/dev/null 2>&1
}

current() {
	crontab -l 2>/dev/null || true
}

without_ours() {
	current | grep -v -F "$MARKER" || true
}

case "${1:-}" in
--show)
	have_cron || { echo "cron is not installed on this machine." >&2; exit 1; }
	line="$(current | grep -F "$MARKER" || true)"
	if [ -n "$line" ]; then
		echo "$line"
	else
		echo "No Spooldesk backup is scheduled."
	fi
	exit 0
	;;
--remove)
	have_cron || { echo "cron is not installed on this machine." >&2; exit 1; }
	without_ours | crontab -
	echo "Removed. Nothing is backing this instance up now."
	exit 0
	;;
esac

AT="${1:-03:30}"
if ! printf '%s' "$AT" | grep -qE '^([01][0-9]|2[0-3]):[0-5][0-9]$'; then
	echo "Give the time as HH:MM in 24-hour form, e.g. 03:30." >&2
	exit 1
fi
HOUR="${AT%%:*}"
MIN="${AT##*:}"
# Strip a leading zero so cron does not read it as octal.
HOUR="$((10#$HOUR))"
MIN="$((10#$MIN))"

if ! have_cron; then
	cat >&2 <<EOF
cron is not installed on this machine, so there is nothing to install into.

On Debian/Ubuntu:  sudo apt install cron
Then run this again.

If this host uses systemd timers instead, the command to schedule is:
  cd $ROOT && bash scripts/backup_postgres.sh
EOF
	exit 1
fi

mkdir -p "$ROOT/logs"

# Output goes to a log rather than to cron's mail, which on most servers is
# nowhere. A backup whose failure is invisible is not a backup.
ENTRY="$MIN $HOUR * * * cd \"$ROOT\" && bash scripts/backup_postgres.sh >> \"$LOG\" 2>&1 $MARKER"

{ without_ours; echo "$ENTRY"; } | crontab -

cat <<EOF
Scheduled. Spooldesk will back itself up every day at $AT.

  entry:  $ENTRY
  log:    $LOG
  keep:   14 days by default; configure BACKUP_KEEP_DAYS in cron's environment

Two things this does NOT do, and both of them matter:

  1. Configure BACKUP_OFFSITE_DIR in cron's environment, or copy verified
     archives to independent storage through your own backup system.

  2. Nothing has restored one. A backup nobody has restored is a hypothesis.
     Follow docs/BACKUP_RESTORE.md and configure an external failure/freshness
     alarm; a log file alone does not alert anybody.
EOF
