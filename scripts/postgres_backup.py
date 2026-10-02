#!/usr/bin/env python3
"""PostgreSQL + attachment recovery for the standard Docker Compose deployment.

Only stdlib and an existing Docker CLI are required. Never reads .env directly,
includes key material, overwrites a database, or starts a restored application.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 50 * 1024**3
CHUNK = 1024 * 1024
FILE = re.compile(r'attachments/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{32})\Z')
REFS_SQL = """SELECT DISTINCT file_id FROM messages m JOIN tickets t ON t.id=m.ticket_id
JOIN users u ON u.id=t.user_id WHERE u.channel IN ('widget','email')
AND file_id IS NOT NULL AND m.deleted_at IS NULL
UNION SELECT file_key FROM outbound_jobs j JOIN messages m ON m.id=j.message_id
WHERE file_key IS NOT NULL AND m.deleted_at IS NULL"""
QUARANTINE_SQL = """BEGIN;
UPDATE messages SET delivery_status='uncertain', delivery_error='e_delivery_uncertain'
WHERE id IN (SELECT message_id FROM outbound_jobs WHERE status IN ('pending','retry','sending'));
UPDATE outbound_jobs SET status='uncertain', last_error='restored_backup_delivery_unknown',
next_attempt_at=NULL WHERE status IN ('pending','retry','sending');
UPDATE inbound_events SET status='failed', last_error='restored_backup_requires_review'
WHERE status='pending';
UPDATE messages SET mutation_status='uncertain' WHERE mutation_status='pending';
UPDATE message_revisions SET status='uncertain' WHERE status='pending';
DELETE FROM worker_leases;
DELETE FROM conversation_presence;
UPDATE admin_users SET session_version=session_version+1;
UPDATE operator_invitations SET revoked_at=CURRENT_TIMESTAMP WHERE accepted_at IS NULL;
COMMIT;"""


class BackupError(ValueError):
    pass


def digest(handle):
    size, sha = 0, hashlib.sha256()
    while chunk := handle.read(CHUNK):
        size += len(chunk)
        sha.update(chunk)
    return {'size': size, 'sha256': sha.hexdigest()}


def safe_name(name):
    if name in {'manifest.json', 'database.dump'}:
        return True
    match = FILE.fullmatch(name)
    return bool(match and match[3].startswith(match[1] + match[2]))


def verify(path: Path, *, attachments_only=False, max_bytes=LIMIT):
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = {item.filename for item in members}
        required = {'manifest.json'} if attachments_only else {'manifest.json', 'database.dump'}
        if len(names) != len(members) or not required <= names:
            raise BackupError('Duplicate entries or missing database/manifest')
        if any(not safe_name(i.filename) or i.is_dir() or stat.S_ISLNK(i.external_attr >> 16)
               for i in members):
            raise BackupError('Unsafe archive path or symlink')
        if sum(i.file_size for i in members) > max_bytes or archive.getinfo('manifest.json').file_size > 16*1024**2:
            raise BackupError('Archive exceeds size limit')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('format_version') != 1 or manifest.get('database_engine') != 'postgresql':
            raise BackupError('Not a supported PostgreSQL archive')
        files = manifest.get('files')
        if not isinstance(files, dict) or set(files) != names - {'manifest.json'}:
            raise BackupError('Manifest must describe exactly the archive contents')
        timestamp = datetime.fromisoformat(manifest['created_at'])
        if timestamp.tzinfo is None or (timestamp-datetime.now(timezone.utc)).total_seconds() > 3600:
            raise BackupError('Invalid archive timestamp')
        for name, expected in files.items():
            with archive.open(name) as source:
                if digest(source) != expected:
                    raise BackupError('Archive checksum mismatch')
        if not attachments_only:
            with archive.open('database.dump') as source:
                if source.read(5) != b'PGDMP':
                    raise BackupError('Database is not a custom-format PostgreSQL dump')
            references = manifest.get('required_attachments')
            if not isinstance(references, list) or any(not isinstance(r, str) or not FILE.fullmatch(r) for r in references):
                raise BackupError('Invalid attachment references')
            if set(references) - names:
                raise BackupError('Missing local attachment referenced by the database')
    return manifest


def export_attachments(directory: Path, output: Path):
    if directory.is_symlink() or directory.is_junction():
        raise BackupError('Attachment root cannot be a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    def state():
        result = {}
        for path in sorted(directory.rglob('*')):
            if path.is_symlink() or path.is_junction():
                raise BackupError('Attachment links are not permitted')
            info = path.stat()
            if stat.S_ISDIR(info.st_mode):
                continue
            name = 'attachments/' + path.relative_to(directory).as_posix()
            if not stat.S_ISREG(info.st_mode) or not FILE.fullmatch(name) or not safe_name(name):
                raise BackupError('Unexpected attachment file; expected content-store keys only')
            result[name] = (path, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return result
    before = state()
    if sum(v[1] for v in before.values()) > LIMIT:
        raise BackupError('Attachments exceed size limit')
    manifest = {'format_version':1, 'database_engine':'postgresql',
                'created_at':datetime.now(timezone.utc).isoformat(), 'files':{}}
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, (path, *_) in before.items():
            with path.open('rb') as source:
                manifest['files'][name] = digest(source)
            archive.write(path, name)
        archive.writestr('manifest.json', json.dumps(manifest))
    if state() != before:
        raise BackupError('Attachments changed while writers should have been stopped')
    verify(output, attachments_only=True)


def import_attachments(archive_path: Path, target: Path):
    manifest = verify(archive_path, attachments_only=True)
    if target.is_symlink() or target.is_junction() or (target.exists() and any(target.iterdir())):
        raise BackupError('Attachments restore requires an empty directory')
    # The caller leaves application writers stopped throughout recovery.
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for name in manifest['files']:
            if name == 'database.dump':
                continue
            path = target.joinpath(*PurePosixPath(name).parts[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, path.open('xb') as destination:
                shutil.copyfileobj(source, destination, CHUNK)
            with path.open('rb') as source:
                if digest(source) != manifest['files'][name]:
                    raise BackupError('Archive changed after verification')


class Compose:
    def run(self, *args, data=None, output=None):
        # Compose resolves its own environment. Never log its rendered config,
        # stderr (which may contain credentials) or a dump's contents.
        input_options = {'stdin':data} if hasattr(data, 'read') else {'input':data}
        result = subprocess.run(['docker','compose',*args], cwd=ROOT, **input_options,
                                stdout=output or subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise BackupError('Docker Compose command failed; inspect the service locally (no credentials logged)')
        return result.stdout

    def validate(self):
        config = json.loads(self.run('config','--format','json'))
        services = config.get('services', {})
        pg = services.get('postgres', {}).get('environment', {})
        if not pg.get('POSTGRES_DB') or not pg.get('POSTGRES_USER'):
            raise BackupError('Standard Compose PostgreSQL service is required')
        for service in ('dashboard','bot'):
            env = services.get(service, {}).get('environment', {})
            if (env.get('DATABASE_URL') or env.get('POSTGRES_HOST') != 'postgres'
                or env.get('POSTGRES_PORT') != '5432'
                or env.get('POSTGRES_DB') != pg['POSTGRES_DB']
                or env.get('POSTGRES_USER') != pg['POSTGRES_USER']
                or env.get('ATTACHMENTS_DIR') != '/app/data/attachments'):
                raise BackupError('Nonstandard database/storage configuration; use the documented manual procedure')

    def running(self):
        return [s for s in self.run('ps','--status','running','--services').decode().splitlines()
                if s in {'dashboard','bot'}]

    def sql(self, query):
        return self.run('exec','-T','postgres','sh','-c',
            'exec psql -X -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"', data=query.encode())

    def helper(self, operation, *, data=None, output=None):
        return self.run('run','--rm','--no-deps','-T','--entrypoint','python',
            '--volume', str(Path(__file__).resolve())+':/tmp/postgres_backup.py:ro',
            'dashboard','/tmp/postgres_backup.py', operation, data=data, output=output)


@contextmanager
def lock(directory):
    path = directory / '.postgres-backup.lock'
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise BackupError('Another backup holds the lock; investigate a stale lock before removing it') from None
    try:
        yield
    finally:
        path.rmdir()


def backup(destination: Path, compose=None):
    compose = compose or Compose()
    compose.validate()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock(destination), tempfile.TemporaryDirectory(prefix='.pg-backup-', dir=destination) as temporary:
        directory = Path(temporary)
        running = compose.running()
        try:
            if running:
                compose.run('stop', *running)
            dump = directory / 'database.dump'
            with dump.open('xb') as output:
                compose.run('exec','-T','postgres','sh','-c',
                    'exec pg_dump -Fc --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"', output=output)
            with dump.open('rb') as source:
                compose.run('exec','-T','postgres','pg_restore','--list', data=source)
            refs = compose.sql(REFS_SQL).decode().splitlines()
            required = ['attachments/'+r[:2]+'/'+r[2:4]+'/'+r for r in refs if re.fullmatch('[0-9a-f]{32}', r)]
            attachments = directory / 'attachments.zip'
            with attachments.open('xb') as output:
                compose.helper('_export', output=output)
        finally:
            if running:
                compose.run('start', *running)
        manifest = verify(attachments, attachments_only=True)
        manifest['required_attachments'] = sorted(required)
        with dump.open('rb') as source:
            manifest['files']['database.dump'] = digest(source)
        staged = directory / 'complete.zip'
        with zipfile.ZipFile(staged, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(dump, 'database.dump')
            with zipfile.ZipFile(attachments) as files:
                for name in manifest['files']:
                    if name == 'database.dump':
                        continue
                    with files.open(name) as source, archive.open(name, 'w', force_zip64=True) as target:
                        shutil.copyfileobj(source, target, CHUNK)
            archive.writestr('manifest.json', json.dumps(manifest))
        verify(staged)
        output = destination / ('spooldesk-pg-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]+'.zip')
        os.chmod(staged, 0o600)
        os.link(staged, output)  # exclusive publication; never replace a backup
        return output


def restore(archive: Path, *, offline: bool, compose=None):
    if not offline:
        raise BackupError('Stop all application writers and pass --offline')
    verify(archive)
    compose = compose or Compose()
    compose.validate()
    if compose.running():
        raise BackupError('Stop dashboard and bot first; recovery never starts them automatically')
    count = compose.sql("SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'").decode().strip()
    if count != '0':
        raise BackupError('Restore requires a fresh empty database; existing tables are never overwritten')
    compose.helper('_empty')
    with tempfile.TemporaryDirectory(prefix='spooldesk-pg-restore-') as temporary:
        dump = Path(temporary) / 'database.dump'
        with zipfile.ZipFile(archive) as bundle, bundle.open('database.dump') as source, dump.open('xb') as target:
            shutil.copyfileobj(source, target, CHUNK)
        # File-backed stdin avoids loading a large database into memory.
        with dump.open('rb') as source:
            compose.run('exec','-T','postgres','sh','-c',
                'exec pg_restore --exit-on-error --single-transaction --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"', data=source)
        compose.sql(QUARANTINE_SQL)
        # Stream the bundle through a temporary file in the helper container.
        with archive.open('rb') as source:
            compose.helper('_import', data=source)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('backup'); p.add_argument('--destination', type=Path, default=ROOT/'backups')
    p = sub.add_parser('verify'); p.add_argument('archive', type=Path)
    p = sub.add_parser('restore'); p.add_argument('archive', type=Path); p.add_argument('--offline', action='store_true')
    for mode in ('_export','_import','_empty'):
        sub.add_parser(mode, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command.startswith('_'):
            target = Path('/app/data/attachments')
            if args.command == '_empty':
                if target.is_symlink() or (target.exists() and any(target.iterdir())):
                    raise BackupError('Attachments directory is not empty')
                return 0
            with tempfile.TemporaryDirectory(prefix='spooldesk-pg-files-') as temp:
                path = Path(temp)/'files.zip'
                if args.command == '_export':
                    export_attachments(target, path)
                    with path.open('rb') as source:
                        shutil.copyfileobj(source, sys.stdout.buffer, CHUNK)
                else:
                    with path.open('xb') as output:
                        shutil.copyfileobj(sys.stdin.buffer, output, CHUNK)
                    import_attachments(path, target)
            return 0
        if args.command == 'backup':
            print('Verified backup:', backup(args.destination))
        elif args.command == 'verify':
            manifest = verify(args.archive)
            print('Verified PostgreSQL archive:', len(manifest['files']), 'files; created', manifest['created_at'])
        else:
            restore(args.archive, offline=args.offline)
            print('Restored. Application remains stopped. Restore keys separately, inspect quarantined jobs, then validate before starting channels.')
        return 0
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        # Do not dump exception content: corrupt archive/config text can contain secrets.
        print('Backup/recovery failed. No existing database is overwritten; keep a failed restore stopped. Check the documented prerequisites and target configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
