"""Real PostgreSQL races; SQLite is deliberately not lock evidence."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from . import services as f
from .models import CustomerTaxSettlement, OwnTaxPayment, OwnTaxRecognition
from .tests_tax_money import TaxMoneyFixtures


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class TaxMoneyConcurrencyTests(TaxMoneyFixtures, TransactionTestCase):
    def race(self, *actions):
        barrier = Barrier(len(actions))

        def execute(action):
            close_old_connections()
            try:
                self.assertTrue(
                    connections["default"].settings_dict["NAME"].startswith("test_")
                )
                barrier.wait(timeout=10)
                try:
                    action()
                    return "committed"
                except ValidationError:
                    return "rejected"
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            futures = [pool.submit(execute, action) for action in actions]
            return [future.result(timeout=30) for future in futures]

    def test_one_certificate_cannot_settle_two_bank_receipts_concurrently(self):
        self.profile()
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        row = self.approved(invoice=invoice)
        self.credit(row)
        banks = [self.movement("490000", reference=f"RACE-{n}") for n in range(2)]
        actions = [
            lambda bank=bank: self.net_receipt(
                row, bank=bank, reference=f"RACE-{bank.pk}"
            )
            for bank in banks
        ]
        self.assertCountEqual(self.race(*actions), ["committed", "rejected"])
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)
        self.assertEqual(invoice.paid_amount, Decimal(500000))
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"], Decimal(490000)
        )

    def test_net_receipt_and_late_credit_cannot_consume_one_certificate_twice(self):
        self.profile()
        invoice = f.issue_invoice(
            organization=self.org, actor=self.actor, invoice=self.invoice()
        )
        row = self.approved(invoice=invoice)
        self.credit(row)
        allocation = f.reconcile_receipt(
            organization=self.org,
            actor=self.actor,
            invoice=invoice,
            transaction=self.movement("200000", reference="OLD-CASH"),
            amount="200000",
        )
        extra_cash = self.movement("290000", reference="NEW-CASH")
        actions = [
            lambda: self.net_receipt(
                row, amount="290000", reference="NET-RACE", bank=extra_cash
            ),
            lambda: f.recognize_customer_tax_credit(
                organization=self.org,
                actor=self.actor,
                obligation=row,
                source_allocation=allocation,
                date=self.day,
                reference="LATE-RACE",
                evidence="Synthetic late certificate",
            ),
        ]
        self.assertCountEqual(self.race(*actions), ["committed", "rejected"])
        self.assertEqual(CustomerTaxSettlement.objects.count(), 1)
        self.assertEqual(
            f.report_balances(organization=self.org)["TAX_RECEIVABLE"], Decimal(10000)
        )
        row = CustomerTaxSettlement.objects.get()
        self.assertEqual(invoice.paid_amount, Decimal(200000) + row.gross_amount)
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"],
            Decimal(200000) + row.cash_amount,
        )

    def test_two_own_payments_cannot_overspend_one_recognized_liability(self):
        row = self.recognition(self.final_obligation())
        banks = [self.movement("-5000", reference=f"RACE-{n}") for n in range(2)]
        actions = [
            lambda bank=bank: self.payment(
                row, amount="5000", bank=bank, reference=f"RACE-{bank.pk}"
            )
            for bank in banks
        ]
        self.assertCountEqual(self.race(*actions), ["committed", "rejected"])
        self.assertEqual(OwnTaxPayment.objects.count(), 1)
        self.assertEqual(row.remaining_amount, 0)
        self.assertEqual(
            f.report_balances(organization=self.org)["BANK"], Decimal(-5000)
        )

    def test_two_recognitions_cannot_book_the_same_final_period_twice(self):
        row = self.final_obligation()
        actions = [
            lambda n=n: f.recognize_own_tax(
                organization=self.org,
                actor=self.actor,
                obligation=row,
                date=self.day,
                reference=f"RACE-{n}",
                evidence="Synthetic reviewed recognition",
            )
            for n in range(2)
        ]
        self.assertCountEqual(self.race(*actions), ["committed", "rejected"])
        self.assertEqual(OwnTaxRecognition.objects.count(), 1)
        self.assertEqual(
            f.report_balances(organization=self.org)["OWN_TAX_PAYABLE"], Decimal(-5000)
        )
