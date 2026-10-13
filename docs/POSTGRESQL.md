# PostgreSQL deployment and recovery

PostgreSQL 16 is the default for new Spooldesk installations. Each company runs
its own application, database and attachment volume. This is a self-hosted
deployment boundary, not shared-database SaaS tenant isolation.

## Start a new installation

1. Run `python scripts/setup.py` in a new checkout. It generates separate database,
   session-signing and encryption secrets in `.env`. Do not commit that file.
2. Run `docker compose -p spooldesk up -d --build dashboard`. Compose starts PostgreSQL and
   waits for its health check. The dashboard manages enabled channel workers; a separate bot is not required.
3. Open `http://localhost:8000`, sign in as `admin@local` with the configured
   initial password, and create individual operator accounts or invitations.
4. For public use add the TLS overlay and complete `scripts/preflight.py` as
   described in [deployment guide](DEPLOYMENT.md).

`pgdata` stores PostgreSQL; `data` stores attachments and fallback key material.
The database port is not published. Dashboard and bot connect to host `postgres`
with the same split credentials. Passwords containing `@`, `#`, `/` and `%` are
escaped by the application; do not pre-encode `POSTGRES_PASSWORD`.

For a manual installation, provision PostgreSQL separately and set
`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
Alternatively, clear `POSTGRES_HOST` and use a correctly URL-encoded
`DATABASE_URL=postgresql+asyncpg://...`. SQLite remains available for isolated
tests and legacy recovery; production preflight requires PostgreSQL.

## Existing test installation

This project's SQLite rows were explicitly declared disposable. They are not
imported. The old database file is preserved; starting the PostgreSQL stack
creates a fresh schema and owner account. No script deletes the SQLite file or
silently edits an existing `.env`. Add a strong `POSTGRES_PASSWORD` to the existing
configuration before starting the new stack. Existing per-instance settings and
KB content must be set up in the new database.

This is not a general migration tool for customer SQLite data. A future customer
with valuable rows needs a rehearsed export/import, ID/foreign-key validation and
attachment reconciliation before switching engines.

## Schema upgrades

Startup applies ordered transactional revisions under a PostgreSQL advisory lock.
Both application processes can start without racing schema changes. Unknown newer
revisions are rejected. The conversation revision adds reply references, edit
timestamps, optimistic versions and mutation state; metadata creates revision,
presence, invitation and membership tables. Back up before upgrading. Downgrading
means restoring a matching database/application backup, not deleting revisions.

## Verified backup

Run from the checkout with Python 3.12+ and the existing Docker CLI. Set
`COMPOSE_PROJECT_NAME=spooldesk` (PowerShell: `$env:COMPOSE_PROJECT_NAME='spooldesk'`)
and `COMPOSE_FILE` for the same overlays as the installation, so the tool targets
the correct volumes. Then:

```bash
python scripts/postgres_backup.py backup --destination ./backups
python scripts/postgres_backup.py verify ./backups/spooldesk-pg-<id>.zip
# Linux wrapper: retention and optional independent storage copy
make backup
```

The tool validates the standard Compose database/storage configuration, stops
only running bot/dashboard services, runs `pg_dump -Fc`, checks its archive table,
captures attachments while writers are stopped, and restarts exactly those
services even when backup fails. Stop any additional writers yourself. A lock
prevents overlapping backups; after a host crash investigate before removing a
stale `backups/.postgres-backup.lock` directory.

Each private ZIP has the dump, content-store files, required attachment references
and a SHA-256/size manifest. Missing referenced files, changed files, unsafe paths
or duplicate members fail verification. The archive excludes `.env`, encryption
keys, logs and PostgreSQL role definitions. Protect it as customer data. The Python
command retains archives; `make backup` retains 14 days by default and optionally
copies to `BACKUP_OFFSITE_DIR`. No scheduler or offsite service is installed
automatically. `make backup-daily` explicitly installs a host cron entry.

## Restore into a fresh isolated Compose project

Only restore archives from a trusted source: a PostgreSQL dump contains SQL.
Use the same application release and PostgreSQL major version for the first
recovery. Supply a separate Compose project name and new volumes, and keep live
channel credentials out of the recovery environment. Preserve the original
`SECRETS_KEY` separately; a replacement cannot decrypt old integration secrets.

```bash
# Example: run in an isolated recovery checkout with a private recovery .env
export COMPOSE_PROJECT_NAME=spooldesk-recovery
docker compose up -d postgres
python scripts/postgres_backup.py restore /private/backup.zip --offline
```

Restore refuses running app services, a nonempty database or nonempty attachment
directory. It uses transactional `pg_restore`, restores and rechecks attachment
bytes, revokes restored operator sessions/invitations, clears presence/leases,
and quarantines pending outbound, inbound and message-edit work. It leaves the
application stopped. A failed restore may leave the *new* target partially
prepared; inspect it and retry into another fresh target, never an existing live
database.

Before starting channels: validate schema, ticket counts, attachments, operator
login, decrypted settings and quarantined jobs against provider history. Review
pending input whose external side effects are unknown. Run the dashboard alone
in a network-isolated drill first. Only then decide which deliveries need an
explicit retry. Do not delete the original deployment to make room for a drill.

For external managed PostgreSQL or custom storage, use your provider's snapshot
procedure plus a consistent attachment snapshot; the bundled tool deliberately
refuses mismatched Compose configurations. Apply the same quarantine and key
recovery steps. See `QUARANTINE_SQL` in `scripts/postgres_backup.py`.

## Recorded drill

2026-09-22: a real isolated Docker PostgreSQL 16 dump/restore passed schema, rows,
attachment bytes, operator authentication, encrypted-setting recovery and inbound,
outbound and mutation quarantine checks. The source dashboard was restarted;
the recovered dashboard stayed stopped. The synthetic small-data cycle took
28.2 seconds. This is procedure verification, not a production RTO or load test.
Repeat on the customer's data volume and independent backup storage before launch.
