"""Completeness attestations tested through statement intake and public review commands."""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, Membership, Organization
from finance.models import BankAccount, BankTransaction, Journal
from webapp import test_statements as fixtures
from webapp.models import StatementImport

from . import statement_services as statements
from .statement_models import StatementPeriodReview


@override_settings(MFA_REQUIRED=False)
class StatementCoverageTests(TestCase):
    setUp = fixtures.StatementWorkflowTests.setUp
    upload = fixtures.StatementWorkflowTests.upload
    preview = fixtures.StatementWorkflowTests.preview
    confirm = fixtures.StatementWorkflowTests.confirm

    def imported(self):
        source = self.upload()
        self.confirm(source, self.preview(source).context["token"])
        source.refresh_from_db()
        return source

    def facts(self, source, **changes):
        return {
            "organization": self.org,
            "actor": self.user,
            "statement": source,
            "period_start": timezone.localdate(),
            "period_end": timezone.localdate(),
            "opening_balance": "2000.00",
            "closing_balance": "3000.25",
            "total_in": "1000.25",
            "total_out": "0.00",
            "transaction_count": 1,
            "source_order": "ascending",
            "evidence": "Saldo dan jumlah dibandingkan dengan e-statement asli",
            **changes,
        }

    def test_import_does_not_certify_period_until_finance_reviews_reconciled_totals(
        self,
    ):
        source = self.imported()
        day = timezone.localdate()
        self.assertFalse(
            statements.statement_coverage(organization=self.org, start=day, end=day)[
                "verified"
            ]
        )
        preview = statements.preview_statement_period(**self.facts(source))
        self.assertFalse(preview["issues"])
        review = statements.review_statement_period(
            **self.facts(source),
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        coverage = statements.statement_coverage(
            organization=self.org, start=day, end=day
        )
        self.assertTrue(coverage["verified"])
        self.assertEqual(coverage["accounts"][0]["closing_balance"], "3000.25")
        self.assertEqual(coverage["accounts"][0]["review_ids"], [review.pk])
        self.assertFalse(Journal.objects.exists())
        self.assertEqual(
            AuditEvent.objects.filter(
                action="finance.statement.period_reviewed"
            ).count(),
            1,
        )

    def test_missing_transaction_stale_preview_and_changed_source_do_not_certify(self):
        source = self.imported()
        preview = statements.preview_statement_period(**self.facts(source))
        BankTransaction.objects.create(
            organization=self.org,
            account=self.account,
            date=timezone.localdate(),
            reference="MISSING-SOURCE",
            description="Newly found transaction",
            amount=Decimal(1),
        )
        with self.assertRaises(ValidationError):
            statements.review_statement_period(
                **self.facts(source),
                expected_fingerprint=preview["fingerprint"],
                confirmed=True,
            )
        self.assertFalse(
            statements.statement_coverage(
                organization=self.org,
                start=timezone.localdate(),
                end=timezone.localdate(),
            )["verified"]
        )

    def test_zero_activity_requires_explicit_empty_source_and_finance_review(self):
        response = self.client.post(
            reverse("bank_import"),
            {
                "account": self.account.pk,
                "file": SimpleUploadedFile(
                    "zero.csv", b"date,reference,description,amount\n"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        source = StatementImport.objects.get()
        empty_form = self.client.get(
            reverse("statement_preview", args=[source.pk])
        ).context["form"]
        self.assertFalse(self.preview(source).context["token"])
        preview = self.preview(
            source,
            no_transactions="on",
            first_row=empty_form["first_row"].value() or "",
            last_row=empty_form["last_row"].value()
            if empty_form["last_row"].value() is not None
            else "",
        )
        self.assertContains(preview, "Pratinjau 0 transaksi")
        self.assertTrue(preview.context["token"])
        self.assertEqual(
            self.confirm(source, preview.context["token"]).status_code, 302
        )
        source.refresh_from_db()
        facts = self.facts(
            source, closing_balance="2000.00", total_in="0", transaction_count=0
        )
        self.assertFalse(
            statements.statement_coverage(
                organization=self.org,
                start=timezone.localdate(),
                end=timezone.localdate(),
            )["verified"]
        )
        reviewed = statements.preview_statement_period(**facts)
        self.assertFalse(reviewed["issues"])
        statements.review_statement_period(
            **facts, expected_fingerprint=reviewed["fingerprint"], confirmed=True
        )
        self.assertTrue(
            statements.statement_coverage(
                organization=self.org,
                start=timezone.localdate(),
                end=timezone.localdate(),
            )["verified"]
        )
        self.assertFalse(BankTransaction.objects.exists())
        self.assertFalse(Journal.objects.exists())

    def test_nonempty_source_cannot_be_declared_zero_and_public_review_is_explicit_stale_safe(
        self,
    ):
        source = self.upload()
        self.assertFalse(self.preview(source, no_transactions="on").context["token"])
        self.confirm(source, self.preview(source).context["token"])
        data = {
            key: value
            for key, value in self.facts(source).items()
            if key not in {"organization", "actor", "statement"}
        }
        url = reverse("statement_period_review", args=[source.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, data)
        fingerprint = response.context["preview"]["fingerprint"]
        self.assertFalse(response.context["preview"]["issues"])
        self.client.post(
            url, {**data, "action": "confirm", "expected_fingerprint": fingerprint}
        )
        self.assertFalse(StatementPeriodReview.objects.exists())
        self.assertEqual(
            self.client.post(
                url,
                {
                    **data,
                    "action": "confirm",
                    "confirmed": "yes",
                    "expected_fingerprint": fingerprint,
                },
            ).status_code,
            302,
        )
        self.client.post(
            url,
            {
                **data,
                "action": "confirm",
                "confirmed": "yes",
                "expected_fingerprint": fingerprint,
            },
        )
        self.assertEqual(StatementPeriodReview.objects.count(), 1)
        self.assertContains(self.client.get(url), "Riwayat")

    def test_changed_source_invalidates_review_and_append_only_supersession_preserves_history(
        self,
    ):
        source = self.imported()
        facts = self.facts(source)
        preview = statements.preview_statement_period(**facts)
        review = statements.review_statement_period(
            **facts, expected_fingerprint=preview["fingerprint"], confirmed=True
        )
        day = timezone.localdate()
        old = statements.statement_coverage(organization=self.org, start=day, end=day)
        with source.file.open("rb") as handle:
            original = handle.read()
        with open(source.file.path, "wb") as handle:
            handle.write(original + b"changed")
        stale = statements.statement_coverage(organization=self.org, start=day, end=day)
        self.assertFalse(stale["verified"])
        self.assertNotEqual(stale["fingerprint"], old["fingerprint"])
        with open(source.file.path, "wb") as handle:
            handle.write(original)
        facts["evidence"] = "Checked again using original source; reference corrected"
        preview = statements.preview_statement_period(**facts)
        replacement = statements.review_statement_period(
            **facts,
            expected_fingerprint=preview["fingerprint"],
            supersedes=review,
            confirmed=True,
        )
        self.assertEqual(replacement.supersedes_id, review.pk)
        self.assertEqual(StatementPeriodReview.objects.count(), 2)
        self.assertEqual(
            statements.statement_coverage(organization=self.org, start=day, end=day)[
                "accounts"
            ][0]["review_ids"],
            [replacement.pk],
        )
        with self.assertRaises(ValidationError):
            review.delete()
        review.evidence = "overwrite"
        with self.assertRaises(ValidationError):
            review.save()
        with self.assertRaises(ValidationError):
            StatementPeriodReview.objects.filter(pk=review.pk).update(
                evidence="overwrite"
            )

    def test_control_mismatch_scope_revocation_and_coverage_gap_block_certification(
        self,
    ):
        source = self.imported()
        for changes in [
            {"transaction_count": 2},
            {"total_in": "1000.26"},
            {"closing_balance": "3001.25"},
        ]:
            facts = self.facts(source, **changes)
            preview = statements.preview_statement_period(**facts)
            self.assertTrue(preview["issues"])
            with self.assertRaises(ValidationError):
                statements.review_statement_period(
                    **facts, expected_fingerprint=preview["fingerprint"], confirmed=True
                )
        facts = self.facts(source)
        preview = statements.preview_statement_period(**facts)
        statements.review_statement_period(
            **facts, expected_fingerprint=preview["fingerprint"], confirmed=True
        )
        day = timezone.localdate()
        self.assertFalse(
            statements.statement_coverage(
                organization=self.org, start=day - timedelta(days=1), end=day
            )["verified"]
        )
        other = Organization.objects.create(name="Other statement org")
        with self.assertRaises(PermissionDenied):
            statements.preview_statement_period(
                **self.facts(source, organization=other)
            )
        Membership.all_objects.filter(pk=self.membership.pk).update(role="marketing")
        with self.assertRaises(PermissionDenied):
            statements.preview_statement_period(**facts)
        self.assertEqual(
            self.client.get(
                reverse("statement_period_review", args=[source.pk])
            ).status_code,
            403,
        )

    def ingest(self, csv_text):
        before = StatementImport.objects.count()
        response = self.client.post(
            reverse("bank_import"),
            {
                "account": self.account.pk,
                "file": SimpleUploadedFile("controls.csv", csv_text.encode()),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StatementImport.objects.count(), before + 1)
        source = StatementImport.objects.latest("pk")
        response = self.preview(source, balance="4")
        self.assertTrue(response.context["token"])
        self.confirm(source, response.context["token"])
        source.refresh_from_db()
        return source

    def test_running_balance_and_adjacent_balance_must_reconcile(self):
        first_day = timezone.localdate() - timedelta(days=1)
        first = self.ingest(
            f"date,reference,description,amount,balance\n{first_day},P1,Transfer,10,110\n"
        )
        facts = self.facts(
            first,
            period_start=first_day,
            period_end=first_day,
            opening_balance="100",
            closing_balance="110",
            total_in="10",
        )
        preview = statements.preview_statement_period(**facts)
        self.assertFalse(preview["issues"])
        statements.review_statement_period(
            **facts, expected_fingerprint=preview["fingerprint"], confirmed=True
        )
        second = self.ingest(
            f"date,reference,description,amount,balance\n{timezone.localdate()},P2,Transfer,20,1019\n"
        )
        facts = self.facts(
            second, opening_balance="999", closing_balance="1019", total_in="20"
        )
        preview = statements.preview_statement_period(**facts)
        self.assertFalse(preview["issues"])
        with self.assertRaisesMessage(ValidationError, "berkesinambungan"):
            statements.review_statement_period(
                **facts, expected_fingerprint=preview["fingerprint"], confirmed=True
            )
        mismatch = statements.preview_statement_period(
            **{**facts, "opening_balance": "110", "closing_balance": "130"}
        )
        self.assertTrue(any("Saldo berjalan" in issue for issue in mismatch["issues"]))
        self.assertEqual(StatementPeriodReview.objects.count(), 1)

    def test_all_accounts_must_be_covered_and_unauthorized_account_scope_is_rejected(
        self,
    ):
        source = self.imported()
        facts = self.facts(source)
        preview = statements.preview_statement_period(**facts)
        statements.review_statement_period(
            **facts, expected_fingerprint=preview["fingerprint"], confirmed=True
        )
        extra = BankAccount.objects.create(
            organization=self.org,
            name="Unreviewed secondary bank",
            account_number="SYNTHETIC-SECOND",
        )
        day = timezone.localdate()
        self.assertFalse(
            statements.statement_coverage(organization=self.org, start=day, end=day)[
                "verified"
            ]
        )
        self.assertTrue(
            statements.statement_coverage(
                organization=self.org, account=self.account, start=day, end=day
            )["verified"]
        )
        other = Organization.objects.create(name="Other bank org")
        with self.assertRaises(ValidationError):
            statements.statement_coverage(
                organization=other, account=extra, start=day, end=day
            )

    def test_close_controls_reconcile_exact_statement_closing_with_book_bank_balance(
        self,
    ):
        from .services import post_journal

        source = self.imported()
        day = timezone.localdate()
        facts = self.facts(source, period_start=day.replace(day=1))
        preview = statements.preview_statement_period(**facts)
        statements.review_statement_period(
            **facts, expected_fingerprint=preview["fingerprint"], confirmed=True
        )
        blockers = statements.statement_close_blockers(organization=self.org, end=day)
        self.assertTrue(any("buku besar" in message for message in blockers))
        post_journal(
            organization=self.org,
            actor=self.user,
            date=day,
            source_key="synthetic-bank-opening",
            description="Synthetic opening and observed cash",
            entries=[("BANK", "3000.25", "0"), ("EQUITY", "0", "3000.25")],
        )
        self.assertEqual(
            statements.statement_close_blockers(organization=self.org, end=day), []
        )
