"""Native PostgreSQL row-lock contracts; SQLite is not concurrency evidence."""

from datetime import date
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase

from . import services as ledger
from . import supplier_advances as advances
from . import tests_tax_money_concurrency as race_fixtures
from .models import (
    Journal,
    SupplierAdvance,
    SupplierAdvanceApplication,
    SupplierAdvanceRefund,
)
from .tests_supplier_advances import SupplierAdvanceFixtures


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class SupplierAdvanceRaceTests(SupplierAdvanceFixtures, TransactionTestCase):
    race = race_fixtures.TaxMoneyConcurrencyTests.race

    def test_two_cash_claims_have_one_bank_capacity(self):
        bank = self.movement("-100", reference="RACE-CAPTURE")
        fp = advances.supplier_advance_action_context(
            organization=self.org,
            action="record",
            supplier=self.supplier,
            transaction=bank,
        )["fingerprint"]
        self.assertCountEqual(
            self.race(
                *[
                    lambda: self.command(
                        "record",
                        supplier=self.supplier,
                        transaction=bank,
                        amount="100",
                        expected_fingerprint=fp,
                    )
                    for _ in range(2)
                ]
            ),
            ["committed", "rejected"],
        )
        self.assertEqual(
            (SupplierAdvance.objects.count(), bank.unallocated_amount), (1, 0)
        )

    def test_identical_retry_commits_one_event(self):
        bank = self.movement("-100", reference="RACE-IDENTICAL")
        data = dict(
            organization=self.org,
            actor=self.actor,
            supplier=self.supplier,
            transaction=bank,
            amount="100",
            source_reference="RACE-SAME",
            reason="Synthetic same reviewed request.",
            evidence="Synthetic same bank evidence.",
            request_key=str(uuid4()),
            confirmed=True,
            expected_fingerprint=advances.supplier_advance_action_context(
                organization=self.org,
                action="record",
                supplier=self.supplier,
                transaction=bank,
            )["fingerprint"],
        )
        self.assertEqual(
            self.race(
                lambda: advances.record_supplier_advance(**data),
                lambda: advances.record_supplier_advance(**data),
            ),
            ["committed", "committed"],
        )
        self.assertEqual(
            (SupplierAdvance.objects.count(), Journal.objects.count()), (1, 1)
        )

    def test_application_and_refund_cannot_spend_same_residual(self):
        row, bill = self.advance("100"), self.bill("100")
        bank = self.movement("100", day=date(2026, 1, 3), reference="RACE-RETURN")
        apply_fp = advances.supplier_advance_action_context(
            organization=self.org, action="apply", advance=row, bill=bill
        )["fingerprint"]
        refund_fp = advances.supplier_advance_action_context(
            organization=self.org, action="refund", advance=row, transaction=bank
        )["fingerprint"]
        self.assertCountEqual(
            self.race(
                lambda: self.command(
                    "apply",
                    advance=row,
                    bill=bill,
                    amount="100",
                    date=date(2026, 1, 3),
                    expected_fingerprint=apply_fp,
                ),
                lambda: self.command(
                    "refund",
                    advance=row,
                    transaction=bank,
                    amount="100",
                    expected_fingerprint=refund_fp,
                ),
            ),
            ["committed", "rejected"],
        )
        self.assertEqual(self.position(row)["remaining"], 0)
        self.assertEqual(
            SupplierAdvanceApplication.objects.count()
            + SupplierAdvanceRefund.objects.count(),
            1,
        )

    def test_application_and_ordinary_cash_cannot_overpay_bill(self):
        row, bill = self.advance("100"), self.bill("100")
        bank = self.movement("-100", day=date(2026, 1, 3), reference="RACE-ORDINARY")
        fp = advances.supplier_advance_action_context(
            organization=self.org, action="apply", advance=row, bill=bill
        )["fingerprint"]
        self.assertCountEqual(
            self.race(
                lambda: self.command(
                    "apply",
                    advance=row,
                    bill=bill,
                    amount="100",
                    date=date(2026, 1, 3),
                    expected_fingerprint=fp,
                ),
                lambda: ledger.reconcile_bill_payment(
                    organization=self.org,
                    actor=self.actor,
                    bill=bill,
                    transaction=bank,
                    amount="100",
                ),
            ),
            ["committed", "rejected"],
        )
        self.assertEqual(bill.outstanding_amount, 0)
        self.assertIn(self.position(row)["remaining"], (Decimal(0), Decimal(100)))

    def test_two_bills_compete_for_one_advance_residual(self):
        row = self.advance("100")
        bills = [self.bill("100"), self.bill("100")]
        fingerprints = [
            advances.supplier_advance_action_context(
                organization=self.org, action="apply", advance=row, bill=bill
            )["fingerprint"]
            for bill in bills
        ]
        actions = [
            lambda bill=bill, fp=fp: self.command(
                "apply",
                advance=row,
                bill=bill,
                amount="100",
                date=date(2026, 1, 3),
                expected_fingerprint=fp,
            )
            for bill, fp in zip(bills, fingerprints)
        ]
        self.assertCountEqual(self.race(*actions), ["committed", "rejected"])
        self.assertEqual(SupplierAdvanceApplication.objects.count(), 1)
        self.assertEqual(self.position(row)["remaining"], 0)

    def test_same_application_reversal_race_has_one_full_correction(self):
        row, bill = self.advance("100"), self.bill("100")
        application = self.command(
            "apply", advance=row, bill=bill, amount="100", date=date(2026, 1, 3)
        )
        fp = advances.supplier_advance_action_context(
            organization=self.org, action="reverse", application=application
        )["fingerprint"]
        self.assertCountEqual(
            self.race(
                *[
                    lambda: self.command(
                        "reverse",
                        application=application,
                        date=date(2026, 1, 4),
                        expected_fingerprint=fp,
                    )
                    for _ in range(2)
                ]
            ),
            ["committed", "rejected"],
        )
        self.assertEqual(
            (self.position(row)["remaining"], bill.outstanding_amount),
            (Decimal(100), Decimal(100)),
        )
