> New/default installations use PostgreSQL. `make backup` and `make backup-daily`
> now use `scripts/postgres_backup.py` through `backup_postgres.sh`. Follow
> [the PostgreSQL runbook](POSTGRESQL.md), including the recorded real restore drill.
> The SQLite sections below are retained for legacy archives. Their shell command
> is `bash scripts/backup.sh`, not the current `make backup` default.

# Backup and recovery

The SQLite tool backs up the database and local attachments, verifies their
hashes and database integrity, and restores into a **new directory**. It never
loads `.env`, includes `secrets.key`, overwrites an existing database or starts
an application. Python 3.12+ is sufficient; application dependencies are not
needed for the archive tool.

## What must be preserved

- A verified archive containing `support.db`, `attachments/` and `manifest.json`.
- Separately secured configuration and the **original `SECRETS_KEY`**, or the
  `secrets.key` file if the installation used file-based key storage. Without
  this key, encrypted provider credentials cannot be recovered. Mailbox
  credentials from the environment must be preserved in that separate configuration.
- The application release, deployment configuration, domain and provider account
  details. Store recovery material outside the server being backed up.

Archives contain private customer conversations and files. SHA-256 detects
corruption; it does not authenticate an archive deliberately modified by an
attacker. Restrict access and encrypt offsite storage. The tool's ZIP files do
not encrypt their contents.

## Create and verify a SQLite backup

Database and attachment storage must be quiet together. Stop **every process
that can write**, including the bot, dashboard, mailbox intake and delivery
workers. An online SQLite database snapshot alone cannot make a concurrently
changing attachment directory consistent.

For the standard Linux Docker deployment:

```bash
make backup
# Optional: a separately mounted storage system, with its own failure domain.
BACKUP_OFFSITE_DIR=/mnt/offsite/spooldesk make backup
```

`scripts/backup.sh` remembers which `bot`/`dashboard` services were running,
stops those services, uses a one-off container to snapshot and verify, and
restarts those same services even when backup fails. It requires an existing
image and `flock` from util-linux. Other installations must stop their additional
writers themselves. Use the same `COMPOSE_FILE`/project settings as the deployed
stack; for overlays, export `COMPOSE_FILE` with the appropriate files first.

The final `backups/spooldesk-*.zip` name is published only after verification.
Interrupted `.part` files are never complete backups. Local retention defaults
to 14 days (`BACKUP_KEEP_DAYS=0` disables pruning). Pruning only runs after the
configured local/offsite pipeline succeeds. Old `.tar.gz` archives are not
pruned or converted by this tool.

For a stopped installation without Docker, give explicit paths:

```bash
python scripts/backup_restore.py backup \
  --database /srv/spooldesk/data/support.db \
  --attachments /srv/spooldesk/data/attachments \
  --output /srv/backups/spooldesk-2026-09-17.zip --offline

python scripts/backup_restore.py verify /srv/backups/spooldesk-2026-09-17.zip
```

Create an empty attachment directory first if the instance has never stored
files. A missing database or a missing local file referenced by the snapshot
fails the backup. Remote-only attachments held by Telegram/Slack are not exported
by this tool; their continued availability also depends on the provider.

Verification checks every member, SHA-256 and size, SQLite integrity and foreign
keys, and local file references. It rejects archive path traversal, symlinks,
duplicate members and unexpected contents. The default verification/extraction
size limit is 50 GiB; standalone `verify`/`restore` accept `--max-bytes` for a
larger known archive. The Docker wrapper currently uses the default limit.

Backup also compares the attachment file set and metadata before and after
archiving, and refuses ordinary additions, deletions or changes during the copy.
This supplements the offline requirement; it is not a filesystem snapshot and
cannot defend against a local attacker changing files and metadata together.
An interrupted offsite copy removes only the partial file created by that run.

## Restore SQLite onto a new host or directory

1. Keep the old deployment stopped. Prepare a separate recovery environment and
   restrict outbound network access during the drill so restored workers cannot
   contact real customers or mailbox accounts.
2. Retrieve the trusted archive and original encryption key through separate
   protected channels. Verify the archive before selecting it for recovery.
3. Restore into a path that does **not** exist:

   ```bash
   python scripts/backup_restore.py restore /srv/backups/spooldesk-2026-09-17.zip \
     --target /srv/spooldesk/restored-data --offline
   ```

4. Restore `SECRETS_KEY` through the new environment, or place the original
   `secrets.key` beside the restored database with restrictive permissions.
   Rotate `DASH_SECRET_KEY` to invalidate pre-disaster session cookies. Review
   active operators and roles because changes made after the snapshot are lost.
5. Configure the application to use the new database and attachments directory.
   For the default Docker image, the files must be readable/writable by UID
   10001. Point both services at the **same** recovered directory. For example,
   add an explicit recovery overlay:

   ```yaml
   services:
     dashboard:
       volumes:
         - /srv/spooldesk/restored-data:/app/data
     bot:
       volumes:
         - /srv/spooldesk/restored-data:/app/data
   ```

   The volume entry replaces the `/app/data` mount when merged by Compose. Keep
   the original volume for rollback. Do not copy a restored database over a live
   database or place an old `-wal`/`-shm` file beside it.
6. Start the target application release in the isolated recovery environment.
   Check `/healthz`, operator sign-in, roles, a known ticket and attachment,
   encrypted credential availability, and a test customer reply using test
   channel credentials. Inspect migration and worker errors before enabling
   public traffic.
7. Reconcile outbound delivery and provider history, then enable the recovered
   installation. Record the snapshot cutoff and any unrecoverable interval.

Restore changes `pending`, `retry` and `sending` outbound jobs to **uncertain**
and clears old worker leases in the restored copy. Their matching transcript
rows also become uncertain. A send that was pending at backup time may have
succeeded afterwards; replaying it automatically would risk duplicate customer
messages. Operators must check provider history before manually retrying these
jobs. Confirmed `delivered` and `failed` statuses are preserved. External events
and messages after the backup cutoff may require further reconciliation.

For legacy `.tar.gz` archives, stage trusted contents into a **new** directory
while writers are stopped, verify the SQLite database and local references,
then create and restore a new-format archive to apply the recovery safeguards.
Never run the old `compose down` followed by `compose cp` instructions: `down`
removes the container addressed by the copy command.

## Scheduling, offsite copies and alarms

`make backup-daily` installs Linux cron; `make backup-status` shows its entry.
Backups briefly interrupt the standard deployment, so choose a maintenance
window. Environment variables supplied during a manual shell command are not
automatically inherited by cron. Configure `BACKUP_OFFSITE_DIR`, retention and
Compose project settings in the scheduler's environment as well.

Keep an independently retained offsite copy. `BACKUP_OFFSITE_DIR` performs a
byte-for-byte checked copy to an already provisioned mount; it does not provision
remote storage, encryption, credentials or offsite retention. A second directory
on the same disk does not protect against loss of that disk.

Monitor command exit status and archive age from outside the application. For
daily backups, an example freshness check is:

```bash
python scripts/backup_restore.py verify /path/to/latest-known-good.zip --max-age-hours 36
```

Route a nonzero exit or missing archive to the operator's actual alert channel.
The cron log alone is not an alert. `.last-success` is written only after all
configured backup steps succeed, but verify the archive itself as well.
Configure independent offsite retention, such as 30 daily and 12 monthly copies.

Run a restore drill at least monthly and before a risky upgrade. Record archive
ID, application release, restored ticket/file checks, key recovery, outbound
reconciliation, elapsed recovery time and the offsite copy used. A daily schedule
targets at most 24 hours of data loss only while backups succeed. Measure recovery
time on a realistically sized archive instead of assuming a target has been met.

## PostgreSQL

See [POSTGRESQL.md](POSTGRESQL.md) for the standard deployment, verified archive
format, empty-target restore, key recovery and quarantining of pending work.
The real Docker PostgreSQL recovery drill passed on 2026-09-22; repeat it on the
target installation and independently stored backup before production use.
