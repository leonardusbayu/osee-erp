"""Public synthetic contracts for a certificate received after an advance was applied."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from core.models import Membership, Organization
from taxes import workflow as work

from . import services as f
from .models import (
    AccountingPeriod,
    AdvanceApplication,
    Allocation,
    BankPosting,
    CustomerTaxSettlement,
    Journal,
)
from .settlement import applied_payment_amounts, collectible_amounts
from .tests_tax_money import TaxMoneyFixtures


class LateAdvanceFixtures(TaxMoneyFixtures):
    def advance_case(
        self,
        *,
        advance_amount="490000",
        application_amount="490000",
        bank_day=date(2026, 9, 9),
        invoice_day=date(2026, 9, 10),
        application_day=date(2026, 9, 11),
        tax_point_date=date(2026, 9, 9),
        tax_point_basis="due",
        base="500000",
        reference="ADVANCE",
    ):
        self.day = invoice_day
        bank = self.movement(advance_amount, day=bank_day, reference=reference)
        advance = f.record_customer_advance(
            organization=self.org,
            actor=self.actor,
            party=self.party,
            transaction=bank,
            amount=advance_amount,
            reference=reference,
            evidence="Synthetic bank payment received before the invoice",
        )
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        application = f.apply_customer_advance(
            organization=self.org,
            actor=self.actor,
            advance=advance,
            invoice=invoice,
            date=application_day,
            amount=application_amount,
            reference=f"{reference}-APPLY",
            evidence="Synthetic recorded advance applied to this invoice",
        )
        context = self.pph23_context(
            direction="receivable", tax_point_date=tax_point_date, invoice=invoice
        )
        context["tax_point_basis"] = tax_point_basis
        obligation = work.prepare_obligation(
            organization=self.org,
            actor=self.actor,
            period=tax_point_date.replace(day=1),
            rule_code="pph23_service_2",
            base=base,
            direction="receivable",
            source_reference=f"{reference}-TAX",
            rule_reference="Synthetic professional service classification",
            object_code="24-104-99",
            source_evidence=self.evidence(),
            source_invoice=invoice,
            **context,
        )
        obligation = work.approve_tax_obligation(
            obligation,
            self.actor,
            external_review=self.external(obligation, {"action": "approve_obligation"}),
        )
        self.credit(obligation)
        return invoice, advance, application, obligation, bank

    def advance_credit(self, obligation, application, **changes):
        kwargs = dict(
            organization=self.org,
            actor=self.actor,
            obligation=obligation,
            source_advance_application=application,
            date=date(2026, 10, 1),
            reference="LATE-ADVANCE-CERT",
            evidence="Synthetic certificate received after the recorded advance application",
        )
        kwargs.update(changes)
        return f.recognize_customer_tax_credit(**kwargs)


class LateAdvanceTests(LateAdvanceFixtures, TestCase):
    def test_preinvoice_advance_then_late_certificate_closes_ar_without_new_cash(self):
        invoice, advance, application, obligation, bank = self.advance_case()
        self.assertEqual(
            (invoice.paid_amount, invoice.outstanding_amount, advance.remaining_amount),
            (Decimal(490000), Decimal(10000), Decimal(0)),
        )
        original_journals = list(
            Journal.objects.filter(pk__in=[advance.journal_id, application.journal_id])
            .order_by("pk")
            .values("pk", "date", "source_key", "status")
        )
        original_lines = list(
            Journal.objects.filter(pk__in=[advance.journal_id, application.journal_id])
            .order_by("pk")
            .values_list("lines__account", "lines__debit", "lines__credit")
        )
        self.assertEqual(len(original_journals), 2)
        self.assertEqual(len(original_lines), 4)
        original_counts = (
            BankPosting.objects.count(),
            Allocation.objects.count(),
            AdvanceApplication.objects.count(),
        )
        row = self.advance_credit(obligation, application)
        self.assertEqual(
            (
                row.source_advance_application_id,
                row.source_allocation_id,
                row.bank_posting_id,
            ),
            (application.pk, None, None),
        )
        self.assertEqual(
            (row.cash_amount, row.tax_amount, row.gross_amount),
            (0, Decimal(10000), Decimal(10000)),
        )
        self.assertEqual(
            (
                invoice.paid_amount,
                invoice.outstanding_amount,
                advance.remaining_amount,
                bank.unallocated_amount,
            ),
            (Decimal(500000), 0, 0, 0),
        )
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"], Decimal(490000)
        )
        self.assertEqual(
            f.report_balances(organization=self.org)["CUSTOMER_ADVANCE"], 0
        )
        self.assertEqual(
            f.report_balances(organization=self.org)["TAX_RECEIVABLE"], Decimal(10000)
        )
        self.assertEqual(f.report_balances(organization=self.org)["AR"], 0)
        self.assertEqual(
            (
                BankPosting.objects.count(),
                Allocation.objects.count(),
                AdvanceApplication.objects.count(),
            ),
            original_counts,
        )
        self.assertEqual(
            list(
                Journal.objects.filter(
                    pk__in=[advance.journal_id, application.journal_id]
                )
                .order_by("pk")
                .values("pk", "date", "source_key", "status")
            ),
            original_journals,
        )
        self.assertEqual(
            list(
                Journal.objects.filter(
                    pk__in=[advance.journal_id, application.journal_id]
                )
                .order_by("pk")
                .values_list("lines__account", "lines__debit", "lines__credit")
            ),
            original_lines,
        )
        self.assertEqual(
            collectible_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=date(2026, 9, 30)
            )[invoice.pk],
            Decimal(10000),
        )
        self.assertEqual(
            applied_payment_amounts(
                organization=self.org, invoice_ids=[invoice.pk], as_of=date(2026, 10, 1)
            )[invoice.pk],
            Decimal(500000),
        )
        self.assertEqual(self.advance_credit(obligation, application).pk, row.pk)
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)

    def test_exact_one_source_scoped_actor_and_changed_retry_are_rejected(self):
        invoice, advance, application, obligation, bank = self.advance_case()
        before = Journal.objects.count()
        for changes in (
            {"source_advance_application": None},
            {"source_allocation": application.pk},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.advance_credit(obligation, application, **changes)
        marketing = get_user_model().objects.create_user(
            username="late-advance-marketing"
        )
        Membership.objects.create(
            organization=self.org, user=marketing, role="marketing"
        )
        with self.assertRaises(PermissionDenied):
            self.advance_credit(obligation, application, actor=marketing)
        foreign = Organization.objects.create(name="Synthetic foreign advance credit")
        Membership.objects.create(organization=foreign, user=self.actor, role="owner")
        with self.assertRaises(ValidationError):
            self.advance_credit(obligation, application, organization=foreign)
        self.assertEqual(Journal.objects.count(), before)
        row = self.advance_credit(obligation, application)
        for changes in (
            {"reference": "CHANGED"},
            {"evidence": "Changed synthetic instruction"},
            {"date": date(2026, 10, 2)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.advance_credit(obligation, application, **changes)
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)
        self.assertEqual(self.advance_credit(obligation, application).pk, row.pk)

    def test_original_bank_tax_point_and_application_date_are_required(self):
        invoice, advance, application, obligation, bank = self.advance_case(
            tax_point_basis="paid"
        )
        with self.assertRaises(ValidationError):
            self.advance_credit(obligation, application, date=date(2026, 9, 10))
        self.advance_credit(obligation, application)
        for basis, point in (
            ("paid", date(2026, 9, 8)),
            ("available", date(2026, 9, 10)),
            ("due", date(2026, 9, 10)),
        ):
            with self.subTest(basis=basis):
                invoice, advance, application, obligation, bank = self.advance_case(
                    tax_point_basis=basis,
                    tax_point_date=point,
                    reference=f"POINT-{basis}",
                )
                with self.assertRaisesMessage(ValidationError, "waktu terutang"):
                    self.advance_credit(
                        obligation, application, reference=f"CREDIT-{basis}"
                    )
                self.assertEqual(invoice.outstanding_amount, Decimal(10000))

    def test_completed_retry_precedes_freshness_and_closed_period_guards(self):
        invoice, advance, application, obligation, bank = self.advance_case()
        row = self.advance_credit(obligation, application)
        AccountingPeriod.objects.update_or_create(
            organization=self.org,
            year=2026,
            month=10,
            defaults={"closed": True, "closed_at": timezone.now()},
        )
        from taxes.recipient_identity import assert_pph23_credit_verification

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE taxes_taxevidence SET content=%s WHERE id=%s",
                [b"Synthetic damaged certificate", row.verification.evidence_id],
            )
        with self.assertRaises(ValidationError):
            assert_pph23_credit_verification(row.verification, obligation)
        self.assertEqual(self.advance_credit(obligation, application).pk, row.pk)
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)
        self.assertEqual(
            Journal.objects.filter(
                source_key=f"customer-tax-credit:{obligation.pk}"
            ).count(),
            1,
        )

    def test_closed_source_period_does_not_reopen_for_new_credit(self):
        invoice, advance, application, obligation, bank = self.advance_case()
        AccountingPeriod.objects.update_or_create(
            organization=self.org,
            year=2026,
            month=9,
            defaults={"closed": True, "closed_at": timezone.now()},
        )
        with self.assertRaises(ValidationError):
            self.advance_credit(obligation, application, date=date(2026, 9, 30))
        row = self.advance_credit(obligation, application)
        self.assertEqual(row.date, date(2026, 10, 1))
        self.assertTrue(
            AccountingPeriod.objects.get(
                organization=self.org, year=2026, month=9
            ).closed
        )

    def test_partial_application_uses_exact_invoice_certificate_without_proration(self):
        invoice, advance, application, obligation, bank = self.advance_case(
            application_amount="200000"
        )
        row = self.advance_credit(obligation, application)
        self.assertEqual(
            (row.tax_amount, invoice.outstanding_amount, advance.remaining_amount),
            (Decimal(10000), Decimal(290000), Decimal(290000)),
        )
        second = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        second_application = f.apply_customer_advance(
            organization=self.org,
            actor=self.actor,
            advance=advance,
            invoice=second,
            date=date(2026, 9, 11),
            amount="290000",
            reference="ADVANCE-SECOND-APPLY",
            evidence="Synthetic remaining advance applied to a second invoice",
        )
        self.day = date(2026, 9, 9)
        second_obligation = self.approved(invoice=second)
        self.credit(second_obligation)
        second_row = self.advance_credit(
            second_obligation, second_application, reference="SECOND-LATE-CERT"
        )
        self.assertEqual(
            second_row.source_advance_application.advance_id,
            row.source_advance_application.advance_id,
        )
        self.assertEqual(
            (second.outstanding_amount, advance.remaining_amount), (Decimal(200000), 0)
        )
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"], Decimal(490000)
        )
        self.assertEqual(CustomerTaxSettlement.objects.count(), 2)

    def test_current_price_changes_do_not_replace_frozen_invoice_or_credit(self):
        invoice, advance, application, obligation, bank = self.advance_case()
        self.product.default_price = Decimal(530000)
        self.product.save()
        row = self.advance_credit(obligation, application)
        invoice.refresh_from_db()
        self.assertEqual(
            (invoice.total, row.tax_amount, invoice.paid_amount),
            (Decimal(500000), Decimal(10000), Decimal(500000)),
        )

    def test_cancelled_or_service_credited_invoice_requires_tax_correction(self):
        invoice, advance, application, obligation, bank = self.advance_case()
        f.cancel_invoice(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            date=date(2026, 9, 12),
            reason="Synthetic unfulfilled order cancellation",
        )
        with self.assertRaisesMessage(ValidationError, "koreksi"):
            self.advance_credit(obligation, application)
        self.assertFalse(CustomerTaxSettlement.objects.exists())

    def test_credit_residual_and_service_correction_cannot_overlap(self):
        from .fulfillment import credit_units

        invoice, advance, application, obligation, bank = self.advance_case()
        f.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement("9000", reference="EXTRA-CASH"),
            amount="9000",
        )
        with self.assertRaisesMessage(ValidationError, "sisa invoice"):
            self.advance_credit(obligation, application)
        self.assertEqual(invoice.outstanding_amount, Decimal(1000))
        credit_units(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            quantity=1,
            kind="unfulfilled",
            date=date(2026, 10, 1),
            reference="SERVICE-CREDIT",
            evidence="Synthetic recorded undelivered unit correction",
        )
        with self.assertRaisesMessage(ValidationError, "koreksi"):
            self.advance_credit(obligation, application)
        second, advance, application, obligation, bank = self.advance_case(
            reference="ACTIVE-ADVANCE"
        )
        self.advance_credit(obligation, application, reference="ACTIVE-CERT")
        with self.assertRaises(ValidationError):
            f.cancel_invoice(
                organization=self.org,
                actor=self.actor,
                invoice=second,
                date=date(2026, 10, 1),
                reason="Synthetic post-certificate cancellation",
            )
        with self.assertRaises(ValidationError):
            credit_units(
                organization=self.org,
                actor=self.actor,
                invoice=second,
                quantity=1,
                kind="unfulfilled",
                date=date(2026, 10, 1),
                reference="LATE-SERVICE-CREDIT",
                evidence="Synthetic post-certificate unit correction",
            )
