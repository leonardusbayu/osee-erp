"""Offline, additive transfer into an EMPTY PostgreSQL database; never edit source.

Run only during a maintenance window with no writers and an empty private-media
destination. Intermediate fixtures contain sensitive data and stay private.
"""
import argparse
import base64
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backup import _safe_name, create_backup, digest, restore_backup, verify_backup

ROOT = Path(__file__).resolve().parent.parent


def _json(path, value):
    with Path(path).open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, ensure_ascii=False)
    Path(path).chmod(0o600)


def _canonical(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _schema_fingerprint():
    checksum = hashlib.sha256()
    for path in sorted(ROOT.glob("*/migrations/*.py")):
        checksum.update(path.relative_to(ROOT).as_posix().encode() + b"\n" + path.read_bytes())
    return checksum.hexdigest()


def _models():
    from django.apps import apps
    from django.db.migrations.recorder import MigrationRecorder
    models = [model for model in apps.get_models(include_auto_created=True)
              if model._meta.managed and not model._meta.proxy]
    models.append(MigrationRecorder.Migration)
    return sorted(models, key=lambda model: model._meta.db_table)


def _inventory():
    from django.db import connection
    result = {}
    models = _models()
    actual = set(connection.introspection.table_names())
    mapped = {model._meta.db_table for model in models}
    if actual != mapped:
        raise ValueError("Database has unmapped/missing tables; manual migration review is required")
    for model in models:
        with connection.cursor() as cursor:
            columns = {column.name for column in connection.introspection.get_table_description(cursor, model._meta.db_table)}
        if columns != {field.column for field in model._meta.concrete_fields}:
            raise ValueError("Database has unmapped/missing columns; manual migration review is required")
        fields = [field.attname for field in model._meta.concrete_fields]
        checksum, count = hashlib.sha256(), 0
        rows = model._base_manager.order_by(model._meta.pk.attname).values(*fields).iterator(chunk_size=2000)
        for row in rows:
            checksum.update(json.dumps(_canonical(row), ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n")
            count += 1
        result[model._meta.db_table] = {"count": count, "sha256": checksum.hexdigest()}
    return result


def _file_references():
    from django.conf import settings
    from django.db.models import FileField
    root = Path(settings.MEDIA_ROOT).resolve()
    references = []
    for model in _models():
        fields = [field for field in model._meta.concrete_fields if isinstance(field, FileField)]
        if not fields:
            continue
        for row in model._base_manager.all().iterator(chunk_size=1000):
            for field in fields:
                name = getattr(row, field.attname).name
                if not name and field.blank:
                    continue
                _safe_name(name)
                path = (root / name).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError("A database-referenced private document is missing or outside storage")
                checksum, size = digest(path), path.stat().st_size
                if hasattr(row, "sha256") and row.sha256 != checksum:
                    raise ValueError("A private document differs from its recorded hash")
                if hasattr(row, "size") and row.size != size:
                    raise ValueError("A private document differs from its recorded size")
                references.append({"table": model._meta.db_table, "pk": str(row.pk), "field": field.name,
                                   "path": name, "sha256": checksum, "size": size})
    return references


def _worker(mode, workspace):
    import django
    django.setup()
    from django.core.management import call_command
    from django.core import serializers
    from django.core.serializers.json import DjangoJSONEncoder
    from django.apps import apps
    from django.core.management.color import no_style
    from django.db import connection, transaction
    from django.db.migrations.recorder import MigrationRecorder
    from django.utils.dateparse import parse_datetime

    workspace = Path(workspace)
    if mode == "export":
        # This is a PRIVATE copied SQLite DB, never the original path.
        if Path(connection.settings_dict["NAME"]).resolve() != (workspace / "source.sqlite3").resolve():
            raise ValueError("Refusing to migrate any SQLite path except this transfer snapshot")
        call_command("migrate", verbosity=0, interactive=False)
        _json(workspace / "source-file-references.json", _file_references())
        class FullPrecisionEncoder(DjangoJSONEncoder):
            # Django's default JSON encoder truncates datetime microseconds to
            # milliseconds. A migration must preserve the complete stored value.
            def default(self, value):
                if isinstance(value, datetime):
                    return value.isoformat(timespec="microseconds")
                return super().default(value)

        def objects():
            for model in apps.get_models():
                if model._meta.managed and not model._meta.proxy:
                    yield from model._base_manager.order_by(model._meta.pk.attname).iterator(chunk_size=2000)
        with (workspace / "rows.json").open("w", encoding="utf-8") as output:
            serializers.serialize("json", objects(), stream=output, cls=FullPrecisionEncoder)
        (workspace / "rows.json").chmod(0o600)
        extras = {}
        for model in _models():
            if model._meta.auto_created or model is MigrationRecorder.Migration:
                fields = [field.attname for field in model._meta.concrete_fields]
                extras[model._meta.db_table] = [_canonical(row) for row in model._base_manager.order_by(model._meta.pk.attname).values(*fields)]
        _json(workspace / "extra-tables.json", extras)
        _json(workspace / "source-inventory.json", _inventory())
        return

    if connection.vendor != "postgresql" or connection.introspection.table_names():
        raise ValueError("Target PostgreSQL database must contain NO tables before transfer")
    call_command("migrate", verbosity=0, interactive=False)
    expected = json.loads((workspace / "source-inventory.json").read_text(encoding="utf-8"))
    extras = json.loads((workspace / "extra-tables.json").read_text(encoding="utf-8"))
    models = _models()
    if set(connection.introspection.table_names()) != set(expected):
        raise ValueError("Source and target schema tables differ")
    with transaction.atomic():
        # Only reached after verifying an empty target and creating its schema.
        # Remove migration-created permissions/content types, then preserve exact
        # source IDs rather than assuming these system IDs match across engines.
        quoted = ", ".join(connection.ops.quote_name(model._meta.db_table) for model in models)
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE " + quoted + " RESTART IDENTITY CASCADE")
        call_command("loaddata", str(workspace / "rows.json"), verbosity=0)
        for model in models:
            if model._meta.db_table not in extras:
                continue
            model._base_manager.all().delete()
            rows = extras[model._meta.db_table]
            if model is MigrationRecorder.Migration:
                rows = [row | {"applied": parse_datetime(row["applied"])} for row in rows]
            model._base_manager.bulk_create([model(**row) for row in rows], batch_size=1000)
        with connection.cursor() as cursor:
            for statement in connection.ops.sequence_reset_sql(no_style(), models):
                cursor.execute(statement)
        connection.check_constraints()
        actual = _inventory()
        if actual != expected:
            _json(workspace / "mismatch-inventory.json", actual)
            raise ValueError("Canonical row hashes or table counts differ; target transfer rolled back")
        _json(workspace / "target-inventory.json", actual)


def _worker_environment(options, mode, workspace):
    env = os.environ.copy()
    # No .env or provider keys are inherited into this offline migration process.
    for key in list(env):
        if key.startswith(("POSTGRES_", "OPENROUTER_", "DIRECTOR_", "MARKETING_AI_", "OTP_ENCRYPTION_KEY", "DJANGO_")):
            env.pop(key)
    env.update(DJANGO_SETTINGS_MODULE="config.settings", DJANGO_READ_DOT_ENV="0", DJANGO_DEBUG="1",
               DJANGO_SECRET_KEY="private-offline-transfer-not-a-runtime-secret", OSEE_DEMO_MODE="0", OSEE_LOCAL_SETUP="0",
               OSEE_MFA_REQUIRED="0", DJANGO_MEDIA_ROOT=str(workspace / "source-media"))
    if mode == "export":
        env["DJANGO_SQLITE_PATH"] = str(workspace / "source.sqlite3")
    else:
        env.update(POSTGRES_HOST=options.target_host, POSTGRES_PORT=str(options.target_port),
                   POSTGRES_DB=options.target_database, POSTGRES_USER=options.target_user,
                   POSTGRES_PASSWORD_FILE=str(Path(options.target_password_file).resolve()))
    return env


def _run_worker(mode, workspace, options):
    log_path = workspace / (mode + ".log")
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_worker", mode, str(workspace)],
                                env=_worker_environment(options, mode, workspace), stdout=log, stderr=subprocess.STDOUT,
                                cwd=ROOT, timeout=3600)
    log_path.chmod(0o600)
    if result.returncode:
        raise RuntimeError(f"Transfer {mode} failed. Review the private workspace log; target is NOT ready.")


def transfer(options):
    if not options.quiesced:
        raise ValueError("Stop every writer and explicitly confirm --quiesced")
    workspace = Path(options.workspace).resolve()
    source = Path(options.source_sqlite).resolve()
    source_media = Path(options.source_media).resolve()
    target_media = Path(options.target_media).resolve()
    if workspace.exists() or not source.is_file() or not source_media.is_dir():
        raise ValueError("Workspace must be new; source DB and media must exist")
    if (workspace.is_relative_to(source_media) or target_media.is_relative_to(source_media)
            or source.is_relative_to(workspace) or workspace.is_relative_to(target_media)
            or target_media.is_relative_to(workspace)):
        raise ValueError("Source, workspace and destination must be separate")
    if target_media.exists() and (not target_media.is_dir() or any(target_media.iterdir())):
        raise ValueError("Target private storage must be empty")
    if not Path(options.target_password_file).is_file():
        raise ValueError("An explicit target password file is required")
    # Read-only target preflight happens before any target media is written.
    import psycopg
    with psycopg.connect(host=options.target_host, port=options.target_port, dbname=options.target_database,
                        user=options.target_user, password=Path(options.target_password_file).read_text().strip(), connect_timeout=10) as database:
        count = database.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f');").fetchone()[0]
        if count:
            raise ValueError("Target PostgreSQL database is not empty")
    workspace.mkdir(parents=True, mode=0o700)
    source_hash = digest(source)
    wal_path = Path(str(source) + "-wal")
    source_wal_hash = digest(wal_path) if wal_path.is_file() else None
    schema_hash = _schema_fingerprint()
    backup = workspace / "original-snapshot.zip"
    create_backup(output=backup, media_root=source_media, sqlite_path=source, quiesced=True)
    restore_backup(archive_path=backup, sqlite_path=workspace / "source.sqlite3", media_root=workspace / "source-media")
    _run_worker("export", workspace, options)
    if _schema_fingerprint() != schema_hash:
        raise ValueError("Migration source files changed; freeze the release and repeat")
    _run_worker("load", workspace, options)
    manifest = verify_backup(backup)
    target_media.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = []
    for record in manifest["files"]:
        if not record["path"].startswith("media/"):
            continue
        relative = Path(record["path"]).relative_to("media")
        original = workspace / "source-media" / relative
        destination = target_media / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with original.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst)
        destination.chmod(0o600)
        if digest(destination) != record["sha256"]:
            raise ValueError("Destination document hash differs; target is NOT ready")
        files.append({"path": str(relative.as_posix()), "size": record["size"], "sha256": record["sha256"]})
    final_wal_hash = digest(wal_path) if wal_path.is_file() else None
    if digest(source) != source_hash or final_wal_hash != source_wal_hash:
        raise ValueError("Source changed during transfer; target is NOT approved for cutover")
    if _schema_fingerprint() != schema_hash:
        raise ValueError("Migration source files changed; target is NOT approved for cutover")
    inventory = json.loads((workspace / "target-inventory.json").read_text())
    result = {"verified": True, "source_unchanged": True, "source_file_sha256": source_hash,
              "source_wal_sha256": source_wal_hash,
              "snapshot_sha256": digest(backup), "rows_fixture_sha256": digest(workspace / "rows.json"),
              "migration_source_sha256": schema_hash,
              "tables": inventory, "documents": files, "sequence_reset": True,
              "file_references": json.loads((workspace / "source-file-references.json").read_text()),
              "secrets_transferred": False, "public_cutover": False,
              "note": "Preserve original Django/OTP encryption keys in the target vault before login. This transfer is not business or cloud acceptance."}
    _json(workspace / "verification.json", result)
    return {"verified": True, "tables": len(inventory), "documents": len(files), "source_unchanged": True,
            "manifest": str(workspace / "verification.json"), "public_cutover": False}


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "_worker":
        _worker(sys.argv[2], sys.argv[3])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-sqlite", "source-media", "target-media", "workspace", "target-host", "target-database", "target-user", "target-password-file"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--target-port", type=int, default=5432)
    parser.add_argument("--quiesced", action="store_true")
    options = parser.parse_args()
    print(json.dumps(transfer(options)))


if __name__ == "__main__":
    main()
