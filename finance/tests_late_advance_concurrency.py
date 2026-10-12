"""PostgreSQL acceptance for late advance credits; SQLite is not lock evidence."""

from datetime import date
from decimal import Decimal
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from . import services as f
from . import tests_tax_money_concurrency as concurrency_fixtures
from .models import AdvanceApplication, BankPosting, CustomerTaxSettlement, Journal
from .tests_late_advance import LateAdvanceFixtures


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class LateAdvanceConcurrencyTests(LateAdvanceFixtures, TransactionTestCase):
    race = concurrency_fixtures.TaxMoneyConcurrencyTests.race

    def test_identical_retry_commits_one_credit_event_and_journal(self):
        invoice, advance, application, obligation, bank = self.advance_case()
        self.assertEqual(
            self.race(
                lambda: self.advance_credit(obligation, application),
                lambda: self.advance_credit(obligation, application),
            ),
            ["committed", "committed"],
        )
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)
        self.assertEqual(
            Journal.objects.filter(
                source_key=f"customer-tax-credit:{obligation.pk}"
            ).count(),
            1,
        )
        self.assertEqual(
            (invoice.paid_amount, advance.remaining_amount, bank.unallocated_amount),
            (Decimal(500000), 0, 0),
        )
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"], Decimal(490000)
        )

    def test_advance_allocation_and_net_receipt_compete_for_one_certificate(self):
        invoice, advance, application, obligation, bank = self.advance_case(
            application_amount="200000"
        )
        allocation = f.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement("100000", reference="RACE-OLD-CASH"),
            amount="100000",
        )
        net_bank = self.movement("190000", reference="RACE-NEW-CASH")
        outcomes = self.race(
            lambda: self.advance_credit(
                obligation, application, reference="RACE-ADVANCE"
            ),
            lambda: f.recognize_customer_tax_credit(
                organization=self.org,
                actor=self.actor,
                obligation=obligation,
                source_allocation=allocation,
                date=date(2026, 10, 1),
                reference="RACE-ALLOCATION",
                evidence="Synthetic alternative old cash proof",
            ),
            lambda: self.net_receipt(
                obligation, amount="190000", reference="RACE-NET", bank=net_bank
            ),
        )
        self.assertCountEqual(outcomes, ["committed", "rejected", "rejected"])
        row = CustomerTaxSettlement.objects.get()
        self.assertEqual(invoice.paid_amount, Decimal(300000) + row.gross_amount)
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"],
            Decimal(590000) + row.cash_amount,
        )
        self.assertEqual(
            f.report_balances(organization=self.org)["TAX_RECEIVABLE"], Decimal(10000)
        )
        self.assertEqual(AdvanceApplication.objects.count(), 1)

    def test_remaining_advance_application_and_credit_cannot_overpay_ar(self):
        invoice, advance, application, obligation, bank = self.advance_case(
            advance_amount="500000"
        )
        outcomes = self.race(
            lambda: self.advance_credit(obligation, application),
            lambda: f.apply_customer_advance(
                organization=self.org,
                actor=self.actor,
                advance=advance,
                invoice=invoice,
                date=date(2026, 10, 1),
                amount="10000",
                reference="RACE-RESIDUAL-APPLY",
                evidence="Synthetic application of the actual remaining advance",
            ),
        )
        self.assertCountEqual(outcomes, ["committed", "rejected"])
        self.assertEqual(
            (invoice.paid_amount, invoice.outstanding_amount), (Decimal(500000), 0)
        )
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"], Decimal(500000)
        )
        if CustomerTaxSettlement.objects.exists():
            self.assertEqual(
                (AdvanceApplication.objects.count(), advance.remaining_amount),
                (1, Decimal(10000)),
            )
        else:
            self.assertEqual(
                (AdvanceApplication.objects.count(), advance.remaining_amount), (2, 0)
            )
        self.assertEqual(BankPosting.objects.count(), 1)

    def test_cancel_and_credit_have_one_consistent_winner(self):
        invoice, advance, application, obligation, bank = self.advance_case()
        outcomes = self.race(
            lambda: self.advance_credit(obligation, application),
            lambda: f.cancel_invoice(
                organization=self.org,
                actor=self.actor,
                invoice=invoice,
                date=date(2026, 10, 1),
                reason="Synthetic concurrent unfulfilled order cancellation",
            ),
        )
        self.assertCountEqual(outcomes, ["committed", "rejected"])
        invoice.refresh_from_db()
        balances = f.report_balances(organization=self.org)
        if CustomerTaxSettlement.objects.exists():
            self.assertEqual(invoice.status, "issued")
            self.assertEqual(
                (balances["TAX_RECEIVABLE"], balances["CUSTOMER_REFUND"]),
                (Decimal(10000), 0),
            )
        else:
            self.assertEqual(invoice.status, "cancelled")
            self.assertEqual(
                (balances["TAX_RECEIVABLE"], balances["CUSTOMER_REFUND"]),
                (0, Decimal(-490000)),
            )
        self.assertEqual(balances["AR"], 0)
        self.assertEqual(balances["BANK"], Decimal(490000))

    def test_service_credit_and_tax_credit_have_one_consistent_winner(self):
        from .fulfillment import credit_units

        invoice, advance, application, obligation, bank = self.advance_case()
        outcomes = self.race(
            lambda: self.advance_credit(obligation, application),
            lambda: credit_units(
                organization=self.org,
                actor=self.actor,
                invoice=invoice,
                quantity=1,
                kind="unfulfilled",
                date=date(2026, 10, 1),
                reference="RACE-UNIT-CREDIT",
                evidence="Synthetic concurrent unfulfilled unit correction",
            ),
        )
        self.assertCountEqual(outcomes, ["committed", "rejected"])
        balances = f.report_balances(organization=self.org)
        if CustomerTaxSettlement.objects.exists():
            self.assertFalse(invoice.unit_credits.exists())
            self.assertEqual(
                (balances["TAX_RECEIVABLE"], balances["CUSTOMER_REFUND"]),
                (Decimal(10000), 0),
            )
        else:
            self.assertEqual(invoice.unit_credits.count(), 1)
            self.assertEqual(
                (balances["TAX_RECEIVABLE"], balances["CUSTOMER_REFUND"]),
                (0, Decimal(-490000)),
            )
        self.assertEqual((balances["BANK"], balances["AR"]), (Decimal(490000), 0))
