import io
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, SimpleTestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from core.models import Organization, Membership
from finance.models import BankAccount, BankTransaction, Journal
from .models import StatementImport
from .statement_import import parse_money, read_statement, normalize_statement, parse_reference


class StatementParsingTests(SimpleTestCase):
    def test_equivalent_excel_csv_identity_and_unsafe_numeric_references(self):
        mapping = {"date": "0", "description": "1", "amount": "2", "balance": "3", "reference_mode": "balance_fingerprint", "number_format": "en"}
        references = []
        for timestamp in ["10/01/2026", "2026-01-10T00:00:00", "2026-01-10", "2026-01-09T17:00:00+00:00"]:
            rows, errors = normalize_statement([[timestamp, "Transfer", "1000", "2000"]], mapping)
            self.assertFalse(errors)
            references.append(rows[0]["reference"])
        self.assertEqual(len(set(references)), 1)
        rows, errors = normalize_statement([["2026-01-10T01:00:00", "Transfer", "1000", "2000"]], mapping)
        self.assertNotEqual(rows[0]["reference"], references[0])
        self.assertEqual(parse_reference({"number": "12345.0"}), parse_reference("12345"))
        for value in ["1234567890123456", "1.5", "NaN"]:
            with self.assertRaises(ValidationError):
                parse_reference({"number": value})

    def test_locale_is_explicit_and_native_excel_numbers_are_preserved(self):
        self.assertEqual(parse_money("Rp 1.234.567,89", "id"), Decimal("1234567.89"))
        self.assertEqual(parse_money("1,234,567.89", "en"), Decimal("1234567.89"))
        self.assertEqual(parse_money({"number": "450000.50"}, "id"), Decimal("450000.50"))
        for value in ["450000.50", "1.00", "NaN", "Infinity", "100,123"]:
            with self.assertRaises(ValidationError, msg=value):
                parse_money(value, "id")

    def test_real_excel_cells_and_formula_rejection(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Tanggal", "Referensi", "Debit", "Kredit"])
        sheet.append([timezone.localdate(), "R1", 1234.5, None])
        output = io.BytesIO()
        workbook.save(output)
        headers, rows = read_statement(output.getvalue(), "bank.xlsx")
        normalized, errors = normalize_statement(rows, {"date": "0", "reference": "1", "debit": "2", "credit": "3", "number_format": "id"})
        self.assertFalse(errors)
        self.assertEqual(normalized[0]["amount"], "-1234.50")
        sheet["C2"] = "=1+1"
        output = io.BytesIO()
        workbook.save(output)
        with self.assertRaisesMessage(ValidationError, "formula"):
            read_statement(output.getvalue(), "bank.xlsx")

    def test_duplicate_future_date_and_two_directions_are_rejected(self):
        today = str(timezone.localdate())
        rows = [[today, "R", "100", "0"], [today, "R", "100", "0"], [str(timezone.localdate() + timedelta(days=1)), "F", "0", "100"], [today, "B", "1", "2"]]
        parsed, errors = normalize_statement(rows, {"date": "0", "reference": "1", "debit": "2", "credit": "3", "number_format": "en"})
        self.assertEqual(len(parsed), 1)
        self.assertEqual([row["row"] for row in errors], [2, 3, 4])


@override_settings(MFA_REQUIRED=False)
class StatementWorkflowTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.org = Organization.objects.create(name="Statement synthetic company")
        self.user = User.objects.create_user("statement-owner", password="Synthetic-only-password-327")
        self.membership = Membership.objects.create(organization=self.org, user=self.user, role="owner")
        self.account = BankAccount.objects.create(organization=self.org, name="BNI test", account_number="SYNTHETIC")
        self.client.force_login(self.user)

    def upload(self, reference="REF1", amount="1000.25"):
        content = f"date,reference,description,amount\n{timezone.localdate()},{reference},Synthetic,{amount}\n".encode()
        response = self.client.post(reverse("bank_import"), {"account": self.account.pk, "file": SimpleUploadedFile("bank.csv", content)})
        self.assertEqual(response.status_code, 302)
        return StatementImport.objects.get(sha256=__import__("hashlib").sha256(content).hexdigest())

    def preview(self, statement, **changes):
        data = {"date": "0", "reference": "1", "description": "2", "amount": "3", "number_format": "en", "reference_mode": "bank_reference", "first_row": 1, "last_row": 1, **changes}
        return self.client.post(reverse("statement_preview", args=[statement.pk]), data)

    def confirm(self, statement, token):
        return self.client.post(reverse("statement_preview", args=[statement.pk]), {"action": "confirm", "preview_token": token, "confirmed": "yes"})

    def test_upload_preview_confirm_repeat_and_overlap_are_idempotent(self):
        statement = self.upload()
        self.assertEqual(self.upload().pk, statement.pk)
        self.assertFalse(BankTransaction.objects.exists())
        response = self.preview(statement)
        self.assertContains(response, "Pratinjau 1 transaksi")
        self.assertContains(response, "Rp1.000,25")
        token = response.context["token"]
        self.assertTrue(token)
        self.assertFalse(BankTransaction.objects.exists())
        self.assertEqual(self.confirm(statement, token).status_code, 302)
        self.confirm(statement, token)
        self.assertEqual(BankTransaction.objects.count(), 1)
        self.assertEqual(BankTransaction.objects.get().amount, Decimal("1000.25"))
        self.assertFalse(Journal.objects.exists(), "Bank import alone must not recognize revenue")

    def test_conflicting_reference_rolls_back_whole_confirmation(self):
        first = self.upload()
        self.confirm(first, self.preview(first).context["token"])
        second = self.upload(amount="2000.50")
        response = self.confirm(second, self.preview(second).context["token"])
        self.assertContains(response, "isi berbeda")
        second.refresh_from_db()
        self.assertIsNone(second.confirmed_at)
        self.assertEqual(BankTransaction.objects.count(), 1)

    def test_tampered_token_source_scope_csrf_and_revocation_fail(self):
        statement = self.upload()
        token = self.preview(statement).context["token"]
        self.confirm(statement, token + "bad")
        self.assertFalse(BankTransaction.objects.exists())
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.user)
        self.assertEqual(secure.post(reverse("statement_preview", args=[statement.pk]), {"action": "confirm", "preview_token": token, "confirmed": "yes"}).status_code, 403)
        stranger = User.objects.create_user("statement-stranger")
        other = Organization.objects.create(name="Other")
        Membership.objects.create(organization=other, user=stranger, role="owner")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse("statement_source", args=[statement.pk])).status_code, 404)
        self.assertEqual(self.confirm(statement, token).status_code, 404)
        self.client.force_login(self.user)
        Membership.all_objects.filter(pk=self.membership.pk).update(is_active=False)
        self.assertEqual(self.confirm(statement, token).status_code, 403)

    def test_invalid_mapping_has_no_confirmation_and_source_is_immutable(self):
        statement = self.upload()
        response = self.preview(statement, debit="3")
        self.assertFalse(response.context["token"])
        self.assertContains(response, "satu informasi")
        statement.rows = []
        with self.assertRaises(ValidationError):
            statement.save()
        self.assertFalse(BankTransaction.objects.exists())

    def test_source_changed_after_preview_cannot_be_imported(self):
        statement = self.upload()
        token = self.preview(statement).context["token"]
        with open(statement.file.path, "wb") as source:
            source.write(b"changed")
        self.assertContains(self.confirm(statement, token), "File sumber berubah")
        self.assertFalse(BankTransaction.objects.exists())

    def test_long_filename_preserves_extension_through_confirmation(self):
        content = f"date,reference,description,amount\n{timezone.localdate()},LONG,Test,1000\n".encode()
        response = self.client.post(reverse("bank_import"), {"account": self.account.pk, "file": SimpleUploadedFile("a" * 245 + ".csv", content)})
        self.assertEqual(response.status_code, 302)
        statement = StatementImport.objects.get()
        self.assertTrue(statement.original_name.endswith(".csv"))
        self.assertLessEqual(len(statement.original_name), 240)
        self.assertEqual(self.confirm(statement, self.preview(statement).context["token"]).status_code, 302)
        self.assertEqual(BankTransaction.objects.count(), 1)
