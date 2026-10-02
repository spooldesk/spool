#!/usr/bin/env python3
"""Offline SQLite + attachment archives, verification and non-overwriting restore.

Uses only the Python standard library. It never imports the application, reads
.env, infers a database path, or includes the encryption key. Stop every app
writer first; --offline acknowledges that operational prerequisite.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import zipfile

VERSION = 1
MAX_BYTES = 50 * 1024**3
CHUNK = 1024 * 1024
_REF = re.compile(r"[0-9a-f]{32}\Z")


class BackupError(ValueError):
    """An incomplete, unsafe or incompatible archive must not be published."""


def _digest(handle) -> tuple[int, str]:
    size, checksum = 0, hashlib.sha256()
    while chunk := handle.read(CHUNK):
        size += len(chunk)
        checksum.update(chunk)
    return size, checksum.hexdigest()


def _check_database(path: Path) -> None:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("SQLite foreign-key check failed")


def _required_files(database: Path) -> set[str]:
    """Local references in the snapshot, including pending outgoing jobs."""
    refs = set()
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"users", "tickets", "messages"} <= tables:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            user_columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            if "file_id" in columns and "channel" in user_columns:
                public = " AND m.deleted_at IS NULL" if "deleted_at" in columns else ""
                refs.update(r[0] for r in conn.execute(
                    "SELECT m.file_id FROM messages m JOIN tickets t ON t.id=m.ticket_id "
                    "JOIN users u ON u.id=t.user_id WHERE u.channel IN ('widget','email') "
                    "AND m.file_id IS NOT NULL" + public))
        if {"outbound_jobs", "messages"} <= tables:
            refs.update(r[0] for r in conn.execute(
                "SELECT j.file_key FROM outbound_jobs j JOIN messages m ON m.id=j.message_id "
                "WHERE j.file_key IS NOT NULL AND m.deleted_at IS NULL"))
    return {f"attachments/{ref[:2]}/{ref[2:4]}/{ref}"
            for ref in refs if isinstance(ref, str) and _REF.fullmatch(ref)}


def _attachment_state(directory: Path) -> dict[Path, tuple[int, int, int, int]]:
    """Detect ordinary file changes if an operator missed an offline writer.

    This is an extra consistency check, not a filesystem snapshot or a defence
    against a local attacker who can manipulate metadata. Writers must stop.
    """
    state = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise BackupError("Attachment symlinks and junctions are not permitted in a backup")
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BackupError("An attachment is not a regular file")
        state[path] = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
    return state


def create_backup(database: Path, attachments: Path, output: Path, *, offline: bool) -> dict:
    if not offline:
        raise BackupError("Stop all app writers first and pass --offline")
    database, attachments, output = Path(database), Path(attachments), Path(output)
    if database.is_symlink() or not database.is_file():
        raise BackupError("The explicit SQLite database must exist and must not be a symlink")
    if attachments.is_symlink() or attachments.is_junction() or not attachments.is_dir():
        raise BackupError("The explicit attachments directory must exist and must not be a symlink")
    if output.is_symlink():
        raise BackupError("The output must not be a symlink")
    output = output.resolve()
    if output.exists():
        raise BackupError("The output already exists; choose a new archive name")
    if output.is_relative_to(attachments.resolve()):
        raise BackupError("The archive must be outside the attachments directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"format_version": VERSION, "database_engine": "sqlite",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "consistency": "application-stopped", "files": {}}
    # All temporary paths are owned by this invocation and are removed by its
    # TemporaryDirectory context; no supplied source directory is ever deleted.
    with tempfile.TemporaryDirectory(prefix=".spooldesk-backup-", dir=output.parent) as temp:
        staging = Path(temp)
        snapshot = staging / "support.db"
        src = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            before = src.execute("PRAGMA data_version").fetchone()[0]
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
            _check_database(snapshot)
            files = [("support.db", snapshot)]
            attachment_state = _attachment_state(attachments)
            for path in attachment_state:
                files.append(("attachments/" + path.relative_to(attachments).as_posix(), path))
            missing = _required_files(snapshot) - {name for name, _ in files}
            if missing:
                raise BackupError(f"Snapshot references {len(missing)} missing local attachment(s)")
            archive_path = staging / "archive.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True) as archive:
                for name, path in files:
                    size, digest = 0, hashlib.sha256()
                    with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as member:
                        while chunk := source.read(CHUNK):
                            member.write(chunk)
                            size += len(chunk)
                            digest.update(chunk)
                    manifest["files"][name] = {"size": size, "sha256": digest.hexdigest()}
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            if src.execute("PRAGMA data_version").fetchone()[0] != before:
                raise BackupError("Database changed during backup; stop every app writer and retry")
            if _attachment_state(attachments) != attachment_state:
                raise BackupError("Attachments changed during backup; stop every app writer and retry")
        finally:
            src.close()
        verify_archive(archive_path, max_bytes=max(MAX_BYTES, sum(f["size"] for f in manifest["files"].values())))
        # Exclusive creation prevents a concurrent job from replacing a good
        # archive. A .part can be discarded after interruption; final names only
        # appear after all bytes have been flushed and the archive verified.
        partial = output.with_name(output.name + ".part")
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise BackupError("A previous .part exists; inspect it or choose a new archive name") from exc
        try:
            with os.fdopen(fd, "wb") as destination, archive_path.open("rb") as source:
                shutil.copyfileobj(source, destination, CHUNK)
                destination.flush()
                os.fsync(destination.fileno())
            # Hard-link publication cannot overwrite an archive produced by a
            # concurrent process. Both paths are on the same filesystem.
            os.link(partial, output)
        finally:
            partial.unlink(missing_ok=True)
    return manifest


def _safe_name(name: str) -> bool:
    if "\\" in name or ":" in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        return False
    return name in {"manifest.json", "support.db"} or name.startswith("attachments/")


def verify_archive(archive_path: Path, *, max_bytes: int = MAX_BYTES,
                   max_age_hours: float | None = None) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        names = {item.filename for item in members}
        if len(names) != len(members) or not {"manifest.json", "support.db"} <= names:
            raise BackupError("Archive contains duplicate members or lacks its database/manifest")
        if any(not _safe_name(item.filename) or item.is_dir()
               or stat.S_ISLNK(item.external_attr >> 16) for item in members):
            raise BackupError("Archive contains an unsafe path or symlink")
        if sum(item.file_size for item in members) > max_bytes:
            raise BackupError("Archive exceeds the configured uncompressed size limit")
        if archive.getinfo("manifest.json").file_size > 16 * 1024**2:
            raise BackupError("Manifest is too large")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("format_version") != VERSION \
                or manifest.get("database_engine") != "sqlite":
            raise BackupError("Unsupported archive format")
        records = manifest.get("files")
        if not isinstance(records, dict) or set(records) != names - {"manifest.json"}:
            raise BackupError("Manifest does not describe exactly the archived files")
        created = datetime.fromisoformat(manifest["created_at"])
        if created.tzinfo is None:
            raise BackupError("Archive timestamp has no timezone")
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if age_hours < -1 or (max_age_hours is not None and age_hours > max_age_hours):
            raise BackupError("Archive timestamp is in the future or older than the permitted age")
        for name, expected in records.items():
            with archive.open(name) as member:
                size, checksum = _digest(member)
            if not isinstance(expected, dict) or expected.get("size") != size \
                    or expected.get("sha256") != checksum:
                raise BackupError("Archive checksum/size verification failed")
        with tempfile.TemporaryDirectory(prefix="spooldesk-verify-") as temp:
            snapshot = Path(temp) / "support.db"
            with archive.open("support.db") as source, snapshot.open("wb") as destination:
                shutil.copyfileobj(source, destination, CHUNK)
            _check_database(snapshot)
            if _required_files(snapshot) - names:
                raise BackupError("Archive lacks an attachment referenced by the database")
    return manifest


def _quarantine_restored_work(database: Path) -> int:
    """A snapshot cannot know which pending sends succeeded after its cutoff."""
    with closing(sqlite3.connect(database)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        affected = 0
        if "outbound_jobs" in tables:
            pending = "status IN ('pending','retry','sending')"
            affected = conn.execute(f"SELECT COUNT(*) FROM outbound_jobs WHERE {pending}").fetchone()[0]
            conn.execute("UPDATE messages SET delivery_status='uncertain', delivery_error='e_delivery_uncertain' "
                         f"WHERE id IN (SELECT message_id FROM outbound_jobs WHERE {pending})")
            conn.execute("UPDATE outbound_jobs SET status='uncertain', "
                         "last_error='restored_backup_delivery_unknown', next_attempt_at=NULL "
                         f"WHERE {pending}")
        if "worker_leases" in tables:
            conn.execute("DELETE FROM worker_leases")
        conn.commit()
        # Publish a self-contained file even after recovery-status updates.
        # Application startup enables WAL again on this restored database.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    return affected


def restore_archive(archive_path: Path, target: Path, *, offline: bool,
                    max_bytes: int = MAX_BYTES) -> dict:
    if not offline:
        raise BackupError("Stop all app writers first and pass --offline")
    target = Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise BackupError("Restore target must be a new directory; existing data is never overwritten")
    manifest = verify_archive(archive_path, max_bytes=max_bytes)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".spooldesk-restore-", dir=target.parent) as temp:
        staged = Path(temp) / "data"
        staged.mkdir(mode=0o700)
        with zipfile.ZipFile(archive_path) as archive:
            for name in manifest["files"]:
                path = staged.joinpath(*PurePosixPath(name).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with archive.open(name) as source, os.fdopen(fd, "wb") as destination:
                    shutil.copyfileobj(source, destination, CHUNK)
                with path.open("rb") as restored:
                    size, digest = _digest(restored)
                if {"size": size, "sha256": digest} != manifest["files"][name]:
                    raise BackupError("Archive changed after verification")
        (staged / "attachments").mkdir(exist_ok=True)
        quarantined = _quarantine_restored_work(staged / "support.db")
        _check_database(staged / "support.db")
        if target.exists() or target.is_symlink():
            raise BackupError("Restore target appeared during verification; refusing to overwrite")
        staged.rename(target)
    return dict(manifest, quarantined_jobs=quarantined)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="snapshot an explicitly stopped SQLite installation")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--attachments", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--offline", action="store_true")
    verify = commands.add_parser("verify", help="check manifest, hashes, SQLite and local file references")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--max-age-hours", type=float)
    verify.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    restore = commands.add_parser("restore", help="restore into a new directory only")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--offline", action="store_true")
    restore.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            manifest = create_backup(args.database, args.attachments, args.output, offline=args.offline)
        elif args.command == "verify":
            manifest = verify_archive(args.archive, max_bytes=args.max_bytes, max_age_hours=args.max_age_hours)
        else:
            manifest = restore_archive(args.archive, args.target, offline=args.offline, max_bytes=args.max_bytes)
    except (BackupError, OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, KeyError, TypeError) as exc:
        print(f"Backup/restore failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "command": args.command, "created_at": manifest["created_at"],
                      "files": len(manifest["files"]),
                      "quarantined_jobs": manifest.get("quarantined_jobs", 0),
                      "uncompressed_bytes": sum(v["size"] for v in manifest["files"].values())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
