"""Synthetic public Finance journeys for the read-only monthly source bridge."""

import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfWriter

from core.models import Membership, Organization
from finance import fulfillment
from finance import services as finance
from finance import tests_remediation as fixtures
from finance import tests_tax_money as tax_money_fixtures
from finance.models import Invoice, Journal, JournalLine
from imports import tests as import_fixtures


class MonthlySourceTests(TestCase):
    setUp = fixtures.FinanceRemediationTests.setUp
    invoice = fixtures.FinanceRemediationTests.invoice
    movement = fixtures.FinanceRemediationTests.movement

    def extract(self, period):
        from .monthly_sources import build_monthly_sources

        return build_monthly_sources(self.org, period)

    def test_issued_order_prepaid_next_month_and_test_third_month_have_distinct_facts(
        self,
    ):
        invoice = finance.issue_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=self.invoice(service_date=date(2026, 3, 10)),
        )
        january = self.extract("2026-01")
        receipt = finance.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement(day=date(2026, 2, 10)),
            amount="500000",
        )
        february = self.extract("2026-02")
        delivery = fulfillment.deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            date=date(2026, 3, 10),
            reference="MARCH-TEST",
            evidence="Synthetic attendance",
        )
        march = self.extract("2026-03")
        self.assertEqual(
            [january["book_revenue"], february["book_revenue"], march["book_revenue"]],
            ["0.00", "0.00", "500000.00"],
        )
        candidates = [row for row in march["rows"] if row["role"] == "candidate"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["matched_event"], {"kind": "delivery", "id": delivery.pk}
        )
        self.assertEqual(candidates[0]["invoice_id"], invoice.pk)
        receipt_row = next(
            row for row in february["rows"] if row["row_id"] == f"receipt:{receipt.pk}"
        )
        self.assertEqual(receipt_row["cash_amount"], "500000.00")
        self.assertEqual(receipt_row["role"], "context")
        self.assertIsNone(march["taxable_turnover"])
        self.assertEqual(march["inventory"]["completeness"], "unknown")
        self.assertEqual(
            january, self.extract("2026-01")
        )  # Later payment/delivery cannot rewrite January.
        self.assertEqual(february, self.extract("2026-02"))
        json.dumps(march)  # Portable value snapshot; no live ORM/Decimal/date values.
        before = (
            Invoice.objects.count(),
            Journal.objects.count(),
            JournalLine.objects.count(),
        )
        self.extract("2026-03")
        self.assertEqual(
            before,
            (
                Invoice.objects.count(),
                Journal.objects.count(),
                JournalLine.objects.count(),
            ),
        )

    def issued_paid(self, *, quantity=1):
        invoice = finance.issue_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=self.invoice(quantity=quantity),
        )
        finance.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement(str(quantity * 500000)),
            amount=str(quantity * 500000),
        )
        return invoice

    def test_partial_delivery_credit_then_refund_never_recognizes_or_reduces_revenue_twice(
        self,
    ):
        invoice = self.issued_paid(quantity=2)
        fulfillment.deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            date=self.day,
            reference="PART-1",
            evidence="Synthetic attendance",
        )
        january = self.extract("2026-01")
        fulfillment.deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            date=date(2026, 2, 10),
            reference="PART-2",
            evidence="Synthetic attendance two",
        )
        february = self.extract("2026-02")
        credit = fulfillment.credit_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            kind="service",
            date=date(2026, 3, 10),
            reference="RETURN-1",
            evidence="Synthetic owner correction",
        )
        march = self.extract("2026-03")
        refund = fulfillment.refund_credit(
            organization=self.org,
            actor=self.actor,
            credit=credit,
            transaction=self.movement(
                "-500000", day=date(2026, 4, 10), reference="REFUND"
            ),
            amount="500000",
            reference="REFUND-1",
            evidence="Synthetic refund bank evidence",
        )
        april = self.extract("2026-04")
        self.assertEqual(
            [data["book_revenue"] for data in (january, february, march, april)],
            ["500000.00", "500000.00", "-500000.00", "0.00"],
        )
        march_candidate = next(
            row for row in march["rows"] if row["role"] == "candidate"
        )
        self.assertEqual(
            march_candidate["matched_event"],
            {"kind": "invoice-credit", "id": credit.pk},
        )
        self.assertIn("revenue_debit", march_candidate["review_flags"])
        cash_refund = next(
            row
            for row in april["rows"]
            if row["row_id"] == f"credit-refund:{refund.pk}"
        )
        self.assertEqual(
            (cash_refund["cash_amount"], cash_refund["revenue_effect"]),
            ("-500000.00", "0.00"),
        )
        self.assertEqual(january, self.extract("2026-01"))
        self.assertEqual(february, self.extract("2026-02"))
        self.assertEqual(march, self.extract("2026-03"))
        self.assertEqual(
            self.extract("2026-05")["invoices"], []
        )  # Settled historical orders are not repeated forever.

    def test_price_snapshot_legacy_delivery_and_offsetting_manual_revenue_remain_traceable(
        self,
    ):
        invoice = self.issued_paid()
        finance.record_delivery(
            organization=self.org,
            invoice=invoice,
            date=self.day,
            evidence="Synthetic legacy completion",
        )
        january = self.extract("2026-01")
        self.product.default_price = Decimal(530000)
        self.product.save()
        self.assertEqual(january, self.extract("2026-01"))
        candidate = next(row for row in january["rows"] if row["role"] == "candidate")
        self.assertEqual(
            candidate["matched_event"], {"kind": "legacy_delivery", "id": invoice.pk}
        )
        self.assertEqual(january["invoices"][0]["unit_price"], "500000.00")
        finance.post_journal(
            organization=self.org,
            actor=self.actor,
            date=self.day,
            source_key="manual-positive",
            description="Synthetic manual revenue",
            entries=[("BANK", "100", "0"), ("REVENUE", "0", "100")],
        )
        finance.post_journal(
            organization=self.org,
            actor=self.actor,
            date=self.day,
            source_key="manual-negative",
            description="Synthetic reversal",
            entries=[("REVENUE", "100", "0"), ("BANK", "0", "100")],
        )
        result = self.extract("2026-01")
        self.assertEqual(result["book_revenue"], "500000.00")
        orphan_rows = [
            row
            for row in result["rows"]
            if "orphan_revenue" in row.get("review_flags", [])
        ]
        self.assertEqual({row["amount"] for row in orphan_rows}, {"100.00", "-100.00"})
        self.assertEqual(len(orphan_rows), 2)
        self.assertNotEqual(january["source_hash"], result["source_hash"])

    def test_multiline_matching_journal_is_not_accepted_even_when_total_matches_event(
        self,
    ):
        invoice = self.issued_paid()
        delivery = fulfillment.deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            date=self.day,
            reference="SPLIT",
            evidence="Synthetic attendance",
        )
        # Explicit database-corruption probe: normal public services forbid edits
        # to a posted journal. Preserve its sum but split the revenue line.
        original = JournalLine.objects.get(journal=delivery.journal, account="REVENUE")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE finance_journalline SET credit = %s WHERE id = %s",
                ["400000", original.pk],
            )
            cursor.execute(
                "INSERT INTO finance_journalline (organization_id, journal_id, account, debit, credit, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    self.org.pk,
                    delivery.journal_id,
                    "REVENUE",
                    "0",
                    "100000",
                    "2026-01-10 00:00:00",
                ],
            )
        result = self.extract("2026-01")
        candidates = [row for row in result["rows"] if row["role"] == "candidate"]
        self.assertEqual(result["book_revenue"], "500000.00")
        self.assertEqual(len(candidates), 2)
        self.assertTrue(
            all(
                "revenue_multiline" in row["review_flags"]
                and not row["proposed_inclusion"]
                for row in candidates
            )
        )
        self.assertTrue(
            any(
                item["code"] == "revenue_multiline" and item["hard_blocker"]
                for item in result["issues"]
            )
        )

    def test_orphan_revenue_still_validates_entire_journal_balance_and_counterline_scope(
        self,
    ):
        first = finance.post_journal(
            organization=self.org,
            actor=self.actor,
            date=self.day,
            source_key="orphan-unbalanced",
            description="Synthetic manual amount",
            entries=[("BANK", "100", "0"), ("REVENUE", "0", "100")],
        )
        second = finance.post_journal(
            organization=self.org,
            actor=self.actor,
            date=self.day,
            source_key="orphan-foreign-counterline",
            description="Synthetic manual amount two",
            entries=[("BANK", "100", "0"), ("REVENUE", "0", "100")],
        )
        valid = self.extract("2026-01")
        self.assertFalse(any(item["hard_blocker"] for item in valid["issues"]))
        other = Organization.objects.create(
            name="Synthetic foreign journal counterline"
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE finance_journalline SET debit = %s WHERE journal_id = %s AND account = %s",
                ["50", first.pk, "BANK"],
            )
            cursor.execute(
                "UPDATE finance_journalline SET organization_id = %s WHERE journal_id = %s AND account = %s",
                [other.pk, second.pk, "BANK"],
            )
        changed = self.extract("2026-01")
        self.assertEqual(changed["book_revenue"], "200.00")
        candidates = [row for row in changed["rows"] if row["role"] == "candidate"]
        first_row = next(row for row in candidates if row["journal_id"] == first.pk)
        second_row = next(row for row in candidates if row["journal_id"] == second.pk)
        self.assertIn("source_journal_unbalanced", first_row["review_flags"])
        self.assertIn("source_scope", second_row["review_flags"])
        self.assertTrue(any(item["hard_blocker"] for item in changed["issues"]))
        self.assertNotEqual(valid["source_hash"], changed["source_hash"])

    def test_customer_advance_application_cancellation_and_refund_are_context_only(
        self,
    ):
        advance = finance.record_customer_advance(
            organization=self.org,
            actor=self.actor,
            party=self.party,
            transaction=self.movement(),
            amount="500000",
            reference="ADV",
            evidence="Synthetic advance",
        )
        invoice = finance.issue_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=self.invoice(
                date=date(2026, 2, 10), service_date=date(2026, 3, 10)
            ),
        )
        apply = finance.apply_customer_advance(
            organization=self.org,
            actor=self.actor,
            advance=advance,
            invoice=invoice,
            date=date(2026, 2, 10),
            amount="500000",
            reference="APPLY",
            evidence="Synthetic allocation",
        )
        february = self.extract("2026-02")
        self.assertEqual(february["book_revenue"], "0.00")
        application = next(
            row
            for row in february["rows"]
            if row["row_id"] == f"advance-application:{apply.pk}"
        )
        self.assertEqual(application["cash_amount"], "0.00")
        self.assertTrue(
            any(
                row["row_id"] == f"customer-advance:{advance.pk}"
                for row in february["rows"]
            )
        )
        finance.cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            date=date(2026, 3, 10),
            reason="Synthetic whole unperformed cancellation",
        )
        finance.refund_cancelled_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement(
                "-500000", day=date(2026, 4, 10), reference="RETURN-ADV"
            ),
            amount="500000",
            reference="REFUND-ADV",
            evidence="Synthetic refund",
        )
        april = self.extract("2026-04")
        self.assertEqual(april["book_revenue"], "0.00")
        self.assertEqual(
            [
                row["cash_amount"]
                for row in april["rows"]
                if row["source_kind"] == "refund"
            ],
            ["-500000.00"],
        )
        self.assertEqual(february, self.extract("2026-02"))

    def pdf(self):
        output = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(output)
        return SimpleUploadedFile(
            "synthetic.pdf", output.getvalue(), content_type="application/pdf"
        )

    def test_new_invoice_evidence_and_tampered_bytes_invalidate_source_identity(self):
        from evidence.services import attach_document

        invoice = self.issued_paid()
        previous = self.extract("2026-01")
        doc = attach_document(
            organization=self.org, actor=self.actor, invoice=invoice, upload=self.pdf()
        )
        intact = self.extract("2026-01")
        self.assertNotEqual(previous["source_hash"], intact["source_hash"])
        self.assertEqual(intact["evidence_manifest"][0]["integrity"], "verified")
        Path(doc.file.path).write_bytes(b"changed synthetic source")
        changed = self.extract("2026-01")
        self.assertNotEqual(intact["source_hash"], changed["source_hash"])
        self.assertTrue(
            any(
                item["code"] == "evidence_integrity" and item["hard_blocker"]
                for item in changed["issues"]
            )
        )

    def test_unreviewed_bank_upload_for_selected_month_stales_inventory_but_future_upload_does_not(
        self,
    ):
        before = self.extract("2026-01")
        self.client.force_login(self.actor)
        response = self.client.post(
            reverse("bank_import"),
            {
                "account": self.account.pk,
                "file": SimpleUploadedFile(
                    "january.csv",
                    b"date,reference,description,amount\n2026-01-10,NEW,Synthetic,100\n",
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        january = self.extract("2026-01")
        self.assertNotEqual(before["source_hash"], january["source_hash"])
        observations = january["inventory"]["observations"]
        self.assertEqual(observations[0]["kind"], "unreviewed_statement")
        response = self.client.post(
            reverse("bank_import"),
            {
                "account": self.account.pk,
                "file": SimpleUploadedFile(
                    "february.csv",
                    b"date,reference,description,amount\n2026-02-10,FUTURE,Synthetic,100\n",
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(january, self.extract("2026-01"))

    def test_other_company_does_not_change_snapshot_and_input_period_is_strict(self):
        before = self.extract("2026-01")
        other = Organization.objects.create(name="Synthetic other company")
        Membership.objects.create(organization=other, user=self.actor, role="owner")
        finance.post_journal(
            organization=other,
            actor=self.actor,
            date=self.day,
            source_key="FOREIGN",
            description="Other income",
            entries=[("BANK", "123", "0"), ("REVENUE", "0", "123")],
        )
        self.assertEqual(before, self.extract("2026-01"))
        for value in (True, "2026-13", "2026-1", date(2026, 1, 2)):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.extract(value)

    def test_staged_registration_inventory_stales_month_without_becoming_a_sale(self):
        from operations.intake import map_batch, stage_upload

        before = self.extract("2026-01")
        batch = stage_upload(
            organization=self.org,
            actor=self.actor,
            source="Synthetic registration",
            filename="january.csv",
            content=b"name,planned_date\nSynthetic participant,2026-01-10\n",
        )
        staged = self.extract("2026-01")
        self.assertNotEqual(before["source_hash"], staged["source_hash"])
        self.assertEqual(staged["book_revenue"], "0.00")
        self.assertEqual(
            staged["inventory"]["observations"][0]["kind"], "registration_intake"
        )
        map_batch(
            organization=self.org,
            actor=self.actor,
            batch=batch,
            mapping={"name": "0", "planned_date": "1"},
            expected_version=batch.version,
        )
        mapped = self.extract("2026-01")
        self.assertNotEqual(staged["source_hash"], mapped["source_hash"])
        stage_upload(
            organization=self.org,
            actor=self.actor,
            source="Synthetic later registration",
            filename="november.csv",
            content=b"name,planned_date\nSynthetic future participant,2026-11-10\n",
        )
        self.assertEqual(mapped, self.extract("2026-01"))

    def test_revenue_and_refund_corrupted_scope_or_future_actual_dates_are_hard_blockers(
        self,
    ):
        invoice = self.issued_paid()
        delivery = fulfillment.deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            date=self.day,
            reference="SCOPE-TEST",
            evidence="Synthetic attendance",
        )
        credit = fulfillment.credit_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            kind="service",
            date=self.day,
            reference="SCOPE-CREDIT",
            evidence="Synthetic owner correction",
        )
        refund = fulfillment.refund_credit(
            organization=self.org,
            actor=self.actor,
            credit=credit,
            transaction=self.movement("-500000", reference="SCOPE-REFUND"),
            amount="500000",
            reference="SCOPE-REFUND",
            evidence="Synthetic refund",
        )
        with patch(
            "taxes.monthly_sources.timezone.localdate", return_value=date(2026, 1, 1)
        ):
            result = self.extract("2026-01")
        self.assertTrue(
            any(
                item["code"] == "future_actual_source" and item["hard_blocker"]
                for item in result["issues"]
            )
        )
        other = Organization.objects.create(name="Synthetic scope corruption")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE finance_invoicecredit SET organization_id = %s WHERE id = %s",
                [other.pk, credit.pk],
            )
            cursor.execute(
                "UPDATE finance_journalline SET organization_id = %s WHERE journal_id = %s AND account = %s",
                [other.pk, delivery.journal_id, "REVENUE"],
            )
        changed = self.extract("2026-01")
        self.assertTrue(
            any(
                item["code"] == "source_scope"
                and item["row_id"] == f"credit-refund:{refund.pk}"
                and item["hard_blocker"]
                for item in changed["issues"]
            )
        )
        self.assertTrue(
            any(
                item["code"] == "source_scope"
                and str(item["row_id"]).startswith("revenue-line:")
                for item in changed["issues"]
            )
        )

    def test_cancelled_invoice_refund_rejects_corrupted_foreign_origin_and_prior_date(
        self,
    ):
        invoice = self.issued_paid()
        finance.cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            date=self.day,
            reason="Synthetic cancellation",
        )
        refund = finance.refund_cancelled_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement("-500000", reference="CANCEL-REFUND"),
            amount="500000",
            reference="CANCEL-REFUND",
            evidence="Synthetic refund",
        )
        other = Organization.objects.create(name="Synthetic refund foreign origin")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE finance_invoicecancellation SET organization_id = %s WHERE id = %s",
                [other.pk, invoice.cancellation.pk],
            )
        data = self.extract("2026-01")
        self.assertTrue(
            any(
                row["code"] == "source_scope"
                and row["row_id"] == f"refund:{refund.pk}"
                and row["hard_blocker"]
                for row in data["issues"]
            )
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE finance_invoicecancellation SET organization_id = %s WHERE id = %s",
                [self.org.pk, invoice.cancellation.pk],
            )
            cursor.execute(
                "UPDATE finance_customerrefund SET date = %s WHERE id = %s",
                ["2026-01-09", refund.pk],
            )
        data = self.extract("2026-01")
        self.assertTrue(
            any(
                row["code"] == "source_scope" and row["row_id"] == f"refund:{refund.pk}"
                for row in data["issues"]
            )
        )

    def test_reviewed_statement_file_is_evidence_and_changed_bytes_are_a_hard_blocker(
        self,
    ):
        from finance.test_statement_fixtures import reviewed_statement

        review = reviewed_statement(
            self,
            organization=self.org,
            actor=self.actor,
            account=self.account,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
        )
        intact = self.extract("2026-01")
        observation = next(
            row
            for row in intact["inventory"]["observations"]
            if row["kind"] == "statement_review"
        )
        self.assertEqual(observation["id"], review.pk)
        self.assertEqual(intact["evidence_manifest"][0]["integrity"], "verified")
        Path(review.statement.file.path).write_bytes(b"changed synthetic statement")
        tampered = self.extract("2026-01")
        self.assertNotEqual(intact["source_hash"], tampered["source_hash"])
        self.assertTrue(
            any(
                item["code"] == "evidence_integrity" and item["hard_blocker"]
                for item in tampered["issues"]
            )
        )


class MonthlyTaxReceiptSources(tax_money_fixtures.TaxMoneyFixtures, TestCase):
    def extract(self):
        from .monthly_sources import build_monthly_sources

        return build_monthly_sources(self.org, "2026-09")

    def test_net_customer_tax_cash_and_gross_settlement_do_not_discount_invoice_revenue(
        self,
    ):
        self.profile()
        invoice = finance.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        obligation = self.approved(invoice=invoice)
        self.credit(obligation)
        settlement = self.net_receipt(obligation)
        fulfillment.deliver_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            date=self.day,
            reference="TAX-NET-DELIVERY",
            evidence="Synthetic completed register",
        )
        data = self.extract()
        self.assertEqual(data["book_revenue"], "500000.00")
        row = next(
            row
            for row in data["rows"]
            if row["row_id"] == f"customer-tax:{settlement.pk}"
        )
        self.assertEqual(
            (row["cash_amount"], row["tax_amount"], row["gross_amount"]),
            ("490000.00", "10000.00", "500000.00"),
        )
        self.assertEqual(data["invoices"][0]["unit_price"], "500000.00")
        self.assertFalse(any(issue["hard_blocker"] for issue in data["issues"]))

    def test_preparing_own_tax_and_recognition_payment_does_not_self_stale_income_package(
        self,
    ):
        before = self.extract()
        obligation = self.final_obligation()
        recognition = self.recognition(obligation)
        self.payment(recognition)
        self.assertEqual(before, self.extract())

    def test_late_customer_certificate_changes_only_gross_settlement_month_not_original_cash(
        self,
    ):
        from taxes.monthly_sources import build_monthly_sources

        self.profile()
        invoice = finance.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        obligation = self.approved(invoice=invoice)
        bank = self.movement("490000")
        allocation = finance.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=bank,
            amount="490000",
        )
        september = self.extract()
        self.credit(obligation)
        settlement = finance.recognize_customer_tax_credit(
            organization=self.org,
            actor=self.actor,
            obligation=obligation,
            source_allocation=allocation,
            date=date(2026, 10, 1),
            reference="LATE-CERT",
            evidence="Synthetic October recovery",
        )
        october = build_monthly_sources(self.org, "2026-10")
        self.assertEqual(september, self.extract())
        row = next(
            row
            for row in october["rows"]
            if row["row_id"] == f"customer-tax:{settlement.pk}"
        )
        self.assertEqual(
            (row["cash_amount"], row["tax_amount"], row["gross_amount"]),
            ("0.00", "10000.00", "10000.00"),
        )
        self.assertEqual(october["invoices"][0]["gross_settled"], "500000.00")
        self.assertEqual(october["invoices"][0]["cash_received"], "490000.00")


class MonthlyImportedSourceTests(TestCase):
    setUp = import_fixtures.SourceImportTests.setUp
    write_bundle = import_fixtures.SourceImportTests.write_bundle
    run_import = import_fixtures.SourceImportTests.run_import

    def extract(self):
        from .monthly_sources import build_monthly_sources

        return build_monthly_sources(self.org, "2026-01")

    def test_recap_observations_and_review_evidence_stale_sources_without_posting_sales(
        self,
    ):
        from imports.review_services import review_exception

        before = self.extract()
        batch = self.run_import()
        imported = self.extract()
        self.assertNotEqual(before["source_hash"], imported["source_hash"])
        self.assertEqual(imported["book_revenue"], "0.00")
        self.assertEqual(imported["rows"], [])
        observation = next(
            row
            for row in imported["inventory"]["observations"]
            if row["kind"] == "monthly_import"
        )
        self.assertEqual(observation["printed_amount"], "100.00")
        self.assertEqual(imported["evidence_manifest"][0]["integrity"], "verified")
        review_exception(
            organization=self.org,
            actor=self.owner,
            exception=batch.exceptions.get(),
            expected_previous_id=None,
            decision="resolved",
            responsible_user=self.owner,
            reason="Synthetic year checked against original",
            evidence="Synthetic recap and registration",
            correction="Interpret the row as January 2026; retain original 2025 value",
            source_document=batch.source_documents.get(),
        )
        reviewed = self.extract()
        self.assertNotEqual(imported["source_hash"], reviewed["source_hash"])
        self.assertEqual(reviewed["book_revenue"], "0.00")
        self.assertEqual(reviewed["inventory"]["completeness"], "unknown")
        Path(batch.source_documents.get().file.path).write_bytes(
            b"changed synthetic recap"
        )
        changed = self.extract()
        self.assertTrue(
            any(
                item["code"] == "evidence_integrity" and item["hard_blocker"]
                for item in changed["issues"]
            )
        )
