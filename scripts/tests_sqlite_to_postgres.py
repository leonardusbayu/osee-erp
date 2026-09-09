"""Opt-in complete-schema transfer rehearsal using synthetic SQLite data only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
import uuid

import psycopg
from psycopg import sql
from scripts.sqlite_to_postgres import transfer
from scripts.backup import digest


FIXTURE = r'''
import django
django.setup()
from django.core.management import call_command
call_command('migrate', verbosity=0, interactive=False)
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from django.contrib.auth.models import User, Group, Permission
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.core.files.uploadedfile import SimpleUploadedFile
from core.models import Organization, Membership, AccountSecurity, AuditEvent
from finance.models import Party, Product, Invoice
from evidence.services import attach_document
from taxes.models import TaxProfile
from pypdf import PdfWriter
org = Organization.objects.create(id=15, name='Perusahaan sintetis — Uji migrasi')
user = User.objects.create_user(id=40, username='synthetic-migration-owner', password='Synthetic-passphrase-123!')
Membership.objects.create(id=61, user=user, organization=org, role='owner')
revoked = User.objects.create_user(id=51, username='revoked-synthetic')
Membership.all_objects.create(id=72, user=revoked, organization=org, role='finance', is_active=False)
group = Group.objects.create(id=9, name='Synthetic group')
user.groups.add(group)
group.permissions.add(Permission.objects.first())
with connection.cursor() as cursor:
    cursor.execute('UPDATE auth_user_groups SET id = 71')
    cursor.execute('UPDATE auth_group_permissions SET id = 89')
AccountSecurity.objects.create(user=user, totp_secret='opaque-synthetic-encrypted-state', totp_enabled=True, totp_last_counter=1234, recovery_codes=['synthetic-hash-1'], session_version=7)
session = SessionStore()
session['_auth_user_id'] = str(user.pk)
session['auth_security_version'] = 7
session['mfa_version'] = 7
session.set_expiry(datetime(2027, 5, 4, 16, 30, tzinfo=timezone(timedelta(hours=7))))
session.save()
AuditEvent.objects.create(organization=org, actor=user, action='synthetic.transfer.fixture', detail={'amount':'444000.25','unknown':None,'unicode':'Bahasa Indonesia — sumber','items':[1,2,3]})
party=Party.objects.create(organization=org, name='Mitra sintetis', kind='customer')
product=Product.objects.create(organization=org, code='TEST', name='Produk sintetis', kind='itp', default_price=Decimal('530000.25'))
invoice=Invoice.objects.create(organization=org,party=party,product=product,number='SYNTHETIC-1',quantity=2,unit_price=Decimal('530000.25'),date='2026-05-04',due_date='2026-05-04',service_date='2026-05-04')
TaxProfile.objects.create(organization=org)
writer=PdfWriter(); writer.add_blank_page(width=100,height=100); stream=BytesIO();writer.write(stream)
attach_document(organization=org,actor=user,invoice=invoice,upload=SimpleUploadedFile('synthetic.pdf',stream.getvalue()))
print('Synthetic fixture ready')
'''


@unittest.skipUnless(os.environ.get("OSEE_TEST_POSTGRES_TRANSFER") == "1", "Explicit synthetic transfer opt-in required")
class SQLitePostgreSQLTransferTests(unittest.TestCase):
    def test_all_tables_sessions_mfa_m2m_ids_sequences_and_document_hashes_survive(self):
        params = {"host": os.environ.get("POSTGRES_HOST", "localhost"), "port": os.environ.get("POSTGRES_PORT", "5432"),
                  "user": os.environ.get("POSTGRES_USER", "osee"), "password": os.environ.get("POSTGRES_PASSWORD", "")}
        database_name = "osee_transfer_test_" + uuid.uuid4().hex
        with psycopg.connect(**params, dbname="postgres", autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
            try:
                with TemporaryDirectory(prefix="osee-transfer-test-") as temporary:
                    root = Path(temporary)
                    media = root / "original-media"
                    media.mkdir()
                    source = root / "original.sqlite3"
                    password_file = root / "target-password"
                    password_file.write_text(params['password'])
                    password_file.chmod(0o600)
                    env = os.environ.copy()
                    for key in list(env):
                        if key.startswith(("POSTGRES_", "DJANGO_", "OTP_ENCRYPTION_KEY")):
                            env.pop(key)
                    env.update(DJANGO_SETTINGS_MODULE='config.settings', DJANGO_READ_DOT_ENV='0', DJANGO_DEBUG='1',
                               DJANGO_SECRET_KEY='synthetic-session-key-not-a-company-key', DJANGO_SQLITE_PATH=str(source),
                               DJANGO_MEDIA_ROOT=str(media), OSEE_DEMO_MODE='0', OSEE_LOCAL_SETUP='0')
                    fixture = subprocess.run([sys.executable, '-c', FIXTURE], env=env, capture_output=True, text=True)
                    self.assertEqual(fixture.returncode, 0, fixture.stderr)
                    original_hash = digest(source)
                    options = argparse.Namespace(source_sqlite=source, source_media=media, target_media=root / 'target-media',
                                                 workspace=root / 'private-transfer', target_host=params['host'], target_port=int(params['port']),
                                                 target_database=database_name, target_user=params['user'], target_password_file=password_file, quiesced=True)
                    private_pdf = next(media.rglob('*.pdf'))
                    original_pdf_bytes = private_pdf.read_bytes()
                    private_pdf.unlink()
                    with self.assertRaises(RuntimeError):
                        transfer(argparse.Namespace(**(vars(options) | {'workspace': root / 'missing-source-attempt'})))
                    with psycopg.connect(**params, dbname=database_name) as untouched_target:
                        self.assertEqual(untouched_target.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0], 0)
                    private_pdf.write_bytes(original_pdf_bytes)
                    try:
                        result = transfer(options)
                    except Exception:
                        logs = '\n'.join(path.read_text() for path in (root / 'private-transfer').glob('*.log'))
                        mismatch = root / 'private-transfer/mismatch-inventory.json'
                        if mismatch.exists():
                            actual = json.loads(mismatch.read_text())
                            expected = json.loads((root / 'private-transfer/source-inventory.json').read_text())
                            logs += '\nDiffering tables: ' + str({name: {'source': value, 'target': actual.get(name)} for name, value in expected.items() if actual.get(name) != value})
                        self.fail('Synthetic transfer failed: ' + logs[-9000:])
                    self.assertTrue(result['verified'])
                    self.assertEqual(digest(source), original_hash)
                    manifest = json.loads((root / 'private-transfer/verification.json').read_text())
                    if os.environ.get('OSEE_TRANSFER_EVIDENCE_DIR'):
                        evidence = Path(os.environ['OSEE_TRANSFER_EVIDENCE_DIR'])
                        evidence.mkdir(parents=True, exist_ok=True)
                        for name in ['verification.json', 'source-inventory.json', 'target-inventory.json']:
                            shutil.copyfile(root / 'private-transfer' / name, evidence / name)
                    self.assertGreaterEqual(len(manifest['tables']), 58)
                    self.assertEqual(manifest['tables']['django_session']['count'], 1)
                    self.assertEqual(manifest['tables']['core_accountsecurity']['count'], 1)
                    self.assertEqual(len(manifest['documents']), 1)
                    with psycopg.connect(**params, dbname=database_name) as target:
                        self.assertEqual(target.execute('SELECT id FROM auth_user_groups').fetchone()[0], 71)
                        self.assertEqual(target.execute('SELECT id FROM auth_group_permissions').fetchone()[0], 89)
                        self.assertEqual(target.execute('SELECT is_active FROM core_membership WHERE id=72').fetchone()[0], False)
                        sequence_id = target.execute("SELECT nextval(pg_get_serial_sequence('auth_user','id'))").fetchone()[0]
                        self.assertGreater(sequence_id, 51)
                    with self.assertRaises(ValueError):
                        transfer(argparse.Namespace(**(vars(options) | {'workspace': root / 'another-transfer','target_media': root / 'another-media'})))
            finally:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


if __name__ == '__main__':
    unittest.main()
