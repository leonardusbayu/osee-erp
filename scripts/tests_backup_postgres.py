"""Opt-in destructive tests affect only freshly generated synthetic database names."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid

import psycopg
from psycopg import sql
from scripts.backup import create_backup, restore_backup


@unittest.skipUnless(os.environ.get("OSEE_TEST_POSTGRES_BACKUP") == "1", "Explicit synthetic PostgreSQL backup opt-in required")
class PostgreSQLBackupTests(unittest.TestCase):
    def test_portable_restore_and_refuse_nonempty_database(self):
        params = {"host": os.environ.get("POSTGRES_HOST", "localhost"), "port": os.environ.get("POSTGRES_PORT", "5432"),
                  "user": os.environ.get("POSTGRES_USER", "osee"), "password": os.environ.get("POSTGRES_PASSWORD", "")}
        source = "osee_backup_test_" + uuid.uuid4().hex
        target = "osee_restore_test_" + uuid.uuid4().hex
        with psycopg.connect(**params, dbname="postgres", autocommit=True) as admin:
            try:
                for name in (source, target):
                    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                with psycopg.connect(**params, dbname=source) as database:
                    database.execute("CREATE TABLE evidence_test (id integer PRIMARY KEY, amount numeric(18,2))")
                    database.execute("INSERT INTO evidence_test VALUES (1, 444000.25)")
                with TemporaryDirectory(prefix="osee-pg-backup-test-") as temporary:
                    root = Path(temporary)
                    media = root / "source"
                    media.mkdir()
                    (media / "source.pdf").write_bytes(b"synthetic private bytes")
                    archive = root / "backup.zip"
                    create_backup(output=archive, media_root=media, postgres_database=source, quiesced=True)
                    result = restore_backup(archive_path=archive, media_root=root / "restored", postgres_database=target)
                    self.assertTrue(result["restored"])
                    self.assertEqual((root / "restored/source.pdf").read_bytes(), (media / "source.pdf").read_bytes())
                    with psycopg.connect(**params, dbname=target) as database:
                        self.assertEqual(str(database.execute("SELECT amount FROM evidence_test").fetchone()[0]), "444000.25")
                    with self.assertRaises(ValueError):
                        restore_backup(archive_path=archive, media_root=root / "another", postgres_database=target)
            finally:
                for name in (source, target):
                    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


if __name__ == "__main__":
    unittest.main()
