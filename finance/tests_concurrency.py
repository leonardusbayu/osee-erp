"""Actual concurrent service calls on PostgreSQL, isolated by Django's test DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from core.models import AuditEvent
from . import services as f
from .models import Allocation, Journal
from . import tests_remediation as fixtures


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locks")
class ConcurrentFinanceTests(TransactionTestCase):
    setUp = fixtures.FinanceRemediationTests.setUp
    invoice = fixtures.FinanceRemediationTests.invoice
    movement = fixtures.FinanceRemediationTests.movement

    def race(self, *actions):
        ready = Barrier(len(actions))

        def execute(action):
            close_old_connections()
            try:
                self.assertTrue(connections["default"].settings_dict["NAME"].startswith("test_"))
                ready.wait(timeout=10)
                try:
                    action()
                    return "committed"
                except ValidationError:
                    return "rejected"
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=len(actions)) as workers:
            futures = [workers.submit(execute, action) for action in actions]
            return [future.result(timeout=20) for future in futures]

    def test_two_receipts_cannot_oversubscribe_the_same_bank_row(self):
        invoices = [f.issue_invoice(organization=self.org, actor=self.actor, invoice=self.invoice(number=f"RACE-{n}")) for n in range(2)]
        bank = self.movement("500000")
        actions = [lambda inv=inv: f.reconcile_receipt(organization=self.org, actor=self.actor,
                    invoice=inv, transaction=bank, amount="500000") for inv in invoices]
        self.assertCountEqual(self.race(*actions), ["committed", "rejected"])
        self.assertEqual(Allocation.objects.count(), 1)
        self.assertEqual(bank.unallocated_amount, 0)
        self.assertEqual(f.report_balances(organization=self.org)["BANK"], Decimal("500000"))
        self.assertEqual(f.report_balances(organization=self.org)["AR"], Decimal("500000"))
        self.assertEqual(Journal.objects.count(), 3)

    def test_close_and_earlier_posting_are_serialized_and_close_snapshot_stays_true(self):
        results = self.race(
            lambda: f.close_month(organization=self.org, actor=self.actor, year=2026, month=2),
            lambda: f.post_journal(organization=self.org, actor=self.actor, date=date(2026, 1, 10),
                source_key="racing-earlier-post", description="Synthetic reviewed earlier posting",
                entries=[("BANK", "100", "0"), ("REVENUE", "0", "100")]),
        )
        self.assertEqual(results[0], "committed")
        self.assertIn(results[1], ("committed", "rejected"))
        snapshot = AuditEvent.objects.get(organization=self.org, action="finance.month.closed").detail["balances"]
        report = f.monthly_summary(organization=self.org, year=2026, month=2)
        self.assertTrue(report["closed"])
        self.assertEqual({key: Decimal(value) for key, value in snapshot.items()}, report["balances"])
        with self.assertRaises(ValidationError):
            f.post_journal(organization=self.org, actor=self.actor, date=date(2026, 1, 11),
                source_key="after-race", description="Must reject after close",
                entries=[("BANK", "1", "0"), ("REVENUE", "0", "1")])
