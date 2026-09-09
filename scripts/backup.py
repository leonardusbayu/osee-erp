"""Portable, hash-verified database + private-document backup and empty-target restore.

No implicit production target, overwrite, deletion, shell interpolation, or secret
export. Operators must stop every writer before creating a complete snapshot.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
import zipfile

MAX_ARCHIVE_BYTES = 20 * 1024**3
MAX_FILES = 100000


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _pg_environment():
    env = os.environ.copy()
    password_file = env.get("POSTGRES_PASSWORD_FILE")
    password = env.get("POSTGRES_PASSWORD", "")
    if password_file:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    env.update(PGHOST=env.get("POSTGRES_HOST", "localhost"), PGPORT=env.get("POSTGRES_PORT", "5432"),
               PGUSER=env.get("POSTGRES_USER", "osee"), PGPASSWORD=password, PGCONNECT_TIMEOUT="10")
    return env


def _pg_command(tool, *arguments):
    executable = str(Path(os.environ["POSTGRES_BIN"]) / tool) if os.environ.get("POSTGRES_BIN") else tool
    result = subprocess.run([executable, *arguments], env=_pg_environment(), capture_output=True, timeout=1800)
    if result.returncode:
        # Client output may contain private SQL data or addresses; do not print it.
        raise RuntimeError(f"{tool} failed; restore/backup is incomplete.")
    return result.stdout


def _safe_name(name):
    if not isinstance(name, str) or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Unsafe archive path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or name != path.as_posix():
        raise ValueError("Unsafe archive path")
    return name


def verify_backup(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(infos) > MAX_FILES or len(set(names)) != len(names) or sum(item.file_size for item in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive size or duplicate-entry limit")
        for info in infos:
            _safe_name(info.filename)
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Archive links/directories are not allowed")
        if "manifest.json" not in names or archive.getinfo("manifest.json").file_size > 20 * 1024**2:
            raise ValueError("Missing or oversized manifest")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format_version") != 1 or manifest.get("engine") not in {"sqlite", "postgresql"} or manifest.get("writers_quiesced") is not True:
            raise ValueError("Unsupported or inconsistent backup")
        records = manifest["files"]
        expected = {record["path"] for record in records}
        if len(expected) != len(records) or expected != set(names) - {"manifest.json"}:
            raise ValueError("Manifest and archive differ")
        database_name = "database.sqlite3" if manifest["engine"] == "sqlite" else "database.dump"
        if manifest.get("database_file") != database_name or database_name not in expected:
            raise ValueError("Database snapshot is missing")
        for record in records:
            name = _safe_name(record["path"])
            if name != database_name and not name.startswith("media/"):
                raise ValueError("Unexpected backup member")
            with archive.open(name) as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            if checksum != record["sha256"] or archive.getinfo(name).file_size != record["size"]:
                raise ValueError("Backup integrity check failed")
    return manifest


def create_backup(*, output, media_root, sqlite_path=None, postgres_database=None, quiesced=False):
    if not quiesced:
        raise ValueError("Stop all application/background writers and explicitly confirm --quiesced.")
    if (sqlite_path is None) == (postgres_database is None):
        raise ValueError("Choose one explicit database source")
    output, media_root = Path(output).resolve(), Path(media_root).resolve()
    if output.exists() or not media_root.is_dir() or output.is_relative_to(media_root):
        raise ValueError("Output must be new and outside existing private storage")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="osee-backup-", dir=output.parent) as temporary:
        directory = Path(temporary)
        engine = "sqlite" if sqlite_path is not None else "postgresql"
        database_name = "database.sqlite3" if engine == "sqlite" else "database.dump"
        database_file = directory / database_name
        if sqlite_path is not None:
            source = Path(sqlite_path).resolve()
            if not source.is_file() or source.is_relative_to(media_root):
                raise ValueError("Source database must exist outside private media")
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as source_db, closing(sqlite3.connect(database_file)) as target:
                source_db.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone() != ("ok",) or target.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Source database failed integrity checks")
        else:
            _pg_command("pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(database_file), "--dbname", postgres_database)
        manifest = {"format_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                    "engine": engine, "database_file": database_name, "writers_quiesced": True, "files": []}
        archive_path = directory / "complete.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            paths = [(database_file, database_name)]
            for path in sorted(media_root.rglob("*")):
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise ValueError("Private storage cannot contain links")
                if path.is_file():
                    paths.append((path, "media/" + path.relative_to(media_root).as_posix()))
            for path, name in paths:
                before = path.stat()
                checksum = digest(path)
                archive.write(path, _safe_name(name))
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Source changed during backup; stop every writer")
                manifest["files"].append({"path": name, "size": before.st_size, "sha256": checksum})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
        verify_backup(archive_path)
        # Exclusive output creation; never replace a prior backup on a race.
        with output.open("xb") as target, archive_path.open("rb") as source:
            shutil.copyfileobj(source, target)
        output.chmod(0o600)
    return {"path": str(output), "sha256": digest(output), "files": len(manifest["files"]), "engine": engine}


def restore_backup(*, archive_path, media_root, sqlite_path=None, postgres_database=None):
    manifest = verify_backup(archive_path)
    if (sqlite_path is None) == (postgres_database is None):
        raise ValueError("Choose one explicit empty database destination")
    engine = "sqlite" if sqlite_path is not None else "postgresql"
    if manifest["engine"] != engine:
        raise ValueError("Backup engine differs from target; this is not a database conversion")
    media_root = Path(media_root).resolve()
    if media_root.exists() and (not media_root.is_dir() or any(media_root.iterdir())):
        raise ValueError("Restore requires empty private storage")
    if sqlite_path is not None and Path(sqlite_path).exists():
        raise ValueError("Restore will never overwrite a database")
    if postgres_database is not None:
        tables = _pg_command("psql", "--no-psqlrc", "--tuples-only", "--no-align", "--dbname", postgres_database,
                             "--command", "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f');")
        if tables.strip() != b"0":
            raise ValueError("Restore requires an empty PostgreSQL database")
    media_root.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="osee-restore-", dir=media_root.parent) as temporary:
        directory = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            for record in manifest["files"]:
                target = directory / record["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(record["path"]) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600)
        database_file = directory / manifest["database_file"]
        if sqlite_path is not None:
            with closing(sqlite3.connect(database_file)) as database:
                if database.execute("PRAGMA integrity_check").fetchone() != ("ok",) or database.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Restored database integrity failed")
            target_database = Path(sqlite_path).resolve()
            target_database.parent.mkdir(parents=True, exist_ok=True)
            with target_database.open("xb") as output, database_file.open("rb") as source:
                shutil.copyfileobj(source, output)
            target_database.chmod(0o600)
        else:
            _pg_command("pg_restore", "--single-transaction", "--exit-on-error", "--no-owner", "--no-acl", "--dbname", postgres_database, str(database_file))
        media_root.mkdir(parents=True, exist_ok=True)
        for record in manifest["files"]:
            if not record["path"].startswith("media/"):
                continue
            relative = PurePosixPath(record["path"]).relative_to("media")
            destination = media_root / str(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output, (directory / record["path"]).open("rb") as source:
                shutil.copyfileobj(source, output)
            destination.chmod(0o600)
            if digest(destination) != record["sha256"]:
                raise ValueError("Restored evidence hash differs")
    return {"engine": engine, "files_verified": len(manifest["files"]), "restored": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "verify", "restore"])
    parser.add_argument("archive", type=Path)
    parser.add_argument("--media", type=Path)
    parser.add_argument("--sqlite", type=Path)
    parser.add_argument("--postgres-database")
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args()
    if args.action == "verify":
        result = verify_backup(args.archive)
        print(json.dumps({"verified": True, "engine": result["engine"], "files": len(result["files"])}))
        return
    if args.media is None:
        parser.error("--media is required")
    kwargs = {"media_root": args.media, "sqlite_path": args.sqlite, "postgres_database": args.postgres_database}
    if args.action == "create":
        result = create_backup(output=args.archive, quiesced=args.quiesced, **kwargs)
    else:
        result = restore_backup(archive_path=args.archive, **kwargs)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
