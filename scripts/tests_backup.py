from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
import zipfile

from scripts.backup import create_backup, restore_backup, verify_backup


class PortableBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="osee-backup-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "source.sqlite3"
        with closing(sqlite3.connect(self.database)) as database:
            database.execute("CREATE TABLE receipt (id INTEGER PRIMARY KEY, amount TEXT NOT NULL)")
            database.execute("INSERT INTO receipt VALUES (1, '444000.25')")
            database.commit()
        self.media = self.root / "private"
        self.media.mkdir()
        (self.media / "evidence.pdf").write_bytes(b"synthetic private archived bytes")
        self.archive = self.root / "snapshot.zip"

    def create(self):
        return create_backup(output=self.archive, media_root=self.media, sqlite_path=self.database, quiesced=True)

    def test_restore_keeps_database_values_and_all_document_bytes(self):
        result = self.create()
        self.assertEqual(result["files"], 2)
        restored_db = self.root / "restore" / "database.sqlite3"
        restored_media = self.root / "restore" / "media"
        result = restore_backup(archive_path=self.archive, sqlite_path=restored_db, media_root=restored_media)
        self.assertTrue(result["restored"])
        with closing(sqlite3.connect(restored_db)) as database:
            self.assertEqual(database.execute("SELECT amount FROM receipt").fetchone(), ("444000.25",))
        self.assertEqual((restored_media / "evidence.pdf").read_bytes(), (self.media / "evidence.pdf").read_bytes())

    def test_create_requires_quiescence_and_never_overwrites_existing_archive(self):
        with self.assertRaises(ValueError):
            create_backup(output=self.archive, media_root=self.media, sqlite_path=self.database)
        self.create()
        before = self.archive.read_bytes()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.archive.read_bytes(), before)

    def test_restore_rejects_existing_database_and_nonempty_media(self):
        self.create()
        before = self.database.read_bytes()
        with self.assertRaises(ValueError):
            restore_backup(archive_path=self.archive, sqlite_path=self.database, media_root=self.root / "new-media")
        with self.assertRaises(ValueError):
            restore_backup(archive_path=self.archive, sqlite_path=self.root / "new.sqlite3", media_root=self.media)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((self.root / "new.sqlite3").exists())

    def test_corruption_and_traversal_fail_before_any_restore(self):
        self.create()
        with zipfile.ZipFile(self.archive) as original:
            content = {name: original.read(name) for name in original.namelist()}
        for label, change in [("changed", {"media/evidence.pdf": b"tampered"}),
                              ("traversal", {"../escape": b"escape"}),
                              ("absolute", {"C:/escape": b"escape"})]:
            modified = self.root / f"{label}.zip"
            with zipfile.ZipFile(modified, "w") as archive:
                for name, data in (content | change).items():
                    archive.writestr(name, data)
            with self.subTest(label=label), self.assertRaises(ValueError):
                restore_backup(archive_path=modified, sqlite_path=self.root / "new.sqlite3", media_root=self.root / "new-media")
            self.assertFalse((self.root / "new.sqlite3").exists())

    def test_manifest_engine_and_members_are_validated(self):
        self.create()
        self.assertEqual(verify_backup(self.archive)["engine"], "sqlite")
        with self.assertRaises(ValueError):
            restore_backup(archive_path=self.archive, postgres_database="wrong-engine", media_root=self.root / "new-media")


if __name__ == "__main__":
    unittest.main()
