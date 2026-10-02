"""Restore drills use private SQLite databases and never launch a real service."""

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import shutil
import zipfile

import pytest

from scripts.backup_restore import BackupError, create_backup, restore_archive, verify_archive


@pytest.fixture
def source(tmp_path):
    data = tmp_path / "source"
    data.mkdir()
    attachments = data / "attachments"
    attachments.mkdir()
    reference = "ab12" + "3" * 28
    file = attachments / reference[:2] / reference[2:4] / reference
    file.parent.mkdir(parents=True)
    file.write_bytes(b"customer screenshot bytes")
    database = data / "support.db"
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, channel TEXT);
        CREATE TABLE tickets (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id));
        CREATE TABLE messages (id INTEGER PRIMARY KEY, ticket_id INTEGER REFERENCES tickets(id),
                               text TEXT, file_id TEXT, deleted_at TEXT);
        INSERT INTO users VALUES (1, 'widget');
        INSERT INTO tickets VALUES (1, 1);
    """)
    conn.execute("INSERT INTO messages VALUES(1, 1, 'committed in WAL', ?, NULL)", (reference,))
    conn.commit()
    # Keep the WAL connection open so the test would fail with a raw copy of
    # the database file. Backup API must include the last committed row.
    (data / ".env").write_text("DO_NOT_ARCHIVE=environment-secret")
    (data / "secrets.key").write_text("DO_NOT_ARCHIVE=encryption-secret")
    yield database, attachments, file
    conn.close()


def test_snapshot_restore_contains_wal_commit_files_and_no_secrets(source, tmp_path):
    database, attachments, file = source
    archive = tmp_path / "backup.zip"
    created = create_backup(database, attachments, archive, offline=True)
    assert verify_archive(archive) == created
    target = tmp_path / "restored"
    restore_archive(archive, target, offline=True)
    assert not (target / "support.db-wal").exists(), "an old WAL must never accompany the snapshot"
    with closing(sqlite3.connect(target / "support.db")) as conn:
        assert conn.execute("SELECT text FROM messages").fetchone() == ("committed in WAL",)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert (target / "attachments" / file.relative_to(attachments)).read_bytes() == file.read_bytes()
    assert not (target / ".env").exists() and not (target / "secrets.key").exists()
    with zipfile.ZipFile(archive) as zipped:
        assert set(zipped.namelist()) == {"support.db", "manifest.json",
                                         "attachments/" + file.relative_to(attachments).as_posix()}


def test_missing_referenced_attachment_never_publishes_a_backup(source, tmp_path):
    database, attachments, file = source
    file.unlink()
    archive = tmp_path / "backup.zip"
    with pytest.raises(BackupError, match="missing local attachment"):
        create_backup(database, attachments, archive, offline=True)
    assert not archive.exists()


@pytest.mark.parametrize("change", ["edit", "add", "delete"])
def test_attachment_mutation_during_backup_is_not_published(source, tmp_path, monkeypatch, change):
    from scripts import backup_restore
    database, attachments, file = source
    original = backup_restore._attachment_state
    calls = 0

    def inspect(directory):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "edit":
                file.write_bytes(b"changed while archive was being written")
            elif change == "add":
                (attachments / "new-file").write_bytes(b"new upload")
            else:
                file.unlink()
        return original(directory)

    monkeypatch.setattr(backup_restore, "_attachment_state", inspect)
    archive = tmp_path / "mutating.zip"
    with pytest.raises(BackupError, match="Attachments changed"):
        create_backup(database, attachments, archive, offline=True)
    assert not archive.exists() and not archive.with_suffix(".zip.part").exists()


def test_an_existing_backup_or_restore_target_is_preserved(source, tmp_path):
    database, attachments, _ = source
    archive = tmp_path / "backup.zip"
    create_backup(database, attachments, archive, offline=True)
    contents = archive.read_bytes()
    with pytest.raises(BackupError, match="already exists"):
        create_backup(database, attachments, archive, offline=True)
    assert archive.read_bytes() == contents
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("existing data")
    with pytest.raises(BackupError, match="new directory"):
        restore_archive(archive, target, offline=True)
    assert (target / "keep.txt").read_text() == "existing data"


def test_the_offline_prerequisite_is_explicit(source, tmp_path):
    database, attachments, _ = source
    with pytest.raises(BackupError, match="Stop all app writers"):
        create_backup(database, attachments, tmp_path / "backup.zip", offline=False)
    with pytest.raises(BackupError, match="Stop all app writers"):
        restore_archive(tmp_path / "backup.zip", tmp_path / "target", offline=False)


def _alter_archive(source, target, changes):
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        for info in old.infolist():
            data = old.read(info.filename)
            new.writestr(info, changes.pop(info.filename, data))
        for name, value in changes.items():
            new.writestr(name, value)


@pytest.mark.parametrize("tampering", ["hash", "traversal", "symlink", "extra", "duplicate"])
def test_untrusted_archives_are_rejected_before_restore(source, tmp_path, tampering):
    database, attachments, file = source
    archive = tmp_path / "good.zip"
    create_backup(database, attachments, archive, offline=True)
    damaged = tmp_path / "damaged.zip"
    if tampering == "hash":
        changes = {"attachments/" + file.relative_to(attachments).as_posix(): b"changed bytes"}
    elif tampering == "traversal":
        changes = {"attachments/../../escape.txt": b"outside"}
    elif tampering == "symlink":
        info = zipfile.ZipInfo("attachments/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        changes = {info: b"../../outside"}
    else:
        changes = {"attachments/unlisted": b"unlisted"}
    _alter_archive(archive, damaged, changes)
    if tampering == "duplicate":
        with zipfile.ZipFile(damaged, "a") as zipped, pytest.warns(UserWarning):
            zipped.writestr("support.db", b"duplicate")
    target = tmp_path / "restore"
    with pytest.raises(BackupError):
        restore_archive(damaged, target, offline=True)
    assert not target.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_freshness_and_size_limits_fail_closed(source, tmp_path):
    database, attachments, _ = source
    archive = tmp_path / "good.zip"
    create_backup(database, attachments, archive, offline=True)
    with pytest.raises(BackupError, match="size limit"):
        verify_archive(archive, max_bytes=10)
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
    manifest["created_at"] = "2000-01-01T00:00:00+00:00"
    old = tmp_path / "old.zip"
    _alter_archive(archive, old, {"manifest.json": json.dumps(manifest).encode()})
    with pytest.raises(BackupError, match="older than"):
        verify_archive(old, max_age_hours=36)


def test_missing_database_never_creates_an_empty_successful_backup(tmp_path):
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    database = tmp_path / "missing.db"
    with pytest.raises(BackupError, match="must exist"):
        create_backup(database, attachments, tmp_path / "empty.zip", offline=True)
    assert not database.exists()


def test_restored_pending_outbox_requires_manual_reconciliation(source, tmp_path):
    database, attachments, _ = source
    with closing(sqlite3.connect(database)) as conn:
        conn.executescript("""
            ALTER TABLE messages ADD COLUMN delivery_status TEXT DEFAULT 'delivered';
            ALTER TABLE messages ADD COLUMN delivery_error TEXT;
            CREATE TABLE outbound_jobs (id INTEGER PRIMARY KEY, message_id INTEGER REFERENCES messages(id),
                                        status TEXT, last_error TEXT, next_attempt_at TEXT, file_key TEXT);
            CREATE TABLE worker_leases (name TEXT, owner TEXT, expires_at TEXT);
            INSERT INTO worker_leases VALUES ('outbox', 'old-worker', '2099-01-01');
        """)
        for index, status in enumerate(("pending", "retry", "sending", "delivered", "failed"), start=2):
            conn.execute("INSERT INTO messages(id,ticket_id,text,delivery_status) VALUES(?,1,'reply',?)", (index, status))
            conn.execute("INSERT INTO outbound_jobs(id,message_id,status,next_attempt_at) VALUES(?,?,?,'2099-01-01')",
                         (index, index, status))
        conn.commit()
    archive = tmp_path / "jobs.zip"
    create_backup(database, attachments, archive, offline=True)
    target = tmp_path / "restored-jobs"
    assert restore_archive(archive, target, offline=True)["quarantined_jobs"] == 3
    with closing(sqlite3.connect(target / "support.db")) as conn:
        assert conn.execute("SELECT status FROM outbound_jobs ORDER BY id").fetchall() == [
            ("uncertain",), ("uncertain",), ("uncertain",), ("delivered",), ("failed",)]
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE delivery_status='uncertain'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM worker_leases").fetchone()[0] == 0
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT status FROM outbound_jobs WHERE id=2").fetchone()[0] == "pending"


def test_restored_application_bootstraps_and_decrypts_with_the_separate_key(tmp_path):
    """Fresh runtime, real schema/seeds/password hash/encryption, disposable data."""
    root = Path(__file__).resolve().parent.parent
    source = tmp_path / "runtime-source"
    source.mkdir()
    (source / "attachments").mkdir()
    env = dict(os.environ, TESTING="1", DATABASE_URL=f"sqlite+aiosqlite:///{(source / 'support.db').as_posix()}",
               BOT_TOKEN="", ADMIN_CHAT_ID="", ACCESS_PASSWORD="test-restore-password",
               DASH_SECRET_KEY="test-restore-session-key", SECRETS_KEY="test-separate-encryption-key",
               ATTACHMENTS_DIR=str(source / "attachments"))
    bootstrap = """
import asyncio
from src.database.db import init_db, db_session
from src.database.models import AdminUser, AppSetting
from src.services.secret_store import encrypt
from sqlalchemy import select
async def run():
    await init_db()
    async with db_session() as session:
        assert (await session.execute(select(AdminUser))).first()
        session.add(AppSetting(key='restore_drill_secret', value=encrypt('dummy-provider-value')))
asyncio.run(run())
"""
    seeded = subprocess.run([sys.executable, "-c", bootstrap], env=env, cwd=root,
                            capture_output=True, text=True, timeout=30)
    assert seeded.returncode == 0, seeded.stderr
    archive = tmp_path / "runtime.zip"
    create_backup(source / "support.db", source / "attachments", archive, offline=True)
    restored = tmp_path / "runtime-restored"
    restore_archive(archive, restored, offline=True)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{(restored / 'support.db').as_posix()}"
    env["ATTACHMENTS_DIR"] = str(restored / "attachments")
    verification = """
import asyncio
from src.database.db import init_db, db_session
from src.database.models import AdminUser, AppSetting
from src.services.secret_store import decrypt
from argon2 import PasswordHasher
from sqlalchemy import select
async def run():
    await init_db()
    async with db_session() as session:
        actor = (await session.execute(select(AdminUser).where(AdminUser.email=='admin@local'))).scalar_one()
        assert actor.is_active and PasswordHasher().verify(actor.password_hash, 'test-restore-password')
        setting = await session.get(AppSetting, 'restore_drill_secret')
        assert decrypt(setting.value) == 'dummy-provider-value'
asyncio.run(run())
"""
    checked = subprocess.run([sys.executable, "-c", verification], env=env, cwd=root,
                             capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize("failure", ["", "export", "verify", "offsite"])
def test_docker_wrapper_restarts_only_previously_running_services_and_keeps_failed_backups_unpublished(
    source, tmp_path, failure,
):
    """Exercise the shell's failure/trap ordering with a mocked Docker command."""
    candidates = [Path("C:/Program Files/Git/bin/bash.exe")] if os.name == "nt" else []
    bash = next((str(p) for p in candidates if p.is_file()), None) or shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available")
    database, attachments, _ = source
    sample = tmp_path / "sample.zip"
    create_backup(database, attachments, sample, offline=True)
    commands = tmp_path / "commands.jsonl"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("""#!/usr/bin/env python
import json, os, sys
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
args = sys.argv[1:]
if 'config' in args:
    if '--environment' in args:
        print('DATABASE_URL=sqlite+aiosqlite:////tmp/test.db')
    else:
        print('services:\\n  dashboard:\\n    environment:\\n      DATABASE_URL: sqlite+aiosqlite:////app/data/support.db')
elif 'ps' in args:
    print('dashboard')
elif 'run' in args:
    phase = 'export' if 'create_backup' in args[-1] else 'verify'
    if phase == os.environ.get('FAKE_FAILURE'):
        raise SystemExit(7)
    if phase == 'export':
        with open(os.environ['FAKE_ARCHIVE'], 'rb') as archive:
            sys.stdout.buffer.write(archive.read())
    else:
        assert sys.stdin.buffer.read()
""", encoding="utf-8", newline="\n")
    docker.chmod(0o700)
    # Git Bash on Windows lacks flock. This test covers lifecycle/trap
    # handling, while the real Linux wrapper uses the OS lock utility.
    flock = binaries / "flock"
    flock.write_text("#!/bin/sh\nexit 0\n", newline="\n")
    flock.chmod(0o700)
    compare = binaries / "cmp"
    compare.write_text("""#!/usr/bin/env python
import os, pathlib, sys
if os.environ.get('FAKE_FAILURE') == 'offsite':
    raise SystemExit(7)
raise SystemExit(0 if pathlib.Path(sys.argv[-2]).read_bytes() == pathlib.Path(sys.argv[-1]).read_bytes() else 1)
""", encoding="utf-8", newline="\n")
    compare.chmod(0o700)
    root = Path(__file__).resolve().parent.parent

    def shell_path(path):
        value = path.as_posix()
        return "/" + value[0].lower() + value[2:] if os.name == "nt" else value

    destination = tmp_path / "backups"
    env = dict(os.environ, FAKE_LOG=str(commands), FAKE_ARCHIVE=str(sample), FAKE_FAILURE=failure,
               BACKUP_KEEP_DAYS="14", BACKUP_OFFSITE_DIR=str(tmp_path / "offsite"), PYTHONIOENCODING="utf-8")
    result = subprocess.run([
        bash, "-c", 'export PATH="$1:$PATH"; exec bash "$2" "$3"', "test",
        shell_path(binaries), shell_path(root / "scripts" / "backup.sh"), shell_path(destination),
    ], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == (7 if failure else 0), result.stderr
    log = [json.loads(line) for line in commands.read_text().splitlines()]
    assert [entry for entry in log if "stop" in entry] == [["compose", "stop", "dashboard"]]
    assert [entry for entry in log if "start" in entry] == [["compose", "start", "dashboard"]]
    assert bool(list(destination.glob("spooldesk-*.zip"))) is (failure in {"", "offsite"})
    assert not list(destination.glob("*.part"))
    assert not list((tmp_path / "offsite").glob("*.part"))
    assert bool(list((tmp_path / "offsite").glob("*.zip"))) is (not bool(failure))
    assert (destination / ".last-success").exists() is (not bool(failure))


@pytest.mark.parametrize("dotenv_url,shell_url,service_url,refused", [
    ("postgresql+asyncpg://u:p@postgres/db", None, "sqlite+aiosqlite:////app/data/support.db", True),
    ("sqlite+aiosqlite:///local.db", "postgresql://u:p@host/db", "sqlite+aiosqlite:////app/data/support.db", True),
    ("postgresql://u:p@host/db", "sqlite+aiosqlite:///explicit.db", "sqlite+aiosqlite:////app/data/support.db", False),
    ("sqlite+aiosqlite:///local.db", None, "postgresql+asyncpg://u:p@postgres/db", True),
    ("sqlite+aiosqlite:///local.db", None, "mysql://u:p@host/db", True),
])
def test_backup_rejects_unsupported_configuration_before_creating_destination(
    tmp_path, dotenv_url, shell_url, service_url, refused,
):
    """CI's PostgreSQL refusal, shell precedence and an effective PG overlay."""
    candidates = [Path("C:/Program Files/Git/bin/bash.exe")] if os.name == "nt" else []
    bash = next((str(p) for p in candidates if p.is_file()), None) or shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available")
    isolated = tmp_path / "checkout"
    scripts = isolated / "scripts"
    scripts.mkdir(parents=True)
    repository = Path(__file__).resolve().parent.parent
    shutil.copyfile(repository / "scripts" / "backup.sh", scripts / "backup.sh")
    (isolated / ".env").write_text("DATABASE_URL=" + dotenv_url + "\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    commands = tmp_path / "commands.jsonl"
    docker = binaries / "docker"
    docker.write_text("""#!/usr/bin/env python
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args == ['compose', 'config', '--environment']:
    dotenv = pathlib.Path('.env').read_text().strip().split('=', 1)[1]
    print('DATABASE_URL=' + os.environ.get('DATABASE_URL', dotenv))
elif args == ['compose', 'config']:
    print('services:\\n  dashboard:\\n    environment:\\n      DATABASE_URL: "' + os.environ['FAKE_SERVICE_URL'] + '"')
else:
    raise SystemExit(99)
""", encoding="utf-8", newline="\n")
    docker.chmod(0o700)
    flock = binaries / "flock"
    flock.write_text("#!/bin/sh\nexit 0\n", newline="\n")
    flock.chmod(0o700)

    def shell_path(path):
        value = path.as_posix()
        return "/" + value[0].lower() + value[2:] if os.name == "nt" else value

    env = dict(os.environ, FAKE_LOG=str(commands), FAKE_SERVICE_URL=service_url,
               BACKUP_KEEP_DAYS="14", BACKUP_OFFSITE_DIR="")
    env.pop("DATABASE_URL", None)
    if shell_url is not None:
        env["DATABASE_URL"] = shell_url
    destination = tmp_path / "should-not-appear"
    result = subprocess.run([
        bash, "-c", 'export PATH="$1:$PATH"; exec bash "$2" "$3"', "test",
        shell_path(binaries), shell_path(scripts / "backup.sh"), shell_path(destination),
    ], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == (1 if refused else 99), result.stderr
    assert destination.exists() is (not refused)
    log = [json.loads(line) for line in commands.read_text().splitlines()]
    if refused:
        assert all(entry[:2] == ["compose", "config"] for entry in log)
    assert "u:p@" not in result.stdout + result.stderr
