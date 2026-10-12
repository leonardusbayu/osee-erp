"""Synthetic statement journeys keep the simpler column picker financially safe."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from finance.models import BankTransaction, Journal
from .models import StatementImport
from . import test_statements as fixtures


@override_settings(MFA_REQUIRED=False)
class StatementMappingUXTests(TestCase):
    setUp = fixtures.StatementWorkflowTests.setUp
    upload = fixtures.StatementWorkflowTests.upload
    preview = fixtures.StatementWorkflowTests.preview
    confirm = fixtures.StatementWorkflowTests.confirm

    def stage(self, content):
        response = self.client.post(
            reverse("bank_import"),
            {
                "account": self.account.pk,
                "file": SimpleUploadedFile("synthetic.csv", content.encode()),
            },
        )
        self.assertEqual(response.status_code, 302)
        return StatementImport.objects.latest("pk")

    def test_populated_picker_groups_essential_choices_and_imports_only_after_confirmation(
        self,
    ):
        statement = self.upload()
        response = self.client.get(reverse("statement_preview", args=[statement.pk]))
        for heading in [
            "Tanggal &amp; keterangan",
            "Uang masuk / keluar",
            "Identitas transaksi",
            "Pengaturan tambahan",
        ]:
            self.assertContains(response, heading)
        self.assertNotContains(response, 'id="id_no_transactions"')
        self.assertContains(response, "Pilih salah satu cara")
        self.assertContains(response, "+ berarti uang masuk")
        self.assertContains(response, "− berarti uang keluar")
        self.assertRegex(
            response.content.decode(), r'<details class="ui-details statement-options">'
        )
        token = self.preview(statement).context["token"]
        self.assertTrue(token)
        self.assertFalse(BankTransaction.objects.exists())
        self.assertEqual(self.confirm(statement, token).status_code, 302)
        self.assertEqual(BankTransaction.objects.count(), 1)
        self.assertFalse(Journal.objects.exists())

    def test_empty_source_requires_only_explicit_no_transactions_attestation(self):
        statement = self.stage("date,reference,description,amount\n")
        url = reverse("statement_preview", args=[statement.pk])
        response = self.client.get(url)
        self.assertContains(response, 'id="id_no_transactions"')
        self.assertNotContains(response, '<select name="date"')
        self.assertNotContains(response, "Pilih salah satu cara")
        self.assertFalse(self.client.post(url, {"action": "preview"}).context["token"])
        response = self.client.post(url, {"action": "preview", "no_transactions": "on"})
        token = response.context["token"]
        self.assertTrue(token)
        self.assertContains(response, "Pratinjau 0 transaksi")
        self.client.post(url, {"action": "confirm", "preview_token": token})
        statement.refresh_from_db()
        self.assertIsNone(statement.confirmed_at)
        self.assertEqual(self.confirm(statement, token).status_code, 302)
        statement.refresh_from_db()
        self.assertIsNotNone(statement.confirmed_at)
        self.assertFalse(BankTransaction.objects.exists())
        self.assertFalse(Journal.objects.exists())

    def test_populated_source_cannot_use_hidden_no_transactions_option(self):
        statement = self.upload()
        response = self.preview(statement, no_transactions="on")
        self.assertFalse(response.context["token"])
        self.assertContains(response, "File berisi baris data")
        self.assertNotContains(response, 'id="id_no_transactions"')
        self.assertFalse(BankTransaction.objects.exists())

    def test_missing_identity_is_visible_without_silently_changing_identity_method(
        self,
    ):
        statement = self.stage(
            f"date,description,amount,balance\n{timezone.localdate()},Synthetic,1000,2000\n"
        )
        url = reverse("statement_preview", args=[statement.pk])
        response = self.client.get(url)
        self.assertEqual(
            response.context["form"]["reference_mode"].value(), "bank_reference"
        )
        self.assertContains(response, "Belum ada kolom referensi unik yang dipilih")
        response = self.preview(
            statement, reference="", description="1", amount="2", balance="3"
        )
        self.assertFalse(response.context["token"])
        self.assertIn("reference", response.context["form"].errors)
        response = self.preview(
            statement,
            reference="",
            description="1",
            amount="2",
            balance="3",
            reference_mode="balance_fingerprint",
        )
        self.assertTrue(response.context["token"])
        self.assertEqual(
            self.confirm(statement, response.context["token"]).status_code, 302
        )
        self.assertTrue(BankTransaction.objects.get().reference.startswith("BNI-STMT-"))

    def test_invalid_range_and_conditional_identity_errors_reveal_the_controls_to_fix(
        self,
    ):
        statement = self.stage(
            f"date,reference,description,amount,balance\n{timezone.localdate()},FIRST,Synthetic,1000,2000\n{timezone.localdate()},SECOND,Synthetic,1000,3000\n"
        )
        response = self.preview(statement, first_row=2, last_row=1)
        self.assertFalse(response.context["token"])
        self.assertContains(response, "Rentang baris")
        self.assertRegex(
            response.content.decode(),
            r'<details class="ui-details statement-options" open>',
        )
        response = self.preview(
            statement,
            reference_mode="balance_fingerprint",
            reference="",
            description="",
            balance="",
        )
        self.assertFalse(response.context["token"])
        self.assertIn("description", response.context["form"].errors)
        self.assertIn("balance", response.context["form"].errors)
        self.assertFalse(BankTransaction.objects.exists())
