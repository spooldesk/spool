#!/usr/bin/env bash
# Put the backup on a clock.
#
# `make backup` produces a good archive and always has. What it could not do
# is run when nobody is at the keyboard, which is the only time a backup
# matters — the machine fails on a Tuesday, not during a maintenance window.
# So this installs a cron entry that runs it every night.
#
# Deliberately host cron rather than a container: scripts/backup.sh drives
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
  cd $ROOT && bash scripts/backup.sh
EOF
	exit 1
fi

mkdir -p "$ROOT/logs"

# Output goes to a log rather than to cron's mail, which on most servers is
# nowhere. A backup whose failure is invisible is not a backup.
ENTRY="$MIN $HOUR * * * cd $ROOT && bash scripts/backup.sh >> $LOG 2>&1 $MARKER"

{ without_ours; echo "$ENTRY"; } | crontab -

cat <<EOF
Scheduled. Spooldesk will back itself up every day at $AT.

  entry:  $ENTRY
  log:    $LOG
  keep:   ${BACKUP_KEEP_DAYS:-14} days of archives, older ones deleted

Two things this does NOT do, and both of them matter:

  1. The archives stay on this machine. The disk holding the only copy is
     the disk that fails. Copy them somewhere else — a Hetzner Storage Box,
     S3, another server, anything with a different power supply.

  2. Nothing has restored one. A backup nobody has restored is a hypothesis.
     Try it once, now, while it is cheap: the restore steps are at the top
     of scripts/backup.sh.
EOF
